"""ACAMAR dashboard (Streamlit).

Run: ``uv run streamlit run src/acamar/dashboard/streamlit_app.py``
"""

from __future__ import annotations

import base64
import re
from datetime import date
from html import escape
from pathlib import Path
from typing import Any

import requests
import streamlit as st

from acamar.config import get_settings

_SETTINGS = get_settings()
# Full lockup (icon + "acamar" wordmark) — header display.
_LOGO_PATH = Path(__file__).parent / "assets" / "logo.png"
# Icon only, no wordmark — the favicon. A wordmark this wide turns to mush at
# tab-icon size, so the favicon gets a tight square crop of just the mark.
_LOGO_ICON_PATH = Path(__file__).parent / "assets" / "logo_icon.png"
# Embedded as a data URI (2026-09-24) rather than `st.image` — the header is
# now one centered HTML block (logo/date/weather stacked, `text-align:
# center`); Streamlit renders each `st.*` call as its own separate DOM node,
# so an `st.image` call can't be nested inside a div from a different
# `st.markdown` call the way plain HTML would. Read once at import time, not
# per rerun (Streamlit reruns this whole script on every interaction).
_LOGO_DATA_URI = "data:image/png;base64," + base64.b64encode(_LOGO_PATH.read_bytes()).decode(
    "ascii"
)

# Items due within this many days land in "Needs your attention".
_URGENT_WINDOW_DAYS = 3

# Icon by organization (domain.enums.Organization) — the "type" of a coming-up
# row. Category HOLIDAY overrides this regardless of organization.
_TYPE_ICON = {
    "school_calendar": "🎓",
    "district": "👥",
    "feef": "🎉",
    "classroom": "✏️",
    "faria_admin": "🏫",
    "city_recreation": "🌳",
    "other": "📝",
}
_HOLIDAY_ICON = "🚫"
_MENU_ICON = "🍽️"
_DEFAULT_ICON = "📝"

# Weather icon by keyword found in the provider's `condition` string (e.g.
# "rain likely") — checked in order, first match wins. Presentation-only;
# the underlying WMO-code table lives in adapters/weather/open_meteo.py.
_WEATHER_ICON_RULES = [
    ("thunder", "⛈️"),
    ("snow", "❄️"),
    ("rain", "🌧️"),
    ("drizzle", "🌦️"),
    ("fog", "🌫️"),
    ("clear", "☀️"),
    ("cloud", "☁️"),
]
_DEFAULT_WEATHER_ICON = "🌡️"

# Advice-code -> minimal label, shown as plain text next to the temperature
# (2026-09-29 unified day-pill redesign — no per-advice icon badge anymore,
# see the day-pill CSS/functions below). Codes come from services.weather
# .weather_advice — a fixed small set, so an unrecognized code (shouldn't
# happen) falls back to a titlecased version of the raw code rather than
# dropping it.
_ADVICE_LABELS = {
    "umbrella": "Umbrella",
    "warm": "Warm",
    "sun": "Hat + sunscreen",  # matches weather_answer.py's phrasing exactly
}

# Matches a lunch item-list fragment like "Option 1 - Meat" (real vendor
# PDF shape, see sources.manual_menu.parser) — dropped from a menu's food
# list (`_food_items`) so it reads as real food ("Chicken Sandwich"), not
# category scaffolding ("Option 1 - Meat").
#
# 2026-09-29: this used to be a bare triple-quoted string sitting right
# after the assignment (an attribute-docstring convention some tools
# recognize) — harmless in an ordinary module, but streamlit_app.py *is*
# the script Streamlit executes directly, and Streamlit's "magic" mode
# auto-renders any bare top-level expression via st.write(). That made
# this literally render as page content, above everything `main()` draws
# (module-level code runs before `main()` is ever called). Converted to a
# plain comment — never executed, never rendered.
_OPTION_LABEL_PATTERN = re.compile(r"^Option \d+ -")

# Left-border color by category — amber is reserved for urgency ("needs your
# attention" cards), so these three are deliberately elsewhere on the wheel.
_CATEGORY_COLOR = {
    "event": "#3b82f6",
    "reminder": "#8b5cf6",
    "holiday": "#ec4899",
    "menu": "#14b8a6",
}
_DEFAULT_COLOR = "#9ca3af"

# Organization-level override, checked before _CATEGORY_COLOR (2026-10-02,
# by request) — every category color above is keyed by `category` alone, so
# a cupertino_events EVENT would otherwise be indistinguishable (same blue)
# from a Faria EVENT. `#22c55e` matches `_URGENCY_GREEN`'s own RGB
# (34, 197, 94) below, reused rather than a new unrelated green — can't
# reference that constant directly here, it's defined later in the file.
_ORGANIZATION_COLOR = {
    "city_recreation": "#22c55e",
}

# "Show: All / School / Parks & City" filter pills, above the Needs-your-
# attention/Coming-up sections (2026-10-02, by request) — a city event is
# the only thing tagged `city_recreation`, so that one organization value is
# enough to bucket every dated item into "School" or "Parks & City" without
# needing a separate domain field on DashboardItem. Dot emoji stand in for
# the colored dots in the supplied reference image — simpler and more
# robust than reskinning st.pills' internals to draw real colored dots,
# which isn't exposed as a per-option style hook.
_SHOW_ALL = "All"
_SHOW_SCHOOL = "🟠 School"
_SHOW_CITY = "🟢 Parks & City"
_SHOW_OPTIONS = (_SHOW_ALL, _SHOW_SCHOOL, _SHOW_CITY)
_CITY_ORGANIZATION = "city_recreation"

# Urgency gradient for the "days remaining" text: red today, green at/beyond
# this many days out, red->amber->green in between (a red/green direct blend
# reads as muddy brown, so this goes through an amber midpoint).
_URGENCY_GRADIENT_DAYS = 14
_URGENCY_RED = (220, 38, 38)
_URGENCY_AMBER = (234, 179, 8)
_URGENCY_GREEN = (34, 197, 94)
_URGENCY_NEUTRAL = "#aaaaaa"

_STYLE = """
<style>
.acamar-header {
    /* Logo moved from centered to top-left (2026-10-01, by request). */
    display: flex; flex-direction: column; align-items: flex-start;
    text-align: left; margin-bottom: 6px;
}
.acamar-logo { width: 156px; height: auto; display: block; } /* 130px * 1.2, by request */
.acamar-wordmark {
    /* The "acamar" text wordmark, as actual HTML text (2026-10-01) — the
       logo `<img>` above is just the zigzag-of-dots + star mark, not the
       combined wordmark lockup CLAUDE.md describes; this is the text half,
       added separately so its color can be styled per-letter, which a
       flattened PNG can't be. White against this app's pinned-dark page
       background; the middle of the word's three "a"s — `ac`**`a`**`mar`
       — picked out in the brand orange already used for the weather
       temp/ask-button accent (`#e8712f`), not a new color. */
    font-size: 1.3em; font-weight: 700; letter-spacing: 0.02em;
    color: #ffffff;
    /* Moved closer to the logo + opacity dropped 25% (2026-10-01, by
       request): `margin-top` pulled in from its original 4px to -10px
       (negative, to close the gap meaningfully rather than trim an
       already-small number), `align-self`/`margin-left` made explicit rather than
       relying only on the parent `.acamar-header`'s own `align-items:
       flex-start`, so this element's own left edge is pinned regardless
       of anything else in the header. `opacity: 0.75` = 25% more
       transparent than the fully-opaque text before (applied here, not
       as separate `rgba()` colors, so it fades the orange "a" by the same
       amount as the rest of the word). Caveat: true pixel alignment
       against the icon also depends on how much transparent padding the
       logo PNG itself carries around the visible dots/star, which isn't
       inspectable from code — if it still reads as offset once rendered,
       that's a crop-the-asset fix, not a CSS one. */
    margin-top: -10px; margin-left: 0; align-self: flex-start;
    opacity: 0.75;
}
.acamar-wordmark-a { color: #e8712f; }
/* Pulls the header closer to the actual top of the viewport (2026-10-01,
   "even further top left" follow-up) — `align-items: flex-start` above only
   pins the logo to the left edge of Streamlit's own centered content
   column; the real gap above it is Streamlit's own `.block-container` top
   padding (~6rem by default), not anything in this file's own CSS. Reaches
   into that native container via a broad selector, same idiom this file
   already uses for `.stTextInput input`/`.stButton button` — flagged, like
   those, as resting on Streamlit's current DOM rather than a public API,
   so a future Streamlit upgrade is the thing most likely to silently
   un-do this. Does NOT touch `layout="centered"` itself (`main()`'s
   `st.set_page_config`) — switching to `layout="wide"` would pull the
   logo to the page's literal left edge too, but also re-widens every other
   element on the page, a far bigger change than was asked for here. */
.block-container { padding-top: 1.2rem !important; }
.acamar-day-card-row {
    /* No longer centers with its own max-width (2026-09-29, by request —
       the card used to be capped at 640px and centered, which didn't
       line up with the ask row below it, which spans this app's full
       content-column width via st.columns). Now just a plain block so
       the card naturally spans that same width and both edges align with
       the ask box. The card itself is built and rendered entirely inside
       one `st.fragment(run_every="1h")` call (see `_day_card_fragment`) —
       2026-09-29, 6th revision of this component; see backlog item 27's
       full history for the five that came before.
       `margin-bottom` bumped 10px -> 22px (2026-09-29, by request, now
       that the pill no longer spans the full row — see `.acamar-day-card`
       — the tighter gap read as cramped against the ask box below). 22px
       matches `.acamar-section-label`'s own margin-top used everywhere
       else on this page, and `.acamar-meal-section:last-child`'s
       margin-bottom below was bumped to the same value, so both gaps
       (pill-to-ask-box, menu-to-"Needs your attention") read as one
       consistent rhythm rather than two arbitrary numbers. */
    margin-top: 10px; margin-bottom: 22px;
}
.acamar-day-card {
    /* A white card floating on this app's pinned-dark page background
       (CLAUDE.md's own recorded decision, 2026-09-24 — not revisited
       here) — deliberate, not an oversight: `.acamar-attention`/
       `.acamar-row` already use the same "light card surface on a dark
       page" pattern elsewhere in this file, this is that same pattern at
       full-card scale, built against a supplied complete reference image.
       Sized down twice now (2026-09-29, by request each time) — height
       specifically targeted to roughly match the ask row's own height
       below it (`.stTextInput input`/`.stButton button`, ~51-56px with
       their own padding/min-height), not just "smaller in general".
       Changed to an actual pill (2026-09-29, 7th revision — the full-width
       bar from the previous revision didn't read right in review): `width:
       100%` dropped so the card sizes to its own content instead of
       stretching to the row's width, and `border-radius` bumped to 999px
       to match the pill radius the ask row below already uses
       (`.stTextInput input`/`.stButton button`). `.acamar-day-card-row`
       stays a plain block (no centering), so the pill still left-aligns
       with the ask box instead of drifting to the middle. */
    width: fit-content; box-sizing: border-box;
    background: #ffffff; border-radius: 999px; padding: 10px 22px;
    box-shadow: 0 8px 28px rgba(0, 0, 0, 0.16); color: #1a1a1a;
}
.acamar-day-card .acamar-section-label {
    /* Reuses the file's existing label style (uppercase/letter-spaced/
       gray) for "TODAY"/"BREAKFAST"/"LUNCH" — but that style's default
       `margin: 22px 0 10px` assumes standalone vertical stacking, wrong
       inside this card's own layout, so it's reset here rather than
       forked into a near-duplicate class. */
    margin: 0 0 1px; font-size: 0.62em;
}
.acamar-day-card-top {
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
}
.acamar-day-card-date {
    font-size: 1.05em; font-weight: 700; line-height: 1.1; white-space: nowrap;
}
.acamar-day-card-top-right { display: flex; align-items: center; gap: 16px; }
.acamar-day-card-vdivider { width: 1px; height: 24px; background: #e5e5e5; }
.acamar-weather-box {
    /* Sized down again to match the day card's own further reduction
       (2026-09-29, by request). */
    background: #fdf1e4; border-radius: 10px; padding: 3px 14px; text-align: center;
}
.acamar-weather-box-temp {
    font-size: 0.92em; font-weight: 700; color: #e8712f; white-space: nowrap;
}
.acamar-weather-box-advice {
    font-size: 0.62em; color: #8a7a6a; margin-top: 0; white-space: nowrap; line-height: 1.2;
}
.acamar-meal-section { margin-bottom: 18px; }
.acamar-meal-section:last-child {
    /* Was 0 — relied on `.acamar-section-label`'s own 22px margin-top
       (the "Needs your attention" label right after) to carry all the
       spacing. Bumped to 22px too (2026-09-29, by request — that read as
       too tight) so the gap is explicit here rather than borrowed from
       the next section's label, and matches the pill-to-ask-box gap
       above for the same reason. */
    margin-bottom: 22px;
}
.acamar-food-chips { display: flex; flex-wrap: wrap; gap: 10px; }
.acamar-food-chip {
    /* Fill lightened #eef0ef -> #f5f6f5 (2026-09-29, by request). */
    background: #f5f6f5; border-radius: 14px; padding: 10px 18px;
    font-size: 0.95em; font-weight: 600; color: #1a1a1a; white-space: nowrap;
}
.acamar-attention {
    /* 2026-09-29: left-border + light tint (screenshot-prompted), replacing
       the old flat #f7e0ac fill that looked identical regardless of
       urgency — color-mix() matches the technique cockpit_app.py's own
       CSS already uses elsewhere for the same "accent tinted into a light
       background" effect, reused here rather than a new one. */
    background: color-mix(in srgb, var(--acamar-attention-accent, #9ca3af) 7%, white);
    border-left: 4px solid var(--acamar-attention-accent, #9ca3af);
    border-radius: 12px; padding: 14px 20px; margin-bottom: 10px;
}
.acamar-attention-label {
    text-transform: uppercase; letter-spacing: 0.06em; font-size: 0.75em;
    font-weight: 700; margin-bottom: 3px;
}
.acamar-attention-title { font-weight: 600; color: #333; font-size: 1.02em; }
.acamar-attention-sub { color: #777; font-size: 0.88em; margin-top: 3px; }
.acamar-attention-when { color: #999; font-size: 0.82em; margin-top: 3px; font-style: italic; }
.acamar-section-label {
    text-transform: uppercase; letter-spacing: 0.06em; font-size: 0.78em;
    color: #8a8a8a; font-weight: 600; margin: 22px 0 10px;
}
.acamar-row {
    display: flex; gap: 16px; padding: 10px 14px; margin-bottom: 8px;
    border-left: 4px solid var(--acamar-accent, #9ca3af); border-radius: 6px;
    background: rgba(127, 127, 127, 0.05);
}
.acamar-row-date { width: 70px; flex-shrink: 0; color: #999; font-size: 0.84em; padding-top: 1px; }
.acamar-row-when { display: block; font-size: 0.82em; font-style: italic; color: #aaa; }
.acamar-row-title { font-weight: 600; }
.acamar-row-sub { color: #999; font-size: 0.88em; margin-top: 2px; }
.acamar-item-link {
    /* Hyperlink to the source's own page (2026-10-02, cupertino_events) —
       inherits the card's own dark text color rather than default link
       blue (these cards are light-on-dark-page, see CLAUDE.md's 2026-09-24
       decision), with a dotted underline as the only clickability cue so
       it still reads as plain text at a glance. */
    color: inherit; text-decoration: none; border-bottom: 1px dotted currentColor;
}
.acamar-item-link:hover { border-bottom-style: solid; }
.acamar-empty { color: #999; font-size: 0.9em; padding: 4px 0 12px; }
.acamar-answer {
    background: rgba(127, 127, 127, 0.05); border-radius: 10px; padding: 14px 18px;
    margin: 10px 0 18px; border-left: 4px solid #3b82f6;
}
.acamar-answer-text { font-size: 0.95em; }
.acamar-answer-sources { color: #999; font-size: 0.82em; margin-top: 8px; }
.acamar-answer-low-confidence {
    color: #7a5510; background: #f7e0ac; border-radius: 6px; padding: 4px 10px;
    display: inline-block; font-size: 0.78em; margin-top: 8px;
}
/* Ask box reskin (2026-09-29, matches the supplied reference image) —
   native Streamlit widgets (st.text_input/st.button), not custom HTML, so
   this can only go as far as broad element-selector CSS reaches, same
   idiom cockpit_app.py's own native-widget reskinning already uses
   (.stTextInput input/.stButton button there too).
   Rebuilt 2026-10-01: the previous two `st.columns`-based attempts both
   failed on a real screenshot (first overflowed the pill, second rendered
   the button on its own line below the input) — root cause confirmed to
   be Streamlit's own responsive column-stacking kicking in on a narrow
   `[9, 1]` column, not a CSS value being merely slightly off. Removed
   `st.columns` from the ask row entirely (see `_render`) in favor of
   `st.container(key="ask_row")` + `st.button(..., key="ask_button")` —
   `key=` is Streamlit's own documented mechanism for a stable CSS hook
   (`.st-key-<key>`), unlike the internal `data-testid` names the previous
   attempts guessed at, and a single container has no column-stacking
   behavior to fight in the first place. */
.st-key-ask_row { position: relative; }
.st-key-ask_row .stTextInput input {
    border-radius: 999px !important; padding: 14px 50px 14px 22px !important;
    background: #ffffff !important; color: #1a1a1a !important;
    border: none !important; font-size: 1.0em !important;
}
.st-key-ask_button {
    /* Self-centers against whatever the input's real rendered height turns
       out to be (`top: 50%` + `translateY`), rather than a fixed pixel
       guess — containing block is `.st-key-ask_row` above, regardless of
       how many of Streamlit's own static wrapper divs sit in between. */
    position: absolute !important; right: 5px; top: 50%;
    transform: translateY(-50%); z-index: 2;
}
.st-key-ask_button button {
    border-radius: 999px !important; background: #e8712f !important;
    color: #ffffff !important; border: none !important;
    width: 36px !important; height: 36px !important;
    min-width: 0 !important; min-height: 0 !important; padding: 0 !important;
    line-height: 1 !important; font-size: 0.95em !important;
    display: flex !important; align-items: center !important; justify-content: center !important;
}
.st-key-ask_button button:hover { background: #d6631f !important; color: #ffffff !important; }
</style>
"""


def _fetch_dashboard(token: str, days: int) -> dict[str, Any]:
    response = requests.get(
        f"{_SETTINGS.api_base_url}/dashboard",
        headers={"Authorization": f"Bearer {token}"},
        params={"days": days},
        timeout=10,
    )
    response.raise_for_status()
    return dict(response.json())


def _query(token: str, question: str) -> dict[str, Any]:
    response = requests.post(
        f"{_SETTINGS.api_base_url}/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"question": question},
        timeout=60,  # the reasoning pipeline can take 20-30s, unlike /dashboard
    )
    response.raise_for_status()
    return dict(response.json())


def _dismiss(token: str, item_id: str) -> None:
    """Mark an item done via POST /dashboard/items/{id}/dismiss.

    Not currently wired to any UI control — the previous checkmark-button
    treatment looked bad and was pulled. The endpoint is real and tested;
    this is ready for whatever nicer interaction replaces it.
    """
    requests.post(
        f"{_SETTINGS.api_base_url}/dashboard/items/{item_id}/dismiss",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    ).raise_for_status()


def _item_icon(item: dict[str, Any]) -> str:
    if item.get("category") == "holiday":
        return _HOLIDAY_ICON
    if item.get("category") == "menu":
        return _MENU_ICON
    org = item.get("organization")
    return _TYPE_ICON.get(org, _DEFAULT_ICON) if org else _DEFAULT_ICON


def _days_until(item_date: str | None, as_of: date) -> int | None:
    if not item_date:
        return None
    return (date.fromisoformat(item_date) - as_of).days


def _when_text(days: int | None) -> str:
    if days is None:
        return ""
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def _lerp_rgb(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2, strict=True))  # type: ignore[return-value]


def _urgency_color(days: int | None) -> str:
    """Red today, green at/beyond `_URGENCY_GRADIENT_DAYS` out, gradient between."""
    if days is None:
        return _URGENCY_NEUTRAL
    t = max(0.0, min(days, _URGENCY_GRADIENT_DAYS)) / _URGENCY_GRADIENT_DAYS
    rgb = (
        _lerp_rgb(_URGENCY_RED, _URGENCY_AMBER, t / 0.5)
        if t <= 0.5
        else _lerp_rgb(_URGENCY_AMBER, _URGENCY_GREEN, (t - 0.5) / 0.5)
    )
    return _hex(rgb)


def _hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _attention_tier(days: int | None) -> tuple[str, str]:
    """(label, accent color) for an attention card.

    A coarse two-tier system (TODAY/SOON) — 2026-09-29, screenshot-
    prompted — distinct from `_urgency_color`'s continuous red-amber-green
    gradient, which stays unchanged for the "in N days" text below the
    title. This one drives the card's border/background-tint/label
    instead. Only ever called for already-urgent items (pre-filtered to
    `_URGENT_WINDOW_DAYS`), so `days` is 0-3 in practice, never `None` —
    the `None` branch is defensive only, matching `_urgency_color`'s own
    defensive `None` handling.
    """
    if days is None:
        return "SOON", _URGENCY_NEUTRAL
    if days == 0:
        return "TODAY", _hex(_URGENCY_RED)
    return "SOON", _hex(_URGENCY_AMBER)


def _short_date(item_date: str) -> str:
    return date.fromisoformat(item_date).strftime("%a %-m/%-d")


def _weather_icon(condition: str) -> str:
    for keyword, icon in _WEATHER_ICON_RULES:
        if keyword in condition:
            return icon
    return _DEFAULT_WEATHER_ICON


def _food_items(items_text: str) -> str:
    """Every actual food item in a menu's item-list text, comma-joined.

    Skips "Option N - Category" fragments (the real 3-option lunch shape
    from `sources.manual_menu.parser` — e.g. "Option 1 - Meat, Chicken
    Sandwich, Option 2 - ..."), keeping the real food names ("Chicken
    Sandwich, Veggie Burger, ...") and dropping the category scaffolding.
    Breakfast's plain comma list ("Banana Strawberry, Yogurt Smoothie,
    ...") has no such fragments, so this is a no-op filter for that case —
    every chunk survives. Falls back to the whole text if every chunk
    looks like a label (shouldn't happen, but an empty result would look
    broken). 2026-09-29: shows the full list, not just the first item —
    see this function's predecessor `_short_food_item` in git history if
    a short version is ever wanted again.
    """
    chunks = [c.strip() for c in items_text.split(", ")]
    foods = [c for c in chunks if c and not _OPTION_LABEL_PATTERN.match(c)]
    return ", ".join(foods) if foods else items_text


def _menu_items_label(item: dict[str, Any]) -> str:
    """The full food-items list for `item`, for the unified day-pill.

    Only ever called for today's own menu (`menu_fallback_date is None`) —
    a fallback future date belongs in the full "Menu" section only, not
    the header (2026-09-29, docs/p2-5-and-attention-card-implementation-
    spec.md §C).
    """
    # detail is "{Meal} on {weekday}, {month} {day}, {year}: {items}"
    # (services.dashboard._detail falls through to the full `content`
    # string for a menu doc — it has no summary and no " — " separator for
    # that function's usual em-dash trim to catch).
    _, _, items_text = item["detail"].partition(": ")
    items_text = items_text or item["detail"]  # fallback if no ": " separator
    return _food_items(items_text)


def _weather_box_html(weather: dict[str, Any]) -> str:
    """Peach-tinted box: temp (bold, orange) on top, advice text below.

    2026-09-29 — matches a supplied full-card reference image exactly: a
    distinctly-colored box, not a plain inline span (that was this
    function's predecessor, `_weather_section_html`) — and temperature/
    advice stacked on separate lines, not run together on one.
    """
    temp = round(weather["temp_high_f"])
    condition = weather["condition"]
    icon = _weather_icon(condition)
    codes = weather["advice"]
    advice_text = ", ".join(_ADVICE_LABELS.get(code, code.title()) for code in codes)
    tooltip = escape(f"{condition}, {', '.join(codes)}" if codes else condition)
    advice_html = (
        f'<div class="acamar-weather-box-advice">{escape(advice_text)}</div>'
        if advice_text
        else ""
    )
    return (
        f'<div class="acamar-weather-box" title="{tooltip}">'
        f'<div class="acamar-weather-box-temp">{icon} {temp}°F</div>'
        f"{advice_html}"
        f"</div>"
    )


def _food_chip_html(food: str) -> str:
    return f'<span class="acamar-food-chip">{escape(food)}</span>'


def _meal_section_html(item: dict[str, Any]) -> str:
    """"<MEAL NAME>" label + a row of individual food chips, for one meal.

    2026-09-29 — matches a supplied full-card reference image: discrete
    chips per food item (not one comma-joined line — that was this
    function's predecessor, `_meal_line_html`), under a section label that
    reuses this file's existing `.acamar-section-label` style (the same
    small-caps gray label already used for "Menu"/"Needs your attention"/
    "Coming up" — see the day-card CSS's own override note for why its
    margin is reset inside the card).
    """
    label = item["title"].split(" — ")[0].removesuffix(" Menu").upper()
    foods = [f.strip() for f in _menu_items_label(item).split(", ") if f.strip()]
    chips = "".join(_food_chip_html(food) for food in foods)
    return (
        f'<div class="acamar-section-label">{escape(label)}</div>'
        f'<div class="acamar-food-chips">{chips}</div>'
    )


def _day_card_html(fresh: dict[str, Any]) -> str:
    """The day card: TODAY/date on the left, a weather box on the right.

    2026-09-29 — 5th revision of this component (see backlog item 27's
    full history). Meals used to live inside this card (v4) but moved out
    entirely to their own section below the ask box, by request — see
    `_todays_meals_html`. Built from one fetch (`fresh`,
    `_day_card_fragment`'s own re-fetched dashboard data) so date/weather
    can't show data from two different moments. Weather is additive — a
    failed fetch just means that side's absent, not a broken card.
    """
    as_of = date.fromisoformat(fresh["as_of"])
    date_block = (
        '<div class="acamar-day-card-date-block">'
        '<div class="acamar-section-label">TODAY</div>'
        f'<div class="acamar-day-card-date">{escape(as_of.strftime("%a, %b %-d"))}</div>'
        "</div>"
    )

    top = f'<div class="acamar-day-card-top">{date_block}'
    weather = fresh.get("weather")
    if weather:
        top += (
            '<div class="acamar-day-card-top-right">'
            '<div class="acamar-day-card-vdivider"></div>'
            f"{_weather_box_html(weather)}"
            "</div>"
        )
    top += "</div>"

    return f'<div class="acamar-day-card">{top}</div>'


def _todays_meals_html(data: dict[str, Any]) -> str:
    """Today's breakfast/lunch as labeled chip-row sections, or "" if none.

    2026-09-29 — relocated out of the day card to its own spot below the
    ask box, by request, and the old card-list "Menu" section (label +
    `_row_html` per item, including a fallback-to-next-available-date
    message) was removed entirely in the same change — this block had
    become fully redundant with it. **Real, deliberate behavior loss, not
    an oversight**: this returns "" (renders nothing at all) when today
    has no menu of its own — a fallback future date, or genuinely nothing
    uploaded yet — the old section's "No menu for today — next up <date>"
    messaging and its multi-day-fallback display aren't reproduced here.
    See backlog item 27's full history.
    """
    if data.get("menu_fallback_date") is not None or not data.get("menu"):
        return ""
    return "".join(
        f'<div class="acamar-meal-section">{_meal_section_html(item)}</div>'
        for item in data["menu"]
    )


def _answer_card(answer: dict[str, Any]) -> str:
    """Answer text + low-confidence flag.

    No source list (dropped 2026-09-24, by request) — `answer["sources"]`
    still comes back from the API and is used for nothing here; not
    removed there since /query's response shape is a documented contract,
    not just this one view's concern.
    """
    text = escape(answer["answer"])
    low_confidence_html = ""
    if not answer.get("grounded", True):
        low_confidence_html = (
            '<div class="acamar-answer-low-confidence">⚠️ Not fully sure about this one</div>'
        )
    return (
        f'<div class="acamar-answer">'
        f'<div class="acamar-answer-text">{text}</div>'
        f"{low_confidence_html}"
        f"</div>"
    )


def _linked(title_html: str, url: str | None) -> str:
    """Wrap `title_html` in a link to the source's own page, if it has one.

    Added 2026-10-02 for cupertino_events — opens in a new tab so the
    dashboard itself is never navigated away from.
    """
    if not url:
        return title_html
    return (
        f'<a class="acamar-item-link" href="{escape(url, quote=True)}" '
        f'target="_blank" rel="noopener noreferrer">{title_html}</a>'
    )


def _attention_card(item: dict[str, Any], as_of: date) -> str:
    # Icon added 2026-10-02 (found by review): this card never called
    # _item_icon at all, only _row_html ("Coming up") did — a city event
    # that becomes urgent lost its tree icon the moment it moved into this
    # section. The accent stays urgency-tier (red/amber), not org-colored,
    # deliberately — that border's whole job is signaling TODAY/SOON, not
    # category/organization, which _row_html's border exists for instead.
    icon = _item_icon(item)
    title = _linked(escape(item["title"]), item.get("url"))
    detail = escape(item["detail"])
    days = _days_until(item.get("date"), as_of)
    when = escape(_when_text(days))
    label, accent = _attention_tier(days)
    return (
        f'<div class="acamar-attention" style="--acamar-attention-accent:{accent}">'
        f'<div class="acamar-attention-label" style="color:{accent}">{label}</div>'
        f'<div class="acamar-attention-title">{icon} {title}</div>'
        f'<div class="acamar-attention-sub">{detail}</div>'
        f'<div class="acamar-attention-when">{when}</div>'
        f"</div>"
    )


def _row_html(item: dict[str, Any], as_of: date) -> str:
    days = _days_until(item.get("date"), as_of)
    date_text = _short_date(item["date"]) if item.get("date") else ""
    when = escape(_when_text(days))
    when_color = _urgency_color(days)
    icon = _item_icon(item)
    title = _linked(escape(item["title"]), item.get("url"))
    detail = escape(item["detail"])
    category = item.get("category")
    org = item.get("organization")
    # category checked before organization (2026-10-02, found by review):
    # _item_icon above already does holiday/menu-before-organization: this
    # used to check organization first with no category carve-out, so a
    # hypothetical future city holiday/menu document (not reachable today —
    # cupertino_events only ever emits category=EVENT — but nothing
    # structurally prevented it) would get the icon right but the wrong
    # (green, not pink/teal) border. Matching the same precedence in both
    # places now, not just one.
    if category == "holiday":
        color = _CATEGORY_COLOR["holiday"]
    elif category == "menu":
        color = _CATEGORY_COLOR["menu"]
    else:
        color = (_ORGANIZATION_COLOR.get(org, "") if org else "") or (
            _CATEGORY_COLOR.get(category, _DEFAULT_COLOR) if category else _DEFAULT_COLOR
        )
    return (
        f'<div class="acamar-row" style="--acamar-accent:{color}">'
        f'<div class="acamar-row-date">{date_text}'
        f'<span class="acamar-row-when" style="color:{when_color}">{when}</span></div>'
        f"<div><div class=\"acamar-row-title\">{icon} {title}</div>"
        f'<div class="acamar-row-sub">{detail}</div></div>'
        f"</div>"
    )


def _section_label(text: str) -> None:
    st.markdown(f'<div class="acamar-section-label">{escape(text)}</div>', unsafe_allow_html=True)


def _empty(text: str) -> None:
    st.markdown(f'<div class="acamar-empty">{escape(text)}</div>', unsafe_allow_html=True)


@st.fragment(run_every="1h")
def _day_card_fragment(token: str, days: int) -> None:
    """Re-fetches and re-renders the date/weather day card, hourly.

    2026-09-29 — see docs/p2-5-and-attention-card-implementation-spec.md
    §A3. This is the 5th revision (see backlog item 27's full history for
    the four before it) — meals moved out of this card entirely to their
    own section below the ask box (`_todays_meals_html`, using `_render`'s
    own plain fetch, not this fragment's), since meals never needed hourly
    polling in the first place — only weather did. `st.fragment(run_every=
    ...)` reruns only this function on its own schedule, independent of
    user interaction, without re-running the rest of the page (verified
    against Streamlit's own docs this session — no new dependency,
    available since Streamlit 1.33/1.37, well within this project's
    pinned `>=1.63.0`).

    Date and weather come from ONE fetch (`_day_card_html`'s `fresh`
    argument) so they can't show data from two different moments, and —
    because it's genuinely one `st.markdown` call — can actually lay out
    together as one card, which separate elements structurally cannot.

    Calls the same full `_fetch_dashboard` a normal page load uses — there
    is no dedicated weather-only endpoint, and adding one wasn't in scope
    here. Accepted inefficiency: this re-fetches menu/event data the
    fragment no longer even uses, once an hour, rather than adding new API
    surface for a household-scale app.

    Fails soft: a request error here skips rendering this run entirely
    (stale card until the next hourly rerun) rather than raising inside a
    background auto-rerun, which would surface as a broken fragment on
    every subsequent scheduled tick, not just once.
    """
    try:
        fresh = _fetch_dashboard(token, days)
    except requests.RequestException:
        return
    st.markdown(
        f'<div class="acamar-day-card-row">{_day_card_html(fresh)}</div>',
        unsafe_allow_html=True,
    )


def _render(data: dict[str, Any], *, token: str, days: int) -> None:
    st.markdown(_STYLE, unsafe_allow_html=True)
    as_of = date.fromisoformat(data["as_of"])

    # Just the logo now (2026-09-24 layout, narrowed 2026-09-29) — one
    # `st.markdown` call since Streamlit gives every `st.*` call its own
    # top-level DOM node, so an `st.image` alone doesn't need the
    # multi-element centering wrapper this block used to need. The date
    # moved into the day card below (it polls hourly via `st.fragment`,
    # which renders as its own top-level element regardless of what
    # "block" it's called from, so date/weather/meals were never going to
    # stay groupable with the logo once any of them needed an independent
    # rerun — see `_day_card_fragment`'s docstring).
    header = (
        '<div class="acamar-header">'
        f'<img class="acamar-logo" src="{_LOGO_DATA_URI}" alt="acamar">'
        '<div class="acamar-wordmark">ac<span class="acamar-wordmark-a">a</span>mar</div>'
        "</div>"
    )
    st.markdown(header, unsafe_allow_html=True)

    _day_card_fragment(token, days)

    # Not `st.columns` anymore (2026-10-01) — a two-column [9, 1] row is
    # exactly what Streamlit responsively stacks to two full-width rows
    # once the narrow column gets too thin, which is what actually broke
    # the embedded-button layout below (confirmed by screenshot: the
    # button rendered on its own line, not overlapping the input). A
    # single `st.container(key=...)` has no such responsive-stacking
    # behavior to fight, and `key=` is Streamlit's own documented,
    # version-stable way to get a real CSS hook (`.st-key-<key>`) — unlike
    # the internal `data-testid` names the previous attempt guessed at.
    with st.container(key="ask_row"):
        question = st.text_input(
            "Ask",
            key="ask_question",
            placeholder="Ask ACAMAR anything",
            label_visibility="collapsed",
        )
        asked = st.button("→", key="ask_button")

    if asked and question:
        with st.spinner("Thinking…"):
            try:
                st.session_state["ask_answer"] = _query(token, question)
            except requests.RequestException as exc:
                st.session_state["ask_answer"] = None
                st.error(f"Couldn't get an answer: {exc}")

    answer = st.session_state.get("ask_answer")
    if answer:
        st.markdown(_answer_card(answer), unsafe_allow_html=True)

    # 2026-09-29: the old card-list "Menu" section (label + _row_html per
    # item, including a fallback-to-next-available-date message) was
    # removed here entirely, by request — it had become fully redundant
    # with the chip-based today's-meals block below, which relocated out
    # of the day card into this spot. Real, deliberate behavior loss, not
    # an oversight: this chip block shows nothing at all when today has no
    # menu of its own (a fallback future date, or nothing uploaded yet) —
    # the old section's "No menu for today — next up <date>" messaging and
    # its multi-day-fallback display aren't reproduced here. See
    # `_todays_meals_html`'s own docstring and backlog item 27's history.
    meals_html = _todays_meals_html(data)
    if meals_html:
        st.markdown(meals_html, unsafe_allow_html=True)

    # Everything in events/reminders/holidays is guaranteed dated now —
    # DashboardService drops anything without an event_date before this ever
    # sees it (2026-09-16): undated items are never returned, not just
    # de-emphasized, so there's no "more reminders, no date" bucket to render.
    dated = data["events"] + data["reminders"] + data["holidays"]
    dated.sort(key=lambda item: item["date"])

    label_col, pills_col = st.columns([1, 6])
    with label_col:
        st.markdown(
            '<div class="acamar-section-label" style="margin:10px 0 0;">Show</div>',
            unsafe_allow_html=True,
        )
    with pills_col:
        show = st.pills(
            "Show",
            _SHOW_OPTIONS,
            default=_SHOW_ALL,
            required=True,
            label_visibility="collapsed",
            key="domain_filter",
        )
    if show == _SHOW_SCHOOL:
        dated = [item for item in dated if item.get("organization") != _CITY_ORGANIZATION]
    elif show == _SHOW_CITY:
        dated = [item for item in dated if item.get("organization") == _CITY_ORGANIZATION]

    def _is_urgent(item: dict[str, Any]) -> bool:
        d = _days_until(item["date"], as_of)
        return d is not None and 0 <= d <= _URGENT_WINDOW_DAYS

    urgent = [item for item in dated if _is_urgent(item)]
    coming_up = [item for item in dated if item not in urgent]

    _section_label("Needs your attention")
    if urgent:
        st.markdown(
            "".join(_attention_card(item, as_of) for item in urgent), unsafe_allow_html=True
        )
    else:
        _empty("Nothing urgent.")

    _section_label("Coming up")
    if coming_up:
        st.markdown("".join(_row_html(item, as_of) for item in coming_up), unsafe_allow_html=True)
    else:
        _empty("Nothing else in the next few days.")


def main() -> None:
    """Render the dashboard: token gate → fetch → cards."""
    st.set_page_config(page_title="ACAMAR", page_icon=str(_LOGO_ICON_PATH), layout="centered")

    token = st.session_state.get("token", "")
    with st.sidebar:
        st.subheader("Access")
        token = st.text_input("Token", value=token, type="password")
        st.session_state["token"] = token
        days = st.slider("Look ahead", 1, 30, 30, help="days of events / reminders")

    if not token:
        if not _SETTINGS.disable_auth:
            st.info(
                "Enter your access token in the sidebar. Create one with "
                "`uv run python -m scripts.token create --label me`."
            )
            return
        token = "dev"  # noqa: S105 - not a secret, disable_auth means the API ignores it

    try:
        data = _fetch_dashboard(token, days)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        st.error("Token rejected." if code == 401 else f"API error: {exc}")
        return
    except requests.RequestException as exc:
        st.error(f"Can't reach the API at {_SETTINGS.api_base_url} — is it running?  ({exc})")
        return

    _render(data, token=token, days=days)


main()
