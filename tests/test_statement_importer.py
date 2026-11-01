"""The 交易明細 importer, against files shaped like the online banking exports."""

from __future__ import annotations

import datetime
import zipfile
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from beancount import loader
from beancount.core import data
from beancount.core.amount import Amount
from beancount.parser import printer
from beangulp.extract import DUPLICATE
from fava.core.ingest import extract_from_file, load_import_config

from beancount_agent_api.importers import layouts
from beancount_agent_api.importers.layouts import parse
from beancount_agent_api.importers.statement import BOOKED_KEY, ID_KEY, StatementImporter

ACCOUNT = "Assets:Bank:Checking"
TITLE = "測試銀行 - 交易明細"
FALLBACK = "Expenses:Uncategorized"
RULES = [("存款息", "Income:Interest"), ("劃撥轉帳", "Assets:Broker:Stocks")]
HEADER = ["交易日", "帳務日", "摘要", "金額", "餘額", "備註"]
ROWS = [
    ["2026/06/01 02:10:00", "2026/06/01", "存款息", 120, 10120, ""],
    ["2026/06/08 10:20:00", "2026/06/08", "轉帳支取", -5000, 5120, "劃撥轉帳測試股"],
    ["2026/06/08 10:20:00", "2026/06/08", "CD轉出", -1000, 4120, "網路跨行000-0000"],
]


def cell(ref: str, value: object) -> str:
    if isinstance(value, int):
        return f'<c r="{ref}" s="65"><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c t="inlineStr" r="{ref}"><is><t xml:space="preserve">{text}</t></is></c>'


def write_statement(
    path: Path,
    rows: list[list[object]],
    *,
    footer: str | None = None,
    exported: str = "2026/11/01",
    title: str = TITLE,
) -> Path:
    lines = [[title], HEADER, *rows]
    lines.append([footer or f"共{len(rows)}筆資料 資料日期：{exported} 09:25:47"])
    xml_rows = "".join(
        f'<row r="{n}">'
        + "".join(cell(f"{'ABCDEF'[i]}{n}", v) for i, v in enumerate(line) if v != "")
        + "</row>"
        for n, line in enumerate(lines, start=1)
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{xml_rows}</sheetData></worksheet>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return path


@pytest.fixture
def statement(tmp_path: Path) -> Path:
    return write_statement(tmp_path / "交易明細.xlsx", ROWS)


@pytest.fixture
def importer() -> StatementImporter:
    return StatementImporter(ACCOUNT, TITLE, RULES, FALLBACK)


def test_identify_accepts_only_the_export(
    importer: StatementImporter, statement: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other.xlsx"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
    text = tmp_path / "note.txt"
    text.write_text(TITLE, encoding="utf-8")
    other_bank = write_statement(tmp_path / "other-bank.xlsx", ROWS, title="另一家銀行 - 交易明細")
    assert importer.identify(str(statement))
    assert not importer.identify(str(other))
    assert not importer.identify(str(text))
    assert not importer.identify(str(other_bank))


def hand(day: datetime.date, amount: int, other: str = "Assets:Bank:Other") -> data.Transaction:
    """A transaction entered in fava by hand, so it has no statement-id."""
    return data.Transaction(
        data.new_metadata("main.beancount", 1),
        day,
        "*",
        None,
        "hand",
        data.EMPTY_SET,
        data.EMPTY_SET,
        [
            data.Posting(ACCOUNT, Amount(Decimal(amount), "TWD"), None, None, None, None),
            data.Posting(other, Amount(Decimal(-amount), "TWD"), None, None, None, None),
        ],
    )


def balances(entries: list[data.Directive]) -> list[tuple[datetime.date, Decimal]]:
    return [(e.date, e.amount.number) for e in entries if isinstance(e, data.Balance)]


def duplicates(entries: list[data.Directive]) -> list[bool]:
    return [bool(e.meta.get(DUPLICATE)) for e in entries]


def test_extract_writes_one_transaction_per_row(
    importer: StatementImporter, statement: Path
) -> None:
    entries = importer.extract(str(statement), [])
    txns = [e for e in entries if isinstance(e, data.Transaction)]
    assert [(t.date, t.flag, t.narration, t.postings[1].account) for t in txns] == [
        (datetime.date(2026, 6, 1), "*", "存款息", "Income:Interest"),
        (datetime.date(2026, 6, 8), "*", "劃撥轉帳測試股", "Assets:Broker:Stocks"),
        (datetime.date(2026, 6, 8), "!", "網路跨行000-0000", FALLBACK),
    ]
    assert [t.postings[0].units.number for t in txns] == [120, -5000, -1000]
    assert txns[1].meta[ID_KEY] == "2026/06/08 10:20:00 -5000 5120"
    assert txns[2].meta[ID_KEY] == "2026/06/08 10:20:00 -1000 4120"


def test_extract_asserts_the_opening_and_each_day_close(
    importer: StatementImporter, statement: Path
) -> None:
    entries = importer.extract(str(statement), [])
    assert {e.account for e in entries if isinstance(e, data.Balance)} == {ACCOUNT}
    assert balances(entries) == [
        (datetime.date(2026, 6, 1), Decimal(10000)),
        (datetime.date(2026, 6, 2), Decimal(10120)),
        (datetime.date(2026, 6, 9), Decimal(4120)),
    ]


def test_no_close_for_the_export_day(importer: StatementImporter, tmp_path: Path) -> None:
    path = write_statement(tmp_path / "s.xlsx", ROWS, exported="2026/06/08")
    assert balances(importer.extract(str(path), [])) == [
        (datetime.date(2026, 6, 1), Decimal(10000)),
        (datetime.date(2026, 6, 2), Decimal(10120)),
    ]


def test_an_empty_statement_extracts_nothing(importer: StatementImporter, tmp_path: Path) -> None:
    assert importer.extract(str(write_statement(tmp_path / "s.xlsx", [])), []) == []


def load_with(entries: list[data.Directive], tmp_path: Path) -> list[datetime.date]:
    """Load the entries after a 10000 opening; return the dates of failed assertions."""
    accounts = [ACCOUNT, FALLBACK, "Income:Interest", "Assets:Broker:Stocks"]
    accounts += ["Assets:Bank:Other", "Equity:Open"]
    text = "".join(f"2026-01-01 open {a}\n" for a in accounts)
    text += f'2026-05-31 * "opening"\n  {ACCOUNT}  10000 TWD\n  Equity:Open\n\n'
    text += "\n".join(printer.format_entry(e) for e in entries)
    ledger = tmp_path / "main.beancount"
    ledger.write_text(text, encoding="utf-8")
    _, errors, _ = loader.load_file(str(ledger))
    return [e.entry.date for e in errors]


def test_extracted_entries_load_as_a_valid_ledger(
    importer: StatementImporter, statement: Path, tmp_path: Path
) -> None:
    assert load_with(importer.extract(str(statement), []), tmp_path) == []


def test_a_row_saved_twice_fails_the_assertion_of_its_day(
    importer: StatementImporter, statement: Path, tmp_path: Path
) -> None:
    entries = importer.extract(str(statement), [])
    assert load_with([hand(datetime.date(2026, 6, 8), -1000), *entries], tmp_path) == [
        datetime.date(2026, 6, 9)
    ]


def test_a_skipped_row_fails_the_assertion_of_its_day(
    importer: StatementImporter, statement: Path, tmp_path: Path
) -> None:
    entries = importer.extract(str(statement), [])[1:]
    assert load_with(entries, tmp_path) == [datetime.date(2026, 6, 2), datetime.date(2026, 6, 9)]


def test_a_balance_that_does_not_follow_is_refused(tmp_path: Path) -> None:
    rows = [list(r) for r in ROWS]
    rows[2][4] = 4121
    with pytest.raises(ValueError, match=r"row 5: 5120 \+ -1000 is not the balance 4121"):
        parse(str(write_statement(tmp_path / "s.xlsx", rows)), TITLE)


def test_a_footer_count_that_differs_is_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path / "s.xlsx", ROWS, footer="共4筆資料 資料日期：2026/11/01")
    with pytest.raises(ValueError, match="footer counts 4 rows, the sheet has 3"):
        parse(str(path), TITLE)


def test_a_missing_footer_is_refused(tmp_path: Path) -> None:
    path = write_statement(tmp_path / "s.xlsx", ROWS, footer="2026/06/09 00:00:00")
    with pytest.raises(ValueError, match="expected the 共N筆資料 footer"):
        parse(str(path), TITLE)


def test_deduplicate_marks_saved_rows_and_balances(
    importer: StatementImporter, statement: Path
) -> None:
    first = importer.extract(str(statement), [])
    again = importer.extract(str(statement), [])
    importer.deduplicate(again, first)
    assert all(duplicates(again))


def test_deduplicate_keeps_new_rows_and_a_changed_close(
    importer: StatementImporter, statement: Path, tmp_path: Path
) -> None:
    saved = importer.extract(str(statement), [])
    rows = [*ROWS, ["2026/06/08 09:00:00", "2026/06/08", "CD轉出", -1000, 3120, "網路跨行000-0000"]]
    entries = importer.extract(str(write_statement(tmp_path / "later.xlsx", rows)), [])
    importer.deduplicate(entries, saved)
    new = [e for e in entries if not e.meta.get(DUPLICATE)]
    assert [(type(e).__name__, e.date) for e in new] == [
        ("Transaction", datetime.date(2026, 6, 8)),
        ("Balance", datetime.date(2026, 6, 9)),
    ]
    assert new[0].meta[ID_KEY] == "2026/06/08 09:00:00 -1000 3120"
    assert new[1].amount.number == 3120


OTHER_LEGS = [
    data.Posting("Assets:Bank:Other", Amount(Decimal(120), "TWD"), None, None, None, None),
    data.Posting("Income:Interest", Amount(Decimal(-120), "TWD"), None, None, None, None),
]


@pytest.mark.parametrize(
    ("saved", "flagged"),
    [
        (hand(datetime.date(2026, 6, 1), 120), True),
        (hand(datetime.date(2026, 6, 1), 121), False),
        # A trade entered on its trade day, with other accounts, debited a week later.
        (hand(datetime.date(2026, 5, 25), 120, "Assets:Broker:Holdings"), True),
        (hand(datetime.date(2026, 5, 24), 120), False),
        # The same amount between two other accounts.
        (hand(datetime.date(2026, 6, 1), 120)._replace(postings=OTHER_LEGS), False),
    ],
)
def test_deduplicate_matches_a_hand_entry_by_exact_amount_within_a_week(
    importer: StatementImporter, statement: Path, saved: data.Transaction, flagged: bool
) -> None:
    entries = importer.extract(str(statement), [])
    importer.deduplicate(entries, [saved])
    assert duplicates(entries)[:3] == [flagged, False, False]


def test_one_hand_entry_matches_one_row_the_nearest(
    importer: StatementImporter, tmp_path: Path
) -> None:
    rows = [
        ["2026/06/02 10:00:00", "2026/06/02", "CD轉出", -1000, 9000, ""],
        ["2026/06/09 10:00:00", "2026/06/09", "CD轉出", -1000, 8000, ""],
    ]
    entries = importer.extract(str(write_statement(tmp_path / "s.xlsx", rows)), [])
    importer.deduplicate(entries, [hand(datetime.date(2026, 6, 8), -1000)])
    assert duplicates(entries)[:2] == [False, True]


def test_two_rows_of_one_amount_need_two_hand_entries(
    importer: StatementImporter, tmp_path: Path
) -> None:
    rows = [
        ["2026/06/15 13:20:00", "2026/06/15", "CD轉出", -1000, 9000, ""],
        ["2026/06/15 13:21:00", "2026/06/15", "CD轉出", -1000, 8000, ""],
    ]
    one = importer.extract(str(write_statement(tmp_path / "s.xlsx", rows)), [])
    importer.deduplicate(one, [hand(datetime.date(2026, 6, 15), -1000)])
    assert duplicates(one)[:2].count(True) == 1
    two = importer.extract(str(write_statement(tmp_path / "s.xlsx", rows)), [])
    day = datetime.date(2026, 6, 15)
    importer.deduplicate(two, [hand(day, -1000), hand(day, -1000)])
    assert duplicates(two)[:2] == [True, True]


def test_fava_loads_an_import_config_that_uses_it(statement: Path, tmp_path: Path) -> None:
    config = tmp_path / "importers.py"
    config.write_text(
        "from beancount_agent_api.importers.statement import StatementImporter\n"
        f"CONFIG = [StatementImporter({ACCOUNT!r}, {TITLE!r}, {RULES!r}, {FALLBACK!r}),\n"
        "          StatementImporter('Assets:Bank:Savings', 'x', [], 'Expenses:Uncategorized')]\n",
        encoding="utf-8",
    )
    loaded = load_import_config(config)
    wrapped = loaded.importers[f"statement:{ACCOUNT}"]
    assert wrapped.identify(statement)
    entries = extract_from_file(wrapped, statement, [])
    assert len(entries) == len(ROWS) + 3


def test_rows_are_dated_by_the_transaction_day(importer: StatementImporter, tmp_path: Path) -> None:
    # Two statements split at a row booked on a later business day: the
    # assertions follow the running balance, so both save without an error.
    first = [
        ["2026/06/12 10:00:00", "2026/06/12", "CD轉出", -1000, 9000, ""],
        ["2026/06/12 20:00:00", "2026/06/15", "CD轉出", -500, 8500, ""],
    ]
    second = [
        ["2026/06/13 09:00:00", "2026/06/15", "CD轉出", -200, 8300, ""],
        ["2026/06/15 09:00:00", "2026/06/15", "CD轉出", -100, 8200, ""],
    ]
    a = importer.extract(
        str(write_statement(tmp_path / "a.xlsx", first, exported="2026/06/14")), []
    )
    b = importer.extract(
        str(write_statement(tmp_path / "b.xlsx", second, exported="2026/06/20")), []
    )
    importer.deduplicate(b, a)
    late = a[1]
    assert (late.date, late.meta[BOOKED_KEY]) == (
        datetime.date(2026, 6, 12),
        datetime.date(2026, 6, 15),
    )
    assert BOOKED_KEY not in a[0].meta
    assert load_with(a + [e for e in b if not e.meta.get(DUPLICATE)], tmp_path) == []


def test_identify_says_no_to_a_file_it_cannot_read(
    importer: StatementImporter, statement: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(path: str) -> list[list[str]]:
        raise RuntimeError("File is encrypted, password required for extraction")

    monkeypatch.setattr(layouts, "read_table", unreadable)
    assert not importer.identify(str(statement))


def test_a_pad_is_not_taken_for_a_saved_row(
    importer: StatementImporter, statement: Path, tmp_path: Path
) -> None:
    ledger = tmp_path / "pad.beancount"
    ledger.write_text(
        f"2026-01-01 open {ACCOUNT}\n2026-01-01 open Equity:Open\n"
        f"2026-05-30 pad {ACCOUNT} Equity:Open\n2026-05-31 balance {ACCOUNT} 120 TWD\n",
        encoding="utf-8",
    )
    existing, errors, _ = loader.load_file(str(ledger))
    assert errors == []
    assert any(isinstance(e, data.Transaction) and e.flag == "P" for e in existing)
    entries = importer.extract(str(statement), [])
    importer.deduplicate(entries, existing)
    assert duplicates(entries)[0] is False
