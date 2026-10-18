"""Dependency-injection wiring for the routers."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, cast

import psycopg
from fastapi import Depends, Request
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from acamar.adapters.clock import SystemClock
from acamar.adapters.db.document_repository import PostgresDocumentRepository
from acamar.adapters.db.source_repository import PostgresSourceRepository
from acamar.adapters.db.subscriber_repository import PostgresSubscriberRepository
from acamar.adapters.embedding.ollama_embedder import OllamaEmbedder
from acamar.adapters.enrichment.ollama_enricher import OllamaEnricher
from acamar.adapters.llm.ollama_chat_model import OllamaChatModel
from acamar.adapters.weather.open_meteo import OpenMeteoProvider
from acamar.api.middleware import CockpitState
from acamar.config import get_settings
from acamar.ingestion.connector_factory import connector_for
from acamar.ingestion.service import IngestionService
from acamar.ports.chat_model import ChatModel
from acamar.ports.clock import Clock
from acamar.ports.embedder import Embedder
from acamar.ports.enricher import Enricher
from acamar.ports.repositories import (
    DocumentRepository,
    SourceRepository,
    SubscriberRepository,
)
from acamar.ports.weather import WeatherProvider
from acamar.reasoning.graph import build_graph
from acamar.reasoning.nodes.count_answer import CountAnswerNode
from acamar.reasoning.nodes.critique import CritiqueNode
from acamar.reasoning.nodes.retrieval import RetrievalNode
from acamar.reasoning.nodes.retrieval_grade import RetrievalGradeNode
from acamar.reasoning.nodes.router import RouterNode
from acamar.reasoning.nodes.synthesis import SynthesisNode
from acamar.reasoning.nodes.weather_answer import WeatherAnswerNode
from acamar.reasoning.state import QueryState
from acamar.services.dashboard import DashboardService

_Pool = AsyncConnectionPool[psycopg.AsyncConnection[dict[str, Any]]]


def get_pool(request: Request) -> _Pool:
    return cast("_Pool", request.app.state.pool)


PoolDep = Annotated[_Pool, Depends(get_pool)]


def get_document_repository(pool: PoolDep) -> DocumentRepository:
    return PostgresDocumentRepository(pool)


def get_source_repository(pool: PoolDep) -> SourceRepository:
    return PostgresSourceRepository(pool)


def get_subscriber_repository(pool: PoolDep) -> SubscriberRepository:
    return PostgresSubscriberRepository(pool)


def get_clock() -> Clock:
    return SystemClock()


def get_cockpit_state(request: Request) -> CockpitState:
    return cast("CockpitState", request.app.state.cockpit)


@lru_cache
def get_weather_provider() -> WeatherProvider:
    """Process-wide singleton (like `config.get_settings`).

    A fresh `OpenMeteoProvider` per request would never hit its own cache,
    defeating the point of caching at all.
    """
    return OpenMeteoProvider()


def get_dashboard_service(
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    clock: Annotated[Clock, Depends(get_clock)],
    weather: Annotated[WeatherProvider, Depends(get_weather_provider)],
) -> DashboardService:
    return DashboardService(documents=documents, clock=clock, weather=weather)


@lru_cache
def get_embedder() -> Embedder:
    """Process-wide singleton — same reasoning as `get_weather_provider`."""
    return OllamaEmbedder()


@lru_cache
def get_enricher() -> Enricher:
    """Process-wide singleton — same reasoning as `get_embedder`."""
    return OllamaEnricher()


def get_ingestion_service(
    sources: Annotated[SourceRepository, Depends(get_source_repository)],
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    enricher: Annotated[Enricher, Depends(get_enricher)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> IngestionService:
    """Built fresh per request from cached leaf dependencies.

    Same pattern as `get_reasoning_graph`. Backs `POST /admin/menu-upload`,
    the only place the API triggers ingestion itself rather than
    `scripts.run_ingestion`.
    """
    return IngestionService(
        sources=sources,
        documents=documents,
        embedder=embedder,
        enricher=enricher,
        connector_factory=connector_for,
        clock=clock,
    )


@lru_cache
def get_router_chat_model() -> ChatModel:
    return OllamaChatModel(model=get_settings().ollama_router_model)


@lru_cache
def get_synthesis_chat_model() -> ChatModel:
    return OllamaChatModel(model=get_settings().ollama_model)


@lru_cache
def get_retrieval_grade_chat_model() -> ChatModel:
    return OllamaChatModel(model=get_settings().ollama_retrieval_grade_model)


@lru_cache
def get_critique_chat_model() -> ChatModel:
    return OllamaChatModel(model=get_settings().ollama_critique_model)


def get_reasoning_graph(
    documents: Annotated[DocumentRepository, Depends(get_document_repository)],
    weather: Annotated[WeatherProvider, Depends(get_weather_provider)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    router_chat: Annotated[ChatModel, Depends(get_router_chat_model)],
    retrieval_grade_chat: Annotated[ChatModel, Depends(get_retrieval_grade_chat_model)],
    synthesis_chat: Annotated[ChatModel, Depends(get_synthesis_chat_model)],
    critique_chat: Annotated[ChatModel, Depends(get_critique_chat_model)],
) -> CompiledStateGraph[QueryState, None, QueryState, QueryState]:
    """Built fresh per request from cached leaf dependencies.

    Building the graph itself is cheap (in-memory wiring, no I/O) — the
    expensive parts (Ollama clients, the DB pool) are already shared
    singletons underneath it, same pattern as `get_dashboard_service`.
    """
    return build_graph(
        router=RouterNode(router_chat),
        retrieval=RetrievalNode(embedder, documents),
        retrieval_grade=RetrievalGradeNode(retrieval_grade_chat),
        synthesis=SynthesisNode(synthesis_chat),
        critique=CritiqueNode(critique_chat),
        weather_answer=WeatherAnswerNode(weather),
        count_answer=CountAnswerNode(documents),
    )
