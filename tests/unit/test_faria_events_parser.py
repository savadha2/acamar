"""Parser tests: synthetic HTML for edge cases, the real capture for the snapshot."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from acamar.domain.enums import Category, Domain
from acamar.domain.models import RawDocument
from acamar.sources.faria_events.parser import (
    CalendarStructureError,
    parse_calendar_html,
)

# ---------------------------------------------------------------------------
# synthetic-HTML builders — the smallest markup the parser needs to see
# ---------------------------------------------------------------------------

_WEEKDAYS = '<div class="fsCalendarWeekdays"><span>Sunday</span></div>'


def _page(*bodies: str) -> str:
    inner = "".join(bodies)
    return f'<html><body><div class="fsElement fsCalendar">{_WEEKDAYS}{inner}</div></body></html>'


def _event(
    occur_id: str,
    title: str,
    *,
    start: str | None = None,
    end: str | None = None,
    all_day: bool = False,
) -> str:
    if all_day:
        time_range = '<div class="fsTimeRange"><span class="fsAllDayEvent">All Day</span>'
        if start:  # a stray <time> alongside "All Day" — all-day must still win
            time_range += f'<time datetime="{start}" class="fsStartTime"></time>'
        time_range += "</div>"
    elif start:
        time_range = '<div class="fsTimeRange">'
        time_range += f'<time datetime="{start}" class="fsStartTime"></time>'
        if end:
            time_range += f'<time datetime="{end}" class="fsEndTime"></time>'
        time_range += "</div>"
    else:
        time_range = ""
    anchor = (
        f'<a class="fsCalendarTitle fsCalendarEventLink" title="{title}" '
        f'data-occur-id="{occur_id}" href="#">{title}</a>'
    )
    return f'<div class="fsCalendarInfo fsCalendarEvent">{anchor}{time_range}</div>'


def _daybox(year: int, month_zero_indexed: int, day: int, *events: str) -> str:
    cell = (
        f'<div class="fsCalendarDate" data-year="{year}" '
        f'data-month="{month_zero_indexed}" data-day="{day}"></div>'
    )
    return f'<div class="fsCalendarDaybox fsStateHasEvents">{cell}{"".join(events)}</div>'


# ---------------------------------------------------------------------------
# structural robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "not_a_calendar",
    [
        "",
        "   ",
        "<html><body>You are being redirected</body></html>",
        "<html><body><form>Sign in</form></body></html>",
    ],
)
def test_non_calendar_input_raises(not_a_calendar: str) -> None:
    with pytest.raises(CalendarStructureError):
        parse_calendar_html(not_a_calendar)


def test_calendar_with_no_events_returns_empty_list() -> None:
    assert parse_calendar_html(_page()) == []


def test_blank_occur_id_is_skipped_others_survive() -> None:
    good = _daybox(2026, 8, 4, _event("111_x_y", "Real", all_day=True))
    blank = _daybox(2026, 8, 5, _event("", "Ghost", all_day=True))
    docs = parse_calendar_html(_page(good, blank))
    assert [d.title for d in docs] == ["Real"]


def test_out_of_range_month_falls_back_to_time_then_survives() -> None:
    # data-month=12 -> date() raises -> no cell date -> use <time>
    box = _daybox(
        2026, 12, 9, _event("222_x_y", "Odd", start="2026-09-09T17:00:00-07:00")
    )
    docs = parse_calendar_html(_page(box))
    assert docs[0].event_date == date(2026, 9, 9)


def test_event_with_no_date_anywhere_is_dropped() -> None:
    # no daybox, no <time>, and an occur-id that isn't ISO -> unusable
    loose = _event("not-an-iso-id", "Nowhere")
    assert parse_calendar_html(_page(loose)) == []


# ---------------------------------------------------------------------------
# data edge cases
# ---------------------------------------------------------------------------


def test_html_entities_in_title_are_decoded() -> None:
    box = _daybox(
        2026, 8, 4, _event("1_x_y", "Author &amp; Illustrator &#39;Day&#39;", all_day=True)
    )
    (doc,) = parse_calendar_html(_page(box))
    assert doc.title == "Author & Illustrator 'Day'"
    assert "Author & Illustrator 'Day'" in doc.content


def test_timed_event_without_end_time() -> None:
    box = _daybox(
        2026, 8, 4, _event("1_x_y", "Open House", start="2026-09-04T18:00:00-07:00")
    )
    (doc,) = parse_calendar_html(_page(box))
    assert "from 6:00 PM." in doc.content
    assert " to " not in doc.content


def test_all_day_flag_wins_over_a_stray_time_tag() -> None:
    box = _daybox(
        2026, 8, 4,
        _event("1_x_y", "Spirit Week", start="2026-09-04T09:00:00-07:00", all_day=True),
    )
    (doc,) = parse_calendar_html(_page(box))
    assert "all day" in doc.content
    assert "AM" not in doc.content and "PM" not in doc.content


def test_event_only_in_hidden_day_view_uses_time_for_date() -> None:
    # a copy with no surrounding daybox, only the <time> offset
    hidden = _event("9_x_y", "Board Meeting", start="2026-09-15T19:00:00-07:00")
    (doc,) = parse_calendar_html(_page(f'<div class="fsCalendarDayView">{hidden}</div>'))
    assert doc.event_date == date(2026, 9, 15)


def test_multi_day_event_across_month_boundary() -> None:
    occur = "5_2026-09-29T00:00:00Z_2026-10-03T00:00:00Z"
    boxes = _page(
        _daybox(2026, 8, 29, _event(occur, "Book Fair", all_day=True)),
        _daybox(2026, 8, 30, _event(occur, "Book Fair", all_day=True)),
        _daybox(2026, 9, 1, _event(occur, "Book Fair", all_day=True)),
        _daybox(2026, 9, 2, _event(occur, "Book Fair", all_day=True)),
    )
    (doc,) = parse_calendar_html(boxes)
    assert doc.event_date == date(2026, 9, 29)
    assert "September 29, 2026" in doc.content
    assert "October 2, 2026" in doc.content


def test_same_title_different_dates_are_distinct_documents() -> None:
    boxes = _page(
        _daybox(2026, 8, 4, _event("77_2026-09-04T00:00:00Z_x", "Coffee", all_day=True)),
        _daybox(2026, 9, 2, _event("77_2026-10-02T00:00:00Z_x", "Coffee", all_day=True)),
    )
    docs = parse_calendar_html(boxes)
    assert len({d.external_id for d in docs}) == 2
    assert {d.event_date for d in docs} == {date(2026, 9, 4), date(2026, 10, 2)}


def test_repeated_markup_for_one_event_is_deduped() -> None:
    occur = "1_2026-09-04T00:00:00Z_x"
    ev = _event(occur, "Assembly", all_day=True)
    # same event: grid copy + hidden day-view copy on the same day
    box = _daybox(2026, 8, 4, ev, ev)
    docs = parse_calendar_html(_page(box))
    assert len(docs) == 1


# ---------------------------------------------------------------------------
# the real captured page — snapshot + spot checks
# ---------------------------------------------------------------------------


@pytest.fixture
def documents(faria_calendar_html: str) -> list[RawDocument]:
    return parse_calendar_html(faria_calendar_html)


def test_matches_the_checked_in_snapshot(
    documents: list[RawDocument], faria_expected_events: list[dict[str, Any]]
) -> None:
    actual = [
        {
            "external_id": d.external_id,
            "title": d.title,
            "domain": d.domain.value,
            "category": d.category.value,
            "event_date": d.event_date.isoformat() if d.event_date else None,
            "published_at": d.published_at,
            "student_ref": d.student_ref,
            "content": d.content,
        }
        for d in documents
    ]
    assert actual == faria_expected_events


def test_output_is_sorted_by_date_then_title(documents: list[RawDocument]) -> None:
    keys = [(d.event_date, d.title) for d in documents]
    assert keys == sorted(keys)


def test_every_document_is_a_school_event(documents: list[RawDocument]) -> None:
    for doc in documents:
        assert doc.domain is Domain.SCHOOL
        assert doc.category is Category.EVENT
        assert doc.content.strip()
        assert doc.published_at is None
        assert doc.student_ref is None


def test_evening_event_keeps_local_date_not_utc(documents: list[RawDocument]) -> None:
    # occur-id says 2026-09-10T00:45Z; the event is really Sept 9 evening
    feef = next(d for d in documents if d.title == "FEEF Meeting")
    assert feef.event_date == date(2026, 9, 9)


def test_parsing_is_idempotent(faria_calendar_html: str) -> None:
    first = [d.model_dump(mode="json") for d in parse_calendar_html(faria_calendar_html)]
    second = [d.model_dump(mode="json") for d in parse_calendar_html(faria_calendar_html)]
    assert first == second
