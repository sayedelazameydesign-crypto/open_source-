"""Adaptive model routing — cost/quality trade-offs and the budget ceiling."""

from __future__ import annotations

import pytest
from os_core.router import ModelRouter, RouterConfig, default_registry


def test_trivial_task_routes_to_fast_tier(router: ModelRouter) -> None:
    choice = router.select("what is 2+2?")
    assert choice.role == "fast"
    assert "Qwen2.5-7B" in choice.model


def test_planning_signal_routes_to_reasoning_tier(router: ModelRouter) -> None:
    choice = router.select("Plan a multi-step research comparison of open-source LLMs")
    assert choice.role == "planner"
    assert "DeepSeek-R1" in choice.model


def test_arabic_planning_signal_is_detected(router: ModelRouter) -> None:
    choice = router.select("خطط لمهمة بحث متعددة الخطوات وقارن النتائج")
    assert choice.role == "planner"


def test_vision_request_wins_over_planning(router: ModelRouter) -> None:
    choice = router.select("plan the next click on this screenshot", has_image=True)
    assert choice.role == "vision"
    assert "VL" in choice.model


def test_review_routes_to_critic_tier(router: ModelRouter) -> None:
    choice = router.select("is this trajectory progressing?", is_review=True)
    assert choice.role == "critic"


def test_long_prompt_escalates_to_planner() -> None:
    """A prompt past the cheap-planner threshold goes to the reasoning tier."""
    roomy = ModelRouter(config=RouterConfig(token_budget=1_000_000))
    long_prompt = "summarise this " + ("context " * 8_000)
    profile = roomy.profile(long_prompt)
    assert profile.prompt_tokens > roomy.config.cheap_planner_max_tokens
    choice = roomy.route(profile)
    assert choice.role == "planner"
    assert "reasoning tier" in choice.reason


def test_oversized_prompt_downgrades_when_it_cannot_fit_the_budget() -> None:
    """Constraint #1: a 16k-token prompt cannot use a 10k-token budget, so the
    router trades the reasoning tier for one that fits."""
    tight = ModelRouter(config=RouterConfig(token_budget=10_000))
    choice = tight.select("summarise this " + ("context " * 8_000))
    assert choice.role == "fast"
    assert "downgraded" in choice.reason


def test_budget_exhaustion_downgrades_planner_to_fast() -> None:
    tight = ModelRouter(config=RouterConfig(token_budget=1_000, allow_fallback=True))
    tight.charge(950)
    choice = tight.select("Plan a complex multi-step migration")
    assert choice.role == "fast"
    assert "budget" in choice.reason


def test_budget_without_fallback_keeps_tier_but_warns() -> None:
    tight = ModelRouter(config=RouterConfig(token_budget=100, allow_fallback=False))
    tight.charge(99)
    choice = tight.select("Plan a complex multi-step migration")
    assert choice.role == "planner"
    assert "WARNING" in choice.reason


def test_spending_is_accumulated(router: ModelRouter) -> None:
    assert router.budget_remaining == 10_000
    router.charge(1_234)
    assert router.spent_tokens == 1_234
    assert router.budget_remaining == 10_000 - 1_234


def test_negative_charge_is_ignored(router: ModelRouter) -> None:
    router.charge(-500)
    assert router.spent_tokens == 0


def test_cost_is_normalised_by_tier(router: ModelRouter) -> None:
    cheap = router.select("hello there")
    pricey = router.select("Plan a multi-step architecture review")
    assert pricey.cost_units > cheap.cost_units


def test_force_role_overrides_profiling(router: ModelRouter) -> None:
    router.config.force_role = "critic"
    assert router.select("Plan a multi-step migration").role == "critic"


def test_default_registry_covers_the_2026_open_stack() -> None:
    registry = default_registry()
    ids = {spec.model_id for spec in registry.all()}
    assert any("DeepSeek-R1" in i for i in ids)
    assert any("Qwen2.5-VL" in i for i in ids)
    assert registry.fallback_planner is not None


def test_unknown_forced_role_raises(router: ModelRouter) -> None:
    router.config.force_role = "nonexistent"  # type: ignore[assignment]
    with pytest.raises(AttributeError):
        router.select("anything")
