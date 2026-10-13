"""Run-outcome ledger — one append-only line per execution, on disk.

The event bus shows a run *while* it happens and :class:`~os_core.state.RunState`
describes a run *while it is still in memory*. Neither answers the question an
operator asks the morning after: how many runs did we execute, and how many of
them actually finished? That question needs a record that outlives the process.

:class:`RunLedger` is that record, and it is deliberately boring:

* **Append-only JSON Lines** — one JSON object per line, so a torn write at the
  end of the file costs one entry, never the history, and unreadable lines are
  skipped and counted instead of breaking the read.
* **No new dependency** — a file handle, a lock and ``os.fsync``. A database
  behind this interface belongs to roadmap phase 4 (persistent store), not to
  the ledger.
* **A coarse verdict of its own** — :class:`RunOutcome` has four values instead
  of mirroring :class:`~os_core.state.RunStatus`, so a new run status can be
  added without silently changing what "succeeded" means in the aggregate.

The ledger is written by :meth:`os_orchestration.platform.Platform.run` in a
``finally`` block, so an execution is recorded whether it completed, failed,
exhausted its budget or was interrupted.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from os_core.state import RunState, RunStatus

__all__ = [
    "DEFAULT_LEDGER_FILENAME",
    "LEDGER_DISABLED_VALUES",
    "LEDGER_ENV_VAR",
    "LEDGER_SCHEMA_VERSION",
    "MAX_TASK_CHARS",
    "LedgerEntry",
    "LedgerError",
    "RunLedger",
    "RunOutcome",
    "outcome_for",
    "resolve_ledger",
]

LEDGER_SCHEMA_VERSION: int = 1
LEDGER_ENV_VAR: str = "CELA_LEDGER_PATH"
DEFAULT_LEDGER_FILENAME: str = ".cela/ledger.jsonl"

#: Values of ``CELA_LEDGER_PATH`` that mean "do not write a ledger at all".
LEDGER_DISABLED_VALUES: frozenset[str] = frozenset({"", "0", "off", "none", "disabled", "false"})

#: The task text is context, not payload — the full prompt lives in the run
#: state and in the event bus, never duplicated on disk unbounded.
MAX_TASK_CHARS: int = 400


class RunOutcome(StrEnum):
    """The ledger's verdict: four values that cannot drift with ``RunStatus``."""

    SUCCESS = "success"  # produced a final answer
    FAILED = "failed"  # error, guardrail rejection, planner failure
    BUDGET = "budget"  # stopped by the budget/reviewer without an answer
    ABORTED = "aborted"  # cancelled, or interrupted before reaching a terminal state


_OUTCOME_BY_STATUS: dict[RunStatus, RunOutcome] = {
    RunStatus.COMPLETED: RunOutcome.SUCCESS,
    RunStatus.FAILED: RunOutcome.FAILED,
    RunStatus.MAX_STEPS: RunOutcome.BUDGET,
    RunStatus.CANCELLED: RunOutcome.ABORTED,
}


def outcome_for(status: RunStatus) -> RunOutcome:
    """Map a run status onto the ledger's verdict.

    Any status that is *not* terminal (``pending`` / ``running`` /
    ``awaiting_approval``) is reported as :attr:`RunOutcome.ABORTED`: the run
    was interrupted before it could decide for itself.
    """
    return _OUTCOME_BY_STATUS.get(status, RunOutcome.ABORTED)


class LedgerError(RuntimeError):
    """Raised when the ledger cannot be written. Never raised by a read."""


class LedgerEntry(BaseModel):
    """One line of the ledger: the outcome of one execution."""

    schema_version: int = LEDGER_SCHEMA_VERSION
    seq: int = 0
    run_id: str = ""
    task: str = ""
    mode: str = "react"
    status: str = RunStatus.PENDING.value
    outcome: RunOutcome = RunOutcome.ABORTED
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: float | None = None
    steps_used: int = 0
    tokens_used: int = 0
    replans: int = 0
    reviews: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    artifacts: list[str] = Field(default_factory=list)
    error: str | None = None
    recorded_at: str = Field(default_factory=lambda: _now_iso())
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_run(
        cls, run: RunState, *, seq: int, metadata: dict[str, Any] | None = None
    ) -> LedgerEntry:
        """Project a :class:`~os_core.state.RunState` onto one ledger line."""
        return cls(
            seq=seq,
            run_id=run.run_id,
            task=run.task[:MAX_TASK_CHARS],
            mode=run.mode,
            status=run.status.value,
            outcome=outcome_for(run.status),
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_ms=_duration_ms(run.started_at, run.finished_at),
            steps_used=run.steps_used,
            tokens_used=run.tokens_used,
            replans=run.replans,
            reviews=run.reviews,
            tool_calls=len(run.results),
            tool_failures=sum(1 for r in run.results if not r.ok),
            artifacts=[a.filename for a in run.artifacts],
            error=run.error,
            metadata=metadata or {},
        )

    def to_line(self) -> str:
        """Serialize to one JSON line (no trailing newline)."""
        return self.model_dump_json()


class RunLedger:
    """An append-only, fsync'd JSON Lines log of run outcomes.

    ``RunLedger(path)`` is safe to share between threads (the server records
    from asyncio task callbacks) — appends are serialised by a lock, and the
    line sequence keeps counting from whatever is already on disk.
    """

    def __init__(self, path: str | Path, *, fsync: bool = True) -> None:
        self.path: Path = Path(path).expanduser()
        self.fsync: bool = fsync
        self._lock = threading.Lock()
        self._seq: int = self._count_lines()

    # -- write ----------------------------------------------------------
    def record(self, run: RunState, *, metadata: dict[str, Any] | None = None) -> LedgerEntry:
        """Append the outcome of ``run`` and return the entry that was written.

        Raises :class:`LedgerError` if the file cannot be written. Callers that
        must not fail because the *log* failed (the platform) catch it and
        report it on the event bus instead.
        """
        with self._lock:
            self._seq += 1
            entry = LedgerEntry.from_run(run, seq=self._seq, metadata=metadata)
            self._append(entry.to_line())
            return entry

    def _append(self, line: str) -> None:
        try:
            parent = self.path.parent
            if str(parent):
                parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                if self.fsync:
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise LedgerError(f"cannot append to {self.path}: {exc}") from exc

    # -- read -----------------------------------------------------------
    def read(self) -> tuple[list[LedgerEntry], int]:
        """Return ``(entries, unreadable_lines)`` in file order.

        A line that is not valid JSON, or that no longer validates, is skipped
        and counted: one truncated append must not make the history unreadable.
        """
        if not self.path.exists():
            return [], 0
        entries: list[LedgerEntry] = []
        corrupt = 0
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LedgerError(f"cannot read {self.path}: {exc}") from exc
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                entries.append(LedgerEntry.model_validate(json.loads(line)))
            except (json.JSONDecodeError, ValidationError, TypeError):
                corrupt += 1
        return entries, corrupt

    def entries(self, *, limit: int | None = None) -> list[LedgerEntry]:
        """Return every entry, or the last ``limit`` of them (newest last)."""
        found, _corrupt = self.read()
        if limit is None or limit >= len(found):
            return found
        return found[-limit:] if limit > 0 else []

    def entry_for(self, run_id: str) -> LedgerEntry | None:
        """The newest entry recorded for ``run_id``, if any."""
        found, _corrupt = self.read()
        for entry in reversed(found):
            if entry.run_id == run_id:
                return entry
        return None

    def summary(self) -> dict[str, Any]:
        """Aggregate the whole ledger — the "how did we do" answer."""
        found, corrupt = self.read()
        outcomes: dict[str, int] = {value.value: 0 for value in RunOutcome}
        modes: dict[str, int] = {}
        statuses: dict[str, int] = {}
        durations: list[float] = []
        totals = {
            "steps_used": 0,
            "tokens_used": 0,
            "tool_calls": 0,
            "tool_failures": 0,
            "replans": 0,
            "reviews": 0,
        }
        for entry in found:
            outcomes[entry.outcome.value] = outcomes.get(entry.outcome.value, 0) + 1
            modes[entry.mode] = modes.get(entry.mode, 0) + 1
            statuses[entry.status] = statuses.get(entry.status, 0) + 1
            if entry.duration_ms is not None:
                durations.append(entry.duration_ms)
            for key in totals:
                totals[key] += getattr(entry, key)
        total = len(found)
        return {
            "total": total,
            "outcomes": outcomes,
            "modes": modes,
            "statuses": statuses,
            "success_rate": (outcomes[RunOutcome.SUCCESS.value] / total) if total else 0.0,
            "totals": totals,
            "duration_ms": _duration_stats(durations),
            "first_recorded_at": found[0].recorded_at if found else None,
            "last_recorded_at": found[-1].recorded_at if found else None,
            "corrupt_lines": corrupt,
        }

    def describe(self) -> dict[str, Any]:
        """Where the ledger lives and how much is in it (safe to expose)."""
        entries, corrupt = self.read()
        return {
            "enabled": True,
            "path": str(self.path),
            "schema_version": LEDGER_SCHEMA_VERSION,
            "entries": len(entries),
            "corrupt_lines": corrupt,
        }

    # -- internals ------------------------------------------------------
    def _count_lines(self) -> int:
        """Existing line count, so ``seq`` keeps increasing across restarts."""
        if not self.path.exists():
            return 0
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0


def resolve_ledger(
    path: str | Path | None = None,
    *,
    env_var: str = LEDGER_ENV_VAR,
    fsync: bool = True,
) -> RunLedger | None:
    """Resolve the process ledger from an argument or ``CELA_LEDGER_PATH``.

    Returns ``None`` (no ledger) when the value is one of
    :data:`LEDGER_DISABLED_VALUES`, otherwise a :class:`RunLedger` at the given
    path. An unset variable falls back to ``./.cela/ledger.jsonl`` relative to
    the working directory, which keeps the default honest: it is visible next
    to the process that wrote it, and it is git-ignored.
    """
    resolved = path if path is not None else os.environ.get(env_var)
    if resolved is None:
        resolved = DEFAULT_LEDGER_FILENAME
    if str(resolved).strip().lower() in LEDGER_DISABLED_VALUES:
        return None
    return RunLedger(Path(resolved), fsync=fsync)


# -- helpers -------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _duration_ms(started_at: str | None, finished_at: str | None) -> float | None:
    """Wall-clock duration of a run in milliseconds, or ``None`` if unknown."""
    start = _parse_iso(started_at) if started_at else None
    end = _parse_iso(finished_at) if finished_at else None
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() * 1000.0, 3)


def _percentile(sorted_values: list[float], quantile: float) -> float:
    """Linear-interpolation percentile — no numpy for a five-value summary."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    fraction = position - low
    return round(sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction, 3)


def _duration_stats(durations: list[float]) -> dict[str, float]:
    ordered = sorted(durations)
    return {
        "count": len(ordered),
        "mean": round(sum(ordered) / len(ordered), 3) if ordered else 0.0,
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "max": ordered[-1] if ordered else 0.0,
    }
