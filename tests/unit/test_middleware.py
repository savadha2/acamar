"""TrafficMiddleware / CockpitState — the Pilot Cockpit's traffic snapshot + pause gate."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from acamar.api.middleware import CockpitState, TrafficMiddleware


@pytest.fixture
def cockpit() -> CockpitState:
    return CockpitState(max_recent=3)


@pytest.fixture
def client(cockpit: CockpitState) -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(TrafficMiddleware, state=cockpit)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/dashboard")
    def dashboard() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/admin/status")
    def admin_status() -> dict[str, str]:
        return {"status": "ok"}

    yield TestClient(app)


def test_records_method_path_and_status(client: TestClient, cockpit: CockpitState) -> None:
    client.get("/dashboard")

    [record] = cockpit.recent()
    assert record.method == "GET"
    assert record.path == "/dashboard"
    assert record.status_code == 200
    assert record.duration_ms >= 0


def test_ring_buffer_keeps_only_the_most_recent_n(
    client: TestClient, cockpit: CockpitState
) -> None:
    for _ in range(5):
        client.get("/dashboard")

    assert len(cockpit.recent()) == 3


def test_recent_is_most_recent_first(client: TestClient, cockpit: CockpitState) -> None:
    client.get("/health")
    client.get("/dashboard")

    paths = [r.path for r in cockpit.recent()]
    assert paths == ["/dashboard", "/health"]


def test_paused_blocks_ordinary_routes_with_503(
    client: TestClient, cockpit: CockpitState
) -> None:
    cockpit.paused = True

    response = client.get("/dashboard")

    assert response.status_code == 503


def test_paused_still_allows_admin_and_health_routes(
    client: TestClient, cockpit: CockpitState
) -> None:
    cockpit.paused = True

    assert client.get("/health").status_code == 200
    assert client.get("/admin/status").status_code == 200


def test_a_blocked_request_is_still_recorded(client: TestClient, cockpit: CockpitState) -> None:
    cockpit.paused = True

    client.get("/dashboard")

    [record] = cockpit.recent()
    assert record.status_code == 503


def test_unpausing_restores_normal_routes(client: TestClient, cockpit: CockpitState) -> None:
    cockpit.paused = True
    cockpit.paused = False

    assert client.get("/dashboard").status_code == 200
