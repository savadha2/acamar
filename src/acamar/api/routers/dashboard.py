"""`GET /dashboard`, `POST /dashboard/items/{id}/dismiss`."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from acamar.api.auth import CurrentSubscriber
from acamar.api.dependencies import get_dashboard_service, get_document_repository
from acamar.ports.repositories import DocumentRepository
from acamar.services.dashboard import Dashboard, DashboardService

router = APIRouter()


@router.get("/dashboard")
async def get_dashboard(
    _: CurrentSubscriber,
    service: Annotated[DashboardService, Depends(get_dashboard_service)],
    days: Annotated[int, Query(ge=1, le=60)] = 7,
) -> Dashboard:
    """Menu for today; events and reminders through ``days`` ahead."""
    return await service.today(window_days=days)


@router.post("/dashboard/items/{item_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_item(
    item_id: UUID,
    _: CurrentSubscriber,
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
) -> None:
    """Mark an item "done" — it stops showing on the dashboard regardless of its date."""
    found = await documents.dismiss(item_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown or already-dismissed item")
