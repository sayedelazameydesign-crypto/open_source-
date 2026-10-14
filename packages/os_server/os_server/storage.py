"""Durable run storage behind a repository seam (Roadmap phase 4, step 1).

``ApiRuntime`` used to keep final states, artifacts, events and WebSocket
tickets in plain process-local dicts, so a restart lost every finished run
and a second server instance could never see them. This module extracts
that state behind :class:`RunStore`:

* :class:`InMemoryRunStore` — the historical behaviour; the default, so the
  demo and the test-suite stay hermetic with zero configuration.
* :class:`SQLiteRunStore` — durable, single-file storage on the standard
  library only. It survives process restarts, preserves the ``seq`` resume
  cursor exactly (events are keyed by ``(run_id, seq)``), and stores
  WebSocket tickets **hashed** (SHA-256) so a leaked database file does not
  leak live credentials. Suitable for a single host / single writer; the
  protocol is the seam a Postgres (states/artifacts) + Redis (events /
  tickets) implementation plugs into for multi-instance deployments
  without touching ``ApiRuntime`` or any route.

Selection is explicit, never silent: :func:`run_store_from_url` accepts
``memory://``, ``sqlite:///<path>`` (or a bare path) and raises for
``postgres://`` / ``redis://`` until those backends land, instead of
quietly falling back to memory and losing durability guarantees.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from os_core.events import Event
from os_core.state import RunState
from pydantic import BaseModel, ConfigDict, Field


class RunMeta(BaseModel):
    """Book-keeping a store keeps about a run independently of its state."""

    run_id: str
    mode: str = "auto"
    target_url: str | None = None
    status: str = "running"
    created_at: float = Field(default_factory=time.time)


class StoredTicket(BaseModel):
    run_id: str
    expires_at: float  # wall-clock epoch seconds, so tickets survive restarts


def _ticket_key(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


@runtime_checkable
class RunStore(Protocol):
    """Persistence seam for run state, events, artifacts and WS tickets."""

    def save_run_meta(self, meta: RunMeta) -> None: ...
    def get_run_meta(self, run_id: str) -> RunMeta | None: ...
    def has_run(self, run_id: str) -> bool: ...
    def list_run_ids(self) -> list[str]: ...
    def save_state(self, state: RunState) -> None: ...
    def get_state(self, run_id: str) -> RunState | None: ...
    def save_event(self, event: Event) -> None: ...
    def list_events(self, run_id: str, *, after_seq: int = 0) -> list[Event]: ...
    def last_seq(self, run_id: str) -> int: ...
    def save_artifact(self, run_id: str, filename: str, data: dict[str, Any]) -> None: ...
    def get_artifact(self, run_id: str, filename: str) -> dict[str, Any] | None: ...
    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]: ...
    def save_ticket(self, ticket: str, run_id: str, expires_at: float) -> None: ...
    def pop_ticket(self, ticket: str) -> StoredTicket | None: ...
    def prune_tickets(self, now: float) -> None: ...
    def close(self) -> None: ...


class InMemoryRunStore:
    """Process-local store — identical semantics to the old runtime dicts."""

    def __init__(self) -> None:
        self.metas: dict[str, RunMeta] = {}
        self.states: dict[str, RunState] = {}
        self.events: dict[str, list[Event]] = {}
        self.artifacts: dict[str, dict[str, dict[str, Any]]] = {}
        self.tickets: dict[str, StoredTicket] = {}

    def save_run_meta(self, meta: RunMeta) -> None:
        self.metas[meta.run_id] = meta

    def get_run_meta(self, run_id: str) -> RunMeta | None:
        return self.metas.get(run_id)

    def has_run(self, run_id: str) -> bool:
        return run_id in self.metas or run_id in self.states

    def list_run_ids(self) -> list[str]:
        return list(dict.fromkeys([*self.metas, *self.states]))

    def save_state(self, state: RunState) -> None:
        self.states[state.run_id] = state
        meta = self.metas.get(state.run_id) or RunMeta(run_id=state.run_id)
        meta.status = state.status.value
        meta.mode = state.mode
        self.metas[state.run_id] = meta

    def get_state(self, run_id: str) -> RunState | None:
        return self.states.get(run_id)

    def save_event(self, event: Event) -> None:
        if not event.run_id:
            return
        buf = self.events.setdefault(event.run_id, [])
        if all(existing.seq != event.seq for existing in buf):
            buf.append(event)

    def list_events(self, run_id: str, *, after_seq: int = 0) -> list[Event]:
        return [e for e in self.events.get(run_id, []) if e.seq > after_seq]

    def last_seq(self, run_id: str) -> int:
        return max((e.seq for e in self.events.get(run_id, [])), default=0)

    def save_artifact(self, run_id: str, filename: str, data: dict[str, Any]) -> None:
        self.artifacts.setdefault(run_id, {})[filename] = data

    def get_artifact(self, run_id: str, filename: str) -> dict[str, Any] | None:
        return self.artifacts.get(run_id, {}).get(filename)

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.artifacts.get(run_id, {}).values())

    def save_ticket(self, ticket: str, run_id: str, expires_at: float) -> None:
        self.tickets[_ticket_key(ticket)] = StoredTicket(run_id=run_id, expires_at=expires_at)

    def pop_ticket(self, ticket: str) -> StoredTicket | None:
        return self.tickets.pop(_ticket_key(ticket), None)

    def prune_tickets(self, now: float) -> None:
        for key in [k for k, v in self.tickets.items() if v.expires_at <= now]:
            del self.tickets[key]

    def close(self) -> None:
        pass


class SQLiteRunStore:
    """Single-file durable store (WAL) built only on :mod:`sqlite3`.

    One connection guarded by a lock: writes from the event collector are
    short and serialised, which keeps ``seq`` ordering exact. This is a
    single-writer store — multi-instance deployments implement
    :class:`RunStore` over Postgres + Redis instead (see module docstring).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS run_meta(
                    run_id TEXT PRIMARY KEY, mode TEXT, target_url TEXT,
                    status TEXT, created_at REAL);
                CREATE TABLE IF NOT EXISTS run_states(
                    run_id TEXT PRIMARY KEY, state_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_events(
                    run_id TEXT NOT NULL, seq INTEGER NOT NULL, event_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, seq));
                CREATE TABLE IF NOT EXISTS run_artifacts(
                    run_id TEXT NOT NULL, filename TEXT NOT NULL, data_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, filename));
                CREATE TABLE IF NOT EXISTS ws_tickets(
                    ticket_hash TEXT PRIMARY KEY, run_id TEXT NOT NULL, expires_at REAL);
                """
            )

    # -- runs / states ---------------------------------------------------
    def save_run_meta(self, meta: RunMeta) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO run_meta VALUES(?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET mode=excluded.mode, "
                "target_url=excluded.target_url, status=excluded.status",
                (meta.run_id, meta.mode, meta.target_url, meta.status, meta.created_at),
            )

    def get_run_meta(self, run_id: str) -> RunMeta | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id, mode, target_url, status, created_at FROM run_meta WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return RunMeta(
            run_id=row[0],
            mode=row[1] or "auto",
            target_url=row[2],
            status=row[3] or "running",
            created_at=row[4] or 0.0,
        )

    def has_run(self, run_id: str) -> bool:
        return self.get_run_meta(run_id) is not None or self.get_state(run_id) is not None

    def list_run_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id FROM run_meta UNION SELECT run_id FROM run_states"
            ).fetchall()
        return [row[0] for row in rows]

    def save_state(self, state: RunState) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO run_states VALUES(?,?) ON CONFLICT(run_id) "
                "DO UPDATE SET state_json=excluded.state_json",
                (state.run_id, state.model_dump_json()),
            )
            self._conn.execute(
                "INSERT INTO run_meta(run_id, mode, status, created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, mode=excluded.mode",
                (state.run_id, state.mode, state.status.value, time.time()),
            )

    def get_state(self, run_id: str) -> RunState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json FROM run_states WHERE run_id=?", (run_id,)
            ).fetchone()
        return RunState.model_validate_json(row[0]) if row else None

    # -- events ------------------------------------------------------------
    def save_event(self, event: Event) -> None:
        if not event.run_id or not event.seq:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO run_events VALUES(?,?,?)",
                (event.run_id, event.seq, event.model_dump_json()),
            )

    def list_events(self, run_id: str, *, after_seq: int = 0) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_json FROM run_events WHERE run_id=? AND seq>? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [Event.model_validate_json(row[0]) for row in rows]

    def last_seq(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM run_events WHERE run_id=?", (run_id,)
            ).fetchone()
        return int(row[0])

    # -- artifacts -----------------------------------------------------------
    def save_artifact(self, run_id: str, filename: str, data: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO run_artifacts VALUES(?,?,?) "
                "ON CONFLICT(run_id, filename) DO UPDATE SET data_json=excluded.data_json",
                (run_id, filename, json.dumps(data, ensure_ascii=False)),
            )

    def get_artifact(self, run_id: str, filename: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data_json FROM run_artifacts WHERE run_id=? AND filename=?",
                (run_id, filename),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data_json FROM run_artifacts WHERE run_id=? ORDER BY filename", (run_id,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    # -- tickets (hashed at rest) ---------------------------------------------
    def save_ticket(self, ticket: str, run_id: str, expires_at: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO ws_tickets VALUES(?,?,?)",
                (_ticket_key(ticket), run_id, expires_at),
            )

    def pop_ticket(self, ticket: str) -> StoredTicket | None:
        key = _ticket_key(ticket)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT run_id, expires_at FROM ws_tickets WHERE ticket_hash=?", (key,)
            ).fetchone()
            if row is not None:
                self._conn.execute("DELETE FROM ws_tickets WHERE ticket_hash=?", (key,))
        return StoredTicket(run_id=row[0], expires_at=row[1]) if row else None

    def prune_tickets(self, now: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM ws_tickets WHERE expires_at<=?", (now,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def run_store_from_url(url: str | None) -> RunStore:
    """Build a store from ``memory://`` / ``sqlite:///<path>`` / a bare path.

    ``postgres://`` and ``redis://`` are recognised but rejected loudly:
    silently downgrading them to memory would fake a durability guarantee.
    """
    if not url or url == "memory://":
        return InMemoryRunStore()
    if url.startswith(("postgres://", "postgresql://", "redis://", "rediss://")):
        raise RuntimeError(
            f"run store backend {url.split('://', 1)[0]!r} is not implemented yet; "
            "implement RunStore over Postgres/Redis or use sqlite:///<path>"
        )
    if url.startswith("sqlite:///"):
        return SQLiteRunStore(url[len("sqlite:///") :])
    if url.startswith("sqlite://"):
        raise ValueError("use sqlite:///<path> (three slashes) or a bare filesystem path")
    return SQLiteRunStore(url)


__all__ = [
    "InMemoryRunStore",
    "RunMeta",
    "RunStore",
    "SQLiteRunStore",
    "StoredTicket",
    "run_store_from_url",
]
