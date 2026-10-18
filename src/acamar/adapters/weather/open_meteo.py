"""`WeatherProvider` backed by the free, keyless Open-Meteo forecast API.

No API key/account (docs/weather-design-proposal.md §3 — chosen specifically
so there's nothing new to provision in ``.env``). One HTTP call returns
per-hour data for today, which this module reduces to the 8am-5pm "school
day" worst case per metric (docs/p2-5-and-attention-card-implementation-spec.md
§A — 2026-09-29, replacing the original whole-day `daily` aggregate, which
is why the dashboard's temperature used to read as static most of the day:
a day's forecast *high* barely moves hour to hour once computed, no matter
how often it's re-fetched).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from acamar.adapters.clock import SystemClock
from acamar.config import get_settings
from acamar.ports.clock import Clock
from acamar.ports.weather import WeatherError, WeatherForecast

_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather-interpretation codes (open-meteo.com/en/docs), grouped to a
# short display phrase. A code missing from this table (new/undocumented)
# falls back to a generic phrase rather than raising — a slightly-vague
# label is fine, a missing card over one unmapped code is not.
_CONDITIONS: dict[int, str] = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "foggy",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain likely",
    63: "rain likely",
    65: "heavy rain likely",
    66: "freezing rain likely",
    67: "freezing rain likely",
    71: "light snow likely",
    73: "snow likely",
    75: "heavy snow likely",
    77: "snow grains likely",
    80: "rain showers likely",
    81: "rain showers likely",
    82: "violent rain showers likely",
    85: "snow showers likely",
    86: "heavy snow showers likely",
    95: "thunderstorms likely",
    96: "thunderstorms with hail likely",
    99: "thunderstorms with hail likely",
}
_UNKNOWN_CONDITION = "mixed conditions"

_SCHOOL_DAY_START = "08:00"
_SCHOOL_DAY_END = "17:00"
"""The window a daily-care advisory actually needs — not the whole day
(worst overnight/pre-dawn extremes are irrelevant to what a kid wears to
school). `hourly.time` entries are local (see `timezone=auto` below), so
a plain string slice/compare on the "HH:MM" portion is enough; no
timezone math needed (verified live against a real Open-Meteo response,
2026-09-29 — `timezone=auto` returns local, not UTC, timestamps)."""


class OpenMeteoProvider:
    """Implements `WeatherProvider`.

    Caches the last fetch in memory for ``cache_ttl_seconds`` — a forecast
    doesn't need re-fetching on every dashboard load, and this needs no
    table/Redis, just a process-lifetime singleton (see
    ``api.dependencies.get_weather_provider``). The cache lives on the
    instance, not the module, so it only works as a shared cache when one
    instance is reused across requests — that's what the singleton
    dependency is for.
    """

    def __init__(
        self,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        cache_ttl_seconds: int | None = None,
        client: httpx.AsyncClient | None = None,  # inject in tests / to share a pool
        clock: Clock | None = None,  # inject in tests
    ) -> None:
        settings = get_settings()
        self._latitude = settings.weather_latitude if latitude is None else latitude
        self._longitude = settings.weather_longitude if longitude is None else longitude
        self._ttl = (
            settings.weather_cache_ttl_seconds
            if cache_ttl_seconds is None
            else cache_ttl_seconds
        )
        self._client = client
        self._clock: Clock = clock or SystemClock()
        self._cached: tuple[datetime, WeatherForecast] | None = None

    async def today(self) -> WeatherForecast:
        if self._cached is not None:
            fetched_at, forecast = self._cached
            if (self._clock.now() - fetched_at).total_seconds() < self._ttl:
                return forecast
        forecast = await self._fetch()
        self._cached = (self._clock.now(), forecast)
        return forecast

    async def _fetch(self) -> WeatherForecast:
        # Own a short-lived client when none was injected; don't close a shared one.
        if self._client is not None:
            return await self._fetch_with(self._client)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            return await self._fetch_with(client)

    async def _fetch_with(self, client: httpx.AsyncClient) -> WeatherForecast:
        params: dict[str, str | int | float] = {
            "latitude": self._latitude,
            "longitude": self._longitude,
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "forecast_days": 1,
        }
        try:
            response = await client.get(_FORECAST_URL, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise WeatherError(f"open-meteo forecast fetch failed: {exc}") from exc
        return _parse(response.json())


def _parse(payload: dict[str, Any]) -> WeatherForecast:
    """Reduce the hourly response to the 8am-5pm worst case per metric.

    "Worst case" means a different direction per metric — the hottest
    school-day hour for heat/sun advice, the coldest for cold/jacket
    advice, the highest precipitation chance for umbrella advice — not one
    shared number. `WeatherForecast`'s field names stay `temp_high_f`/
    `temp_low_f` (unchanged from the old whole-day-aggregate shape) so
    `services.weather.weather_advice()`'s three thresholds need no changes
    at all — they now just receive window-scoped values instead of
    whole-day ones.
    """
    try:
        hourly = payload["hourly"]
        times: list[str] = hourly["time"]
        window = [
            i for i, t in enumerate(times) if _SCHOOL_DAY_START <= t[11:16] <= _SCHOOL_DAY_END
        ]
        if not window:
            raise ValueError("no hourly entries fall in the 8am-5pm window")

        window_temps = [float(hourly["temperature_2m"][i]) for i in window]
        window_precip = [float(hourly["precipitation_probability"][i]) for i in window]
        window_codes = [int(hourly["weather_code"][i]) for i in window]

        # Condition string follows the hour with the highest rain chance in
        # the window — the most representative single hour for "why the
        # umbrella", not just whichever hour happens to be first/last.
        worst_precip_index = window_precip.index(max(window_precip))

        return WeatherForecast(
            temp_high_f=max(window_temps),
            temp_low_f=min(window_temps),
            precipitation_probability=max(window_precip),
            condition=_CONDITIONS.get(window_codes[worst_precip_index], _UNKNOWN_CONDITION),
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise WeatherError(f"open-meteo returned an unusable forecast: {exc}") from exc
