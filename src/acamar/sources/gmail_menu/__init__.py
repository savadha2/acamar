"""District menu — parsed from an auto-subscribed Gmail email, never scraped.

``cusdk8nutrition.com`` and ``schoolnutritionandfitness.com`` both disallow
automated access in their ``robots.txt`` (settled in CLAUDE.md) — this package
reads the subscription email instead of touching either site.
"""

from acamar.sources.gmail_menu.connector import GmailMenuConnector
from acamar.sources.gmail_menu.parser import parse_menu_email

__all__ = ["GmailMenuConnector", "parse_menu_email"]
