"""parse_calendar_page_html / page_url.

Fixtures are real, unedited captures of cupertino.gov's Parks & Recreation
Event Calendar listing (both pages), fetched 2026-10-25 from a residential
connection — the site's Akamai WAF 403s Google Cloud egress.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from acamar.domain.enums import Category, Domain
from acamar.domain.models import RawDocument
from acamar.sources.cupertino_events.parser import (
    CalendarStructureError,
    page_url,
    parse_calendar_page_html,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "cupertino_events"
_CALENDAR = "https://www.cupertino.gov/Parks-Recreation/Events/Parks-and-Recreation-Event-Calendar"


def _page(n: int) -> str:
    return (_FIXTURES / f"calendar_page{n}.html").read_text(encoding="utf-8")


def _docs(n: int) -> dict[str, RawDocument]:
    page = parse_calendar_page_html(_page(n), base_url=_CALENDAR)
    return {d.external_id: d for d in page.documents}


# ---------------------------------------------------------------------------
# the real listing
# ---------------------------------------------------------------------------


def test_finds_every_event_on_both_real_pages() -> None:
    assert sorted(_docs(1)) == [
        "mcclellan-events/baer-blacksmith-shop-demonstrations",
        "mcclellan-events/environmental-education-center-open-house",
        "mcclellan-events/join-a-ranger-bird-id-and-guided-hike",
        "mcclellan-events/join-a-ranger-night-hike-and-flashlight-tour",
        "mcclellan-events/watershed-habitat-of-the-steelhead-nature-walk",
        "tree-lighting/tree-lighting-event",
    ]
    assert sorted(_docs(2)) == [
        "breakfast-with-santa/breakfast-with-santa-event",
        "signing-santa/signing-santa-event",
    ]


def test_reads_the_pager_total_from_either_page() -> None:
    assert parse_calendar_page_html(_page(1), base_url=_CALENDAR).total_pages == 2
    assert parse_calendar_page_html(_page(2), base_url=_CALENDAR).total_pages == 2


def test_date_comes_from_the_listings_explicit_day_month_year() -> None:
    assert _docs(1)["tree-lighting/tree-lighting-event"].event_date == date(2026, 12, 4)
    assert _docs(2)["signing-santa/signing-santa-event"].event_date == date(2026, 12, 12)


def test_tree_lighting_is_a_city_event_with_its_real_fields() -> None:
    doc = _docs(1)["tree-lighting/tree-lighting-event"]
    assert doc.title == "Tree Lighting"
    assert doc.domain is Domain.CITY
    assert doc.category is Category.EVENT
    assert doc.url == "https://www.cupertino.gov/Parks-Recreation/Events/Tree-Lighting/Tree-Lighting-Event"
    assert doc.summary is not None and doc.summary.startswith("Kick off the holiday season")
    assert "Quinlan Community Center" in doc.content


def test_summary_is_the_listings_own_description_so_no_llm_is_needed() -> None:
    doc = _docs(1)["mcclellan-events/join-a-ranger-bird-id-and-guided-hike"]
    assert doc.summary is not None
    assert doc.summary.startswith("The Park Rangers invite you to learn about the birds")
    assert doc.headline is None  # IngestionService's deterministic branch sets it


def test_repeating_event_notes_its_extra_dates_in_content() -> None:
    doc = _docs(1)["mcclellan-events/environmental-education-center-open-house"]
    assert doc.content.endswith("Also on 8 more dates")


def test_one_off_event_has_no_extra_dates_note() -> None:
    assert "more date" not in _docs(1)["tree-lighting/tree-lighting-event"].content


def test_external_id_is_stable_across_parses() -> None:
    assert sorted(_docs(1)) == sorted(_docs(1))


# ---------------------------------------------------------------------------
# structure + fault isolation
# ---------------------------------------------------------------------------

_ARTICLE = """
<article>
  <a href="/Parks-Recreation/Events/Some-Program/Some-Event">
    <h2 class="list-item-title">{title}</h2>
    <span class="part-date">{day}</span><span class="part-month">Nov</span>
    <span class="part-year">2026</span>
    <span class="list-item-block-desc">A description.</span>
  </a>
</article>
"""


def _listing(*articles: str, pager: str = "Page 1 of 1") -> str:
    return (
        '<html><body><div class="events-list-container">'
        + "".join(articles)
        + f'</div><div class="seamless-pagination-info">{pager}</div></body></html>'
    )


def test_a_quiet_listing_with_no_events_is_empty_not_an_error() -> None:
    page = parse_calendar_page_html(_listing(), base_url=_CALENDAR)
    assert page.documents == []
    assert page.total_pages == 1


def test_no_listing_container_at_all_raises() -> None:
    with pytest.raises(CalendarStructureError):
        parse_calendar_page_html("<html><body><p>Redesigned!</p></body></html>", base_url=_CALENDAR)


def test_an_entry_missing_its_date_is_skipped_others_kept() -> None:
    html = _listing(_ARTICLE.format(title="Good", day="07"), _ARTICLE.format(title="Bad", day=""))
    docs = parse_calendar_page_html(html, base_url=_CALENDAR).documents
    assert [d.title for d in docs] == ["Good"]


def test_an_impossible_date_is_skipped_not_guessed() -> None:
    html = _listing(_ARTICLE.format(title="Feb 31st-ish", day="31").replace("Nov", "Feb"))
    assert parse_calendar_page_html(html, base_url=_CALENDAR).documents == []


def test_missing_pager_reads_as_a_single_page() -> None:
    html = _listing(_ARTICLE.format(title="Good", day="07"), pager="")
    assert parse_calendar_page_html(html, base_url=_CALENDAR).total_pages == 1


# ---------------------------------------------------------------------------
# page_url
# ---------------------------------------------------------------------------


def test_page_one_is_the_bare_calendar_url() -> None:
    assert page_url(_CALENDAR, 1) == _CALENDAR


def test_later_pages_use_the_sites_own_pageindex_parameter_verbatim() -> None:
    assert page_url(_CALENDAR, 2) == (
        f"{_CALENDAR}?dlv_PR%20Events%20Listing%20-%20Custom=(pageindex=2)"
    )
