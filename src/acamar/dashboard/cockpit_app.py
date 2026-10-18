"""ACAMAR Pilot Cockpit (Streamlit) — menu upload, observability, kill switch.

A separate app/process from the household-facing dashboard
(`dashboard/streamlit_app.py`) — a different credential (`cockpit_token`,
not a `subscribers` token) and a different, admin-shaped surface: upload,
traffic visibility, and the pause/shutdown controls are a materially bigger
blast radius than the read-only dashboard.

Visual language ("Acamar Glass Deck", 2026-09-25 design review — see the
backlog): round analog dials with a digital numeric core, a Master Caution/
Warning/API-Link annunciator strip, a traffic tape, and a guarded kill
switch. Real interactivity stays on native Streamlit widgets; the bezel/
gauge/annunciator chrome is decorative HTML/CSS/SVG injected via
`st.markdown(unsafe_allow_html=True)`. Two real Streamlit constraints shaped
this file (see `streamlit_app.py`'s own header comment for the first one,
already discovered there):
- A custom HTML block from one `st.markdown` call can never *contain*
  another `st.*` call's widget — each renders as its own top-level DOM
  node. Sections with no interactive widget (the header, annunciators,
  source gauges, traffic tape, Flight Data Recorder) are one HTML blob
  each; sections with real widgets (upload form, pause/resume, kill
  switch) stay native, reskinned by broad element-selector CSS instead of
  a bespoke per-widget bezel.
- `st.form` batches its children's changes until submit — incompatible
  with a *live* "currently on file" coverage preview that should update as
  soon as meal/year/month change. The upload section deliberately doesn't
  use `st.form`; a plain button guards the actual upload action instead.

Run: ``uv run streamlit run src/acamar/dashboard/cockpit_app.py --server.port 8502``
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, date, datetime
from html import escape
from typing import Any

import requests
import streamlit as st

from acamar.config import get_settings

_SETTINGS = get_settings()
_SHUTDOWN_CONFIRMATION = "SHUTDOWN"
_EXPECTED_CADENCE_HOURS = 4.0
"""A source is considered fully "overdue" (needle fully drooped) at 3x this
— matches the ingest cron's current 6x/day (~4h) schedule
(`scripts/com.acamar.ingest-faria-events.plist`); not stored anywhere
queryable, so this is a display-only estimate, not a per-source contract.
Default for any `domain` not in `_EXPECTED_CADENCE_HOURS_BY_DOMAIN` below.
"""
_EXPECTED_CADENCE_HOURS_BY_SOURCE: dict[tuple[str, str], float | None] = {
    ("city", "event"): 24.0,
    # cupertino_events runs once daily (scripts/com.acamar.ingest-cupertino-
    # events.plist), not 6x/day like the sources above — real bug this
    # would otherwise have reintroduced had it just reused the flat 4.0: a
    # perfectly healthy run would read "overdue"/RECENCY DECAY for ~20 of
    # every 24 hours, exactly the "dashboard looks stale" false alarm
    # CLAUDE.md's own gotcha already warns about, just on this panel
    # instead of the household one.
    ("school", "menu"): None,
    # manual_menu — a second real instance of the exact same bug class,
    # found by review (2026-10-02): keying the override by `domain` alone
    # meant manual_menu (school/menu, upload-triggered, no fixed cadence at
    # all) inherited faria_events' 4h expectation just for sharing
    # `Domain.SCHOOL`. `None` means "never decays" (see `_recency_angle`) —
    # there's no meaningful "expected hours between uploads" to assign an
    # upload-triggered source, and `_menu_reminder_lit` below is already the
    # purpose-built signal for "is the menu going stale", not this gauge.
}

_GLYPH_SVG = (
    '<svg viewBox="0 0 32 32" aria-hidden="true">'
    '<circle cx="16" cy="16" r="14" fill="#0b0f16" stroke="#3a3f4a" stroke-width="1.5"/>'
    '<path d="M2 16 A14 14 0 0 1 30 16" fill="#12345c"/>'
    '<path d="M2 16 A14 14 0 0 0 30 16" fill="#3a2a14"/>'
    '<circle cx="16" cy="16" r="14" fill="none" stroke="#22d3ee" stroke-width="1.2"/>'
    '<line x1="16" y1="16" x2="6" y2="12" stroke="#eef3f8" stroke-width="1.6"'
    ' stroke-linecap="round"/>'
    '<line x1="16" y1="16" x2="26" y2="12" stroke="#eef3f8" stroke-width="1.6"'
    ' stroke-linecap="round"/>'
    '<circle cx="16" cy="16" r="2" fill="#eef3f8"/>'
    "</svg>"
)

_FONTS_HREF = (
    "https://fonts.googleapis.com/css2?"
    "family=Chakra+Petch:wght@500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap"
)

_CSS = f"""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{_FONTS_HREF}" rel="stylesheet">
<style>
  :root {{
    --bg: #060a12; --bezel-hi: #3a3f4a; --bezel-lo: #14161b;
    --screen: #0b0f16; --screen-2: #0e1420;
    --text: #eef3f8; --text-dim: #8b96a5;
    --ok: #33d17a; --warn: #ffb703; --danger: #ff2d3c;
    --info: #22d3ee; --command: #d946ef;
    --hairline: #232a35; --rivet: #5a6272;
    --font-display: 'Chakra Petch', 'Arial Narrow', sans-serif;
    --font-mono: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
  }}
  .stApp, body {{ background: var(--bg) !important; }}
  .mono {{ font-family: var(--font-mono); font-variant-numeric: tabular-nums; }}
  .placard {{
    font-family: var(--font-display); text-transform: uppercase; letter-spacing: .09em;
    font-weight: 600; color: var(--text-dim); font-size: 10px;
  }}
  .bezel {{
    position: relative; border-radius: 14px; padding: 2px;
    background: linear-gradient(155deg, var(--bezel-hi), var(--bezel-lo) 65%);
    box-shadow: 0 10px 30px -14px rgba(0,0,0,.7);
  }}
  .bezel::before {{
    content: ""; position: absolute; inset: 0; pointer-events: none; z-index: 2;
    background-repeat: no-repeat;
    background-image:
      radial-gradient(circle 2.6px, var(--rivet) 45%, transparent 46%),
      radial-gradient(circle 2.6px, var(--rivet) 45%, transparent 46%),
      radial-gradient(circle 2.6px, var(--rivet) 45%, transparent 46%),
      radial-gradient(circle 2.6px, var(--rivet) 45%, transparent 46%);
    background-position:
      9px 9px, calc(100% - 9px) 9px, 9px calc(100% - 9px), calc(100% - 9px) calc(100% - 9px);
  }}
  .screen {{
    position: relative; border-radius: 12px; overflow: hidden; padding: 16px;
    background: linear-gradient(175deg, var(--screen-2), var(--screen) 40%);
  }}
  .screen::after {{
    content: ""; position: absolute; inset: 0; pointer-events: none;
    background: linear-gradient(
      115deg, rgba(255,255,255,.05) 0%, rgba(255,255,255,.015) 20%, transparent 38%
    );
  }}
  .panel-label {{
    font-family: var(--font-display); text-transform: uppercase; letter-spacing: .12em;
    font-size: 11px; font-weight: 700; color: var(--info); margin: 4px 0 12px;
    display: flex; align-items: center; gap: 8px;
  }}
  .panel-label::before {{ content: "◆"; font-size: 8px; }}
  .overhead {{
    display: flex; align-items: center; justify-content: space-between;
    gap: 16px; padding: 12px 20px;
  }}
  .overhead-id {{ display: flex; align-items: center; gap: 14px; }}
  .overhead-id svg {{ width: 32px; height: 32px; flex: none; }}
  .overhead-title {{
    font-family: var(--font-display); font-weight: 700; font-size: 16px;
    letter-spacing: .12em; color: var(--text);
  }}
  .overhead-title small {{
    display: block; font-size: 9px; letter-spacing: .18em; color: var(--text-dim);
    font-weight: 500; margin-top: 2px;
  }}
  .overhead-right {{ display: flex; align-items: center; gap: 16px; }}
  .run-placard {{
    font-family: var(--font-display); font-weight: 700; font-size: 11px; letter-spacing: .1em;
    padding: 6px 12px; border-radius: 4px; border: 1px solid var(--hairline);
    background: var(--screen);
  }}
  .run-placard.is-run {{
    color: var(--ok); border-color: color-mix(in srgb, var(--ok) 45%, var(--hairline));
  }}
  .run-placard.is-paused {{
    color: var(--warn); border-color: color-mix(in srgb, var(--warn) 45%, var(--hairline));
  }}
  .clock {{ font-size: 13px; color: var(--text-dim); }}
  .annunciator-row {{ display: flex; gap: 10px; margin: 10px 0; flex-wrap: wrap; }}
  .annunciator {{ flex: 1 1 160px; }}
  .light {{
    display: flex; align-items: center; gap: 10px; padding: 10px 14px; border-radius: 10px;
    border: 1px solid var(--hairline); background: var(--screen);
  }}
  .light .dot {{ width: 11px; height: 11px; border-radius: 50%; background: #2a3038; flex: none; }}
  .light .txt {{
    font-family: var(--font-display); font-weight: 700; font-size: 11px;
    letter-spacing: .1em; color: var(--text-dim);
  }}
  .light.lit-danger {{ border-color: color-mix(in srgb, var(--danger) 50%, var(--hairline)); }}
  .light.lit-danger .dot {{
    background: var(--danger);
    box-shadow: 0 0 12px 2px color-mix(in srgb, var(--danger) 70%, transparent);
  }}
  .light.lit-danger .txt {{ color: var(--danger); }}
  .light.lit-warn {{ border-color: color-mix(in srgb, var(--warn) 50%, var(--hairline)); }}
  .light.lit-warn .dot {{
    background: var(--warn);
    box-shadow: 0 0 12px 2px color-mix(in srgb, var(--warn) 65%, transparent);
  }}
  .light.lit-warn .txt {{ color: var(--warn); }}
  .light.lit-ok {{ border-color: color-mix(in srgb, var(--ok) 45%, var(--hairline)); }}
  .light.lit-ok .dot {{
    background: var(--ok);
    box-shadow: 0 0 10px 1px color-mix(in srgb, var(--ok) 60%, transparent);
  }}
  .light.lit-ok .txt {{ color: var(--ok); }}
  .hero-counters {{
    display: grid; grid-template-columns: repeat(4,1fr); gap: 10px; margin-bottom: 6px;
  }}
  .hero-tile {{ padding: 12px 14px; }}
  .hero-tile .n {{
    font-family: var(--font-mono); font-weight: 700; font-size: 26px; line-height: 1;
  }}
  .hero-tile .l {{ margin-top: 6px; }}
  .hero-tile.c-ok .n {{ color: var(--ok); }}
  .hero-tile.c-warn .n {{ color: var(--warn); }}
  .hero-tile.c-danger .n {{ color: var(--danger); }}
  .hero-tile.c-info .n {{ color: var(--text); }}
  .domain-group {{ margin-bottom: 14px; }}
  .domain-group:last-child {{ margin-bottom: 0; }}
  .domain-head {{
    display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px;
    font-family: var(--font-display); font-size: 10px; letter-spacing: .1em;
    color: var(--text-dim); border-bottom: 1px solid var(--hairline); padding-bottom: 6px;
  }}
  .domain-head b {{ color: var(--text); }}
  .domain-head .dcount {{ margin-left: auto; font-family: var(--font-mono); }}
  .domain-head .dcount.attn {{ color: var(--warn); }}
  .tile-grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px;
  }}
  .tile {{ text-align: center; }}
  .tile .src-label {{ font-size: 9px; margin-bottom: 6px; }}
  .tile svg {{ width: 110px; height: 92px; }}
  .tile .readout .big {{
    font-family: var(--font-mono); font-weight: 700; font-size: 14px; color: var(--text);
  }}
  .tile .readout .small {{
    font-family: var(--font-mono); font-size: 9.5px; color: var(--text-dim); margin-top: 2px;
  }}
  .led-strip {{
    height: 4px; border-radius: 2px; margin-top: 8px; background: #1b2029; overflow: hidden;
  }}
  .led-strip > i {{ display: block; height: 100%; width: 100%; }}
  .led-strip.ok > i {{ background: var(--ok); }}
  .led-strip.warn > i {{ background: var(--warn); }}
  .led-strip.danger > i {{ background: var(--danger); }}
  .tag {{
    display: inline-block; margin-top: 6px; font-size: 8px; letter-spacing: .07em;
    font-family: var(--font-display); font-weight: 600; color: var(--warn);
  }}
  .tape {{ max-height: 260px; overflow-y: auto; }}
  .tape-row {{
    display: grid; grid-template-columns: 58px 1fr 40px 40px 70px; gap: 8px;
    align-items: center; padding: 6px 4px; border-bottom: 1px solid var(--hairline);
    font-family: var(--font-mono); font-size: 10.5px;
  }}
  .tape-row:last-child {{ border-bottom: none; }}
  .tape-row.latest {{
    border-left: 2px solid var(--info); padding-left: 8px;
    background: color-mix(in srgb, var(--info) 5%, transparent);
  }}
  .tape-row .dim {{ color: var(--text-dim); }}
  .tape-row .method {{ color: var(--text-dim); font-weight: 700; }}
  .tape-row .path {{
    color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .chip {{
    padding: 1px 6px; border-radius: 3px; font-weight: 700;
    text-align: center; font-size: 10px;
  }}
  .chip.s2 {{ background: color-mix(in srgb, var(--ok) 18%, transparent); color: var(--ok); }}
  .chip.s4 {{ background: color-mix(in srgb, var(--warn) 20%, transparent); color: var(--warn); }}
  .bar-track {{ height: 5px; background: #1b2029; border-radius: 3px; overflow: hidden; }}
  .bar-fill {{ height: 100%; background: var(--info); border-radius: 3px; }}
  .on-file-chip {{
    font-size: 10.5px; color: var(--text-dim); font-family: var(--font-mono);
    border: 1px dashed var(--hairline); border-radius: 6px; padding: 6px 10px; margin: 8px 0;
  }}
  .on-file-chip b {{ color: var(--info); font-weight: 700; }}
  .coverage-grid {{
    display: grid; grid-template-columns: repeat(10,1fr); gap: 4px; margin-bottom: 10px;
  }}
  .cov-cell {{
    aspect-ratio: 1; border-radius: 3px; display: flex; align-items: center;
    justify-content: center; font-family: var(--font-mono); font-size: 8px;
    color: var(--text-dim); background: #1b2029;
  }}
  .cov-cell.filled {{
    background: color-mix(in srgb, var(--ok) 30%, var(--screen-2)); color: var(--ok);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--ok) 45%, transparent);
  }}
  .cov-cell.off {{ opacity: .35; }}
  .chart-sub {{ font-size: 10px; color: var(--text-dim); margin: 4px 0; }}
  .bars {{ display: flex; flex-direction: column; gap: 8px; }}
  .bar-item {{
    display: grid; grid-template-columns: 92px 1fr 46px; align-items: center; gap: 8px;
  }}
  .bar-item .b-label {{
    font-family: var(--font-mono); font-size: 10px; color: var(--text-dim);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .bar-item .b-track {{
    height: 11px; background: #1b2029; border-radius: 3px; overflow: hidden;
  }}
  .bar-item .b-fill {{
    height: 100%; border-radius: 3px; background: var(--info); min-width: 3px;
  }}
  .bar-item .b-val {{ font-family: var(--font-mono); font-size: 10px; text-align: right; }}
  .vbars {{ display: flex; align-items: flex-end; gap: 4px; height: 84px; }}
  .vbar {{ flex: 1; background: var(--info); border-radius: 2px 2px 0 0; min-height: 2px; }}
  .vbar.peak {{ background: var(--text); }}
  .vbar-labels {{ display: flex; gap: 4px; margin-top: 4px; }}
  .vbar-labels span {{
    flex: 1; text-align: center; font-family: var(--font-mono);
    font-size: 7px; color: var(--text-dim);
  }}
  .pipeline-stat {{
    display: flex; align-items: center; gap: 14px; margin-top: 12px;
    padding-top: 10px; border-top: 1px solid var(--hairline);
  }}
  .pipeline-stat .num {{
    font-family: var(--font-mono); font-weight: 700; font-size: 28px;
    color: var(--command); line-height: 1;
  }}
  .pipeline-stat .unit {{ font-size: 10px; color: var(--text-dim); }}
  .spark {{ flex: 1; }}
  .spark svg {{ width: 100%; height: 36px; display: block; }}
  .kill-bay {{
    margin-top: 12px; padding: 14px; border-radius: 10px;
    background: repeating-linear-gradient(
      135deg, #1a1005, #1a1005 8px, #120c04 8px, #120c04 16px
    );
    border: 1px solid color-mix(in srgb, var(--warn) 35%, var(--hairline));
  }}
  .kill-label {{
    font-family: var(--font-display); color: var(--warn); font-weight: 700;
    letter-spacing: .1em; font-size: 11px; text-transform: uppercase; margin-bottom: 6px;
  }}
  .kill-note {{ font-size: 10px; color: var(--text-dim); line-height: 1.5; margin-bottom: 8px; }}
  /* reskin native widgets so they read as part of the same instrument panel */
  .stTextInput input, .stNumberInput input,
  .stSelectbox div[data-baseweb="select"] > div {{
    background: var(--screen-2) !important; border: 1px solid var(--hairline) !important;
    color: var(--text) !important; font-family: var(--font-mono) !important;
  }}
  .stButton button {{
    background: var(--screen-2); border: 1px solid var(--hairline); color: var(--text-dim);
    font-family: var(--font-display); font-weight: 700; letter-spacing: .06em;
    text-transform: uppercase; font-size: 12px;
  }}
  .stButton button:hover:not(:disabled) {{ border-color: var(--info); color: var(--info); }}
  .stButton button:disabled {{ opacity: .35; }}
  .stCheckbox label p {{
    font-family: var(--font-display) !important; font-size: 11px !important;
    letter-spacing: .06em; text-transform: uppercase; color: var(--text-dim) !important;
  }}
  /* Streamlit's file-uploader testid has moved across versions (pinned
     streamlit>=1.63.0) — this targets the current dropzone testid; if a
     future bump silently drops this styling, that's why. */
  [data-testid="stFileUploaderDropzone"] {{
    background: var(--screen-2) !important; border: 1px dashed var(--hairline) !important;
  }}
</style>
"""


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _admin_status(token: str) -> dict[str, Any]:
    response = requests.get(
        f"{_SETTINGS.api_base_url}/admin/status", headers=_headers(token), timeout=10
    )
    response.raise_for_status()
    return dict(response.json())


def _menu_coverage(
    token: str, *, source_id: str, meal: str, year: int, month: int
) -> dict[str, Any]:
    response = requests.get(
        f"{_SETTINGS.api_base_url}/admin/menu-coverage",
        headers=_headers(token),
        params={"source_id": source_id, "meal": meal, "year": str(year), "month": str(month)},
        timeout=10,
    )
    response.raise_for_status()
    return dict(response.json())


def _upload_menu(
    token: str,
    *,
    source_id: str,
    meal: str,
    year: int,
    month: int,
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    response = requests.post(
        f"{_SETTINGS.api_base_url}/admin/menu-upload",
        headers=_headers(token),
        params={"source_id": source_id, "meal": meal, "year": str(year), "month": str(month)},
        files={"file": (filename, content, "application/pdf")},
        timeout=120,  # ingestion (embedding every day's document) runs inline
    )
    response.raise_for_status()
    return dict(response.json())


def _set_paused(token: str, *, paused: bool) -> dict[str, Any]:
    action = "pause" if paused else "resume"
    response = requests.post(
        f"{_SETTINGS.api_base_url}/admin/{action}", headers=_headers(token), timeout=10
    )
    response.raise_for_status()
    return dict(response.json())


def _shutdown(token: str) -> None:
    requests.post(
        f"{_SETTINGS.api_base_url}/admin/shutdown",
        headers=_headers(token),
        params={"confirm": _SHUTDOWN_CONFIRMATION},
        timeout=10,
    ).raise_for_status()


# ---------------------------------------------------------------------------
# gauge SVG — a round analog dial with a symbolic needle position (ok:
# continuous recency decay; error/never_run: fixed sectors) and a digital
# numeric core. Pure decoration, no interactivity, so it's plain HTML/SVG.
# ---------------------------------------------------------------------------

_STATE_COLOR = {"ok": "var(--ok)", "error": "var(--danger)", "never_run": "#4a5160"}


def _polar(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    rad = math.radians(deg - 90)
    return cx + r * math.cos(rad), cy + r * math.sin(rad)


def _arc_path(cx: float, cy: float, r: float, a0: float, a1: float) -> str:
    x0, y0 = _polar(cx, cy, r, a0)
    x1, y1 = _polar(cx, cy, r, a1)
    large = 1 if (a1 - a0) % 360 > 180 else 0
    return f"M {x0:.2f} {y0:.2f} A {r} {r} 0 {large} 1 {x1:.2f} {y1:.2f}"


def _recency_angle(
    last_run_at: str | None, *, now: datetime, domain: str = "", category: str = ""
) -> float:
    """0deg (fresh, 12 o'clock) sweeping to 90deg at 3x this source's expected cadence.

    Continuous, not the three fixed symbolic positions error/never_run use —
    so an `ok` source that's quietly overdue still visibly droops instead of
    reading identically to one that just ran (the gap the 2026-09-25 design
    review's real finding: CLAUDE.md's own "not discovered by the dashboard
    looks stale" gotcha, applied to this panel too). `(domain, category)`
    looks up `_EXPECTED_CADENCE_HOURS_BY_SOURCE` (falling back to the flat
    default; an explicit `None` entry means this source never decays at
    all) — added 2026-10-01 when `cupertino_events` (`city`, once-daily)
    made the single global cadence actively wrong for a real source, not
    just imprecise for a hypothetical one; keyed by the full `(domain,
    category)` pair since 2026-10-02, when `domain` alone turned out to
    conflate `manual_menu` (upload-triggered, no cadence) with
    `faria_events` (6x/day) just for sharing `Domain.SCHOOL`.
    """
    if last_run_at is None:
        return 0.0
    expected = _EXPECTED_CADENCE_HOURS_BY_SOURCE.get((domain, category), _EXPECTED_CADENCE_HOURS)
    if expected is None:
        return 0.0
    age_hours = (now - datetime.fromisoformat(last_run_at)).total_seconds() / 3600
    return min(age_hours / (expected * 3), 1.0) * 90.0


def _gauge_config(source: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    status = source["last_status"]
    if status == "error":
        return {"state": "error", "angle": 110.0, "flag": False}
    if status == "never_run":
        return {"state": "never_run", "angle": 150.0, "flag": True}
    angle = _recency_angle(
        source.get("last_run_at"), now=now, domain=source["domain"], category=source["category"]
    )
    return {"state": "ok", "angle": angle, "flag": False}


_STALE_ANGLE_THRESHOLD = 30.0
"""Shared by every "is this source stale" check in this module (2026-10-02,
found by review — the 30deg threshold used to be duplicated as a bare
literal in `_render_header` and `_tile_html` independently, with
`_source_tiles_html`'s "Attention only" filter not checking it at all,
so a source could light Master Caution globally while reading "ALL
NOMINAL"/being hidden under that filter, with no way to find out why
the light was on)."""


def _is_stale(source: dict[str, Any], *, now: datetime) -> bool:
    """True only for an `ok`-status source whose recency has decayed past the threshold.

    `error`/`never_run` are handled by their own explicit status checks
    wherever this is used, not folded in here.
    """
    return source["last_status"] == "ok" and (
        _recency_angle(
            source.get("last_run_at"),
            now=now,
            domain=source["domain"],
            category=source["category"],
        )
        > _STALE_ANGLE_THRESHOLD
    )


def _sector_path(cx: float, cy: float, r: float, a0: float, a1: float, stroke: str) -> str:
    return (
        f'<path d="{_arc_path(cx, cy, r, a0, a1)}" fill="none" '
        f'stroke="{stroke}" stroke-width="4" stroke-linecap="round"/>'
    )


def _gauge_svg(cfg: dict[str, Any]) -> str:
    cx, cy, r = 59.0, 52.0, 40.0
    ticks = "".join(
        _tick_svg(cx, cy, r, -135 + i * 22.5, major=(i % 3 == 0)) for i in range(13)
    )
    sectors = (
        _sector_path(cx, cy, r + 3, -135, 20, "#233a2c")
        + _sector_path(cx, cy, r + 3, 20, 90, "#3d3418")
        + _sector_path(cx, cy, r + 3, 90, 135, "#3c1c1f")
    )
    color = _STATE_COLOR.get(str(cfg["state"]), "#4a5160")
    tip_x, tip_y = _polar(cx, cy, r - 10, float(cfg["angle"]))
    flag = ""
    if cfg.get("flag"):
        flag = (
            f'<rect x="{cx + 12}" y="{cy - 26}" width="14" height="9" rx="1.5"'
            ' fill="var(--danger)"/>'
            f'<text x="{cx + 19}" y="{cy - 19}" font-size="6" fill="#fff"'
            ' text-anchor="middle">X</text>'
        )
    needle = (
        f'<line x1="{cx}" y1="{cy}" x2="{tip_x:.1f}" y2="{tip_y:.1f}" stroke="{color}"'
        ' stroke-width="2.6" stroke-linecap="round"/>'
    )
    return (
        '<svg viewBox="0 0 118 100">'
        f'<circle cx="{cx}" cy="{cy}" r="{r + 8}" fill="#0b0f16"/>'
        f"{sectors}{ticks}{needle}"
        f'<circle cx="{cx}" cy="{cy}" r="4" fill="{color}"/>'
        f"{flag}</svg>"
    )


def _tick_svg(cx: float, cy: float, r: float, angle: float, *, major: bool) -> str:
    x1, y1 = _polar(cx, cy, r, angle)
    x2, y2 = _polar(cx, cy, r - (7 if major else 4), angle)
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"'
        ' stroke="#3a4150" stroke-width="1.3"/>'
    )


def _relative_time(iso_timestamp: str | None, *, now: datetime) -> str:
    if iso_timestamp is None:
        return "—"
    delta = now - datetime.fromisoformat(iso_timestamp)
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{max(0, int(delta.total_seconds() // 60))}m ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"


def _tile_html(source: dict[str, Any], *, now: datetime) -> str:
    cfg = _gauge_config(source, now=now)
    label = escape(f"{source['domain']} / {source['category']}".upper())
    status = source["last_status"]
    tag = ""
    if status == "error":
        big, small, led = "ERROR", _relative_time(source.get("last_run_at"), now=now), "danger"
    elif status == "never_run":
        big, small, led = "NEVER RUN", "— no query set", "warn"
    else:
        stale = float(cfg["angle"]) > _STALE_ANGLE_THRESHOLD
        big = "OK"
        small = _relative_time(source.get("last_run_at"), now=now) + (" · overdue" if stale else "")
        led = "warn" if stale else "ok"
        tag = "RECENCY DECAY" if stale else ""
    readout = f'<div class="readout"><div class="big">{big}</div>'
    readout += f'<div class="small">{escape(small)}</div></div>'
    return (
        '<div class="tile">'
        f'<div class="src-label placard">{label}</div>'
        f"{_gauge_svg(cfg)}"
        f"{readout}"
        + (f'<div class="tag">{escape(tag)}</div>' if tag else "")
        + f'<div class="led-strip {led}"><i></i></div>'
        "</div>"
    )


def _light_html(label: str, lit: str | None) -> str:
    cls = f"lit-{lit}" if lit else ""
    return (
        f'<div class="annunciator bezel"><div class="screen light {cls}">'
        f'<span class="dot"></span><span class="txt">{escape(label)}</span>'
        "</div></div>"
    )


def _count_status(sources: list[dict[str, Any]], status: str) -> int:
    return sum(1 for s in sources if s["last_status"] == status)


def _hero_tile_html(n: int, label: str, css_class: str) -> str:
    return (
        f'<div class="bezel"><div class="screen hero-tile {css_class}">'
        f'<div class="n mono">{n}</div><div class="l placard">{escape(label)}</div>'
        "</div></div>"
    )


def _menu_reminder_lit(token: str) -> bool | None:
    """Whether the "Menu Reminder" annunciator should light.

    `None` means "can't tell" (no `manual_menu_source_id` configured, or
    the coverage check itself failed) — must never render as lit, since a
    failed check isn't evidence a menu is actually missing. Checks *next*
    calendar month's lunch coverage specifically — the month still weeks
    away, not the one already in progress (the per-month coverage chip
    already in the upload section covers "is this month done" well
    enough on its own). 2026-09-29,
    docs/p2-5-and-attention-card-implementation-spec.md §B.
    """
    if not _SETTINGS.manual_menu_source_id:
        return None
    today = date.today()
    next_month = today.month % 12 + 1
    next_year = today.year + (1 if today.month == 12 else 0)
    try:
        coverage = _menu_coverage(
            token,
            source_id=_SETTINGS.manual_menu_source_id,
            meal="lunch",
            year=next_year,
            month=next_month,
        )
    except requests.RequestException:
        return None
    return len(coverage.get("covered_days", [])) == 0


def _render_header(status: dict[str, Any] | None, *, token: str, api_reachable: bool) -> None:
    """Overhead panel + Master Caution/Warning/API-Link/Menu-Reminder annunciators + hero counters.

    All-decorative HTML (no widgets), so it's one `st.markdown` call. The
    clock reads "last refreshed at", not a live tick — Streamlit reruns on
    interaction, not on a timer, so a genuinely ticking clock would need a
    JS auto-refresh helper this project doesn't depend on; a mockup showed
    a live clock, this honestly doesn't fake one.
    """
    now = datetime.now(UTC)
    paused = bool((status or {}).get("paused", False))
    sources = (status or {}).get("sources", [])

    header = (
        '<div class="bezel"><div class="screen overhead">'
        f'<div class="overhead-id">{_GLYPH_SVG}'
        '<div class="overhead-title">ACAMAR · PILOT COCKPIT'
        # "faria elementary" dropped from this subtitle (2026-10-01) — no
        # longer accurate on its own now that cupertino_events (a city, not
        # school, source) is one of the sources this cockpit watches.
        "<small>household daily-care telemetry</small></div></div>"
        '<div class="overhead-right">'
        f'<span class="clock mono">refreshed {now.astimezone().strftime("%H:%M:%S")}</span>'
        f'<span class="run-placard {"is-paused" if paused else "is-run"}">'
        f'● {"PAUSED" if paused else "RUNNING"}</span>'
        "</div></div></div>"
    )

    has_error = any(s["last_status"] == "error" for s in sources)
    has_caution = any(s["last_status"] == "never_run" for s in sources) or any(
        _is_stale(s, now=now) for s in sources
    )
    menu_reminder = _menu_reminder_lit(token)
    annunciators = (
        '<div class="annunciator-row">'
        + _light_html("Master Warning", "danger" if has_error else None)
        + _light_html("Master Caution", "warn" if has_caution else None)
        + _light_html("API Link", "ok" if api_reachable else "danger")
        + _light_html("Menu Reminder", "warn" if menu_reminder else None)
        + "</div>"
    )

    counters = (
        '<div class="hero-counters">'
        + _hero_tile_html(len(sources), "Sources", "c-info")
        + _hero_tile_html(_count_status(sources, "ok"), "Nominal", "c-ok")
        + _hero_tile_html(_count_status(sources, "never_run"), "Caution", "c-warn")
        + _hero_tile_html(_count_status(sources, "error"), "Warning", "c-danger")
        + "</div>"
    )
    st.markdown(header + annunciators + counters, unsafe_allow_html=True)


def _render_observability_section(status: dict[str, Any] | None) -> None:
    """Source-health instrument tiles (grouped by domain) + the traffic tape.

    Grouping by `domain` is the answer to "how does this scale past a
    handful of sources": a domain with nothing wrong collapses to one
    summary line instead of N individual dials. The "Attention only"
    checkbox is the one real widget here; everything else is markup.
    """
    st.markdown('<div class="panel-label">Source Health</div>', unsafe_allow_html=True)
    attention_only = st.checkbox("Attention only", key="attn_only")
    sources = (status or {}).get("sources", [])
    tiles_html = _source_tiles_html(sources, attention_only=attention_only)
    st.markdown(tiles_html, unsafe_allow_html=True)

    st.markdown('<div class="panel-label">Traffic Tape</div>', unsafe_allow_html=True)
    recent_requests = (status or {}).get("recent_requests", [])
    st.markdown(_traffic_tape_html(recent_requests), unsafe_allow_html=True)


def _source_tiles_html(sources: list[dict[str, Any]], *, attention_only: bool) -> str:
    if not sources:
        return '<div class="chart-sub">No sources seeded yet.</div>'
    now = datetime.now(UTC)
    domains: list[str] = []
    for s in sources:
        if s["domain"] not in domains:
            domains.append(s["domain"])

    groups_html = []
    for domain in domains:
        group = [s for s in sources if s["domain"] == domain]
        # != "ok" OR stale (2026-10-02, found by review): this used to check
        # last_status alone, so a source that was `ok` but genuinely overdue
        # — exactly what lights Master Caution above — read "ALL NOMINAL"
        # here and vanished entirely under "Attention only", with no way to
        # find out which source was causing the lit light.
        need_attention = [s for s in group if s["last_status"] != "ok" or _is_stale(s, now=now)]
        visible = need_attention if attention_only else group
        if not visible:
            continue
        count_label = (
            f"{len(need_attention)} NEEDS ATTENTION" if need_attention else "ALL NOMINAL"
        )
        source_word = "source" if len(group) == 1 else "sources"
        attn_class = " attn" if need_attention else ""
        header = (
            '<div class="domain-head">'
            f"<b>{escape(domain)}</b><span>{len(group)} {source_word}</span>"
            f'<span class="dcount{attn_class}">{count_label}</span>'
            "</div>"
        )
        tiles = "".join(_tile_html(s, now=now) for s in visible)
        groups_html.append(
            f'<div class="domain-group">{header}<div class="tile-grid">{tiles}</div></div>'
        )

    return "".join(groups_html) or '<div class="chart-sub">Nothing needs attention right now.</div>'


def _traffic_tape_html(requests_: list[dict[str, Any]]) -> str:
    if not requests_:
        return '<div class="chart-sub">No requests captured yet.</div>'
    shown = requests_[:30]  # the buffer already caps at cockpit_max_recent_requests
    max_ms = max((r["duration_ms"] for r in shown), default=1) or 1
    rows = []
    for i, r in enumerate(shown):
        chip_class = "s4" if r["status_code"] >= 400 else "s2"
        pct = max(4, round(100 * r["duration_ms"] / max_ms))
        at = datetime.fromisoformat(r["at"]).strftime("%H:%M:%S")
        rows.append(
            f'<div class="tape-row{" latest" if i == 0 else ""}">'
            f'<span class="dim">{at}</span><span class="path">{escape(r["path"])}</span>'
            f'<span class="method">{escape(r["method"])}</span>'
            f'<span class="chip {chip_class}">{r["status_code"]}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{pct}%"></span></span>'
            "</div>"
        )
    return f'<div class="tape">{"".join(rows)}</div>'


def _render_upload_section(token: str) -> None:
    """Menu upload — real widgets, live "currently on file" preview.

    Deliberately not `st.form` (see the module docstring) so the coverage
    preview updates as soon as meal/year/month change, not only on submit.
    """
    st.markdown('<div class="panel-label">Menu Manifest Loader</div>', unsafe_allow_html=True)

    pending = st.session_state.pop("menu_upload_result", None)
    if pending is not None:
        (st.success if pending[0] == "success" else st.error)(pending[1])

    today = date.today()
    next_month = today.month % 12 + 1
    next_month_year = today.year + (1 if today.month == 12 else 0)

    source_id = st.text_input(
        "Source id", value=_SETTINGS.manual_menu_source_id or "",
        help="the (school, menu) source row's UUID — see scripts/seed.py",
    )
    meal = st.selectbox("Meal", ["lunch", "breakfast"])
    col1, col2 = st.columns(2)
    year = col1.number_input("Year", min_value=2020, max_value=2100, value=next_month_year)
    month = col2.number_input("Month", min_value=1, max_value=12, value=next_month)

    if source_id:
        try:
            coverage = _menu_coverage(
                token, source_id=source_id, meal=meal, year=int(year), month=int(month)
            )
        except requests.RequestException:
            pass  # a broken preview must never block the actual upload below
        else:
            coverage_html = _coverage_html(coverage, meal=meal, year=int(year), month=int(month))
            st.markdown(coverage_html, unsafe_allow_html=True)

    upload_key = st.session_state.setdefault("menu_upload_key", 0)
    uploaded = st.file_uploader("Manifest file (PDF)", type="pdf", key=f"menu_pdf_{upload_key}")

    if st.button("Upload and ingest", disabled=uploaded is None or not source_id):
        assert uploaded is not None  # noqa: S101 - guarded by the disabled= above
        with st.spinner("Uploading and running ingestion…"):
            try:
                report = _upload_menu(
                    token, source_id=source_id, meal=meal, year=int(year), month=int(month),
                    filename=uploaded.name, content=uploaded.getvalue(),
                )
            except requests.RequestException as exc:
                st.error(f"Upload failed: {exc}")
                return
        if report.get("status") == "ok":
            st.session_state["menu_upload_key"] = upload_key + 1
            st.session_state["menu_upload_result"] = (
                "success", f"Ingested — {report.get('upserted', 0)} documents upserted.",
            )
            st.rerun()
        else:
            st.error(f"Ingestion reported an error: {report.get('error')}")


def _coverage_html(coverage: dict[str, Any], *, meal: str, year: int, month: int) -> str:
    covered = set(coverage.get("covered_days", []))
    n = len(covered)
    chip = (
        '<div class="on-file-chip">Currently on file for '
        f"<b>{meal.upper()} · {year}-{month:02d}</b> — {n} document{'' if n == 1 else 's'}</div>"
    )
    days_in_month = int(coverage.get("days_in_month", 0))
    cells = "".join(
        f'<div class="cov-cell {"filled" if d in covered else "off"}">{d}</div>'
        for d in range(1, days_in_month + 1)
    )
    return chip + f'<div class="coverage-grid">{cells}</div>'


def _render_controls_section(token: str, status: dict[str, Any] | None) -> None:
    st.markdown('<div class="panel-label">Controls</div>', unsafe_allow_html=True)
    paused = bool((status or {}).get("paused", False))

    col1, col2 = st.columns(2)
    if col1.button("⏸ Pause", disabled=paused, use_container_width=True):
        try:
            _set_paused(token, paused=True)
            st.rerun()
        except requests.RequestException as exc:
            st.error(f"Failed: {exc}")
    if col2.button("▶ Resume", disabled=not paused, use_container_width=True):
        try:
            _set_paused(token, paused=False)
            st.rerun()
        except requests.RequestException as exc:
            st.error(f"Failed: {exc}")

    st.markdown(
        '<div class="kill-bay"><div class="kill-label">⚠ Kill switch</div>'
        '<div class="kill-note">Sends the live API process SIGTERM. Nothing supervises it '
        "today — a terminal restart is the only way back.</div></div>",
        unsafe_allow_html=True,
    )
    lift = st.checkbox("Lift safety guard")
    if lift:
        confirm = st.text_input(f'Type "{_SHUTDOWN_CONFIRMATION}" to arm it')
        armed = confirm.strip() == _SHUTDOWN_CONFIRMATION
        if st.button("🛑 Shut down now", disabled=not armed):
            try:
                _shutdown(token)
                st.success("Shutdown signal sent.")
            except requests.RequestException as exc:
                st.error(f"Failed: {exc}")


# ---------------------------------------------------------------------------
# Flight Data Recorder — throughput + query-topic charts. Traffic volume and
# response time are real data shapes already in `recent_requests`; query
# topics need `recent_queries` (see `api.middleware.QueryRecord`, populated
# by `api.routers.query`).
# ---------------------------------------------------------------------------


def _count_by(items: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


def _bars_html(data: dict[str, int], *, unit: str = "") -> str:
    if not data:
        return '<div class="chart-sub">Nothing to show yet.</div>'
    items = sorted(data.items(), key=lambda kv: -kv[1])
    max_v = max(v for _, v in items) or 1
    rows = "".join(_bar_item_html(k, v, max_v, unit) for k, v in items)
    return f'<div class="bars">{rows}</div>'


def _bar_item_html(label: str, value: int, max_value: int, unit: str) -> str:
    pct = max(3, round(100 * value / max_value))
    return (
        f'<div class="bar-item" title="{escape(label)}: {value}{unit}">'
        f'<span class="b-label">{escape(label)}</span>'
        f'<span class="b-track"><span class="b-fill" style="width:{pct}%"></span></span>'
        f'<span class="b-val mono">{value}{unit}</span></div>'
    )


def _traffic_volume_html(requests_: list[dict[str, Any]]) -> str:
    if not requests_:
        return '<div class="chart-sub">No requests captured yet.</div>'
    now = datetime.now(UTC)
    buckets: dict[int, int] = {}
    for r in requests_:
        hours_ago = int((now - datetime.fromisoformat(r["at"])).total_seconds() // 3600)
        if 0 <= hours_ago <= 11:
            buckets[hours_ago] = buckets.get(hours_ago, 0) + 1
    data = [(h, buckets.get(h, 0)) for h in range(11, -1, -1)]
    max_v = max((v for _, v in data), default=0) or 1
    peak_hour = max(data, key=lambda hv: hv[1])[0]
    bars = "".join(
        f'<div class="vbar{" peak" if h == peak_hour else ""}" '
        f'style="height:{max(2, round(100 * v / max_v))}%" title="{v} requests, {h}h ago"></div>'
        for h, v in data
    )
    labels = "".join(f"<span>-{h}h</span>" for h, _ in data)
    return f'<div class="vbars">{bars}</div><div class="vbar-labels">{labels}</div>'


def _latency_bars_html(requests_: list[dict[str, Any]]) -> str:
    # /query is excluded here and given its own stat tile — it's an LLM
    # round trip (~20s), not a DB read (~10ms); sharing one linear-axis bar
    # chart would flatten every other route to invisible.
    per_path: dict[str, list[int]] = {}
    for r in requests_:
        if r["path"] == "/query":
            continue
        per_path.setdefault(r["path"], []).append(r["duration_ms"])
    averages = {path: round(sum(ms) / len(ms)) for path, ms in per_path.items()}
    top = dict(sorted(averages.items(), key=lambda kv: -kv[1])[:6])
    return _bars_html(top, unit="ms")


def _pipeline_stat_html(requests_: list[dict[str, Any]]) -> str:
    durations = [r["duration_ms"] for r in requests_ if r["path"] == "/query"]
    if not durations:
        return '<div class="chart-sub">No /query calls captured yet.</div>'
    avg_s = (sum(durations) / len(durations)) / 1000
    return (
        '<div class="pipeline-stat"><div>'
        f'<div class="num mono">{avg_s:.1f}s</div>'
        '<div class="unit">avg /query (reasoning pipeline)</div></div>'
        f'<div class="spark">{_sparkline_svg(list(reversed(durations))[-10:])}</div></div>'
    )


def _sparkline_svg(values: list[int]) -> str:
    if len(values) < 2:
        return ""
    w, h, pad = 160.0, 36.0, 3.0
    max_v, min_v = max(values), min(values)
    rng = (max_v - min_v) or 1
    span = len(values) - 1
    points = [
        (pad + (i / span) * (w - pad * 2), h - pad - ((v - min_v) / rng) * (h - pad * 2))
        for i, v in enumerate(values)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area_line = line.replace(" ", " L ")
    area = f"M {points[0][0]:.1f},{h} L {area_line} L {points[-1][0]:.1f},{h} Z"
    last_x, last_y = points[-1]
    return (
        f'<svg viewBox="0 0 {w:.0f} {h:.0f}" preserveAspectRatio="none">'
        f'<path d="{area}" fill="var(--command)" opacity="0.12"/>'
        f'<polyline points="{line}" fill="none" stroke="var(--command)" stroke-width="1.6"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.6" fill="var(--command)"/>'
        "</svg>"
    )


def _render_flight_data_recorder(status: dict[str, Any] | None) -> None:
    st.markdown('<div class="panel-label">Flight Data Recorder</div>', unsafe_allow_html=True)
    requests_ = (status or {}).get("recent_requests", [])
    queries = (status or {}).get("recent_queries", [])

    col1, col2, col3 = st.columns(3)
    with col1:
        subtitle = '<div class="chart-sub"><b>Traffic</b> — last 12h</div>'
        st.markdown(subtitle, unsafe_allow_html=True)
        st.markdown(_traffic_volume_html(requests_), unsafe_allow_html=True)
    with col2:
        subtitle = '<div class="chart-sub"><b>Response time</b> — fast routes (ms)</div>'
        st.markdown(subtitle, unsafe_allow_html=True)
        st.markdown(_latency_bars_html(requests_), unsafe_allow_html=True)
        st.markdown(_pipeline_stat_html(requests_), unsafe_allow_html=True)
    with col3:
        subtitle = '<div class="chart-sub"><b>Most-queried topics</b> — by intent</div>'
        st.markdown(subtitle, unsafe_allow_html=True)
        st.markdown(_bars_html(_count_by(q["intent"] for q in queries)), unsafe_allow_html=True)
        by_category = '<div class="chart-sub" style="margin-top:12px;">'
        by_category += 'By category, within "document" intent</div>'
        st.markdown(by_category, unsafe_allow_html=True)
        doc_categories = (
            c for q in queries if q["intent"] == "document" for c in q["categories"]
        )
        st.markdown(_bars_html(_count_by(doc_categories)), unsafe_allow_html=True)
        if not queries:
            empty_note = '<div class="chart-sub">No queries captured yet — '
            empty_note += "ask something on the main dashboard.</div>"
            st.markdown(empty_note, unsafe_allow_html=True)


def main() -> None:
    """Render the Pilot Cockpit: admin-token gate → upload / observability / controls / FDR."""
    st.set_page_config(page_title="ACAMAR Pilot Cockpit", page_icon="✈️", layout="wide")
    st.markdown(_CSS, unsafe_allow_html=True)

    token = st.session_state.get("cockpit_token", "")
    with st.sidebar:
        st.subheader("Admin access")
        token = st.text_input("Cockpit token", value=token, type="password")
        st.session_state["cockpit_token"] = token

    if not token:
        if not _SETTINGS.disable_auth:
            st.info("Enter the cockpit admin token in the sidebar.")
            return
        token = "dev"  # noqa: S105 - not a secret, disable_auth means the API ignores it

    status: dict[str, Any] | None = None
    api_error: str | None = None
    try:
        status = _admin_status(token)
    except requests.RequestException as exc:
        api_error = str(exc)

    _render_header(status, token=token, api_reachable=api_error is None)
    if api_error:
        st.error(f"Can't reach the API at {_SETTINGS.api_base_url}: {api_error}")

    _render_upload_section(token)
    st.divider()
    _render_observability_section(status)
    st.divider()
    _render_controls_section(token, status)
    st.divider()
    _render_flight_data_recorder(status)


main()
