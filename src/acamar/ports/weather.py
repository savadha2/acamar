"""The weather-forecast port. The core never calls a weather API directly.

Deliberately not a ``SourceConnector`` — a forecast is a live, hours-stale
snapshot, not durable retrieval-worthy content, so it bypasses
``sources``/``documents`` entirely instead of becoming a new source type.
See docs/weather-design-proposal.md §1 for the reasoning.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict


class WeatherError(RuntimeError):
    """The weather API was unreachable or returned an unusable forecast."""


class WeatherForecast(BaseModel):
    """Today's forecast for the one location this app cares about."""

    model_config = ConfigDict(frozen=True)

    temp_high_f: float
    """The hottest hour between 8am-5pm today, not the whole day's high
    (2026-09-29 — see `adapters.weather.open_meteo`'s module docstring for
    why: a whole-day aggregate barely moves hour to hour, which is why the
    dashboard temperature used to read as static)."""
    temp_low_f: float
    """The coldest hour between 8am-5pm today — same school-day window as
    `temp_high_f`, not the whole day's low."""
    precipitation_probability: float
    """Highest percent chance of precipitation in any hour between
    8am-5pm today, 0-100 — not a whole-day aggregate."""
    condition: str
    """Short human-readable condition, e.g. "rain likely", "clear" — taken
    from the hour with the highest precipitation chance in the 8am-5pm
    window, the most representative single hour for "why the umbrella"."""


class WeatherProvider(Protocol):
    """Fetches today's forecast.

    Implementations are expected to cache — callers (the dashboard) may
    call this once per request.
    """

    async def today(self) -> WeatherForecast:
        """Return today's forecast, from cache if still fresh.

        Raises:
            WeatherError: the API was unreachable or returned something
                unusable. Callers should degrade gracefully (no weather
                card), not fail the whole request — see
                ``DashboardService._weather_card``.
        """
        ...
