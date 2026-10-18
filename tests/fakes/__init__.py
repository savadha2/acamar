"""In-memory test doubles for the ingestion ports."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel

from acamar.domain.enums import Category, Domain, Organization, SourceStatus
from acamar.domain.models import Document, RawDocument, Source, Subscriber, Vector
from acamar.ports.chat_model import ChatModelError
from acamar.ports.embedder import EmbeddingError
from acamar.ports.enricher import ContentSummary, DocumentEnrichment, EnrichmentError
from acamar.ports.mail_client import RawEmail
from acamar.ports.repositories import SourceNotFoundError
from acamar.ports.weather import WeatherError, WeatherForecast

_T = TypeVar("_T", bound=BaseModel)


class FrozenClock:
    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def today(self) -> date:
        return self._moment.date()

    def advance(self, delta: timedelta) -> None:
        """Move the frozen instant forward — for TTL/expiry tests."""
        self._moment += delta


class FakeEmbedder:
    """Returns a deterministic vector per text. `vector_count` overrides the
    number of vectors returned (to simulate a broken model).
    """

    def __init__(
        self, *, dim: int = 768, fail: bool = False, vector_count: int | None = None
    ) -> None:
        self._dim = dim
        self._fail = fail
        self._vector_count = vector_count
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[Vector]:
        self.calls.append(texts)
        if self._fail:
            raise EmbeddingError("fake embedder failure")
        n = len(texts) if self._vector_count is None else self._vector_count
        return [[float(i)] * self._dim for i in range(n)]


class FakeEnricher:
    """Returns a canned `DocumentEnrichment`/`ContentSummary` per doc, or
    fails for chosen ids.

    `responses`/`fail_for` govern `enrich()`; `summarize_responses`/
    `summarize_fail_for` govern `summarize()` independently (a test can make
    one method fail without affecting the other).
    """

    def __init__(
        self,
        *,
        responses: dict[str, DocumentEnrichment] | None = None,
        fail_for: set[str] | None = None,
        summarize_responses: dict[str, ContentSummary] | None = None,
        summarize_fail_for: set[str] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._fail_for = fail_for or set()
        self._summarize_responses = summarize_responses or {}
        self._summarize_fail_for = summarize_fail_for or set()
        self.calls: list[str] = []
        self.summarize_calls: list[str] = []

    async def enrich(self, doc: RawDocument) -> DocumentEnrichment:
        self.calls.append(doc.external_id)
        if doc.external_id in self._fail_for:
            raise EnrichmentError("fake enricher failure")
        if doc.external_id in self._responses:
            return self._responses[doc.external_id]
        return DocumentEnrichment(
            headline=f"headline: {doc.title[:20]}",
            summary=f"summary of: {doc.content[:20]}",
            is_relevant=True,
            organization=Organization.OTHER,
        )

    async def summarize(self, doc: RawDocument) -> ContentSummary:
        self.summarize_calls.append(doc.external_id)
        if doc.external_id in self._summarize_fail_for:
            raise EnrichmentError("fake enricher summarize failure")
        if doc.external_id in self._summarize_responses:
            return self._summarize_responses[doc.external_id]
        return ContentSummary(
            headline=f"headline: {doc.title[:20]}",
            summary=f"summary of: {doc.content[:20]}",
        )


class FakeSourceRepository:
    def __init__(self, *sources: Source) -> None:
        self._by_id = {s.id: s for s in sources}
        self.runs: list[tuple[UUID, SourceStatus]] = []

    async def get(self, source_id: UUID) -> Source:
        try:
            return self._by_id[source_id]
        except KeyError:
            raise SourceNotFoundError(str(source_id)) from None

    async def mark_run(
        self, source_id: UUID, status: SourceStatus, *, at: datetime
    ) -> None:
        self.runs.append((source_id, status))

    async def list_all(self) -> list[Source]:
        return list(self._by_id.values())


_DATE_MIN = date(1, 1, 1)
_DATE_MAX = date(9999, 12, 31)


def _matches(
    doc: Document,
    categories: list[Category],
    domains: list[Domain] | None,
    date_from: date | None,
    date_to: date | None,
) -> bool:
    """Same semantics as the real `_SEARCH`/`_COUNT` SQL — kept as one
    helper so `search()` and `count()` can't silently drift apart."""
    if doc.category not in categories:
        return False
    if domains is not None and doc.domain not in domains:
        return False
    if doc.event_date is None:
        return True
    return (date_from or _DATE_MIN) <= doc.event_date <= (date_to or _DATE_MAX)


class FakeDocumentRepository:
    def __init__(self, stored: list[Document] | None = None) -> None:
        self.stored = stored if stored is not None else []
        self.upserts: list[tuple[UUID, list[tuple[RawDocument, Vector]]]] = []
        self.reconciles: list[tuple[UUID, datetime, date, timedelta]] = []
        self.reconcile_result = 0
        self.raise_on_upsert: Exception | None = None
        self.dismissed: set[UUID] = set()
        self.search_calls: list[dict[str, object]] = []
        """Recorded kwargs per `search()` call — asserts the retry-widening
        table (docs/reasoning-pipeline-design.md §2.4) reached the *n*th
        attempt's actual filter, not just that `search` was called."""

    async def upsert(
        self, source_id: UUID, documents: list[tuple[RawDocument, Vector]]
    ) -> int:
        if self.raise_on_upsert is not None:
            raise self.raise_on_upsert
        self.upserts.append((source_id, documents))
        return len(documents)

    async def reconcile_deletions(
        self,
        source_id: UUID,
        *,
        run_started_at: datetime,
        on_or_after: date,
        grace: timedelta = timedelta(),
    ) -> int:
        self.reconciles.append((source_id, run_started_at, on_or_after, grace))
        return self.reconcile_result

    async def search(
        self,
        embedding: Vector,
        *,
        categories: list[Category],
        domains: list[Domain] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 8,
    ) -> list[Document]:
        self.search_calls.append(
            {
                "categories": categories,
                "domains": domains,
                "date_from": date_from,
                "date_to": date_to,
                "limit": limit,
            }
        )
        return [
            d
            for d in self.stored
            if d.id not in self.dismissed and _matches(d, categories, domains, date_from, date_to)
        ][:limit]

    async def count(
        self,
        *,
        categories: list[Category],
        domains: list[Domain] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> int:
        return sum(
            1
            for d in self.stored
            if d.id not in self.dismissed
            and _matches(d, categories, domains, date_from, date_to)
        )

    async def list_documents(
        self,
        *,
        category: Category,
        date_from: date,
        date_to: date,
        undated_since: date | None = None,
        limit: int = 50,
    ) -> list[Document]:
        def _keeps(d: Document) -> bool:
            if d.id in self.dismissed:
                return False
            if d.event_date is not None:
                return date_from <= d.event_date <= date_to
            if undated_since is None:
                return True
            return d.published_at is None or d.published_at.date() >= undated_since

        matches = [d for d in self.stored if d.category == category and _keeps(d)]
        return matches[:limit]

    async def get_existing(
        self, source_id: UUID, external_ids: list[str]
    ) -> dict[str, Document]:
        return {
            d.external_id: d
            for d in self.stored
            if d.source_id == source_id and d.external_id in external_ids
        }

    async def dismiss(self, document_id: UUID) -> bool:
        if document_id in self.dismissed or not any(d.id == document_id for d in self.stored):
            return False
        self.dismissed.add(document_id)
        return True

    async def next_event_date(self, *, category: Category, after: date) -> date | None:
        candidates = [
            d.event_date
            for d in self.stored
            if d.id not in self.dismissed
            and d.category == category
            and d.event_date is not None
            and d.event_date > after
        ]
        return min(candidates) if candidates else None


class FakeSubscriberRepository:
    def __init__(self, subscribers: dict[str, Subscriber] | None = None) -> None:
        self._by_hash = subscribers or {}

    async def active_by_token_hash(self, token_hash: str) -> Subscriber | None:
        return self._by_hash.get(token_hash)


class FakeConnector:
    name = "fake"

    def __init__(
        self, docs: list[RawDocument] | None = None, error: Exception | None = None
    ) -> None:
        self._docs = docs if docs is not None else []
        self._error = error

    async def fetch(self) -> list[RawDocument]:
        if self._error is not None:
            raise self._error
        return self._docs


class FakeChatModel:
    """Returns a canned structured response per requested schema type.

    Keyed by type, not call order — order varies across a CRAG retry loop
    and shouldn't be baked into every test. A list value is popped in call
    order (for tests where the same schema is requested more than once,
    e.g. Critique across retries needing a different verdict each time).
    """

    def __init__(self, responses: dict[type[BaseModel], BaseModel | list[BaseModel]]) -> None:
        self._responses = responses
        self.calls: list[type[BaseModel]] = []
        self.prompts: list[str] = []

    async def structured(self, prompt: str, *, schema: type[_T]) -> _T:
        self.calls.append(schema)
        self.prompts.append(prompt)
        try:
            response = self._responses[schema]
        except KeyError:
            raise ChatModelError(f"fake chat model has no response queued for {schema}") from None
        if isinstance(response, list):
            if not response:
                raise ChatModelError(f"fake chat model exhausted its queued responses for {schema}")
            result = response.pop(0)
        else:
            result = response
        assert isinstance(result, schema)
        return result


class FakeWeatherProvider:
    """Returns a canned forecast, or raises `WeatherError` when `fail=True`."""

    def __init__(self, forecast: WeatherForecast | None = None, *, fail: bool = False) -> None:
        self._forecast = forecast or WeatherForecast(
            temp_high_f=70.0,
            temp_low_f=50.0,
            precipitation_probability=10.0,
            condition="clear",
        )
        self._fail = fail
        self.calls = 0

    async def today(self) -> WeatherForecast:
        self.calls += 1
        if self._fail:
            raise WeatherError("fake weather failure")
        return self._forecast


class FakeMailClient:
    """In-memory `MailClient` — ignores `query`, returns whatever is preloaded."""

    def __init__(
        self, emails: list[RawEmail] | None = None, error: Exception | None = None
    ) -> None:
        self._emails = emails if emails is not None else []
        self._error = error
        self.queries: list[tuple[str, int]] = []

    async def search(self, query: str, *, max_results: int = 20) -> list[RawEmail]:
        self.queries.append((query, max_results))
        if self._error is not None:
            raise self._error
        return self._emails[:max_results]
