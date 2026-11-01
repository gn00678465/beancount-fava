"""The csv layout with 提出 and 存入 columns, and transfers between two imported accounts."""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from beancount import loader
from beancount.core import data
from beancount.core.amount import Amount
from beancount.parser import printer
from beangulp.extract import DUPLICATE
from test_statement_importer import ACCOUNT, TITLE, write_statement

from beancount_agent_api.importers.layouts import SPLIT, parse
from beancount_agent_api.importers.statement import BOOKED_KEY, ID_KEY, StatementImporter

SAVINGS = "Assets:Bank:Savings"
SAVINGS_TITLE = "000123456789 活期儲蓄存款"
FALLBACK = "Expenses:Uncategorized"
SAVINGS_RULES = [
    ("薪資獎金", "Income:Salary"),
    ("存款息", "Income:Interest"),
    ("(999)0000111", ACCOUNT),
]
NONE = "−"
TO = "(999)0000111222333\n"
FROM = "(999)0000111***333\n"
# Newest first, as the bank writes them; the opening balance is 10000.
SAVINGS_ROWS = [
    ["2026/07/09\n10:42", "2026/07/09", "電子轉出", "20,000", NONE, "41,307", TO, NONE],
    ["2026/07/09\n10:41", "2026/07/09", "電子轉出", "20,000", NONE, "61,307", TO, NONE],
    ["2026/06/24\n23:30", "2026/06/27", "存款息", NONE, "57", "81,307", NONE, NONE],
    ["2026/06/11\n14:05", "2026/06/11", "跨行轉入", NONE, "20,000", "81,250", FROM, NONE],
    ["2026/06/11\n14:05", "2026/06/11", "跨行轉入", NONE, "20,000", "61,250", FROM, NONE],
    ["2026/06/03\n09:12", "2026/06/03", "薪資獎金", NONE, "31,250", "41,250", NONE, "測試公司    "],
]


def side(rows: list[list[str]], column: int) -> tuple[Decimal, int]:
    amounts = [Decimal(r[column].replace(",", "")) for r in rows if r[column] != NONE]
    return sum(amounts), len(amounts)


def write_split(
    path: Path,
    rows: list[list[str]],
    *,
    count: int | None = None,
    paid: tuple[Decimal, int] | None = None,
    received: tuple[Decimal, int] | None = None,
    end: str = "2026/07/10",
    title: str = SAVINGS_TITLE,
) -> Path:
    paid = paid or side(rows, 3)
    received = received or side(rows, 4)
    lines = [
        [title],
        ["幣別：TWD"],
        [f"筆數：{len(rows) if count is None else count} 筆", f"(自 2026/06/01 至 {end})"],
        ["\r\n"],
        list(SPLIT),
        *rows,
        ["\r\n"],
        ["提出", f"總金額 TWD {paid[0]:,} ( 共 {paid[1]} 筆 )"],
        ["存入", f"總金額 TWD {received[0]:,} ( 共 {received[1]} 筆 )"],
    ]
    text = "\r\n".join(",".join(f'"{cell}"' for cell in line) for line in lines)
    path.write_bytes(("﻿" + text).encode("utf-8"))
    return path


@pytest.fixture
def savings_file(tmp_path: Path) -> Path:
    return write_split(tmp_path / "000123456789.csv", SAVINGS_ROWS)


@pytest.fixture
def savings() -> StatementImporter:
    return StatementImporter(SAVINGS, SAVINGS_TITLE, SAVINGS_RULES, FALLBACK)


def test_identify_tells_the_two_layouts_apart(
    savings: StatementImporter, savings_file: Path, tmp_path: Path
) -> None:
    checking = StatementImporter(ACCOUNT, TITLE, [], FALLBACK)
    sheet = write_statement(tmp_path / "s.xlsx", [])
    binary = tmp_path / "photo.jpg"
    binary.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\xff\xfe")
    assert savings.identify(str(savings_file))
    assert not savings.identify(str(sheet))
    assert not checking.identify(str(savings_file))
    assert not savings.identify(str(binary))
    other = write_split(tmp_path / "other.csv", SAVINGS_ROWS, title="000999999999 活期儲蓄存款")
    assert not savings.identify(str(other))


def test_rows_come_out_oldest_first_with_signed_amounts(
    savings: StatementImporter, savings_file: Path
) -> None:
    txns = [e for e in savings.extract(str(savings_file), []) if isinstance(e, data.Transaction)]
    assert [(t.date, t.flag, t.narration, t.postings[1].account) for t in txns] == [
        (datetime.date(2026, 6, 3), "*", "測試公司", "Income:Salary"),
        (datetime.date(2026, 6, 11), "*", "跨行轉入 (999)0000111***333", ACCOUNT),
        (datetime.date(2026, 6, 11), "*", "跨行轉入 (999)0000111***333", ACCOUNT),
        (datetime.date(2026, 6, 24), "*", "存款息", "Income:Interest"),
        (datetime.date(2026, 7, 9), "*", "電子轉出 (999)0000111222333", ACCOUNT),
        (datetime.date(2026, 7, 9), "*", "電子轉出 (999)0000111222333", ACCOUNT),
    ]
    assert [t.postings[0].units.number for t in txns] == [31250, 20000, 20000, 57, -20000, -20000]
    assert txns[0].meta[ID_KEY] == "2026/06/03 09:12 31250 41250"
    assert txns[3].meta[BOOKED_KEY] == datetime.date(2026, 6, 27)
    assert txns[0].meta["lineno"] == 11
    assert BOOKED_KEY not in txns[0].meta


def test_the_assertions_follow_the_running_balance(
    savings: StatementImporter, savings_file: Path, tmp_path: Path
) -> None:
    entries = savings.extract(str(savings_file), [])
    assert [(e.date, e.amount.number) for e in entries if isinstance(e, data.Balance)] == [
        (datetime.date(2026, 6, 3), Decimal(10000)),
        (datetime.date(2026, 6, 4), Decimal(41250)),
        (datetime.date(2026, 6, 12), Decimal(81250)),
        (datetime.date(2026, 6, 25), Decimal(81307)),
        (datetime.date(2026, 7, 10), Decimal(41307)),
    ]
    assert load(entries, tmp_path) == []


def test_no_close_for_the_last_day_of_the_period(
    savings: StatementImporter, tmp_path: Path
) -> None:
    path = write_split(tmp_path / "s.csv", SAVINGS_ROWS, end="2026/07/09")
    days = [e.date for e in savings.extract(str(path), []) if isinstance(e, data.Balance)]
    assert days[-1] == datetime.date(2026, 6, 25)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"count": 7}, "the header counts 7 rows, the file has 6"),
        ({"paid": (40001, 2)}, "the 提出 total is 40,001 in 2 rows, the rows add up to 40000 in 2"),
        ({"received": (71307, 3)}, "the 存入 total is 71,307 in 3 rows"),
    ],
)
def test_a_count_or_total_that_differs_is_refused(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    path = write_split(tmp_path / "s.csv", SAVINGS_ROWS, **change)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=message.replace("(", r"\(")):
        parse(str(path), SAVINGS_TITLE)


def test_a_balance_that_does_not_follow_is_refused(tmp_path: Path) -> None:
    rows = [list(r) for r in SAVINGS_ROWS]
    rows[2][5] = "81,308"
    with pytest.raises(ValueError, match=r"row 8: 81250 \+ 57 is not the balance 81308"):
        parse(str(write_split(tmp_path / "s.csv", rows)), SAVINGS_TITLE)


def test_a_row_with_both_sides_is_refused(tmp_path: Path) -> None:
    rows = [list(r) for r in SAVINGS_ROWS]
    rows[2][3] = "1"
    with pytest.raises(ValueError, match="row 8: expected one of 提出 '1' and 存入 '57'"):
        parse(str(write_split(tmp_path / "s.csv", rows, paid=(40001, 3))), SAVINGS_TITLE)


def test_amounts_and_totals_can_have_cents(tmp_path: Path) -> None:
    rows = [
        ["2026/06/03\n09:12", "2026/06/03", "存款息", NONE, "1,000.25", "11,000.25", NONE, NONE]
    ]
    statement = parse(str(write_split(tmp_path / "s.csv", rows)), SAVINGS_TITLE)
    assert [(r.amount, r.balance) for r in statement.rows] == [
        (Decimal("1000.25"), Decimal("11000.25"))
    ]


def test_missing_totals_are_refused(tmp_path: Path) -> None:
    path = write_split(tmp_path / "s.csv", SAVINGS_ROWS)
    text = path.read_bytes().decode("utf-8")
    path.write_bytes(text[: text.index('"提出","總金額')].encode("utf-8"))
    with pytest.raises(ValueError, match="expected the 提出 total below the rows"):
        parse(str(path), SAVINGS_TITLE)


# The same transfers as seen from the checking account, oldest first.
CHECKING_ROWS = [
    ["2026/06/11 14:05:12", "2026/06/11", "CD轉出", -20000, 80000, "網路跨行999-000123456789"],
    ["2026/06/11 14:05:48", "2026/06/11", "CD轉出", -20000, 60000, "網路跨行999-000123456789"],
    ["2026/07/09 10:41:30", "2026/07/09", "ATM", 20000, 80000, "ATM 999-000123456789"],
    ["2026/07/09 10:42:07", "2026/07/09", "ATM", 20000, 100000, "ATM 999-000123456789"],
]


def load(entries: list[data.Directive], tmp_path: Path) -> list[str]:
    """Load the entries after a 10000 opening of savings and 100000 of checking."""
    accounts = [ACCOUNT, SAVINGS, FALLBACK, "Income:Salary", "Income:Interest", "Equity:Open"]
    text = "".join(f"2026-01-01 open {a}\n" for a in accounts)
    text += f'2026-05-31 * "opening"\n  {SAVINGS}  10000 TWD\n  {ACCOUNT}  100000 TWD\n'
    text += "  Equity:Open\n\n" + "\n".join(printer.format_entry(e) for e in entries)
    ledger = tmp_path / "main.beancount"
    ledger.write_text(text, encoding="utf-8")
    _, errors, _ = loader.load_file(str(ledger))
    return [f"{e.entry.date} {e.message}" for e in errors]


def test_a_transfer_saved_from_the_other_account_is_marked(
    savings: StatementImporter, savings_file: Path, tmp_path: Path
) -> None:
    checking = StatementImporter(ACCOUNT, TITLE, [("999-000123456789", SAVINGS)], FALLBACK)
    saved = checking.extract(str(write_statement(tmp_path / "c.xlsx", CHECKING_ROWS)), [])
    entries = savings.extract(str(savings_file), [])
    savings.deduplicate(entries, saved)
    txns = [e for e in entries if isinstance(e, data.Transaction)]
    assert [bool(t.meta.get(DUPLICATE)) for t in txns] == [False, True, True, False, True, True]
    kept = [e for e in entries if not e.meta.get(DUPLICATE)]
    assert load(saved + kept, tmp_path) == []


def test_a_saved_transaction_without_postings_is_skipped(
    savings: StatementImporter, savings_file: Path
) -> None:
    entries = savings.extract(str(savings_file), [])
    empty = entries[0]._replace(postings=[])
    savings.deduplicate(entries, [empty])
    assert not entries[0].meta.get(DUPLICATE)


def test_an_id_saved_from_another_account_is_not_this_accounts_row(
    savings: StatementImporter, savings_file: Path
) -> None:
    entries = savings.extract(str(savings_file), [])
    salary = entries[0]
    other = salary._replace(
        postings=[
            data.Posting(ACCOUNT, Amount(Decimal(1), "TWD"), None, None, None, None),
            data.Posting(FALLBACK, Amount(Decimal(-1), "TWD"), None, None, None, None),
        ]
    )
    savings.deduplicate(entries, [other])
    assert not salary.meta.get(DUPLICATE)
