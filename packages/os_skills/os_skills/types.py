"""Skill card: a distilled, reusable pattern learned from a successful run."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from pydantic import BaseModel, Field


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

    def search_text(self) -> str:
        return " ".join([self.name, self.description, *self.triggers, *self.tools])
