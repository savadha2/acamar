"""Parse Cupertino's Parks & Rec events pages into :class:`RawDocument`s.

Two distinct pages, two functions: the index page (a plain list of event
links, no dates at all) and one event's own detail page (which may or may not
currently state a date).

Built against real content fetched live from the real site 2026-10-01 — not
guessed at — see docs/cupertino-events-source-design.md §2 for the full
findings this was built from. Two real, load-bearing shapes confirmed there:

- A scheduled event states its date as a short, isolated ``<strong>``/``<b>``
  fragment (e.g. "Friday, December 4") — sometimes **with no year at all**.
  Other short bold fragments on the same kind of page are FAQ headings
  ("When do the movies start?"), not dates — the date regex exists
  specifically to pick the real date out from that noise.
- An event with nothing currently scheduled reads as a plain, non-bold
  sentence ("Movies in the Park will return in August 2027...") — this
  never matches the date pattern at all, which is the correct outcome: no
  document should be produced for it this run, not a fabricated date.

**Direct `curl`/`httpx` access to cupertino.gov from this project's usual dev
environment was found to be blocked by the site's own Akamai edge WAF
(403, regardless of User-Agent) while building this** — a real, confirmed
environmental constraint, not a guess. Whoever runs this connector for real
needs network egress that WAF doesn't block (a normal residential/office
connection works fine; a shared datacenter/cloud IP range may not).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from acamar.domain.enums import Category, Domain
from acamar.domain.models import RawDocument

logger = logging.getLogger("acamar.ingestion")

_HTML_PARSER = "lxml"

# Matches an /Events/<slug> detail-page link, not the bare index URL itself
# (confirmed necessary: the real index page's own nav includes the bare
# index URL repeated many times over, e.g. with `?oc_lang=...` query params
# for the language switcher — those have no path segment after "Events" and
# so never match this).
_EVENT_LINK_PATTERN = re.compile(r"/Parks-Recreation/Events/[^/?#]+/?$")

_NON_EVENT_SLUGS = frozenset(
    {
        # Confirmed real during the 2026-10-02 live run (see docs/
        # cupertino-events-source-design.md §10): these match
        # _EVENT_LINK_PATTERN (same /Events/<slug> shape as a real event)
        # but are utility/info pages, not individual events — there's no
        # structural way to tell them apart from the URL or link text
        # alone, so this is an explicit, known-finite denylist rather than
        # a heuristic. Harmless if left in (they just never match a date
        # and produce no document), but each one wastes a throttled fetch.
        "parks-and-recreation-event-calendar",
        "festival-information",
    }
)

_BOLD_TAGS = ("strong", "b")

_DATE_PATTERN = re.compile(
    r"(?:(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+)?"
    r"(?P<month>[A-Z][a-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,\s+(?P<year>\d{4}))?\b"
)
# NOTE vs. the original spec this was built from: the trailing ", <year>" is
# OPTIONAL here (a non-capturing `(?:...)?` group) — a real live page states
# its date as "Friday, December 4" with no year anywhere on the page at all;
# a regex requiring `\d{4}` unconditionally would never match it.
#
# Two more real gaps found by review (2026-10-02), both fixed the same way:
# the leading "<weekday>, " is now ALSO optional — the design doc's own real
# finding (§2: Fourth-of-July's page states its next instance as plain
# "July 4, 2027", no weekday at all) is a confirmed real shape this
# originally required-weekday pattern couldn't match — and the day number
# now tolerates an ordinal suffix ("4th", "22nd"), matching the same
# accommodation `ingestion.service._ABSOLUTE_DATE_PATTERN` already makes for
# Gmail-narrated dates; nothing confirmed this site never uses one, and the
# failure mode (silently indistinguishable from a genuinely unscheduled
# event) is the same one the optional-year fix above already guards against.

_MAX_CONTENT_CHARS = 1200


class IndexStructureError(ValueError):
    """The index page has no recognisable event links at all.

    Raised instead of silently returning ``[]`` so a site redesign surfaces
    as a failed ingestion run rather than a quietly empty events list — same
    reasoning as `faria_events.parser.CalendarStructureError`.
    """


def parse_index_html(html: str, *, base_url: str) -> list[tuple[str, str]]:
    """Return ``(title, absolute_url)`` for every linked event, de-duplicated by URL.

    ``title`` is the link's own text (already clean on the real page — e.g.
    "Tree Lighting" — so the detail page's own ``<title>``, which likely
    carries a site-wide suffix, is never used for this).

    Raises:
        IndexStructureError: no ``/Parks-Recreation/Events/<slug>`` link was
            found anywhere on the page — the real index always has ~27.
    """
    soup = BeautifulSoup(html or "", _HTML_PARSER)
    seen_slugs: set[str] = set()
    entries: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href = str(anchor["href"])
        if not _EVENT_LINK_PATTERN.search(href):
            continue
        slug = href.rstrip("/").rsplit("/", 1)[-1].lower()
        if slug in _NON_EVENT_SLUGS:
            continue
        # Dedup by the same normalized slug external_id is keyed by below
        # (2026-10-02, real bug found by review) — deduping by the raw
        # absolute URL string instead let a case- or trailing-slash-differing
        # variant of the same event slip through as two "different" entries:
        # a wasted extra fetch, and a silent external_id collision at upsert
        # time (whichever document happened to be upserted last would win,
        # non-deterministically, depending on index-page HTML order).
        if slug in seen_slugs:
            continue
        title = anchor.get_text(strip=True)
        if not title:
            continue
        seen_slugs.add(slug)
        entries.append((title, urljoin(base_url, href)))

    if not entries:
        raise IndexStructureError(
            "no /Parks-Recreation/Events/<slug> links found on the index page"
        )
    return entries


def parse_event_detail_html(
    html: str, *, url: str, title: str, today: date
) -> RawDocument | None:
    """Return one :class:`RawDocument` for this event, or ``None``.

    ``None`` is the expected, common outcome for an event with nothing
    currently scheduled ("will return in <year>") — not an error.
    """
    soup = BeautifulSoup(html or "", _HTML_PARSER)
    bold_fragments = [
        text
        for tag in soup.find_all(_BOLD_TAGS)
        if (text := tag.get_text(strip=True))
    ]
    event_date = extract_event_date(bold_fragments, today=today)
    if event_date is None:
        return None

    slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
    body = _body_text(soup)
    return RawDocument(
        external_id=slug,
        title=title,
        content=f"{title} — {body}" if body else title,
        domain=Domain.CITY,
        category=Category.EVENT,
        event_date=event_date,
        student_ref=None,  # whole-household, same default as faria_events
        url=url,  # lets the dashboard link straight to the real event page
    )


def extract_event_date(fragments: list[str], *, today: date) -> date | None:
    """First date-shaped fragment among ``fragments``, resolved to a real date.

    ``None`` if nothing matches the date pattern at all (confirmed real case:
    an "off-season" event page's prose never matches it), or if a
    date-shaped fragment is found but can't be resolved to a self-consistent
    date (its stated weekday doesn't match any candidate year) — logged, and
    treated the same as "no date found" rather than guessed.
    """
    for fragment in fragments:
        match = _DATE_PATTERN.search(fragment)
        if match is None:
            continue
        resolved = _resolve_match(match, today=today)
        if resolved is not None:
            return resolved
        logger.warning(
            "cupertino_events: date-shaped fragment did not resolve to a "
            "self-consistent date, skipping: %r",
            fragment,
        )
    return None


def _resolve_match(match: re.Match[str], *, today: date) -> date | None:
    weekday_name = match.group("weekday")  # None when the fragment has no weekday
    month_name = match.group("month")
    day = int(match.group("day"))
    year_str = match.group("year")

    if year_str is not None:
        candidate = _build_date(int(year_str), month_name, day)
        if candidate is None:
            return None
        if weekday_name is None:
            # Year stated, no weekday to cross-check against (the real
            # "July 4, 2027" shape) — nothing to validate it against, so
            # trust it as-is, same as a year-bearing fragment always has
            # been trusted here (even a past one — an upstream content
            # question, not this parser's to silently correct).
            return candidate
        # Year AND weekday stated: cross-validate. A mismatch means the
        # regex matched something that isn't really a self-consistent
        # calendar date; don't guess which half is wrong.
        return candidate if candidate.strftime("%A") == weekday_name else None

    if weekday_name is None:
        # No year, no weekday: no anchor at all to pick which year "July 4"
        # means. Genuinely ambiguous — decline rather than guess, same
        # anti-fabrication posture as everywhere else in this module.
        return None

    # No year stated (the real "Friday, December 4" shape). Try this year,
    # then next, and take the first that's (a) not already in the past and
    # (b) actually falls on the stated weekday — same "weekday math as a
    # confirming cross-check" discipline `ingestion.service._resolve_
    # relative_date` already uses for LLM-narrated dates, applied here to a
    # scraped one instead.
    for candidate_year in (today.year, today.year + 1):
        candidate = _build_date(candidate_year, month_name, day)
        if (
            candidate is not None
            and candidate >= today
            and candidate.strftime("%A") == weekday_name
        ):
            return candidate
    return None


def _build_date(year: int, month_name: str, day: int) -> date | None:
    try:
        month = datetime.strptime(month_name, "%B").month
        return date(year, month, day)
    except ValueError:
        return None


def _body_text(soup: BeautifulSoup, *, max_chars: int = _MAX_CONTENT_CHARS) -> str:
    """Trimmed real page prose for ``content``.

    Loose, not templated — see docs/cupertino-events-source-design.md §1
    decision #4. Pulled from ``<p>`` tags specifically: confirmed-noisy site-wide chrome
    (nav menus, the language switcher, breadcrumbs) renders as link lists,
    not paragraphs, on this site — real prose is reliably inside ``<p>``.
    **Not verified against a specific "main content" container selector**
    (no raw HTML was available to confirm one — see this module's own
    docstring); sanity-check this heuristic once real HTML is available,
    per docs/cupertino-events-source-design.md's own flagged gap.
    """
    paragraphs = [text for p in soup.find_all("p") if (text := p.get_text(" ", strip=True))]
    combined = re.sub(r"\s+", " ", " ".join(paragraphs)).strip()
    if len(combined) <= max_chars:
        return combined
    return combined[:max_chars].rsplit(" ", 1)[0] + "…"
