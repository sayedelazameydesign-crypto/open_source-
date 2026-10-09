"""Budget + stagnation: the infinite-loop breaker."""

from __future__ import annotations

from os_agents.budget import Budget, BudgetConfig
from os_core.types import ToolCall


def make_budget(**overrides: object) -> Budget:
    budget = Budget(config=BudgetConfig(**overrides))  # type: ignore[arg-type]
    budget.start()
    return budget


def test_fresh_budget_does_not_stop() -> None:
    budget = make_budget()
    verdict = budget.check()
    assert not verdict.stopped
    assert verdict.reason == ""


def test_max_steps_ceiling_is_enforced() -> None:
    budget = make_budget(max_steps=3)
    for _ in range(3):
        budget.tick()
    verdict = budget.check()
    assert verdict.stopped and verdict.exhausted
    assert "max_steps=3" in verdict.reason


def test_token_ceiling_is_enforced() -> None:
    budget = make_budget(max_tokens=1_000)
    budget.charge_tokens(1_500)
    verdict = budget.check()
    assert verdict.stopped
    assert "token budget" in verdict.reason


def test_consecutive_errors_abort_the_loop() -> None:
    budget = make_budget(max_consecutive_errors=2)
    budget.note_result(ok=False)
    assert not budget.check().stopped
    budget.note_result(ok=False)
    verdict = budget.check()
    assert verdict.stopped
    assert "consecutive tool failures" in verdict.reason


def test_a_success_resets_the_error_streak() -> None:
    budget = make_budget(max_consecutive_errors=2)
    budget.note_result(ok=False)
    budget.note_result(ok=True)
    budget.note_result(ok=False)
    assert not budget.check().stopped


def test_identical_repeated_call_is_detected_as_stagnation() -> None:
    budget = make_budget(repeat_threshold=3)
    call = ToolCall(name="browser_click", args={"selector": "#go"})
    budget.note_call(call)
    budget.note_call(call)
    verdict = budget.check(pending_call=call)
    assert verdict.stopped
    assert verdict.should_review
    assert "stagnation" in verdict.reason


def test_same_tool_different_args_is_not_stagnation() -> None:
    budget = make_budget(repeat_threshold=2)
    budget.note_call(ToolCall(name="browser_click", args={"selector": "#a"}))
    verdict = budget.check(pending_call=ToolCall(name="browser_click", args={"selector": "#b"}))
    assert not verdict.stopped


def test_review_triggers_before_the_hard_ceiling() -> None:
    budget = make_budget(max_steps=25, review_at_step=3)
    for _ in range(3):
        budget.tick()
    verdict = budget.check()
    assert verdict.stopped
    assert verdict.should_review
    assert not verdict.exhausted


def test_wall_clock_ceiling_is_enforced() -> None:
    budget = Budget(config=BudgetConfig(max_seconds=0.0))
    budget.start()
    budget._started -= 5.0  # simulate elapsed time
    verdict = budget.check()
    assert verdict.stopped
    assert "wall-clock" in verdict.reason


def test_status_reports_progress() -> None:
    budget = make_budget(max_steps=10)
    budget.tick()
    budget.charge_tokens(42)
    status = budget.status()
    assert status["steps"] == 1
    assert status["steps_left"] == 9
    assert status["tokens"] == 42


def test_most_repeated_tracks_the_hot_signature() -> None:
    budget = make_budget()
    hot = ToolCall(name="x", args={"a": 1})
    budget.note_call(hot)
    budget.note_call(hot)
    budget.note_call(ToolCall(name="y", args={}))
    signature, count = budget.most_repeated  # type: ignore[misc]
    assert count == 2
    assert signature.startswith("x:")


def test_signature_is_order_independent() -> None:
    a = ToolCall(name="t", args={"x": 1, "y": 2})
    b = ToolCall(name="t", args={"y": 2, "x": 1})
    assert a.signature() == b.signature()
