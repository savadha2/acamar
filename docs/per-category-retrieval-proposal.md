# P1.3 design proposal: per-category retrieval + RRF merge

**Status: proposal for review, not implemented.** Per this project's
standing convention that design changes get proposed and reviewed before
anything is built — nothing here gets built until the open questions in
§7 are resolved.
Produced by reading the real RAG-Fusion paper directly (not from memory —
see sourcing note below) and re-deriving the current retrieval code from
the actual files, then adversarially checked against both; three real
inaccuracies from the first draft are corrected below rather than
glossed over (§3 items 2/4, §6's missing test breakage, §1's inverted
example).

**Sourcing.** The paper — Rackauckas (2024), "RAG-Fusion: A New Take on
Retrieval-Augmented Generation," [arXiv:2402.03367](https://arxiv.org/abs/2402.03367)
— was fetched and read directly via arXiv's HTML rendering (the PDF
didn't extract cleanly), cross-checked against two independent fetches
plus the abstract page and the reference implementation
([github.com/Raudaschl/rag-fusion](https://github.com/Raudaschl/rag-fusion)).
It's an Infineon industry case study (a product-support chatbot), not a
controlled IR benchmark — evaluation throughout is manual/qualitative
(accuracy/relevance/comprehensiveness judged on real chatbot
transcripts), no recall@k/nDCG anywhere.

## 1. Problem this solves

`RetrievalNode.__call__` ([retrieval.py:58-88](../src/acamar/reasoning/nodes/retrieval.py))
issues exactly one `DocumentRepository.search()` call per attempt,
passing `state.categories` straight into `_SEARCH`
([document_repository.py:64-74](../src/acamar/adapters/db/document_repository.py)):
one `category = ANY(%s)` predicate, one `ORDER BY embedding <=> %s`, one
shared `LIMIT %s`. Router routinely emits 2+ categories — its prompt says
so explicitly ("A broad question... should include several categories,
not just one," [router.py:99-100](../src/acamar/reasoning/nodes/router.py)).

This is **Decision C's pooling-imbalance limitation**, logged verbatim at
[reasoning-pipeline-design.md:535-544](../docs/reasoning-pipeline-design.md):

> a single top-K query across a union of categories isn't balanced — if
> the corpus has more event-shaped documents near a question's embedding
> than reminder-shaped ones, the top-8 could skew almost entirely to
> events even when a reminder was equally relevant, silently
> under-representing it rather than excluding it outright... Tracked in
> the backlog memory, not fixed here — revisit if a real "it missed an
> obviously-relevant reminder" case shows up as the corpus grows.

**Important correction, caught on adversarial review of the first draft:**
the design doc's *own* logged "real failure" example (lines 838-846,
the Social Studies Project incident) is **not an instance of this bug**,
and this proposal does not fix it. That incident is `search(categories=
[reminder, event])` finding the document while `search(categories=
[reminder])` alone misses it entirely — Router excluding the right
category from the array outright, a **category-selection/exclusion**
bug. RRF fan-out only rebalances fairness *among categories Router
already included*; there is no sub-search to fan out into for a category
that was never in `state.categories` to begin with. So: **as of this
writing, Decision C's pooling-imbalance failure (crowding-out within an
included set) has no confirmed real-world instance logged** — it remains
exactly what the design doc called it, a theoretical risk flagged for
revisit "if a real case shows up," not yet observed. This proposal is
worth doing on that basis (closing a known, reasoned gap before it bites,
same as `RetrievalGradeNode` was built ahead of most failures it now
catches) — but it should not be sold as fixing the one incident already
on record, which needs a different fix (Router category-selection
reliability, already tracked separately as an open follow-up in that
same design-doc passage).

## 2. What's adapted from RAG-Fusion, and what deliberately isn't

`docs/sota.md` §2 already scopes the borrow correctly: reuse **only the
RRF merge step**. The paper's actual mechanism, quoted from its "RAG vs
RAG-Fusion" section: the model generates "a number of new search queries
based on the original query" (no fixed count — the paper's own worked
example uses 4; the reference repo hardcodes 4+original = 5), each
searched independently, then "the rrf score is accumulated with previous
scores for the same document, and when all scores are accumulated,
documents are fused together and reranked" — formula `1/(rank + k)`,
summed per document across every list it appears in.

That fan-out exists to solve **query ambiguity** — per the abstract, the
generated queries work by "contextualizing the original query from
various perspectives" (a domain-specific elaboration in the Account
Managers section adds "generating multiple queries is key to devising a
comprehensive and multiperspective sales strategy," but that's a
sales-specific claim, not the paper's general thesis). **ACAMAR has no
query ambiguity left to resolve at this point in the pipeline** — Router
has already picked the category set; the bug is purely mechanical
pooling over an already-known, finite partition. So this design takes
only the fusion mechanism, not the multi-query-generation step, and that
omission is deliberate: adding query generation would import the paper's
own logged costs (a second LLM call — the paper measured RAG-Fusion at
34.62s vs. RAG's 19.52s, ~1.77×, attributed to that second call; and
off-topic answers "when irrelevant queries are generated by the large
language model") to solve a problem this codebase doesn't have.

## 3. Deviations from the canonical paper — stated plainly

1. **Fan-out axis differs.** Paper: one sub-search per LLM-generated
   paraphrase of a single query. This design: one sub-search per
   **already-routed category**, same embedding, same question. Related
   problems (imbalanced pooling over a known partition vs. limited
   semantic reach of one phrasing), not the same one.
2. **No query-generation LLM call added** — the paper's stage 1 doesn't
   exist here.
3. **RRF's celebrated benefit can't transfer.** The paper's mechanism
   rewards a document whose score "is accumulated with previous scores
   for the same document" across lists — cross-list corroboration. In
   ACAMAR, `documents.category` is single-valued by schema, so
   per-category result lists are **disjoint by construction**: no
   document can ever appear in two lists. RRF here can't reward
   corroboration; it can only act as a rank-based, scale-free normalizer
   that gives each category's own top item a comparable score. Real,
   load-bearing, not a footnote.
4. **`k` is unvalidated for either setting, not just this one.** The
   paper never states a numeric `k` anywhere; its evaluation is entirely
   manual/qualitative with no k-sensitivity analysis. The reference
   repo's `k=60` default is inherited from Cormack et al.'s general
   meta-search RRF work, not tested by Rackauckas's paper at all. `k=60`
   is not "validated for paraphrase-corroboration but not for ACAMAR's
   case" — it has no evidentiary backing from this source in *either*
   setting. Proposing it here is a starting point to validate against
   real results, not a known-good value.
5. **The paper never evaluated filtered/categorical retrieval, uneven
   list lengths, or k-sensitivity at all** — nothing from the source
   material to lean on; this needs its own validation against ACAMAR's
   golden set after building, not an assumption of correctness.

## 4. Proposed mechanics

At each existing attempt (0 / 1 / final — the widening table in
[retrieval.py:6-11](../src/acamar/reasoning/nodes/retrieval.py) stays
exactly as documented; this change is orthogonal to it), replace the
single `search()` call ([retrieval.py:80-87](../src/acamar/reasoning/nodes/retrieval.py))
with **one call per category in that attempt's resolved `categories`
list**, same `domains`/`date_from`/`date_to`, gathered concurrently
(`asyncio.gather`). **No change to `DocumentRepository.search()`'s
signature** — `categories: list[Category]`
([document_repository.py:171](../src/acamar/adapters/db/document_repository.py))
already accepts a one-element list; the SQL is correct for it as-is.

Merge with a small pure function (same style as the existing `_widen()`
at [retrieval.py:44-48](../src/acamar/reasoning/nodes/retrieval.py)):
rank each returned list by its own distance order (position 0 =
closest), score `1/(k + rank)` per list, sum across lists per document
id (in practice a no-op sum since lists are disjoint — see §3.3), sort
descending.

Two limit knobs need explicit values, currently conflated into one
`limit`:
- **(a)** per-category sub-search limit — proposed equal to today's
  per-attempt cap, so each category gets its own full fair top-K rather
  than a pre-split shared budget.
- **(b)** post-fusion cap actually handed to `state.retrieved` —
  proposed unchanged: `_ATTEMPT_LIMIT=8` (attempts 0/1), `_FINAL_LIMIT=12`
  (final), preserving `RetrievalContext`'s existing char-budget
  assumptions untouched.

**Degenerate case:** a single-category attempt (`len(categories) == 1`)
fans out to exactly one sub-search, and RRF-over-one-list preserves
distance order — a no-op vs. today's behavior. Good regression-safety
property to verify once built: this only changes behavior for the
2+-category case it exists to fix.

## 5. Interaction with the retry loop, retry_count, and RetrievalContext

No interaction with `retry_count` or the graph's routing —
[retrieval_grade.py:62-71](../src/acamar/reasoning/nodes/retrieval_grade.py)
and [critique.py:53-66](../src/acamar/reasoning/nodes/critique.py) bump
the shared `state.retry_count` based on `state.retrieved`/
`state.draft_answer`, unaware of how `retrieved` was assembled inside
one `RetrievalNode.__call__`. `RetrievalContext.build()`
([context.py:33-44](../src/acamar/reasoning/context.py)) truncates
whatever final ordered list it's given by cumulative char budget — no
dedupe, no rerank — so the merge step must still hand back one final,
size-bounded, RRF-ordered list, matching today's per-attempt caps.

**Real operational constraint, not just a design curiosity — caught on
review:** the final attempt widens to all 5 `Category` members
([retrieval.py:65](../src/acamar/reasoning/nodes/retrieval.py)), so
full fan-out there means 5 concurrent `search()` calls. The DB pool is
configured `min_size=1, max_size=4`
([adapters/db/pool.py:31-32](../src/acamar/adapters/db/pool.py)) — **one
connection short of what 5-way fan-out needs**, so `asyncio.gather`
would *not* achieve near-single-call latency on the final attempt; the
5th sub-search waits for one of the first four to release its
connection, partially serializing. Under concurrent `/query` traffic
this is a real pool-contention point, not a theoretical one, since the
final attempt is exactly the "everything else already failed" fallback
that's most likely to coincide with a slow, already-retried request.

This pushes toward a concrete recommendation (open for your review, not
decided unilaterally): **don't fan out on the final attempt** — keep it
as today's single unioned `categories=list(Category)` query. The
final attempt's purpose ("cast the widest net" safety net against a bad
Router guess) doesn't need per-category fairness the same way an
early, Router-narrowed attempt does, and this sidesteps the pool
contention entirely. Fan-out would apply only to attempts 0/1, where
`categories` is Router's own (usually 1-3 categories, well under the
pool cap).

## 6. Tests: what changes vs. stays green

`FakeDocumentRepository.search()` ([tests/fakes/__init__.py:159-182](../tests/fakes/__init__.py))
already records per-call kwargs in `self.search_calls` — no fake changes
needed to assert "N calls, one per category, each single-element."

**Stay green, unmodified** (single-category fixtures throughout —
[test_retrieval_node.py:43-52](../tests/unit/test_retrieval_node.py)):
`test_attempt_1_widens_the_date_window_keeps_categories`,
`test_widening_is_not_cumulative_across_attempts`,
`test_domains_pass_through_unchanged_on_the_non_final_attempts`,
`test_embeds_the_question_exactly_once`,
`test_same_day_window_still_widens_meaningfully_on_retry`,
`test_dismissed_documents_are_excluded_from_results`, and
`test_final_attempt_resets_domains_too_not_just_categories` (its only
assertion is `search_calls[0]["domains"] is None` — no assertion on
`categories`, and under fan-out `search_calls[0]`, the first of the
per-category sub-calls, still has `domains is None`).

**Need rewriting:** `test_attempt_0_uses_routers_own_filter_unchanged`
(gains a multi-category fan-out variant), and
`test_final_attempt_drops_category_filter_and_raises_the_limit` (asserts
`categories == list(Category)` on one `search_calls[0]` entry — under
the §5 recommendation of *not* fanning out on the final attempt, this
test is actually unaffected; it only needs rewriting if the final
attempt **is** fanned out instead).

**Missed in the first pass, caught on review — `test_reasoning_graph.py`
has three tests that break if attempts 0/1 fan out, none of which were
checked before:**
- `test_retry_cap_is_reached_not_an_infinite_loop`
  ([:213-236](../tests/unit/test_reasoning_graph.py)) — asserts
  `len(documents.search_calls) == 3` and `search_calls[2]["categories"]
  == list(Category)`, indexing position 2 as *the* final-attempt call.
- `test_retrieval_grade_retry_cap_is_reached_not_an_infinite_loop`
  ([:326-347](../tests/unit/test_reasoning_graph.py)) — same pattern.
- `test_retrieval_grade_and_critique_share_one_retry_budget`
  ([:350-383](../tests/unit/test_reasoning_graph.py)) — `len(documents.
  search_calls) == 3`.

All three use `categories=[Category.EVENT]` for attempts 0/1 (single
category, so those stay 1 call each) — under the §5 recommendation
(final attempt stays unioned, not fanned out), **all three keep passing
unmodified**, since only attempts 0/1 change shape and this fixture
never routes more than one category through them. If the final-attempt
fan-out option is chosen instead, all three need rewriting (the count
assumption and the position-2 indexing both break).

**New tests needed either way:** a pure-function unit test for the RRF
merge (score formula, tie-breaking, a zero-row category contributing
nothing); a `RetrievalNode` test asserting call fan-out count equals
`len(categories)` and correct merge order for a multi-category attempt
with overlapping/interleaved ranks.

## 7. Open questions for you to weigh in on

1. **Final-attempt fan-out** (§5): recommended default is *no* — keep
   it as today's single unioned all-category query, both for the pool-
   contention reason and because it also means all three
   `test_reasoning_graph.py` tests need zero changes. Object if you'd
   rather fan out there too and accept partial connection-pool
   serialization (or raise `max_size` as a separate, explicit infra
   change).
2. **Per-category sub-search limit (a)**: equal to the per-attempt cap
   (my default), or split a shared budget across N categories up front?
3. **`k=60`**: accept the reference-repo default as a starting point to
   validate against the golden set, or pick a different starting value
   / make it configurable from day one?
4. Is the disjoint-list caveat (§3.3) — RRF here can't do cross-list
   corroboration, only rank normalization — an acceptable framing to
   build against, given it's a narrower benefit than the paper's own?
5. Any appetite for gating this behind a config flag for an easy A/B
   against today's unioned query on `tests/eval/golden_qa.jsonl`'s
   multi-category questions, or go straight to replacing it?
6. Separately, and explicitly **not** solved by this proposal: the
   Social Studies Project incident's actual cause (Router
   category-selection unreliability — excluding a relevant category
   outright rather than crowding it out) is still open, still tracked
   where the design doc already logs it as a follow-up. Worth a
   distinct backlog item of its own if you want it picked up.

Not implemented — this is the design for review before any code changes.
