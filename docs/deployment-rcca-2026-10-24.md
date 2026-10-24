# Cloud deployment RCCA — 2026-10-24

Handoff doc. Each issue below is root-caused and verified against the real
live deployment, not guessed — fixes are specified precisely enough to
execute without further investigation. None of these fixes have been
applied yet; this doc is the spec, not the change.

---

## Issue 1 — Cockpit showed "Can't reach the API... 401 Unauthorized for /admin/status"

**Status: RESOLVED on its own — not a bug, no fix needed.**

**What was seen**: a screenshot of the live Cockpit showing a red "API Link"
annunciator and the error `Can't reach the API at
https://api-rg24e3hmpa-uc.a.run.app: 401 Client Error: Unauthorized for
url: .../admin/status`.

**Root cause**: the screenshot was taken while GitHub Actions run
`40065701975` (deploying commit `a8005fc`, which sets `DISABLE_AUTH=true`
on the `api` service) was still `in_progress`. The `api` service's
*configured* env vars already showed `DISABLE_AUTH=true` at the time (a
new revision had been created), but that revision either wasn't yet
serving 100% of traffic or wasn't fully ready — a timing/rollout artifact,
not a code defect.

**Verification performed**: re-ran `curl .../admin/status` and `curl
.../dashboard` with **no** `Authorization` header at all, after run
`40065701975` reached `completed`/`success`. Both now return `200` with
real data. No further action needed. (If this recurs *after* confirming
the relevant CI run is fully green, that would be a real regression —
re-open as a new issue, don't assume it's this same timing artifact again.)

---

## Issue 2 — Dashboard/Cockpit render on a jarring black background with mismatched light-themed cards

**Status: OPEN, root-caused, not fixed.**

**What was seen**: a screenshot of the live `dashboard` service — pure
black page background, with the weather card, search bar, and event cards
rendered in their intended light/cream colors, floating awkwardly on the
black void. User confirmed this does **not** match how the same app looks
when run locally.

**Root cause**: `.streamlit/config.toml` pins the theme explicitly
(`base = "dark"`, committed 2026-09-24, a deliberate choice — see the
file's own comment). It **is** correctly tracked in git (confirmed via
`git ls-files`). But `.dockerignore` line 17 excludes `.streamlit/` from
the Docker build context:

```
# Local-only state, never needed inside a container.
...
.streamlit/
```

This characterization is wrong for this specific file — `config.toml` is
real, versioned, deliberate app configuration (not local/machine-specific
state), but it's bucketed under "local-only state" and never gets copied
into the image. Confirmed the directory holds nothing else (`ls -la
.streamlit/` → only `config.toml`, no `secrets.toml` or anything
machine-specific that *should* stay excluded).

Because the file is missing entirely inside the container, Streamlit
falls back to its own default theme resolution instead of the pinned
`dark` setting — producing a different, inconsistent look from local,
where the file is present on disk.

This affects **both** `dashboard` and `cockpit` (both Streamlit apps built
from the same Docker build context) — not just the one screenshotted.

**Fix** (`.dockerignore`):

```diff
 .venv/
 .git/
 .mypy_cache/
 .pytest_cache/
 .ruff_cache/
 logs/
 data/
-.streamlit/
 .DS_Store
```

Just remove that one line. No other change needed — `Dockerfile` already
does a broad `COPY src/ src/` etc. for the app, but `.streamlit/` lives at
the **repo root** (sibling to `src/`), so also confirm the Dockerfile
actually copies the repo root's `.streamlit/` directory into the image.
Check the `base` stage's `COPY` lines (currently `COPY src/ src/`,
`COPY scripts/ scripts/`, `COPY db/ db/` — `.streamlit/` is **not** in
that list) and add:

```diff
 COPY src/ src/
 COPY scripts/ scripts/
 COPY db/ db/
+COPY .streamlit/ .streamlit/
```

Streamlit looks for `.streamlit/config.toml` relative to the **current
working directory** at process start, not relative to the script path —
confirm the `dashboard`/`cockpit` stages' `CMD` runs with `/app` as the
working directory (it does — `WORKDIR /app` is set once in the shared
`base` stage and never changed), so `COPY .streamlit/ .streamlit/` lands
it at `/app/.streamlit/config.toml`, exactly where Streamlit will look.

**Verification after fix**: rebuild the `dashboard` target locally
(`docker build --target dashboard -t acamar-dashboard-test .`), run it,
and confirm `/app/.streamlit/config.toml` exists inside the running
container (`docker exec <container> cat /app/.streamlit/config.toml`)
before pushing to CI. After deploying for real, load both the `dashboard`
and `cockpit` URLs and confirm the dark theme renders consistently (no
light-card-on-black-void mismatch).

---

## Issue 3 — Cockpit page shows two raw blocks of comment-like text at the top

**Status: OPEN, pre-existing (not caused by this deployment), already
logged as backlog item 47 — re-surfaced here because it's visible in the
same screenshot as Issue 1 and worth fixing in the same pass.**

**What was seen**: text like `A source is considered fully "overdue"
(needle fully drooped) at 3x this — matches the ingest cron's current
6x/day (~4h) schedule...` and `Shared by every "is this source stale"
check in this module...` rendering as plain page content above the Pilot
Cockpit panel, not as a code comment or tooltip.

**Root cause** (already confirmed via `ast.parse` against the real file,
not re-derived here): Streamlit's "magic" mode automatically calls
`st.write()` on any bare top-level string expression in the script. Two
module-level constants in `cockpit_app.py` have a triple-quoted string
placed immediately after their assignment, as an attribute-docstring-style
comment — valid Python, but Streamlit renders each one as page content:

- `cockpit_app.py:48-53` — `_EXPECTED_CADENCE_HOURS`'s explanation.
- `cockpit_app.py:489-492` — `_STALE_ANGLE_THRESHOLD`'s explanation.

`streamlit_app.py` does **not** have this problem (its only bare top-level
string is the real module docstring at line 1 — confirmed by the same AST
check), so this fix is scoped to `cockpit_app.py` only.

**Fix**: convert both triple-quoted strings to `#`-prefixed comment
blocks. No behavior change to the constants themselves — e.g.:

```diff
 _EXPECTED_CADENCE_HOURS = 4.0
-"""A source is considered fully "overdue" (needle fully drooped) at 3x this
-— matches the ingest cron's current 6x/day (~4h) schedule
-(`scripts/com.acamar.ingest-faria-events.plist`); not stored anywhere
-queryable, so this is a display-only estimate, not a per-source contract.
-Default for any `domain` not in `_EXPECTED_CADENCE_HOURS_BY_DOMAIN` below.
-"""
+# A source is considered fully "overdue" (needle fully drooped) at 3x this
+# — matches the ingest cron's current 6x/day (~4h) schedule
+# (`scripts/com.acamar.ingest-faria-events.plist`); not stored anywhere
+# queryable, so this is a display-only estimate, not a per-source contract.
+# Default for any `domain` not in `_EXPECTED_CADENCE_HOURS_BY_DOMAIN` below.
```

(Same mechanical transform for `_STALE_ANGLE_THRESHOLD`'s docstring at
line 489 — read the exact current text from the file before editing, don't
retype from this doc, in case it has drifted since 2026-10-10.)

**Also requested (logged on item 47, not yet built): a regression test for
this bug *class***, not just these two instances. Proposed shape (already
verified to work during diagnosis of this issue):

```python
import ast
from pathlib import Path

def test_no_leaked_module_level_docstrings():
    """Streamlit's magic mode st.write()s any bare top-level string —
    the only one that should exist is the real module docstring (body[0])."""
    for path in [
        Path("src/acamar/dashboard/streamlit_app.py"),
        Path("src/acamar/dashboard/cockpit_app.py"),
    ]:
        tree = ast.parse(path.read_text())
        for i, node in enumerate(tree.body):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                assert i == 0, f"{path}:{node.lineno} — leaked pseudo-docstring, not the module docstring"
```

Scope note from the original proposal: this deliberately only catches
bare top-level *string* expressions (the actual risk here), not every
possible magic-mode leak — don't over-build it.

**Verification after fix**: run the new test; load the cockpit page and
confirm no stray text block appears above the main panel.

---

## Issue 4 — Cockpit's "API Link" annunciator doesn't mean what it looks like it means

**Status: OPEN, pre-existing, already logged as backlog item 45 —
unrelated to today's deploy, but worth fixing in the same pass since it's
directly adjacent to Issue 1's confusion (a real outage and an auth hiccup
would look identical on this light).**

**Root cause**: `cockpit_app.py:692` sets the "API Link" light based on
whether the `/admin/status` call succeeded:

```python
+ _light_html("API Link", "ok" if api_reachable else "danger")
```

`/admin/status` requires the admin credential (`CurrentAdmin` /
`cockpit_token`) to succeed — so this light actually measures "is my admin
token valid and accepted," not "is the API process up." A correctly-issued
expired/wrong cockpit token and a genuinely-down API produce the exact
same red light, with no way to tell them apart from the UI alone (this is
exactly what caused the confusion in Issue 1 while the CI deploy was still
rolling out).

**Fix**: check the real `/health` endpoint (no auth required at all, see
`api/routers/health.py` or equivalent) for actual liveness, and keep the
`/admin/status` check as a separate, distinctly-labeled signal (e.g. an
"Admin Auth" light) rather than conflating the two under one "API Link"
label. Concretely:

1. Find where `api_reachable` is computed (search `cockpit_app.py` for the
   variable definition, likely near where `/admin/status` is called at
   line ~369).
2. Add a separate call to `GET {api_base_url}/health` (no auth header
   needed) to determine real liveness.
3. Rename/split the single "API Link" light into two: one driven by the
   `/health` result (real liveness), one by the `/admin/status` result
   (admin-auth validity) — or at minimum, change "API Link"'s condition to
   use the `/health` result, and surface the admin-auth failure as its own
   distinct error message (it already is one, via the "Can't reach the
   API..." error text — just stop tying the light itself to that same
   check).

**Verification after fix**: with a valid cockpit token and a healthy API,
both lights should be green. With a *wrong* cockpit token and a healthy
API, "API Link"/liveness should stay green while the admin-specific
signal goes red — proving the two are no longer conflated.
