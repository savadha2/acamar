"""JudgeVerdict / _judge prompt-shape tests — fast, no live Ollama call.

Not the golden-set evaluation run itself (scripts/run_eval.py's own
docstring: that's deliberately "not wired into pytest/CI" since a hard
pass/fail gate on 10 examples would be noise). This tests the deterministic
parts instead: the schema's field order and the prompt's actual wording —
NOT whether a real Ollama model actually generates keys in that order
(a parsed pydantic object has no memory of key order; that needs a live
smoke-test, not a unit test — see the design note in JudgeVerdict).
"""

from __future__ import annotations

from scripts.run_eval import GoldenExample, JudgeVerdict, _judge

from tests.fakes import FakeChatModel


def test_quoted_reference_span_is_the_first_field() -> None:
    # Pins the structural-enforcement assumption this whole feature depends
    # on — see JudgeVerdict's own docstring. If this ever fails, someone
    # reordered the fields and silently turned this back into a prose-only
    # request.
    assert list(JudgeVerdict.model_fields) == [
        "quoted_reference_span",
        "correctness",
        "grounded",
        "reasoning",
    ]


def test_judge_verdict_accepts_the_new_field() -> None:
    verdict = JudgeVerdict(
        quoted_reference_span="September 25, 2026",
        correctness=5,
        grounded=True,
        reasoning="matches the reference",
    )
    assert verdict.quoted_reference_span == "September 25, 2026"


async def test_prompt_instructs_extract_before_score() -> None:
    example = GoldenExample(
        question="When is the Social Studies Project due?",
        expected_intent="document",
        expected_categories=["reminder"],
        reference_answer="Due September 25, 2026.",
    )
    canned = JudgeVerdict(
        quoted_reference_span="September 25, 2026",
        correctness=5,
        grounded=True,
        reasoning="ok",
    )
    chat_model = FakeChatModel({JudgeVerdict: canned})

    await _judge(chat_model, example, "It's due September 25, 2026.")

    prompt = chat_model.prompts[0]
    assert "quote the exact substring" in prompt
    assert example.question in prompt
    assert example.reference_answer in prompt
