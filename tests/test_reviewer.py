"""Reviewer agent — the model-driven half of the loop breaker."""

from __future__ import annotations

from os_agents.reviewer import HeuristicReviewer, ModelReviewer, _parse_verdict
from os_core.models import ModelResponse
from os_core.types import Message, ReviewVerdict, ToolResult, ToolStatus


def ok(tool: str, out: str = "fine") -> ToolResult:
    return ToolResult(tool_call_id="c", tool_name=tool, status=ToolStatus.SUCCESS, output=out)


def failed(tool: str, err: str = "boom") -> ToolResult:
    return ToolResult(tool_call_id="c", tool_name=tool, status=ToolStatus.ERROR, error=err)


async def test_existing_final_answer_passes() -> None:
    decision = await HeuristicReviewer().review([], [], final_answer="42")
    assert decision.verdict is ReviewVerdict.PASS


async def test_repeated_identical_observation_triggers_correction() -> None:
    results = [ok("click", "same") for _ in range(3)]
    decision = await HeuristicReviewer().review([], results)
    assert decision.verdict is ReviewVerdict.CORRECT
    assert "repeated" in decision.reason
    assert decision.instruction


async def test_failures_plus_repetition_terminates() -> None:
    results = [failed("click") for _ in range(3)]
    decision = await HeuristicReviewer().review([], results)
    assert decision.verdict is ReviewVerdict.TERMINATE


async def test_incomplete_trajectory_continues() -> None:
    decision = await HeuristicReviewer().review([], [ok("echo")], reason="step ceiling")
    assert decision.verdict is ReviewVerdict.CORRECT


async def test_model_reviewer_parses_structured_output() -> None:
    class Stub:
        name = "stub-critic"

        async def complete(self, messages, **kwargs):
            assert "reviewer" in messages[0].content.lower()
            return ModelResponse(
                content=(
                    "VERDICT: correct\n"
                    "REASON: the agent keeps clicking a dead selector\n"
                    "INSTRUCTION: switch to the text locator"
                )
            )

    decision = await ModelReviewer(model=Stub()).review([Message(role="user", content="task")], [])
    assert decision.verdict is ReviewVerdict.CORRECT
    assert "dead selector" in decision.reason
    assert decision.instruction == "switch to the text locator"


async def test_model_reviewer_tolerates_unstructured_output() -> None:
    class Stub:
        name = "stub"

        async def complete(self, messages, **kwargs):
            return ModelResponse(content="looks fine to me")

    decision = await ModelReviewer(model=Stub()).review([], [], reason="ceiling")
    assert decision.verdict is ReviewVerdict.CORRECT
    assert decision.reason == "ceiling"


def test_parse_verdict_handles_all_three_verdicts() -> None:
    for raw, expected in (
        ("VERDICT: pass\nREASON: done", ReviewVerdict.PASS),
        ("verdict: TERMINATE\nreason: stuck", ReviewVerdict.TERMINATE),
        ("VERDICT: correct", ReviewVerdict.CORRECT),
    ):
        assert _parse_verdict(raw, "fallback").verdict is expected


def test_parse_verdict_rejects_unknown_value() -> None:
    """An unparseable verdict degrades to 'correct', but a stated reason wins."""
    decision = _parse_verdict("VERDICT: maybe\nREASON: unsure", "fallback reason")
    assert decision.verdict is ReviewVerdict.CORRECT
    assert decision.reason == "unsure"


def test_parse_verdict_falls_back_when_nothing_parses() -> None:
    decision = _parse_verdict("i have no idea", "fallback reason")
    assert decision.verdict is ReviewVerdict.CORRECT
    assert decision.reason == "fallback reason"
    assert decision.instruction == ""
