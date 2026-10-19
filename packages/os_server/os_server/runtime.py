"""Shared API runtime state and bearer-token resolution.

Extracting the runtime state from ``create_app``'s closure lets the public
platform endpoints (``/runs``, ``/ws/{id}``, …), the private HTTP namespace
(``/cela/v1/…``) and the in-process Python facade (:class:`CelaBrowser`) drive
the exact same execution engine and state tables without drift.

The runtime also owns the run-outcome ledger: it hands the ledger to every
platform it starts a run with, so all three surfaces leave the same durable
record of how each execution ended.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from typing import Any

from os_core.events import EventBus, EventType, replay_and_stream
from os_core.hitl import ApprovalDecision, QueueApprover
from os_core.ledger import RunLedger, resolve_ledger
from os_core.models import describe_execution
from os_core.redact import contains_secret, scrub_credentials
from os_core.state import RunState, RunStatus
from os_orchestration.platform import Platform
from os_tools.egress import EgressPolicy
from os_tools.local_ai import LocalAIEngine
from pydantic import BaseModel, ConfigDict

from os_server.demo import demo_egress_policy
from os_server.secret_file import load_secret_files
from os_server.storage import RunMeta, RunStore, run_store_from_url

PRODUCT_NAME: str = "cela_use_browser"
PRODUCT_VERSION: str = "0.1.0"
DEFAULT_WS_TICKET_TTL: float = 60.0

PLATFORM_TOKEN_ENV_VARS: tuple[str, ...] = ("CELA_API_TOKEN", "OS_API_TOKEN")
PRIVATE_TOKEN_ENV_VARS: tuple[str, ...] = ("CELA_PRIVATE_API_TOKEN",)

PlatformFactory = Callable[[str, int], Platform]


class ArtifactIntegrityError(RuntimeError):
    """Raised when an artifact's stored SHA-256 does not match its content bytes."""


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
    # Token surfaces may be built without create_app (standalone routers), so
    # resolving from the environment also honours the secret file (ADR-0021).
    # An explicit argument — including "" to force the unconfigured state —
    # bypasses the file entirely.
    load_secret_files()
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
    """State shared by the platform routes, ``/cela/v1`` and :class:`CelaBrowser`.

    Live handles (task + bus) stay process-local by nature; final states,
    events, artifacts and WS tickets are written through to ``store``
    (in-memory by default, durable via ``store_url="sqlite:///<path>"``
    or the ``CELA_RUN_STORE_URL`` environment variable). A restarted
    process over the same durable store therefore keeps serving finished
    runs: snapshots, artifact listings and ``after_seq`` replay all work
    without the original live handle.
    """

    def __init__(
        self,
        platform_or_factory: Platform | PlatformFactory,
        *,
        egress_policy: EgressPolicy | None = None,
        ws_ticket_ttl: float = DEFAULT_WS_TICKET_TTL,
        ai_engine: LocalAIEngine | None = None,
        store: RunStore | None = None,
        store_url: str | None = None,
        ledger: RunLedger | None = None,
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
        self.store: RunStore = (
            store
            if store is not None
            else run_store_from_url(store_url or os.environ.get("CELA_RUN_STORE_URL"))
        )
        # Live handles are inherently process-local; the dicts below are
        # write-through caches kept for backwards compatibility, while the
        # store is the durable record consulted after a restart.
        # Every run started through this runtime leaves an outcome record,
        # wherever it was started from (REST, /cela/v1 or CelaBrowser). The
        # default comes from $CELA_LEDGER_PATH; `CELA_LEDGER_PATH=off` disables
        # it. An explicitly passed ledger always wins.
        self.ledger: RunLedger | None = ledger if ledger is not None else resolve_ledger()
        if self.platform.ledger is None:
            self.platform.ledger = self.ledger
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
        chosen_mode = run_platform.choose_mode(task)
        if mode == "auto" and run_platform.config.mode == "auto":
            # A mode-specific factory must get the resolved mode before its
            # transcript/model and event bus are chosen. No task starts twice.
            run_platform = self.make_platform(chosen_mode, max_steps)
        if run_platform.ledger is None:
            # A platform that already carries its own ledger keeps it; one that
            # does not inherits the runtime's so no run escapes the record.
            run_platform.ledger = self.ledger
        bus = run_platform.make_bus(run_id)

        def _collect(event: Any) -> None:
            # Persist every event so `after_seq` replay survives a restart;
            # events are keyed (run_id, seq) in the store, so a re-delivered
            # event can never duplicate a cursor position.
            self.store.save_event(event)
            if event.type is EventType.ARTIFACT:
                self.artifacts.setdefault(run_id, {})[event.data["filename"]] = event.data
                self.store.save_artifact(run_id, event.data["filename"], event.data)

        bus.subscribe(_collect)
        self.store.save_run_meta(
            RunMeta(run_id=run_id, mode=chosen_mode, target_url=target_url, status="running")
        )
        run_task = asyncio.create_task(run_platform.run(task, run_id=run_id, bus=bus))
        run_task.add_done_callback(lambda _t: self._persist_final_state(_t, run_id))
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

    def has_run(self, run_id: str) -> bool:
        """True for live runs and for runs recovered from the store."""
        return run_id in self.runs or self.store.has_run(run_id)

    def stored_events(self, run_id: str, *, after_seq: int = 0) -> list[Any]:
        return self.store.list_events(run_id, after_seq=after_seq)

    def _persist_final_state(self, task: asyncio.Task[RunState], run_id: str) -> RunState:
        state = self.states.get(run_id) or self._final_state(task, run_id)
        self.states[run_id] = state
        self.store.save_state(state)
        return state

    def _stored_snapshot(self, run_id: str) -> dict[str, Any] | None:
        state = self.states.get(run_id) or self.store.get_state(run_id)
        if state is None:
            return None
        self.states[run_id] = state
        snap = state.snapshot()
        meta = self.store.get_run_meta(run_id)
        if meta is not None and meta.target_url is not None:
            snap.setdefault("target_url", meta.target_url)
        return snap

    def get_run_snapshot(self, run_id: str) -> dict[str, Any] | None:
        handle = self.runs.get(run_id)
        if handle is None:
            return self._stored_snapshot(run_id)
        if handle.task.done():
            state = self._persist_final_state(handle.task, run_id)
            snap = state.snapshot()
            if handle.target_url is not None:
                snap.setdefault("target_url", handle.target_url)
            return snap
        snap = {
            "run_id": run_id,
            "status": RunStatus.RUNNING.value,
            "execution": describe_execution(handle.platform.model).model_dump(),
            "events": len(handle.bus.buffer),
            "pending_approvals": self._pending(handle.platform) or self._pending(self.platform),
        }
        if handle.target_url is not None:
            snap["target_url"] = handle.target_url
        return snap

    def list_runs(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for run_id in [*self.runs, *self.store.list_run_ids()]:
            if run_id in seen or run_id.startswith("doc_"):
                continue
            seen.add(run_id)
            snap = self.get_run_snapshot(run_id)
            if snap is not None and snap.get("mode") != "document":
                result.append(snap)
        return result

    def stream_exhausted(self, run_id: str, after_seq: int = 0) -> bool:
        """True when a resume cursor already sits at the end of a finished run.

        Without this check both transports would subscribe to a bus that will
        never publish again and hold the connection open forever.
        """
        handle = self.runs.get(run_id)
        if handle is None:
            state = self.store.get_state(run_id)
            return bool(
                state is not None and state.is_terminal and after_seq >= self.store.last_seq(run_id)
            )
        return handle.task.done() and after_seq >= handle.bus.last_seq

    async def sse_events(self, run_id: str, *, after_seq: int = 0) -> AsyncIterator[str]:
        handle = self.runs.get(run_id)
        if handle is None:
            # Recovered run: replay the durable log, then close — there is
            # no live bus in this process to stream from.
            for event in self.store.list_events(run_id, after_seq=after_seq):
                yield f"data: {event.to_wire()}\n\n"
            return
        if self.stream_exhausted(run_id, after_seq):
            return
        # Subscribe before the backlog is delivered: events published while the
        # history is still streaming are queued, not lost (see replay_and_stream).
        async with aclosing(replay_and_stream(handle.bus, after_seq=after_seq)) as stream:
            async for event in stream:
                yield f"data: {event.to_wire()}\n\n"
                if event.type is EventType.RUN_FINISHED:
                    break

    def issue_ws_ticket(self, run_id: str) -> dict[str, Any] | None:
        if not self.has_run(run_id):
            return None
        # Wall clock, not monotonic: a ticket minted just before a restart
        # must keep its (short) TTL meaning in the new process. The store
        # only ever sees the SHA-256 hash of the ticket.
        now = time.time()
        self._prune_expired_tickets(now)
        ticket = secrets.token_urlsafe(24)
        expires_at = now + self.ws_ticket_ttl
        self.tickets[ticket] = WsTicket(run_id=run_id, expires_at=expires_at)
        self.store.save_ticket(ticket, run_id, expires_at)
        return {"ticket": ticket, "run_id": run_id, "expires_in": self.ws_ticket_ttl}

    def consume_ws_ticket(self, ticket: str | None, run_id: str) -> bool:
        if not ticket:
            return False
        now = time.time()
        self._prune_expired_tickets(now)
        self.tickets.pop(ticket, None)
        stored = self.store.pop_ticket(ticket)
        if stored is None or stored.expires_at <= now:
            return False
        return secrets.compare_digest(stored.run_id, run_id)

    def approve(self, run_id: str, tool_call_id: str, decision: ApprovalDecision) -> bool:
        handle = self.runs.get(run_id)
        if handle is None:
            raise KeyError(run_id)
        if not isinstance(handle.platform.approver, QueueApprover):
            raise RuntimeError("this platform is not configured for interactive approval")
        return handle.platform.approver.answer(tool_call_id, decision)

    def get_artifact(self, run_id: str, filename: str) -> dict[str, Any] | None:
        cached = self.artifacts.get(run_id, {}).get(filename)
        return cached if cached is not None else self.store.get_artifact(run_id, filename)

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]] | None:
        if not self.has_run(run_id):
            return None
        cached = list(self.artifacts.get(run_id, {}).values())
        return cached if cached else self.store.list_artifacts(run_id)

    def download_artifact(self, run_id: str, filename: str) -> dict[str, Any]:
        """Return downloadable bytes + SHA-256 verification for a run artifact.

        Document runs (``doc_...``) are served exclusively by ``/document-runs``
        and are refused here so retention/isolation rules stay uniform.
        """
        if run_id.startswith("doc_") or not self.has_run(run_id):
            raise KeyError(run_id)
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            raise KeyError(filename)

        state = self.states.get(run_id) or self.store.get_state(run_id)
        if state is not None and state.mode == "document":
            raise KeyError(run_id)

        art = next(
            (a for a in (state.artifacts if state is not None else []) if a.filename == filename),
            None,
        )
        meta = self.get_artifact(run_id, filename)

        if art is not None:
            content_text = art.content
            mime_type = art.mime_type or "text/plain; charset=utf-8"
            expected_sha256 = (meta or {}).get("sha256") or art.metadata.get("sha256")
        elif meta is not None and "content" in meta:
            content_text = str(meta["content"])
            mime_type = str(meta.get("mime_type") or "text/plain; charset=utf-8")
            expected_sha256 = meta.get("sha256")
        else:
            raise KeyError(filename)

        if contains_secret(content_text):
            content_text = scrub_credentials(content_text)

        raw_bytes = content_text.encode("utf-8")
        actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise ArtifactIntegrityError(f"artifact integrity check failed for {filename}")

        return {
            "filename": filename,
            "content_bytes": raw_bytes,
            "mime_type": mime_type,
            "sha256": actual_sha256,
            "size_bytes": len(raw_bytes),
        }

    # -- run-outcome ledger -------------------------------------------------

    def ledger_report(self, *, limit: int = 20) -> dict[str, Any]:
        """The last ``limit`` outcomes plus the aggregate summary.

        Reads the file on every call — correct and cheap at prototype scale;
        roadmap phase 4 replaces the file with a persistent store behind the
        same methods.
        """
        if self.ledger is None:
            return {"ledger": {"enabled": False}, "entries": [], "summary": None}
        return {
            "ledger": self.ledger.describe(),
            "entries": [entry.model_dump() for entry in self.ledger.entries(limit=limit)],
            "summary": self.ledger.summary(),
        }

    def ledger_entry_for(self, run_id: str) -> dict[str, Any] | None:
        """The recorded outcome of one run, or ``None`` if it has none."""
        if self.ledger is None:
            return None
        entry = self.ledger.entry_for(run_id)
        return entry.model_dump() if entry is not None else None

    def close(self) -> None:
        self.store.close()

    def _prune_expired_tickets(self, now: float) -> None:
        expired = [tok for tok, meta in self.tickets.items() if meta.expires_at <= now]
        for tok in expired:
            self.tickets.pop(tok, None)
        self.store.prune_tickets(now)

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
