"""Cupertino Parks & Rec events — scraped from the city's public events pages.

Index: https://www.cupertino.gov/Parks-Recreation/Events (a plain list of
event links, no dates). Each linked event's own page is then fetched
separately to learn its date (if one is currently posted at all).

robots.txt allows the index page and most individual events, but explicitly
disallows several specific event paths (e.g. `/Events/Monster-Mash/*`) — this
connector checks every URL it fetches against robots.txt individually, not
just once like `faria_events` does for its single calendar URL. Full design
and real findings: docs/cupertino-events-source-design.md.
"""

from acamar.sources.cupertino_events.connector import CupertinoEventsConnector
from acamar.sources.cupertino_events.parser import (
    IndexStructureError,
    extract_event_date,
    parse_event_detail_html,
    parse_index_html,
)

__all__ = [
    "CupertinoEventsConnector",
    "IndexStructureError",
    "extract_event_date",
    "parse_event_detail_html",
    "parse_index_html",
]
