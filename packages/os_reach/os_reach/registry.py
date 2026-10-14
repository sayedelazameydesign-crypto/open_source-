"""Reach registry — routing (URL → channel) and health collection.

Mirrors the two behaviours worth keeping from Agent-Reach's doctor:

* **One misbehaving channel never takes the report down.** A probe that raises
  degrades to ``status=error`` for that channel alone, with the exception text
  as the message (scrubbed).
* **Health is a snapshot, not a promise.** ``active_backend`` is what answered
  *now*; the report carries ``generated_at`` for exactly that reason.
"""

from __future__ import annotations

from typing import Any

from os_tools.egress import EgressPolicy
from pydantic import BaseModel, Field

from os_reach.contract import ChannelHealth, ReachChannel, ReachReport, ReachState
from os_reach.native import native_channels


class ReachRegistry(BaseModel):
    """Ordered channel map. Registration order *is* routing priority."""

    channels: dict[str, Any] = Field(default_factory=dict)
    model_config = {"arbitrary_types_allowed": True}

    def register(self, channel: ReachChannel) -> None:
        self.channels[channel.name] = channel

    def get(self, name: str) -> ReachChannel | None:
        return self.channels.get(name)

    def names(self) -> list[str]:
        return list(self.channels)

    def all(self) -> list[ReachChannel]:
        return list(self.channels.values())

    def channel_for(self, url: str) -> ReachChannel | None:
        """First channel that claims *url*; register catch-alls last."""
        for channel in self.all():
            try:
                if channel.can_handle(url):
                    return channel
            except Exception:  # a broken matcher must not break routing
                continue
        return None

    def probe(self, channel: ReachChannel) -> ChannelHealth:
        try:
            return channel.probe()
        except Exception as exc:
            return ChannelHealth(
                channel=getattr(channel, "name", "unknown"),
                status=ReachState.ERROR,
                message=f"probe failed: {type(exc).__name__}: {exc}",
            )

    def report(self, *, source: str = "local") -> ReachReport:
        return ReachReport(
            channels={health.channel: health for health in (self.probe(c) for c in self.all())},
            source=source,
        )

    def probe_all(self, *, source: str = "local") -> ReachReport:
        """Alias kept for callers that read better with the explicit verb."""
        return self.report(source=source)


def build_native_registry(
    *,
    policy: EgressPolicy | None = None,
    transport: Any = None,
) -> ReachRegistry:
    """A registry holding only the tier-0 native channels."""
    registry = ReachRegistry()
    for channel in native_channels(policy=policy, transport=transport):
        registry.register(channel)
    return registry
