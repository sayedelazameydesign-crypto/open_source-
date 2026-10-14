"""SkillExtractor: distill a completed run into a reusable skill card."""

from __future__ import annotations

import hashlib
from typing import Any

from os_core.state import RunState, RunStatus
from os_core.types import DANGEROUS_ACTIONS

from os_skills.parameterize import parameterize_steps
from os_skills.types import Skill


def tool_scope(tool: str) -> str:
    if tool == "python_execute":
        return "code:execute:sandbox"
    if tool.startswith("browser_"):
        return "network:browser"
    if tool in {"send_email", "transfer_money", "delete_database", "publish", "exec_shell"}:
        return f"dangerous:{tool}"
    return f"tool:{tool}"


class SkillExtractor:
    """Only successful, tool-using runs teach the system anything."""

    def __init__(self, ai_engine: Any | None = None) -> None:
        self.ai = ai_engine

    def keywords(self, text: str) -> list[str]:
        if self.ai is not None:
            try:
                return list(self.ai.extract_entities(text).get("keywords", []))[:12]
            except Exception:
                pass
        return [w.strip(".,;:!?").lower() for w in text.split() if len(w) > 3][:12]

    def extract(self, run_state: RunState) -> Skill | None:
        if run_state.status is not RunStatus.COMPLETED:
            return None
        steps: list[dict[str, Any]] = []
        tools: list[str] = []
        for step in run_state.steps:
            if step.status.value not in {"done", "skipped"} and step.result is None:
                continue
            steps.append(
                {
                    "id": step.id,
                    "title": step.title,
                    "tool": step.tool,
                    "args": step.args,
                    "depends_on": list(step.depends_on),
                }
            )
            if step.tool:
                tools.append(step.tool)
        if not steps:  # fall back to the tool results a ReAct run leaves behind
            for result in run_state.results:
                if result.ok:
                    steps.append(
                        {"title": f"Use {result.tool_name}", "tool": result.tool_name, "args": {}}
                    )
                    tools.append(result.tool_name)
        if not steps:
            return None  # a chat-only success contains no reusable procedure
        digest = hashlib.sha256(run_state.task.encode()).hexdigest()[:10]
        steps, inputs = parameterize_steps(run_state.task, steps)
        tools_sorted = sorted(set(tools))
        return Skill(
            skill_id=f"skill_{digest}",
            name=f"Skill: {run_state.task[:60]}",
            description=run_state.task,
            triggers=self.keywords(run_state.task),
            steps_template=steps,
            tools=tools_sorted,
            source_run_id=run_state.run_id,
            inputs=inputs,
            preconditions=["A previous run with this pattern completed successfully."],
            expected_outcome=(run_state.final_answer or "")[:200],
            allowed_tools=tools_sorted,
            tool_scopes=sorted({tool_scope(t) for t in tools_sorted}),
            requires_approval=bool(set(tools_sorted) & set(DANGEROUS_ACTIONS)),
        )
