"""IngestionService — orchestration only, all collaborators faked."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from acamar.domain.enums import Category, Domain, Organization, SourceStatus
from acamar.domain.models import Document, RawDocument, Source
from acamar.ingestion import service as service_module
from acamar.ingestion.service import IngestionService
from acamar.ports.enricher import DocumentEnrichment, HolidayExtraction
from acamar.ports.repositories import SourceNotFoundError
from acamar.ports.source_connector import ConnectorError
from tests.fakes import (
    FakeConnector,
    FakeDocumentRepository,
    FakeEmbedder,
    FakeEnricher,
    FakeSourceRepository,
    FrozenClock,
)

NOW = datetime(2026, 9, 12, 13, 0, tzinfo=UTC)


def _source(*, domain: Domain = Domain.SCHOOL) -> Source:
    return Source(
        id=uuid4(),
        school_id=uuid4(),
        domain=domain,
        category=Category.EVENT,
        config={"calendar_url": "https://example.test/cal"},
    )


def _raw(external_id: str, *, domain: Domain = Domain.SCHOOL) -> RawDocument:
    return RawDocument(
        external_id=external_id,
        title=f"Event {external_id}",
        content=f"Event {external_id} — all day.",
        domain=domain,
        category=Category.EVENT,
        event_date=date(2026, 9, 20),
    )


def _service(
    *,
    source: Source,
    connector: FakeConnector,
    embedder: FakeEmbedder | None = None,
    enricher: FakeEnricher | None = None,
    documents: FakeDocumentRepository | None = None,
    sources: FakeSourceRepository | None = None,
) -> tuple[IngestionService, FakeSourceRepository, FakeDocumentRepository]:
    src_repo = sources or FakeSourceRepository(source)
    doc_repo = documents or FakeDocumentRepository()
    service = IngestionService(
        sources=src_repo,
        documents=doc_repo,
        embedder=embedder or FakeEmbedder(dim=4),
        enricher=enricher or FakeEnricher(),
        connector_factory=lambda _s: connector,
        clock=FrozenClock(NOW),
    )
    return service, src_repo, doc_repo


async def test_happy_path_marks_ok_and_reports_counts() -> None:
    source = _source()
    doc_repo = FakeDocumentRepository()
    doc_repo.reconcile_result = 2
    service, src_repo, _ = _service(
        source=source,
        connector=FakeConnector([_raw("a"), _raw("b"), _raw("c")]),
        documents=doc_repo,
    )

    report = await service.run(source.id)

    assert report.status is SourceStatus.OK
    assert (report.fetched, report.upserted, report.deleted) == (3, 3, 2)
    assert src_repo.runs == [(source.id, SourceStatus.OK)]
    assert doc_repo.upserts[0][0] == source.id
    assert len(doc_repo.upserts[0][1]) == 3


async def test_reconcile_uses_run_start_and_today() -> None:
    source = _source()
    service, _, doc_repo = _service(source=source, connector=FakeConnector([_raw("a")]))

    await service.run(source.id)

    (_, run_started_at, on_or_after, grace) = doc_repo.reconciles[0]
    assert run_started_at == NOW
    assert on_or_after == NOW.date()
    assert grace == timedelta()  # no reconcile_grace_hours set -> today's exact behavior


async def test_reconcile_grace_hours_is_passed_through_to_the_repository() -> None:
    # Real bug fix (2026-10-02, found by review): a connector whose own
    # per-event fault isolation can legitimately return a partial fetch()
    # on a transient failure must not have that one miss treated identically
    # to "upstream removed it" — source.config["reconcile_grace_hours"]
    # tolerates a configurable number of missed runs before reconciling.
    source = _source().model_copy(update={"config": {"reconcile_grace_hours": 48}})
    service, _, doc_repo = _service(source=source, connector=FakeConnector([_raw("a")]))

    await service.run(source.id)

    (_, _, _, grace) = doc_repo.reconciles[0]
    assert grace == timedelta(hours=48)


async def test_reconciliation_is_skipped_for_gmail_sources() -> None:
    # "Not seen this run" only implies "removed upstream" for a source that
    # does a full re-fetch every time (Faria). Gmail's fetch is incremental
    # (GmailSchoolConnector's `since`), so reconciling against it would wrongly
    # soft-delete anything older than the incremental window.
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([_raw("a", domain=Domain.GMAIL)]),
        documents=doc_repo,
    )

    report = await service.run(source.id)

    assert doc_repo.reconciles == []
    assert report.deleted == 0


async def test_connector_error_is_recorded_not_raised() -> None:
    source = _source()
    service, src_repo, doc_repo = _service(
        source=source,
        connector=FakeConnector(error=ConnectorError("site down", source="x")),
    )

    report = await service.run(source.id)

    assert report.status is SourceStatus.ERROR
    assert report.error is not None and "site down" in report.error
    assert src_repo.runs == [(source.id, SourceStatus.ERROR)]
    assert doc_repo.upserts == []


# ---------------------------------------------------------------------------
# enrichment — non-Gmail domains get deterministic, LLM-free treatment
# ---------------------------------------------------------------------------


async def test_non_gmail_docs_skip_the_enricher_entirely() -> None:
    source = _source(domain=Domain.SCHOOL)
    enricher = FakeEnricher()
    doc_repo = FakeDocumentRepository()
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([_raw("a")]),
        enricher=enricher,
        documents=doc_repo,
    )

    await service.run(source.id)

    assert enricher.calls == []
    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.headline == "Event a"  # == raw title, unchanged
    assert upserted_doc.is_relevant is True
    assert upserted_doc.organization == Organization.SCHOOL_CALENDAR


async def test_city_domain_event_docs_get_city_recreation_organization() -> None:
    # cupertino_events (Domain.CITY, Category.EVENT) regression test for the
    # bug this would otherwise have hit a second time: _DETERMINISTIC_
    # ORGANIZATION is keyed by (domain, category), not category alone, so
    # this doesn't collide with SCHOOL_CALENDAR despite also being EVENT.
    source = _source(domain=Domain.CITY)
    doc_repo = FakeDocumentRepository()
    service, _, _ = _service(
        source=source, connector=FakeConnector([_raw("a", domain=Domain.CITY)]), documents=doc_repo
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.organization == Organization.CITY_RECREATION


async def test_non_gmail_non_event_docs_get_no_organization_guess() -> None:
    # Real gap found adding manual_menu (2026-09-25): this branch used to
    # hardcode SCHOOL_CALENDAR for every non-Gmail document because
    # faria_events (category=EVENT) was the only one that existed. A menu
    # document is non-Gmail too but isn't the school calendar, and no other
    # closed Organization value fits it — None, not a guess.
    source = _source(domain=Domain.SCHOOL)
    doc_repo = FakeDocumentRepository()
    menu_doc = _raw("a").model_copy(update={"category": Category.MENU})
    service, _, _ = _service(
        source=source, connector=FakeConnector([menu_doc]), documents=doc_repo
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.organization is None


async def test_manual_menu_source_skips_reconciliation_via_config_flag() -> None:
    # Not domain-keyed like Gmail's skip — manual_menu opts out via its own
    # sources.config, since a monthly upload is a partial view of what's
    # still valid, same underlying reason as Gmail's incremental fetch.
    source = _source(domain=Domain.SCHOOL).model_copy(
        update={"config": {"reconcile": False}}
    )
    doc_repo = FakeDocumentRepository()
    service, _, _ = _service(
        source=source, connector=FakeConnector([_raw("a")]), documents=doc_repo
    )

    report = await service.run(source.id)

    assert doc_repo.reconciles == []
    assert report.deleted == 0


# ---------------------------------------------------------------------------
# enrichment — Gmail domain calls the enricher
# ---------------------------------------------------------------------------


async def test_gmail_docs_get_enriched_before_upsert() -> None:
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([_raw("a", domain=Domain.GMAIL)]),
        documents=doc_repo,
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.headline == "headline: Event a"
    assert upserted_doc.summary is not None
    assert upserted_doc.organization == Organization.OTHER


async def test_enricher_fallback_date_fills_a_parser_gap_and_flips_category() -> None:
    # The real Chuck E. Cheese case: the parser found no date (it's only in
    # an image's alt text), so it's a REMINDER with event_date=None going in.
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    undated = _raw("a", domain=Domain.GMAIL).model_copy(
        update={"event_date": None, "category": Category.REMINDER}
    )
    enrichment = DocumentEnrichment(
        headline="Chuck E. Cheese Night",
        summary="20% of sales go to the school.",
        is_relevant=True,
        organization=Organization.FEEF,
        event_date=date(2026, 9, 18),
    )
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([undated]),
        enricher=FakeEnricher(responses={"a": enrichment}),
        documents=doc_repo,
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.event_date == date(2026, 9, 18)
    assert upserted_doc.category == Category.EVENT


async def test_enricher_fallback_date_never_overrides_the_parsers_own_date() -> None:
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    dated = _raw("a", domain=Domain.GMAIL)  # event_date already 2026-09-20
    enrichment = DocumentEnrichment(
        headline="Wrong date",
        summary="s",
        is_relevant=True,
        organization=Organization.OTHER,
        event_date=date(2026, 9, 25),  # must be ignored — parser already knew
    )
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([dated]),
        enricher=FakeEnricher(responses={"a": enrichment}),
        documents=doc_repo,
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.event_date == date(2026, 9, 20)


async def test_already_past_gmail_doc_skips_the_enricher_entirely() -> None:
    # It'll never show on the dashboard (event_date < today is filtered
    # there too) — spending an LLM call on it is pure waste, see
    # docs/dashboard-redesign-proposal.md and the 2026-09-15 llama3 runtime
    # blowup that prompted this.
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    enricher = FakeEnricher()
    past_doc = _raw("a", domain=Domain.GMAIL).model_copy(
        update={"event_date": date(2026, 9, 1)}  # NOW is 2026-09-12
    )
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([past_doc]),
        enricher=enricher,
        documents=doc_repo,
    )

    await service.run(source.id)

    assert enricher.calls == []
    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.headline == "Event a"  # raw title, no LLM polish
    assert upserted_doc.is_relevant is False


async def test_enricher_failure_falls_back_and_does_not_abort_the_run() -> None:
    """The whole point of fault isolation: one bad call costs one document
    its polish, not the run (docs/dashboard-redesign-proposal.md §3.2)."""
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    service, src_repo, _ = _service(
        source=source,
        connector=FakeConnector(
            [_raw("a", domain=Domain.GMAIL), _raw("b", domain=Domain.GMAIL)]
        ),
        enricher=FakeEnricher(fail_for={"a"}),
        documents=doc_repo,
    )

    report = await service.run(source.id)

    assert report.status is SourceStatus.OK
    assert src_repo.runs == [(source.id, SourceStatus.OK)]
    docs = {doc.external_id: doc for doc, _vector in doc_repo.upserts[0][1]}
    assert docs["a"].headline == "Event a"  # fallback: raw title
    assert docs["a"].organization == Organization.OTHER
    assert docs["b"].headline == "headline: Event b"  # unaffected


async def test_holiday_extraction_adds_a_second_document() -> None:
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    enrichment = DocumentEnrichment(
        headline="Newsletter",
        summary="A newsletter.",
        is_relevant=False,
        organization=Organization.CLASSROOM,
        holiday=HolidayExtraction(label="Staff Learning Day", event_date=date(2026, 9, 25)),
    )
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([_raw("a", domain=Domain.GMAIL)]),
        enricher=FakeEnricher(responses={"a": enrichment}),
        documents=doc_repo,
    )

    report = await service.run(source.id)

    assert report.fetched == 1
    assert report.upserted == 2  # the parent + the synthesized holiday doc
    docs = {doc.external_id: doc for doc, _vector in doc_repo.upserts[0][1]}
    holiday_doc = docs["a::holiday"]
    assert holiday_doc.category == Category.HOLIDAY
    assert holiday_doc.event_date == date(2026, 9, 25)
    assert holiday_doc.is_relevant is True  # surfaced even though the parent isn't
    assert docs["a"].is_relevant is False


async def test_unchanged_gmail_content_reuses_prior_enrichment() -> None:
    source = _source(domain=Domain.GMAIL)
    prior = Document(
        id=uuid4(),
        source_id=source.id,
        domain=Domain.GMAIL,
        category=Category.EVENT,
        student_ref=None,
        title="Event a",
        content="Event a — all day.",
        headline="Cached headline",
        summary="Cached summary.",
        is_relevant=True,
        organization=Organization.FEEF,
        external_id="a",
        event_date=date(2026, 9, 20),
        published_at=None,
    )
    doc_repo = FakeDocumentRepository(stored=[prior])
    enricher = FakeEnricher()
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([_raw("a", domain=Domain.GMAIL)]),  # same content as `prior`
        enricher=enricher,
        documents=doc_repo,
    )

    await service.run(source.id)

    assert enricher.calls == []  # reused, not re-called
    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.headline == "Cached headline"
    assert upserted_doc.organization == Organization.FEEF


async def test_reuse_carries_forward_a_previously_found_event_date() -> None:
    # Real regression (2026-09-15): the reuse path used to copy only
    # headline/summary/is_relevant/organization, silently dropping a
    # previously-discovered event_date/category back to None/REMINDER on the
    # next unrelated re-fetch of unchanged content (the Chuck E. Cheese case).
    source = _source(domain=Domain.GMAIL)
    prior = Document(
        id=uuid4(),
        source_id=source.id,
        domain=Domain.GMAIL,
        category=Category.EVENT,  # already resolved by a prior enrichment run
        student_ref=None,
        title="Event a",
        content="Event a — all day.",
        headline="Cached headline",
        summary="Cached summary.",
        is_relevant=True,
        organization=Organization.FEEF,
        external_id="a",
        event_date=date(2026, 9, 18),
        published_at=None,
    )
    doc_repo = FakeDocumentRepository(stored=[prior])
    # This run's fresh parse finds no date (as if the deterministic parser
    # never could) — matches the real Chuck E. Cheese shape exactly.
    fresh = _raw("a", domain=Domain.GMAIL).model_copy(
        update={"event_date": None, "category": Category.REMINDER, "content": "Event a — all day."}
    )
    enricher = FakeEnricher()
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([fresh]),
        enricher=enricher,
        documents=doc_repo,
    )

    await service.run(source.id)

    assert enricher.calls == []  # still a cache hit, content unchanged
    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.event_date == date(2026, 9, 18)
    assert upserted_doc.category == Category.EVENT


async def test_narrative_date_backfills_when_the_structured_field_is_empty() -> None:
    # Real gap (2026-09-15): the model sometimes states a date correctly in
    # `summary` ("due on Friday, September 25th") but leaves the structured
    # `event_date` field null in that same response.
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    undated = _raw("a", domain=Domain.GMAIL).model_copy(
        update={
            "event_date": None,
            "category": Category.REMINDER,
            "published_at": datetime(2026, 8, 31, tzinfo=UTC),
        }
    )
    enrichment = DocumentEnrichment(
        headline="Social Studies Project",
        summary="The project is due on Friday, September 25th.",
        is_relevant=True,
        organization=Organization.CLASSROOM,
        event_date=None,  # the gap being tested
    )
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([undated]),
        enricher=FakeEnricher(responses={"a": enrichment}),
        documents=doc_repo,
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.event_date == date(2026, 9, 25)
    assert upserted_doc.category == Category.EVENT


async def test_narrative_date_uses_the_documents_published_year() -> None:
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    undated = _raw("a", domain=Domain.GMAIL).model_copy(
        update={
            "event_date": None,
            "category": Category.REMINDER,
            "published_at": datetime(2025, 12, 1, tzinfo=UTC),
        }
    )
    enrichment = DocumentEnrichment(
        headline="h",
        summary="Due December 5th.",
        is_relevant=True,
        organization=Organization.OTHER,
        event_date=None,
    )
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([undated]),
        enricher=FakeEnricher(responses={"a": enrichment}),
        documents=doc_repo,
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.event_date == date(2025, 12, 5)


async def _run_narrative_summary(summary: str, *, published_at: datetime) -> date | None:
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    undated = _raw("a", domain=Domain.GMAIL).model_copy(
        update={"event_date": None, "category": Category.REMINDER, "published_at": published_at}
    )
    enrichment = DocumentEnrichment(
        headline="h", summary=summary, is_relevant=True, organization=Organization.OTHER
    )
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([undated]),
        enricher=FakeEnricher(responses={"a": enrichment}),
        documents=doc_repo,
    )
    await service.run(source.id)
    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    return upserted_doc.event_date


# Published Wed 2026-09-09 throughout, so relative terms resolve predictably:
# tonight/today -> 09-09, tomorrow -> 09-10, Friday (this week) -> 09-11.
_PUBLISHED_WED = datetime(2026, 9, 9, 18, 0, tzinfo=UTC)


async def test_relative_date_tonight_means_the_published_day() -> None:
    result = await _run_narrative_summary(
        "The board meeting is tonight at 7pm.", published_at=_PUBLISHED_WED
    )
    assert result == date(2026, 9, 9)


async def test_relative_date_tomorrow() -> None:
    result = await _run_narrative_summary(
        "The festival is tomorrow evening.", published_at=_PUBLISHED_WED
    )
    assert result == date(2026, 9, 10)


async def test_relative_date_bare_weekday_resolves_to_the_closest_occurrence() -> None:
    result = await _run_narrative_summary(
        "Please arrive by Friday morning.", published_at=_PUBLISHED_WED
    )
    assert result == date(2026, 9, 11)


async def test_relative_date_this_weekday_prefix() -> None:
    result = await _run_narrative_summary(
        "Sign up before this Friday.", published_at=_PUBLISHED_WED
    )
    assert result == date(2026, 9, 11)


async def test_relative_date_same_weekday_as_published_means_today() -> None:
    result = await _run_narrative_summary(
        "The event is Wednesday afternoon.", published_at=_PUBLISHED_WED
    )
    assert result == date(2026, 9, 9)


async def test_absolute_date_is_preferred_over_a_relative_one_in_the_same_text() -> None:
    result = await _run_narrative_summary(
        "Not tomorrow — the real date is September 20.", published_at=_PUBLISHED_WED
    )
    assert result == date(2026, 9, 20)


async def test_unfilled_placeholder_in_summary_blocks_a_structured_event_date() -> None:
    # Real bug (2026-09-17, the Parent Teacher Conference case): the model's
    # own summary admitted it didn't know the main subject's date
    # ("scheduled for [year]") while its structured event_date field still
    # carried a date borrowed from an unrelated "return forms by tomorrow"
    # detail. The left-in bracket is a strong signal to distrust it.
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    undated = _raw("a", domain=Domain.GMAIL).model_copy(
        update={"event_date": None, "category": Category.REMINDER}
    )
    enrichment = DocumentEnrichment(
        headline="PTC Schedule Now Open",
        summary=(
            "Parent-teacher conferences are scheduled for [year]. "
            "Forms must be returned by tomorrow."
        ),
        is_relevant=True,
        organization=Organization.FARIA_ADMIN,
        event_date=date(2026, 9, 18),  # wrongly borrowed from "tomorrow"
    )
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([undated]),
        enricher=FakeEnricher(responses={"a": enrichment}),
        documents=doc_repo,
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.event_date is None
    assert upserted_doc.category == Category.REMINDER  # never promoted to EVENT


async def test_unfilled_placeholder_in_summary_blocks_the_narrative_fallback_too() -> None:
    # Same guard, but for the case where the structured field was empty and
    # only the narrative-date fallback would have picked up the wrong date.
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    undated = _raw("a", domain=Domain.GMAIL).model_copy(
        update={
            "event_date": None,
            "category": Category.REMINDER,
            "published_at": datetime(2026, 9, 17, tzinfo=UTC),
        }
    )
    enrichment = DocumentEnrichment(
        headline="PTC Schedule Now Open",
        summary=(
            "Parent-teacher conferences are scheduled for [year]. "
            "Forms must be returned by tomorrow."
        ),
        is_relevant=True,
        organization=Organization.FARIA_ADMIN,
        event_date=None,  # nothing in the structured field this time
    )
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([undated]),
        enricher=FakeEnricher(responses={"a": enrichment}),
        documents=doc_repo,
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.event_date is None
    assert upserted_doc.category == Category.REMINDER


async def test_a_short_bracketed_citation_is_not_mistaken_for_a_placeholder() -> None:
    # "[c]" (a legal-citation-style bracket) must not trip the guard the way
    # a real placeholder like "[year]" does.
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    undated = _raw("a", domain=Domain.GMAIL).model_copy(
        update={"event_date": None, "category": Category.REMINDER}
    )
    enrichment = DocumentEnrichment(
        headline="h",
        summary="Voting concludes September 6th, per policy [c].",
        is_relevant=True,
        organization=Organization.OTHER,
        event_date=date(2026, 9, 6),
    )
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([undated]),
        enricher=FakeEnricher(responses={"a": enrichment}),
        documents=doc_repo,
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.event_date == date(2026, 9, 6)
    assert upserted_doc.category == Category.EVENT


async def test_no_narrative_date_leaves_event_date_none() -> None:
    source = _source(domain=Domain.GMAIL)
    doc_repo = FakeDocumentRepository()
    undated = _raw("a", domain=Domain.GMAIL).model_copy(
        update={"event_date": None, "category": Category.REMINDER}
    )
    enrichment = DocumentEnrichment(
        headline="h",
        summary="No specific action required.",
        is_relevant=True,
        organization=Organization.OTHER,
        event_date=None,
    )
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([undated]),
        enricher=FakeEnricher(responses={"a": enrichment}),
        documents=doc_repo,
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.event_date is None
    assert upserted_doc.category == Category.REMINDER


async def test_changed_gmail_content_does_call_the_enricher() -> None:
    source = _source(domain=Domain.GMAIL)
    prior = Document(
        id=uuid4(),
        source_id=source.id,
        domain=Domain.GMAIL,
        category=Category.EVENT,
        student_ref=None,
        title="Event a",
        content="OLD content, different from the new fetch.",
        headline="Stale headline",
        summary="Stale.",
        is_relevant=True,
        organization=Organization.FEEF,
        external_id="a",
        event_date=date(2026, 9, 20),
        published_at=None,
    )
    doc_repo = FakeDocumentRepository(stored=[prior])
    enricher = FakeEnricher()
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([_raw("a", domain=Domain.GMAIL)]),
        enricher=enricher,
        documents=doc_repo,
    )

    await service.run(source.id)

    assert enricher.calls == ["a"]


# ---------------------------------------------------------------------------
# enrichment — summary-only domains (built for cupertino_events, 2026-10-02)
#
# A narrower treatment than Gmail's: only headline/summary come from the
# LLM. event_date/category/organization are never touched here — the
# connector's own parser already set them deterministically, more reliably
# than an LLM re-guess over messy scraped prose (see _DETERMINISTIC_
# ORGANIZATION and _summarize_only's own docstring).
#
# _SUMMARY_ONLY_DOMAINS is empty since 2026-10-25 (Cupertino's calendar
# listing has a clean description of its own), but the mechanism is kept for
# the next scraped source — these tests opt Domain.CITY back in to cover it.
# ---------------------------------------------------------------------------


@pytest.fixture
def _city_is_summary_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "_SUMMARY_ONLY_DOMAINS", frozenset({Domain.CITY}))


async def test_city_docs_skip_the_llm_and_keep_the_connectors_own_summary() -> None:
    # Today's real behavior: no summary-only domains, so a city doc takes the
    # fully deterministic branch — headline = title, the connector-set
    # summary (the calendar listing's own description) untouched, no LLM.
    source = _source(domain=Domain.CITY)
    doc_repo = FakeDocumentRepository()
    enricher = FakeEnricher()
    raw = _city_raw("a").model_copy(update={"summary": "The listing's own description."})
    service, _, _ = _service(
        source=source, connector=FakeConnector([raw]), enricher=enricher, documents=doc_repo
    )

    await service.run(source.id)

    assert enricher.summarize_calls == [] and enricher.calls == []
    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.headline == "Event a"
    assert upserted_doc.summary == "The listing's own description."
    assert upserted_doc.organization == Organization.CITY_RECREATION


def _city_raw(external_id: str, *, event_date: date | None = date(2026, 9, 20)) -> RawDocument:
    return RawDocument(
        external_id=external_id,
        title=f"Event {external_id}",
        content=f"Event {external_id} — messy scraped prose, image.jpg, Register today!",
        domain=Domain.CITY,
        category=Category.EVENT,
        event_date=event_date,
    )


async def test_city_docs_get_summarized_before_upsert(_city_is_summary_only: None) -> None:
    source = _source(domain=Domain.CITY)
    doc_repo = FakeDocumentRepository()
    service, _, _ = _service(
        source=source, connector=FakeConnector([_city_raw("a")]), documents=doc_repo
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.headline == "headline: Event a"
    assert upserted_doc.summary is not None


async def test_city_docs_keep_the_parsers_own_event_date_and_category_and_organization(
    _city_is_summary_only: None,
) -> None:
    # The core guarantee of the "narrow" branch: unlike Gmail, the LLM's
    # own opinion (even if the fake enricher had one) is never consulted
    # for these fields at all — summarize() doesn't even return them.
    source = _source(domain=Domain.CITY)
    doc_repo = FakeDocumentRepository()
    service, _, _ = _service(
        source=source, connector=FakeConnector([_city_raw("a")]), documents=doc_repo
    )

    await service.run(source.id)

    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.event_date == date(2026, 9, 20)
    assert upserted_doc.category == Category.EVENT
    assert upserted_doc.organization == Organization.CITY_RECREATION


async def test_already_past_city_doc_skips_summarize_entirely(_city_is_summary_only: None) -> None:
    source = _source(domain=Domain.CITY)
    doc_repo = FakeDocumentRepository()
    enricher = FakeEnricher()
    past_doc = _city_raw("a", event_date=date(2026, 9, 1))  # NOW is 2026-09-12
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([past_doc]),
        enricher=enricher,
        documents=doc_repo,
    )

    await service.run(source.id)

    assert enricher.summarize_calls == []
    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.headline == "Event a"  # raw title, no LLM polish
    assert upserted_doc.is_relevant is True  # still relevant — just not shown (past date)


async def test_summarize_failure_falls_back_and_does_not_abort_the_run(
    _city_is_summary_only: None,
) -> None:
    source = _source(domain=Domain.CITY)
    doc_repo = FakeDocumentRepository()
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([_city_raw("a"), _city_raw("b")]),
        enricher=FakeEnricher(summarize_fail_for={"a"}),
        documents=doc_repo,
    )

    report = await service.run(source.id)

    assert report.status is SourceStatus.OK
    docs = {doc.external_id: doc for doc, _vector in doc_repo.upserts[0][1]}
    assert docs["a"].headline == "Event a"  # fallback: raw title
    assert docs["a"].organization == Organization.CITY_RECREATION  # still deterministic
    assert docs["b"].headline == "headline: Event b"  # unaffected


async def test_unchanged_city_content_reuses_prior_summary(_city_is_summary_only: None) -> None:
    source = _source(domain=Domain.CITY)
    prior = Document(
        id=uuid4(),
        source_id=source.id,
        domain=Domain.CITY,
        category=Category.EVENT,
        student_ref=None,
        title="Event a",
        content=_city_raw("a").content,
        headline="Cached headline",
        summary="Cached summary.",
        is_relevant=True,
        organization=Organization.CITY_RECREATION,
        external_id="a",
        event_date=date(2026, 9, 20),
        published_at=None,
    )
    doc_repo = FakeDocumentRepository(stored=[prior])
    enricher = FakeEnricher()
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([_city_raw("a")]),  # same content as `prior`
        enricher=enricher,
        documents=doc_repo,
    )

    await service.run(source.id)

    assert enricher.summarize_calls == []  # reused, not re-called
    upserted_doc, _vector = doc_repo.upserts[0][1][0]
    assert upserted_doc.headline == "Cached headline"
    assert upserted_doc.summary == "Cached summary."


async def test_changed_city_content_does_call_summarize(_city_is_summary_only: None) -> None:
    source = _source(domain=Domain.CITY)
    prior = Document(
        id=uuid4(),
        source_id=source.id,
        domain=Domain.CITY,
        category=Category.EVENT,
        student_ref=None,
        title="Event a",
        content="OLD content, different from the new fetch.",
        headline="Stale headline",
        summary="Stale.",
        is_relevant=True,
        organization=Organization.CITY_RECREATION,
        external_id="a",
        event_date=date(2026, 9, 20),
        published_at=None,
    )
    doc_repo = FakeDocumentRepository(stored=[prior])
    enricher = FakeEnricher()
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([_city_raw("a")]),
        enricher=enricher,
        documents=doc_repo,
    )

    await service.run(source.id)

    assert enricher.summarize_calls == ["a"]


async def test_embedder_failure_is_recorded() -> None:
    source = _source()
    service, src_repo, _ = _service(
        source=source,
        connector=FakeConnector([_raw("a")]),
        embedder=FakeEmbedder(fail=True),
    )

    report = await service.run(source.id)

    assert report.status is SourceStatus.ERROR
    assert src_repo.runs == [(source.id, SourceStatus.ERROR)]


async def test_embedder_vector_count_mismatch_is_recorded() -> None:
    source = _source()
    service, _, _ = _service(
        source=source,
        connector=FakeConnector([_raw("a"), _raw("b")]),
        embedder=FakeEmbedder(dim=4, vector_count=1),  # 1 vector for 2 docs
    )

    report = await service.run(source.id)

    assert report.status is SourceStatus.ERROR


async def test_empty_fetch_still_marks_ok() -> None:
    source = _source()
    service, _, doc_repo = _service(source=source, connector=FakeConnector([]))

    report = await service.run(source.id)

    assert report.status is SourceStatus.OK
    assert report.fetched == 0
    assert doc_repo.upserts == [(source.id, [])]


async def test_upsert_failure_is_recorded() -> None:
    source = _source()
    doc_repo = FakeDocumentRepository()
    doc_repo.raise_on_upsert = RuntimeError("deadlock")
    service, _, _ = _service(
        source=source, connector=FakeConnector([_raw("a")]), documents=doc_repo
    )

    report = await service.run(source.id)

    assert report.status is SourceStatus.ERROR
    assert report.error is not None and "deadlock" in report.error


async def test_duration_is_recorded() -> None:
    source = _source()
    service, _, _ = _service(source=source, connector=FakeConnector([_raw("a")]))

    report = await service.run(source.id)

    assert report.duration_ms >= 0


async def test_unknown_source_raises() -> None:
    # get() is before the try/except: no row means no report and nothing to mark.
    service, _, _ = _service(
        source=_source(),
        connector=FakeConnector([]),
        sources=FakeSourceRepository(),  # empty
    )
    with pytest.raises(SourceNotFoundError):
        await service.run(uuid4())
