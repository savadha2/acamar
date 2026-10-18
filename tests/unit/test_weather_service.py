"""weather_advice() — deterministic thresholds, no model call involved."""

from __future__ import annotations

from acamar.ports.weather import WeatherForecast
from acamar.services.weather import weather_advice


def _forecast(
    *,
    temp_high_f: float = 70.0,
    temp_low_f: float = 55.0,
    precipitation_probability: float = 10.0,
    condition: str = "clear",
) -> WeatherForecast:
    return WeatherForecast(
        temp_high_f=temp_high_f,
        temp_low_f=temp_low_f,
        precipitation_probability=precipitation_probability,
        condition=condition,
    )


def test_mild_dry_day_has_no_advice() -> None:
    assert weather_advice(_forecast()) == []


def test_high_precip_probability_suggests_an_umbrella() -> None:
    advice = weather_advice(_forecast(precipitation_probability=40.0))
    assert "umbrella" in advice


def test_precip_just_under_threshold_has_no_umbrella_advice() -> None:
    advice = weather_advice(_forecast(precipitation_probability=39.9))
    assert "umbrella" not in advice


def test_cold_low_suggests_dressing_warm() -> None:
    advice = weather_advice(_forecast(temp_low_f=44.9))
    assert "warm" in advice


def test_low_at_threshold_has_no_warm_advice() -> None:
    advice = weather_advice(_forecast(temp_low_f=45.0))
    assert "warm" not in advice


def test_hot_high_suggests_sun_protection() -> None:
    advice = weather_advice(_forecast(temp_high_f=80.1))
    assert "sun" in advice


def test_cold_and_rainy_day_returns_both() -> None:
    advice = weather_advice(_forecast(precipitation_probability=50.0, temp_low_f=40.0))
    assert set(advice) == {"umbrella", "warm"}
