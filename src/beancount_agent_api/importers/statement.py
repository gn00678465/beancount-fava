"""beangulp importer for 交易明細 exported by online banking.

The bank layouts are read in layouts.py; this module turns a Statement into
transactions and balance assertions and marks the rows the ledger already has.
The title of the export names the bank, so the ledger's import config passes it in.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from decimal import Decimal

import beangulp
from beancount.core import data, flags
from beancount.core.amount import Amount
from beangulp.extract import DUPLICATE

from beancount_agent_api.importers.layouts import Row, Statement, is_statement, parse

# The bank gives no transaction number. The time, amount and running balance
# together identify one row, so a re-imported row matches the one already saved.
ID_KEY = "statement-id"
BOOKED_KEY = "booking-date"
# A stock trade is often entered by hand on the trade day while the bank debits
# it days later; the daily balance assertions catch a pair this window misses.
MATCH_DAYS = 7


class StatementImporter(beangulp.Importer):
    """Turn each row into a transaction between `account` and a matched account.

    `title` is the text of the export's first row; it tells this export apart.
    `rules` is a list of (text, account) pairs. The first pair whose text occurs
    in the row's description or note gives the other account; a transfer between
    two of the owner's accounts is matched by the other account's number. A row
    no rule matches goes to `fallback_account` with the `!` flag, so it stands
    out on the import page.
    """

    def __init__(
        self,
        account: str,
        title: str,
        rules: Sequence[tuple[str, str]],
        fallback_account: str,
        currency: str = "TWD",
    ) -> None:
        self._account = account
        self._title = title
        self._rules = list(rules)
        self._fallback = fallback_account
        self._currency = currency

    @property
    def name(self) -> str:
        # fava refuses two importers with one name; each account gets its own.
        return f"statement:{self._account}"

    def identify(self, filepath: str) -> bool:
        try:
            return is_statement(filepath, self._title)
        except Exception:  # noqa: BLE001
            # fava lists every uploaded file through identify and one exception
            # fails the whole import page, so a file this cannot read is not ours.
            return False

    def account(self, filepath: str) -> str:
        return self._account

    def _transaction(self, filepath: str, row: Row) -> data.Transaction:
        text = f"{row.summary} {row.note}"
        other = next((account for needle, account in self._rules if needle in text), None)
        units = Amount(row.amount, self._currency)
        meta = data.new_metadata(
            filepath, row.line, {ID_KEY: f"{row.time} {row.amount} {row.balance}"}
        )
        if row.booked != row.date:
            meta[BOOKED_KEY] = row.booked
        # deduplicate reads the first posting back to tell this account's saved rows apart.
        postings = [
            data.Posting(self._account, units, None, None, None, None),
            data.Posting(other or self._fallback, -units, None, None, None, None),
        ]
        flag = flags.FLAG_OKAY if other else flags.FLAG_WARNING
        narration = row.note or row.summary
        return data.Transaction(
            meta, row.date, flag, None, narration, data.EMPTY_SET, data.EMPTY_SET, postings
        )

    def _balances(self, filepath: str, statement: Statement) -> list[data.Balance]:
        # The bank's balance at the start of every day with rows, so a row saved
        # twice or skipped fails the assertion on the day it happened.
        if not statement.rows:
            return []
        first = statement.rows[0]
        starts = {first.date: (first.line, first.balance - first.amount)}
        for row in statement.rows:
            # The export day may get rows after the export, so its close is unknown.
            if row.date < statement.exported:
                starts[row.date + datetime.timedelta(days=1)] = (row.line, row.balance)
        return [
            data.Balance(
                data.new_metadata(filepath, line),
                day,
                self._account,
                Amount(balance, self._currency),
                None,
                None,
            )
            for day, (line, balance) in starts.items()
        ]

    def extract(self, filepath: str, existing: data.Entries) -> data.Entries:
        statement = parse(filepath, self._title)
        entries: data.Entries = [self._transaction(filepath, row) for row in statement.rows]
        return entries + self._balances(filepath, statement)

    def _cash(self, entry: data.Transaction) -> Decimal | None:
        units = [p.units for p in entry.postings if p.account == self._account and p.units]
        if not units or any(u.currency != self._currency for u in units):
            return None
        return sum((u.number for u in units), Decimal(0))

    def _saved_here(self, entry: data.Transaction) -> bool:
        meta = entry.meta or {}
        return (
            ID_KEY in meta and bool(entry.postings) and entry.postings[0].account == self._account
        )

    def deduplicate(self, entries: data.Entries, existing: data.Entries) -> None:
        """Mark rows and balances the ledger already has; fava skips them on save.

        A row saved by an earlier import of this account matches by its id. Any
        other transaction (entered by hand, through the agent API, or imported
        from another account's statement as a transfer to this one) matches a row
        when its postings on the account sum to the row's amount within
        MATCH_DAYS days.
        Each such transaction matches one row at most, the nearest in date, so two
        rows of one amount on one day are not both taken for one saved entry.
        """
        txns = [e for e in existing if isinstance(e, data.Transaction)]
        ids = {e.meta[ID_KEY] for e in txns if self._saved_here(e)}
        balances = {(e.date, e.account, e.amount) for e in existing if isinstance(e, data.Balance)}
        by_amount: dict[Decimal, list[data.Transaction]] = {}
        for e in txns:
            # A pad's filler transaction is not a saved row of any statement.
            if e.flag != flags.FLAG_PADDING and not self._saved_here(e):
                cash = self._cash(e)
                if cash is not None:
                    by_amount.setdefault(cash, []).append(e)
        pairs = []
        for i, entry in enumerate(entries):
            if isinstance(entry, data.Balance):
                if (entry.date, entry.account, entry.amount) in balances:
                    entry.meta[DUPLICATE] = True
            elif entry.meta[ID_KEY] in ids:
                entry.meta[DUPLICATE] = True
            else:
                for old in by_amount.get(entry.postings[0].units.number, []):
                    days = abs((old.date - entry.date).days)
                    if days <= MATCH_DAYS:
                        pairs.append((days, i, id(old)))
        rows_taken: set[int] = set()
        saved_taken: set[int] = set()
        for _, i, old in sorted(pairs):
            if i not in rows_taken and old not in saved_taken:
                rows_taken.add(i)
                saved_taken.add(old)
                entries[i].meta[DUPLICATE] = True
