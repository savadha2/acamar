"""LangGraph state for `POST /query` — one shared object every node reads/updates.

Deliberately flat: `RouteDecision`/`SynthesisOutput`/`CritiqueVerdict` (each
node's own structured `ChatModel` output type) live in their node's own
module and get unpacked into these plain fields, not stored as nested
objects here — keeps this module free of any dependency on node internals.
See docs/reasoning-pipeline-design.md §2.3.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from acamar.domain.enums import Category, Domain, OutOfScopeReason, QueryIntent
from acamar.domain.models import Document

MAX_RETRIES = 2
"""CRAG retry cap — shared between `RetrievalNode` (picks the attempt's
filter) and the graph's `after_retrieval_grade`/`after_critique` routing
(each decides retry vs. proceed). All three must agree on this number, so
it lives here, not duplicated."""


class QueryState(BaseModel):
    """The graph's working state for one `/query` call."""

    question: str
    today: date
    """From `Clock`, for Router's relative-date resolution and Synthesis's
    natural phrasing."""

    # Router's output.
    intent: QueryIntent | None = None
    categories: list[Category] = []
    """Only meaningful when ``intent == DOCUMENT``."""
    domains: list[Domain] | None = None
    """``None`` = search all domains; only narrowed when the question names
    a specific source (Decision A)."""
    date_from: date | None = None
    date_to: date | None = None
    out_of_scope_reason: OutOfScopeReason | None = None

    # Retrieval / Count.
    retrieved: list[Document] = []
    count_result: int | None = None
    retrieval_relevant: bool = True
    """Set by `RetrievalGradeNode` (canonical CRAG's actual mechanism: grade
    the retrieved documents, not just the drafted answer — see
    `nodes/retrieval_grade.py`). Real bug this exists to close (2026-09-24):
    an honest "I don't know" synthesized from an empty/off-topic retrieval
    reads as *grounded* to Critique (it isn't a fabrication), so the
    widen-and-retry safety net used to never fire for the single most
    common failure — a too-narrow Router guess that missed the document
    outright. Defaults `True` so weather/count/out-of-scope, which never
    touch this field, aren't accidentally treated as ungrounded retrieval."""

    # Synthesis / Critique.
    draft_answer: str = ""
    grounded: bool = True
    """``True`` by default — weather/count/out-of-scope answers are
    trivially "grounded" (no CRAG loop applies to them); Critique sets this
    explicitly for the ``DOCUMENT`` branch only."""
    critique_reason: str = ""
    retry_count: int = 0

    final_answer: str = ""
