"""`GET /sources/{id}/status` — ingestion health for one source."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from acamar.api.auth import CurrentSubscriber
from acamar.api.dependencies import get_source_repository
from acamar.api.schemas import SourceStatusResponse
from acamar.ports.repositories import SourceNotFoundError, SourceRepository

router = APIRouter()


@router.get("/sources/{source_id}/status")
async def source_status(
    source_id: UUID,
    _: CurrentSubscriber,
    sources: Annotated[SourceRepository, Depends(get_source_repository)],
) -> SourceStatusResponse:
    """Ingestion status for one source; 404 if the id is unknown."""
    try:
        source = await sources.get(source_id)
    except SourceNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown source") from None
    return SourceStatusResponse(
        domain=source.domain,
        category=source.category,
        last_status=source.last_status,
        last_run_at=source.last_run_at,
    )
