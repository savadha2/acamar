"""Settings loading and the get_settings cache."""

from __future__ import annotations

import pytest

from acamar.config import Settings, get_settings


def test_defaults_apply_with_no_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DATABASE_URL", "OLLAMA_MODEL", "SCRAPER_MAX_ATTEMPTS"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)
    assert settings.database_url == "postgresql://localhost:5432/acamar"
    assert settings.ollama_model == "llama3"
    assert settings.scraper_max_attempts == 3


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://db:5432/other")
    monkeypatch.setenv("SCRAPER_MIN_REQUEST_INTERVAL_SECONDS", "12.5")
    settings = Settings(_env_file=None)
    assert settings.database_url == "postgresql://db:5432/other"
    assert settings.scraper_min_request_interval_seconds == 12.5


def test_unknown_environment_variables_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACAMAR_TOTALLY_UNKNOWN", "x")
    Settings(_env_file=None)  # must not raise


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "phi3")
    first = get_settings()
    monkeypatch.setenv("OLLAMA_MODEL", "llama3")
    assert get_settings() is first  # cached, env change not picked up
    assert first.ollama_model == "phi3"

    get_settings.cache_clear()
    assert get_settings().ollama_model == "llama3"
