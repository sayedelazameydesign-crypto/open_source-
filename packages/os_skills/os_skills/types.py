"""Skill card: a distilled, reusable pattern learned from a successful run."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from pydantic import BaseModel, Field


class SkillInput(BaseModel):
    """One variable slot extracted from a run and filled on reuse."""

    name: str
    placeholder: str  # e.g. "{{limit}}"
    original_value: str
    kind: str = "string"  # number | string
    source: str = "task"  # task | parent | local — where the value binds from
    input_type: str = "str"  # int | float | str | path | url
    constraints: dict[str, Any] = Field(default_factory=dict)  # min/max/pattern


class PermissionGrant(BaseModel):
    """An approved, recorded expansion of a skill's tool permissions.

    Grants are a security ledger, not card behaviour: ``rollback`` never
    re-opens a revoked grant and never revokes a live one; only
    ``revoke_permission(grant_id)`` changes this list.
    """

    grant_id: str = ""
    tools: list[str]
    scopes: list[str] = Field(default_factory=list)
    approved_by: str = "hitl"
    granted_at: float = Field(default_factory=time.time)


class Skill(BaseModel):
    skill_id: str
    name: str
    description: str
    triggers: list[str] = Field(default_factory=list)
    steps_template: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    success_count: int = 1
    failure_count: int = 0
    version: int = 1
    source_run_id: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    # --- generalisation & governance (ADR-0008) ---------------------------
    inputs: list[SkillInput] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    expected_outcome: str = ""
    status: str = "candidate"  # candidate -> active (>=2 successes) -> quarantined
    confidence: float = 0.5
    last_outcome_at: float | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    tool_scopes: list[str] = Field(default_factory=list)
    permission_grants: list[PermissionGrant] = Field(default_factory=list)
    requires_approval: bool = False
    dependencies: list[str] = Field(default_factory=list)  # composed child skill_ids
    consecutive_successes: int = 0
    probationary_entered_at: float | None = None
    confidence_penalty: float = 0.0
    uses: int = 0
    total_tokens: int = 0
    total_seconds: float = 0.0

    @property
    def signature(self) -> str:
        """Identity of the *pattern* (tools + step shape), not of one task wording."""
        shape = {
            "tools": sorted(self.tools),
            "steps": [
                {"title": s.get("title", ""), "tool": s.get("tool")} for s in self.steps_template
            ],
        }
        return hashlib.sha256(json.dumps(shape, sort_keys=True, default=str).encode()).hexdigest()[
            :16
        ]

    @property
    def reliability(self) -> float:
        """Laplace-smoothed success rate: one success is evidence, not proof.

        ``(success + 1) / (total + 2)`` keeps a single-use skill at 0.67
        instead of a reckless 1.0, and a never-used skill at a neutral 0.5.
        """
        total = self.success_count + self.failure_count
        return (self.success_count + 1) / (total + 2)

    def refresh_status(self, *, now: float | None = None) -> None:
        """Decay confidence with age and quarantine persistent failures."""
        import time as _time

        moment = now if now is not None else _time.time()
        raw = 0.7 * self.reliability + 0.3 * self._recency(moment)
        self.confidence = round(max(0.0, raw - self.confidence_penalty), 4)
        if self.status in {"probationary", "deprecated"}:
            return  # explicit states are managed by SkillManager transitions
        trials = self.success_count + self.failure_count
        if trials >= 3 and self.failure_count > self.success_count:
            self.status = "quarantined"
        elif self.success_count >= 2:
            self.status = "active"
        else:
            self.status = "candidate"

    def _recency(self, now: float) -> float:
        import math

        if self.last_outcome_at is None:
            return 0.5
        age_days = max(0.0, (now - self.last_outcome_at) / 86400.0)
        # Adaptive horizon, short where evidence is thin: a one-trial
        # candidate must fade in ~24 days (it is the thing to doubt most),
        # while proven skills earn a longer memory.
        # trials=1 -> ~24d, trials=10 -> ~50d, trials=100 -> ~83d.
        trials = self.success_count + self.failure_count
        window_days = 14.0 + 15.0 * math.log1p(trials)
        return math.exp(-age_days / window_days)

    def search_text(self) -> str:
        return " ".join([self.name, self.description, *self.triggers, *self.tools])
