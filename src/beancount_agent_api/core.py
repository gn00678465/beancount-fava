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
from fava.beans import create
from fava.beans.funcs import hash_entry


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

    for _, _, amount_field, amount_text in pairs:
        if AMOUNT_RE.fullmatch(amount_text) is None or Decimal(amount_text) <= 0:
            return Rejection(
                amount_field,
                "invalid_amount",
                f"Send {amount_field!r} as a positive decimal string with at most 2 decimals, "
                'such as "190".',
            )

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
        narration=narration,
        payee=payee or None,
        tags=tags,
        meta=meta,
        key=key,
        dry_run=raw.get("dry_run", False),
    )


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
