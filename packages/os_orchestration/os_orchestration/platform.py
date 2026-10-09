"""The Platform facade — one entry point that picks the right execution mode.

Mirrors the dual-mode design: a task is profiled, then dispatched either to
the ReAct loop (fast, single-agent) or the PlanningFlow (DAG, re-planning).
Everything else (registry, guardrails, approver, event bus) is shared, so both
modes have identical security semantics.
"""

from __future__ import annotations

from typing import Any

from os_agents.budget import BudgetConfig
from os_agents.flow import PlanningFlow
from os_agents.react import ReActAgent
from os_agents.reviewer import HeuristicReviewer
from os_core.events import EventBus
from os_core.guardrails import Guardrails
from os_core.hitl import Approver, DenyApprover
from os_core.models import ChatModel
from os_core.router import ModelRouter, RouterConfig
from os_core.state import RunState
from os_tools.registry import ToolRegistry
from pydantic import BaseModel, Field


class PlatformConfig(BaseModel):
    mode: str = "auto"  # "auto" | "react" | "flow"
    max_steps: int = 25
    max_seconds: float = 300.0
    token_budget: int = 200_000
    max_replans: int = 2
    parallelism: int = 4


class Platform(BaseModel):
    """Wires the layers together and exposes :meth:`run`."""

    model: ChatModel
    registry: ToolRegistry
    config: PlatformConfig = Field(default_factory=PlatformConfig)
    router: ModelRouter | None = None
    guardrails: Guardrails = Field(default_factory=Guardrails)
    approver: Approver = Field(default_factory=DenyApprover)
    reviewer: Any = Field(default_factory=HeuristicReviewer)
    last_bus: EventBus | None = None  # kept so the server can stream events

    model_config = {"arbitrary_types_allowed": True}

    def router_or_default(self) -> ModelRouter:
        if self.router is None:
            self.router = ModelRouter(config=RouterConfig(token_budget=self.config.token_budget))
        return self.router

    def budget(self) -> BudgetConfig:
        return BudgetConfig(
            max_steps=self.config.max_steps,
            max_seconds=self.config.max_seconds,
            max_tokens=self.config.token_budget,
        )

    def choose_mode(self, task: str) -> str:
        """Auto mode: planning-shaped tasks get the DAG, the rest get ReAct."""
        if self.config.mode in {"react", "flow"}:
            return self.config.mode
        profile = self.router_or_default().profile(task)
        return "flow" if profile.is_planning else "react"

    def make_bus(self, run_id: str) -> EventBus:
        bus = EventBus(run_id=run_id)
        self.last_bus = bus
        return bus

    async def run(
        self, task: str, *, run_id: str | None = None, bus: EventBus | None = None
    ) -> RunState:
        mode = self.choose_mode(task)
        state = RunState(task=task, mode=mode)
        if run_id:
            state.run_id = run_id
        stream = bus or self.make_bus(state.run_id)
        stream.run_id = state.run_id

        if mode == "flow":
            flow = PlanningFlow(
                planner=self.model,
                registry=self.registry,
                router=self.router_or_default(),
                bus=stream,
                guardrails=self.guardrails,
                approver=self.approver,
                budget_cfg=self.budget(),
                max_replans=self.config.max_replans,
                parallelism=self.config.parallelism,
            )
            result = await flow.run(task, state=state)
            return result.run

        agent = ReActAgent(
            model=self.model,
            registry=self.registry,
            router=self.router_or_default(),
            bus=stream,
            guardrails=self.guardrails,
            approver=self.approver,
            budget_cfg=self.budget(),
            reviewer=self.reviewer,
        )
        outcome = await agent.run(task, state=state)
        return outcome.run


def build_platform(
    model: ChatModel,
    *,
    registry: ToolRegistry,
    mode: str = "auto",
    approver: Approver | None = None,
    token_budget: int = 200_000,
    max_steps: int = 25,
) -> Platform:
    """Convenience factory used by the CLI, the server and the tests."""
    return Platform(
        model=model,
        registry=registry,
        config=PlatformConfig(mode=mode, token_budget=token_budget, max_steps=max_steps),
        approver=approver or DenyApprover(),
    )
