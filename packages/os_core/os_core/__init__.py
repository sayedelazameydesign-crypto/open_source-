"""os_core — shared types, model routing, guardrails, events, state."""

from __future__ import annotations

from os_core.events import Event, EventBus, EventType
from os_core.guardrails import Guardrails, ScanReport, ScanVerdict
from os_core.hitl import (
    ApprovalDecision,
    Approver,
    AutoApprover,
    DenyApprover,
    PolicyApprover,
    QueueApprover,
)
from os_core.models import ChatModel, OpenAICompatModel, ScriptedModel
from os_core.router import (
    ModelChoice,
    ModelRegistry,
    ModelRouter,
    ModelSpec,
    RouterConfig,
    TaskProfile,
    default_registry,
)
from os_core.state import RunState, RunStatus
from os_core.types import (
    Artifact,
    Message,
    ModelResponse,
    PlanStep,
    ReviewDecision,
    ReviewVerdict,
    RiskLevel,
    StepStatus,
    ToolCall,
    ToolResult,
    ToolStatus,
    Usage,
)

__version__ = "0.1.0"

__all__ = [
    "ApprovalDecision",
    "Approver",
    "Artifact",
    "AutoApprover",
    "ChatModel",
    "DenyApprover",
    "Event",
    "EventBus",
    "EventType",
    "Guardrails",
    "Message",
    "ModelChoice",
    "ModelRegistry",
    "ModelResponse",
    "ModelRouter",
    "ModelSpec",
    "OpenAICompatModel",
    "PlanStep",
    "PolicyApprover",
    "QueueApprover",
    "ReviewDecision",
    "ReviewVerdict",
    "RiskLevel",
    "RouterConfig",
    "RunState",
    "RunStatus",
    "ScanReport",
    "ScanVerdict",
    "ScriptedModel",
    "StepStatus",
    "TaskProfile",
    "ToolCall",
    "ToolResult",
    "ToolStatus",
    "Usage",
    "__version__",
    "default_registry",
]
