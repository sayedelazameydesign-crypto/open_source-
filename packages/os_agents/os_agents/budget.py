"""Budget + stagnation detection — the infinite-loop breaker.

The design doc caps a run at ``max_steps=25``. A bare counter is not enough:
an agent can burn 25 steps while making zero progress. So the loop is broken
by *either* condition:

* hard ceilings (steps, wall clock, tokens, consecutive errors), and
* stagnation — the same tool call signature repeated, which is the signature
  of a model stuck in a loop.

When stagnation trips, the reviewer agent gets a chance to redirect before the
run is terminated.
"""

from __future__ import annotations

import time
from collections import Counter

from os_core.types import ToolCall
from pydantic import BaseModel, Field, PrivateAttr


class BudgetConfig(BaseModel):
    max_steps: int = 25
    max_seconds: float = 300.0
    max_tokens: int = 200_000
    max_consecutive_errors: int = 3
    repeat_threshold: int = 3  # identical call N times -> stagnation
    review_at_step: int = 20  # reviewer wakes up before the hard ceiling


class StopReason(BaseModel):
    stopped: bool
    reason: str
    should_review: bool = False
    exhausted: bool = False


class Budget(BaseModel):
    config: BudgetConfig = Field(default_factory=BudgetConfig)
    steps: int = 0
    tokens: int = 0
    consecutive_errors: int = 0
    _started: float = PrivateAttr(default=0.0)
    _signatures: Counter[str] = PrivateAttr(default_factory=Counter)

    def start(self) -> None:
        self._started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started if self._started else 0.0

    @property
    def steps_left(self) -> int:
        return max(0, self.config.max_steps - self.steps)

    def tick(self) -> None:
        self.steps += 1

    def charge_tokens(self, tokens: int) -> None:
        self.tokens += max(0, tokens)

    def note_result(self, ok: bool) -> None:
        self.consecutive_errors = 0 if ok else self.consecutive_errors + 1

    def note_call(self, call: ToolCall) -> None:
        self._signatures[call.signature()] += 1

    def is_stagnant(self, call: ToolCall) -> bool:
        """True when this exact call has already been issued too often."""
        return self.repeat_count(call) >= self.config.repeat_threshold

    def repeat_count(self, call: ToolCall) -> int:
        return self._signatures[call.signature()]

    @property
    def most_repeated(self) -> tuple[str, int] | None:
        if not self._signatures:
            return None
        signature, count = self._signatures.most_common(1)[0]
        return (signature, count)

    def check(self, *, pending_call: ToolCall | None = None) -> StopReason:
        """Ask: should the loop stop right now?"""
        cfg = self.config
        if self.steps >= cfg.max_steps:
            return StopReason(
                stopped=True,
                reason=f"max_steps={cfg.max_steps} reached",
                exhausted=True,
            )
        if self.elapsed >= cfg.max_seconds:
            return StopReason(
                stopped=True,
                reason=f"wall-clock limit {cfg.max_seconds}s reached ({self.elapsed:.1f}s elapsed)",
                exhausted=True,
            )
        if self.tokens >= cfg.max_tokens:
            return StopReason(
                stopped=True,
                reason=f"token budget {cfg.max_tokens} exhausted ({self.tokens} used)",
                exhausted=True,
            )
        if self.consecutive_errors >= cfg.max_consecutive_errors:
            return StopReason(
                stopped=True,
                reason=f"{self.consecutive_errors} consecutive tool failures — aborting to "
                "avoid a failing loop",
                exhausted=True,
            )
        if pending_call is not None:
            repeats = self.repeat_count(pending_call) + 1
            if repeats >= cfg.repeat_threshold:
                return StopReason(
                    stopped=True,
                    reason=(
                        f"stagnation: '{pending_call.name}' with identical arguments has now "
                        f"been requested {repeats} times"
                    ),
                    should_review=True,
                )
        if self.steps and self.steps >= cfg.review_at_step:
            return StopReason(
                stopped=True,
                reason=f"approaching step ceiling ({self.steps}/{cfg.max_steps})",
                should_review=True,
            )
        return StopReason(stopped=False, reason="")

    def status(self) -> dict[str, object]:
        return {
            "steps": self.steps,
            "steps_left": self.steps_left,
            "elapsed_s": round(self.elapsed, 2),
            "tokens": self.tokens,
            "consecutive_errors": self.consecutive_errors,
            "most_repeated": self.most_repeated,
        }
