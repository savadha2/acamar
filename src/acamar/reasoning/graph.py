"""Builds and compiles the reasoning `StateGraph`.

```
                    +- DOCUMENT --> Retrieval --> RetrievalGrade --> Synthesis --> Critique -+
                    |                   ^               |    ^                        |      |
                    |                   |   not relevant |    +--- retry, not grounded-+      |
query --> Router ---+                   +----------------+                                    v
                    +- WEATHER --> weather_answer (no LLM) -----------------------------> answer
                    +- COUNT ----> count_answer (no LLM) -------------------------------> answer
                    +- OUT_OF_SCOPE --> decline (no LLM) -------------------------------> answer
```

RetrievalGrade (2026-09-24) is canonical CRAG's actual mechanism — grading
retrieved *documents* before generation, not just the drafted answer
after (see `nodes/retrieval_grade.py`). It shares one retry budget with
Critique's post-generation check (`MAX_RETRIES`, `state.retry_count`) —
either gate can send the graph back to `retrieval` for a widened attempt,
and either can exhaust the cap and let the graph proceed anyway with a
best-effort answer.

No checkpointer compiled in (Decision B — no conversation memory for v1);
one `ainvoke()` per `/query` call. Takes already-constructed nodes, not
raw ports — the per-node model wiring (Decision, §2.7) happens in
`api/dependencies.py`, this module only wires the graph topology.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from acamar.domain.enums import QueryIntent
from acamar.reasoning.nodes.count_answer import CountAnswerNode
from acamar.reasoning.nodes.critique import CritiqueNode
from acamar.reasoning.nodes.decline import decline_node
from acamar.reasoning.nodes.retrieval import RetrievalNode
from acamar.reasoning.nodes.retrieval_grade import RetrievalGradeNode
from acamar.reasoning.nodes.router import RouterNode
from acamar.reasoning.nodes.synthesis import SynthesisNode
from acamar.reasoning.nodes.weather_answer import WeatherAnswerNode
from acamar.reasoning.state import MAX_RETRIES, QueryState


def _after_router(state: QueryState) -> str:
    if state.intent is None:  # pragma: no cover - RouteDecision.intent is required
        raise ValueError("RouterNode did not set an intent")
    return state.intent.value


def _after_retrieval_grade(state: QueryState) -> str:
    if state.retrieval_relevant or state.retry_count > MAX_RETRIES:
        return "synthesize"
    return "retry"


def _after_critique(state: QueryState) -> str:
    if state.grounded or state.retry_count > MAX_RETRIES:
        return "done"
    return "retry"


def build_graph(
    *,
    router: RouterNode,
    retrieval: RetrievalNode,
    retrieval_grade: RetrievalGradeNode,
    synthesis: SynthesisNode,
    critique: CritiqueNode,
    weather_answer: WeatherAnswerNode,
    count_answer: CountAnswerNode,
) -> CompiledStateGraph[QueryState, None, QueryState, QueryState]:
    """Wire the graph topology from already-constructed nodes and compile it."""
    graph: StateGraph[QueryState, None, QueryState, QueryState] = StateGraph(QueryState)
    graph.add_node("router", router)
    graph.add_node("retrieval", retrieval)
    graph.add_node("retrieval_grade", retrieval_grade)
    graph.add_node("synthesis", synthesis)
    graph.add_node("critique", critique)
    graph.add_node("weather_answer", weather_answer)
    graph.add_node("count_answer", count_answer)
    graph.add_node("decline", decline_node)

    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        _after_router,
        {
            QueryIntent.DOCUMENT.value: "retrieval",
            QueryIntent.WEATHER.value: "weather_answer",
            QueryIntent.COUNT.value: "count_answer",
            QueryIntent.OUT_OF_SCOPE.value: "decline",
        },
    )
    graph.add_edge("retrieval", "retrieval_grade")
    graph.add_conditional_edges(
        "retrieval_grade", _after_retrieval_grade, {"retry": "retrieval", "synthesize": "synthesis"}
    )
    graph.add_edge("synthesis", "critique")
    graph.add_conditional_edges("critique", _after_critique, {"retry": "retrieval", "done": END})
    graph.add_edge("weather_answer", END)
    graph.add_edge("count_answer", END)
    graph.add_edge("decline", END)

    return graph.compile()
