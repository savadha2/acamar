"""Liveness — the only unauthenticated route."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Always 200 if the process is up."""
    return {"status": "ok"}
