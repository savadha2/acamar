# Implementation spec: P2.5 (weather / cockpit reminder / pill meals) + attention-card restyle

**Status: pseudocode spec for handoff, not implemented.** Written for
implementation handoff — architecture and algorithm precision, not literal
syntax, per this project's convention that a spec meant for review before
execution stays pseudocode until it's actually time to build. Every file/
function named below is real and verified by reading the actual code this
session, not assumed. Every external-library claim (Open-Meteo's hourly
API, Streamlit's `st.fragment`) was verified live against real docs/API
calls this session, not from training memory — sources cited inline.

Four independent pieces. Build and review them separately — don't treat
this as one PR. Each still needs a design nod before an actual code
session starts on it, same as every other feature in this project; this
doc is the proposal.

---

## Part A — Weather: diagnose "static," hourly polling, 8am–5pm worst-case advisory

(Backlog item 25.)

### A1. Root cause of "seems broken and static" — diagnosed, not a crash/error

Traced through the real chain: `OpenMeteoProvider._fetch_with()`
([adapters/weather/open_meteo.py:109-126](../src/acamar/adapters/weather/open_meteo.py))
requests Open-Meteo's `daily` aggregate — `temperature_2m_max`,
`temperature_2m_min`, one value each for the whole day. The dashboard chip
(`_weather_chip()`, [streamlit_app.py:241-269](../src/acamar/dashboard/streamlit_app.py))
displays `round(weather["temp_high_f"])` — **today's forecasted high for
the entire day**, not a live "right now" reading. That number is
inherently near-constant hour to hour (a day's forecast high doesn't
swing much once computed), and on top of that: (a) `OpenMeteoProvider`
caches it for `weather_cache_ttl_seconds` (1800s / 30min,
[config.py](../src/acamar/config.py)) via a process-wide singleton
(`get_weather_provider()`, `@lru_cache`), and (b) Streamlit only re-fetches
on page load/interaction — nothing polls today. Three layers compounding
into "looks frozen all day": a slow-changing-by-nature number, a 30-min
cache, and zero proactive refresh. **Not a bug in the caching logic
itself** — the TTL/singleton mechanics work as designed — it's a mismatch
between what's shown (whole-day forecast high) and what a user expects
(something that visibly updates through the day).

### A2. Fix: switch to Open-Meteo's `hourly` endpoint, keep the daily call only if still needed elsewhere

Verified live this session (real API call against
`api.open-meteo.com/v1/forecast?...&hourly=temperature_2m,
precipitation_probability,weather_code&forecast_days=1&timezone=auto`):

- Returns a clean parallel-array shape: `hourly.time` = 24 ISO8601
  local-time strings (`"2026-09-28T00:00"`, ..., `"...T23:00"` — **local**,
  not UTC, confirmed via `utc_offset_seconds`/`timezone` in the same
  response), and `hourly.temperature_2m` / `hourly.precipitation_
  probability` / `hourly.weather_code` are same-length arrays aligned by
  index to `time`.
- `timezone=auto` (the param this codebase already sends) correctly
  localizes — verified `hourly.time[0]` was local midnight for the
  configured lat/lon, not UTC midnight. So slicing "8am–5pm" is a plain
  string match on the `T08:00`..`T17:00` portion of each `time` entry, no
  timezone math needed.
- Rate limits (free tier, verified from Open-Meteo's pricing page): 600
  calls/min, 5,000/hour, 10,000/day — call-count based, `forecast_days=1`
  is trivially within any of these. One thing **not** independently
  confirmed: whether Open-Meteo's fractional-call weighting (documented
  for multi-week historical pulls) applies at all to a 1-day forecast call
  — near-certainly irrelevant at this scale, flagged so it isn't assumed
  with false confidence.

**PSEUDOCODE — `adapters/weather/open_meteo.py`:**

```
function _fetch_with(client):
    params = {
        latitude, longitude,
        hourly: "temperature_2m,precipitation_probability,weather_code",
        temperature_unit: "fahrenheit",
        timezone: "auto",
        forecast_days: 1,
    }
    response = GET forecast_url with params
    return _parse_hourly(response.json())

function _parse_hourly(payload):
    times = payload.hourly.time            # 24 local ISO strings
    temps = payload.hourly.temperature_2m
    precip = payload.hourly.precipitation_probability
    codes = payload.hourly.weather_code

    # slice to the school-day window — indices where the hour-of-day
    # portion of `times[i]` falls in [08:00, 17:00]
    window_indices = [i for i, t in enumerate(times)
                       if "08:00" <= t[11:16] <= "17:00"]
    if window_indices is empty:              # defensive: malformed/short response
        raise WeatherError("no hourly data in the 8am-5pm window")

    window_temps = [temps[i] for i in window_indices]
    window_precip = [precip[i] for i in window_indices]
    window_codes = [codes[i] for i in window_indices]

    return WeatherForecast(
        temp_high_f = max(window_temps),          # worst case for heat/sun advisory
        temp_low_f = min(window_temps),            # worst case for cold/jacket advisory
        precipitation_probability = max(window_precip),  # worst case for umbrella advisory
        condition = map_code_to_condition(
            window_codes[argmax(window_precip)]    # condition string tied to the
        ),                                          # highest-precip hour, most
    )                                                # representative of "why the umbrella"
```

**Open question (flag for review, don't assume):** `WeatherForecast`'s
field *names* (`temp_high_f`/`temp_low_f`) stay unchanged so
`weather_advice()`'s three thresholds
([services/weather.py:20-37](../src/acamar/services/weather.py)) need
**zero changes** — they now just receive 8am–5pm-window values instead of
whole-day values. This is the minimal-blast-radius option. Alternative
(bigger change, not recommended as the default): change `WeatherForecast`
to carry the raw hourly arrays and move windowing/aggregation into
`services/weather.py` instead — more flexible if a future feature needs
per-hour detail, but touches more call sites for no benefit this ask
needs. **Recommend the minimal option above** unless there's a reason to
want raw hourly data elsewhere.

`_CONDITIONS` code-lookup table, `WeatherError` handling, and the
`_TIMEOUT`/client-injection structure all stay exactly as-is — only the
request params and the parsing function change.

### A3. Hourly polling — `st.fragment(run_every="1h")`, verified this session

Verified against Streamlit's own docs (not assumed): `st.fragment`'s
`run_every` parameter reruns the decorated fragment on a schedule
("1h" accepted directly, pandas-Timedelta-style string) **independently of
user interaction**, without re-running the rest of the page — shipped in
Streamlit 1.33 (April 2024), stabilized as `st.fragment` in 1.37 (July
2024) — both far predate this project's pinned `streamlit>=1.63.0`, so
it's available, no new dependency. One honest caveat found in a
community report (not an official doc statement): a **backgrounded/
unfocused browser tab** can throttle the actual interval due to browser
power-saving, not a Streamlit bug — acceptable here (worst case, a
household dashboard left unfocused refreshes a bit less than hourly, not
a functional break). Composing `run_every` with the existing 30-min TTL
cache on `OpenMeteoProvider` is *not* explicitly blessed by a doc sentence
(the one explicit caching caveat is about stacking `@st.cache_data` and
`@st.fragment` on the *same* function, a different situation) but is
mechanically sound: the fragment decides *when to check*, the TTL cache
decides *whether that check hits the network*.

**PSEUDOCODE — `dashboard/streamlit_app.py`:**

```
@st.fragment(run_every="1h")
function _weather_fragment(weather_data):
    if weather_data exists:
        render st.markdown(_weather_chip(weather_data), unsafe_allow_html=True)
    # nothing else goes in this fragment — keep it scoped to just the
    # weather chip, not the whole header, so only this piece re-renders
    # hourly, not the logo/date/rest of the page

# in _render(), where the header HTML block is currently built as one
# combined st.markdown(...) call: split the weather chip OUT of that
# single combined block and call _weather_fragment(data.get("weather"))
# separately, right after the combined header markdown call. This is a
# real structural change — today's header is deliberately ONE st.markdown
# call so logo/date/weather-chip nest under one centered div (see the
# existing comment above `_render`'s header-building code). Splitting the
# weather chip into its own fragment breaks that "one block" layout
# constraint, so the CSS/layout needs re-checking once built (e.g. does
# .acamar-header's flex/center styling still visually center a
# fragment-rendered sibling element the same way) — flagged as a real
# risk to verify visually, not assumed to just work.
```

**Recommendation on the TTL vs. poll-interval mismatch**: leave
`weather_cache_ttl_seconds` at 1800 (30min) — shorter than the 1h poll
interval means most hourly fragment reruns still do a fresh fetch (cache
already expired), which is fine and arguably desirable (fresher data).
Not a required change, just noting it's not a conflict.

### A4. Open questions for review before building

1. Should the *displayed chip number* also switch to the 8am–5pm max (for
   consistency with what now drives the advisory), or stay as a whole-day
   number while only the advisory logic uses the window? Recommend: switch
   the chip too, so the number shown and the number driving "bring an
   umbrella" are never silently different things.
2. Poll interval: `"1h"` as asked, or shorter (e.g. `"30m"`, matching the
   existing cache TTL exactly)? Recommend `"1h"` as asked — the underlying
   forecast doesn't change fast enough to need finer.
3. Confirm visually once built that splitting the weather chip into its
   own fragment doesn't break the existing centered-header layout (real,
   flagged risk above, not verified by this spec).

---

## Part B — Cockpit: "Upload lunch menu" reminder

(Backlog item 26.)

### B1. Data source — already exists, no backend change needed

`GET /admin/menu-coverage` ([api/routers/admin.py:117-148](../src/acamar/api/routers/admin.py))
already answers "is `(source_id, meal, year, month)` covered" — reuse
directly, don't build a new endpoint.

### B2. Design — a 4th annunciator light, matching the existing Glass Deck visual language

`cockpit_app.py`'s header already renders exactly 3 annunciator lights
(Master Warning / Master Caution / API Link) via `_light_html()`
([dashboard/cockpit_app.py:542-548](../src/acamar/dashboard/cockpit_app.py),
rendered in `_render_header()`). Adding a reminder here reuses an existing
pattern instead of inventing a new banner element.

**PSEUDOCODE:**

```
function _menu_reminder_lit(token) -> bool | None:
    # None = "can't tell" (no source_id configured, or the coverage call
    # failed) — must not light up amber on an error, that's misleading
    if SETTINGS.manual_menu_source_id is empty:
        return None
    next_month, next_year = the calendar month after today
    try:
        coverage = GET /admin/menu-coverage(
            source_id=SETTINGS.manual_menu_source_id,
            meal="lunch", year=next_year, month=next_month,
        )
    except request failure:
        return None
    return len(coverage.covered_days) == 0   # fully uncovered next month

# in _render_header(), alongside the existing 3 _light_html(...) calls:
reminder_state = _menu_reminder_lit(token)
annunciators += _light_html(
    "Menu Reminder",
    "warn" if reminder_state is True else None,
)
```

### B3. Open questions for review

1. **Trigger threshold**: "next month has zero covered days" (simplest,
   proposed above) vs. something more nuanced (e.g. "fewer than N school
   days covered, N days before month start")? Recommend starting with the
   simple zero-coverage check — cheap to compute, matches the plain
   English of "remind me to upload the menu."
2. Should it check **breakfast** coverage too, or lunch only (as the
   backlog item's own title says)? The existing `_upload_menu`/coverage
   flow treats meals independently — recommend lunch-only for v1, matching
   the item as scoped, and revisit if breakfast turns out to lag too.
3. Does a lit "Menu Reminder" annunciator need its own dedicated color
   (today's palette: `--ok` green, `--warn` amber, `--danger` red,
   `--info` cyan) or reuse `--warn` (proposed above, matching "needs
   attention but not broken")? Recommend reusing `--warn` — consistent
   with Master Caution's existing meaning.

---

## Part C — Main dashboard: breakfast/lunch as pills next to the temperature chip

(Backlog item 27.)

### C1. Current state

Today, menu items render as full-width `_row_html()` cards in their own
"Menu" section ([streamlit_app.py:379-389](../src/acamar/dashboard/streamlit_app.py)),
entirely separate from the header's weather chip.

### C2. Design — compact pills in the header, full section stays for detail

**Open design decision, resolved here with a recommendation, not silently
assumed**: keep the full "Menu" section exactly as-is (it's the only place
multi-day look-ahead and full item detail live), and ADD compact
header pills **only for today's own menu** (`data.get("menu_fallback_date")
is None` — i.e. don't show a header pill for a *fallback* future date,
that belongs in the full section only, to avoid header clutter implying
"today" when it's actually a future date).

**PSEUDOCODE — extends the existing header-building block in `_render()`:**

```
function _meal_pill(item) -> html string:
    # matches _weather_chip's pill shape/class conventions, not a new
    # visual language — same border-radius/padding/background treatment
    icon = _item_icon(item)   # already exists, handles the menu icon case
    label = item.title split on " — " first part   # "Breakfast Menu" / "Lunch Menu"
                                                      # -> shorten to "Breakfast"/"Lunch"
    tooltip = item.detail     # full item text, same `title=` hover pattern
                               # _weather_chip already uses
    return html for one small pill: icon + short label, title=tooltip

# in _render(), only when data.get("menu_fallback_date") is None:
todays_meals = [item for item in data["menu"]]   # already filtered to
                                                   # today's own menu by
                                                   # DashboardService when
                                                   # fallback_date is None
if todays_meals is non-empty:
    append one _meal_pill(item) per item into the same header HTML block,
    positioned next to / after the weather chip, inside the existing
    single combined st.markdown(...) call (NOT a separate fragment/
    markdown call — unlike the weather chip in Part A, these don't need
    independent polling, so keep them in the header's original single-
    block structure)
```

### C3. Open questions for review

1. Pill label: meal name only ("Lunch"), or name + first item ("Lunch:
   Chicken Sandwich")? Recommend name-only + hover tooltip for full detail
   (matches the weather chip's own icon+temp-only, hover-for-detail
   pattern already established) — keeps the header uncluttered.
2. If there are 2 meals today (breakfast + lunch), two separate pills, or
   one combined pill? Recommend two separate pills — each meal is a
   distinct real-world thing with its own detail text; combining loses
   that.
3. Confirm the full "Menu" section stays (recommended above) rather than
   being replaced entirely by the pills — the full section is the only
   place a multi-day look-ahead or a fallback-date menu is visible at all.

---

## Part D — "Needs your attention" cards: left-border + tint, not solid yellow fill

(New — not yet a backlog item; number to be assigned once this doc is
reviewed. Prompted by a screenshot showing the desired look: a colored
left border + a very light tinted background + a small-caps urgency label
above the title — red "TODAY" / amber "SOON" — replacing the current flat
yellow card.)

### D1. Current state

`.acamar-attention` ([streamlit_app.py:117-123](../src/acamar/dashboard/streamlit_app.py))
is a flat, single-color card: `background: #f7e0ac` (solid tan/yellow, no
border), same for every urgency level — a same-day item and a 3-days-out
item look visually identical today. `_attention_card()`
([streamlit_app.py:294-306](../src/acamar/dashboard/streamlit_app.py))
builds it.

### D2. Design — reuse the existing left-border-accent pattern already established by `.acamar-row`

`.acamar-row` ([streamlit_app.py:128-132](../src/acamar/dashboard/streamlit_app.py))
already does exactly this shape for the "Coming up" section — a
`border-left: 4px solid var(--acamar-accent, ...)` driven by an inline
CSS custom property set per-item. Reuse that same mechanism here instead
of inventing a new one, driven by urgency instead of category.

**Two-tier labeling, not a continuous gradient** — the screenshot shows
exactly two discrete labels ("TODAY" / "SOON"), not a rainbow gradient
like `_urgency_color()`'s existing red→amber→green (that gradient is used
elsewhere, for the small "in N days" text — keep that as-is, this is a
separate, coarser two-tier system specifically for the attention-card
label/border):

```
function _attention_tier(days) -> ("TODAY" | "SOON", accent_color):
    if days == 0:
        return ("TODAY", RED)      # same red family as _urgency_color's RED endpoint
    else:                           # 1..3, since this function only ever
        return ("SOON", AMBER)      # receives already-filtered "urgent" items
                                     # (_URGENT_WINDOW_DAYS = 3, unchanged)

function _attention_card(item, as_of) -> html string:
    days = _days_until(item.date, as_of)         # existing function, unchanged
    label, accent = _attention_tier(days)
    title = escape(item.title)
    detail = escape(item.detail)
    when = escape(_when_text(days))               # existing function, unchanged

    return html:
        <div class="acamar-attention" style="--acamar-attention-accent: {accent}">
          <div class="acamar-attention-label" style="color:{accent}">{label}</div>
          <div class="acamar-attention-title">{title}</div>
          <div class="acamar-attention-sub">{detail}</div>
          <div class="acamar-attention-when">{when}</div>
        </div>
```

**PSEUDOCODE — CSS** (replaces `.acamar-attention`'s current flat-fill rule):

```
.acamar-attention {
    background: {accent color mixed into white at low opacity, ~6-8%}
       /* e.g. color-mix(in srgb, var(--acamar-attention-accent) 7%, white) —
          same technique cockpit_app.py's CSS already uses elsewhere
          (color-mix(...) appears throughout cockpit_app.py's _CSS) —
          reuse that established pattern rather than a new one */
    border-left: 4px solid var(--acamar-attention-accent);
    border-radius: 12px;      /* keep existing radius */
    padding: 14px 20px;       /* keep existing padding */
}
.acamar-attention-label {
    text-transform: uppercase; letter-spacing: 0.06em;
    font-size: 0.75em; font-weight: 700;
    /* color set inline per-card via style=, matches the accent */
}
/* .acamar-attention-title / -sub / -when: keep existing rules, but drop
   their current hardcoded brownish colors (#7a5510 / #8a6a28) tied to the
   old yellow-fill design — switch to neutral text colors (matching
   .acamar-row-title/-sub's existing neutral grays) since the accent now
   lives in the border/label, not the whole card's text */
```

Matches exactly what the screenshot shows: colored left border, light
tinted background scaled to the same hue, small-caps colored label above
the title, day-count still shown below (via the existing `_when_text()` +
`_urgency_color()` for that one line, unchanged — the *fine-grained*
red→amber→green gradient stays for "in N days," only the *card's* accent
becomes the coarser two-tier TODAY/SOON system).

### D3. Open questions for review

1. Exact tint opacity/lightness — pick by eye once built and viewed in
   a real browser (this project has no way to screenshot/visually verify
   inside this environment — same limitation already logged for the
   cockpit's own CSS: show the rendered result before considering this
   done, don't guess the values).
2. Should `.acamar-row` (the "Coming up" section, currently
   category-colored) also gain the same light-tint-background treatment
   for visual consistency with the restyled attention cards, or stay as
   today's plain `rgba(127,127,127,0.05)` neutral gray? Not asked for —
   flagged as a natural follow-on question, not assumed in scope here.
