"""Cupertino Parks & Rec events — scraped from the city's public Event Calendar.

Listing: https://www.cupertino.gov/Parks-Recreation/Events/Parks-and-Recreation-Event-Calendar
(paged via a ``pageindex`` query parameter). Every field the dashboard needs
— title, full date, description, venue, link — is on the listing itself, so
individual event pages are never fetched (rebuilt 2026-10-25; the original
index-page + detail-page scrape missed most dated events). Each listing page
is still checked against robots.txt individually by `PoliteFetcher`.

Runs from the household Mac only: the site's Akamai WAF 403s Google Cloud
egress. Design history: docs/cupertino-events-source-design.md.
"""

from acamar.sources.cupertino_events.connector import CupertinoEventsConnector
from acamar.sources.cupertino_events.parser import (
    CalendarPage,
    CalendarStructureError,
    page_url,
    parse_calendar_page_html,
)

__all__ = [
    "CalendarPage",
    "CalendarStructureError",
    "CupertinoEventsConnector",
    "page_url",
    "parse_calendar_page_html",
]
