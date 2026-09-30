from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import fava.core  # noqa: F401  # fava.beans.str raises a circular ImportError unless fava.core loads first
import pytest
from beancount import loader
from beancount.core.data import Close, Open
from fava.beans.str import to_string

from beancount_agent_api.core import (
    Accounts,
    AddRequest,
    Rejection,
    build_transaction,
    parse_add_request,
)

LEDGER = Path(__file__).resolve().parent / "fixtures" / "agent-ledger" / "main.beancount"
TZ = ZoneInfo("Asia/Taipei")
TODAY = datetime.date(2026, 9, 30)
DINNER = {"date": "2026-09-24", "source": "錢包", "target": "晚餐", "amount": "190", "key": "d1"}


@pytest.fixture(scope="module")
def accounts() -> Accounts:
    entries, errors, options = loader.load_file(str(LEDGER))
    assert errors == []
    return Accounts.from_directives(
        [entry for entry in entries if isinstance(entry, Open)],
        [entry for entry in entries if isinstance(entry, Close)],
        options["operating_currency"],
    )


def parse(accounts: Accounts, **changes: object) -> AddRequest | Rejection:
    raw = {**DINNER, **changes}
    return parse_add_request({k: v for k, v in raw.items() if v is not None}, accounts, TZ, TODAY)


def code(result: AddRequest | Rejection) -> tuple[str, str]:
    assert isinstance(result, Rejection), result
    return result.field, result.code


def test_dinner_resolves_aliases(accounts: Accounts) -> None:
    assert parse(accounts) == AddRequest(
        date=datetime.date(2026, 9, 24),
        time=None,
        source="Assets:TW:Cash",
        target="Expenses:Food:Dinner",
        amount=Decimal(190),
        currency="TWD",
        narration="",
        key="d1",
        dry_run=False,
    )


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("錢包", "晚餐", ("Assets:TW:Cash", "Expenses:Food:Dinner")),
        ("Assets:TW:Cash", "食物/晚餐", ("Assets:TW:Cash", "Expenses:Food:Dinner")),
        ("錢包", "Expenses:Food:Dinner", ("Assets:TW:Cash", "Expenses:Food:Dinner")),
        (
            "Equity:Opening-Balances",
            "食物/午餐",
            ("Equity:Opening-Balances", "Expenses:Food:Lunch"),
        ),
        ("錢包", "公務/午餐", ("Assets:TW:Cash", "Expenses:Work:Lunch")),
    ],
)
def test_account_aliases(
    accounts: Accounts, source: str, target: str, expected: tuple[str, str]
) -> None:
    result = parse(accounts, source=source, target=target)
    assert isinstance(result, AddRequest), result
    assert (result.source, result.target) == expected


def test_shared_last_segment_lists_every_candidate(accounts: Accounts) -> None:
    assert parse(accounts, target="午餐") == Rejection(
        "target",
        "ambiguous_account",
        "'午餐' matches more than one account; send one of the candidates.",
        ("Expenses:Food:Lunch", "Expenses:Work:Lunch"),
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"source": "悠遊卡"}, ("source", "account_closed")),
        ({"source": "悠遊卡", "date": "2026-06-30"}, None),
        ({"date": "2025-12-31"}, ("source", "account_not_open")),
        ({"date": "2026-01-01"}, None),
        ({"source": "Assets:錢包"}, ("source", "invalid_account_name")),
        ({"target": "Expenses:Food:Snack"}, ("target", "unknown_account")),
        ({"target": "錢包"}, ("target", "same_account")),
        ({"currency": "USD"}, ("currency", "currency_not_allowed")),
        ({"currency": "twd"}, ("currency", "invalid_currency")),
    ],
)
def test_account_rules(
    accounts: Accounts, changes: dict[str, str], expected: tuple[str, str] | None
) -> None:
    result = parse(accounts, **changes)
    if expected is None:
        assert isinstance(result, AddRequest), result
    else:
        assert code(result) == expected


def test_closed_account_message(accounts: Accounts) -> None:
    assert parse(accounts, source="悠遊卡") == Rejection(
        "source", "account_closed", "Assets:TW:EasyCard was closed on 2026-06-30."
    )
    assert parse(accounts, currency="USD") == Rejection(
        "currency", "currency_not_allowed", "Assets:TW:Cash accepts only TWD."
    )


def test_invalid_account_name_opened_in_the_ledger() -> None:
    accounts = Accounts.from_directives(
        [
            Open({"name-zh": "零錢"}, datetime.date(2026, 1, 1), "Assets:Cash:錢包", None, None),
            Open({}, datetime.date(2026, 1, 1), "Expenses:Food", None, None),
        ],
        [],
        ["TWD"],
    )
    assert parse(accounts, source="零錢", target="Expenses:Food") == Rejection(
        "source", "invalid_account_name", "Assets:Cash:錢包 is not a valid account name."
    )


@pytest.mark.parametrize(
    "amount",
    ["190.5.1", "1 @ 2", "-5", "0.001", "0", "0.00", "１９０", "190\n", "1e3", "", "1" * 16],
)
def test_bad_amounts(accounts: Accounts, amount: str) -> None:
    assert code(parse(accounts, amount=amount)) == ("amount", "invalid_amount")


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("190", "190"), ("0.5", "0.5"), ("12.34", "12.34"), ("9" * 15 + ".99", "9" * 15 + ".99")],
)
def test_good_amounts(accounts: Accounts, amount: str, expected: str) -> None:
    result = parse(accounts, amount=amount)
    assert isinstance(result, AddRequest), result
    assert result.amount == Decimal(expected)


def test_amount_must_be_a_string(accounts: Accounts) -> None:
    assert code(parse(accounts, amount=190)) == ("amount", "invalid_type")


@pytest.mark.parametrize(
    ("time", "date", "local"),
    [
        ("2026-09-25T07:00:00+08:00", datetime.date(2026, 9, 25), "07:00:00"),
        ("2026-09-24T23:30:00Z", datetime.date(2026, 9, 25), "07:30:00"),
        ("2026-09-24T15:59:59.9-00:00", datetime.date(2026, 9, 24), "23:59:59"),
    ],
)
def test_time_converts_to_the_ledger_day(
    accounts: Accounts, time: str, date: datetime.date, local: str
) -> None:
    result = parse(accounts, date=None, time=time)
    assert isinstance(result, AddRequest), result
    assert (result.date, result.time) == (date, local)


@pytest.mark.parametrize(
    "time",
    [
        "2026-09-25T07:00:00",
        "2026-09-25 07:00:00+08:00",
        "2026-09-25T25:00:00Z",
        "9999-12-31T23:00:00Z",
        "",
    ],
)
def test_bad_times(accounts: Accounts, time: str) -> None:
    assert code(parse(accounts, date=None, time=time)) == ("time", "invalid_time")


@pytest.mark.parametrize("date", ["2026-9-24", "2026-09-31", "20260924", "2026-09-24T00:00:00"])
def test_bad_dates(accounts: Accounts, date: str) -> None:
    assert code(parse(accounts, date=date)) == ("date", "invalid_date")


def test_date_and_time_are_exclusive(accounts: Accounts) -> None:
    assert code(parse(accounts, time="2026-09-24T07:00:00+08:00")) == ("date", "date_or_time")
    assert code(parse(accounts, date=None)) == ("date", "date_or_time")


@pytest.mark.parametrize("key", ["a b", "a\n", "a^b", "", "é"])
def test_bad_keys(accounts: Accounts, key: str) -> None:
    assert code(parse(accounts, key=key)) == ("key", "invalid_key")


def test_key_charset(accounts: Accounts) -> None:
    result = parse(accounts, key="a/b.c-d_e")
    assert isinstance(result, AddRequest), result
    assert result.link == "ik-a/b.c-d_e"


@pytest.mark.parametrize("field", ["postings", "price", "cost", "flag", "links", "meta"])
def test_client_cannot_send_entry_syntax(accounts: Accounts, field: str) -> None:
    assert code(parse(accounts, **{field: "x"})) == (field, "unknown_field")


@pytest.mark.parametrize("narration", ["dinner\n", "a\rb", "tab\there", "a\ud800b", "a b", "\x00"])
def test_narration_rejects_line_breaks(accounts: Accounts, narration: str) -> None:
    assert code(parse(accounts, narration=narration)) == ("narration", "invalid_narration")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([], ("", "invalid_body")),
        (None, ("", "invalid_body")),
        ({**DINNER, "dry_run": "true"}, ("dry_run", "invalid_type")),
        ({k: v for k, v in DINNER.items() if k != "key"}, ("key", "missing")),
    ],
)
def test_body_shape(accounts: Accounts, raw: object, expected: tuple[str, str]) -> None:
    assert code(parse_add_request(raw, accounts, TZ, TODAY)) == expected


def test_render_dinner(accounts: Accounts) -> None:
    result = parse(accounts)
    assert isinstance(result, AddRequest), result
    assert to_string(build_transaction(result), 61, 2) == (
        "2026-09-24 ! ^ik-d1\n"
        "  Expenses:Food:Dinner                                  190 TWD\n"
        "  Assets:TW:Cash                                       -190 TWD\n"
    )


def test_render_with_time_and_narration(accounts: Accounts) -> None:
    result = parse(
        accounts,
        date=None,
        time="2026-09-25T07:00:00+08:00",
        amount="80.5",
        narration='早餐 "蛋餅"',
        key="b1",
    )
    assert isinstance(result, AddRequest), result
    assert to_string(build_transaction(result), 61, 2) == (
        '2026-09-25 ! "早餐 \\"蛋餅\\"" ^ik-b1\n'
        '  time: "07:00:00"\n'
        "  Expenses:Food:Dinner                                 80.5 TWD\n"
        "  Assets:TW:Cash                                      -80.5 TWD\n"
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"date": "2027-10-02"}, ("date", "date_out_of_range")),
        ({"date": "9999-12-31"}, ("date", "date_out_of_range")),
        ({"date": None, "time": "2027-10-01T16:00:00Z"}, ("time", "date_out_of_range")),
    ],
)
def test_dates_more_than_366_days_ahead(
    accounts: Accounts, changes: dict[str, str | None], expected: tuple[str, str]
) -> None:
    assert code(parse(accounts, **changes)) == expected


def test_last_accepted_date(accounts: Accounts) -> None:
    result = parse(accounts, date="2027-10-01")
    assert isinstance(result, AddRequest), result
    assert result.date == datetime.date(2027, 10, 1)
