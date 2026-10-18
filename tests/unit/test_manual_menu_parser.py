"""parse_menu_pdf — against real captured September 2026 sample PDFs."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from acamar.domain.enums import Category, Domain
from acamar.sources.manual_menu.parser import parse_menu_pdf

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "manual_menu"

# Weekdays in September 2026 that aren't "No School" (the 7th and 21st, both
# Mondays — Labor Day + a second holiday) — hand-verified against the real
# calendar this sample covers.
_SCHOOL_DAYS_SEPT_2026 = [
    1, 2, 3, 4, 8, 9, 10, 11, 14, 15, 16, 17, 18, 22, 23, 24, 25, 28, 29, 30,
]


def _lunch_pdf() -> bytes:
    return (_FIXTURES / "lunch_2026-09.pdf").read_bytes()


def _lunch_pdf_oct() -> bytes:
    return (_FIXTURES / "lunch_2026-10.pdf").read_bytes()


def _breakfast_pdf() -> bytes:
    return (_FIXTURES / "breakfast_2026-09.pdf").read_bytes()


def test_parses_one_document_per_school_day() -> None:
    docs = parse_menu_pdf(_lunch_pdf(), meal="lunch", year=2026, month=9)

    dates = [d.event_date for d in docs]
    assert None not in dates  # every parsed document must have a date
    assert sorted(d for d in dates if d is not None) == [
        date(2026, 9, day) for day in _SCHOOL_DAYS_SEPT_2026
    ]


def test_no_school_days_are_skipped_not_synthesized() -> None:
    docs = parse_menu_pdf(_lunch_pdf(), meal="lunch", year=2026, month=9)

    assert date(2026, 9, 7) not in {d.event_date for d in docs}
    assert date(2026, 9, 21) not in {d.event_date for d in docs}


def test_external_id_is_keyed_by_meal_and_day_not_the_file() -> None:
    docs = parse_menu_pdf(_lunch_pdf(), meal="lunch", year=2026, month=9)

    first = next(d for d in docs if d.event_date == date(2026, 9, 1))
    assert first.external_id == "menu::lunch::2026-09-01"


def test_documents_are_tagged_school_menu() -> None:
    docs = parse_menu_pdf(_lunch_pdf(), meal="lunch", year=2026, month=9)

    assert all(d.domain == Domain.SCHOOL for d in docs)
    assert all(d.category == Category.MENU for d in docs)


def test_content_reads_the_actual_menu_items() -> None:
    docs = parse_menu_pdf(_lunch_pdf(), meal="lunch", year=2026, month=9)

    first = next(d for d in docs if d.event_date == date(2026, 9, 1))
    assert "Chicken Sandwich" in first.content
    assert "Veggie Burger" in first.content
    assert "Turkey and Cheese" in first.content


def test_a_short_label_day_is_kept_as_freeform_content() -> None:
    # "Mix It Up Lunch Day!" (Sept 22) has no Option 1/2/3 structure at all —
    # must still produce a document, not be mistaken for "No School".
    docs = parse_menu_pdf(_lunch_pdf(), meal="lunch", year=2026, month=9)

    day22 = next(d for d in docs if d.event_date == date(2026, 9, 22))
    assert "Mix It Up Lunch Day" in day22.content


def test_trailing_footer_text_in_an_empty_grid_cell_is_not_a_parse_error() -> None:
    # Real shape found in the sample: September ends on a Wednesday, so the
    # last week's Thursday/Friday cells are empty — and pdfplumber's table
    # detection merges the page's footer note into them. Must not raise and
    # must not produce spurious documents for it.
    docs = parse_menu_pdf(_lunch_pdf(), meal="lunch", year=2026, month=9)

    assert not any("equal opportunity" in d.content for d in docs)


def test_breakfast_pdf_parses_into_meal_tagged_documents() -> None:
    docs = parse_menu_pdf(_breakfast_pdf(), meal="breakfast", year=2026, month=9)

    dates = [d.event_date for d in docs]
    assert None not in dates  # every parsed document must have a date
    assert sorted(d for d in dates if d is not None) == [
        date(2026, 9, day) for day in _SCHOOL_DAYS_SEPT_2026
    ]
    first = next(d for d in docs if d.event_date == date(2026, 9, 1))
    assert first.external_id == "menu::breakfast::2026-09-01"
    assert "Banana Strawberry" in first.content


def test_out_of_range_day_number_raises() -> None:
    # September has 30 days — a day-32 cell would mean either the wrong
    # month/year was supplied or the table was misread; either way, an
    # honest error beats a silently wrong date.
    with pytest.raises(ValueError, match="out of range"):
        parse_menu_pdf(_lunch_pdf(), meal="lunch", year=2026, month=2)  # Feb has 28 days


_BLANK_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n0\n%%EOF"
)
"""A minimal but structurally valid single blank-page PDF — no tables at all,
so this exercises the "wrong document entirely" path distinctly from a
malformed/corrupt file (which `ManualMenuConnector` already turns into a
`ConnectorError` via its broad catch, same as every other connector)."""


def test_a_pdf_with_no_table_at_all_raises() -> None:
    with pytest.raises(ValueError, match=r"no Monday\.\.Friday calendar table"):
        parse_menu_pdf(_BLANK_PDF, meal="lunch", year=2026, month=9)


def test_a_phantom_blank_header_column_does_not_break_table_detection() -> None:
    # Real bug, real file (2026-10-01): this October lunch PDF's extracted
    # header came back as ["Monday", "", "Tuesday", "Wednesday", "Thursday",
    # "Friday"] — pdfplumber picked up an extra, empty gutter column between
    # Monday and Tuesday that the September fixtures above don't have. An
    # exact-equality header check rejected this real, correctly-shaped
    # calendar table outright ("no Monday..Friday calendar table found"),
    # surfaced to the cockpit operator as an ingestion error on a perfectly
    # good file. Must not raise, and must still land every school day on the
    # right date despite the extra column.
    docs = parse_menu_pdf(_lunch_pdf_oct(), meal="lunch", year=2026, month=10)

    dates = {d.event_date for d in docs}
    assert date(2026, 10, 5) in dates  # a Monday — right of the phantom column
    assert date(2026, 10, 23) not in dates  # "No School", confirmed in the real PDF
    assert len(docs) == 21
