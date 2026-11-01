"""The 交易明細 layouts that StatementImporter reads.

Banks export their statements in different layouts. A layout is known by its
header row and turns the export into a Statement: rows in the order of the
running balance, and the day the export ends. The importer works only on
Statement and Row, so another bank's layout is another parser here.
"""

from __future__ import annotations

import csv
import datetime
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Row:
    line: int
    time: str
    # The running balance follows the transaction time, so a row is dated by it;
    # the booking date can fall on a later business day.
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


SHEET = "xl/worksheets/sheet1.xml"
COLUMNS = "ABCDEF"
_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _read_xlsx(path: str) -> list[list[str]]:
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


def _read_csv(path: str) -> list[list[str]]:
    with open(path, encoding="utf-8-sig", newline="") as file:
        return [[cell.strip() for cell in row] for row in csv.reader(file)]


def read_table(path: str) -> list[list[str]]:
    return _read_xlsx(path) if zipfile.is_zipfile(path) else _read_csv(path)


def _date(text: str) -> datetime.date:
    return datetime.date.fromisoformat(text.replace("/", "-"))


def _decimal(text: str, where: str) -> Decimal:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"{where}: {text!r} is not a number") from None


# One signed 金額 column, ascending rows, and a 共N筆資料 footer with the export time.
SIGNED = ("交易日", "帳務日", "摘要", "金額", "餘額", "備註")
_SIGNED_FOOTER = re.compile(r"共(\d+)筆資料\s*資料日期：(\d{4}/\d{2}/\d{2})(?: \d{2}:\d{2}:\d{2})?")


def _parse_signed(rows: list[list[str]], start: int) -> Statement:
    footer = _SIGNED_FOOTER.fullmatch(rows[-1][0])
    if footer is None:
        raise ValueError(f"row {len(rows)}: expected the 共N筆資料 footer, got {rows[-1][0]!r}")
    parsed = []
    for line, (time, booked, summary, amount, balance, note) in enumerate(
        rows[start + 1 : -1], start=start + 2
    ):
        parsed.append(
            Row(
                line=line,
                time=time,
                date=_date(time.split(" ")[0]),
                booked=_date(booked),
                summary=summary,
                amount=_decimal(amount, f"D{line}"),
                balance=_decimal(balance, f"E{line}"),
                note=note,
            )
        )
    if len(parsed) != int(footer[1]):
        raise ValueError(f"the footer counts {footer[1]} rows, the sheet has {len(parsed)}")
    return Statement(parsed, _date(footer[2]))


# 提出 and 存入 columns, newest row first, the count and period above the header,
# and a total per side below the rows.
SPLIT = ("交易日期", "帳務日期", "說明", "提出", "存入", "餘額", "交易資訊", "備註")
_COUNT = re.compile(r"筆數：(\d+) 筆")
_PERIOD = re.compile(r"\(自 (\d{4}/\d{2}/\d{2}) 至 (\d{4}/\d{2}/\d{2})\)")
_TOTAL = re.compile(r"總金額 \S+ ([\d,]+(?:\.\d+)?) \( 共 (\d+) 筆 \)")
_NONE = ("", "−", "-")


def _split_row(line: int, cells: list[str]) -> Row:
    time, booked, summary, paid, received, balance, detail, note = cells
    time = " ".join(time.split())
    if (paid in _NONE) == (received in _NONE):
        raise ValueError(f"row {line}: expected one of 提出 {paid!r} and 存入 {received!r}")
    if paid in _NONE:
        amount = _decimal(received, f"row {line} 存入")
    else:
        amount = -_decimal(paid, f"row {line} 提出")
    if detail not in _NONE:
        # The counterparty's bank and account, which a transfer rule looks for.
        summary = f"{summary} {' '.join(detail.split())}"
    return Row(
        line=line,
        time=time,
        date=_date(time.split(" ")[0]),
        booked=_date(booked),
        summary=summary,
        amount=amount,
        balance=_decimal(balance, f"row {line} 餘額"),
        note="" if note in _NONE else note,
    )


def _parse_split(rows: list[list[str]], start: int) -> Statement:
    above = " ".join(" ".join(row) for row in rows[:start])
    count, period = _COUNT.search(above), _PERIOD.search(above)
    if count is None or period is None:
        raise ValueError("expected the 筆數 and the period above the header")
    end = start + 1
    while end < len(rows) and len(rows[end]) == len(SPLIT):
        end += 1
    parsed = [_split_row(line + 1, rows[line]) for line in reversed(range(start + 1, end))]
    if len(parsed) != int(count[1]):
        raise ValueError(f"the header counts {count[1]} rows, the file has {len(parsed)}")
    totals = {row[0]: _TOTAL.fullmatch(row[1]) for row in rows[end:] if len(row) == 2}
    for side, sign in (("提出", -1), ("存入", 1)):
        total = totals.get(side)
        if total is None:
            raise ValueError(f"expected the {side} total below the rows")
        amounts = [sign * row.amount for row in parsed if sign * row.amount > 0]
        if (sum(amounts), len(amounts)) != (_decimal(total[1], side), int(total[2])):
            raise ValueError(
                f"the {side} total is {total[1]} in {total[2]} rows,"
                f" the rows add up to {sum(amounts)} in {len(amounts)}"
            )
    return Statement(parsed, _date(period[2]))


_LAYOUTS: dict[tuple[str, ...], Callable[[list[list[str]], int], Statement]] = {
    SIGNED: _parse_signed,
    SPLIT: _parse_split,
}


def _layout(
    rows: list[list[str]], title: str
) -> tuple[Callable[[list[list[str]], int], Statement], int] | None:
    if not rows or not rows[0] or rows[0][0] != title:
        return None
    for start, row in enumerate(rows):
        layout = _LAYOUTS.get(tuple(row))
        if layout is not None:
            return layout, start
    return None


def is_statement(path: str, title: str) -> bool:
    return _layout(read_table(path), title) is not None


def parse(path: str, title: str) -> Statement:
    """Read the statement and check that its rows are complete and in order."""
    rows = read_table(path)
    found = _layout(rows, title)
    if found is None:
        raise ValueError(f"{path} is not a {title!r} export")
    layout, start = found
    statement = layout(rows, start)
    # Each balance must follow from the previous one; a gap means a lost or reordered row.
    for previous, row in zip(statement.rows, statement.rows[1:], strict=False):
        if previous.balance + row.amount != row.balance:
            raise ValueError(
                f"row {row.line}: {previous.balance} + {row.amount} is not the balance {row.balance}"
            )
    return statement
