from __future__ import annotations

import datetime
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from beancount.core import account as account_name
from beancount.core import amount as beancount_amount
from beancount.core.data import Close, Open, Transaction
from fava.beans import create


@dataclass(frozen=True)
class Account:
    name: str
    open_date: datetime.date
    close_date: datetime.date | None
    currencies: frozenset[str]


@dataclass(frozen=True)
class Accounts:
    by_name: Mapping[str, Account]
    lookup: Mapping[str, tuple[str, ...]]
    default_currency: str | None

    @classmethod
    def from_directives(
        cls,
        opens: Iterable[Open],
        closes: Iterable[Close],
        operating_currencies: Sequence[str],
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
        )


@dataclass(frozen=True)
class AddRequest:
    date: datetime.date
    time: str | None
    source: str
    target: str
    amount: Decimal
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


# Each request field with its JSON type check and the type name that a rejection shows.
FIELD_TYPES: Mapping[str, tuple[Callable[[object], bool], str]] = {
    "date": (_is_string, "string"),
    "time": (_is_string, "string"),
    "source": (_is_string, "string"),
    "target": (_is_string, "string"),
    "amount": (_is_string, "string"),
    "currency": (_is_string, "string"),
    "narration": (_is_string, "string"),
    "payee": (_is_string, "string"),
    "tags": (_is_string_array, "array of strings"),
    "meta": (_is_string_object, "object with string values"),
    "key": (_is_string, "string"),
    "dry_run": (_is_boolean, "boolean"),
}
FIELDS = tuple(FIELD_TYPES)
REQUIRED = ("source", "target", "amount", "key")
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
    if not isinstance(raw, dict):
        return Rejection(
            "",
            "invalid_body",
            "Send a JSON object as the body, with Content-Type: application/json.",
        )
    for field in raw:
        if field not in FIELDS:
            return Rejection(
                field,
                "unknown_field",
                f"Remove {field!r}; accepted fields are {', '.join(FIELDS)}.",
            )
    for field, value in raw.items():
        is_type, json_type = FIELD_TYPES[field]
        if not is_type(value):
            return Rejection(field, "invalid_type", f"Send {field!r} as a JSON {json_type}.")
    for field in REQUIRED:
        if field not in raw:
            return Rejection(field, "missing", f"Send {field!r}.")

    key = raw["key"]
    if KEY_RE.fullmatch(key) is None:
        return Rejection(
            "key", "invalid_key", "Use only ASCII letters, digits, and - _ / . in 'key'."
        )

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

    amount_text = raw["amount"]
    if AMOUNT_RE.fullmatch(amount_text) is None or Decimal(amount_text) <= 0:
        return Rejection(
            "amount",
            "invalid_amount",
            "Send 'amount' as a positive decimal string with at most 2 decimals, such as \"190\".",
        )
    amount = Decimal(amount_text)

    currency = raw.get("currency", accounts.default_currency)
    if currency is None:
        return Rejection(
            "currency", "missing", "The ledger has no operating currency; send 'currency'."
        )
    if CURRENCY_RE.fullmatch(currency) is None:
        return Rejection(
            "currency", "invalid_currency", f"{currency!r} is not a currency, such as TWD."
        )

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

    resolved = []
    for field in ("source", "target"):
        account = _resolve(field, raw[field], date, accounts)
        if isinstance(account, Rejection):
            return account
        resolved.append(account)
    source, target = resolved

    if source.name == target.name:
        return Rejection(
            "target", "same_account", f"'source' and 'target' both resolve to {source.name}."
        )
    for account in (source, target):
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
        target=target.name,
        amount=amount,
        currency=currency,
        narration=narration,
        payee=payee or None,
        tags=tags,
        meta=meta,
        key=key,
        dry_run=raw.get("dry_run", False),
    )


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


def build_transaction(req: AddRequest) -> Transaction:
    meta = dict(req.meta)
    if req.time is not None:
        meta["time"] = req.time
    return create.transaction(
        # fava prints metadata in insertion order and the key check compares printed entries,
        # so the keys go in one fixed order.
        meta=dict(sorted(meta.items())),
        date=req.date,
        flag="!",
        payee=req.payee,
        narration=req.narration,
        tags=req.tags,
        links=frozenset({req.link}),
        postings=[
            create.posting(req.target, create.amount(req.amount, req.currency)),
            create.posting(req.source, create.amount(-req.amount, req.currency)),
        ],
    )
