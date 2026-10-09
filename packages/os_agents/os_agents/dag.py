"""Plan DAG: parsing, validation and topological scheduling.

The PlanningFlow works on a directed acyclic graph of steps. A model that
emits a cyclic or dangling-dependency plan must not be able to wedge the
executor, so :func:`validate` rejects those shapes with a precise error the
planner can act on during a re-plan.
"""

from __future__ import annotations

import json
import re
from typing import Any

from os_core.types import PlanStep, StepStatus
from pydantic import BaseModel

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class PlanError(ValueError):
    """The model produced a plan that cannot be executed."""


class PlanGraph(BaseModel):
    steps: list[PlanStep]

    def by_id(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def ready(self) -> list[PlanStep]:
        done = {s.id for s in self.steps if s.status in {StepStatus.DONE, StepStatus.SKIPPED}}
        return [
            s
            for s in self.steps
            if s.status in {StepStatus.PENDING, StepStatus.READY}
            and all(dep in done for dep in s.depends_on)
        ]

    def topo_order(self) -> list[str]:
        order: list[str] = []
        remaining = {s.id: set(s.depends_on) for s in self.steps}
        while remaining:
            batch = sorted(sid for sid, deps in remaining.items() if not deps)
            if not batch:
                raise PlanError(f"cycle detected among steps: {sorted(remaining)}")
            order.extend(batch)
            for sid in batch:
                remaining.pop(sid)
            for deps in remaining.values():
                deps.difference_update(batch)
        return order

    @property
    def pending_count(self) -> int:
        return sum(1 for s in self.steps if s.status in {StepStatus.PENDING, StepStatus.READY})

    def describe(self) -> str:
        return "\n".join(
            f"{s.id}. {s.title} [{s.status.value}] "
            f"tool={s.tool or '-'} deps={','.join(s.depends_on) or '-'}"
            for s in self.steps
        )


def parse_plan(raw: str, *, fallback_title: str = "step") -> PlanGraph:
    """Accept a JSON array/object from the planner, tolerating code fences."""
    payload = _extract_json(raw)
    if payload is None:
        raise PlanError(f"plan is not valid JSON: {raw[:200]!r}")

    if isinstance(payload, dict):
        items = payload.get("steps") or payload.get("plan")
        if not isinstance(items, list):
            raise PlanError("plan object must contain a 'steps' array")
    elif isinstance(payload, list):
        items = payload
    else:
        raise PlanError(f"unsupported plan shape: {type(payload).__name__}")

    steps: list[PlanStep] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            steps.append(PlanStep(id=f"s{index + 1}", title=item))
            continue
        if not isinstance(item, dict):
            raise PlanError(f"step {index} must be an object or string")
        step_id = str(item.get("id") or f"s{index + 1}")
        deps = item.get("depends_on") or item.get("deps") or []
        if isinstance(deps, str):
            deps = [deps]
        steps.append(
            PlanStep(
                id=step_id,
                title=str(item.get("title") or item.get("name") or f"{fallback_title} {index + 1}"),
                tool=item.get("tool"),
                args=item.get("args") or item.get("arguments") or {},
                depends_on=[str(d) for d in deps],
            )
        )
    return validate(PlanGraph(steps=steps))


def validate(graph: PlanGraph) -> PlanGraph:
    if not graph.steps:
        raise PlanError("plan has no steps")
    ids = [s.id for s in graph.steps]
    if len(ids) != len(set(ids)):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        raise PlanError(f"duplicate step ids: {duplicates}")
    known = set(ids)
    for step in graph.steps:
        unknown = [d for d in step.depends_on if d not in known]
        if unknown:
            raise PlanError(f"step '{step.id}' depends on unknown step(s): {unknown}")
        if step.id in step.depends_on:
            raise PlanError(f"step '{step.id}' depends on itself")
    graph.topo_order()  # raises on cycles
    return graph


def _extract_json(raw: str) -> Any:
    text = raw.strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    if start < 0:
        return None
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
    return None
