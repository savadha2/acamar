"""Pilot Cockpit admin routes: menu upload, traffic/health snapshot, pause/kill.

Gated by `CurrentAdmin` — a separate credential from the `subscribers` pool
the rest of the API uses (see `api/auth.py`). Everything here is also a plain
HTTP endpoint reachable by curl/a script, not only the cockpit UI.
"""

from __future__ import annotations

import calendar
import os
import signal
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status

from acamar.api.auth import CurrentAdmin
from acamar.api.dependencies import (
    get_cockpit_state,
    get_document_repository,
    get_ingestion_service,
    get_source_repository,
)
from acamar.api.middleware import CockpitState
from acamar.api.schemas import (
    AdminStatusResponse,
    MenuCoverageResponse,
    PauseStateResponse,
    QueryTopicResponse,
    SourceHealthResponse,
    TrafficRecordResponse,
)
from acamar.config import get_settings
from acamar.domain.models import IngestionReport
from acamar.ingestion.service import IngestionService
from acamar.ports.repositories import DocumentRepository, SourceRepository

router = APIRouter(prefix="/admin", tags=["admin"])

_SHUTDOWN_CONFIRMATION = "SHUTDOWN"


@router.post("/menu-upload")
async def upload_menu(
    _: CurrentAdmin,
    file: UploadFile,
    source_id: UUID,
    meal: Literal["breakfast", "lunch"],
    year: int,
    month: int,
    ingestion: Annotated[IngestionService, Depends(get_ingestion_service)],
) -> IngestionReport:
    """Stage an uploaded menu PDF and immediately run ingestion for it.

    ``source_id`` must be the ``(school, menu)`` manual-menu source's row id
    — passed explicitly rather than assumed, so this doesn't have to guess
    which source an upload belongs to as more manual sources are added.
    ``year``/``month`` are required because the PDF itself never states its
    own month (confirmed against real vendor samples — see
    ``sources.manual_menu.parser``); it can't be inferred from content.
    """
    if file.content_type != "application/pdf":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "only a PDF file is accepted")
    body = await file.read()
    meal_dir = Path(get_settings().cockpit_upload_dir) / meal
    meal_dir.mkdir(parents=True, exist_ok=True)
    (meal_dir / f"{year:04d}-{month:02d}.pdf").write_bytes(body)

    return await ingestion.run(source_id)


@router.get("/status")
async def admin_status(
    _: CurrentAdmin,
    sources: Annotated[SourceRepository, Depends(get_source_repository)],
    cockpit: Annotated[CockpitState, Depends(get_cockpit_state)],
) -> AdminStatusResponse:
    """Sources health + the in-memory recent-traffic view, for the cockpit."""
    return AdminStatusResponse(
        paused=cockpit.paused,
        sources=[
            SourceHealthResponse(
                id=s.id,
                domain=s.domain,
                category=s.category,
                last_status=s.last_status,
                last_run_at=s.last_run_at,
            )
            for s in await sources.list_all()
        ],
        recent_requests=[
            TrafficRecordResponse(
                at=r.at,
                method=r.method,
                path=r.path,
                status_code=r.status_code,
                duration_ms=r.duration_ms,
            )
            for r in cockpit.recent()
        ],
        recent_queries=[
            QueryTopicResponse(
                at=q.at,
                question=q.question,
                intent=q.intent,
                categories=q.categories,
                grounded=q.grounded,
            )
            for q in cockpit.recent_queries()
        ],
    )


@router.get("/menu-coverage")
async def menu_coverage(
    _: CurrentAdmin,
    source_id: UUID,
    meal: Literal["breakfast", "lunch"],
    year: int,
    month: int,
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
) -> MenuCoverageResponse:
    """Which days of (meal, year, month) are already ingested for `source_id`.

    Feeds the upload panel's "currently on file" chip and per-day coverage
    strip — the concrete, checkable answer to "I uploaded but it still shows
    up in Menu PDF", not just a success toast. Reuses
    `DocumentRepository.get_existing` (already the ingestion pipeline's own
    dedup check) rather than a new query — `manual_menu`'s
    `external_id` convention (`menu::{meal}::{date}`, see
    `sources.manual_menu.parser`) is baked in here since this endpoint only
    ever makes sense for that one connector's upload flow.
    """
    days_in_month = calendar.monthrange(year, month)[1]
    external_ids = [
        f"menu::{meal}::{date(year, month, day).isoformat()}"
        for day in range(1, days_in_month + 1)
    ]
    existing = await documents.get_existing(source_id, external_ids)
    covered_days = [
        day
        for day in range(1, days_in_month + 1)
        if f"menu::{meal}::{date(year, month, day).isoformat()}" in existing
    ]
    return MenuCoverageResponse(days_in_month=days_in_month, covered_days=covered_days)


@router.post("/pause")
async def pause(
    _: CurrentAdmin, cockpit: Annotated[CockpitState, Depends(get_cockpit_state)]
) -> PauseStateResponse:
    """Reversible soft-stop: every non-admin route 503s until `/admin/resume`."""
    cockpit.paused = True
    return PauseStateResponse(paused=True)


@router.post("/resume")
async def resume(
    _: CurrentAdmin, cockpit: Annotated[CockpitState, Depends(get_cockpit_state)]
) -> PauseStateResponse:
    """Undoes `/admin/pause`."""
    cockpit.paused = False
    return PauseStateResponse(paused=False)


@router.post("/shutdown", status_code=status.HTTP_202_ACCEPTED)
async def shutdown(_: CurrentAdmin, confirm: str) -> dict[str, str]:
    """Hard stop: sends this process SIGTERM.

    Irreversible from the cockpit — nothing supervises the API today, so a
    terminal restart is the only way back (see the Pilot Cockpit design
    notes). ``confirm`` must be the literal string "SHUTDOWN", a heavier
    confirmation than Pause/Resume for a control this destructive.
    """
    if confirm != _SHUTDOWN_CONFIRMATION:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f'confirm must be "{_SHUTDOWN_CONFIRMATION}"'
        )
    os.kill(os.getpid(), signal.SIGTERM)
    return {"detail": "shutdown signal sent", "at": datetime.now(UTC).isoformat()}
