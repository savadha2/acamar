"""parse_index_html / parse_event_detail_html / extract_event_date.

HTML fixtures are hand-reconstructed from real content fetched live from
cupertino.gov on 2026-10-01, not raw `curl` captures (the site's own Akamai
edge WAF blocks direct curl/httpx access from this project's usual dev
environment) — see each fixture file's own header comment, and
docs/cupertino-events-source-design.md §2, for exactly what was and wasn't
independently verified.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from acamar.domain.enums import Category, Domain
from acamar.sources.cupertino_events.parser import (
    IndexStructureError,
    extract_event_date,
    parse_event_detail_html,
    parse_index_html,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "cupertino_events"
_BASE_URL = "https://www.cupertino.gov/Parks-Recreation/Events"
_TODAY = date(2026, 10, 2)


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_index_html
# ---------------------------------------------------------------------------


def test_finds_every_real_event_link() -> None:
    entries = parse_index_html(_fixture("index.html"), base_url=_BASE_URL)

    titles = {title for title, _url in entries}
    assert "Pumpkin Pool-ooza" in titles
    assert "Tree Lighting" in titles
    assert "Movies in the Park" in titles
    # Confirmed real, robots-disallowed events: the PARSER still returns them
    # — filtering by robots.txt happens in the connector, not here. Keeping
    # that separation explicit and tested.
    assert "Monster Mash" in titles
    assert "Pooch Plunge" in titles


def test_excludes_nav_chrome_and_the_language_switcher() -> None:
    entries = parse_index_html(_fixture("index.html"), base_url=_BASE_URL)

    urls = {url for _title, url in entries}
    assert not any("Meetings/Cupertino-Calendar" in url for url in urls)
    assert not any("oc_lang=" in url for url in urls)
    assert not any(url.rstrip("/") == _BASE_URL for url in urls)


def test_urls_are_absolute() -> None:
    entries = parse_index_html(_fixture("index.html"), base_url=_BASE_URL)

    assert all(url.startswith("https://www.cupertino.gov/") for _title, url in entries)


def test_deduplicates_by_url() -> None:
    html = """
    <a href="/Parks-Recreation/Events/Tree-Lighting">Tree Lighting</a>
    <a href="/Parks-Recreation/Events/Tree-Lighting">Tree Lighting (again)</a>
    """
    entries = parse_index_html(html, base_url=_BASE_URL)

    assert len(entries) == 1


def test_deduplicates_by_normalized_slug_not_raw_url_string() -> None:
    # Real bug found by review (2026-10-02): a case- or trailing-slash-
    # differing variant of the same event used to slip past dedup here
    # (exact string match) only to collide later at external_id/upsert time
    # — a wasted fetch plus a non-deterministic silent overwrite.
    html = """
    <a href="/Parks-Recreation/Events/Tree-Lighting">Tree Lighting</a>
    <a href="/Parks-Recreation/Events/tree-lighting/">Tree Lighting</a>
    """
    entries = parse_index_html(html, base_url=_BASE_URL)

    assert len(entries) == 1


def test_no_event_links_at_all_raises() -> None:
    with pytest.raises(IndexStructureError):
        parse_index_html("<html><body>nothing here</body></html>", base_url=_BASE_URL)


def test_known_non_event_utility_links_are_excluded() -> None:
    # Confirmed real during the 2026-10-02 live run: these two match the
    # /Events/<slug> shape but are utility/info pages, not individual
    # events (docs/cupertino-events-source-design.md §10).
    html = """
    <a href="/Parks-Recreation/Events/Tree-Lighting">Tree Lighting</a>
    <a href="/Parks-Recreation/Events/Parks-and-Recreation-Event-Calendar">Calendar</a>
    <a href="/Parks-Recreation/Events/Festival-Information">Festival Info</a>
    """
    entries = parse_index_html(html, base_url=_BASE_URL)

    titles = {title for title, _url in entries}
    assert titles == {"Tree Lighting"}


def test_excluding_every_link_still_raises_not_returns_empty() -> None:
    # If a future site change means ONLY utility links are ever found, that
    # should still surface as a failed run (same as zero links at all) —
    # not a silently empty, always-succeeding ingestion.
    html = '<a href="/Parks-Recreation/Events/Festival-Information">Festival Info</a>'
    with pytest.raises(IndexStructureError):
        parse_index_html(html, base_url=_BASE_URL)


# ---------------------------------------------------------------------------
# parse_event_detail_html — a real, scheduled event
# ---------------------------------------------------------------------------


def test_tree_lighting_resolves_the_real_year_less_date() -> None:
    doc = parse_event_detail_html(
        _fixture("tree_lighting.html"),
        url=f"{_BASE_URL}/Tree-Lighting",
        title="Tree Lighting",
        today=_TODAY,
    )

    assert doc is not None
    assert doc.event_date == date(2026, 12, 4)


def test_tree_lighting_is_tagged_city_event() -> None:
    doc = parse_event_detail_html(
        _fixture("tree_lighting.html"),
        url=f"{_BASE_URL}/Tree-Lighting",
        title="Tree Lighting",
        today=_TODAY,
    )

    assert doc is not None
    assert doc.domain == Domain.CITY
    assert doc.category == Category.EVENT
    assert doc.student_ref is None


def test_tree_lighting_carries_its_own_url_for_the_dashboard_hyperlink() -> None:
    url = f"{_BASE_URL}/Tree-Lighting"
    doc = parse_event_detail_html(
        _fixture("tree_lighting.html"), url=url, title="Tree Lighting", today=_TODAY
    )

    assert doc is not None
    assert doc.url == url


def test_tree_lighting_external_id_is_the_lowercased_url_slug() -> None:
    doc = parse_event_detail_html(
        _fixture("tree_lighting.html"),
        url=f"{_BASE_URL}/Tree-Lighting",
        title="Tree Lighting",
        today=_TODAY,
    )

    assert doc is not None
    assert doc.external_id == "tree-lighting"


def test_tree_lighting_external_id_is_stable_across_parses() -> None:
    html = _fixture("tree_lighting.html")
    first = parse_event_detail_html(
        html, url=f"{_BASE_URL}/Tree-Lighting", title="Tree Lighting", today=_TODAY
    )
    second = parse_event_detail_html(
        html, url=f"{_BASE_URL}/Tree-Lighting", title="Tree Lighting", today=_TODAY
    )

    assert first is not None and second is not None
    assert first.external_id == second.external_id


def test_tree_lighting_content_includes_the_real_venue_text() -> None:
    doc = parse_event_detail_html(
        _fixture("tree_lighting.html"),
        url=f"{_BASE_URL}/Tree-Lighting",
        title="Tree Lighting",
        today=_TODAY,
    )

    assert doc is not None
    assert "Quinlan Community Center" in doc.content


def test_title_comes_from_the_index_page_link_text_not_the_detail_page() -> None:
    # The index page's own link text is already clean; passed straight
    # through rather than re-derived from the detail page's own <title>
    # (which would carry a " | City of Cupertino" site-wide suffix).
    doc = parse_event_detail_html(
        _fixture("tree_lighting.html"),
        url=f"{_BASE_URL}/Tree-Lighting",
        title="Tree Lighting",
        today=_TODAY,
    )

    assert doc is not None
    assert doc.title == "Tree Lighting"
    assert "City of Cupertino" not in doc.title


# ---------------------------------------------------------------------------
# parse_event_detail_html — an off-season event with nothing scheduled
# ---------------------------------------------------------------------------


def test_off_season_page_with_no_bold_date_returns_none() -> None:
    doc = parse_event_detail_html(
        _fixture("movies_in_the_park.html"),
        url=f"{_BASE_URL}/Movies-in-the-Park",
        title="Movies in the Park",
        today=_TODAY,
    )

    assert doc is None


# ---------------------------------------------------------------------------
# extract_event_date — pure, isolated from any HTML parsing
# ---------------------------------------------------------------------------


def test_year_less_date_resolves_to_the_nearest_future_matching_weekday() -> None:
    # Dec 4, 2026 really is a Friday; Dec 4, 2027 is a Saturday — confirmed
    # via calendar.monthrange/date.strftime before writing this assertion,
    # not assumed.
    assert extract_event_date(["Friday, December 4"], today=date(2026, 10, 2)) == date(
        2026, 12, 4
    )


def test_year_less_date_rolls_to_next_year_once_this_years_has_passed() -> None:
    # Dec 4, 2026 has already passed by this `today`; Dec 4, 2027 is checked
    # next but is a Saturday, not a Friday, so it must NOT be returned —
    # correctly falls through to None rather than guessing the wrong year.
    assert extract_event_date(["Friday, December 4"], today=date(2026, 12, 10)) is None


def test_year_bearing_date_with_consistent_weekday_resolves() -> None:
    # July 4, 2026 really is a Saturday — confirmed, not assumed.
    assert extract_event_date(["Saturday, July 4, 2026"], today=date(2026, 1, 1)) == date(
        2026, 7, 4
    )


def test_year_bearing_date_with_inconsistent_weekday_is_rejected() -> None:
    # July 4, 2026 is a Saturday, not a Tuesday — a deliberately
    # self-inconsistent fixture; must not be silently trusted.
    assert extract_event_date(["Tuesday, July 4, 2026"], today=date(2026, 1, 1)) is None


def test_no_date_shaped_fragment_at_all_returns_none() -> None:
    assert extract_event_date(["When do the movies start?", "8 p.m."], today=_TODAY) is None


def test_weekday_less_year_bearing_date_is_trusted_as_is() -> None:
    # Real shape, confirmed on the live site (design doc §2): Fourth-of-
    # July's page states its next instance as plain "July 4, 2027", no
    # weekday at all. Nothing to cross-check it against — trusted as-is,
    # same as every other year-bearing fragment already is.
    assert extract_event_date(["July 4, 2027"], today=date(2026, 10, 2)) == date(2027, 7, 4)


def test_weekday_less_year_less_date_is_unresolvable() -> None:
    # No weekday and no year: genuinely no anchor to pick which year "July
    # 4" means. Decline rather than guess.
    assert extract_event_date(["July 4"], today=date(2026, 10, 2)) is None


def test_ordinal_day_suffix_is_tolerated() -> None:
    # Confirmed real elsewhere in this codebase's own date handling
    # (ingestion.service._ABSOLUTE_DATE_PATTERN already strips st/nd/rd/th)
    # — nothing established this site never uses one either.
    assert extract_event_date(["Friday, December 4th"], today=date(2026, 10, 2)) == date(
        2026, 12, 4
    )
    assert extract_event_date(["Saturday, July 4th, 2026"], today=date(2026, 1, 1)) == date(
        2026, 7, 4
    )


def test_faq_bold_fragments_are_not_mistaken_for_a_date() -> None:
    # The real Movies-in-the-Park shape: several bold FAQ fragments, none of
    # them date-shaped, plus a real date buried among them.
    fragments = [
        "When do the movies start?",
        "What do I bring?",
        "Friday, December 4",
        "Can I bring my dog?",
    ]
    assert extract_event_date(fragments, today=date(2026, 10, 2)) == date(2026, 12, 4)


def test_empty_fragment_list_returns_none() -> None:
    assert extract_event_date([], today=_TODAY) is None
