"""Execution state: the serialisable execution tree the UI mirrors.

Constraint #3 of the design — state synchronisation. The run state is the
single source of truth; the UI is a projection of :meth:`RunState.snapshot`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from os_core.types import Artifact, Message, ModelChoice, PlanStep, StepStatus, ToolResult


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MAX_STEPS = "max_steps"


class RunState(BaseModel):
    """Full, snapshot-able state of one agent run."""

    run_id: str = Field(default_factory=lambda: f"run_{uuid.uuid4().hex[:10]}")
    task: str = ""
    status: RunStatus = RunStatus.PENDING
    mode: str = "react"  # "react" | "flow"
    steps: list[PlanStep] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    results: list[ToolResult] = Field(default_factory=list)
    model_choices: list[ModelChoice] = Field(default_factory=list)
    final_answer: str = ""
    steps_used: int = 0
    tokens_used: int = 0
    replans: int = 0
    reviews: int = 0
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    # -- step access ----------------------------------------------------
    def get_step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def add_step(self, step: PlanStep) -> PlanStep:
        self.steps.append(step)
        return step

    def ready_steps(self) -> list[PlanStep]:
        """Steps whose dependencies are all satisfied."""
        done = {s.id for s in self.steps if s.status in {StepStatus.DONE, StepStatus.SKIPPED}}
        return [
            s
            for s in self.steps
            if s.status in {StepStatus.PENDING, StepStatus.READY}
            and all(dep in done for dep in s.depends_on)
        ]

    def record_result(self, result: ToolResult) -> None:
        self.results.append(result)
        self.artifacts.extend(result.artifacts)

    def mark_finished(
        self, status: RunStatus, *, answer: str = "", error: str | None = None
    ) -> None:
        self.status = status
        self.finished_at = datetime.now(timezone.utc).isoformat()
        if answer:
            self.final_answer = answer
        if error:
            self.error = error

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.MAX_STEPS,
        }

    def snapshot(self) -> dict[str, Any]:
        """Compact projection for the WebSocket / REST layer."""
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "mode": self.mode,
            "task": self.task,
            "steps_used": self.steps_used,
            "tokens_used": self.tokens_used,
            "replans": self.replans,
            "reviews": self.reviews,
            "final_answer": self.final_answer,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": [
                {
                    "id": s.id,
                    "title": s.title,
                    "tool": s.tool,
                    "status": s.status.value,
                    "depends_on": s.depends_on,
                    "attempts": s.attempts,
                }
                for s in self.steps
            ],
            "artifacts": [
                {"kind": a.kind, "filename": a.filename, "mime_type": a.mime_type}
                for a in self.artifacts
            ],
            "results": [
                {
                    "id": r.tool_call_id,
                    "tool": r.tool_name,
                    "status": r.status.value,
                    "output": r.output[:500],
                }
                for r in self.results
            ],
            "models": [c.model_dump() for c in self.model_choices],
        }
