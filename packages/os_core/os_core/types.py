"""Shared domain types for the platform.

Everything that crosses a layer boundary (model layer -> orchestration ->
transport) is a Pydantic model defined here, so the same objects can be
serialised to JSON for the WebSocket transport without a second set of DTOs.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class RiskLevel(StrEnum):
    """How dangerous a tool invocation is. Drives the HITL policy."""

    READ_ONLY = "read_only"
    WRITE = "write"
    NETWORK = "network"
    DANGEROUS = "dangerous"  # money, e-mail, schema changes -> always HITL


# Tools whose side effects are irreversible must never auto-approve.
DANGEROUS_ACTIONS: frozenset[str] = frozenset(
    {"send_email", "transfer_money", "delete_database", "publish", "exec_shell"}
)


class Message(BaseModel):
    role: Role
    content: str = ""
    tool_call_id: str | None = None

    def render(self) -> str:
        prefix = {"system": "SYS", "user": "USR", "assistant": "AST", "tool": "OBS"}[self.role]
        return f"[{prefix}] {self.content}"


class ToolCall(BaseModel):
    """A model's request to invoke a tool."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:8]}")

    def signature(self) -> str:
        """Stable signature used to detect stagnation (identical repeat calls)."""
        import json

        return f"{self.name}:{json.dumps(self.args, sort_keys=True, default=str)}"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    TIMEOUT = "timeout"


class ToolResult(BaseModel):
    tool_call_id: str
    tool_name: str
    status: ToolStatus
    output: str = ""
    error: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    duration_ms: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.SUCCESS

    def observation(self) -> str:
        """Compact string the model sees as the tool observation."""
        if self.status is ToolStatus.SUCCESS:
            body = self.output or "(empty output)"
        elif self.status is ToolStatus.DENIED:
            body = f"DENIED by policy: {self.error or 'human approval required'}"
        else:
            body = f"ERROR: {self.error or 'unknown failure'}"
        if self.artifacts:
            names = ", ".join(a.filename for a in self.artifacts)
            body += f"\n[artifacts: {names}]"
        return body


class Artifact(BaseModel):
    """A renderable output shown in the right-hand Artifacts pane."""

    kind: Literal["code", "html", "markdown", "image", "json", "file", "browser"]
    filename: str
    content: str = ""
    mime_type: str = "text/plain"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"art_{self.kind}_{self.filename}"


class ReviewVerdict(StrEnum):
    PASS = "pass"
    CORRECT = "correct"
    TERMINATE = "terminate"


class ReviewDecision(BaseModel):
    verdict: ReviewVerdict
    reason: str
    instruction: str = ""


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    id: str
    title: str
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    result: ToolResult | None = None
    attempts: int = 0
    max_attempts: int = 3

    @property
    def retries_left(self) -> int:
        return max(0, self.max_attempts - self.attempts)


class ModelChoice(BaseModel):
    """Which model a request was routed to, and why."""

    model: str
    role: Literal["planner", "fast", "vision", "critic"]
    reason: str
    est_tokens: int = 0
    cost_units: float = 0.0


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ModelResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    finish_reason: str = "stop"

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Now:
    """Tiny indirection so tests can freeze time."""

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()


# Artifact must be declared before ToolResult references it at runtime.
ToolResult.model_rebuild()
