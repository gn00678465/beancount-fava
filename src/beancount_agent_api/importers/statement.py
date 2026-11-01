"""beangulp importer for a 交易明細 xlsx exported by online banking.

The export is one sheet: a title row, a header row, one row per transaction in
ascending order, and a footer row with the row count and the export time. The
title names the bank, so the ledger's import config passes it in.
"""

from __future__ import annotations

import datetime
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import beangulp
from beancount.core import data, flags
from beancount.core.amount import Amount
from beangulp.extract import DUPLICATE

HEADER = ("交易日", "帳務日", "摘要", "金額", "餘額", "備註")
COLUMNS = "ABCDEF"
# The bank gives no transaction number. The time, amount and running balance
# together identify one row, so a re-imported row matches the one already saved.
ID_KEY = "statement-id"
BOOKED_KEY = "booking-date"
SHEET = "xl/worksheets/sheet1.xml"
# A stock trade is often entered by hand on the trade day while the bank debits
# it days later; the daily balance assertions catch a pair this window misses.
MATCH_DAYS = 7
_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_FOOTER = re.compile(r"共(\d+)筆資料\s*資料日期：(\d{4}/\d{2}/\d{2})(?: \d{2}:\d{2}:\d{2})?")


@dataclass(frozen=True)
class Row:
    time: str
    date: datetime.date
    booked: datetime.date
    summary: str
    amount: Decimal
    balance: Decimal
    note: str


@dataclass(frozen=True)
class Statement:
    rows: list[Row]
    exported: datetime.date


def _read_rows(path: str) -> list[list[str]]:
    with zipfile.ZipFile(path) as archive:
        sheet = ET.fromstring(archive.read(SHEET))
    rows = []
    for row in sheet.iter(f"{_M}row"):
        cells = dict.fromkeys(COLUMNS, "")
        for cell in row.iter(f"{_M}c"):
            column = cell.get("r", "").rstrip("0123456789")
            if column not in cells:
                continue
            if cell.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(f"{_M}t"))
            elif cell.get("t") is None:
                text = cell.findtext(f"{_M}v") or ""
            else:
                raise ValueError(f"{cell.get('r')}: unsupported cell type {cell.get('t')!r}")
            cells[column] = text.strip()
        rows.append([cells[c] for c in COLUMNS])
    return rows


def _date(text: str) -> datetime.date:
    return datetime.date.fromisoformat(text.replace("/", "-"))


def _decimal(text: str, where: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{where}: {text!r} is not a number") from None


def _is_export(rows: list[list[str]], title: str) -> bool:
    return len(rows) >= 2 and rows[0][0] == title and tuple(rows[1]) == HEADER


def parse(path: str, title: str) -> Statement:
    """Read the statement and check that its rows are complete and in order."""
    rows = _read_rows(path)
    if len(rows) < 3 or not _is_export(rows, title):
        raise ValueError(f"{path} is not a {title!r} export")
    footer = _FOOTER.fullmatch(rows[-1][0])
    if footer is None:
        raise ValueError(f"row {len(rows)}: expected the 共N筆資料 footer, got {rows[-1][0]!r}")
    parsed = []
    for line, (time, date, summary, amount, balance, note) in enumerate(rows[2:-1], start=3):
        # The running balance follows the 交易日 order, so the transaction is
        # dated by it; 帳務日 can fall on a later business day.
        row = Row(
            time=time,
            date=_date(time.split(" ")[0]),
            booked=_date(date),
            summary=summary,
            amount=_decimal(amount, f"D{line}"),
            balance=_decimal(balance, f"E{line}"),
            note=note,
        )
        # Each balance must follow from the previous one; a gap means a lost or reordered row.
        if parsed and parsed[-1].balance + row.amount != row.balance:
            raise ValueError(
                f"row {line}: {parsed[-1].balance} + {row.amount} is not the balance {row.balance}"
            )
        parsed.append(row)
    if len(parsed) != int(footer[1]):
        raise ValueError(f"the footer counts {footer[1]} rows, the sheet has {len(parsed)}")
    return Statement(parsed, _date(footer[2]))


class StatementImporter(beangulp.Importer):
    """Turn each row into a transaction between `account` and a matched account.

    `title` is the text of the export's first row; it tells this export apart.
    `rules` is a list of (text, account) pairs. The first pair whose text occurs
    in the row's 摘要 or 備註 gives the other account. A row no rule matches goes
    to `fallback_account` with the `!` flag, so it stands out on the import page.
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
            rows = _read_rows(filepath)
        except Exception:  # noqa: BLE001
            # fava lists every uploaded file through identify and one exception
            # fails the whole import page, so a file this cannot read is not ours.
            return False
        return _is_export(rows, self._title)

    def account(self, filepath: str) -> str:
        return self._account

    def _transaction(self, filepath: str, line: int, row: Row) -> data.Transaction:
        text = f"{row.summary} {row.note}"
        other = next((account for needle, account in self._rules if needle in text), None)
        units = Amount(row.amount, self._currency)
        meta = data.new_metadata(filepath, line, {ID_KEY: f"{row.time} {row.amount} {row.balance}"})
        if row.booked != row.date:
            meta[BOOKED_KEY] = row.booked
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
        starts = {first.date: (3, first.balance - first.amount)}
        for line, row in enumerate(statement.rows, start=3):
            # The export day may get rows after the export, so its close is unknown.
            if row.date < statement.exported:
                starts[row.date + datetime.timedelta(days=1)] = (line, row.balance)
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
        entries: data.Entries = [
            self._transaction(filepath, line, row)
            for line, row in enumerate(statement.rows, start=3)
        ]
        return entries + self._balances(filepath, statement)

    def _cash(self, entry: data.Transaction) -> Decimal | None:
        units = [p.units for p in entry.postings if p.account == self._account and p.units]
        if not units or any(u.currency != self._currency for u in units):
            return None
        return sum((u.number for u in units), Decimal(0))

    def deduplicate(self, entries: data.Entries, existing: data.Entries) -> None:
        """Mark rows and balances the ledger already has; fava skips them on save.

        A row saved by an earlier import matches by its id. A transaction without
        an id (entered by hand or through the agent API) matches a row when its
        postings on the account sum to the row's amount within MATCH_DAYS days.
        Each such transaction matches one row at most, the nearest in date, so two
        rows of one amount on one day are not both taken for one saved entry.
        """
        ids = {e.meta[ID_KEY] for e in existing if ID_KEY in (e.meta or {})}
        balances = {(e.date, e.account, e.amount) for e in existing if isinstance(e, data.Balance)}
        by_amount: dict[Decimal, list[data.Transaction]] = {}
        for e in existing:
            # A pad's filler transaction is not a saved row of any statement.
            saved = isinstance(e, data.Transaction) and e.flag != flags.FLAG_PADDING
            if saved and ID_KEY not in (e.meta or {}):
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
