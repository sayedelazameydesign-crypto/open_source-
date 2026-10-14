"""Reach audit trail — one record per CLI channel invocation.

The audit answers "what did the agent do with a host-side capability?" without
becoming a second copy of the data: the query is stored as a **hash and a
length**, never as text, and every free-form field passes through
:func:`~os_reach.security.scrub_credentials` first (upstream tools echo the URLs
and flags they were configured with).

Three sinks, all optional and independent:

* the in-memory ring (``entries``, bounded) — always on, used by tests and the
  doctor surfaces;
* a JSONL file (``path``) — append-only, created ``0600``;
* a ``sink`` callable — how the platform wires these into its own event stream
  without :mod:`os_reach` importing anything from :mod:`os_core`.

A sink that raises must never take the tool call down, so failures are recorded
on the entry (``sink_error``) and swallowed.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from os_reach.security import scrub_credentials

#: How much of the query hash is kept — enough to correlate two records, too
#: little to brute-force a short query back out of it.
HASH_CHARS = 16


def query_fingerprint(query: str) -> tuple[str, int]:
    """Return ``(sha256-prefix, length)`` for a query string."""
    digest = hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()
    return digest[:HASH_CHARS], len(query)


class ReachAuditEntry(BaseModel):
    """One CLI channel call: who asked, what was decided, what came back."""

    timestamp: float = Field(default_factory=time.time)
    channel: str
    tool: str = ""
    decision: str = "allowed"  # "allowed" | "denied"
    code: str = ""  # the failure code when the call did not produce a result
    query_sha256: str = ""
    query_chars: int = 0
    exit_code: int | None = None
    bytes_out: int = 0
    duration_ms: float = 0.0
    correlation_id: str = ""
    user: str = ""
    detail: str = ""
    sink_error: str = ""

    @classmethod
    def for_call(
        cls,
        *,
        channel: str,
        tool: str,
        query: str,
        user: str = "",
        correlation_id: str = "",
    ) -> ReachAuditEntry:
        fingerprint, chars = query_fingerprint(query)
        return cls(
            channel=channel,
            tool=tool,
            query_sha256=fingerprint,
            query_chars=chars,
            user=user,
            correlation_id=correlation_id,
        )

    def to_jsonl(self) -> str:
        return json.dumps(self.model_dump(), ensure_ascii=False, sort_keys=True)

    def render(self) -> str:
        outcome = self.code or ("ok" if self.decision == "allowed" else "denied")
        return (
            f"{self.channel}: {self.decision} ({outcome}) "
            f"query#{self.query_sha256[:8]}/{self.query_chars}ch "
            f"exit={self.exit_code} bytes={self.bytes_out}"
        )


class ReachAuditLog(BaseModel):
    """Bounded in-memory audit ring with optional JSONL and event sinks."""

    entries: list[ReachAuditEntry] = Field(default_factory=list)
    path: Path | None = None
    sink: Callable[[ReachAuditEntry], None] | None = Field(default=None, exclude=True)
    max_entries: int = 1000
    model_config = {"arbitrary_types_allowed": True}

    def record(self, entry: ReachAuditEntry) -> ReachAuditEntry:
        """Append one entry to every configured sink; never raise at the caller."""
        entry.detail = scrub_credentials(entry.detail)[:500]
        self.entries.append(entry)
        if len(self.entries) > self.max_entries:
            del self.entries[: len(self.entries) - self.max_entries]
        if self.path is not None:
            entry.sink_error = self._append_jsonl(entry) or entry.sink_error
        if self.sink is not None:
            try:
                self.sink(entry)
            except Exception as exc:
                entry.sink_error = f"{type(exc).__name__}: {scrub_credentials(str(exc))}"
        return entry

    def _append_jsonl(self, entry: ReachAuditEntry) -> str:
        try:
            path = Path(self.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            existed = path.exists()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(entry.to_jsonl() + "\n")
            if not existed and os.name != "nt":
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            return f"{type(exc).__name__}: {exc}"
        return ""

    def summary(self) -> dict[str, int]:
        summary = {"allowed": 0, "denied": 0}
        for entry in self.entries:
            summary[entry.decision] = summary.get(entry.decision, 0) + 1
        return summary

    def denied(self) -> list[ReachAuditEntry]:
        return [entry for entry in self.entries if entry.decision == "denied"]


def default_audit_path() -> Path | None:
    """``$CELA_REACH_AUDIT_LOG`` when set — no implicit file is ever created."""
    configured = os.environ.get("CELA_REACH_AUDIT_LOG", "").strip()
    return Path(configured) if configured else None
