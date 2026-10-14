"""Event stream — the backbone of real-time UI synchronisation.

Every agent mutation emits an event. The in-process :class:`EventBus` is what
the tests and the CLI consume; the server layer bridges the same bus onto a
WebSocket so the React canvas mirrors the execution tree live.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr

from os_core.types import Artifact, ModelChoice, ReviewDecision, ToolCall, ToolResult


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    PLAN_CREATED = "plan_created"
    PLAN_UPDATED = "plan_updated"
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    MODEL_SELECTED = "model_selected"
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    ARTIFACT = "artifact"
    LOG = "log"
    WARNING = "warning"
    BROWSER_ACTION = "browser_action"
    REVIEW = "review"
    RUN_FINISHED = "run_finished"
    ERROR = "error"


class Event(BaseModel):
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
    run_id: str = ""
    step_id: str | None = None
    ts: float = Field(default_factory=time.time)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    # Stamped by EventBus.publish — the reconnect cursor a client sends back as
    # `after_seq`. 0 means "not published yet".
    seq: int = 0

    def to_wire(self) -> str:
        return self.model_dump_json()


Listener = Callable[[Event], Awaitable[None] | None]


class EventBus(BaseModel):
    """Fan-out event bus with a replay buffer (lets a late UI catch up)."""

    run_id: str
    max_buffer: int = 500
    _buffer: list[Event] = PrivateAttr(default_factory=list)
    _listeners: list[Listener] = PrivateAttr(default_factory=list)
    _next_seq: int = PrivateAttr(default=1)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def buffer(self) -> list[Event]:
        return list(self._buffer)

    @property
    def last_seq(self) -> int:
        """Highest sequence number this bus assigned (0 before the first publish).

        A relayed event keeps the number it arrived with, so this is a cursor for
        *locally* published events — which is what a reconnecting client of this
        bus needs, and what ``stream_exhausted`` compares against.
        """
        return self._next_seq - 1

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def publish(self, event: Event) -> None:
        if not event.run_id:
            event.run_id = self.run_id
        if not event.seq:
            # An event relayed from another bus keeps its original number, so a
            # cursor stays meaningful across hops; local events get the next one.
            event.seq = self._next_seq
            self._next_seq += 1
        self._buffer.append(event)
        if len(self._buffer) > self.max_buffer:
            del self._buffer[: len(self._buffer) - self.max_buffer]
        for listener in list(self._listeners):
            outcome = listener(event)
            if asyncio.iscoroutine(outcome):
                await outcome

    def emit(self, type_: EventType, **data: Any) -> Event:
        """Build + return an event without awaiting (sync call sites)."""
        return Event(type=type_, run_id=self.run_id, data=data)

    # -- convenience emitters ------------------------------------------
    async def thought(self, text: str, step_id: str | None = None) -> None:
        await self.publish(
            Event(type=EventType.THOUGHT, run_id=self.run_id, data={"text": text}, step_id=step_id)
        )

    async def model_selected(self, choice: ModelChoice, step_id: str | None = None) -> None:
        await self.publish(
            Event(
                type=EventType.MODEL_SELECTED,
                run_id=self.run_id,
                step_id=step_id,
                data=choice.model_dump(),
            )
        )

    async def tool_call(self, call: ToolCall, step_id: str | None = None) -> None:
        await self.publish(
            Event(
                type=EventType.TOOL_CALL,
                run_id=self.run_id,
                step_id=step_id,
                data={"id": call.id, "name": call.name, "args": call.args},
            )
        )

    async def tool_result(self, result: ToolResult, step_id: str | None = None) -> None:
        await self.publish(
            Event(
                type=EventType.TOOL_RESULT,
                run_id=self.run_id,
                step_id=step_id,
                data={
                    "id": result.tool_call_id,
                    "name": result.tool_name,
                    "status": result.status.value,
                    "output": result.output[:2_000],
                    "error": result.error,
                    "duration_ms": result.duration_ms,
                },
            )
        )

    async def artifact(self, art: Artifact, step_id: str | None = None) -> None:
        await self.publish(
            Event(
                type=EventType.ARTIFACT,
                run_id=self.run_id,
                step_id=step_id,
                data={
                    "kind": art.kind,
                    "filename": art.filename,
                    "mime_type": art.mime_type,
                    "size": len(art.content),
                },
            )
        )

    async def review(self, decision: ReviewDecision, step_id: str | None = None) -> None:
        await self.publish(
            Event(
                type=EventType.REVIEW,
                run_id=self.run_id,
                step_id=step_id,
                data=decision.model_dump(),
            )
        )

    async def log(self, text: str, level: str = "info") -> None:
        et = EventType.WARNING if level in {"warn", "warning", "error"} else EventType.LOG
        await self.publish(Event(type=et, run_id=self.run_id, data={"text": text, "level": level}))

    # -- async iteration (WebSocket bridge) -----------------------------
    def stream(self) -> _Subscription:
        """Context manager yielding an async iterator of live events."""
        return _Subscription(self)

    def replay(self, *, after_seq: int = 0, after_ts: float = 0.0) -> list[Event]:
        """Buffered events past a resume cursor.

        ``after_seq`` is the authoritative cursor: sequence numbers are unique
        and strictly increasing per bus, so the boundary is unambiguous.
        ``after_ts`` remains for callers that only hold a wall-clock mark — two
        events can share a timestamp, so resuming on ``ts`` alone can skip or
        repeat whatever sits on the boundary (ADR-0002).

        Keyword-only on purpose: this parameter used to be a positional
        timestamp, and a silently reinterpreted ``replay(12)`` would be worse
        than a ``TypeError``.
        """
        return [e for e in self._buffer if e.seq > after_seq and e.ts > after_ts]


class _Subscription:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._token: Listener | None = None

    def __enter__(self) -> AsyncIterator[Event]:
        async def _forward(event: Event) -> None:
            await self._queue.put(event)

        self._token = _forward
        self._bus.subscribe(_forward)
        return self.__aiter__()

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            self._bus.unsubscribe(self._token)

    def __aiter__(self) -> AsyncIterator[Event]:
        return self

    async def __anext__(self) -> Event:
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event

    def close(self) -> None:
        self._queue.put_nowait(None)


async def replay_and_stream(
    bus: EventBus, *, after_seq: int = 0, after_ts: float = 0.0
) -> AsyncIterator[Event]:
    """Hand a late subscriber the history, then the live feed — with no gap.

    The subscription is opened **before** the replay buffer is snapshotted. That
    ordering is the whole point: with the naive ``replay()`` → ``stream()``
    sequence every ``await`` spent delivering the backlog lets the run publish
    further events, and those land in neither the (already taken) snapshot nor
    the (not yet opened) subscription — they simply vanish. Here they are queued
    instead, and ids already delivered from the buffer are skipped, so each
    event is yielded exactly once, in publish order.

    ``after_seq`` resumes a disconnected client from the last event it actually
    received; see :meth:`EventBus.replay` for why the sequence number, not the
    timestamp, is the cursor of record.

    Consume it through :func:`contextlib.aclosing` so an early ``break`` (the
    transports stop at ``RUN_FINISHED``) unsubscribes deterministically rather
    than at garbage-collection time.
    """
    with bus.stream() as live:
        history = bus.replay(after_seq=after_seq, after_ts=after_ts)
        delivered = {event.id for event in history}
        for event in history:
            yield event
        async for event in live:
            if event.id in delivered:
                continue
            yield event


def parse_wire(raw: str) -> Event:
    return Event.model_validate(json.loads(raw))
