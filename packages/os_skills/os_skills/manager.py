"""SkillManager: learn -> store -> retrieve -> inject -> feedback -> prune."""

from __future__ import annotations

import time
from typing import Any

from os_core.state import RunState, RunStatus
from os_core.types import DANGEROUS_ACTIONS as DANGEROUS_TOOLS

from os_skills.extractor import SkillExtractor
from os_skills.injector import SkillInjector
from os_skills.retriever import SkillRetriever
from os_skills.store import SkillStore
from os_skills.types import PermissionGrant, Skill


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
            existing.last_outcome_at = time.time()
            existing.refresh_status()
            if len(candidate.steps_template) > len(existing.steps_template):
                existing.steps_template = candidate.steps_template
                existing.version += 1  # prior card is snapshotted by the store
            self.store.save_skill(existing)
            return existing
        self.store.save_skill(candidate)
        return candidate

    # -- retrieval / injection ---------------------------------------------
    def find_relevant(self, task: str) -> tuple[Skill, float] | None:
        self._expire_untried_probation()
        self._auto_revive_stale_quarantine()
        ranked = self.retriever.find(task, top_k=1)
        return ranked[0] if ranked else None

    def guidance_for_task(self, task: str) -> tuple[Skill, float, str] | None:
        found = self.find_relevant(task)
        if found is None:
            return None
        skill, score = found
        return skill, score, self.injector.inject(task, skill, score)

    # -- feedback / housekeeping ---------------------------------------------
    # -- status lifecycle ---------------------------------------------------
    def revive(self, skill_id: str, *, probation: bool = True) -> Skill | None:
        """Bring a quarantined skill back as *probationary* (never silently active).

        Probationary skills are retrieved only with forced approval; two
        consecutive successes restore ``active``, one failure returns them
        to quarantine. Manual path out of what would otherwise be a
        death sentence for a skill that never gets feedback again.
        """
        skill = self.store.get_skill(skill_id)
        if skill is None or skill.status != "quarantined":
            return skill
        skill.status = "probationary" if probation else "candidate"
        skill.consecutive_successes = 0
        skill.probationary_entered_at = time.time() if probation else None
        skill.requires_approval = True
        self.store.save_skill(skill)
        return skill

    def deprecate(self, skill_id: str) -> Skill | None:
        skill = self.store.get_skill(skill_id)
        if skill is None:
            return None
        skill.status = "deprecated"
        self.store.save_skill(skill)
        return skill

    def _auto_revive_stale_quarantine(self, *, stale_days: float = 30.0) -> None:
        now = time.time()
        for skill in self.store.list_skills():
            if (
                skill.status == "quarantined"
                and skill.last_outcome_at is not None
                and now - skill.last_outcome_at > stale_days * 86400
            ):
                self.revive(skill.skill_id)

    def _expire_untried_probation(self, *, probation_days: float = 30.0) -> None:
        """Probation is a trial, not a parking lot (two separate timers).

        ``probationary_entered_at`` governs the trial window;
        ``last_outcome_at`` governs only confidence decay. A probationary
        skill never attempted within the window is deprecated — it did not
        earn retention. A sequence of probation successes is only counted
        inside this window; a failure breaks it immediately (-> quarantine).
        """
        now = time.time()
        for skill in self.store.list_skills():
            if skill.status != "probationary" or skill.probationary_entered_at is None:
                continue
            entered = skill.probationary_entered_at
            attempted = skill.last_outcome_at is not None and skill.last_outcome_at >= entered
            if not attempted and now - entered > probation_days * 86400:
                skill.status = "deprecated"
                self.store.save_skill(skill)

    # -- permissions ---------------------------------------------------------
    def expand_permissions(
        self, skill_id: str, tools: list[str], *, approved: bool, approved_by: str = "hitl"
    ) -> Skill:
        """Widen a skill's tools — only with an explicit HITL approval on record."""
        if not approved:
            raise PermissionError("permission expansion requires HITL approval")
        skill = self.store.get_skill(skill_id)
        if skill is None:
            raise KeyError(skill_id)
        from os_skills.extractor import tool_scope

        scopes = sorted({tool_scope(t) for t in tools})
        skill.allowed_tools = sorted(set(skill.allowed_tools) | set(tools))
        skill.tool_scopes = sorted(set(skill.tool_scopes) | set(scopes))
        import uuid

        skill.permission_grants.append(
            PermissionGrant(
                grant_id=f"grant_{uuid.uuid4().hex[:8]}",
                tools=tools,
                scopes=scopes,
                approved_by=approved_by,
            )
        )
        skill.version += 1  # anomalous change: snapshotted in skill_versions
        self.store.save_skill(skill)
        return skill

    # -- composition ---------------------------------------------------------
    def learn_composite(self, run_state: RunState, child_ids: list[str]) -> Skill | None:
        """Create a parent skill referencing children, with its own counters."""
        from os_skills.composition import validate_composition

        if run_state.status is not RunStatus.COMPLETED or len(child_ids) < 2:
            return None
        children = [self.store.get_skill(cid) for cid in child_ids]
        if any(c is None for c in children):
            return None
        steps = [
            {"title": f"Invoke {c.name}", "tool": f"skill:{c.skill_id}", "args": {}}
            for c in children
            if c is not None
        ]
        parent = Skill(
            skill_id=f"skill_comp_{__import__('hashlib').sha256(','.join(child_ids).encode()).hexdigest()[:8]}",
            name=f"Composite: {' + '.join(c.name[7:30] for c in children if c)}",
            description=run_state.task,
            steps_template=steps,
            tools=[f"skill:{cid}" for cid in child_ids],
            dependencies=list(child_ids),
            source_run_id=run_state.run_id,
        )
        validate_composition(self.store, parent)
        self.store.save_skill(parent)
        return parent

    def record_composite_outcome(
        self,
        parent_id: str,
        child_outcomes: dict[str, bool],
        *,
        tokens_used: int = 0,
        parent_success: bool | None = None,
    ) -> Skill | None:
        """Score children individually; a child failure taxes the parent.

        ``penalty = failed_children / child_calls`` accumulates onto the
        parent's confidence, so a parent cannot stay confident while its
        children collapse — and child counters stay independent of the
        parent's (no double-counted inflation either way).
        """
        for child_id, ok in child_outcomes.items():
            self.record_outcome(child_id, success=ok)
        if self.store.get_skill(parent_id) is None:
            return None
        failed = sum(1 for ok in child_outcomes.values() if not ok)
        # Parent success = the *overall task* succeeded, which is not the
        # same as "every child succeeded". The child-failure tax applies
        # only when the parent succeeded (a fragile parent); a failed
        # parent already records its failure — no double punishment.
        succeeded = (failed == 0) if parent_success is None else parent_success
        updated = self.record_outcome(parent_id, success=succeeded, tokens_used=tokens_used)
        if updated is not None and child_outcomes and failed and succeeded:
            updated.confidence_penalty = round(
                updated.confidence_penalty + failed / len(child_outcomes), 4
            )
            updated.refresh_status()
            self.store.save_skill(updated)
        return updated

    def record_outcome(
        self, skill_id: str, *, success: bool, tokens_used: int = 0, seconds: float = 0.0
    ) -> Skill | None:
        skill = self.store.get_skill(skill_id)
        if skill is None:
            return None
        if success:
            skill.success_count += 1
        else:
            skill.failure_count += 1
        skill.uses += 1
        skill.total_tokens += tokens_used
        skill.total_seconds += seconds
        skill.last_outcome_at = time.time()
        if skill.status == "probationary":
            if success:
                skill.consecutive_successes += 1
                if skill.consecutive_successes >= 2:
                    skill.status = "active"
                    skill.requires_approval = bool(set(skill.allowed_tools) & set(DANGEROUS_TOOLS))
            else:
                skill.status = "quarantined"
                skill.consecutive_successes = 0
        else:
            skill.consecutive_successes = skill.consecutive_successes + 1 if success else 0
            skill.refresh_status()  # decayed confidence + candidate/active/quarantined
            if skill.status == "probationary":  # refresh_status preserves it; keep counters sane
                skill.consecutive_successes = 0
        self.store.save_skill(skill)
        return skill

    def rollback(self, skill_id: str) -> Skill | None:
        """Restore card *behaviour* — never its permission ledger.

        Grants are security decisions kept outside card versions: a
        rollback neither re-opens a revoked grant nor revokes a live one.
        """
        current = self.store.get_skill(skill_id)
        restored = self.store.rollback(skill_id)
        if restored is None or current is None:
            return restored
        live_grants = current.permission_grants
        if [g.grant_id for g in live_grants] != [g.grant_id for g in restored.permission_grants]:
            restored.permission_grants = live_grants
            granted_tools = {t for g in live_grants for t in g.tools}
            granted_scopes = {sc for g in live_grants for sc in g.scopes}
            restored.allowed_tools = sorted(set(restored.allowed_tools) | granted_tools)
            restored.tool_scopes = sorted(set(restored.tool_scopes) | granted_scopes)
            self.store.save_skill(restored)
        return restored

    def revoke_permission(self, skill_id: str, grant_id: str) -> Skill | None:
        """The only way a grant dies: explicitly, by id, never via rollback."""
        skill = self.store.get_skill(skill_id)
        if skill is None:
            return None
        removed = [g for g in skill.permission_grants if g.grant_id == grant_id]
        if not removed:
            return skill
        skill.permission_grants = [g for g in skill.permission_grants if g.grant_id != grant_id]
        removed_tools = {t for g in removed for t in g.tools}
        remaining_tools = {t for g in skill.permission_grants for t in g.tools}
        skill.allowed_tools = sorted(
            (set(skill.allowed_tools) - removed_tools) | remaining_tools | set(skill.tools)
        )
        skill.version += 1
        self.store.save_skill(skill)
        return skill

    def list_versions(self, skill_id: str) -> list[Skill]:
        return self.store.list_versions(skill_id)

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
