"""Skill composition: active skills as tools, with cycle/depth protection."""

from __future__ import annotations

from typing import Any

from os_skills.store import SkillStore
from os_skills.types import Skill

MAX_COMPOSITION_DEPTH = 3


class CompositionError(ValueError):
    pass


def detect_cycle(store: SkillStore, skill: Skill) -> list[str] | None:
    """DFS over ``dependencies``; returns the cycle path if one exists."""
    path: list[str] = []
    on_path: set[str] = set()
    done: set[str] = set()

    def visit(node_id: str) -> list[str] | None:
        if node_id in on_path:
            return [*path[path.index(node_id) :], node_id]
        if node_id in done:
            return None
        node = store.get_skill(node_id)
        deps = node.dependencies if node is not None else []
        path.append(node_id)
        on_path.add(node_id)
        for dep in deps:
            cycle = visit(dep)
            if cycle is not None:
                return cycle
        path.pop()
        on_path.discard(node_id)
        done.add(node_id)
        return None

    return visit(skill.skill_id)


def composition_depth(store: SkillStore, skill: Skill) -> int:
    seen: dict[str, int] = {}

    def depth(node_id: str) -> int:
        if node_id in seen:
            return seen[node_id]
        node = store.get_skill(node_id)
        value = (
            0
            if node is None or not node.dependencies
            else 1 + max(depth(d) for d in node.dependencies)
        )
        seen[node_id] = value
        return value

    return depth(skill.skill_id)


def validate_composition(
    store: SkillStore, skill: Skill, *, max_depth: int = MAX_COMPOSITION_DEPTH
) -> None:
    cycle = detect_cycle(store, skill)
    if cycle is not None:
        raise CompositionError(f"skill composition cycle: {' -> '.join(cycle)}")
    if composition_depth(store, skill) > max_depth:
        raise CompositionError(f"skill composition depth exceeds {max_depth}")


def skill_as_tools(manager: Any) -> list[Any]:
    """Register every active/probationary skill as a callable ``skill:<id>`` tool.

    Invoking the tool returns the child's filled steps template for the
    calling agent to execute through the normal tool/HITL path — composition
    reuses *guidance*, it never bypasses governance by executing directly.
    Child templates are filled from the parent's arguments, not re-harvested
    from the original user task.
    """
    from os_tools.registry import Tool

    from os_skills.parameterize import fill_steps

    tools: list[Any] = []
    for skill in manager.list_skills():
        if skill.status not in {"active", "probationary", "candidate"}:
            continue
        validate_composition(manager.store, skill)

        def _make(child: Skill):
            def _invoke(task: str = "") -> dict[str, Any]:
                steps, filled = fill_steps(child.steps_template, child.inputs, task)
                return {
                    "skill_id": child.skill_id,
                    "status": child.status,
                    "requires_approval": child.requires_approval or child.status == "probationary",
                    "filled_inputs": filled,
                    "steps": steps,
                }

            return _invoke

        tools.append(
            Tool(
                name=f"skill:{skill.skill_id}",
                description=(
                    f"Reuse learned skill '{skill.name}' (status={skill.status}). "
                    "Returns its filled steps template."
                ),
                parameters={
                    "type": "object",
                    "properties": {"task": {"type": "string"}},
                    "required": [],
                },
                fn=_make(skill),
            )
        )
    return tools
