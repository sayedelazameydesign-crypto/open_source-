"""os_core — shared types, model routing, guardrails, events, state, run ledger."""

from __future__ import annotations

from os_core.events import Event, EventBus, EventType, replay_and_stream
from os_core.guardrails import Guardrails, ScanReport, ScanVerdict
from os_core.hitl import (
    ApprovalDecision,
    Approver,
    AutoApprover,
    DenyApprover,
    PolicyApprover,
    QueueApprover,
)
from os_core.ledger import (
    LedgerEntry,
    LedgerError,
    RunLedger,
    RunOutcome,
    outcome_for,
    resolve_ledger,
)
from os_core.models import CachedModel, ChatModel, FallbackModel, OpenAICompatModel, ScriptedModel
from os_core.prompting import (
    Creativity,
    CritiqueResult,
    OutputFormat,
    OutputValidation,
    PromptExample,
    PromptSpec,
    parse_critique,
    validate_output,
)
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
    "CachedModel",
    "ChatModel",
    "Creativity",
    "CritiqueResult",
    "DenyApprover",
    "Event",
    "EventBus",
    "EventType",
    "FallbackModel",
    "Guardrails",
    "LedgerEntry",
    "LedgerError",
    "Message",
    "ModelChoice",
    "ModelRegistry",
    "ModelResponse",
    "ModelRouter",
    "ModelSpec",
    "OpenAICompatModel",
    "OutputFormat",
    "OutputValidation",
    "PlanStep",
    "PolicyApprover",
    "PromptExample",
    "PromptSpec",
    "QueueApprover",
    "ReviewDecision",
    "ReviewVerdict",
    "RiskLevel",
    "RouterConfig",
    "RunLedger",
    "RunOutcome",
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
    "outcome_for",
    "parse_critique",
    "replay_and_stream",
    "resolve_ledger",
    "validate_output",
]
