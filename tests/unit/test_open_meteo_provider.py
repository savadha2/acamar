"""OpenMeteoProvider — HTTP mocked via respx; cache TTL via FrozenClock.

`_PAYLOAD` is a 24-hour fixture, not a single daily aggregate (2026-09-29 —
the provider switched from Open-Meteo's `daily` endpoint to `hourly` +
an 8am-5pm window reduction, see `adapters/weather/open_meteo.py`'s module
docstring). Deliberately puts its temp/precip extremes *outside* the
8am-5pm window (hour 0 and hour 23) so a test that only checked "the
returned value equals some entry in the payload" couldn't pass by
accident — it has to actually be windowing, not just picking a global
max/min.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from acamar.adapters.weather.open_meteo import OpenMeteoProvider
from acamar.ports.weather import WeatherError
from tests.fakes import FrozenClock

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# 24 hourly entries, index = hour of day (local, matching `timezone=auto`).
# Within the 8am-5pm window (indices 8-17): temp peaks at 12:00 (77.4,
# also the global max) and troughs at 17:00 (58.0); precip peaks at 11:00
# (60%). Outside the window (indices 0 and 23): a colder temp (40.0) and a
# hotter temp (95.0), and a higher precip spike at 03:00 (95%) — all of
# which must be EXCLUDED from the windowed result, proving the window
# filter actually filters rather than just happening to pick real values.
_TIMES = [f"2026-09-16T{h:02d}:00" for h in range(24)]
_TEMPS = [
    40.0, 41, 42, 43, 44, 45, 46, 47,  # 00-07, outside window
    60.0, 64.0, 70.0, 74.0, 77.4, 75.0, 72.0, 68.0, 64.0, 58.0,  # 08-17
    50, 49, 48, 47, 46, 95.0,  # 18-23, outside window
]
_PRECIP = [
    0, 0, 0, 95, 0, 0, 0, 0,  # 00-07, outside window (95 at hour 3 excluded)
    10, 15, 20, 60, 55, 50, 45, 40, 35, 30,  # 08-17
    0, 0, 0, 0, 0, 0,  # 18-23
]
_CODES = [0] * 24
_CODES[11] = 63  # "rain likely" — at the precipitation peak's hour (11:00)

_PAYLOAD: dict[str, Any] = {
    "hourly": {
        "time": _TIMES,
        "temperature_2m": _TEMPS,
        "precipitation_probability": _PRECIP,
        "weather_code": _CODES,
    }
}


def _mock_forecast(payload: dict[str, Any] | None = None, status: int = 200) -> respx.Route:
    body = _PAYLOAD if payload is None else payload
    return respx.get(_FORECAST_URL).mock(return_value=httpx.Response(status, json=body))


@respx.mock
async def test_today_reduces_hourly_data_to_the_8am_5pm_window() -> None:
    _mock_forecast()
    provider = OpenMeteoProvider(latitude=37.32, longitude=-122.04)

    forecast = await provider.today()

    assert forecast.temp_high_f == 77.4  # the window's hottest hour (12:00)
    assert forecast.temp_low_f == 58.0  # the window's coldest hour (17:00)
    assert forecast.precipitation_probability == 60.0  # window's peak (11:00)
    assert forecast.condition == "rain likely"  # code at the precip-peak hour


@respx.mock
async def test_hours_outside_the_school_day_window_are_excluded() -> None:
    # Same fixture as above, restated as its own test so the windowing
    # invariant has a name of its own, not just an incidental assertion —
    # the fixture's outside-window extremes (40.0 low, 95.0 high temp;
    # 95% precip at hour 3) must never appear in the result.
    _mock_forecast()
    provider = OpenMeteoProvider(latitude=37.32, longitude=-122.04)

    forecast = await provider.today()

    assert forecast.temp_high_f != 95.0
    assert forecast.temp_low_f != 40.0
    assert forecast.precipitation_probability != 95.0


@respx.mock
async def test_unknown_weather_code_falls_back_to_a_generic_condition() -> None:
    codes = [0] * 24
    codes[11] = 1234  # unmapped code, at the precip-peak hour
    payload: dict[str, Any] = {"hourly": {**_PAYLOAD["hourly"], "weather_code": codes}}
    _mock_forecast(payload)
    provider = OpenMeteoProvider(latitude=37.32, longitude=-122.04)

    forecast = await provider.today()

    assert forecast.condition == "mixed conditions"


@respx.mock
async def test_http_error_status_raises_weather_error() -> None:
    _mock_forecast(status=500)
    provider = OpenMeteoProvider(latitude=37.32, longitude=-122.04)

    with pytest.raises(WeatherError):
        await provider.today()


@respx.mock
async def test_malformed_payload_raises_weather_error() -> None:
    _mock_forecast(payload={"hourly": {}})
    provider = OpenMeteoProvider(latitude=37.32, longitude=-122.04)

    with pytest.raises(WeatherError):
        await provider.today()


@respx.mock
async def test_no_hours_in_window_raises_weather_error() -> None:
    # A malformed/truncated response with hourly data but nothing landing
    # in 8am-5pm (e.g. only overnight hours present) must fail loudly, not
    # silently return an empty/zero forecast.
    payload: dict[str, Any] = {
        "hourly": {
            "time": ["2026-09-16T00:00", "2026-09-16T01:00"],
            "temperature_2m": [40.0, 41.0],
            "precipitation_probability": [0, 0],
            "weather_code": [0, 0],
        }
    }
    _mock_forecast(payload)
    provider = OpenMeteoProvider(latitude=37.32, longitude=-122.04)

    with pytest.raises(WeatherError):
        await provider.today()


@respx.mock
async def test_second_call_within_ttl_reuses_the_cache() -> None:
    route = _mock_forecast()
    clock = FrozenClock(datetime(2026, 9, 16, 8, 0, tzinfo=UTC))
    provider = OpenMeteoProvider(
        latitude=37.32, longitude=-122.04, cache_ttl_seconds=1800, clock=clock
    )

    await provider.today()
    await provider.today()

    assert route.call_count == 1


@respx.mock
async def test_call_after_ttl_expiry_fetches_again() -> None:
    route = _mock_forecast()
    clock = FrozenClock(datetime(2026, 9, 16, 8, 0, tzinfo=UTC))
    provider = OpenMeteoProvider(
        latitude=37.32, longitude=-122.04, cache_ttl_seconds=1800, clock=clock
    )
    await provider.today()

    clock.advance(timedelta(seconds=1801))
    await provider.today()

    assert route.call_count == 2
