"""API routes — dependencies overridden with fakes, no DB, no lifespan."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from acamar.api import dependencies
from acamar.api.app import create_app
from acamar.domain.enums import Category, Domain, OutOfScopeReason, QueryIntent, SourceStatus
from acamar.domain.models import Document, Source, Subscriber
from acamar.reasoning.graph import build_graph
from acamar.reasoning.nodes.count_answer import CountAnswerNode
from acamar.reasoning.nodes.critique import CritiqueNode, CritiqueVerdict
from acamar.reasoning.nodes.retrieval import RetrievalNode
from acamar.reasoning.nodes.retrieval_grade import RetrievalGrade, RetrievalGradeNode
from acamar.reasoning.nodes.router import RouteDecision, RouterNode
from acamar.reasoning.nodes.synthesis import SynthesisNode, SynthesisOutput
from acamar.reasoning.nodes.weather_answer import WeatherAnswerNode
from acamar.security import hash_token
from tests.fakes import (
    FakeChatModel,
    FakeDocumentRepository,
    FakeEmbedder,
    FakeSourceRepository,
    FakeSubscriberRepository,
    FakeWeatherProvider,
    FrozenClock,
)

TOKEN = "acamar_test-token"
CLOCK = FrozenClock(datetime(2026, 9, 9, 8, 0, tzinfo=UTC))
SOURCE = Source(
    id=uuid4(),
    school_id=uuid4(),
    domain=Domain.SCHOOL,
    category=Category.EVENT,
    last_status=SourceStatus.OK,
    last_run_at=datetime(2026, 9, 7, tzinfo=UTC),
)


def _event(title: str, when: date) -> Document:
    return Document(
        id=uuid4(),
        source_id=SOURCE.id,
        domain=Domain.SCHOOL,
        category=Category.EVENT,
        student_ref=None,
        title=title,
        content=f"Faria Elementary School event: {title} — all day on {when}.",
        headline=None,
        summary=None,
        is_relevant=True,
        organization=None,
        external_id=title,
        event_date=when,
        published_at=None,
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    subscribers = FakeSubscriberRepository(
        {hash_token(TOKEN): Subscriber(id=uuid4(), label="me", created_at=CLOCK.now())}
    )
    documents = FakeDocumentRepository([_event("FEEF Meeting", date(2026, 9, 9))])
    app.dependency_overrides[dependencies.get_subscriber_repository] = lambda: subscribers
    app.dependency_overrides[dependencies.get_document_repository] = lambda: documents
    app.dependency_overrides[dependencies.get_source_repository] = (
        lambda: FakeSourceRepository(SOURCE)
    )
    app.dependency_overrides[dependencies.get_clock] = lambda: CLOCK
    yield TestClient(app)  # no `with` → lifespan (and the real pool) never runs
    app.dependency_overrides.clear()


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_needs_no_token(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_dashboard_without_a_token_is_401(client: TestClient) -> None:
    assert client.get("/dashboard").status_code == 401


def test_dashboard_with_a_bad_token_is_401(client: TestClient) -> None:
    assert client.get("/dashboard", headers=_auth("nope")).status_code == 401


def test_dashboard_returns_the_today_view(client: TestClient) -> None:
    body = client.get("/dashboard", headers=_auth()).json()
    assert body["as_of"] == "2026-09-09"
    assert [e["title"] for e in body["events"]] == ["FEEF Meeting"]
    assert body["menu"] == []
    assert body["reminders"] == []


def test_dashboard_days_param_is_validated(client: TestClient) -> None:
    assert client.get("/dashboard?days=0", headers=_auth()).status_code == 422
    assert client.get("/dashboard?days=90", headers=_auth()).status_code == 422


def test_source_status(client: TestClient) -> None:
    body = client.get(f"/sources/{SOURCE.id}/status", headers=_auth()).json()
    assert body["last_status"] == "ok"
    assert body["domain"] == "school"


def test_unknown_source_is_404(client: TestClient) -> None:
    resp = client.get(f"/sources/{uuid4()}/status", headers=_auth())
    assert resp.status_code == 404


def test_dismiss_removes_the_item_from_the_dashboard(client: TestClient) -> None:
    item_id = client.get("/dashboard", headers=_auth()).json()["events"][0]["id"]

    resp = client.post(f"/dashboard/items/{item_id}/dismiss", headers=_auth())

    assert resp.status_code == 204
    assert client.get("/dashboard", headers=_auth()).json()["events"] == []


def test_dismiss_unknown_item_is_404(client: TestClient) -> None:
    resp = client.post(f"/dashboard/items/{uuid4()}/dismiss", headers=_auth())
    assert resp.status_code == 404


def test_dismiss_without_a_token_is_401(client: TestClient) -> None:
    resp = client.post(f"/dashboard/items/{uuid4()}/dismiss")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /query — one fake graph per test, overriding get_reasoning_graph
# directly (bypasses its own Depends() chain entirely, same idea as every
# other override above).
# ---------------------------------------------------------------------------


def _use_graph(
    client: TestClient,
    *,
    route: RouteDecision,
    synthesis: SynthesisOutput | None = None,
    critique: CritiqueVerdict | None = None,
    documents: list[Document] | None = None,
    weather: FakeWeatherProvider | None = None,
) -> None:
    chat_model = FakeChatModel(
        {
            RouteDecision: route,
            RetrievalGrade: RetrievalGrade(relevant=True, reason="ok"),
            SynthesisOutput: synthesis or SynthesisOutput(answer="x"),
            CritiqueVerdict: critique or CritiqueVerdict(grounded=True, reason="ok"),
        }
    )
    doc_repo = FakeDocumentRepository(documents or [])
    graph = build_graph(
        router=RouterNode(chat_model),
        retrieval=RetrievalNode(FakeEmbedder(), doc_repo),
        retrieval_grade=RetrievalGradeNode(chat_model),
        synthesis=SynthesisNode(chat_model),
        critique=CritiqueNode(chat_model),
        weather_answer=WeatherAnswerNode(weather or FakeWeatherProvider()),
        count_answer=CountAnswerNode(doc_repo),
    )
    client.app.dependency_overrides[dependencies.get_reasoning_graph] = lambda: graph  # type: ignore[attr-defined]


def test_query_without_a_token_is_401(client: TestClient) -> None:
    resp = client.post("/query", json={"question": "what's for lunch?"})
    assert resp.status_code == 401


def test_query_document_intent_returns_answer_and_sources(client: TestClient) -> None:
    _use_graph(
        client,
        route=RouteDecision(
            intent=QueryIntent.DOCUMENT, categories=[Category.EVENT],
            date_from=date(2026, 9, 9), date_to=date(2026, 9, 9),
        ),
        synthesis=SynthesisOutput(answer="The FEEF Meeting is today."),
        documents=[_event("FEEF Meeting", date(2026, 9, 9))],
    )

    body = client.post("/query", json={"question": "what's today?"}, headers=_auth()).json()

    assert body["answer"] == "The FEEF Meeting is today."
    assert body["grounded"] is True
    assert [s["title"] for s in body["sources"]] == ["FEEF Meeting"]


def test_query_records_its_routed_topic_into_cockpit_state(client: TestClient) -> None:
    # The Router's intent/category decision lives only inside this call's
    # graph state — nothing else captures it (unlike method/path/duration,
    # already generic via TrafficMiddleware). Feeds the Pilot Cockpit's
    # "most-queried topics" chart.
    _use_graph(
        client,
        route=RouteDecision(
            intent=QueryIntent.DOCUMENT, categories=[Category.EVENT],
            date_from=date(2026, 9, 9), date_to=date(2026, 9, 9),
        ),
        documents=[_event("FEEF Meeting", date(2026, 9, 9))],
    )

    client.post("/query", json={"question": "what's today?"}, headers=_auth())

    [recorded] = client.app.state.cockpit.recent_queries()  # type: ignore[attr-defined]
    assert recorded.question == "what's today?"
    assert recorded.intent == "document"
    assert recorded.categories == ["event"]
    assert recorded.grounded is True


def test_query_weather_intent_returns_an_answer_with_no_sources(client: TestClient) -> None:
    _use_graph(client, route=RouteDecision(intent=QueryIntent.WEATHER))

    body = client.post("/query", json={"question": "will it rain?"}, headers=_auth()).json()

    assert "°F" in body["answer"]
    assert body["sources"] == []
    assert body["grounded"] is True


def test_query_count_intent_returns_an_exact_number(client: TestClient) -> None:
    _use_graph(
        client,
        route=RouteDecision(
            intent=QueryIntent.COUNT, categories=[Category.EVENT],
            date_from=date(2026, 9, 1), date_to=date(2026, 9, 30),
        ),
        documents=[_event("FEEF Meeting", date(2026, 9, 9))],
    )

    body = client.post(
        "/query", json={"question": "how many events?"}, headers=_auth()
    ).json()

    assert "1" in body["answer"]


def test_query_out_of_scope_declines(client: TestClient) -> None:
    _use_graph(
        client,
        route=RouteDecision(
            intent=QueryIntent.OUT_OF_SCOPE, out_of_scope_reason=OutOfScopeReason.NEVER
        ),
    )

    body = client.post(
        "/query", json={"question": "help with homework?"}, headers=_auth()
    ).json()

    assert "not something I can help with" in body["answer"]


class _BrokenGraph:
    """A graph that builds fine but fails mid-run — the case NF1 covers:
    the failure has to happen inside `ainvoke`, not during dependency
    construction, or the route handler's try/except never runs at all."""

    async def ainvoke(self, state: object) -> None:
        raise RuntimeError("boom")


def test_query_graph_failure_is_502(client: TestClient) -> None:
    client.app.dependency_overrides[dependencies.get_reasoning_graph] = _BrokenGraph  # type: ignore[attr-defined]

    resp = client.post("/query", json={"question": "what's today?"}, headers=_auth())

    assert resp.status_code == 502
