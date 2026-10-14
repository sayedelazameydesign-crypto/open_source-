"""SkillManager: learn -> store -> retrieve -> inject -> feedback -> prune."""

from __future__ import annotations

from typing import Any

from os_core.state import RunState

from os_skills.extractor import SkillExtractor
from os_skills.injector import SkillInjector
from os_skills.retriever import SkillRetriever
from os_skills.store import SkillStore
from os_skills.types import Skill


class SkillManager:
    def __init__(
        self,
        store: SkillStore,
        ai_engine: Any,
        *,
        min_score: float = 0.05,
        injector: SkillInjector | None = None,
    ) -> None:
        self.store = store
        self.ai = ai_engine
        self.extractor = SkillExtractor(ai_engine)
        self.retriever = SkillRetriever(store, ai_engine, min_score=min_score)
        self.injector = injector or SkillInjector()

    # -- learning ---------------------------------------------------------
    def learn_from_run(self, run_state: RunState) -> Skill | None:
        """Distill a completed run; identical patterns merge, never duplicate."""
        candidate = self.extractor.extract(run_state)
        if candidate is None:
            return None
        existing = self.store.find_by_signature(candidate.signature)
        if existing is not None:
            existing.success_count += 1
            if len(candidate.steps_template) > len(existing.steps_template):
                existing.steps_template = candidate.steps_template
            self.store.save_skill(existing)
            return existing
        self.store.save_skill(candidate)
        return candidate

    # -- retrieval / injection ---------------------------------------------
    def find_relevant(self, task: str) -> tuple[Skill, float] | None:
        ranked = self.retriever.find(task, top_k=1)
        return ranked[0] if ranked else None

    def guidance_for_task(self, task: str) -> tuple[Skill, float, str] | None:
        found = self.find_relevant(task)
        if found is None:
            return None
        skill, score = found
        return skill, score, self.injector.inject(task, skill, score)

    # -- feedback / housekeeping ---------------------------------------------
    def record_outcome(self, skill_id: str, *, success: bool) -> Skill | None:
        skill = self.store.get_skill(skill_id)
        if skill is None:
            return None
        if success:
            skill.success_count += 1
        else:
            skill.failure_count += 1
        self.store.save_skill(skill)
        return skill

    def prune(self, *, min_trials: int = 3) -> list[str]:
        """Delete skills that fail more than they succeed after enough trials."""
        removed: list[str] = []
        for skill in self.store.list_skills():
            trials = skill.success_count + skill.failure_count
            if (
                trials >= min_trials
                and skill.failure_count > skill.success_count
                and self.store.delete_skill(skill.skill_id)
            ):
                removed.append(skill.skill_id)
        return removed

    def list_skills(self) -> list[Skill]:
        return self.store.list_skills()


def skill_tools(manager: SkillManager) -> list[Any]:
    """Expose skill search as an MCP-style tool agents may call mid-run."""
    from os_tools.registry import Tool

    def _search(query: str) -> dict[str, Any]:
        found = manager.find_relevant(query)
        if found is None:
            return {"found": False}
        skill, score = found
        return {"found": True, "score": score, "skill": skill.model_dump()}

    return [
        Tool(
            name="skill_search",
            description=(
                "Search the learned skill library for a reusable pattern "
                "matching a task description."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            fn=_search,
        )
    ]


__all__ = ["SkillManager", "skill_tools"]
