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
from os_core.tokens import (
    TokenCalibration,
    count_tokens,
    estimate,
    estimate_model_tokens,
    token_ids,
    truncate_to_tokens,
)
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
from os_core.vectors import (
    VectorError,
    embedding_column_type,
    embedding_space,
    pack,
    unpack,
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
    "LedgerEntry",
    "LedgerError",
    "LocalResourceError",
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
    "ResourceCheck",
    "ResourceSnapshot",
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
    "TokenCalibration",
    "ToolCall",
    "ToolResult",
    "ToolStatus",
    "Usage",
    "VectorError",
    "__version__",
    "assess_resources",
    "check_local_resources",
    "count_tokens",
    "default_registry",
    "embedding_column_type",
    "embedding_space",
    "estimate",
    "estimate_model_tokens",
    "format_snapshot",
    "outcome_for",
    "pack",
    "replay_and_stream",
    "resolve_ledger",
    "snapshot_resources",
    "token_ids",
    "truncate_to_tokens",
    "unpack",
]
