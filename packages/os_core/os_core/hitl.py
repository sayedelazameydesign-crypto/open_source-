"""Human-in-the-loop approval.

Section 5.3 of the architecture doc: irreversible actions (money, e-mail,
schema changes) must block until a human answers. The agent layer talks to the
:class:`Approver` protocol only, so the same loop runs headless in tests
(:class:`AutoApprover` / :class:`DenyApprover`) and interactively behind the
WebSocket (:class:`QueueApprover`, which the UI answers with a button).
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field, PrivateAttr

from os_core.types import RiskLevel, ToolCall


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


@runtime_checkable
class Approver(Protocol):
    async def request(self, call: ToolCall, risk: RiskLevel, reason: str) -> ApprovalDecision: ...


class ApprovalRecord(BaseModel):
    tool_call_id: str
    tool_name: str
    risk: RiskLevel
    reason: str
    decision: ApprovalDecision
    detail: str = ""


class BaseApprover(BaseModel):
    """Keeps an audit trail of every decision taken."""

    history: list[ApprovalRecord] = Field(default_factory=list)

    def record(
        self,
        call: ToolCall,
        risk: RiskLevel,
        reason: str,
        decision: ApprovalDecision,
        detail: str = "",
    ) -> ApprovalRecord:
        entry = ApprovalRecord(
            tool_call_id=call.id,
            tool_name=call.name,
            risk=risk,
            reason=reason,
            decision=decision,
            detail=detail,
        )
        self.history.append(entry)
        return entry


class AutoApprover(BaseApprover):
    """Development mode — approves everything, still records it."""

    async def request(self, call: ToolCall, risk: RiskLevel, reason: str) -> ApprovalDecision:
        self.record(call, risk, reason, ApprovalDecision.APPROVED, "auto-approve (dev)")
        return ApprovalDecision.APPROVED


class DenyApprover(BaseApprover):
    """Strict mode — blocks every dangerous action. Default for prod demos."""

    async def request(self, call: ToolCall, risk: RiskLevel, reason: str) -> ApprovalDecision:
        self.record(call, risk, reason, ApprovalDecision.DENIED, "policy: human required")
        return ApprovalDecision.DENIED


class PolicyApprover(BaseApprover):
    """Approves up to a risk ceiling; escalates above it."""

    max_auto_risk: RiskLevel = RiskLevel.WRITE
    _order: tuple[RiskLevel, ...] = (
        RiskLevel.READ_ONLY,
        RiskLevel.NETWORK,
        RiskLevel.WRITE,
        RiskLevel.DANGEROUS,
    )

    async def request(self, call: ToolCall, risk: RiskLevel, reason: str) -> ApprovalDecision:
        ceiling = self._order.index(self.max_auto_risk)
        decision = (
            ApprovalDecision.APPROVED
            if self._order.index(risk) <= ceiling
            else ApprovalDecision.DENIED
        )
        self.record(
            call,
            risk,
            reason,
            decision,
            f"ceiling={self.max_auto_risk.value} actual={risk.value}",
        )
        return decision


class QueueApprover(BaseApprover):
    """Bridges the agent to the UI.

    ``ask()`` blocks until someone calls :meth:`answer` (a button click sent
    over the WebSocket) or ``timeout`` elapses — in which case the safe
    default (deny) wins. Failing closed is the point.
    """

    timeout: float = 300.0
    default_on_timeout: ApprovalDecision = ApprovalDecision.DENIED
    _pending: dict[str, asyncio.Future[ApprovalDecision]] = PrivateAttr(default_factory=dict)
    _prompts: list[ToolCall] = PrivateAttr(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def pending(self) -> list[ToolCall]:
        return list(self._prompts)

    async def request(self, call: ToolCall, risk: RiskLevel, reason: str) -> ApprovalDecision:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending[call.id] = future
        self._prompts.append(call)
        try:
            decision = await asyncio.wait_for(future, timeout=self.timeout)
        except TimeoutError:
            decision = self.default_on_timeout
            detail = f"timed out after {self.timeout}s -> {decision.value}"
        else:
            detail = "answered by human"
        finally:
            self._pending.pop(call.id, None)
            if call in self._prompts:
                self._prompts.remove(call)
        self.record(call, risk, reason, decision, detail)
        return decision

    def answer(self, tool_call_id: str, decision: ApprovalDecision) -> bool:
        future = self._pending.get(tool_call_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True
