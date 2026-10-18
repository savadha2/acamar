"""A time source, so ingestion runs are deterministic under test."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol


class Clock(Protocol):
    """Wall clock, injected everywhere a timestamp is recorded."""

    def now(self) -> datetime:
        """Timezone-aware current instant."""
        ...

    def today(self) -> date:
        """Current local date (used as the reconcile cut-off)."""
        ...
