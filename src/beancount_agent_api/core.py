from __future__ import annotations

import datetime
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from beancount.core import account as account_name
from beancount.core import amount as beancount_amount
from beancount.core.data import Close, Open, Posting, Transaction
from beancount.core.position import CostSpec
from fava.beans import create
from fava.beans.funcs import hash_entry


@dataclass(frozen=True)
class Account:
    name: str
    open_date: datetime.date
    close_date: datetime.date | None
    currencies: frozenset[str]
    # The currencies the Open directive itself lists; empty when it lists none. `currencies`
    # falls back to the operating currencies, which says nothing about a stock commodity.
    declared: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Accounts:
    by_name: Mapping[str, Account]
    lookup: Mapping[str, tuple[str, ...]]
    default_currency: str | None
    # The root of expense accounts, from option "name_expenses".
    expenses_root: str

    @classmethod
    def from_directives(
        cls,
        opens: Iterable[Open],
        closes: Iterable[Close],
        operating_currencies: Sequence[str],
        expenses_root: str,
    ) -> Accounts:
        close_dates = {close.account: close.date for close in closes}
        by_name: dict[str, Account] = {}
        keys: dict[str, set[str]] = {}
        for open_ in opens:
            name = open_.account
            if name in by_name:
                continue
            by_name[name] = Account(
                name=name,
                open_date=open_.date,
                close_date=close_dates.get(name),
                currencies=frozenset(open_.currencies or operating_currencies),
                declared=frozenset(open_.currencies or ()),
            )
            aliases = {name}
            name_zh = open_.meta.get("name-zh")
            if isinstance(name_zh, str) and name_zh:
                aliases |= {name_zh, name_zh.rsplit("/", 1)[-1]}
            for alias in aliases:
                keys.setdefault(alias, set()).add(name)
        return cls(
            by_name=by_name,
            lookup={key: tuple(sorted(names)) for key, names in keys.items()},
            default_currency=operating_currencies[0] if operating_currencies else None,
            expenses_root=expenses_root,
        )


@dataclass(frozen=True)
class Target:
    account: str
    amount: Decimal


@dataclass(frozen=True)
class AddRequest:
    date: datetime.date
    time: str | None
    source: str
    targets: tuple[Target, ...]
    currency: str
    narration: str
    payee: str | None
    tags: frozenset[str]
    meta: tuple[tuple[str, str], ...]
    key: str
    dry_run: bool

    @property
    def link(self) -> str:
        return f"ik-{self.key}"


@dataclass(frozen=True)
class TradeRequest:
    date: datetime.date
    time: str | None
    source: str
    holdings: str
    # None when the trade has no fee.
    fees_account: str | None
    commodity: str
    units: Decimal
    price: Decimal
    fee: Decimal
    currency: str
    narration: str
    payee: str | None
    tags: frozenset[str]
    meta: tuple[tuple[str, str], ...]
    key: str
    dry_run: bool

    @property
    def link(self) -> str:
        return f"ik-{self.key}"

    @property
    def cost(self) -> Decimal:
        return self.units * self.price

    @property
    def total(self) -> Decimal:
        return _plain(self.cost + self.fee)


@dataclass(frozen=True)
class Rejection:
    field: str
    code: str
    message: str
    candidates: tuple[str, ...] = ()


def _is_string(value: object) -> bool:
    return isinstance(value, str)


def _is_string_array(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_string_object(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(item, str) for item in value.values())


def _is_boolean(value: object) -> bool:
    return isinstance(value, bool)


def _is_array(value: object) -> bool:
    return isinstance(value, list)


# Each request field with its JSON type check and the type name that a rejection shows.
FIELD_TYPES: Mapping[str, tuple[Callable[[object], bool], str]] = {
    "date": (_is_string, "string"),
    "time": (_is_string, "string"),
    "source": (_is_string, "string"),
    "target": (_is_string, "string"),
    "amount": (_is_string, "string"),
    "targets": (_is_array, "array"),
    "currency": (_is_string, "string"),
    "narration": (_is_string, "string"),
    "payee": (_is_string, "string"),
    "tags": (_is_string_array, "array of strings"),
    "meta": (_is_string_object, "object with string values"),
    "key": (_is_string, "string"),
    "dry_run": (_is_boolean, "boolean"),
}
FIELDS = tuple(FIELD_TYPES)
TRADE_FIELD_TYPES: Mapping[str, tuple[Callable[[object], bool], str]] = {
    "date": (_is_string, "string"),
    "time": (_is_string, "string"),
    "source": (_is_string, "string"),
    "holdings": (_is_string, "string"),
    "commodity": (_is_string, "string"),
    "units": (_is_string, "string"),
    "price": (_is_string, "string"),
    "total": (_is_string, "string"),
    "fee": (_is_string, "string"),
    "fees_account": (_is_string, "string"),
    "currency": (_is_string, "string"),
    "narration": (_is_string, "string"),
    "payee": (_is_string, "string"),
    "tags": (_is_string_array, "array of strings"),
    "meta": (_is_string_object, "object with string values"),
    "key": (_is_string, "string"),
    "dry_run": (_is_boolean, "boolean"),
}
TRADE_FIELDS = tuple(TRADE_FIELD_TYPES)
# 'total' and 'fee' are alternatives; parse_trade_request checks that pair itself.
TRADE_REQUIRED = ("source", "holdings", "commodity", "units", "price", "key")
UNITS_RE = re.compile(r"[1-9][0-9]{0,11}")
PRICE_RE = re.compile(r"[0-9]{1,9}(\.[0-9]{1,4})?")
CENT = Decimal("0.01")
# Past this the amounts leave AMOUNT_RE's 15 integer digits.
AMOUNT_LIMIT = Decimal(10) ** 15
# 'targets' replaces 'target' and 'amount'; parse_add_request checks that pair itself.
REQUIRED = ("source", "key")
MAX_TARGETS = 20
TARGET_FIELDS = ("account", "amount")
# The link charset of beancount's lexer (lexer.l).
KEY_RE = re.compile(r"[A-Za-z0-9\-_/.]+")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
TIME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})"
)
# Explicit [0-9] and fullmatch: \d accepts fullwidth digits and $ accepts a trailing newline.
# Past 28 significant digits the default Decimal context rounds -amount, and the entry no
# longer balances.
AMOUNT_RE = re.compile(r"[0-9]{1,15}(\.[0-9]{1,2})?")
CURRENCY_RE = re.compile(beancount_amount.CURRENCY_RE)
# beancount's printer escapes only `"` and `\`, so a line break stays literal inside the quoted
# narration; fava's align then rewrites it, and a `\r` becomes `\n` on fava's next file rewrite.
# A lone surrogate fails UTF-8 encoding after fava has truncated the file for its rewrite.
NARRATION_FORBIDDEN = frozenset({"Cc", "Cs", "Zl", "Zp"})
# The metadata key charset of beancount's lexer (lexer.l).
META_KEY_RE = re.compile(r"[a-z][a-zA-Z0-9\-_]*")
# The API writes `time` from the 'time' field, and beancount sets filename and lineno on load.
RESERVED_META_KEYS = frozenset({"time", "filename", "lineno"})
# fava's reports compute date + 1 day, so an entry on date.max breaks them for every user.
MAX_DAYS_AHEAD = 366


def parse_add_request(
    raw: object, accounts: Accounts, tz: datetime.tzinfo, today: datetime.date
) -> AddRequest | Rejection:
    if (rejection := _check_fields(raw, FIELD_TYPES)) is not None:
        return rejection
    assert isinstance(raw, dict)
    if "targets" in raw:
        for field in ("target", "amount"):
            if field in raw:
                return Rejection(
                    field,
                    "target_or_targets",
                    "Send either 'target' and 'amount', or 'targets', not both.",
                )
    for field in ("source", "target", "amount", "key"):
        if field not in raw and (field in REQUIRED or "targets" not in raw):
            return Rejection(field, "missing", f"Send {field!r}.")
    # Each target as (account field, account text, amount field, amount text); the single
    # target keeps its own field names in rejections.
    if "targets" in raw:
        pairs = _target_pairs(raw["targets"])
        if isinstance(pairs, Rejection):
            return pairs
    else:
        pairs = [("target", raw["target"], "amount", raw["amount"])]

    key = _parse_key(raw)
    if isinstance(key, Rejection):
        return key

    when = _parse_when(raw, tz, today)
    if isinstance(when, Rejection):
        return when
    date, local_time = when

    for _, _, amount_field, amount_text in pairs:
        if AMOUNT_RE.fullmatch(amount_text) is None or Decimal(amount_text) <= 0:
            return Rejection(
                amount_field,
                "invalid_amount",
                f"Send {amount_field!r} as a positive decimal string with at most 2 decimals, "
                'such as "190".',
            )

    currency = _parse_currency(raw, accounts)
    if isinstance(currency, Rejection):
        return currency

    note = _parse_note(raw)
    if isinstance(note, Rejection):
        return note

    source = _resolve("source", raw["source"], date, accounts)
    if isinstance(source, Rejection):
        return source
    targets = []
    for account_field, account_text, _, amount_text in pairs:
        account = _resolve(account_field, account_text, date, accounts)
        if isinstance(account, Rejection):
            return account
        # The split form records the expenses of one receipt; spending lists only these.
        if "targets" in raw and account.name.split(":", 1)[0] != accounts.expenses_root:
            return Rejection(
                account_field,
                "not_expense_account",
                f"{account.name} is not under {accounts.expenses_root}; each item in 'targets' "
                "must be an expense account.",
            )
        if account.name == source.name:
            return Rejection(
                account_field,
                "same_account",
                f"'source' and {account_field!r} both resolve to {source.name}.",
            )
        if any(account.name == target.name for target, _ in targets):
            return Rejection(
                account_field,
                "duplicate_account",
                f"{account.name} is already a target; send each account once.",
            )
        targets.append((account, Decimal(amount_text)))

    for account in (source, *(target for target, _ in targets)):
        if currency not in account.currencies:
            return Rejection(
                "currency",
                "currency_not_allowed",
                f"{account.name} accepts only {', '.join(sorted(account.currencies))}.",
            )

    return AddRequest(
        date=date,
        time=local_time,
        source=source.name,
        targets=tuple(Target(account.name, amount) for account, amount in targets),
        currency=currency,
        narration=note.narration,
        payee=note.payee,
        tags=note.tags,
        meta=note.meta,
        key=key,
        dry_run=raw.get("dry_run", False),
    )


def parse_trade_request(
    raw: object, accounts: Accounts, tz: datetime.tzinfo, today: datetime.date
) -> TradeRequest | Rejection:
    if (rejection := _check_fields(raw, TRADE_FIELD_TYPES)) is not None:
        return rejection
    assert isinstance(raw, dict)
    for field in TRADE_REQUIRED:
        if field not in raw:
            return Rejection(field, "missing", f"Send {field!r}.")
    if ("total" in raw) == ("fee" in raw):
        return Rejection("total", "total_or_fee", "Send exactly one of 'total' or 'fee'.")

    key = _parse_key(raw)
    if isinstance(key, Rejection):
        return key

    when = _parse_when(raw, tz, today)
    if isinstance(when, Rejection):
        return when
    date, local_time = when

    if CURRENCY_RE.fullmatch(raw["commodity"]) is None:
        return Rejection(
            "commodity",
            "invalid_commodity",
            f"{raw['commodity']!r} is not a commodity code, such as TWETF1.",
        )
    if UNITS_RE.fullmatch(raw["units"]) is None:
        return Rejection(
            "units",
            "invalid_units",
            "Send 'units' as a positive integer string of at most 12 digits, such as \"5000\".",
        )
    if PRICE_RE.fullmatch(raw["price"]) is None or Decimal(raw["price"]) <= 0:
        return Rejection(
            "price",
            "invalid_price",
            "Send 'price' as a positive decimal string with at most 9 integer digits and "
            'at most 4 decimals, such as "29.5".',
        )
    units, price = Decimal(raw["units"]), Decimal(raw["price"])
    cost = units * price
    # Rounding the cost would leave the postings off balance by the rounded part.
    if cost != cost.quantize(CENT) or cost >= AMOUNT_LIMIT:
        return Rejection(
            "price",
            "inexact_cost",
            "'units' times 'price' must be an amount of at most 15 integer digits and at "
            "most 2 decimals; round the price or check the units.",
        )
    for field in ("total", "fee"):
        if field in raw and AMOUNT_RE.fullmatch(raw[field]) is None:
            return Rejection(
                field,
                "invalid_amount",
                f"Send {field!r} as a non-negative decimal string with at most 2 decimals, "
                'such as "210".',
            )
    if "total" in raw:
        total = Decimal(raw["total"])
        if total < cost:
            return Rejection(
                "total", "total_below_cost", f"'total' is less than units times price, {cost}."
            )
        fee = _plain(total - cost)
    else:
        fee = Decimal(raw["fee"])
    if cost + fee >= AMOUNT_LIMIT:
        return Rejection("fee", "invalid_amount", "The total must have at most 15 integer digits.")
    if fee > 0 and "fees_account" not in raw:
        return Rejection("fees_account", "missing", "Send 'fees_account' for a trade with a fee.")

    currency = _parse_currency(raw, accounts)
    if isinstance(currency, Rejection):
        return currency
    if raw["commodity"] == currency:
        return Rejection(
            "commodity",
            "invalid_commodity",
            f"'commodity' must differ from the currency {currency}.",
        )

    note = _parse_note(raw)
    if isinstance(note, Rejection):
        return note

    source = _resolve("source", raw["source"], date, accounts)
    if isinstance(source, Rejection):
        return source
    holdings = _resolve("holdings", raw["holdings"], date, accounts)
    if isinstance(holdings, Rejection):
        return holdings
    if holdings.name.split(":", 1)[0] == accounts.expenses_root:
        return Rejection(
            "holdings",
            "expense_account",
            f"{holdings.name} is under {accounts.expenses_root}; 'holdings' must hold the shares.",
        )
    fees: Account | None = None
    if "fees_account" in raw:
        fees = _resolve("fees_account", raw["fees_account"], date, accounts)
        if isinstance(fees, Rejection):
            return fees
        if fees.name.split(":", 1)[0] != accounts.expenses_root:
            return Rejection(
                "fees_account",
                "not_expense_account",
                f"{fees.name} is not under {accounts.expenses_root}; 'fees_account' must be "
                "an expense account.",
            )
    named = [("source", source), ("holdings", holdings)]
    if fees is not None:
        named.append(("fees_account", fees))
    for index, (field, account) in enumerate(named):
        if any(account.name == other.name for _, other in named[:index]):
            return Rejection(
                field,
                "same_account",
                f"{field!r} resolves to {account.name}, which another account already uses; "
                "'source', 'holdings', and 'fees_account' must differ.",
            )
    if holdings.declared and raw["commodity"] not in holdings.declared:
        return Rejection(
            "commodity",
            "currency_not_allowed",
            f"{holdings.name} accepts only {', '.join(sorted(holdings.declared))}.",
        )
    for account in (source, *([fees] if fees is not None and fee > 0 else [])):
        if currency not in account.currencies:
            return Rejection(
                "currency",
                "currency_not_allowed",
                f"{account.name} accepts only {', '.join(sorted(account.currencies))}.",
            )

    return TradeRequest(
        date=date,
        time=local_time,
        source=source.name,
        holdings=holdings.name,
        fees_account=fees.name if fees is not None else None,
        commodity=raw["commodity"],
        units=units,
        price=price,
        fee=fee,
        currency=currency,
        narration=note.narration,
        payee=note.payee,
        tags=note.tags,
        meta=note.meta,
        key=key,
        dry_run=raw.get("dry_run", False),
    )


def _plain(number: Decimal) -> Decimal:
    """The number without the trailing zeros that arithmetic leaves, so 210.0 prints as 210."""
    if number == number.to_integral_value():
        return Decimal(int(number))
    return number.normalize()


@dataclass(frozen=True)
class Note:
    narration: str
    payee: str | None
    tags: frozenset[str]
    meta: tuple[tuple[str, str], ...]


def _check_fields(
    raw: object, types: Mapping[str, tuple[Callable[[object], bool], str]]
) -> Rejection | None:
    """The body is an object whose fields are all known and of the right JSON type."""
    if not isinstance(raw, dict):
        return Rejection(
            "",
            "invalid_body",
            "Send a JSON object as the body, with Content-Type: application/json.",
        )
    for field in raw:
        if field not in types:
            return Rejection(
                field,
                "unknown_field",
                f"Remove {field!r}; accepted fields are {', '.join(types)}.",
            )
    for field, value in raw.items():
        is_type, json_type = types[field]
        if not is_type(value):
            return Rejection(field, "invalid_type", f"Send {field!r} as a JSON {json_type}.")
    return None


def _parse_key(raw: Mapping[str, Any]) -> str | Rejection:
    key = raw["key"]
    if KEY_RE.fullmatch(key) is None:
        return Rejection(
            "key", "invalid_key", "Use only ASCII letters, digits, and - _ / . in 'key'."
        )
    return key


def _parse_when(
    raw: Mapping[str, Any], tz: datetime.tzinfo, today: datetime.date
) -> tuple[datetime.date, str | None] | Rejection:
    """The local date, and the local time when the request sent 'time'."""
    if ("date" in raw) == ("time" in raw):
        return Rejection(
            "date",
            "date_or_time",
            "Send exactly one of 'date' (YYYY-MM-DD) or 'time' (RFC 3339 with an offset).",
        )
    local_time = None
    if "date" in raw:
        try:
            if DATE_RE.fullmatch(raw["date"]) is None:
                raise ValueError
            date = datetime.date.fromisoformat(raw["date"])
        except ValueError:
            return Rejection("date", "invalid_date", "Send 'date' as YYYY-MM-DD.")
    else:
        try:
            if TIME_RE.fullmatch(raw["time"]) is None:
                raise ValueError
            moment = datetime.datetime.fromisoformat(raw["time"]).astimezone(tz)
        # astimezone overflows at the ends of the date range, such as 9999-12-31T23:00:00Z.
        except (ValueError, OverflowError):
            return Rejection(
                "time",
                "invalid_time",
                "Send 'time' as RFC 3339 with an offset, such as 2026-09-25T07:00:00+08:00.",
            )
        date = moment.date()
        local_time = moment.strftime("%H:%M:%S")
    latest = today + datetime.timedelta(days=MAX_DAYS_AHEAD)
    if date > latest:
        field = "date" if "date" in raw else "time"
        return Rejection(field, "date_out_of_range", f"The date {date} is after {latest}.")
    return date, local_time


def _parse_currency(raw: Mapping[str, Any], accounts: Accounts) -> str | Rejection:
    currency = raw.get("currency", accounts.default_currency)
    if currency is None:
        return Rejection(
            "currency", "missing", "The ledger has no operating currency; send 'currency'."
        )
    if CURRENCY_RE.fullmatch(currency) is None:
        return Rejection(
            "currency", "invalid_currency", f"{currency!r} is not a currency, such as TWD."
        )
    return currency


def _parse_note(raw: Mapping[str, Any]) -> Note | Rejection:
    narration = raw.get("narration", "")
    if not _is_one_line(narration):
        return Rejection(
            "narration",
            "invalid_narration",
            "Remove line breaks and control characters from 'narration'.",
        )

    payee = raw.get("payee", "")
    if not _is_one_line(payee):
        return Rejection(
            "payee", "invalid_payee", "Remove line breaks and control characters from 'payee'."
        )

    tags = frozenset(raw.get("tags", []))
    for tag in sorted(tags):
        if KEY_RE.fullmatch(tag) is None:
            return Rejection(
                "tags",
                "invalid_tag",
                f"Tag {tag!r} may use only ASCII letters, digits, and - _ / .; "
                "put other text in 'meta'.",
            )

    meta = tuple(sorted(raw.get("meta", {}).items()))
    for meta_key, meta_value in meta:
        if META_KEY_RE.fullmatch(meta_key) is None:
            return Rejection(
                "meta",
                "invalid_meta_key",
                f"Meta key {meta_key!r} must start with a lowercase ASCII letter, "
                "followed by ASCII letters, digits, - or _.",
            )
        if meta_key in RESERVED_META_KEYS:
            return Rejection(
                "meta", "reserved_meta_key", f"Remove {meta_key!r} from 'meta'; the API sets it."
            )
        if not _is_one_line(meta_value):
            return Rejection(
                "meta",
                "invalid_meta_value",
                f"Remove line breaks and control characters from meta {meta_key!r}.",
            )
    return Note(narration, payee or None, tags, meta)


def _target_pairs(raw: list[object]) -> list[tuple[str, str, str, str]] | Rejection:
    if not raw:
        return Rejection("targets", "missing", "Send at least one item in 'targets'.")
    if len(raw) > MAX_TARGETS:
        return Rejection("targets", "too_many", f"Send at most {MAX_TARGETS} items in 'targets'.")
    pairs = []
    for index, item in enumerate(raw):
        field = f"targets[{index}]"
        if not isinstance(item, dict):
            return Rejection(
                field, "invalid_type", f"Send {field} as a JSON object with account and amount."
            )
        for name in item:
            if name not in TARGET_FIELDS:
                return Rejection(
                    f"{field}.{name}",
                    "unknown_field",
                    f"Remove {name!r}; accepted fields are account, amount.",
                )
        for name in TARGET_FIELDS:
            if name not in item:
                return Rejection(f"{field}.{name}", "missing", f"Send {name!r} in {field}.")
            if not isinstance(item[name], str):
                return Rejection(
                    f"{field}.{name}", "invalid_type", f"Send {name!r} as a JSON string."
                )
        pairs.append((f"{field}.account", item["account"], f"{field}.amount", item["amount"]))
    return pairs


def _is_one_line(text: str) -> bool:
    return not any(unicodedata.category(char) in NARRATION_FORBIDDEN for char in text)


def _resolve(field: str, text: str, date: datetime.date, accounts: Accounts) -> Account | Rejection:
    names = accounts.lookup.get(text, ())
    if not names:
        if not account_name.is_valid(text):
            return Rejection(
                field,
                "invalid_account_name",
                f"{text!r} is neither an account alias nor a valid account name.",
            )
        return Rejection(field, "unknown_account", f"{text} is not open in the ledger.")
    if len(names) > 1:
        return Rejection(
            field,
            "ambiguous_account",
            f"{text!r} matches more than one account; send one of the candidates.",
            names,
        )
    account = accounts.by_name[names[0]]
    # The parser only prefix-matches account names, so a ledger can open one that is not valid.
    if not account_name.is_valid(account.name):
        return Rejection(
            field, "invalid_account_name", f"{account.name} is not a valid account name."
        )
    if date < account.open_date:
        return Rejection(field, "account_not_open", f"{account.name} opens on {account.open_date}.")
    # beancount sorts a same-day Close after the transaction, so the close date still accepts it.
    if account.close_date is not None and date > account.close_date:
        return Rejection(
            field, "account_closed", f"{account.name} was closed on {account.close_date}."
        )
    return account


def _entry_meta(meta: tuple[tuple[str, str], ...], time: str | None) -> dict[str, str]:
    merged = dict(meta)
    if time is not None:
        merged["time"] = time
    # fava prints metadata in insertion order and the key check compares printed entries,
    # so the keys go in one fixed order.
    return dict(sorted(merged.items()))


def build_transaction(req: AddRequest) -> Transaction:
    return create.transaction(
        meta=_entry_meta(req.meta, req.time),
        date=req.date,
        flag="!",
        payee=req.payee,
        narration=req.narration,
        tags=req.tags,
        links=frozenset({req.link}),
        # The key check compares printed entries, so the targets keep the request's order.
        postings=[
            *(
                create.posting(t.account, create.amount(t.amount, req.currency))
                for t in req.targets
            ),
            create.posting(
                req.source, create.amount(-sum(t.amount for t in req.targets), req.currency)
            ),
        ],
    )


def build_trade(req: TradeRequest) -> Transaction:
    # The cost carries the trade date, as booking would set it, so a loaded entry prints
    # the same text and a retry finds the entry it wrote.
    cost = CostSpec(req.price, None, req.currency, req.date, None, False)
    postings = [
        create.posting(
            req.holdings,
            create.amount(req.units, req.commodity),
            cost=cost,  # type: ignore[arg-type]
        )
    ]
    if req.fees_account is not None and req.fee > 0:
        postings.append(create.posting(req.fees_account, create.amount(req.fee, req.currency)))
    postings.append(create.posting(req.source, create.amount(-req.total, req.currency)))
    return create.transaction(
        meta=_entry_meta(req.meta, req.time),
        date=req.date,
        flag="!",
        payee=req.payee,
        narration=req.narration,
        tags=req.tags,
        links=frozenset({req.link}),
        postings=postings,
    )


def parse_spending_request(raw: object) -> datetime.date | Rejection:
    if not isinstance(raw, dict):
        return Rejection(
            "",
            "invalid_body",
            "Send a JSON object as the body, with Content-Type: application/json.",
        )
    for field in raw:
        if field != "date":
            return Rejection(field, "unknown_field", f"Remove {field!r}; 'date' is the only field.")
    if "date" not in raw:
        return Rejection("date", "missing", "Send 'date'.")
    text = raw["date"]
    try:
        if not isinstance(text, str) or DATE_RE.fullmatch(text) is None:
            raise ValueError
        date = datetime.date.fromisoformat(text)
        _next_month(date)
    # The month total ends at the first day of the next month, which overflows in 9999-12.
    except (ValueError, OverflowError):
        return Rejection("date", "invalid_date", "Send 'date' as YYYY-MM-DD.")
    return date


def _next_month(date: datetime.date) -> datetime.date:
    return (date.replace(day=1) + datetime.timedelta(days=31)).replace(day=1)


def spending(
    transactions: Iterable[Transaction],
    opens: Iterable[Open],
    expenses_root: str,
    date: datetime.date,
) -> dict[str, Any]:
    names: dict[str, str | None] = {}
    for open_ in opens:
        name_zh = open_.meta.get("name-zh")
        names.setdefault(open_.account, name_zh if isinstance(name_zh, str) else None)

    def is_expense(account: str) -> bool:
        return account.split(":", 1)[0] == expenses_root

    def posting(p: Posting) -> dict[str, Any]:
        return {
            "account": p.account,
            "name_zh": names.get(p.account),
            "number": _decimal(p.units.number),
            "currency": p.units.currency,
        }

    first, end = date.replace(day=1), _next_month(date)
    day: list[dict[str, Any]] = []
    total: dict[str, Decimal] = {}
    pending: dict[str, Decimal] = {}
    for txn in transactions:
        if not first <= txn.date < end:
            continue
        expenses = [p for p in txn.postings if is_expense(p.account)]
        if not expenses:
            continue
        for sums in (total, pending) if txn.flag == "!" else (total,):
            for p in expenses:
                sums[p.units.currency] = sums.get(p.units.currency, Decimal(0)) + p.units.number
        if txn.date != date:
            continue
        keys = sorted(link.removeprefix("ik-") for link in txn.links if link.startswith("ik-"))
        day.append(
            {
                "id": hash_entry(txn),
                "date": txn.date.isoformat(),
                "flag": txn.flag,
                "payee": txn.payee or None,
                "narration": txn.narration,
                "key": keys[0] if keys else None,
                "expenses": [posting(p) for p in expenses],
                "funding": [posting(p) for p in txn.postings if not is_expense(p.account)],
            }
        )
    return {
        "date": date.isoformat(),
        "transactions": day,
        "month": {
            "month": f"{date.year:04}-{date.month:02}",
            "total": _sums(total),
            "pending": _sums(pending),
        },
    }


def _sums(sums: Mapping[str, Decimal]) -> list[dict[str, str]]:
    return [{"currency": c, "number": _decimal(sums[c])} for c in sorted(sums)]


def _decimal(number: Decimal) -> str:
    # A JSON number loses Decimal precision in JavaScript, and str() can print an exponent.
    return format(number, "f")


@dataclass(frozen=True)
class ApproveItem:
    id: str
    key: str | None


MAX_APPROVE_ITEMS = 50
# The flag token after the date on a transaction's first line, in the file's bytes.
HEADER_FLAG_RE = re.compile(rb"[^ \t\r\n]+[ \t]+(!)(?=[ \t\r\n;]|$)")


def parse_approve_request(raw: object) -> list[ApproveItem] | Rejection:
    if not isinstance(raw, dict):
        return Rejection(
            "",
            "invalid_body",
            "Send a JSON object as the body, with Content-Type: application/json.",
        )
    for field in raw:
        if field != "items":
            return Rejection(
                field, "unknown_field", f"Remove {field!r}; 'items' is the only field."
            )
    if "items" not in raw:
        return Rejection("items", "missing", "Send 'items'.")
    items = raw["items"]
    if not isinstance(items, list):
        return Rejection("items", "invalid_type", "Send 'items' as a JSON array.")
    if len(items) > MAX_APPROVE_ITEMS:
        return Rejection(
            "items", "too_many", f"Send at most {MAX_APPROVE_ITEMS} items in one request."
        )
    parsed = []
    for index, item in enumerate(items):
        field = f"items[{index}]"
        if not isinstance(item, dict):
            return Rejection(field, "invalid_type", f"Send {field} as a JSON object.")
        for name in item:
            if name not in ("id", "key"):
                return Rejection(
                    f"{field}.{name}",
                    "unknown_field",
                    f"Remove {name!r}; accepted fields are id, key.",
                )
        if "id" not in item:
            return Rejection(f"{field}.id", "missing", f"Send 'id' in {field}.")
        if not isinstance(item["id"], str):
            return Rejection(f"{field}.id", "invalid_type", "Send 'id' as a JSON string.")
        key = item.get("key")
        if key is not None and not isinstance(key, str):
            return Rejection(f"{field}.key", "invalid_type", "Send 'key' as a JSON string or null.")
        parsed.append(ApproveItem(item["id"], key))
    return parsed


def flag_offset(data: bytes, lineno: int) -> int | None:
    """Byte offset of the `!` flag on line `lineno` (1-based) of a file's bytes.

    None when that line does not start like a pending transaction.
    """
    start = 0
    for _ in range(lineno - 1):
        start = data.find(b"\n", start) + 1
        if start == 0:
            return None
    match = HEADER_FLAG_RE.match(data, start)
    return None if match is None else match.start(1)
