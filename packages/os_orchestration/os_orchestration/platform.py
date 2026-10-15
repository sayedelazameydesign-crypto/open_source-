"""The Platform facade — one entry point that picks the right execution mode.

Mirrors the dual-mode design: a task is profiled, then dispatched either to
the ReAct loop (fast, single-agent) or the PlanningFlow (DAG, re-planning).
Everything else (registry, guardrails, approver, event bus, run-outcome
ledger) is shared, so both modes have identical security semantics — and both
leave the same durable record of how the execution ended.
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
from os_core.ledger import LedgerError, RunLedger
from os_core.models import ChatModel, describe_execution
from os_core.router import ModelRouter, RouterConfig
from os_core.state import RunState, RunStatus
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
    # Same duck-typed hook for long-term memory (os_memory.MemoryManager): recalled
    # records are appended to the task as a *quoted, delimited block*, and the
    # finished run is distilled back. Kept duck-typed so this layer never imports
    # os_memory — the same discipline that keeps os_tools and os_agents strangers.
    memory_manager: Any = None
    memory_auto_write: bool = True
    last_memory_ids: list[str] = Field(default_factory=list)
    last_bus: EventBus | None = None  # kept so the server can stream events
    ledger: RunLedger | None = None  # append-only outcome log; None = don't record

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
        state = RunState(task=task, mode=mode, execution=describe_execution(self.model))
        if run_id:
            state.run_id = run_id
        stream = bus or self.make_bus(state.run_id)
        stream.run_id = state.run_id

        used_memory_ids: list[str] = []
        try:
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
                                "text": (
                                    f"reusing learned skill {skill.skill_id} (match={score:.2f})"
                                ),
                                "skill_id": skill.skill_id,
                                "skill_match": score,
                            },
                        )
                    )

            # Memory recall: quoted, attributed context — appended *after* the task,
            # never substituted for it, and never placed in the system turn. A stored
            # fact may inform the agent; it may not address it.
            if self.memory_manager is not None:
                try:
                    recalled = self.memory_manager.guidance_for_task(task)
                except Exception as exc:
                    # Symmetric with _settle_memory: an unreadable memory layer
                    # costs the run its context, never its result.
                    recalled = None
                    await stream.log(f"memory recall unavailable: {exc}", level="warning")
                if recalled is not None:
                    block, memory_ids, best = recalled
                    effective_task = f"{effective_task}\n\n{block}"
                    used_memory_ids = list(memory_ids)
                    self.last_memory_ids = list(memory_ids)
                    state.metadata["memory_ids"] = list(memory_ids)
                    state.metadata["memory_match"] = best
                    await stream.publish(
                        Event(
                            type=EventType.LOG,
                            run_id=state.run_id,
                            data={
                                "text": (
                                    f"injecting {len(memory_ids)} stored memories "
                                    f"(best match={best:.2f})"
                                ),
                                "kind": "memory_recalled",
                                "memory_ids": list(memory_ids),
                                "memory_match": best,
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
        except Exception as exc:
            # The agents mark their own terminal states. An exception escaping
            # here is an infrastructure failure, so the state must not reach the
            # ledger looking like it is still running.
            if not state.is_terminal:
                state.mark_finished(RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            # Memory settles first: a recalled fact that turned out wrong has to be
            # discounted even when the run itself failed — that is exactly the case
            # where believing memory is most expensive.
            await self._settle_memory(state, used_memory_ids, bus=stream)
            # Every execution is recorded — completed, failed, budget-exhausted
            # or interrupted. That is the whole point of a ledger. It runs after
            # _learn, so skill feedback is settled before the record is written.
            await self.record_outcome(state, bus=stream)

    async def record_outcome(self, state: RunState, *, bus: EventBus | None = None) -> None:
        """Append ``state``'s outcome to the ledger. Never raises.

        A ledger that cannot be written is a real problem, but a *reporting*
        one: it must not replace the run's own error, so it is surfaced as an
        event on the bus instead of propagating out of the ``finally`` block.
        """
        if self.ledger is None:
            return
        stream = bus if bus is not None else self.last_bus
        try:
            self.ledger.record(state)
        except LedgerError as exc:
            if stream is not None:
                await stream.log(f"run-outcome ledger write failed: {exc}", level="error")

    async def _settle_memory(
        self, state: RunState, memory_ids: list[str], *, bus: EventBus | None = None
    ) -> None:
        """Feed the outcome back to memory, then distil this run into an episode.

        Never raises. A memory layer that is down must cost a run its *memory*,
        not its result — the same reasoning that keeps a ledger write failure on
        the event bus instead of out of the ``finally`` block.
        """
        if self.memory_manager is None:
            return
        stream = bus if bus is not None else self.last_bus
        success = state.status.value == "completed"
        try:
            if memory_ids:
                self.memory_manager.feedback(memory_ids, success=success)
            if success and self.memory_auto_write:
                record = self.memory_manager.learn_from_run(state)
                if record is not None:
                    state.metadata.setdefault("learned_memory_id", record.memory_id)
                    if stream is not None:
                        await stream.publish(
                            Event(
                                type=EventType.LOG,
                                run_id=state.run_id,
                                data={
                                    "text": f"stored episode {record.memory_id}",
                                    "kind": "memory_written",
                                    "memory_id": record.memory_id,
                                    "memory_status": record.status.value,
                                },
                            )
                        )
        except Exception as exc:
            if stream is not None:
                await stream.log(f"memory layer unavailable: {exc}", level="warning")

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
    memory_manager: Any = None,
    ledger: RunLedger | None = None,
) -> Platform:
    """Convenience factory used by the CLI, the server and the tests."""
    return Platform(
        model=model,
        registry=registry,
        config=PlatformConfig(mode=mode, token_budget=token_budget, max_steps=max_steps),
        approver=approver or DenyApprover(),
        skill_manager=skill_manager,
        memory_manager=memory_manager,
        ledger=ledger,
    )
