"""The real wall clock."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

# The one household this app serves is in Cupertino, CA. `.today()` must
# answer "what calendar day is it for them", not the server's UTC date —
# UTC is 7-8 hours ahead, so from ~5pm Pacific onward UTC has already
# rolled to the next day while it's still today locally (the actual bug: a
# Friday event read as "tomorrow" instead of 2 days out, reported evening
# of 2026-09-16).
_LOCAL_TZ = ZoneInfo("America/Los_Angeles")


class SystemClock:
    """`Clock` backed by the OS clock.

    `now()` stays UTC — the convention for stored timestamps. `today()` is
    the household's local calendar date; nothing that decides "is this due
    today/tomorrow" should ever use the UTC date instead.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)

    def today(self) -> date:
        return datetime.now(_LOCAL_TZ).date()
