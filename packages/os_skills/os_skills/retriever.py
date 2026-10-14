"""SkillRetriever: semantic match of a new task against the skill library."""

from __future__ import annotations

from typing import Any

from os_skills.store import SkillStore
from os_skills.types import Skill


class SkillRetriever:
    def __init__(self, store: SkillStore, ai_engine: Any, *, min_score: float = 0.05) -> None:
        self.store = store
        self.ai = ai_engine
        self.min_score = min_score

    def find(self, task: str, *, top_k: int = 1) -> list[tuple[Skill, float]]:
        skills = self.store.list_skills()
        if not skills:
            return []
        docs = [
            {"id": s.skill_id, "title": s.name, "text": s.search_text(), "metadata": {}}
            for s in skills
        ]
        result = self.ai.semantic_search(task, documents=docs, top_k=max(top_k, 1))
        by_id = {s.skill_id: s for s in skills}
        ranked: list[tuple[Skill, float]] = []
        for hit in result.get("results", []):
            skill = by_id.get(hit["id"])
            if skill is None:
                continue
            # Reliability discount: a skill that mostly fails must earn its reuse.
            score = float(hit["score"]) * (0.5 + 0.5 * skill.reliability)
            if score >= self.min_score:
                ranked.append((skill, round(score, 4)))
        return sorted(ranked, key=lambda pair: pair[1], reverse=True)[:top_k]
