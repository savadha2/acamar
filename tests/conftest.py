"""Shared test fixtures."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from acamar.config import get_settings

_FIXTURES = Path(__file__).parent / "fixtures" / "faria_events"


@pytest.fixture
def faria_calendar_html() -> str:
    """Real HTML captured from the Faria calendar (September 2026)."""
    return (_FIXTURES / "calendar_2026-09.html").read_text(encoding="utf-8")


@pytest.fixture
def faria_expected_events() -> list[dict[str, Any]]:
    """Checked-in snapshot of what the parser should produce from that capture."""
    raw = (_FIXTURES / "expected_events.json").read_text(encoding="utf-8")
    return cast("list[dict[str, Any]]", json.loads(raw))


@pytest.fixture(autouse=True)
def _isolate_settings() -> Iterator[None]:
    """Every test starts and ends with a clean settings cache (env is monkeypatched)."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
