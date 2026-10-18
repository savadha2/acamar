"""Tests for `dashboard/cockpit_app.py` — added 2026-09-29 alongside
`_menu_reminder_lit` (docs/p2-5-and-attention-card-implementation-spec.md
§B). Not a full sweep of this module's pure logic (see backlog item 34 for
that broader, still-open gap, unrelated to this change) — just the new
function, HTTP mocked via monkeypatched `_menu_coverage` rather than
`requests` directly (simpler, and `_menu_coverage`'s own request shape is
untouched by this change).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from acamar.dashboard import cockpit_app


def test_no_source_id_configured_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cockpit_app, "_SETTINGS", SimpleNamespace(manual_menu_source_id=None)
    )

    assert cockpit_app._menu_reminder_lit("token") is None


def test_lit_when_next_months_lunch_coverage_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cockpit_app, "_SETTINGS", SimpleNamespace(manual_menu_source_id="src-id")
    )
    monkeypatch.setattr(
        cockpit_app,
        "_menu_coverage",
        lambda *a, **k: {"covered_days": [], "days_in_month": 30},
    )

    assert cockpit_app._menu_reminder_lit("token") is True


def test_not_lit_when_next_months_lunch_has_any_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cockpit_app, "_SETTINGS", SimpleNamespace(manual_menu_source_id="src-id")
    )
    monkeypatch.setattr(
        cockpit_app,
        "_menu_coverage",
        lambda *a, **k: {"covered_days": [1, 2], "days_in_month": 30},
    )

    assert cockpit_app._menu_reminder_lit("token") is False


def test_returns_none_not_lit_when_the_coverage_check_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cockpit_app, "_SETTINGS", SimpleNamespace(manual_menu_source_id="src-id")
    )

    def _raise(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise requests.RequestException("boom")

    monkeypatch.setattr(cockpit_app, "_menu_coverage", _raise)

    # A failed check must never read as "lit" — an error isn't evidence a
    # menu is actually missing.
    assert cockpit_app._menu_reminder_lit("token") is None


def test_checks_next_calendar_month_not_the_current_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cockpit_app, "_SETTINGS", SimpleNamespace(manual_menu_source_id="src-id")
    )
    calls: list[dict[str, Any]] = []

    def _record(_token: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"covered_days": [], "days_in_month": 30}

    monkeypatch.setattr(cockpit_app, "_menu_coverage", _record)

    cockpit_app._menu_reminder_lit("token")

    assert len(calls) == 1
    assert calls[0]["meal"] == "lunch"
    # Whatever "next month" resolves to right now, it must not be this
    # month — the whole point of checking ahead, not the month already in
    # progress.
    import datetime as dt

    today = dt.date.today()
    assert (calls[0]["year"], calls[0]["month"]) != (today.year, today.month)


# ---------------------------------------------------------------------------
# _recency_angle — per-domain expected cadence (2026-10-01, cupertino_events)
# ---------------------------------------------------------------------------
# Addresses part of backlog item 34 ("zero unit tests for this module's pure
# logic, including _recency_angle") for the function actually touched here,
# not a full sweep of the rest of that gap.


def test_default_domain_fully_droops_at_3x_the_flat_cadence() -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    last_run = (now - timedelta(hours=12)).isoformat()  # 3 * 4h default

    assert cockpit_app._recency_angle(last_run, now=now) == 90.0


def test_same_age_reads_overdue_by_default_but_not_yet_for_city() -> None:
    # 6 hours old crosses `_tile_html`'s own `angle > 30` "stale" threshold
    # under the flat 4h default (45deg) — but cupertino_events (city,
    # once-daily) isn't actually overdue yet at 6 hours (7.5deg, well under
    # 30). This exact false alarm — a perfectly healthy once-daily source
    # reading "overdue" most of the day — is the bug this fix exists to
    # prevent.
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    last_run = (now - timedelta(hours=6)).isoformat()

    assert cockpit_app._recency_angle(last_run, now=now) == 45.0
    assert cockpit_app._recency_angle(last_run, now=now, domain="city", category="event") == 7.5


def test_city_domain_does_become_stale_once_genuinely_overdue() -> None:
    # The per-domain cadence changes the threshold, not whether staleness can
    # ever be detected at all.
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    last_run = (now - timedelta(hours=73)).isoformat()  # > 3 * 24h

    assert (
        cockpit_app._recency_angle(last_run, now=now, domain="city", category="event") == 90.0
    )


def test_no_last_run_at_is_never_stale_regardless_of_domain() -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    assert cockpit_app._recency_angle(None, now=now, domain="city", category="event") == 0.0


def test_gauge_config_looks_up_cadence_from_the_sources_own_domain_and_category() -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    last_run = (now - timedelta(hours=6)).isoformat()
    source = {"last_status": "ok", "last_run_at": last_run, "domain": "city", "category": "event"}

    cfg = cockpit_app._gauge_config(source, now=now)

    assert cfg["angle"] == 7.5  # not yet stale — same scenario as the test above


def test_domain_alone_is_not_enough_manual_menu_never_decays_despite_sharing_school() -> None:
    # Real bug found by review (2026-10-02): keying the override by `domain`
    # alone meant manual_menu (school/menu, upload-triggered, no fixed
    # cadence) inherited faria_events' (school/event, 6x/day) 4h expectation
    # just for sharing Domain.SCHOOL. A week-old upload is completely normal
    # for manual_menu and must never read as "overdue".
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    last_run = (now - timedelta(days=7)).isoformat()

    faria_angle = cockpit_app._recency_angle(
        last_run, now=now, domain="school", category="event"
    )
    menu_angle = cockpit_app._recency_angle(last_run, now=now, domain="school", category="menu")

    assert faria_angle == 90.0  # a week with no run IS genuinely overdue for faria_events
    assert menu_angle == 0.0  # but never for manual_menu — it has no fixed cadence at all


# ---------------------------------------------------------------------------
# _is_stale / the "Attention only" filter agreeing with Master Caution
# (2026-10-02, found by review: these used to disagree — a stale-but-
# last-run-succeeded source lit Master Caution globally while reading
# "ALL NOMINAL" and being hidden entirely under "Attention only".)
# ---------------------------------------------------------------------------


def test_is_stale_true_for_an_ok_source_past_its_own_threshold() -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    last_run = (now - timedelta(hours=73)).isoformat()
    source = {"last_status": "ok", "last_run_at": last_run, "domain": "city", "category": "event"}

    assert cockpit_app._is_stale(source, now=now) is True


def test_is_stale_false_for_an_ok_source_within_its_own_cadence() -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    last_run = (now - timedelta(hours=6)).isoformat()
    source = {"last_status": "ok", "last_run_at": last_run, "domain": "city", "category": "event"}

    assert cockpit_app._is_stale(source, now=now) is False


def test_is_stale_false_for_error_status_handled_by_its_own_check_elsewhere() -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    last_run = (now - timedelta(days=30)).isoformat()
    source = {
        "last_status": "error",
        "last_run_at": last_run,
        "domain": "city",
        "category": "event",
    }

    assert cockpit_app._is_stale(source, now=now) is False


def test_attention_only_filter_now_catches_a_stale_but_ok_source() -> None:
    # The actual end-to-end regression: this exact source used to read
    # "ALL NOMINAL" and vanish under "Attention only" despite being the
    # reason Master Caution was lit.
    stale_but_ok = {
        "domain": "city",
        "category": "event",
        "last_status": "ok",
        "last_run_at": (datetime(2026, 10, 1, 12, 0, tzinfo=UTC) - timedelta(hours=73)).isoformat(),
    }

    html = cockpit_app._source_tiles_html([stale_but_ok], attention_only=True)

    assert "ALL NOMINAL" not in html
    assert "1 NEEDS ATTENTION" in html
