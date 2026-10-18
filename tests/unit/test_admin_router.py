"""`/admin/*` routes — dependencies overridden with fakes, no DB, no lifespan."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from acamar.api import dependencies
from acamar.api.app import create_app
from acamar.config import get_settings
from acamar.domain.enums import Category, Domain, SourceStatus
from acamar.domain.models import Document, IngestionReport, Source
from tests.fakes import FakeDocumentRepository, FakeSourceRepository

ADMIN_TOKEN = "cockpit-secret"
SOURCE = Source(
    id=uuid4(),
    school_id=uuid4(),
    domain=Domain.SCHOOL,
    category=Category.MENU,
    last_status=SourceStatus.OK,
    last_run_at=datetime(2026, 9, 25, tzinfo=UTC),
)


class _FakeIngestionService:
    def __init__(self, report: IngestionReport | None = None) -> None:
        self._report = report or IngestionReport(
            source_id=SOURCE.id, source="school/menu", status=SourceStatus.OK,
            fetched=20, upserted=20,
        )
        self.calls: list[object] = []

    async def run(self, source_id: object) -> IngestionReport:
        self.calls.append(source_id)
        return self._report


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COCKPIT_TOKEN", ADMIN_TOKEN)
    get_settings.cache_clear()


@pytest.fixture
def documents() -> FakeDocumentRepository:
    return FakeDocumentRepository()


@pytest.fixture
def client(documents: FakeDocumentRepository) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[dependencies.get_source_repository] = (
        lambda: FakeSourceRepository(SOURCE)
    )
    app.dependency_overrides[dependencies.get_ingestion_service] = _FakeIngestionService
    app.dependency_overrides[dependencies.get_document_repository] = lambda: documents
    yield TestClient(app)
    app.dependency_overrides.clear()


def _auth(token: str = ADMIN_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_admin_routes_without_a_token_are_401(client: TestClient) -> None:
    assert client.get("/admin/status").status_code == 401
    assert client.post("/admin/pause").status_code == 401


def test_admin_routes_reject_a_subscriber_style_wrong_token(client: TestClient) -> None:
    assert client.get("/admin/status", headers=_auth("not-the-admin-token")).status_code == 401


def test_status_reports_sources_and_pause_state(client: TestClient) -> None:
    body = client.get("/admin/status", headers=_auth()).json()

    assert body["paused"] is False
    assert body["sources"][0]["domain"] == "school"
    assert body["sources"][0]["category"] == "menu"
    assert body["recent_requests"] == []
    assert body["recent_queries"] == []


def test_pause_then_status_reflects_it(client: TestClient) -> None:
    client.post("/admin/pause", headers=_auth())

    body = client.get("/admin/status", headers=_auth()).json()

    assert body["paused"] is True


def test_resume_undoes_pause(client: TestClient) -> None:
    client.post("/admin/pause", headers=_auth())

    client.post("/admin/resume", headers=_auth())

    assert client.get("/admin/status", headers=_auth()).json()["paused"] is False


def test_menu_upload_requires_a_pdf_content_type(client: TestClient) -> None:
    response = client.post(
        "/admin/menu-upload",
        headers=_auth(),
        params={"source_id": str(SOURCE.id), "meal": "lunch", "year": 2026, "month": 9},
        files={"file": ("menu.txt", b"not a pdf", "text/plain")},
    )

    assert response.status_code == 400


def test_menu_upload_stages_the_file_and_runs_ingestion(
    client: TestClient, tmp_path: Path
) -> None:
    with patch("acamar.api.routers.admin.get_settings") as mock_settings:
        mock_settings.return_value.cockpit_upload_dir = str(tmp_path)
        response = client.post(
            "/admin/menu-upload",
            headers=_auth(),
            params={"source_id": str(SOURCE.id), "meal": "lunch", "year": 2026, "month": 9},
            files={"file": ("menu.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["upserted"] == 20
    staged = tmp_path / "lunch" / "2026-09.pdf"
    assert staged.read_bytes() == b"%PDF-1.4 fake"


def test_shutdown_requires_the_confirmation_phrase(client: TestClient) -> None:
    response = client.post(
        "/admin/shutdown", headers=_auth(), params={"confirm": "nope"}
    )

    assert response.status_code == 400


def test_shutdown_sends_sigterm_when_confirmed(client: TestClient) -> None:
    with patch("acamar.api.routers.admin.os.kill") as mock_kill:
        response = client.post(
            "/admin/shutdown", headers=_auth(), params={"confirm": "SHUTDOWN"}
        )

    assert response.status_code == 202
    mock_kill.assert_called_once()


def _menu_doc(external_id: str) -> Document:
    return Document(
        id=uuid4(), source_id=SOURCE.id, domain=Domain.SCHOOL, category=Category.MENU,
        student_ref=None, title="Lunch Menu", content="lunch", headline=None, summary=None,
        is_relevant=True, organization=None, external_id=external_id,
        event_date=None, published_at=None,
    )


def test_menu_coverage_reports_which_days_are_ingested(
    client: TestClient, documents: FakeDocumentRepository
) -> None:
    documents.stored = [
        _menu_doc("menu::lunch::2026-09-01"),
        _menu_doc("menu::lunch::2026-09-14"),
        _menu_doc("menu::breakfast::2026-09-01"),  # different meal — must not count for lunch
    ]

    body = client.get(
        "/admin/menu-coverage",
        headers=_auth(),
        params={"source_id": str(SOURCE.id), "meal": "lunch", "year": 2026, "month": 9},
    ).json()

    assert body["days_in_month"] == 30
    assert body["covered_days"] == [1, 14]


def test_menu_coverage_is_empty_before_any_upload(client: TestClient) -> None:
    body = client.get(
        "/admin/menu-coverage",
        headers=_auth(),
        params={"source_id": str(SOURCE.id), "meal": "lunch", "year": 2026, "month": 10},
    ).json()

    assert body["days_in_month"] == 31
    assert body["covered_days"] == []


def test_menu_coverage_without_a_token_is_401(client: TestClient) -> None:
    response = client.get(
        "/admin/menu-coverage",
        params={"source_id": str(SOURCE.id), "meal": "lunch", "year": 2026, "month": 9},
    )
    assert response.status_code == 401
