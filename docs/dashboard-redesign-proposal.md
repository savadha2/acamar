# Dashboard redesign — architecture review + proposal (2026-09-15)

Status: **proposal, not implemented.** Written after today's Gmail-events/
reminders work landed and produced a working-but-rough dashboard. Covers the
review of today's build, the four pieces of feedback that prompted this doc,
and a concrete plan for next session. Nothing here is built yet.

---

## 1. What shipped today (review)

| Piece | State |
|---|---|
| `ports/mail_client.py`, `adapters/gmail/client.py` (`GoogleGmailClient`) | Built, OAuth working end-to-end |
| `sources/gmail_school/{connector,parser}.py` | Built — best-effort, subject-only date extraction, real data validated |
| `sources/gmail_menu/*` | Still parked (unchanged today) |
| `documents.summary` + `ports/summarizer.py` + `OllamaSummarizer` | Built, wired into `IngestionService`, backfilled |
| Daily launchd job (`run_daily_ingestion.sh`) | Built, validated live, covers both sources |

**Working, but three real weaknesses surfaced under actual use** (this is
the "review" the user asked for — these are what items 2–4 below fix):

1. **Sequential, per-document LLM calls, no fault isolation.** `IngestionService._summarize`
   is a plain `for` loop — 32 calls today took **26 minutes**, and a single
   failing call (as happened once, wrong model name) aborts the *entire*
   ingestion run, not just that one document. This gets worse, not better,
   as more Gmail sources are added.
2. **No relevance signal.** Every fetched email becomes a dashboard card,
   including pure noise (a `9/9 Wed Test` email the user sent themselves, a
   newsletter that's 95% not-actionable). Nothing distinguishes "this needs
   your attention" from "this technically matched the label."
3. **`title` is the raw email subject verbatim**, which is often clickbait-y
   ParentSquare phrasing ("TOMORROW is the Fall Fest, 5-9pm; Sign Waiver,
   Join the Fun!") rather than something scannable.

None of this was wrong to ship today — it's what "search the real inbox,
see what's actually there" was for. It's the input to this proposal.

---

## 2. The four requests, mapped to concrete changes

### 2.1 "Events should also be cards"

**Current state:** Menu and Events already render via the same `_card()`
component in `streamlit_app.py`. **Reminders does not** — it renders each
item as a standalone `st.warning()` banner (`_render()`, lines 45–51),
visually inconsistent with the other two sections.

**Proposed fix:** drop the Reminders special-case; render all three
sections through one shared card component. Keep a visual distinction (e.g.
a colored left-border or a small icon) rather than a structurally different
widget. This is pure UI, no schema/backend change — cheapest item here.

### 2.2 Dates first, LLM-inferred headline instead of verbatim subject

Two changes, both needed:

- **Card layout:** date prefix, then title — currently title-first
  (`_card()` line 38: `f"**{item['title']}**{when}"`). Trivial reorder.
- **Headline generation:** a new LLM-produced field, distinct from the
  existing `summary`. `summary` (1–2 sentences) stays as the card *body*;
  a new **`headline`** (≤ ~8 words, imperative/assistant framing — "Fall
  Festival tonight, 5–9pm" not "TOMORROW is the Fall Fest...!") becomes the
  card *title*. Raw `title` (the actual subject) is kept on the row
  regardless — useful for debugging/search, just not what's displayed.

  **Recommendation: generate `headline` in the same LLM call as `summary`**,
  not a second call — see §3.1. Doubling the already-slow per-document call
  count is the wrong direction.

### 2.3 Filter low-relevance items; extract holidays even from noisy ones

Two distinct mechanisms, easy to conflate:

- **Relevance filtering** — a boolean (`is_relevant`) computed once at
  ingestion, defaulting `True`. `DashboardService` excludes `is_relevant =
  False` rows from cards. **Retrieval (`DocumentRepository.search`, the
  future `/query`) must ignore this flag** — a "not dashboard-worthy" email
  can still be a correct answer to "did anyone mention pizza day," so it
  stays in the searchable corpus. Only the glance-view dashboard filters.
- **Holiday extraction** — a *different* mechanism from filtering. A
  newsletter can be `is_relevant = False` as a whole and still contain "no
  school Sept 25th" buried in paragraph three. The enrichment step should be
  able to emit **an additional, synthetic document** (not just a flag) when
  it finds one — same pattern already used for the menu's multi-day
  compositing: `parse_school_email` already returns `list[RawDocument]`, so
  "sometimes 2 documents from 1 email" is not a new capability, just a new
  caller of an existing shape.

  Proposed: add `Category.HOLIDAY` to the enum (distinct from generic
  `REMINDER` — "what school holidays are coming up" is a real, specific
  question worth its own category). Synthetic holiday doc gets
  `external_id = f"{parent_external_id}::holiday"` (same `::`-compositing
  convention as the menu design already established) and `event_date` set
  from the extracted date.

  **This is the riskiest piece** — LLM-extracted dates from free prose can
  hallucinate or misparse. See test cases in §4; recommend this ships last,
  behind its own small golden-set check, not trusted on day one the way
  §2.1–2.2 can be.

### 2.4 Column-wise redesign by organization (CUSD / FEEF / classroom, etc.)

No existing field supports this — `sender` isn't even persisted onto
`RawDocument`/`Document` today (it's read off `RawEmail` during parsing and
then dropped). Two sub-decisions:

1. **New `organization` field**, populated by the same enrichment call,
   classified into a **small closed taxonomy** — not open-ended labels
   (open labeling → taxonomy sprawl, unfilterable). Strawman, needs your
   sign-off before building: `district` (CUSD board/superintendent),
   `feef` (fundraising), `classroom` (teacher/homework/projects), `faria_admin`
   (office/front-desk), `other`. Closed-set classification is also just
   easier to unit-test (§4) than free text.
2. **UI grouping**: Streamlit columns (`st.columns`) or expandable sections
   per organization, replacing the current fixed 3-card vertical stack.
   Menu/Events (Faria-scraped, no sender) fall into their own
   pseudo-group ("School Calendar") since they don't come from email.

---

## 3. Architecture changes this implies

### 3.1 Replace `Summarizer` with one structured `Enricher` call

Today's `ports/summarizer.py` (`summarize(text) -> str`) gets **superseded**,
not extended — bolting `headline`/`is_relevant`/`organization`/`holiday`
onto it as separate calls would push per-document LLM calls from 1 to 4–5,
making the existing 26-minute runtime much worse. Proposed replacement:

```python
class HolidayExtraction(BaseModel):
    label: str          # "Staff Learning Day", "Thanksgiving Break"
    event_date: date

class DocumentEnrichment(BaseModel):
    headline: str        # card title
    summary: str         # card body (today's behavior, unchanged shape)
    is_relevant: bool
    organization: OrganizationTag   # closed enum, §2.4
    holiday: HolidayExtraction | None

class Enricher(Protocol):
    async def enrich(self, doc: RawDocument) -> DocumentEnrichment: ...
```

One Ollama call per document (JSON/structured-output mode — `gemma3:1b`
supports this), not five. This is also the natural occasion to build
backlog item #14 (`ChatModel` port) properly rather than leave two
overlapping ad hoc LLM ports (`Summarizer` and a hypothetical new one).

### 3.2 Fault isolation, per document

`IngestionService._summarize` today is:

```python
return [doc.model_copy(update={"summary": await self._summarizer.summarize(doc.content)}) for doc in raw]
```

— one exception anywhere aborts the whole batch (this actually happened
today, harmlessly, from a bad model name). Proposed: catch per-document,
degrade gracefully (`headline = doc.title`, `is_relevant = True` — **fail
open**, never silently hide real content because the model hiccuped on one
item), log a warning, keep going. A single bad document should cost that
document its polish, not the whole run.

### 3.3 Cut redundant LLM calls (the 26-minute problem)

Two independent, additive fixes:

- **Change-detection**: skip re-enrichment when `content` is unchanged from
  the stored row. Faria re-scrapes its whole month every run; Gmail's
  unbounded top-25 query re-fetches mostly-the-same messages daily (see
  backlog note on why it's *not* date-windowed). Right now every one of
  those gets re-enriched for nothing. Needs the repository to expose
  "existing content for this `(source_id, external_id)`" before the
  enrichment step runs, or a content hash comparison inside `upsert`.
- **Bounded concurrency**: `asyncio.gather` with a small semaphore (3–5)
  instead of one-at-a-time. Unverified whether local Ollama actually
  parallelizes on this hardware — **benchmark before committing to this**,
  don't assume it helps.

### 3.4 Schema changes

```sql
ALTER TABLE documents ADD COLUMN headline      TEXT;
ALTER TABLE documents ADD COLUMN is_relevant   BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE documents ADD COLUMN organization  TEXT;
```

Plus `Category.HOLIDAY` in `domain/enums.py`. All additive/nullable-or-defaulted
— safe for existing rows (which get `is_relevant = true`, `headline/organization
= NULL` and fall back to today's behavior until re-ingested).

### 3.5 Dashboard/API/UI shape

- `DashboardService.today()`: filter `is_relevant`, group by `organization`.
  `Dashboard` model likely changes from 3 flat lists to a grouped shape —
  exact shape is a next-session design call, not pre-decided here.
- `streamlit_app.py`: one shared card renderer, date-first, `st.columns`
  (or expanders) per organization group.

---

## 4. Test cases to add (next session)

**Enricher / prompt contract** (fake the port, like `FakeSummarizer` today):
- `is_relevant` defaults `True` when the model call fails (fail-open, not fail-hidden).
- `organization` falls back to `other` for unrecognized content, never raises.
- Holiday extraction: body text with an unambiguous "no school \<date\>" phrase
  → `HolidayExtraction` with the correct date; body with no such phrase → `None`.
- **Do not** assert exact LLM prose in unit tests (flaky by nature) — assert
  the *shape*/fallback behavior with fakes; track extraction quality
  separately via a small golden-set eval (mirrors backlog #20's planned
  LLM-as-judge harness — same pattern, smaller scope).

**`IngestionService`**:
- One failing `enrich()` call among N documents still upserts the other
  N-1 with real enrichment (fault isolation, §3.2) — regression test against
  today's actual failure mode (whole run aborted on one bad call).
- A document whose `content` is unchanged from the stored row does not
  trigger a new `enrich()` call (change-detection, §3.3) — assert on the
  fake's call count.
- A holiday extracted from document A produces a second, distinct upserted
  `RawDocument` with `external_id = f"{A.external_id}::holiday"` and
  `category = HOLIDAY`.

**`DashboardService`**:
- `is_relevant = False` documents are excluded from `today()`'s output.
- A `HOLIDAY`-category document with a future `event_date` is NOT excluded
  by relevance filtering even if its *parent* email was irrelevant (the two
  mechanisms in §2.3 are independent — test that explicitly, it's the
  easiest place to accidentally couple them).
- Grouping: documents route to the right `organization` bucket; unset
  `organization` (pre-migration rows) falls into a defined default group,
  not dropped.

**Reconciliation regression** (existing safety property, must survive this
change unchanged):
- A `HOLIDAY`-category document with `event_date >= today` is protected
  from `reconcile_deletions` the same way `EVENT` documents are today —
  the rule is about `event_date`, not category, but worth a named test
  since a new category is exactly the kind of change that could silently
  break an implicit assumption.

**UI** (data-shape level, not pixel-level — this project has no Streamlit
UI tests today and `dashboard.*` is explicitly exempted from strict mypy;
recommend keeping visual verification manual, but test the *data* the UI
consumes):
- API response includes `headline`/`organization`/`is_relevant` fields with
  sensible values for a fully-enriched row and for a pre-migration
  (`NULL`-enrichment) row.

---

## 5. Suggested sequencing

Roughly cheapest/safest → riskiest, so value lands incrementally rather
than one big change:

1. **Reminders-as-cards + date-first** (§2.1, §2.2 layout half) — pure UI,
   no schema/LLM change, ships in an hour.
2. **`Enricher` replacing `Summarizer`** + fault isolation (§3.1, §3.2) —
   the foundational piece everything else sits on; also fixes today's
   demonstrated single-point-of-failure.
3. **`headline` + `is_relevant` + dashboard filtering** (§2.2 content half,
   §2.3 filtering half) — highest immediate value (this is the actual "too
   much information" complaint from earlier today).
4. **`organization` + column redesign** (§2.4) — needs your sign-off on the
   taxonomy first (strawman in §2.4).
5. **Holiday extraction** (§2.3 second half) — last, smallest blast radius
   if the extraction quality turns out to need more prompt iteration than
   expected.

Also worth doing alongside step 2 since it's the same code path:
**change-detection** (§3.3) — otherwise every future ingestion run stays
slow for content that never changed.
