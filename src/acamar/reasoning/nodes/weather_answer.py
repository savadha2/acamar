"""Weather branch — no LLM call at all.

Reuses `WeatherProvider`/`weather_advice` verbatim (same deterministic,
not-LLM-narrated philosophy as the dashboard chip). A `WeatherError` here
gets the same fail-soft treatment the dashboard already gives it — an
honest "couldn't check" answer, not a 502 (a deliberate, narrow exception
to NF1's general fail-loud rule, scoped to this one already-established
failure mode).
"""

from __future__ import annotations

from typing import Any

from acamar.ports.weather import WeatherError, WeatherProvider
from acamar.reasoning.state import QueryState
from acamar.services.weather import weather_advice

_ADVICE_PHRASING = {
    "umbrella": "bring an umbrella",
    "warm": "dress warm",
    "sun": "wear a hat and sunscreen",
}


class WeatherAnswerNode:
    """Answers a `WEATHER`-intent question directly, no retrieval/LLM."""

    def __init__(self, weather: WeatherProvider) -> None:
        self._weather = weather

    async def __call__(self, state: QueryState) -> dict[str, Any]:
        """Return `{"final_answer": ...}` from today's forecast."""
        try:
            forecast = await self._weather.today()
        except WeatherError:
            return {
                "final_answer": "I couldn't check the weather just now — try again in a bit.",
                "grounded": True,
            }
        temp = round(forecast.temp_high_f)
        answer = f"{temp}°F, {forecast.condition}."
        advice = weather_advice(forecast)
        if advice:
            phrases = [_ADVICE_PHRASING.get(code, code) for code in advice]
            answer += " " + "; ".join(phrases).capitalize() + "."
        return {"final_answer": answer, "grounded": True}
