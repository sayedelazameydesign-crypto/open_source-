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
from os_core.events import Event, EventBus, EventType
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
    # Optional continuous-learning hook (os_skills.SkillManager, duck-typed so
    # this layer never imports os_skills): retrieved skills are injected as
    # initial guidance and completed runs are distilled back into the library.
    skill_manager: Any = None
    skill_auto_learn: bool = True
    last_skill_id: str | None = None
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

        # Skill reuse: retrieve a learned pattern for this task and inject it
        # as initial guidance. The original task remains the state's identity;
        # only the text the agents reason over is enriched. Guardrails and
        # HITL still apply to every resulting tool call, exactly as before.
        effective_task = task
        used_skill_id: str | None = None
        if self.skill_manager is not None:
            found = self.skill_manager.guidance_for_task(task)
            if found is not None:
                skill, score, enriched = found
                effective_task = enriched
                used_skill_id = skill.skill_id
                self.last_skill_id = skill.skill_id
                state.metadata["skill_id"] = skill.skill_id
                state.metadata["skill_status"] = skill.status
                state.metadata["skill_match"] = score
                await stream.publish(
                    Event(
                        type=EventType.LOG,
                        run_id=state.run_id,
                        data={
                            "text": f"reusing learned skill {skill.skill_id} (match={score:.2f})",
                            "skill_id": skill.skill_id,
                            "skill_match": score,
                        },
                    )
                )

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
            result = await flow.run(effective_task, state=state)
            return self._learn(state=result.run, used_skill_id=used_skill_id)

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
        outcome = await agent.run(effective_task, state=state)
        return self._learn(state=outcome.run, used_skill_id=used_skill_id)

    def _learn(self, *, state: RunState, used_skill_id: str | None) -> RunState:
        """Feedback loop: score a reused skill, then distill the run itself."""
        if self.skill_manager is None:
            return state
        success = state.status.value == "completed"
        if used_skill_id is not None:
            self.skill_manager.record_outcome(
                used_skill_id, success=success, tokens_used=state.tokens_used
            )
        elif self.skill_auto_learn and success:
            learned = self.skill_manager.learn_from_run(state)
            if learned is not None:
                state.metadata.setdefault("learned_skill_id", learned.skill_id)
        return state


def build_platform(
    model: ChatModel,
    *,
    registry: ToolRegistry,
    mode: str = "auto",
    approver: Approver | None = None,
    token_budget: int = 200_000,
    max_steps: int = 25,
    skill_manager: Any = None,
) -> Platform:
    """Convenience factory used by the CLI, the server and the tests."""
    return Platform(
        model=model,
        registry=registry,
        config=PlatformConfig(mode=mode, token_budget=token_budget, max_steps=max_steps),
        approver=approver or DenyApprover(),
        skill_manager=skill_manager,
    )
