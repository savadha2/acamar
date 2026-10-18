"""Faria Elementary events — scraped from the public Finalsite calendar.

Source URL: https://faria.cusdk8.org/about-us/school-calendar
robots.txt: that path is allowed; ``Crawl-delay: 5``. The ``/production/calendar``
JSON endpoints are disallowed, so this connector parses the server-rendered
month grid instead.
"""

from acamar.sources.faria_events.connector import FariaEventsConnector
from acamar.sources.faria_events.parser import (
    CalendarStructureError,
    parse_calendar_html,
)

__all__ = ["CalendarStructureError", "FariaEventsConnector", "parse_calendar_html"]
