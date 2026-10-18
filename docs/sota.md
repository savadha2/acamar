# SOTA landscape — reasoning pipeline (2026-09-17)

Status: **reference, not a build plan.** Written after the same-day review
of the reasoning-pipeline build (see
[reasoning-pipeline-design.md](reasoning-pipeline-design.md)) surfaced
three real, open gaps, logged in the backlog memory. Each item below is
scored against those gaps, not picked from a general survey — ranked
"fixes something already known to be broken" first, "optimizes something
that isn't broken yet" last. Concrete action items are tracked in the
backlog memory (search "SOTA review, 2026-09-17"), not here — this doc is
the citation/rationale record they point back to.

---

## 1. Grade retrieval, not just the answer — canonical CRAG

**Yan, Gu, Zhu, Ling (2024), "Corrective Retrieval Augmented Generation."**
[arXiv:2401.15884](https://arxiv.org/abs/2401.15884). Reference
implementation: [github.com/HuskyInSalt/CRAG](https://github.com/HuskyInSalt/CRAG).

The paper's CRAG grades the *retrieved documents* ("correct / ambiguous /
incorrect") before generation. `CritiqueNode`
(`src/acamar/reasoning/nodes/critique.py`) grades the *drafted answer's*
groundedness after generation instead — closer to Self-RAG's reflection
tokens than to CRAG proper. That divergence is the root cause of the open
bug already logged in the backlog: zero/weak retrieval → Synthesis
honestly answers "I don't know" → an honest non-answer reads as
*grounded* → the retry loop never fires, no matter how bad the retrieval
was.

**Fix shape:** a lightweight relevance grade on `state.retrieved`,
inserted between `RetrievalNode` and `SynthesisNode`. Same `ChatModel`
port, same `structured()` + pydantic-verdict pattern as `CritiqueVerdict`
— no new dependency, no new port. A "these documents don't look relevant
to the question" verdict triggers widen-and-retry *before* a synthesis
call is wasted, catching the case the current post-hoc check structurally
can't.

## 2. Per-category retrieval + Reciprocal Rank Fusion — RAG-Fusion

**Rackauckas (2024), "RAG-Fusion: a New Take on Retrieval-Augmented
Generation."** [arXiv:2402.03367](https://arxiv.org/abs/2402.03367).
Reference implementation: [github.com/Raudaschl/rag-fusion](https://github.com/Raudaschl/rag-fusion).

Directly targets Decision C's pooling-imbalance limitation, already
logged in the backlog almost verbatim: one top-K query across
`category = ANY([event, reminder, holiday])` lets a category with more
near-embedding matches crowd out one with fewer, even though both were
nominally "included." RAG-Fusion's answer: one retrieval *per* category
(each gets its own fair top-K), merged by Reciprocal Rank Fusion instead
of sharing one `LIMIT`.

**Fix shape:** `RetrievalNode` runs N sub-searches (one per routed
category) instead of one unioned search, and merges by RRF.
`DocumentRepository.search()`'s signature doesn't need to change — this
is contained to the node.

## 3. Extract-then-score judging

Judge calibration literature, e.g. ["Calibrating LLM Judges: Linear
Probes for Fast and Reliable Uncertainty
Estimation"](https://arxiv.org/pdf/2512.22245) and ["When the Judge
Changes, So Does the Measurement: Auditing LLM-as-Judge
Reliability"](https://arxiv.org/html/2607.08535).

Targets the judge reliability finding already logged in the backlog: the
judge (llama3) scored the wrong Social-Studies-Project answer 5/5 because
it misread its own supplied reference text ("does not provide a specific
due date" when the reference states "September 25, 2026"). The cheapest
mitigation in this literature is forcing an **extract-before-compare**
step — have the judge quote the specific reference span its score is
grounded in, *then* score — rather than one holistic read-and-judge call.
Panel-of-judges (run 2-3x, flag disagreement) is the other cheap option
but costs proportionally more per eval run against a golden set this
small (10 examples today).

**Fix shape:** a prompt change to `scripts/run_eval.py`'s
`_JUDGE_PROMPT`. No new dependency; optionally a one-field addition
(`quoted_reference_span: str`) to `JudgeVerdict` if the extraction step
should be structurally enforced rather than just requested in prose.

## 4. Confidence-gated model cascade

FrugalGPT-style cascade routing — e.g. ["UCCI: Calibrated Uncertainty for
Cost-Optimal LLM Cascade Routing"](https://arxiv.org/html/2605.18796);
see also ["Dynamic Model Routing and Cascading for Efficient LLM
Inference: A Survey"](https://arxiv.org/pdf/2603.04445).

Aimed at latency, not correctness: a DOCUMENT question with 2 CRAG
retries makes ~7 sequential `llama3` calls today, and
`streamlit_app.py`'s own `_query()` comment already flags "the reasoning
pipeline can take 20-30s." The substrate for this already exists —
`ollama_router_model` / `ollama_model` (Synthesis) / `ollama_critique_model`
/ `ollama_judge_model` are already separately configured per node
(`config.py`, "Reasoning pipeline" section); this only changes them from
a static per-node assignment to a confidence-gated cascade (try
`gemma3:1b` — already benchmarked at 5-6x faster — escalate to `llama3`
only below a confidence threshold).

**Priority note:** optimizes something that isn't broken yet. Worth
building if the 20-30s round trip becomes an actual complaint in
practice, not before.

## 5. DSPy / MIPROv2 — known about, not adopted

[dspy.ai](https://dspy.ai/) — Khattab et al., "programming, not
prompting" foundation models; MIPROv2 optimizes instructions + few-shot
examples jointly against a metric and a labeled set.

Already manually did what DSPy automates:
`scripts/_spot_check_router.py` against 31 hand-labeled questions, one
manual prompt fix, re-measure (65% → 87% → 90%, see `config.py`'s
`ollama_router_model` changelog comment). DSPy would search over
instructions/few-shot examples against `tests/eval/golden_qa.jsonl` + the
judge automatically instead of by hand.

**Priority note:** genuinely high-leverage once the golden set outgrows
what's spot-checkable by eye — same threshold logic already written into
Decision E for upgrading the judge tier
([reasoning-pipeline-design.md](reasoning-pipeline-design.md)). Premature
at the current 10-example corpus size (new dependency, new workflow, for
a search space this small). Logged here so it isn't rediscovered later,
not queued as a backlog action.

---

## Not included, and why

- **Self-RAG / Adaptive-RAG** (query-complexity-routed retrieval
  strategies). Self-RAG: **Asai, Wu, Wang, Sil, Hajishirzi (2023), "Self-RAG:
  Learning to Retrieve, Generate, and Critique through Self-Reflection."**
  [arXiv:2310.11511](https://arxiv.org/abs/2310.11511). Reference
  implementation: [github.com/AkariAsai/self-rag](https://github.com/AkariAsai/self-rag).
  This is the citation `nodes/retrieval_grade.py` and `nodes/critique.py`
  both point to by name ("closer to Self-RAG's reflection tokens than to
  CRAG proper") without actually sourcing it — logged here so it's sourced
  somewhere. `QueryIntent` (document/weather/count/out_of_scope) is already
  an implementation of the *Adaptive-RAG* half of this idea in miniature: a
  router deciding retrieval strategy by query type before any retrieval
  happens. No action on either; noted as validation that the existing
  design already converged on this shape independently, not a gap.
- **Reranking (cross-encoder) / HyDE** — considered and set aside:
  today's documented retrieval failures are category-filter exclusions
  at the SQL `WHERE` clause (the right document never enters the
  candidate set), not embedding-similarity ordering problems within an
  already-correct candidate set. Neither technique reorders or expands
  the candidate set in a way that fixes that; #1/#2 above do. Revisit if
  a *found-but-ranked-low* failure mode shows up — a different symptom
  from anything logged so far.
