"""Controlled vocabularies shared across the pipeline.

`domain` and `category` are stored as free ``text`` in the database (denormalised
onto ``documents`` for fast routing), but every connector and reasoning node
should use these enums so a typo cannot create an unroutable document.
"""

from enum import StrEnum


class Domain(StrEnum):
    """The kind of source a document came from."""

    SCHOOL = "school"
    DAYCARE = "daycare"
    GMAIL = "gmail"
    FINANCIAL = "financial"
    PRODUCT_MANUAL = "product_manual"
    CITY = "city"
    """Municipal/community sources — e.g. ``cupertino_events``, the first one.
    Not ``COMMUNITY``/``RECREATION``: deliberately the broadest fit for any
    future non-school, non-Gmail city source, not just parks & rec."""


class Category(StrEnum):
    """What a document is about, within its domain."""

    EVENT = "event"
    MENU = "menu"
    REMINDER = "reminder"
    ACTIVITY = "activity"
    HOLIDAY = "holiday"
    """A no-school date. Distinct from REMINDER: "what holidays are coming up"
    is a specific-enough question to deserve its own bucket. Usually a
    synthetic document extracted from inside a noisier email, not the email
    itself — see ``ports.enricher.HolidayExtraction``.
    """


class QueryIntent(StrEnum):
    """What kind of question this is — Router's first, coarser decision.

    Closed taxonomy, same reasoning as `Organization` below: an open-ended
    intent label would be unfilterable and prone to sprawl. See
    docs/reasoning-pipeline-design.md §2.3.
    """

    DOCUMENT = "document"
    """Needs retrieval — event/menu/reminder/holiday/activity."""
    WEATHER = "weather"
    """Answerable directly from `WeatherProvider`, no retrieval."""
    COUNT = "count"
    """An exact aggregation ("how many X") — direct SQL count, no retrieval
    (retrieval's top-K is a similarity rank, not exhaustive)."""
    OUT_OF_SCOPE = "out_of_scope"
    """Decline rather than force a bad answer through retrieval."""


class OutOfScopeReason(StrEnum):
    """Why `OUT_OF_SCOPE` fired.

    Lets the decline message be honest instead of one flat unhelpful
    string for every case.
    """

    NEVER = "never"
    """No plausible future version of this system answers this."""
    NOT_YET_BUILT = "not_yet_built"
    """The domain/category is reserved (already a real enum value) but has
    no source ingesting into it yet."""


class SourceStatus(StrEnum):
    """Outcome of a source's most recent ingestion run (``sources.last_status``)."""

    OK = "ok"
    ERROR = "error"
    NEVER_RUN = "never_run"


class Organization(StrEnum):
    """Closed taxonomy for dashboard grouping (docs/dashboard-redesign-proposal.md §2.4).

    Deliberately closed, not free text — open labels would be unfilterable and
    prone to sprawl. Revise this set with a real decision, not a silent
    connector/prompt change.
    """

    DISTRICT = "district"
    """CUSD board / superintendent office."""
    FEEF = "feef"
    """Faria Educational Enrichment Fund — fundraising."""
    CLASSROOM = "classroom"
    """Teacher mail, homework, classroom projects."""
    FARIA_ADMIN = "faria_admin"
    """Faria front office / school-wide admin notices."""
    SCHOOL_CALENDAR = "school_calendar"
    """Faria's scraped calendar — not email, assigned deterministically."""
    CITY_RECREATION = "city_recreation"
    """``cupertino_events``' scraped calendar — not email, assigned
    deterministically, same precedent as ``SCHOOL_CALENDAR``."""
    OTHER = "other"
    """Unclassified, or the enrichment call failed (fail-open)."""
