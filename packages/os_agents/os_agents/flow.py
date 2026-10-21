"""Flow Orchestration Mode — PlanningFlow with dynamic re-planning.

Separates *planning* from *execution*, as the architecture doc requires:

1. The planner tier emits a DAG of steps (:func:`parse_plan` validates it).
2. Ready steps (all dependencies satisfied) run — independent branches run
   concurrently, which is where the parallel speed-up comes from.
3. A failed step retries with backoff, then triggers **dynamic re-planning**:
   the planner sees the observations so far and rewrites the remaining tail
   of the graph, up to ``max_replans`` times.
4. A synthesis step turns the observations into the final answer.

Every mutation is published on the event bus so the UI's execution tree stays
in sync without polling.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from os_core.events import Event, EventBus, EventType
from os_core.guardrails import Guardrails, ScanVerdict
from os_core.hitl import ApprovalDecision, Approver, DenyApprover
from os_core.models import ChatModel
from os_core.router import ModelRouter
from os_core.state import RunState, RunStatus
from os_core.types import (
    Message,
    PlanStep,
    StepStatus,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from os_tools.registry import ToolRegistry
from pydantic import BaseModel, Field

from os_agents.budget import Budget, BudgetConfig
from os_agents.dag import PlanError, PlanGraph, parse_plan

PLANNER_PROMPT = (
    "You are a planning agent. Decompose the task into a DAG of tool steps.\n"
    "Reply with ONLY a JSON array. Each element:\n"
    '  {"id": "s1", "title": "...", "tool": "tool_name", "args": {...}, '
    '"depends_on": []}\n'
    "Rules: independent steps must not depend on each other (they run in "
    "parallel); use only the listed tools; emit at most {max_steps} steps. "
    "This is a hard execution limit, not a suggestion."
)

_REF_RE = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")

SYNTH_PROMPT = (
    "You are the synthesis step. Using only the observations below, write the "
    "final answer to the user's task. Be concrete and cite numbers.\n"
)


class FlowResult(BaseModel):
    run: RunState
    answer: str
    replans: int = 0


class PlanningFlow(BaseModel):
    """DAG executor with retries and dynamic re-planning."""

    planner: ChatModel
    synth: ChatModel | None = None
    registry: ToolRegistry
    router: ModelRouter
    bus: EventBus
    guardrails: Guardrails = Field(default_factory=Guardrails)
    approver: Approver = Field(default_factory=DenyApprover)
    budget_cfg: BudgetConfig = Field(default_factory=BudgetConfig)
    max_replans: int = Field(default=2, ge=0, le=5)
    parallelism: int = Field(default=4, ge=1, le=16)
    retry_backoff: float = Field(default=0.1, ge=0.0, le=10.0)

    model_config = {"arbitrary_types_allowed": True}

    async def run(self, task: str, *, state: RunState | None = None) -> FlowResult:
        run = state or RunState(task=task, mode="flow")
        run.status = RunStatus.RUNNING
        budget = Budget(config=self.budget_cfg)
        budget.start()

        screen = self.guardrails.scan_text(task)
        if screen.blocked:
            run.mark_finished(
                RunStatus.FAILED, error=f"prompt rejected by guardrails ({screen.matched})"
            )
            return FlowResult(run=run, answer="")

        await self.bus.publish(
            Event(
                type=EventType.RUN_STARTED,
                run_id=self.bus.run_id,
                data={
                    "task": task,
                    "mode": "flow",
                    "max_steps": budget.config.max_steps,
                },
            )
        )

        if budget.steps_left == 0:
            reason = f"max_steps={budget.config.max_steps} reached before planning"
            run.mark_finished(RunStatus.MAX_STEPS, error=reason)
            await self.bus.publish(
                Event(
                    type=EventType.RUN_FINISHED,
                    run_id=self.bus.run_id,
                    data={
                        "status": run.status.value,
                        "steps": 0,
                        "replans": 0,
                        "budget": budget.status(),
                        "answer": "",
                    },
                )
            )
            return FlowResult(run=run, answer="")

        graph = await self._plan(task, run, budget, context="")
        if graph is None:
            rejected = run.metadata.pop("plan_rejection", None)
            error = "planner produced no executable plan"
            if rejected:
                error = f"{error}: {rejected}"
            run.mark_finished(RunStatus.FAILED, error=error)
            return FlowResult(run=run, answer="")

        observations: dict[str, str] = {}
        while graph.pending_count:
            if budget.steps_left == 0:
                break
            ready = graph.ready()
            if not ready:
                break
            batch = ready[: min(self.parallelism, budget.steps_left)]
            results = await asyncio.gather(
                *[self._run_step(step, graph, run, budget, observations) for step in batch]
            )
            for step, result in zip(batch, results, strict=True):
                observations[step.id] = result.observation()

            failed = [s for s in graph.steps if s.status is StepStatus.FAILED]
            if failed and run.replans < self.max_replans:
                context = self._failure_context(failed, observations)
                new_graph = await self._replan(task, graph, context, run, budget)
                if new_graph is not None:
                    graph = new_graph
                else:
                    break
            elif failed:
                break

        max_steps_exhausted = graph.pending_count > 0 and budget.steps_left == 0
        skipped: list[str] = []
        if max_steps_exhausted:
            skipped = await self._skip_pending_steps(
                graph, reason=f"max_steps={budget.config.max_steps} reached"
            )

        answer = await self._synthesise(task, run, observations)
        if max_steps_exhausted:
            status = RunStatus.MAX_STEPS
            error = (
                f"max_steps={budget.config.max_steps} reached; "
                f"skipped {len(skipped)} pending step(s)"
            )
        else:
            status = (
                RunStatus.COMPLETED
                if any(s.status is StepStatus.DONE for s in graph.steps) or answer
                else RunStatus.FAILED
            )
            error = None
        run.steps_used = budget.steps
        run.mark_finished(status, answer=answer, error=error)
        await self.bus.publish(
            Event(
                type=EventType.RUN_FINISHED,
                run_id=self.bus.run_id,
                data={
                    "status": run.status.value,
                    "steps": len(graph.steps),
                    "replans": run.replans,
                    "budget": budget.status(),
                    "answer": answer or "",
                },
            )
        )
        return FlowResult(run=run, answer=answer, replans=run.replans)

    # -- planning -------------------------------------------------------
    async def _skip_pending_steps(self, graph: PlanGraph, *, reason: str) -> list[str]:
        """Make every unexecuted step terminal when the hard budget is spent."""
        skipped: list[str] = []
        for step in graph.steps:
            if step.status not in {StepStatus.PENDING, StepStatus.READY}:
                continue
            step.status = StepStatus.SKIPPED
            skipped.append(step.id)
            await self.bus.publish(
                Event(
                    type=EventType.STEP_FINISHED,
                    run_id=self.bus.run_id,
                    step_id=step.id,
                    data={"status": StepStatus.SKIPPED.value, "reason": reason},
                )
            )
        return skipped

    async def _plan(
        self, task: str, run: RunState, budget: Budget, *, context: str
    ) -> PlanGraph | None:
        max_steps = budget.steps_left
        if max_steps <= 0:
            run.metadata["plan_rejection"] = (
                f"max_steps={budget.config.max_steps} exhausted before planning"
            )
            return None
        specs = ", ".join(
            f"{t.name}({', '.join(t.parameters.get('required', [])) or '-'})"
            for t in self.registry.tools.values()
        )
        prior = f"Prior observations:\n{context}" if context else ""
        prompt = (
            f"{PLANNER_PROMPT.replace('{max_steps}', str(max_steps))}\n\n"
            f"Available tools: {specs}\n\nTask: {task}\n{prior}"
        )
        choice = self.router.route(self.router.profile(prompt, complexity_hint="complex"))
        run.model_choices.append(choice)
        await self.bus.model_selected(choice)
        response = await self.planner.complete(
            [Message(role="user", content=prompt)], temperature=0.1
        )
        budget.charge_tokens(response.usage.total)
        run.tokens_used += response.usage.total
        try:
            graph = parse_plan(response.content, max_steps=max_steps)
        except PlanError as exc:
            run.metadata["plan_rejection"] = str(exc)
            await self.bus.log(f"plan rejected: {exc}", level="error")
            return None
        run.steps = list(graph.steps)
        await self.bus.publish(
            Event(
                type=EventType.PLAN_CREATED,
                run_id=self.bus.run_id,
                data={
                    "steps": [s.model_dump(mode="json") for s in graph.steps],
                    "order": graph.topo_order(),
                },
            )
        )
        return graph

    async def _replan(
        self, task: str, graph: PlanGraph, context: str, run: RunState, budget: Budget
    ) -> PlanGraph | None:
        run.replans += 1
        await self.bus.log(f"dynamic re-planning (attempt {run.replans}/{self.max_replans})")
        await self.bus.publish(
            Event(
                type=EventType.PLAN_UPDATED,
                run_id=self.bus.run_id,
                data={
                    "reason": "step failures",
                    "replan": run.replans,
                    "failed": [s.id for s in graph.steps if s.status is StepStatus.FAILED],
                },
            )
        )
        # Drop the failed tail and let the planner rewrite it.
        keep = [s for s in graph.steps if s.status in {StepStatus.DONE, StepStatus.SKIPPED}]
        new_graph = await self._plan(task, run, budget, context=context)
        if new_graph is None:
            return None
        merged = PlanGraph(steps=keep + new_graph.steps)
        run.steps = list(merged.steps)
        return merged

    # -- execution ------------------------------------------------------
    async def _run_step(
        self,
        step: PlanStep,
        graph: PlanGraph,
        run: RunState,
        budget: Budget,
        observations: dict[str, str],
    ) -> ToolResult:
        step.attempts += 1
        step.status = StepStatus.RUNNING
        budget.tick()
        run.steps_used = budget.steps
        await self.bus.publish(
            Event(
                type=EventType.STEP_STARTED,
                run_id=self.bus.run_id,
                step_id=step.id,
                data={"title": step.title, "tool": step.tool, "attempt": step.attempts},
            )
        )

        if step.tool is None or self.registry.get(step.tool) is None:
            step.status = StepStatus.FAILED
            result = ToolResult(
                tool_call_id=step.id,
                tool_name=step.tool or "(none)",
                status=ToolStatus.ERROR,
                error=f"step has no valid tool (got {step.tool!r}); available: "
                f"{', '.join(self.registry.names())}",
            )
            run.record_result(result)
            await self._finish_step(step, result, run)
            return result

        call = ToolCall(name=step.tool, args=self._resolve_args(step.args, observations))
        arg_scan = self.guardrails.screen(call)
        if arg_scan.verdict is not ScanVerdict.CLEAN:
            step.status = StepStatus.FAILED
            result = ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                status=ToolStatus.DENIED,
                error=arg_scan.detail,
            )
            run.record_result(result)
            await self._finish_step(step, result, run)
            return result

        tool = self.registry.get(call.name)
        risk = self.guardrails.classify(call, declared=tool.risk if tool else None)
        if self.guardrails.requires_approval(risk):
            await self.bus.publish(
                Event(
                    type=EventType.APPROVAL_REQUESTED,
                    run_id=self.bus.run_id,
                    step_id=step.id,
                    data={"id": call.id, "name": call.name, "risk": risk.value},
                )
            )
            decision = await self.approver.request(call, risk, f"'{call.name}' is {risk.value}")
            if decision is not ApprovalDecision.APPROVED:
                step.status = StepStatus.FAILED
                result = ToolResult(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    status=ToolStatus.DENIED,
                    error="human approval denied",
                )
                run.record_result(result)
                await self._finish_step(step, result, run)
                return result

        result = await self._with_retry(call, step)
        budget.note_result(ok=result.ok)
        run.record_result(result)
        step.result = result
        step.status = StepStatus.DONE if result.ok else StepStatus.FAILED
        if step.status is StepStatus.FAILED and step.retries_left:
            step.status = StepStatus.PENDING  # retried on the next scheduling pass
            await asyncio.sleep(self.retry_backoff * step.attempts)
        await self._finish_step(step, result, run)
        return result

    async def _with_retry(self, call: ToolCall, step: PlanStep) -> ToolResult:
        """Registry already retries; this adds one observation-level attempt."""
        result = await self.registry.execute(call)
        if result.ok:
            await self.bus.tool_result(result, step_id=step.id)
            # Emit browser_action for browser tool calls
            if call.name in {"browser_navigate", "browser_click", "browser_fill"}:
                action_map = {
                    "browser_navigate": "goto",
                    "browser_click": "click",
                    "browser_fill": "fill",
                }
                await self.bus.publish(
                    Event(
                        type=EventType.BROWSER_ACTION,
                        run_id=self.bus.run_id,
                        step_id=step.id,
                        data={
                            "action": action_map.get(call.name, "unknown"),
                            "url": call.args.get("url", "") if call.args else "",
                            "selector": call.args.get("selector", "") if call.args else "",
                            "x": None,
                            "y": None,
                            "step": step.id,
                        },
                    ),
                )
            for art in result.artifacts:
                await self.bus.artifact(art, step_id=step.id)
            return result
        await self.bus.log(f"step {step.id} failed: {result.error}", level="warn")
        await self.bus.tool_result(result, step_id=step.id)
        return result

    async def _finish_step(self, step: PlanStep, result: ToolResult, run: RunState) -> None:
        await self.bus.publish(
            Event(
                type=EventType.STEP_FINISHED,
                run_id=self.bus.run_id,
                step_id=step.id,
                data={
                    "status": step.status.value,
                    "tool_status": result.status.value,
                    "duration_ms": result.duration_ms,
                },
            )
        )

    # -- synthesis ------------------------------------------------------
    async def _synthesise(self, task: str, run: RunState, observations: dict[str, str]) -> str:
        body = "\n\n".join(f"[{sid}]\n{obs}" for sid, obs in observations.items())
        prompt = f"{SYNTH_PROMPT}\nTask: {task}\n\nObservations:\n{body[:12_000]}"
        model = self.synth or self.planner
        response = await model.complete([Message(role="user", content=prompt)], temperature=0.2)
        run.tokens_used += response.usage.total
        await self.bus.thought(response.content)
        return response.content

    @classmethod
    def _resolve_args(cls, args: dict[str, Any], observations: dict[str, str]) -> dict[str, Any]:
        """Resolve ``{{s1.output}}`` references between steps.

        A value that is *only* a reference keeps its type (so a dict can be
        passed through); a reference embedded in a larger string is
        interpolated as text.
        """
        resolved: dict[str, Any] = {}
        for key, value in args.items():
            if not isinstance(value, str) or "{{" not in value:
                resolved[key] = value
                continue
            whole = _REF_RE.fullmatch(value.strip())
            if whole is not None:
                found = cls._lookup(whole.group(1), observations)
                resolved[key] = found if found is not None else value
                continue

            def _sub(match: re.Match[str]) -> str:
                found = cls._lookup(match.group(1), observations)
                return str(found) if found is not None else match.group(0)

            resolved[key] = _REF_RE.sub(_sub, value)
        return resolved

    @staticmethod
    def _lookup(ref: str, observations: dict[str, str]) -> Any:
        step_id, _, field = ref.partition(".")
        if step_id not in observations:
            return None
        return _maybe_json(observations[step_id], field)

    @staticmethod
    def _failure_context(failed: list[PlanStep], observations: dict[str, str]) -> str:
        lines = []
        for step in failed:
            err = step.result.error if step.result else "unknown"
            lines.append(f"- step {step.id} ({step.tool}) failed: {err}")
        tail = "\n".join(f"[{k}] {v[:400]}" for k, v in list(observations.items())[-4:])
        return "\n".join(lines) + "\n\nLatest observations:\n" + tail


def _maybe_json(text: str, field: str) -> Any:
    if field != "output":
        return text
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    return parsed if isinstance(parsed, (dict, list)) else text


# Tool-name -> canonical browser action. Mirrors os_agents.react so flow and
# ReAct use identical action vocabulary when reporting to the UI/test harness.
BROWSER_ACTIONS: dict[str, str] = {
    "browser_navigate": "goto",
    "browser_goto": "goto",
    "goto": "goto",
    "browser_click": "click",
    "click": "click",
    "browser_fill": "fill",
    "fill": "fill",
    "browser_type": "type",
    "type": "type",
    "browser_scroll": "scroll",
    "scroll": "scroll",
    "browser_key": "key",
    "browser_press": "key",
    "key": "key",
}


async def browser_action(tool: Any, action: str, **kwargs: Any) -> ToolResult:
    """Dispatch a browser action through a BrowserTool and surface x, y coords.

    Accepts either a canonical action name (``goto``, ``click``, ``fill``,
    ``type``, ``scroll``, ``key``) or a tool name (``browser_navigate`` etc.)
    and returns a :class:`ToolResult` whose ``metadata`` carries the action
    name. For ``click`` and ``fill`` the FakeBrowser backend additionally
    reports deterministic integer ``x``/``y`` center coordinates.
    """
    canonical = BROWSER_ACTIONS.get(action, action)
    result = await tool.browser_action(canonical, **kwargs)
    meta = dict(result.metadata)
    meta.setdefault("action", canonical)
    result.metadata = meta
    return result
