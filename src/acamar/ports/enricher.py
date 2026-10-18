"""The document-enrichment port. The core never calls a chat model directly.

Supersedes the earlier, narrower ``ports.summarizer`` (retired) — one
structured call per document instead of stacking separate summarize/
classify/extract calls, which would multiply an already-slow per-document
LLM step (docs/dashboard-redesign-proposal.md §3.1).
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from acamar.domain.enums import Organization
from acamar.domain.models import RawDocument


class EnrichmentError(RuntimeError):
    """The enrichment model was unreachable or returned an unusable result."""


class HolidayExtraction(BaseModel):
    """A no-school date found inside a document's content, if any."""

    model_config = ConfigDict(frozen=True)

    label: str
    """Short name, e.g. "Staff Learning Day", "Thanksgiving Break"."""
    event_date: date


class DocumentEnrichment(BaseModel):
    """Structured output of one :meth:`Enricher.enrich` call."""

    model_config = ConfigDict(frozen=True)

    headline: str = Field(min_length=1)
    """Short, assistant-style card title — never the raw subject verbatim."""
    summary: str = Field(min_length=1)
    """One/two-sentence gloss of the document's content."""
    is_relevant: bool
    """Whether this belongs on the glance-view dashboard (not the full corpus)."""
    organization: Organization
    event_date: date | None = None
    """A general date for this document, if the content clearly states one —
    the fallback for when the parser's own (subject-line-only) extraction
    found nothing, e.g. a date sitting only in an inline image's alt text
    (the real Chuck E. Cheese case that prompted this). Only used when the
    parser found ``None``; never overrides a confident parser hit — see
    ``IngestionService._enrich``.
    """
    holiday: HolidayExtraction | None = None
    """Set only when the content clearly names a specific no-school date."""


class ContentSummary(BaseModel):
    """Structured output of :meth:`Enricher.summarize`.

    Narrower than :class:`DocumentEnrichment` on purpose (2026-10-02, added
    for ``cupertino_events``): a scraped-webpage source already has a
    deterministic, cross-validated ``event_date``/``category``/
    ``organization`` from its own connector, more reliable than an LLM
    re-guess over messy prose — only the human-readable text needs
    synthesis, see ``IngestionService._enrich``'s narrow-summary branch.
    """

    model_config = ConfigDict(frozen=True)

    headline: str = Field(min_length=1)
    """Short, assistant-style card title — never the raw title verbatim."""
    summary: str = Field(min_length=1)
    """One/two-sentence gloss, written to ignore scraped-page noise (nav
    leftovers, registration boilerplate, image filenames) rather than
    naively including it."""
    # min_length=1 on both fields here and on DocumentEnrichment above
    # (2026-10-02, found by review): neither had any constraint before,
    # so a technically-schema-conforming empty-string response from the
    # model passed through silently — now it fails pydantic validation,
    # which both callers already turn into EnrichmentError -> the existing
    # fail-open fallback (raw title), instead of writing a blank card.


class Enricher(Protocol):
    """Turns one fetched document into dashboard-ready metadata."""

    async def enrich(self, doc: RawDocument) -> DocumentEnrichment:
        """Return structured enrichment for ``doc``.

        Raises:
            EnrichmentError: the model failed or returned something unusable.
                Callers should degrade gracefully (fail open), not drop the
                document — see ``IngestionService._enrich``.
        """
        ...

    async def summarize(self, doc: RawDocument) -> ContentSummary:
        """Return just a clean headline + summary for ``doc``.

        For a source whose ``event_date``/``category``/``organization`` are
        already reliable without an LLM (unlike Gmail's narrative text) but
        whose ``content`` is too unstructured to show on a dashboard card
        as-is (unlike Faria's own clean scraped titles) — see
        ``IngestionService._enrich``'s narrow-summary branch.

        Raises:
            EnrichmentError: the model failed or returned something unusable.
                Callers should degrade gracefully (fail open), not drop the
                document — see ``IngestionService._enrich``.
        """
        ...
