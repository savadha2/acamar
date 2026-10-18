"""`POST /query`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from langgraph.graph.state import CompiledStateGraph

from acamar.api.auth import CurrentSubscriber
from acamar.api.dependencies import get_clock, get_cockpit_state, get_reasoning_graph
from acamar.api.middleware import CockpitState, QueryRecord
from acamar.api.schemas import QueryRequest, QueryResponse, QuerySource
from acamar.ports.clock import Clock
from acamar.reasoning.state import QueryState

router = APIRouter()

_Graph = CompiledStateGraph[QueryState, None, QueryState, QueryState]
_QUESTION_PREVIEW_LEN = 80


@router.post("/query")
async def query(
    body: QueryRequest,
    _: CurrentSubscriber,
    graph: Annotated[_Graph, Depends(get_reasoning_graph)],
    clock: Annotated[Clock, Depends(get_clock)],
    cockpit: Annotated[CockpitState, Depends(get_cockpit_state)],
) -> QueryResponse:
    """Answer a free-text question, grounded in the document corpus.

    Fails loud (502) on any graph error — a live person is watching this
    request, unlike ingestion's fail-open posture. See docs/reasoning-
    pipeline-design.md NF1/§5.3.
    """
    state = QueryState(question=body.question, today=clock.today())
    try:
        result = await graph.ainvoke(state)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"query failed: {exc}") from exc

    # The Router's intent/category decision lives only inside this call's
    # graph state — nothing else ever sees it, unlike method/path/duration
    # (already captured generically by TrafficMiddleware). Recorded here,
    # not in the middleware, because only the route handler has it. Feeds
    # the Pilot Cockpit's "most-queried topics" chart — see
    # `api.middleware.QueryRecord`.
    cockpit.record_query(
        QueryRecord(
            at=datetime.now(UTC),
            question=body.question[:_QUESTION_PREVIEW_LEN],
            intent=str(result["intent"]),
            categories=[str(c) for c in result.get("categories", [])],
            grounded=result["grounded"],
        )
    )

    return QueryResponse(
        answer=result["final_answer"],
        sources=[
            QuerySource(id=doc.id, title=doc.headline or doc.title, event_date=doc.event_date)
            for doc in result.get("retrieved", [])
        ],
        grounded=result["grounded"],
    )
