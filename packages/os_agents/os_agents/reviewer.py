"""Reviewer agent — the model-driven loop breaker.

When the budget layer detects stagnation it hands the trajectory to a critic
tier (routed by :class:`ModelRouter` with ``is_review=True``). The critic
returns one of three verdicts:

* ``pass`` — the work is done, stop cleanly.
* ``correct`` — keep going, but here is what to change.
* ``terminate`` — give up and report what we have.

``HeuristicReviewer`` is the offline default: it reads the transcript and
decides from observable signals (repeated calls, error rate, whether a final
answer exists). Swap in a model-backed reviewer for real runs.
"""

from __future__ import annotations

from collections.abc import Sequence

from os_core.models import ChatModel
from os_core.types import Message, ReviewDecision, ReviewVerdict, ToolResult
from pydantic import BaseModel


class HeuristicReviewer(BaseModel):
    """Deterministic critic — no model call, fully testable."""

    error_tolerance: int = 2
    repeat_limit: int = 3

    async def review(
        self,
        messages: Sequence[Message],
        results: Sequence[ToolResult],
        *,
        reason: str = "",
        final_answer: str = "",
    ) -> ReviewDecision:
        failures = [r for r in results if not r.ok]
        signatures = [f"{r.tool_name}:{r.output[:40]}" for r in results]
        repeats = max((signatures.count(s) for s in signatures), default=0)

        if final_answer.strip():
            return ReviewDecision(
                verdict=ReviewVerdict.PASS,
                reason="a final answer is already present in the trajectory",
            )
        if len(failures) >= self.error_tolerance and repeats >= self.repeat_limit:
            return ReviewDecision(
                verdict=ReviewVerdict.TERMINATE,
                reason=(
                    f"{len(failures)} tool failures and the same observation repeated "
                    f"{repeats} times — no further progress is plausible"
                ),
                instruction="Report partial findings and stop.",
            )
        if repeats >= self.repeat_limit:
            return ReviewDecision(
                verdict=ReviewVerdict.CORRECT,
                reason=f"the same tool call has been repeated {repeats} times without progress",
                instruction=(
                    "Stop repeating the identical call. Try a different selector, a different "
                    "tool, or finish with the information already gathered."
                ),
            )
        if failures and not results:
            return ReviewDecision(
                verdict=ReviewVerdict.TERMINATE,
                reason="every attempt failed before producing any observation",
            )
        return ReviewDecision(
            verdict=ReviewVerdict.CORRECT,
            reason=reason or "trajectory is incomplete",
            instruction="Continue with the remaining steps; do not repeat a failed call verbatim.",
        )


class ModelReviewer(BaseModel):
    """Critic backed by a real model (routed to the critic tier)."""

    model: ChatModel
    max_tokens: int = 512

    model_config = {"arbitrary_types_allowed": True}

    async def review(
        self,
        messages: Sequence[Message],
        results: Sequence[ToolResult],
        *,
        reason: str = "",
        final_answer: str = "",
    ) -> ReviewDecision:
        transcript = "\n".join(m.render() for m in messages[-24:])
        summary = "\n".join(
            f"- {r.tool_name}: {r.status.value} | {r.output[:120]}" for r in results[-12:]
        )
        prompt = (
            "You are a reviewer for an autonomous agent. Decide whether it should continue.\n"
            f"Stop reason: {reason}\n\n"
            f"Transcript:\n{transcript}\n\nTool results:\n{summary}\n\n"
            "Answer with exactly one line in the form:\n"
            "VERDICT: pass|correct|terminate\nREASON: <short>\nINSTRUCTION: <what to do next>"
        )
        response = await self.model.complete(
            [Message(role="user", content=prompt)], temperature=0.0, max_tokens=self.max_tokens
        )
        return _parse_verdict(response.content, reason)


def _parse_verdict(text: str, fallback_reason: str) -> ReviewDecision:
    verdict = ReviewVerdict.CORRECT
    reason = fallback_reason or "reviewer returned no explicit verdict"
    instruction = ""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "verdict" and value.lower() in {v.value for v in ReviewVerdict}:
            verdict = ReviewVerdict(value.lower())
        elif key == "reason" and value:
            reason = value
        elif key == "instruction" and value:
            instruction = value
    return ReviewDecision(verdict=verdict, reason=reason, instruction=instruction)
