"""Reach contract — channel health, tiers and the Agent-Reach wire shape.

Adopted from `Agent-Reach <https://github.com/Panniantong/Agent-Reach>`_ (MIT):
its ``doctor --json`` contract, because that is the part of the upstream
project this platform actually consumes. A channel reports

* ``status`` — ``ok`` | ``warn`` | ``off`` | ``error``,
* ``tier`` — 0 zero-config, 1 needs a free key, 2 needs setup,
* ``backends`` — an **ordered** candidate list (first is preferred), and
* ``active_backend`` — the backend really serving the channel right now, or
  ``None``. Presence of a file is not health; the upstream probe executes a
  lightweight command before claiming a backend, and so do we.

Nothing in this module touches the network: a channel's :meth:`probe` is a
local capability check, and remote health (if any) arrives as data.
"""

from __future__ import annotations

import time
from enum import IntEnum, StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from os_reach.security import scrub_credentials


class ReachState(StrEnum):
    """Channel health, mirroring the upstream doctor statuses."""

    OK = "ok"
    WARN = "warn"
    OFF = "off"
    ERROR = "error"

    @property
    def explains_absence(self) -> bool:
        """True when the channel is unusable but the report is still trustworthy."""
        return self in {ReachState.OFF, ReachState.ERROR}


class ReachTier(IntEnum):
    """How much setup a channel needs before it works."""

    ZERO_CONFIG = 0
    FREE_KEY = 1
    SETUP = 2


class ChannelHealth(BaseModel):
    """One channel's health, with every operator-visible string scrubbed."""

    channel: str
    status: ReachState
    message: str = ""
    label: str = ""
    tier: ReachTier = ReachTier.ZERO_CONFIG
    backends: list[str] = Field(default_factory=list)
    active_backend: str | None = None

    @property
    def usable(self) -> bool:
        return self.status in {ReachState.OK, ReachState.WARN}


class ReachReport(BaseModel):
    """A snapshot of every known channel, from local probes or a remote doctor."""

    channels: dict[str, ChannelHealth] = Field(default_factory=dict)
    source: str = "local"
    generated_at: float = Field(default_factory=time.time)

    def summary(self) -> dict[str, int]:
        counts = {state.value: 0 for state in ReachState}
        for health in self.channels.values():
            counts[health.status.value] += 1
        return counts

    def usable_channels(self) -> list[str]:
        return sorted(name for name, health in self.channels.items() if health.usable)

    def render(self) -> str:
        """One line per channel — what a human (or a model) needs to route."""
        lines = [f"reach report ({self.source}) — {self.summary()}"]
        for name in sorted(self.channels):
            health = self.channels[name]
            backend = f" [{health.active_backend}]" if health.active_backend else ""
            detail = f" — {health.message}" if health.message else ""
            lines.append(f"  {name}: {health.status.value}{backend}{detail}")
        return "\n".join(lines)


@runtime_checkable
class ReachChannel(Protocol):
    """A content channel: does this URL belong to me, and am I healthy?"""

    name: str
    description: str
    tier: ReachTier
    backends: list[str]

    def can_handle(self, url: str) -> bool: ...

    def probe(self) -> ChannelHealth: ...


def _coerce_state(raw: Any) -> ReachState:
    """Map a status string onto our enum; anything unknown fails closed."""
    try:
        return ReachState(str(raw).strip().lower())
    except ValueError:
        return ReachState.ERROR


def _coerce_tier(raw: Any) -> ReachTier:
    try:
        return ReachTier(int(raw))
    except (TypeError, ValueError):
        return ReachTier.SETUP


def parse_doctor_json(payload: dict[str, Any], *, source: str = "doctor") -> ReachReport:
    """Read an Agent-Reach ``doctor --json`` payload into a :class:`ReachReport`.

    Tolerant by design: the upstream writer may add keys, and a channel entry
    may be partial. An *unknown* status is reported as ``error`` rather than
    ignored — a status we do not understand is not evidence of health.
    """
    channels: dict[str, ChannelHealth] = {}
    for name, entry in (payload or {}).items():
        if not isinstance(entry, dict):
            channels[str(name)] = ChannelHealth(
                channel=str(name),
                status=ReachState.ERROR,
                message="malformed channel entry (expected an object)",
            )
            continue
        message = entry.get("message") or ""
        if not isinstance(message, str):
            message = str(message)
        backends = entry.get("backends") or []
        if not isinstance(backends, list):
            backends = [str(backends)]
        active = entry.get("active_backend")
        channels[str(name)] = ChannelHealth(
            channel=str(name),
            status=_coerce_state(entry.get("status", "")),
            message=scrub_credentials(message),
            label=str(entry.get("name", "") or ""),
            tier=_coerce_tier(entry.get("tier", ReachTier.SETUP)),
            backends=[str(b) for b in backends],
            active_backend=str(active) if active else None,
        )
    return ReachReport(channels=channels, source=source)
