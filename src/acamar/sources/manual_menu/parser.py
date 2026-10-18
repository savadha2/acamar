"""Parses an uploaded lunch/breakfast menu PDF into per-day `RawDocument`s.

Built against two real samples (September 2026 lunch + breakfast, captured
2026-09-25 from the district's own PDF export) rather than guessed at, same
discipline as every other parser in this codebase. `pdfplumber.extract_tables()`
finds the calendar grid cleanly: one table with a `Monday..Friday` header row
followed by up to five week rows, each populated cell reading
``"<day-of-month> <menu text>"`` — either three "Option N - ..." blocks
(lunch) or a single entrée (breakfast). A blank cell or one whose text is
just "No School" means no menu that day.

**The PDF states a year but never a month** — confirmed absent from both real
samples' text and layout. Which month an upload covers can't be inferred from
its content; the operator must say so explicitly (see `year`/`month` below).
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from io import BytesIO
from typing import Literal

import pdfplumber

from acamar.domain.enums import Category, Domain
from acamar.domain.models import RawDocument

Meal = Literal["breakfast", "lunch"]

_WEEKDAY_HEADER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
_CELL_PATTERN = re.compile(r"^(?P<day>\d{1,2})\s+(?P<body>.*)$", re.DOTALL)
_NO_SCHOOL_PATTERN = re.compile(r"^no school\b", re.IGNORECASE)


def parse_menu_pdf(pdf_bytes: bytes, *, meal: Meal, year: int, month: int) -> list[RawDocument]:
    """Parse one month's breakfast or lunch menu PDF into per-day documents.

    ``external_id`` is ``f"menu::{meal}::{event_date.isoformat()}"`` — keyed
    by meal + calendar day, not the uploaded file, so re-uploading a
    corrected PDF for the same month updates the existing day-rows instead
    of duplicating them.

    Raises:
        ValueError: the PDF has no recognisable calendar-grid table, or a
            populated cell doesn't start with a day-of-month number, or that
            number is out of range for ``year``/``month``.
    """
    table = _calendar_table(pdf_bytes)
    days_in_month = calendar.monthrange(year, month)[1]
    documents: list[RawDocument] = []
    for row in table[1:]:  # row 0 is the Monday..Friday header
        for cell in row:
            doc = _cell_to_document(
                cell, meal=meal, year=year, month=month, days_in_month=days_in_month
            )
            if doc is not None:
                documents.append(doc)
    return documents


def _calendar_table(pdf_bytes: bytes) -> list[list[str | None]]:
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        if not pdf.pages:
            raise ValueError("menu PDF has no pages")
        tables = pdf.pages[0].extract_tables()
    for candidate in tables:
        header = [(c or "").strip() for c in candidate[0]] if candidate else []
        # Filters out blank cells before comparing (2026-10-01, real bug: an
        # October lunch PDF's extracted header came back as `["Monday", "",
        # "Tuesday", "Wednesday", "Thursday", "Friday"]` — a 6th, empty
        # gutter column pdfplumber picked up between Monday and Tuesday that
        # the two September samples this parser was originally built against
        # didn't have. An exact-equality check against the hardcoded 5-cell
        # header rejected this real, correctly-shaped calendar table outright
        # ("no Monday..Friday calendar table found"). The phantom column
        # never breaks row parsing itself — `_cell_to_document` already
        # skips any cell that doesn't start with a digit, which an empty
        # gutter cell never does — so filtering blanks here is sufficient;
        # nothing downstream needs to know the phantom column exists.
        if [cell for cell in header if cell] == _WEEKDAY_HEADER:
            return candidate
    raise ValueError("no Monday..Friday calendar table found in the menu PDF")


def _cell_to_document(
    cell: str | None, *, meal: Meal, year: int, month: int, days_in_month: int
) -> RawDocument | None:
    stripped = cell.strip() if cell else ""
    if not stripped or not stripped[0].isdigit():
        # Either a genuinely blank grid cell, or — the real case this guards,
        # found parsing the September 2026 sample — the page's footer note
        # bleeding into an unused trailing cell (a short month leaves empty
        # cells at the end of the last week's row, and pdfplumber's table
        # detection merges the nearby footer text box into them). Neither is
        # a menu day, so skip rather than error.
        return None
    match = _CELL_PATTERN.match(stripped)
    if match is None:
        raise ValueError(f"menu cell doesn't start with a day number: {cell!r}")
    day = int(match.group("day"))
    if not 1 <= day <= days_in_month:
        raise ValueError(f"menu cell day {day} is out of range for {year}-{month:02d}")
    body = match.group("body").strip()
    if _NO_SCHOOL_PATTERN.match(body):
        return None
    event_date = date(year, month, day)
    meal_label = meal.capitalize()
    readable_body = body.replace("\n", ", ")
    return RawDocument(
        external_id=f"menu::{meal}::{event_date.isoformat()}",
        title=f"{meal_label} Menu — {event_date:%A, %B %-d, %Y}",
        content=f"{meal_label} on {event_date:%A, %B %-d, %Y}: {readable_body}",
        domain=Domain.SCHOOL,
        category=Category.MENU,
        event_date=event_date,
    )
