"""SkillInjector: render a retrieved skill as initial guidance for an agent."""

from __future__ import annotations

from os_skills.types import Skill


def build_skill_guidance(skill: Skill, score: float) -> str:
    lines = [
        f"[Learned skill reused: {skill.name} (match={score:.2f}, "
        f"reliability={skill.reliability:.2f}, used successfully {skill.success_count}x)]",
        "A previous successful run solved a similar task with this pattern. "
        "Reuse it as a starting plan and adapt arguments to the current task; "
        "do not copy stale values blindly, and all tool calls still pass the "
        "normal guardrails and human-approval policy.",
        "Steps template:",
    ]
    for i, step in enumerate(skill.steps_template, 1):
        tool = step.get("tool") or "reason"
        lines.append(f"  {i}. {step.get('title', 'step')} [{tool}] args={step.get('args', {})}")
    if skill.tools:
        lines.append(f"Tools involved: {', '.join(skill.tools)}")
    return "\n".join(lines)


class SkillInjector:
    def guidance_for(self, skill: Skill, score: float) -> str:
        return build_skill_guidance(skill, score)

    def inject(self, task: str, skill: Skill, score: float) -> str:
        return f"{build_skill_guidance(skill, score)}\n\nCurrent task: {task}"
