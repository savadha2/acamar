"""SystemClock.today() — must be the household's local date, not UTC's.

Regression for the reported bug: an event on Friday read as "tomorrow" on
Wednesday evening, because `.today()` used UTC's date, which had already
rolled to Thursday.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo

import pytest

from acamar.adapters import clock as clock_module
from acamar.adapters.clock import SystemClock


class _FixedDatetime:
    """Stand-in for the module's `datetime` name — `datetime.now` is a
    classmethod on a built-in type and can't be monkeypatched directly.
    """

    @staticmethod
    def now(tz: tzinfo | None = None) -> datetime:
        # 2026-09-17 03:27 UTC == 2026-09-16 20:27 PDT (UTC-7): still
        # Wednesday evening in Cupertino, already Thursday in UTC.
        fixed = datetime(2026, 9, 17, 3, 27, tzinfo=UTC)
        return fixed.astimezone(tz) if tz is not None else fixed


def test_now_stays_utc() -> None:
    offset = SystemClock().now().utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0


def test_today_uses_the_household_local_date_not_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clock_module, "datetime", _FixedDatetime)

    assert SystemClock().today().isoformat() == "2026-09-16"
