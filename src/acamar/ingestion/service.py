"""`IngestionService` — fetch a source, enrich, embed, upsert, reconcile, record."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from uuid import UUID

from acamar.domain.enums import Category, Domain, Organization, SourceStatus
from acamar.domain.models import Document, IngestionReport, RawDocument, Source
from acamar.ports.clock import Clock
from acamar.ports.embedder import Embedder
from acamar.ports.enricher import ContentSummary, Enricher, EnrichmentError, HolidayExtraction
from acamar.ports.repositories import DocumentRepository, SourceRepository
from acamar.ports.source_connector import SourceConnector

logger = logging.getLogger("acamar.ingestion")

_DETERMINISTIC_ORGANIZATION: dict[tuple[Domain, Category], Organization] = {
    (Domain.SCHOOL, Category.EVENT): Organization.SCHOOL_CALENDAR,
    (Domain.CITY, Category.EVENT): Organization.CITY_RECREATION,
}
"""Deterministic `organization` for non-Gmail connectors, by (domain, category).

Replaces what used to be an inline `SCHOOL_CALENDAR if category == EVENT else
None` ternary — that only ever checked `category`, not which connector/domain
actually produced the document. It was already wrong once (`manual_menu` is
also non-Gmail and also predates this map, and was briefly mislabeled
`SCHOOL_CALENDAR` before being narrowed to `category == EVENT` - 2026-09-25);
`cupertino_events` (`Domain.CITY`, also `Category.EVENT`) would have hit the
exact same bug a second time under that narrowing. An explicit mapping means
the next new non-Gmail source adds one line here instead of a third patch.
A `(domain, category)` pair with no entry (e.g. `manual_menu`'s
`(SCHOOL, MENU)`) falls through to `None` via `.get()` below — unchanged from
today's behavior, not a guess."""

_SUMMARY_ONLY_DOMAINS = frozenset({Domain.CITY})
"""Domains that get an LLM call for headline/summary ONLY — not the full
Gmail-style enrichment. Added 2026-10-02 for `cupertino_events`: its
`event_date`/`category`/`organization` are already deterministic and
cross-validated by its own parser (more reliable than an LLM re-guess over
messy scraped prose), but `content` is too unstructured (nav leftovers,
registration boilerplate, image filenames) to show on a dashboard card
as-is the way Faria's own clean scraped titles can. See `_summarize_only`
and docs/cupertino-events-source-design.md's 2026-10-02 follow-up."""

ConnectorFactory = Callable[[Source], SourceConnector]


class IngestionService:
    """One code path for every source.

    Once the source row is loaded, ``run`` does not raise for an ingestion
    failure — the outcome is the returned :class:`IngestionReport` and the row's
    ``last_status``. An unknown ``source_id`` still raises
    :class:`SourceNotFoundError`.
    """

    def __init__(
        self,
        *,
        sources: SourceRepository,
        documents: DocumentRepository,
        embedder: Embedder,
        enricher: Enricher,
        connector_factory: ConnectorFactory,
        clock: Clock,
    ) -> None:
        self._sources = sources
        self._documents = documents
        self._embedder = embedder
        self._enricher = enricher
        self._connector_factory = connector_factory
        self._clock = clock

    async def run(self, source_id: UUID) -> IngestionReport:
        """Ingest one source: fetch, enrich, embed, upsert, reconcile, record."""
        started_at = self._clock.now()
        started_perf = time.perf_counter()
        source = await self._sources.get(source_id)

        try:
            report = await self._ingest(source, started_at)
            status = SourceStatus.OK
        except Exception as exc:
            status = SourceStatus.ERROR
            report = IngestionReport(
                source_id=source.id,
                source=_label(source),
                status=status,
                error=str(exc),
            )
            logger.exception("ingestion failed", extra={"source": report.source})

        await self._sources.mark_run(source.id, status, at=self._clock.now())
        report = report.model_copy(
            update={"duration_ms": round((time.perf_counter() - started_perf) * 1000)}
        )
        logger.info("ingestion run", extra={"report": report.model_dump(mode="json")})
        return report

    async def _ingest(self, source: Source, started_at: datetime) -> IngestionReport:
        connector = self._connector_factory(source)
        raw = await connector.fetch()
        fetched = len(raw)
        enriched = await self._enrich(source.id, raw, today=self._clock.today())

        vectors = await self._embedder.embed([doc.embedding_text for doc in enriched])
        upserted = await self._documents.upsert(
            source.id, list(zip(enriched, vectors, strict=True))
        )
        deleted = 0
        if self._should_reconcile(source):
            deleted = await self._documents.reconcile_deletions(
                source.id,
                run_started_at=started_at,
                on_or_after=self._clock.today(),
                grace=self._reconcile_grace(source),
            )
        return IngestionReport(
            source_id=source.id,
            source=_label(source),
            status=SourceStatus.OK,
            fetched=fetched,
            upserted=upserted,
            deleted=deleted,
        )

    @staticmethod
    def _should_reconcile(source: Source) -> bool:
        """Whether "not seen this run" may be inferred as "removed upstream".

        True only for a source whose `fetch()` re-asserts its *entire* live
        set every run (Faria's full-month re-scrape). False for Gmail
        (always — its `since`-bounded fetch is inherently incremental, a
        message from 3 weeks ago legitimately isn't re-fetched today even
        though it's still valid) and for any other source that opts out via
        `source.config["reconcile"] = False` — added for `manual_menu`,
        where a single upload only ever covers one month and reconciling
        would wrongly soft-delete every other month already ingested.
        """
        if source.domain == Domain.GMAIL:
            return False
        return bool(source.config.get("reconcile", True))

    @staticmethod
    def _reconcile_grace(source: Source) -> timedelta:
        """How long a row may go "not seen" before reconciliation trusts that absence.

        (2026-10-02, real bug found by review — "removed upstream" and
        "transiently missed" used to be indistinguishable.)

        Default 0 — today's exact behavior for every source that doesn't
        set `source.config["reconcile_grace_hours"]`. `cupertino_events`
        needs a nonzero grace: its own per-event fault isolation means
        `fetch()` can legitimately return a partial set on a run with a
        single transient failure (a timeout, a flaky retry, a robots.txt
        hiccup) — reconciling on "not seen *this* run" alone can't tell
        that apart from a genuine upstream removal, and with a once-daily
        cron a wrongly-tripped reconcile vanishes a real, still-upcoming
        event for up to a day. A grace period longer than one run interval
        tolerates a single missed run for free while a truly removed event
        still disappears within a day or two — set via config, not a
        per-domain default, so each source's own real re-run cadence
        decides its own safe value.
        """
        hours = source.config.get("reconcile_grace_hours", 0)
        return timedelta(hours=float(hours))

    async def _enrich(
        self, source_id: UUID, raw: list[RawDocument], *, today: date
    ) -> list[RawDocument]:
        """Fill headline/summary/is_relevant/organization; may add holiday docs.

        Three treatments, not two:
        - Gmail: full LLM enrichment (this method's bulk, below) — its own
          event_date/category/organization are unreliable from the parser
          alone, so the LLM gets to decide all of them.
        - `_SUMMARY_ONLY_DOMAINS` (currently just `Domain.CITY`): a
          *narrower* LLM call, headline/summary only — see
          `_summarize_only`. event_date/category/organization are already
          deterministic and more reliable than an LLM re-guess, but
          `content` is scraped-webpage prose too unstructured to show as-is.
        - Everything else (Faria's own clean scraped titles, manual_menu):
          fully deterministic, no LLM at all — cheaper and immune to the
          model mangling a title that was already good.

        Gmail/city documents whose content matches what's already stored
        reuse the prior enrichment instead of re-calling the model
        (docs/dashboard-redesign-proposal.md §3.3 — the daily re-fetch of
        mostly-unchanged mail was otherwise re-enriching everything, every
        run). Same idea for a document whose *parser-extracted* `event_date`
        is already in the past: it will never appear on the dashboard
        regardless of how well it's enriched (`DashboardService` filters on
        `event_date >= today`), so paying for an LLM call on it is pure
        waste — skip straight to a cheap fallback, same as the failure path.
        """
        llm_eligible = [
            d for d in raw if d.domain == Domain.GMAIL or d.domain in _SUMMARY_ONLY_DOMAINS
        ]
        existing: dict[str, Document] = {}
        if llm_eligible:
            existing = await self._documents.get_existing(
                source_id, [d.external_id for d in llm_eligible]
            )

        result: list[RawDocument] = []
        for doc in raw:
            if doc.domain in _SUMMARY_ONLY_DOMAINS:
                result.append(await self._summarize_only(doc, existing, today=today))
                continue

            if doc.domain != Domain.GMAIL:
                # Deterministic per (domain, category) — see
                # _DETERMINISTIC_ORGANIZATION's own docstring for why this is
                # a mapping now, not a `category == EVENT` ternary. A pair
                # with no entry (manual_menu's (SCHOOL, MENU); any future
                # non-Gmail source that isn't added here yet) falls through
                # to None rather than a guessed label — see Organization's
                # own "revise with a real decision" note.
                organization = _DETERMINISTIC_ORGANIZATION.get((doc.domain, doc.category))
                result.append(
                    doc.model_copy(
                        update={
                            "headline": doc.title,
                            "is_relevant": True,
                            "organization": organization,
                        }
                    )
                )
                continue

            if doc.event_date is not None and doc.event_date < today:
                result.append(
                    doc.model_copy(
                        update={"headline": doc.title, "is_relevant": False}
                    )
                )
                continue

            prior = existing.get(doc.external_id)
            if (
                prior is not None
                and prior.content == doc.content
                and prior.headline is not None
            ):
                # Bug (2026-09-15, real regression): this used to reuse only
                # headline/summary/is_relevant/organization, silently
                # dropping a previously-found event_date/category back to
                # None/REMINDER on the next unrelated re-fetch (the Chuck E.
                # Cheese case) — carry those forward too.
                reuse_updates: dict[str, object] = {
                    "headline": prior.headline,
                    "summary": prior.summary,
                    "is_relevant": prior.is_relevant,
                    "organization": prior.organization,
                }
                if doc.event_date is None and prior.event_date is not None:
                    reuse_updates["event_date"] = prior.event_date
                    reuse_updates["category"] = prior.category
                result.append(doc.model_copy(update=reuse_updates))
                continue

            try:
                enrichment = await self._enricher.enrich(doc)
            except EnrichmentError:
                # Fail open: one bad model call must cost this document its
                # polish, not the whole ingestion run (it used to — see
                # docs/dashboard-redesign-proposal.md §3.2).
                logger.warning(
                    "enrichment failed for %s, using raw fallback",
                    doc.external_id,
                    exc_info=True,
                )
                result.append(
                    doc.model_copy(
                        update={
                            "headline": doc.title,
                            "is_relevant": True,
                            "organization": Organization.OTHER,
                        }
                    )
                )
                continue

            updates: dict[str, object] = {
                "headline": enrichment.headline,
                "summary": enrichment.summary,
                "is_relevant": enrichment.is_relevant,
                "organization": enrichment.organization,
            }
            event_date = enrichment.event_date
            if _summary_has_unfilled_placeholder(enrichment.summary):
                # Real regression (2026-09-17, the Parent Teacher Conference
                # case): the model's own summary admitted it didn't know the
                # main subject's date ("scheduled for [year]") while a date
                # from an unrelated secondary detail (a "return forms by
                # tomorrow" deadline) still slipped through as event_date —
                # via either this field or the narrative fallback below.
                # A left-in template bracket is a strong self-signal that
                # neither source should be trusted for THIS document, so
                # skip both and let it fall through as undated.
                event_date = None
            elif event_date is None and doc.published_at is not None:
                # Real, reproducible gap (2026-09-15): the model sometimes
                # narrates a date correctly in `summary` ("due on Friday,
                # September 25th", "board meeting tonight") but leaves the
                # structured `event_date` field null in that same response —
                # it can produce fluent prose without reliably mirroring it
                # into the constrained field. Self-consistency net: if its
                # own summary states an unambiguous (absolute or relative-to-
                # the-email) date, trust that over an empty field.
                event_date = _extract_narrative_date(
                    enrichment.summary, reference=doc.published_at.date()
                )
            if doc.event_date is None and event_date is not None:
                # The parser found nothing (subject-line-only scan); the LLM
                # saw a date the parser structurally can't reach (buried in
                # body prose or an image's alt text — the real Chuck E.
                # Cheese case). Never overrides a confident parser hit.
                # Flips REMINDER->EVENT to match the parser's own convention
                # (dated => event, undated => reminder).
                updates["event_date"] = event_date
                if doc.category == Category.REMINDER:
                    updates["category"] = Category.EVENT
            result.append(doc.model_copy(update=updates))
            if enrichment.holiday is not None:
                result.append(
                    _holiday_document(doc, enrichment.holiday, enrichment.organization)
                )
        return result

    async def _summarize_only(
        self, doc: RawDocument, existing: dict[str, Document], *, today: date
    ) -> RawDocument:
        """The narrow `_SUMMARY_ONLY_DOMAINS` treatment.

        headline/summary from the LLM; `event_date`/`category` untouched
        (the connector's own parser already set them, more reliably than an
        LLM re-guess); `organization` from `_DETERMINISTIC_ORGANIZATION`,
        same as the fully deterministic branch, never from the LLM.
        """
        organization = _DETERMINISTIC_ORGANIZATION.get((doc.domain, doc.category))
        base_updates: dict[str, object] = {"is_relevant": True, "organization": organization}

        if doc.event_date is not None and doc.event_date < today:
            # Will never show on the dashboard regardless of polish — same
            # waste-avoidance as the Gmail branch's identical check.
            return doc.model_copy(update={**base_updates, "headline": doc.title})

        prior = existing.get(doc.external_id)
        if prior is not None and prior.content == doc.content and prior.summary is not None:
            return doc.model_copy(
                update={**base_updates, "headline": prior.headline, "summary": prior.summary}
            )

        try:
            summary: ContentSummary = await self._enricher.summarize(doc)
        except EnrichmentError:
            # Fail open, same philosophy as the Gmail branch: one bad model
            # call costs this document its polish, not the whole run.
            logger.warning(
                "summarize failed for %s, using raw fallback", doc.external_id, exc_info=True
            )
            return doc.model_copy(update={**base_updates, "headline": doc.title})

        return doc.model_copy(
            update={**base_updates, "headline": summary.headline, "summary": summary.summary}
        )


_ABSOLUTE_DATE_PATTERN = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b")
_ABSOLUTE_DATE_FORMATS = ("%B %d", "%b %d")

# Real patterns observed in actual mail (2026-09-16): "tonight", "tomorrow",
# a bare weekday name ("Thursday Morning Help", "...Meeting - Wednesday"),
# and "this <weekday>" ("this Friday"). No "next <weekday>" seen - casual
# English is genuinely ambiguous about which week that means, so it's
# deliberately not guessed at here.
_WEEKDAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)
_WEEKDAY_INDEX = {name: i for i, name in enumerate(_WEEKDAY_NAMES)}
_RELATIVE_DATE_PATTERN = re.compile(
    r"\b(tonight|today|tomorrow|(?:this\s+)?(?:" + "|".join(_WEEKDAY_NAMES) + r"))\b",
    re.IGNORECASE,
)


_UNFILLED_PLACEHOLDER_PATTERN = re.compile(r"\[[a-zA-Z ]{2,20}\]")
"""Matches a left-in template bracket like "[year]" or "[date]" — the model
writing its own summary and not filling in a value it clearly didn't have.
Deliberately narrow (short, letters-only, 2+ chars) so it doesn't flag a
single-letter legal-citation bracket ("[c]", "[B]") as a placeholder."""


def _summary_has_unfilled_placeholder(summary: str) -> bool:
    """True if the model's own summary left a template bracket unfilled.

    A strong, self-reported signal that the model didn't actually know a
    date for the email's main subject — see the 2026-09-17 Parent Teacher
    Conference case in ``_enrich``.
    """
    return bool(_UNFILLED_PLACEHOLDER_PATTERN.search(summary))


def _extract_narrative_date(text: str, *, reference: date) -> date | None:
    """First unambiguous date in free text, absolute or relative, or ``None``.

    Tries an absolute ``Month D[st/nd/rd/th]`` match first (no year in the
    pattern itself — prose like "September 25th" never states one; the real
    year comes from ``reference``, i.e. the document's own ``published_at``,
    never guessed). Falls back to a relative term ("tonight", "tomorrow", a
    bare or "this "-prefixed weekday) resolved against that same reference
    date. Deliberately narrow: one match, first one found, no attempt to
    disambiguate multiple dates in the same text.
    """
    absolute = _ABSOLUTE_DATE_PATTERN.search(text)
    if absolute is not None:
        # A leap-year placeholder (not the real year) so "February 29" parses
        # regardless of whether the real year is one — %Y with no year in the
        # input is deprecated (Python 3.15) precisely for this ambiguity.
        raw = f"{absolute.group(1)} {absolute.group(2)} 2000"
        for fmt in _ABSOLUTE_DATE_FORMATS:
            try:
                return datetime.strptime(raw, f"{fmt} %Y").date().replace(year=reference.year)
            except ValueError:
                continue

    relative = _RELATIVE_DATE_PATTERN.search(text)
    if relative is not None:
        return _resolve_relative_date(relative.group(1), reference=reference)

    return None


def _resolve_relative_date(term: str, *, reference: date) -> date:
    normalized = term.lower().removeprefix("this ").strip()
    if normalized in ("tonight", "today"):
        return reference
    if normalized == "tomorrow":
        return reference + timedelta(days=1)
    # A bare/"this "-prefixed weekday: the closest occurrence on or after
    # `reference` (same day => today, matching how "tonight" behaves).
    weekday = _WEEKDAY_INDEX[normalized]
    return reference + timedelta(days=(weekday - reference.weekday()) % 7)


def _holiday_document(
    source_doc: RawDocument, holiday: HolidayExtraction, organization: Organization
) -> RawDocument:
    return RawDocument(
        external_id=f"{source_doc.external_id}::holiday",
        title=holiday.label,
        content=f"{holiday.label} — no school on {holiday.event_date:%A, %B %-d, %Y}.",
        domain=source_doc.domain,
        category=Category.HOLIDAY,
        event_date=holiday.event_date,
        published_at=source_doc.published_at,
        headline=holiday.label,
        summary=f"No school: {holiday.label}.",
        is_relevant=True,
        organization=organization,
    )


def _label(source: Source) -> str:
    return f"{source.domain}/{source.category}"
