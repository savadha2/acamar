"""The ACAMAR API application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from acamar.adapters.db.pool import connection_pool
from acamar.api.middleware import CockpitState, TrafficMiddleware
from acamar.api.routers import admin, dashboard, health, query, sources
from acamar.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Hold one connection pool open for the process lifetime."""
    async with connection_pool() as pool:
        app.state.pool = pool
        yield


def create_app() -> FastAPI:
    """Build the FastAPI app with routers + the Pilot Cockpit middleware mounted."""
    app = FastAPI(title="ACAMAR", version="0.1.0", lifespan=lifespan)
    cockpit_state = CockpitState(max_recent=get_settings().cockpit_max_recent_requests)
    app.state.cockpit = cockpit_state
    app.add_middleware(TrafficMiddleware, state=cockpit_state)
    app.include_router(health.router)
    app.include_router(dashboard.router)
    app.include_router(sources.router)
    app.include_router(query.router)
    app.include_router(admin.router)
    return app


app = create_app()
