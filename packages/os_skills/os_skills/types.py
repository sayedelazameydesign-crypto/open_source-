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
    requires_approval: bool = False
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
        total = self.success_count + self.failure_count
        return self.success_count / total if total else 0.0

    def refresh_status(self, *, now: float | None = None) -> None:
        """Decay confidence with age and quarantine persistent failures."""
        import time as _time

        moment = now if now is not None else _time.time()
        self.confidence = round(0.7 * self.reliability + 0.3 * self._recency(moment), 4)
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
        return math.exp(-age_days / 30.0)  # 30-day decay horizon

    def search_text(self) -> str:
        return " ".join([self.name, self.description, *self.triggers, *self.tools])
