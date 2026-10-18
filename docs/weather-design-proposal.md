# Weather card — architecture proposal (2026-09-15)

Status: **proposal, not implemented.** Written per request, before any code.
Goal: a small card on the dashboard — temperature + condition, plus a plain
"send an umbrella" / "dress warm" / "hat and sunscreen" suggestion — kept as
simple as the feature actually is.

---

## 1. The central fork: does weather go through `sources` → `documents`?

Every existing source (Faria calendar, Gmail) follows the same path:
`SourceConnector.fetch() -> list[RawDocument]` → enrichment → `documents`
row → retrieved by domain/category/date. Weather *could* follow the same
path. **Recommendation: it shouldn't.** Reasoning:

- `SourceConnector`/`documents` is shaped for durable, retrieval-worthy
  narrative content — something with a title, dedup'd by `external_id`,
  soft-deleted on removal, embedded for `/query`. A forecast is a live
  numeric snapshot that's stale in hours. Nobody needs "what was the
  temperature 3 weeks ago" answered via vector search.
- The umbrella/warm-clothes suggestion is **deterministic threshold
  logic** (precip % above X → umbrella), not something worth an LLM call.
  Routing it through Router → Retrieval → Synthesis → Critique would be
  pure overhead for what's really an `if` statement.
- `sources.school_id` is `NOT NULL` — every existing source is anchored to
  Faria. Weather is anchored to the household's location, not the school.
  It'd work today only by coincidence (one school, one household).

So this proposes a **second, smaller path** alongside the documents
pipeline, not a new row in it — same `ports/` vs `adapters/` split, same
dependency-injection style, just not funneled into `documents`.

The trade-off, stated plainly: bypassing `sources` also means losing its
free `last_run_at`/`last_status` health tracking. Proposed answer for v1:
skip that too. A forecast fetch fails loudly and immediately (no card
shows, same day), unlike a scraper silently drifting stale over weeks —
the exact failure mode `sources.last_run_at` exists to catch. Weather
doesn't need the same safety net to be safe.

---

## 2. Shape

```python
# ports/weather.py
class WeatherForecast(BaseModel):
    temp_high_f: float
    temp_low_f: float
    precipitation_probability: float  # 0-100
    condition: str                    # short description, e.g. "rain"

class WeatherProvider(Protocol):
    async def today(self) -> WeatherForecast: ...
```

```python
# adapters/weather/open_meteo.py
class OpenMeteoProvider:
    """Implements WeatherProvider. Owns its own short TTL cache inline —
    no new table, no Redis; a module-level (fetched_at, forecast) tuple is
    enough for single-household traffic."""
```

```python
# services/weather.py
_UMBRELLA_PRECIP_PCT = 40
_WARM_BELOW_F = 45
_HAT_SUNSCREEN_ABOVE_F = 80

def weather_advice(forecast: WeatherForecast) -> list[str]:
    """Each threshold is independent — a cold, rainy day returns both."""
```

`DashboardService` gains a `weather: WeatherProvider | None` constructor
dependency (mirrors `documents`/`clock` — both already ports). `today()`
calls it, catches any error internally, and sets `weather=None` on
failure — the rest of the dashboard renders regardless. Same fail-soft
posture the menu-unavailable placeholder already uses.

`streamlit_app.py` gets a `_weather_card()` alongside `_attention_card()`
— same rounded-card pattern, one new CSS class, no new rendering concept.

This also means `WeatherProvider` is a `Protocol`, so a `FakeWeatherProvider`
in `tests/fakes.py` (same pattern as `FakeDocumentRepository`/`FrozenClock`)
makes `DashboardService`'s weather-handling and `weather_advice()` fully
unit-testable without hitting a real API.

---

## 3. Data source options

| Option | Auth | Notes |
|---|---|---|
| **Open-Meteo** (recommended) | none | Free, no signup, one HTTP call returns current + daily forecast incl. precip probability. Nothing to put in `.env` as a secret. |
| **NWS** (`api.weather.gov`) | none | Also free/no-key, US government data. Two-step lookup (lat/lon → gridpoint URL → forecast) — a bit more ceremony, otherwise equally simple. |
| OpenWeatherMap | API key | Free tier is rate-limited and needs an account + a new `.env` secret. Buys severe-weather alerts and similar extras this feature doesn't need. |

Open-Meteo is the better fit for "keep it simple" — no key to provision,
no account. NWS is the alternative if a .gov data source is preferred.
OpenWeatherMap only makes sense if you want features beyond the
umbrella/hat scope.

## 4. Location

Single household → one fixed lat/lon, read from `.env` via the existing
`Settings` pattern (`weather_latitude` / `weather_longitude`, next to
`faria_calendar_url` etc.). No `locations` table — consistent with "single
household, no per-user model" already established for this project.

## 5. Request-path failure handling

The forecast fetch is a live third-party HTTP call sitting in the
`/dashboard` request path. Needs a short timeout (a few seconds, same
spirit as the Gmail/Faria adapters) and a try/except that degrades to "no
weather card" rather than a 500 for the whole dashboard.

---

## 6. Sequencing

1. `ports/weather.py` + `OpenMeteoProvider` + `weather_advice()` — pure,
   fully unit-testable with a fake, no UI yet.
2. Wire into `DashboardService`/API response as an optional `weather` field.
3. `_weather_card()` in Streamlit, styled like the reference image.

Small enough this is realistically one pass, not a multi-step rollout like
the dashboard redesign was.

---

## Decisions (2026-09-16)

1. **Data source: Open-Meteo.** No key/signup, one HTTP call for temp +
   precip probability.
2. **Architecture: bypass `sources`/`documents`.** §1's proposed
   `WeatherProvider` port + `OpenMeteoProvider` adapter, injected straight
   into `DashboardService`, confirmed.
3. **Location — still open.** Needs a home lat/lon (or an address/zip to
   geocode once) for `weather_latitude`/`weather_longitude` in `.env`.
   Blocks implementation — everything else in this doc is settled.
