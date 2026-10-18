"""Parse Faria's Finalsite calendar HTML into :class:`RawDocument` events.

Scope: the server-rendered **month grid** only. It carries an event's title,
date and time range — but not its location or description (those live behind a
JS-only detail view backed by the ``/production/calendar`` JSON endpoints, which
Faria's ``robots.txt`` disallows).

The calendar renders every event as an ``<a data-occur-id=...>`` inside a day
cell. ``data-occur-id`` has the form ``{eventId}_{startUTC}_{endUTC}`` and is
stable across scrapes, so it is used verbatim as the dedup ``external_id``.

A single event can appear several times in the markup (a hidden day-view copy,
and one cell per day for multi-day events). Occurrences are merged by
``data-occur-id``; the event date is taken from the day cells (which already
carry the correct local calendar date) so no timezone conversion is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from bs4 import BeautifulSoup
from bs4.element import Tag

from acamar.domain.enums import Category, Domain
from acamar.domain.models import RawDocument

# lxml over html.parser: the live page ships unclosed tags and stray whitespace
# that html.parser mis-nests, which would break find_parent() for the day cell.
_HTML_PARSER = "lxml"

# The weekday header row is present in every month view (events or not) and has
# survived several Finalsite template revisions; its absence means we were served
# something other than a calendar (redirect stub, login wall, redesign).
_CALENDAR_MARKER = ".fsCalendarWeekdays"

# Hardcoded: this parser only ever reads Faria's calendar. A second school gets
# its own connector, or this becomes a parameter. The real school name for
# multi-tenancy comes from the `schools` row, not the scraped page.
_SCHOOL_NAME = "Faria Elementary School"


class CalendarStructureError(ValueError):
    """The HTML is not a recognisable Finalsite calendar page.

    Raised instead of silently returning ``[]`` so a site redesign surfaces as a
    failed ingestion run rather than an empty, stale dashboard.
    """


def parse_calendar_html(html: str) -> list[RawDocument]:
    """Return one :class:`RawDocument` per unique event, sorted by (date, title).

    An empty month yields ``[]``. Input that isn't a calendar page raises
    :class:`CalendarStructureError`.
    """
    soup = BeautifulSoup(html or "", _HTML_PARSER)
    if soup.select_one(_CALENDAR_MARKER) is None:
        raise CalendarStructureError(
            f"no {_CALENDAR_MARKER} element — not a Finalsite calendar page"
        )

    occurrences: dict[str, _Occurrence] = {}
    for anchor in soup.find_all("a", attrs={"data-occur-id": True}):
        if not isinstance(anchor, Tag):
            continue
        occur_id = _attr(anchor, "data-occur-id").strip()
        if not occur_id:
            continue
        # Same event shows up many times (hidden day-view copy + one <a> per day
        # it spans); merge every copy into one accumulator keyed by occur-id.
        occ = occurrences.setdefault(occur_id, _Occurrence(occur_id=occur_id))
        occ.observe(anchor)

    # Drop anything we couldn't pin a date on rather than emit a partial event.
    documents = [
        doc
        for occ in occurrences.values()
        if (doc := occ.to_document()) is not None
    ]
    # Stable, human-meaningful order — not DOM order, which shifts with layout.
    return sorted(documents, key=lambda d: (d.event_date or date.min, d.title))


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


@dataclass
class _Occurrence:
    """Mutable accumulator for the (possibly repeated) markup of one event."""

    occur_id: str
    title: str = ""
    cell_dates: set[date] = field(default_factory=set)
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False

    def observe(self, anchor: Tag) -> None:
        """Fold one ``<a data-occur-id>`` element into this occurrence."""
        if not self.title:
            self.title = (_attr(anchor, "title") or anchor.get_text(strip=True)).strip()

        cell_date = _cell_date(anchor)
        if cell_date is not None:
            self.cell_dates.add(cell_date)

        # Times are identical across copies, so keep the first we see.
        time_range = _time_range(anchor)
        if time_range is None:
            return
        if time_range.select_one(".fsAllDayEvent") is not None:
            self.all_day = True
            return
        if self.start is None:
            self.start = _time_value(time_range, "time.fsStartTime")
        if self.end is None:
            self.end = _time_value(time_range, "time.fsEndTime")

    def to_document(self) -> RawDocument | None:
        """Build the :class:`RawDocument`, or ``None`` if the event is unusable."""
        event_date = self._event_date()
        if not self.title or event_date is None:
            return None
        return RawDocument(
            external_id=self.occur_id,
            title=self.title,
            content=self._content(event_date),
            domain=Domain.SCHOOL,
            category=Category.EVENT,
            event_date=event_date,
        )

    def _event_date(self) -> date | None:
        # Prefer the day cell: it's already the school's local calendar date.
        # The occur-id timestamps are UTC, so a 6pm event reads as "tomorrow".
        if self.cell_dates:
            return min(self.cell_dates)  # min = start day of a multi-day event
        if self.start is not None:
            return self.start.date()  # <time> carries a real offset, so safe
        # Last resort: the UTC start baked into data-occur-id (may be a day off
        # for a late-evening event — but this path only fires with no cell and
        # no <time>, which shouldn't happen on a well-formed page).
        parts = self.occur_id.split("_")
        if len(parts) >= 2:
            try:
                return datetime.fromisoformat(parts[1]).date()
            except ValueError:
                return None
        return None

    def _content(self, event_date: date) -> str:
        # `documents` has no JSON column, so the when/where is prose here — this
        # string is also what gets embedded and read back to the LLM.
        start_day = event_date
        end_day = max(self.cell_dates) if self.cell_dates else event_date
        spans_days = end_day > start_day

        if self.all_day or self.start is None:
            when = (
                f"all day from {_long(start_day)} through {_long(end_day)}"
                if spans_days
                else f"all day on {_long(start_day)}"
            )
        else:
            times = _clock(self.start)
            if self.end is not None:
                times += f" to {_clock(self.end)}"
            when = f"on {_long(start_day)} from {times}"

        return f"{_SCHOOL_NAME} event: {self.title} — {when}."


def _attr(tag: Tag, name: str) -> str:
    # bs4 hands back a list for space-separated attrs (e.g. class); normalise.
    value = tag.get(name, "")
    if isinstance(value, list):
        return " ".join(value)
    return value or ""


def _cell_date(anchor: Tag) -> date | None:
    """The calendar date of the day cell containing this event, if any."""
    box = anchor.find_parent("div", class_="fsCalendarDaybox")
    if not isinstance(box, Tag):
        return None
    cell = box.find("div", class_="fsCalendarDate")
    if not isinstance(cell, Tag):
        return None
    try:
        year = int(_attr(cell, "data-year"))
        month = int(_attr(cell, "data-month")) + 1  # Finalsite months are JS 0-indexed
        day = int(_attr(cell, "data-day"))
        return date(year, month, day)
    except ValueError:
        return None


def _time_range(anchor: Tag) -> Tag | None:
    # The <a> title and the .fsTimeRange are siblings under one wrapper div,
    # so scope the search to the anchor's parent, not the whole day cell.
    parent = anchor.parent
    if not isinstance(parent, Tag):
        return None
    found = parent.select_one(".fsTimeRange")
    return found if isinstance(found, Tag) else None


def _time_value(time_range: Tag, selector: str) -> datetime | None:
    el = time_range.select_one(selector)
    if not isinstance(el, Tag):
        return None
    raw = _attr(el, "datetime")
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


# %-d / %-I (no zero-pad) are glibc/BSD only — fine on macOS + Linux CI, the
# only places this runs; revisit if a Windows runner is ever added.
def _long(day: date) -> str:
    return day.strftime("%A, %B %-d, %Y")


def _clock(moment: datetime) -> str:
    return moment.strftime("%-I:%M %p")
