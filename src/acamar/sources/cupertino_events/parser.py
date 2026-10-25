"""Parse Cupertino's Parks & Rec Event Calendar listing into :class:`RawDocument`s.

**Rebuilt 2026-10-25** against the calendar listing
(``/Parks-Recreation/Events/Parks-and-Recreation-Event-Calendar``), replacing
the original index-page + per-event-detail-page scrape. That original only
followed links exactly one level under ``/Events/`` and read a date out of
each detail page's bold text — but the real dated pages sit one level
deeper (``/Events/McClellan-Events/<event>``, ``/Events/Tree-Lighting/
Tree-Lighting-Event``), so most upcoming events were never found (4 stored
vs. 8 on the calendar the day this was rebuilt).

Built against two real pages captured 2026-10-25 (fixtures
``calendar_page1.html``/``calendar_page2.html``), not guessed at. The
listing is server-rendered; each event is one ``<article>``:

- ``h2.list-item-title`` — the event's own clean name.
- ``span.part-date`` / ``span.part-month`` / ``span.part-year`` — the
  (next) date, **with an explicit year** — no year inference needed, unlike
  the old detail-page parser.
- ``.list-item-block-desc`` — a clean one-paragraph description, good
  enough to show as-is (so this source needs no LLM summary at all).
- ``p.list-item-address`` — the venue.
- ``"N more dates"`` — a repeating event; the listing shows its next
  occurrence only, which rolls forward on later runs as each one passes.
- The pager reads ``"Page X of N"`` (no pagination links in the HTML —
  pages are addressed by a ``pageindex`` query parameter instead, see
  :func:`page_url`).

**cupertino.gov sits behind an Akamai edge WAF that 403s Google Cloud
egress outright** (2026-10-24, every URL including ``robots.txt``) — this
runs from the household Mac's launchd job, not on GCP. See CLAUDE.md.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from acamar.domain.enums import Category, Domain
from acamar.domain.models import RawDocument

logger = logging.getLogger("acamar.ingestion")

_HTML_PARSER = "lxml"

_PAGE_QUERY = "dlv_PR%20Events%20Listing%20-%20Custom=(pageindex={page})"
"""The calendar's own pagination parameter, verbatim from the real site."""

_PAGER_PATTERN = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)")
_EVENTS_PATH = "/parks-recreation/events/"


class CalendarStructureError(ValueError):
    """The calendar page has no event listing container at all.

    Raised instead of silently returning nothing so a site redesign surfaces
    as a failed ingestion run rather than a quietly empty events list — same
    reasoning as `faria_events.parser.CalendarStructureError`.
    """


@dataclass(frozen=True)
class CalendarPage:
    """One parsed listing page: its events, plus how many pages there are."""

    documents: list[RawDocument]
    total_pages: int


def page_url(calendar_url: str, page: int) -> str:
    """The URL of listing page ``page`` (1-based); page 1 is the bare calendar URL."""
    if page == 1:
        return calendar_url
    return f"{calendar_url}?{_PAGE_QUERY.format(page=page)}"


def parse_calendar_page_html(html: str, *, base_url: str) -> CalendarPage:
    """Every well-formed event on one listing page.

    A listing container with no events in it is a legitimately quiet
    listing — ``[]``, not an error. A malformed ``<article>`` (missing its
    title, link or any date part) is skipped and logged, never guessed at —
    one bad entry must not cost the others. No ``"Page X of N"`` pager is
    read as a single page.

    Raises:
        CalendarStructureError: no ``div.events-list-container`` at all —
            the page has changed shape, not merely gone quiet.
    """
    soup = BeautifulSoup(html or "", _HTML_PARSER)
    container = soup.find(class_="events-list-container")
    if not isinstance(container, Tag):
        raise CalendarStructureError(
            "no div.events-list-container on the calendar page — the site changed shape"
        )

    documents: list[RawDocument] = []
    for article in container.find_all("article"):
        if isinstance(article, Tag) and article.find(class_="list-item-title"):
            doc = _parse_article(article, base_url=base_url)
            if doc is not None:
                documents.append(doc)
    pager = _PAGER_PATTERN.search(soup.get_text(" "))
    return CalendarPage(documents=documents, total_pages=int(pager.group(2)) if pager else 1)


def _parse_article(article: Tag, *, base_url: str) -> RawDocument | None:
    title = _text(article, "list-item-title")
    link = article.find("a", href=True)
    event_date = _event_date(article)
    if not title or not isinstance(link, Tag) or event_date is None:
        logger.warning(
            "cupertino_events: skipping a malformed listing entry (title=%r)", title
        )
        return None

    url = urljoin(base_url, str(link["href"]))
    description = _text(article, "list-item-block-desc")
    address = _text(article, "list-item-address")
    more_dates = _more_dates(article)
    content = " — ".join(part for part in (title, description, address, more_dates) if part)
    return RawDocument(
        external_id=_external_id(url),
        title=title,
        content=content,
        domain=Domain.CITY,
        category=Category.EVENT,
        event_date=event_date,
        student_ref=None,  # whole-household, same default as faria_events
        url=url,  # lets the dashboard link straight to the real event page
        # The listing's own description is already clean (unlike the old
        # detail-page prose), so it IS the dashboard summary — no LLM call.
        # IngestionService's deterministic branch sets headline = title.
        summary=description or None,
    )


def _event_date(article: Tag) -> date | None:
    day, month, year = (_text(article, c) for c in ("part-date", "part-month", "part-year"))
    if not (day and month and year):
        return None
    try:
        return datetime.strptime(f"{day} {month[:3]} {year}", "%d %b %Y").date()
    except ValueError:
        return None


def _more_dates(article: Tag) -> str | None:
    text = _text(article, "published-on")
    return f"Also on {text}" if text and "more date" in text else None


def _external_id(url: str) -> str:
    """The event's path under ``/Events/``, lowercased — stable across runs.

    A repeating event keeps one ``external_id`` as its next date rolls
    forward, so it updates in place instead of piling up duplicates.
    """
    path = urlsplit(url).path.rstrip("/").lower()
    _, _, tail = path.partition(_EVENTS_PATH)
    return tail or path


def _text(article: Tag, css_class: str) -> str | None:
    el = article.find(class_=css_class)
    if not isinstance(el, Tag):
        return None
    text = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    return text or None
