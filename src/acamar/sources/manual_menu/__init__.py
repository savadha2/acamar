"""Lunch/breakfast menu — parsed from a manually uploaded PDF, not scraped or emailed.

Neither nutrition-vendor site can be scraped (robots.txt, settled in
CLAUDE.md) and the Gmail-subscription path (`sources.gmail_menu`) is still
blocked on the district actually emailing the menu. This package reads
whatever PDF a household member uploads through the Pilot Cockpit
(`POST /admin/menu-upload`) instead of waiting on either.
"""

from acamar.sources.manual_menu.connector import ManualMenuConnector
from acamar.sources.manual_menu.parser import Meal, parse_menu_pdf

__all__ = ["ManualMenuConnector", "Meal", "parse_menu_pdf"]
