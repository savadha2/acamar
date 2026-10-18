# Gap analysis — last 2-3 days of work (2026-09-24/25)

Status: **reference record, not a build plan** — same posture as
[docs/sota.md](sota.md). Produced by a design-only review: read the real
code first (no assumptions from CLAUDE.md's summary), then a multi-agent
pass (4 independent discovery sweeps + 6 findings already confirmed by
direct reading, merged/deduped to 12, each adversarially re-verified
against the live repo, solutions + test cases designed only for what
survived verification). **Nothing in this doc has been built** — no source
file was touched to fix any of these; the only edits from this review were
this doc itself and a citation fix in `sota.md`.

Scope: everything touched 2026-09-24 (the CRAG `RetrievalGradeNode` fix)
and 2026-09-25 (the Pilot Cockpit: `api/routers/admin.py`,
`api/middleware.py`, `dashboard/cockpit_app.py`,
`sources/manual_menu/`).

11 of 12 candidates confirmed; 1 refuted (§"Considered, not confirmed").
Ordered by severity.

---

## Context: what part of CRAG is actually implemented

(Asked and answered mid-review; restated here since several findings below
reference it.) Only **retrieval-grading-before-generation** — canonical
CRAG's (Yan et al. 2024, arXiv:2401.15884) actual mechanism — plus the
pre-existing post-hoc groundedness check, sharing one retry budget:

- `RetrievalGradeNode` (`reasoning/nodes/retrieval_grade.py`, added
  2026-09-24) grades retrieved document **titles only**, binary
  relevant/not-relevant, *before* Synthesis runs.
- `CritiqueNode` (`reasoning/nodes/critique.py`, pre-existing) grades the
  *drafted answer's* groundedness *after* Synthesis — by its own docstring,
  "closer to Self-RAG's reflection tokens than to CRAG proper."
- Both bump the same `state.retry_count`, capped at `MAX_RETRIES=2`
  (`reasoning/state.py:19`). `RetrievalNode` reads that shared counter as
  an attempt index: attempt 0 = Router's own filter; attempt 1 = same
  filter, 2x-widened dates; final attempt (≥2) = all categories, domains
  reset to `None`, widest dates, limit 12.
- **Not implemented**: canonical CRAG's 3-way correct/ambiguous/incorrect
  classification (this is binary), its decompose-then-recompose
  knowledge-refinement step, and any external web-search fallback — the
  only "correction" here is widening the same DB query, never reaching
  outside the local corpus. Deliberate scope decision (`docs/sota.md` §1),
  not an oversight.

---

## High severity

### 1. One bad staged menu PDF permanently breaks ingestion for every meal/month

**Files:** [sources/manual_menu/connector.py](../src/acamar/sources/manual_menu/connector.py), [api/routers/admin.py](../src/acamar/api/routers/admin.py)

**Root cause:** `ManualMenuConnector.fetch()` wraps its *entire* per-meal ×
per-file loop in one `try/except` — any single staged file's parse failure
raises one `ConnectorError` for the whole call, discarding every other
already-parsed file's documents too. This is realistically triggered by
`POST /admin/menu-upload` accepting `year`/`month` as unconstrained `int`s
with no range check before writing the file to disk — a `month=13` upload
stages a file that fails `calendar.monthrange()` on *every future
scheduled ingestion run* of this source until someone finds and manually
deletes it from `data/uploads/menu/`.

**Solution:**
1. `connector.py::fetch()` — move the `parse_menu_pdf(...)` call inside
   the nested loop's own `try/except`; on failure, log (meal + filename)
   and `continue` instead of raising. Narrow the outer `try/except` to
   genuine filesystem-level failures only. Update the docstring's `Raises:`
   note accordingly — this is a deliberate behavior change, so
   `test_a_corrupt_staged_file_raises_connector_error` needs its
   assertion updated alongside it.
2. `admin.py` — change `upload_menu`/`menu_coverage`'s `year`/`month`
   params to `Annotated[int, Query(ge=2000, le=2100)]` /
   `Annotated[int, Query(ge=1, le=12)]`, matching the
   `Annotated[int, Query(ge=,le=)]` convention `dashboard.py`'s `days`
   param already establishes. FastAPI validates before the handler body
   runs, so this fixes the write-before-validate ordering for free (422,
   not a custom 400).

*Follow-on, not folded in:* `sources.last_status` has no error-text
column; the staged-file write isn't atomic (temp+rename); a malformed
filename created outside the admin endpoint is silently skipped.

**Test cases:**
- `test_a_bad_lunch_file_does_not_discard_good_breakfast_documents` (`test_manual_menu_connector.py`) — stage a valid breakfast PDF + a garbage `lunch/2026-13.pdf`; `fetch()` must not raise and must still return all 20 breakfast documents.
- `test_a_corrupt_staged_file_is_skipped_not_raised` (same file) — replaces the old all-or-nothing test; a lone corrupt file → `fetch()` returns `[]`, logs a warning, doesn't raise.
- `test_menu_upload_rejects_an_out_of_range_month` (`test_admin_router.py`) — POST with `month=13` → `422`, no file written to disk.
- `test_menu_coverage_rejects_an_out_of_range_month` (same file) — GET with `month=13` → `422`, not today's unhandled 500.

### 2. Empty `COCKPIT_TOKEN` env value bypasses admin auth entirely

**Files:** [api/auth.py](../src/acamar/api/auth.py), [config.py](../src/acamar/config.py)

**Root cause:** `require_admin` only checks `settings.cockpit_token is
None` (`auth.py:84`). pydantic-settings parses an explicitly-blank
`COCKPIT_TOKEN=` as `""`, not `None` — so a blank-but-set token skips the
loud-fail guard and falls through to
`secrets.compare_digest(authorization[7:].strip(), "")`, which is `True`
for `Authorization: Bearer ` (trailing space, empty content) — full
unauthenticated admin access.

**Solution:** In `auth.py`, change the guard from
`if settings.cockpit_token is None:` to `if not settings.cockpit_token:` —
rejects both unset and blank with the same 401, matching `config.py`'s own
promise ("unset must fail loudly"). No type/default change needed.
Optionally reword that docstring to say "unset *or blank*."

**Test cases:**
- `test_empty_cockpit_token_env_var_is_401_with_blank_bearer_header` (`test_admin_auth.py`) — `COCKPIT_TOKEN=""`, `Authorization: Bearer ` → currently passes silently; must 401.
- `test_empty_cockpit_token_env_var_is_401_regardless_of_header_whitespace_shape` (same) — parametrize over `"Bearer "`, `"Bearer   "`, `"Bearer \t"` → all 401.
- `test_environment_with_empty_cockpit_token_value_is_not_coerced_to_none` (`test_config.py`) — pins the pydantic-settings behavior the bug depends on (`cockpit_token == ""`, not `None`), so a future settings-library upgrade that changed this doesn't silently reopen or hide the bug.

---

## Medium severity

### 3. `/admin/menu-upload` trusts client-supplied Content-Type, no size cap

**File:** [api/routers/admin.py](../src/acamar/api/routers/admin.py)

**Root cause:** `file.content_type != "application/pdf"` is the *only*
check — trivially spoofable — and `body = await file.read()` has no byte
cap, so any request claiming `application/pdf` is fully buffered into
memory and written to disk regardless of actual content or size.

**Solution:** No new dependency.
1. `config.py` — new `cockpit_max_upload_bytes: int = 10_000_000` (10MB), documented like `cockpit_max_recent_requests`.
2. `admin.py` — add `_PDF_MAGIC = b"%PDF-"`; replace the single `file.read()` with a bounded, chunked read (e.g. 1MiB chunks) that raises `413` the moment the running total exceeds the cap, *before* any bytes are written; after a successful bounded read, check the assembled bytes start with `_PDF_MAGIC`, else `400`. Keep the existing content-type check as a cheap pre-check, just not the sole gate.

**Test cases:** in `test_admin_router.py` —
- `test_menu_upload_rejects_bytes_without_the_pdf_magic_number_despite_a_spoofed_content_type` — spoofed content-type, no `%PDF-` prefix → `400`, nothing staged.
- `test_menu_upload_rejects_a_payload_over_the_configured_size_cap` — real PDF prefix, over cap → `413`, nothing staged, ingestion never invoked.
- `test_menu_upload_accepts_a_pdf_prefixed_payload_within_the_size_cap` — regression guard for the existing happy path.
- `test_menu_upload_rejects_the_reported_two_megabyte_junk_payload_repro` — the exact verified repro at scale (~2MB spoofed payload) → must no longer be `200` / written byte-for-byte to disk.

### 4. Zero unit tests for `cockpit_app.py`'s pure logic

**File:** [dashboard/cockpit_app.py](../src/acamar/dashboard/cockpit_app.py)

**Root cause:** Shipped 2026-09-25 with substantial pure, Streamlit-free
logic (recency-angle math, gauge/tile/traffic-tape/sparkline rendering)
and no test file at all — including `_recency_angle`, the literal
mechanism the 2026-09-25 design review's own finding was about (a stale
source shouldn't read identically to a fresh one). Reinforced, not caused,
by `pyproject.toml`'s coverage `omit = ["src/acamar/dashboard/*"]`, which
is a report exclusion with no corresponding no-test policy stated
anywhere.

**Solution:** Add `tests/unit/test_cockpit_app.py` — the module is already
safely importable (proven during verification) despite the unconditional
`main()` call at the bottom. Test the pure helpers directly with plain
dict/list fixtures, no Streamlit harness. Prioritize `_recency_angle` and
`_tile_html`, then round out `_gauge_config`, `_relative_time`,
`_sparkline_svg`, `_coverage_html`, the `_bars_html` family. Add a one-line
note next to the `pyproject.toml` `omit` entry clarifying it's a
coverage-report exclusion, not a testing exemption.

**Test cases:**
- `test_recency_angle_is_zero_when_last_run_at_is_none`
- `test_recency_angle_clamps_at_ninety_degrees_past_three_times_cadence` — the highest-value single test; pins the exact mechanism the design review flagged.
- `test_tile_html_shows_overdue_tag_when_an_ok_sources_angle_exceeds_thirty`
- `test_sparkline_svg_returns_empty_string_for_fewer_than_two_values`

### 5. CRAG shared retry budget: correct today, but unguarded and under-documented

**Files:** [reasoning/graph.py](../src/acamar/reasoning/graph.py), [reasoning/nodes/retrieval.py](../src/acamar/reasoning/nodes/retrieval.py)

**Root cause:** Re-verified by full manual trace: the "exactly 3 total
retrieval attempts no matter which gate fires" guarantee holds today, but
only because `graph.py`'s edges (`retry_count > MAX_RETRIES`) and
`retrieval.py`'s dispatch (`attempt >= MAX_RETRIES`) use compatible
thresholds *on different bases* (a running count vs. a 0-indexed attempt)
— an invariant enforced only by three modules staying in agreement, no
runtime assertion, no comment explaining the operators are deliberately
different, and no test that drives `retry_count` past the cap via *both*
gates in the same run.

**Solution (design-only, no code changed in this pass):**
1. `retrieval.py::RetrievalNode.__call__` — right after `attempt =
   state.retry_count`, add a defensive ceiling: `if attempt > MAX_RETRIES:
   raise RuntimeError(...)  # pragma: no cover - guards graph topology`,
   matching this codebase's existing idiom for exactly this situation
   (`graph.py:45`'s `if state.intent is None: raise ValueError(...)`).
   Not a bare `assert` — `pyproject.toml` only exempts `tests/**` from
   ruff's `S101`.
2. Document the deliberate operator asymmetry in both modules' docstrings
   (`>` vs `>=` on different bases is intentional, not a typo).
3. One-line comment in `critique.py` noting `final_answer` is safely
   written unconditionally only because `critique` is the sole edge to
   `END` on the DOCUMENT branch — a future edge change must preserve that.

**Test cases:**
- `test_stacked_retry_from_both_gates_terminates_in_exactly_three_attempts` (`test_reasoning_graph.py`) — RetrievalGrade fails all 3 times, then Critique also fails once more on the forced-through draft (`retry_count` reaches 4) → still exactly 3 search calls, no crash, `final_answer` still surfaced alongside `grounded=False`.
- `test_retrieval_node_raises_if_invoked_with_an_attempt_past_the_final_widened_one` (`test_retrieval_node.py`) — `retry_count=3` (never produced by the graph today) → the new ceiling raises, no search call.
- `test_retrieval_node_ceiling_check_does_not_false_positive_on_the_legitimate_final_attempt` (same) — `retry_count=2` still works exactly as today.

### 6. No audit trail before pause/resume/shutdown

**Files:** [api/routers/admin.py](../src/acamar/api/routers/admin.py), [api/middleware.py](../src/acamar/api/middleware.py)

**Root cause:** `pause()`/`resume()`/`shutdown()` only mutate
`CockpitState.paused` or call `os.kill` — none log or persist anything —
and the only existing traffic record (`CockpitState._recent`) is an
in-memory deque, explicitly non-durable and cleared on restart. No admin
action, least of all the irreversible shutdown, leaves a record that
survives the action itself.

**Solution:** Fit the codebase's one existing logging idiom
(`ingestion/service.py`'s `logger = logging.getLogger("acamar.ingestion")`).
1. `admin.py` — `logger = logging.getLogger("acamar.admin")`; one
   `logger.info(...)` in `pause()`/`resume()`; in `shutdown()`, log
   *before* `os.kill(...)` (not after — the process may not survive),
   naming the one real identity signal (`"cockpit_token"` — not a fake
   per-user identity, since `require_admin` returns `None` today).
2. Durability needs more than the log call: no logging config exists
   anywhere in `src/acamar/` today, so the line currently goes nowhere. Add
   a `logging.FileHandler` in `app.py`'s `create_app()`, writing to a new
   `cockpit_audit_log_path: str = "data/cockpit_audit.log"` setting.
3. Update `middleware.py`'s "not a durable log" docstring note to point at
   where the durable side now lives.

Explicitly **not** attempting real per-admin attribution here — that
needs an admin-identity model (`require_admin` returning something), a
bigger, separately-proposed change.

**Test cases:** in `test_admin_router.py` —
- `test_shutdown_logs_before_sending_sigterm` — log call happens strictly before `os.kill`.
- `test_pause_and_resume_each_log_one_line`
- `test_admin_actions_do_not_rely_on_the_in_memory_traffic_buffer_for_logging` — log survives even after the traffic deque is cleared.
- `test_shutdown_log_line_names_cockpit_token_not_a_real_admin_identity` — guards against overclaiming attribution.

### 7. A mistagged menu upload can never be retracted — it re-asserts itself forever

**Files:** [ingestion/service.py](../src/acamar/ingestion/service.py), [sources/manual_menu/connector.py](../src/acamar/sources/manual_menu/connector.py)

**Root cause:** `manual_menu`'s dedup key
(`menu::{meal}::{event_date}`) is built from the operator-typed
`year`/`month`, not PDF content — so a mistagged upload's documents are
indistinguishable in shape from correct ones. `IngestionService._should_reconcile`
unconditionally returns `False` for this source (by design, to protect
other legitimate months), so nothing ever soft-deletes wrong-month
documents, and `admin.py` has no endpoint to retire a specific staged
file or month.

**Solution:**
1. `ports/repositories.py` — new `DocumentRepository.soft_delete(source_id,
   external_ids) -> int`, the targeted counterpart to
   `reconcile_deletions` for a source that opted out of automatic
   reconciliation.
2. `adapters/db/document_repository.py` — implement with one parametrized
   `UPDATE ... SET deleted_at = now() WHERE source_id = %s AND external_id
   = ANY(%s) AND deleted_at IS NULL`; extend `FakeDocumentRepository` to
   match.
3. `api/routers/admin.py` — new `POST /admin/menu-upload/retract`
   (`source_id, meal, year, month`, same shape as `menu_coverage`):
   soft-deletes the targeted documents *and* `Path.unlink(missing_ok=True)`s
   the staged file — the file removal is what actually stops
   `ManualMenuConnector.fetch()` from re-globbing and re-asserting the
   wrong-month documents on the next scheduled run, not just a one-time
   DB cleanup.
4. A short note next to `_should_reconcile`'s docstring flagging this
   route as the deliberate substitute for automatic reconciliation, so a
   future reader doesn't "fix" the gap by flipping `config["reconcile"]`
   back to `True` (reintroducing the bug that flag prevents).

**Test cases:** in `test_admin_router.py` —
- `test_menu_retract_soft_deletes_only_the_targeted_months_documents` — retracting October leaves September untouched.
- `test_menu_retract_removes_the_staged_file_so_it_stops_reasserting` — after retract, a fresh `fetch()` no longer returns that month at all.
- `test_menu_retract_without_an_admin_token_is_401` — no side effects on an unauthenticated call.
- `test_menu_retract_is_idempotent_when_the_staged_file_is_already_gone` — missing file doesn't raise; DB rows still get cleaned up.

---

## Low severity

### 8. CRAG's actual scope is scattered across five docstrings, not stated once

Add one paragraph to `docs/sota.md` §1 stating implemented vs. not (see
"Context" above) as the single source of truth; cross-reference it from
`graph.py` instead of re-explaining the contrast in each node's docstring.
Plus one schema-pinning test: `test_retrieval_grade_verdict_stays_binary_relevant_or_not`
(`test_retrieval_grade_node.py`) — asserts `RetrievalGrade`'s fields are
exactly `{relevant: bool, reason}`, so a future silent addition of 3-way
classification is a visible, intentional test update, not silent drift
away from the documented scope. Also add the two "Critique-only retry"
tests noted in finding 5's neighborhood — see the full dump for exact
scenarios if picked up.

### 9. Pause exempts *all* of `/admin/*`, including menu-upload's real ingestion side effects

**Files:** [api/middleware.py](../src/acamar/api/middleware.py), [api/routers/admin.py](../src/acamar/api/routers/admin.py)

`_PAUSE_EXEMPT_PREFIXES` exempting the whole `/admin` prefix is needed so
the cockpit's own control routes stay reachable while paused — but the
same blanket exemption also covers `/admin/menu-upload`, which runs real
DB writes + Ollama embedding calls. Currently exists only as a comment,
absent from `pause()`'s own docstring, and untested. **Solution:**
document the practical implication explicitly in `pause()`'s docstring
and cross-reference it from the middleware comment (no behavior change
recommended — this is plausibly intentional). Only narrow
`_PAUSE_EXEMPT_PREFIXES` to an explicit control-only tuple if the team
later decides the current behavior is actually wrong. **Test cases:**
`test_paused_still_allows_menu_upload_prefix_not_just_status`
(`test_middleware.py`), `test_menu_upload_runs_ingestion_and_stages_the_file_while_paused`
+ `test_pause_blocks_dashboard_but_not_menu_upload` (`test_admin_router.py`)
— assert the asymmetry as an executable test either way.

### 10. `disable_auth`'s docstring doesn't mention it also disarms `cockpit_token`

**File:** [config.py](../src/acamar/config.py)

Written when `/query`/`/dashboard` were the only gated surface; never
revisited when `cockpit_token` was added as a second, higher-privilege
gate. **Solution:** extend `disable_auth`'s docstring to name the admin
surface explicitly (pause/resume/shutdown/menu-upload) and extend
`cockpit_token`'s "unset must fail loudly" promise to note that promise
only holds while `disable_auth` is false. Prose-only. **Test cases** (via
`inspect.getsource(Settings)`, since this is text, not behavior):
`test_disable_auth_docstring_calls_out_the_admin_cockpit_surface`,
`test_disable_auth_docstring_still_lists_query_and_dashboard_routes`,
`test_cockpit_token_docstring_cross_references_disable_auth` (all
`test_config.py`).

### 11. Grab-bag: five smaller untested branches, one with a wrong error message

- **`admin.py::admin_status`** — only ever tested with an empty
  `CockpitState`; add a test seeding real `RequestRecord`/`QueryRecord`
  values and asserting every response field maps correctly, not just
  non-emptiness.
- **`middleware.py::CockpitState.record_query`/`recent_queries`** — the
  ring-buffer-cap and most-recent-first tests exist for `.record()` but
  not `.record_query()`; add the analogues directly (no HTTP path
  triggers this one).
- **`critique.py::CritiqueNode`** — `RetrievalGradeNode` has prompt-content
  tests (context appears in prompt; `_NO_CONTEXT` fallback on empty
  retrieval); `CritiqueNode` never got the same two tests despite the
  identical pattern. New `test_critique_node.py`.
- **`auth.py::require_subscriber`/`require_admin`** — "wrong scheme"
  (`Authorization: Basic ...`) is never tested distinctly from "header
  missing" / "right scheme, wrong value." One test per file.
- **`parser.py`** — two edge cases: a zero-page (but structurally valid)
  PDF hits a different branch (`"no pages"`) than the existing blank-PDF
  test (`"no Monday..Friday table"`) and has no test; and a bare
  digit-only cell (`"5"`, no trailing text) raises `ValueError` with a
  message that's **factually wrong** ("doesn't start with a day number" —
  it does) — flagged as a small, separate follow-up code fix outside this
  design-only pass (either widen `_CELL_PATTERN` to skip such a cell like
  blank/"No School," or at minimum fix the message).

---

## Considered, not confirmed

**`_widen(date_from, date_to)` returning the window unchanged when both
bounds are `None`** (`retrieval.py`) — raised as "a CRAG retry doesn't
actually widen anything for a legitimate no-date-bound question." Refuted
on verification: `DocumentRepository.search()`/`count()` already treat
`None` as "unbounded" (`date_from or _DATE_MIN`, `date_to or _DATE_MAX`),
so `(None, None)` is already the widest possible window — there's nothing
narrower being left un-widened. **Real, untested lead this surfaced
instead**: there's no validator anywhere enforcing `date_from`/`date_to`
are set *together* — an asymmetric case (exactly one bound `None`, the
other a concrete date) is theoretically reachable from Router output and
*would* leave the concrete bound un-widened on retry. Not shown reachable
by any existing test or doc evidence, so not confirmed as a live bug — but
worth a test if this is ever revisited.

---

## Not in scope for this pass

- A real admin-identity model for `require_admin` (today returns `None`)
  — needed for genuine per-admin attribution in finding 6's audit trail;
  bigger change, separate proposal.
- Narrowing `_PAUSE_EXEMPT_PREFIXES` to control-only paths (finding 9) —
  offered as an alternative, not a recommendation; current behavior is
  plausibly intentional.
