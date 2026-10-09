"""Adaptive model routing — constraint #1 of the design.

Agentic reasoning burns tokens, so the platform never sends every turn to the
big model. Each request is profiled into a :class:`TaskProfile` and routed to
the cheapest tier that can plausibly do the job, under a hard token budget.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from os_core.types import ModelChoice

ModelRole = Literal["planner", "fast", "vision", "critic"]

# Heuristic signals that a task genuinely needs the reasoning tier.
_PLANNING_SIGNALS = re.compile(
    r"\b(plan|multi-?step|research|compar|analys|design|architect|refactor|debug|"
    r"خطة|خطط|حلل|قارن|صمم|ابحث)\b",
    re.IGNORECASE,
)
_CODE_SIGNALS = re.compile(
    r"(```|def |class |import |function |const |\{\{|\bhtml\b|\breact\b)", re.I
)


class ModelSpec(BaseModel):
    """A deployable model: what it is, what it costs, what it is good at."""

    model_id: str
    role: ModelRole
    context_window: int = 32_768
    cost_per_1k_tokens: float = 1.0  # normalised units, not USD
    supports_vision: bool = False
    supports_tools: bool = True
    max_output_tokens: int = 8_192


class TaskProfile(BaseModel):
    """Everything the router needs to know about one request."""

    prompt_tokens: int
    needs_vision: bool = False
    is_planning: bool = False
    is_review: bool = False
    expected_tool_calls: bool = True
    complexity_hint: Literal["trivial", "normal", "complex"] = "normal"
    task_text: str = ""


class RouterConfig(BaseModel):
    """Knobs for cost/quality trade-offs."""

    token_budget: int = 200_000  # per run; hard ceiling
    vision_threshold_tokens: int = 4_000  # below this, prefer the small VLM
    cheap_planner_max_tokens: int = 6_000  # short prompts go to the fast tier
    force_role: ModelRole | None = None
    allow_fallback: bool = True


class ModelRegistry(BaseModel):
    """Named catalogue of deployed models (vLLM/Ollama endpoints)."""

    planner: ModelSpec
    fast: ModelSpec
    vision: ModelSpec
    critic: ModelSpec
    fallback_planner: ModelSpec | None = None

    def get(self, role: ModelRole) -> ModelSpec:
        return getattr(self, role)

    def all(self) -> list[ModelSpec]:
        specs = [self.planner, self.fast, self.vision, self.critic]
        if self.fallback_planner is not None:
            specs.append(self.fallback_planner)
        return specs


def default_registry() -> ModelRegistry:
    """The 2026 open-source stack from the architecture doc."""
    return ModelRegistry(
        planner=ModelSpec(
            model_id="deepseek-ai/DeepSeek-R1",
            role="planner",
            context_window=131_072,
            cost_per_1k_tokens=3.0,
        ),
        fast=ModelSpec(
            model_id="Qwen/Qwen2.5-7B-Instruct",
            role="fast",
            context_window=32_768,
            cost_per_1k_tokens=0.4,
        ),
        vision=ModelSpec(
            model_id="Qwen/Qwen2.5-VL-7B-Instruct",
            role="vision",
            context_window=32_768,
            cost_per_1k_tokens=0.6,
            supports_vision=True,
        ),
        critic=ModelSpec(
            model_id="Qwen/Qwen2.5-14B-Instruct",
            role="critic",
            context_window=32_768,
            cost_per_1k_tokens=1.0,
        ),
        fallback_planner=ModelSpec(
            model_id="meta-llama/Llama-3.3-70B-Instruct",
            role="planner",
            context_window=131_072,
            cost_per_1k_tokens=2.5,
        ),
    )


class ModelRouter(BaseModel):
    registry: ModelRegistry = Field(default_factory=default_registry)
    config: RouterConfig = Field(default_factory=RouterConfig)
    spent_tokens: int = 0

    # -- profiling ------------------------------------------------------
    def profile(
        self,
        text: str,
        *,
        has_image: bool = False,
        is_review: bool = False,
        complexity_hint: Literal["trivial", "normal", "complex"] = "normal",
    ) -> TaskProfile:
        prompt_tokens = max(1, len(text) // 4)
        is_planning = (
            complexity_hint == "complex"
            or bool(_PLANNING_SIGNALS.search(text))
            or prompt_tokens > self.config.cheap_planner_max_tokens
        )
        return TaskProfile(
            prompt_tokens=prompt_tokens,
            needs_vision=has_image,
            is_planning=is_planning,
            is_review=is_review,
            complexity_hint=complexity_hint,
            task_text=text[:400],
        )

    # -- routing --------------------------------------------------------
    def route(self, profile: TaskProfile) -> ModelChoice:
        if self.config.force_role is not None:
            role = self.config.force_role
            reason = f"forced by config (force_role={role})"
        elif profile.needs_vision:
            role = "vision"
            reason = "request carries an image -> vision tier (Qwen2.5-VL)"
        elif profile.is_review:
            role = "critic"
            reason = "trajectory review -> critic tier"
        elif profile.is_planning:
            role = "planner"
            reason = "planning signal / long context -> reasoning tier"
        elif profile.complexity_hint == "trivial":
            role = "fast"
            reason = "trivial lookup -> fast tier"
        else:
            role = "fast"
            reason = "single-step tool use -> fast tier"

        spec = self.registry.get(role)
        est = profile.prompt_tokens + min(spec.max_output_tokens, 4_096)

        if self.spent_tokens + est > self.config.token_budget:
            if self.config.allow_fallback and role == "planner":
                spec = self.registry.fast
                role = "fast"
                reason = (
                    f"token budget nearly exhausted ({self.spent_tokens}/"
                    f"{self.config.token_budget}) -> downgraded planner to fast tier"
                )
                est = profile.prompt_tokens + min(spec.max_output_tokens, 2_048)
            else:
                reason += " [WARNING: budget ceiling exceeded]"

        return ModelChoice(
            model=spec.model_id,
            role=role,
            reason=reason,
            est_tokens=est,
            cost_units=round(est / 1000 * spec.cost_per_1k_tokens, 4),
        )

    def select(self, text: str, **kwargs: object) -> ModelChoice:
        """Profile + route in one call."""
        return self.route(self.profile(text, **kwargs))  # type: ignore[arg-type]

    def charge(self, tokens: int) -> None:
        """Record real usage so later turns route more conservatively."""
        self.spent_tokens += max(0, tokens)

    @property
    def budget_remaining(self) -> int:
        return max(0, self.config.token_budget - self.spent_tokens)


def spec_supports_tools(spec: ModelSpec) -> bool:
    """Guard used by the tool layer before advertising tool schemas."""
    return spec.supports_tools
