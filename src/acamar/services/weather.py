"""Turns a forecast into plain advice — thresholds, not a model call.

docs/weather-design-proposal.md §1: this is deliberately an ``if``, not
something routed through Router/Retrieval/Synthesis — the whole reason
weather bypasses that pipeline.
"""

from __future__ import annotations

from acamar.ports.weather import WeatherForecast

_UMBRELLA_PRECIP_PCT = 40.0
"""Precipitation probability (%) at/above which an umbrella gets suggested."""
_WARM_BELOW_F = 45.0
"""Today's 8am-5pm low (`WeatherForecast.temp_low_f`) below this suggests
a jacket — not the whole day's low (2026-09-29, see that field's own
docstring)."""
_HAT_SUNSCREEN_ABOVE_F = 80.0
"""Today's 8am-5pm high (`WeatherForecast.temp_high_f`) above this
suggests sun protection — not the whole day's high."""


def weather_advice(forecast: WeatherForecast) -> list[str]:
    """Which pieces of advice apply today, as stable codes.

    One of ``"umbrella"``, ``"warm"``, ``"sun"`` — never a sentence. Display
    phrasing (icon + label) is a UI concern and lives in one place
    (``dashboard.streamlit_app._ADVICE_DISPLAY``), not duplicated here.

    Each threshold is independent — a cold, rainy day returns both codes, a
    mild dry day returns an empty list.
    """
    advice: list[str] = []
    if forecast.precipitation_probability >= _UMBRELLA_PRECIP_PCT:
        advice.append("umbrella")
    if forecast.temp_low_f < _WARM_BELOW_F:
        advice.append("warm")
    if forecast.temp_high_f > _HAT_SUNSCREEN_ABOVE_F:
        advice.append("sun")
    return advice
