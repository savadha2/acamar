"""School events/reminders, parsed from a filtered Gmail search.

Pulled from ParentSquare and direct teacher mail, never scraped. A second,
independent use of the Gmail connector alongside the (parked) menu one —
Gmail is not menu-only. See CLAUDE.md § "Current scope".
"""

from acamar.sources.gmail_school.connector import GmailSchoolConnector
from acamar.sources.gmail_school.parser import parse_school_email

__all__ = ["GmailSchoolConnector", "parse_school_email"]
