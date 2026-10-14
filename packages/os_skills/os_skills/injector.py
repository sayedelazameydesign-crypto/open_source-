"""SkillInjector: render a retrieved skill as initial guidance for an agent."""

from __future__ import annotations

from os_skills.types import Skill


def build_skill_guidance(skill: Skill, score: float, task: str = "") -> str:
    steps = skill.steps_template
    filled: dict[str, str] = {}
    if task and skill.inputs:
        from os_skills.parameterize import fill_steps

        steps, filled = fill_steps(steps, skill.inputs, task)
    lines = [
        f"[Learned skill reused: {skill.name} (match={score:.2f}, "
        f"status={skill.status}, confidence={skill.confidence:.2f}, "
        f"reliability={skill.reliability:.2f}, used successfully {skill.success_count}x)]",
        *([f"Inputs filled from the current task: {filled}"] if filled else []),
        *(
            ["PROBATIONARY skill: human approval is required before following it."]
            if skill.status == "probationary"
            else []
        ),
        *(
            [
                f"Allowed tools/scopes for this skill: {', '.join(skill.allowed_tools)} "
                f"[{', '.join(skill.tool_scopes)}]; "
                "any other or dangerous tool still requires the normal approval flow."
            ]
            if skill.allowed_tools
            else []
        ),
        "A previous successful run solved a similar task with this pattern. "
        "Reuse it as a starting plan and adapt arguments to the current task; "
        "do not copy stale values blindly, and all tool calls still pass the "
        "normal guardrails and human-approval policy.",
        "Steps template:",
    ]
    for i, step in enumerate(steps, 1):
        tool = step.get("tool") or "reason"
        lines.append(f"  {i}. {step.get('title', 'step')} [{tool}] args={step.get('args', {})}")
    if skill.tools:
        lines.append(f"Tools involved: {', '.join(skill.tools)}")
    return "\n".join(lines)


class SkillInjector:
    def guidance_for(self, skill: Skill, score: float, task: str = "") -> str:
        return build_skill_guidance(skill, score, task)

    def inject(self, task: str, skill: Skill, score: float) -> str:
        return f"{build_skill_guidance(skill, score, task)}\n\nCurrent task: {task}"
