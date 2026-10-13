"""Shared API runtime state and bearer-token resolution.

Extracting the runtime state from ``create_app``'s closure lets the public
platform endpoints (``/runs``, ``/ws/{id}``, …), the private HTTP namespace
(``/cela/v1/…``) and the in-process Python facade (:class:`CelaBrowser`) drive
the exact same execution engine and state tables without drift.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from os_core.events import EventBus, EventType
from os_core.hitl import ApprovalDecision, QueueApprover
from os_core.state import RunState, RunStatus
from os_orchestration.platform import Platform
from os_tools.egress import EgressPolicy
from os_tools.local_ai import LocalAIEngine
from pydantic import BaseModel, ConfigDict

from os_server.demo import demo_egress_policy

PRODUCT_NAME: str = "cela_use_browser"
PRODUCT_VERSION: str = "0.1.0"
DEFAULT_WS_TICKET_TTL: float = 60.0

PLATFORM_TOKEN_ENV_VARS: tuple[str, ...] = ("CELA_API_TOKEN", "OS_API_TOKEN")
PRIVATE_TOKEN_ENV_VARS: tuple[str, ...] = ("CELA_PRIVATE_API_TOKEN",)

PlatformFactory = Callable[[str, int], Platform]


class RunHandle(BaseModel):
    run_id: str
    bus: EventBus
    task: asyncio.Task[RunState]
    platform: Platform
    target_url: str | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


class WsTicket(BaseModel):
    run_id: str
    expires_at: float


def valid_bearer_token(authorization: str | None, expected_token: str | None) -> bool:
    """Constant-time comparison of an HTTP Bearer credential."""
    if not expected_token or not authorization:
        return False
    scheme, separator, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not supplied:
        return False
    return secrets.compare_digest(supplied.encode("utf-8"), expected_token.encode("utf-8"))


def resolve_token(
    explicit: str | None = None,
    *,
    env_vars: tuple[str, ...] = PLATFORM_TOKEN_ENV_VARS,
) -> str | None:
    """Resolve a bearer token from an explicit argument or environment variables.

    Passing ``explicit=""`` intentionally resolves to ``""`` (falsy) so tests
    and callers can force the fail-closed unconfigured state even when an
    environment variable is set in the parent process.
    """
    if explicit is not None:
        return explicit
    for name in env_vars:
        value = os.environ.get(name)
        if value:
            return value
    return None


def resolve_private_token(explicit: str | None = None) -> str | None:
    """Resolve the dedicated token for ``/cela/v1``."""
    return resolve_token(explicit, env_vars=PRIVATE_TOKEN_ENV_VARS)


def single_platform_factory(platform: Platform) -> PlatformFactory:
    """Adapt one shared platform into the per-run factory shape."""

    def factory(mode: str, max_steps: int) -> Platform:
        platform.config.mode = mode
        platform.config.max_steps = max_steps
        return platform

    return factory


class ApiRuntime:
    """In-memory state shared by the platform routes, ``/cela/v1`` and :class:`CelaBrowser`."""

    def __init__(
        self,
        platform_or_factory: Platform | PlatformFactory,
        *,
        egress_policy: EgressPolicy | None = None,
        ws_ticket_ttl: float = DEFAULT_WS_TICKET_TTL,
        ai_engine: LocalAIEngine | None = None,
    ) -> None:
        if ws_ticket_ttl <= 0:
            raise ValueError("ws_ticket_ttl must be positive")
        self.make_platform: PlatformFactory = (
            platform_or_factory
            if callable(platform_or_factory) and not isinstance(platform_or_factory, Platform)
            else single_platform_factory(platform_or_factory)  # type: ignore[arg-type]
        )
        self.platform: Platform = self.make_platform("auto", 25)
        self.egress_policy: EgressPolicy = (
            egress_policy if egress_policy is not None else demo_egress_policy()
        )
        self.ws_ticket_ttl: float = ws_ticket_ttl
        self.ai: LocalAIEngine = ai_engine if ai_engine is not None else LocalAIEngine()
        self.runs: dict[str, RunHandle] = {}
        self.states: dict[str, RunState] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.tickets: dict[str, WsTicket] = {}

    def start_run(
        self,
        task: str,
        *,
        mode: str = "auto",
        max_steps: int = 25,
        target_url: str | None = None,
    ) -> tuple[RunHandle, str]:
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        run_platform = self.make_platform(mode, max_steps)
        bus = run_platform.make_bus(run_id)

        async def _collect(event: Any) -> None:
            if event.type is EventType.ARTIFACT:
                self.artifacts.setdefault(run_id, {})[event.data["filename"]] = event.data

        bus.subscribe(_collect)
        chosen_mode = run_platform.choose_mode(task)
        run_task = asyncio.create_task(run_platform.run(task, run_id=run_id, bus=bus))
        run_task.add_done_callback(
            lambda _t: self.states.setdefault(run_id, self._final_state(_t, run_id))
        )
        handle = RunHandle(
            run_id=run_id,
            bus=bus,
            task=run_task,
            platform=run_platform,
            target_url=target_url,
        )
        self.runs[run_id] = handle
        return handle, chosen_mode

    def get_handle(self, run_id: str) -> RunHandle | None:
        return self.runs.get(run_id)

    def get_run_snapshot(self, run_id: str) -> dict[str, Any] | None:
        handle = self.runs.get(run_id)
        if handle is None:
            return None
        if handle.task.done():
            state = self.states.get(run_id) or self._final_state(handle.task, run_id)
            self.states.setdefault(run_id, state)
            snap = state.snapshot()
            if handle.target_url is not None:
                snap.setdefault("target_url", handle.target_url)
            return snap
        snap = {
            "run_id": run_id,
            "status": RunStatus.RUNNING.value,
            "events": len(handle.bus.buffer),
            "pending_approvals": self._pending(handle.platform) or self._pending(self.platform),
        }
        if handle.target_url is not None:
            snap["target_url"] = handle.target_url
        return snap

    def list_runs(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for run_id in self.runs:
            snap = self.get_run_snapshot(run_id)
            if snap is not None:
                result.append(snap)
        return result

    async def sse_events(self, run_id: str) -> AsyncIterator[str]:
        handle = self.runs[run_id]
        finished = False
        for event in handle.bus.replay():
            yield f"data: {event.to_wire()}\n\n"
            finished = finished or event.type is EventType.RUN_FINISHED
        if finished:
            return
        with handle.bus.stream() as stream:
            async for event in stream:
                yield f"data: {event.to_wire()}\n\n"
                if event.type is EventType.RUN_FINISHED:
                    break

    def issue_ws_ticket(self, run_id: str) -> dict[str, Any] | None:
        if run_id not in self.runs:
            return None
        now = time.monotonic()
        self._prune_expired_tickets(now)
        ticket = secrets.token_urlsafe(24)
        self.tickets[ticket] = WsTicket(run_id=run_id, expires_at=now + self.ws_ticket_ttl)
        return {"ticket": ticket, "run_id": run_id, "expires_in": self.ws_ticket_ttl}

    def consume_ws_ticket(self, ticket: str | None, run_id: str) -> bool:
        if not ticket:
            return False
        now = time.monotonic()
        self._prune_expired_tickets(now)
        meta = self.tickets.pop(ticket, None)
        if meta is None or meta.expires_at <= now:
            return False
        return secrets.compare_digest(meta.run_id, run_id)

    def approve(self, run_id: str, tool_call_id: str, decision: ApprovalDecision) -> bool:
        handle = self.runs.get(run_id)
        if handle is None:
            raise KeyError(run_id)
        if not isinstance(handle.platform.approver, QueueApprover):
            raise RuntimeError("this platform is not configured for interactive approval")
        return handle.platform.approver.answer(tool_call_id, decision)

    def get_artifact(self, run_id: str, filename: str) -> dict[str, Any] | None:
        return self.artifacts.get(run_id, {}).get(filename)

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]] | None:
        if run_id not in self.runs:
            return None
        return list(self.artifacts.get(run_id, {}).values())

    def _prune_expired_tickets(self, now: float) -> None:
        expired = [tok for tok, meta in self.tickets.items() if meta.expires_at <= now]
        for tok in expired:
            self.tickets.pop(tok, None)

    @staticmethod
    def _final_state(task: asyncio.Task[RunState], run_id: str) -> RunState:
        if task.cancelled():
            return RunState(run_id=run_id, status=RunStatus.FAILED, error="cancelled")
        try:
            return task.result()
        except Exception as exc:
            return RunState(run_id=run_id, status=RunStatus.FAILED, error=str(exc))

    @staticmethod
    def _pending(plat: Platform) -> list[dict[str, Any]]:  # pragma: no cover - introspection
        if not isinstance(plat.approver, QueueApprover):
            return []
        return [{"id": c.id, "name": c.name, "args": c.args} for c in plat.approver.pending]
