"""LLM-as-judge evaluator against the golden Q&A set (docs/reasoning-
pipeline-design.md §2.6/item 8). Runs the real compiled graph against each
question, then a separate Judge call scores the generated answer against
the reference. Run on demand - not wired into pytest/CI (the corpus is
tiny; a hard pass/fail gate on 10 examples would be noise, not signal).

Local-only by design: builds the graph directly (never calls the API's
`/query`), so `QUERY_ENABLED=false` on GCP doesn't affect it — every model
here runs on whatever `OLLAMA_BASE_URL` points at (the Mac's Metal Ollama).
To grade the real cloud data, point only `DATABASE_URL` at Cloud SQL (via
the proxy) and keep `OLLAMA_BASE_URL` local.

    uv run python -m scripts.run_eval
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic import BaseModel

from acamar.adapters.clock import SystemClock
from acamar.adapters.db.document_repository import PostgresDocumentRepository
from acamar.adapters.db.pool import connection_pool
from acamar.adapters.embedding.ollama_embedder import OllamaEmbedder
from acamar.adapters.llm.ollama_chat_model import OllamaChatModel
from acamar.adapters.weather.open_meteo import OpenMeteoProvider
from acamar.config import get_settings
from acamar.ports.chat_model import ChatModel
from acamar.reasoning.graph import build_graph
from acamar.reasoning.nodes.count_answer import CountAnswerNode
from acamar.reasoning.nodes.critique import CritiqueNode
from acamar.reasoning.nodes.retrieval import RetrievalNode
from acamar.reasoning.nodes.retrieval_grade import RetrievalGradeNode
from acamar.reasoning.nodes.router import RouterNode
from acamar.reasoning.nodes.synthesis import SynthesisNode
from acamar.reasoning.nodes.weather_answer import WeatherAnswerNode
from acamar.reasoning.state import QueryState

_GOLDEN_SET_PATH = Path(__file__).parent.parent / "tests" / "eval" / "golden_qa.jsonl"

_JUDGE_PROMPT = """\
You are grading whether a generated answer is correct and grounded,
compared to a reference description of what a correct answer looks like.
The reference may describe the SHAPE of a correct answer (e.g. for
weather, which changes daily) rather than exact wording - judge against
that shape, not a literal string match.

Question: {question}

Reference (what a correct answer looks like): {reference}

Generated answer: {answer}

First, before scoring anything: quote the exact substring of the
Reference above that is most directly relevant to judging this specific
question - copy it verbatim, not a paraphrase. This is the fact your score
must be grounded in.

Then, using ONLY that quoted span (not your own paraphrase or memory of
the reference), score correctness from 1 (wrong or fabricated) to 5
(fully correct and matches the reference's substance). Also say whether
the generated answer appears grounded (not fabricating claims beyond what's
plausible from the reference) versus outright making something up.
"""


class JudgeVerdict(BaseModel):
    """LLM-as-judge score for one golden example.

    ``quoted_reference_span`` is deliberately the FIRST field: `ChatModel.
    structured()` constrains generation to this schema's field order
    (`OllamaChatModel.structured()` passes `schema.model_json_schema()` as
    Ollama's `format` — grammar-constrained decoding emits keys in
    schema-declared order), so putting extraction first forces the model to
    locate the relevant reference text *before* it can write a score —
    extract-then-score (docs/sota.md §3), not one holistic read-and-judge
    call. Targets a real, observed failure (2026-09-17): the judge scored a
    wrong answer 5/5 because its own stated reasoning misread the reference
    ("does not provide a specific due date" when it stated "September 25,
    2026") — do not reorder these fields without re-reading that note.
    """

    quoted_reference_span: str
    """The exact substring of `reference_answer` the score below is
    grounded in — must be copied verbatim, not paraphrased, so a human
    spot-checking a transcript can verify the judge actually looked at the
    right fact."""

    correctness: int
    grounded: bool
    reasoning: str


class GoldenExample(BaseModel):
    """One line of `tests/eval/golden_qa.jsonl`."""

    question: str
    expected_intent: str
    expected_categories: list[str]
    reference_answer: str


def _load_golden_set() -> list[GoldenExample]:
    lines = _GOLDEN_SET_PATH.read_text().splitlines()
    return [GoldenExample.model_validate(json.loads(line)) for line in lines if line.strip()]


async def _judge(chat_model: ChatModel, example: GoldenExample, answer: str) -> JudgeVerdict:
    prompt = _JUDGE_PROMPT.format(
        question=example.question, reference=example.reference_answer, answer=answer
    )
    return await chat_model.structured(prompt, schema=JudgeVerdict)


async def main() -> None:
    settings = get_settings()
    clock = SystemClock()
    examples = _load_golden_set()
    judge_model = OllamaChatModel(model=settings.ollama_judge_model)

    async with connection_pool() as pool:
        documents = PostgresDocumentRepository(pool)
        graph = build_graph(
            router=RouterNode(OllamaChatModel(model=settings.ollama_router_model)),
            retrieval=RetrievalNode(OllamaEmbedder(), documents),
            retrieval_grade=RetrievalGradeNode(
                OllamaChatModel(model=settings.ollama_retrieval_grade_model)
            ),
            synthesis=SynthesisNode(OllamaChatModel(model=settings.ollama_model)),
            critique=CritiqueNode(OllamaChatModel(model=settings.ollama_critique_model)),
            weather_answer=WeatherAnswerNode(OpenMeteoProvider()),
            count_answer=CountAnswerNode(documents),
        )

        scores: list[int] = []
        for example in examples:
            state = QueryState(question=example.question, today=clock.today())
            result = await graph.ainvoke(state)
            answer = str(result["final_answer"])
            actual_intent = result.get("intent")
            intent_ok = actual_intent is not None and actual_intent.value == example.expected_intent
            verdict = await _judge(judge_model, example, answer)
            scores.append(verdict.correctness)

            mark = "OK" if intent_ok else "INTENT MISMATCH"
            print(f"\n[{mark}] {example.question}")
            print(f"  expected intent: {example.expected_intent}  actual: {actual_intent}")
            print(f"  answer: {answer}")
            print(
                f"  judge: correctness={verdict.correctness}/5 grounded={verdict.grounded}"
                f"  {verdict.reasoning}"
            )
            print(f"  judge quoted: {verdict.quoted_reference_span!r}")

        avg = sum(scores) / len(scores) if scores else 0.0
        print(
            f"\n{len(examples)} examples, average correctness {avg:.1f}/5 "
            f"(router={settings.ollama_router_model}, judge={settings.ollama_judge_model})"
        )


if __name__ == "__main__":
    asyncio.run(main())
