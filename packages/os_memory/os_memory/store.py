"""Durable memory storage: in-memory (default), SQLite, and a Postgres mirror.

Same single-table contract as ``os_skills.store`` — deliberately, not by
copy-paste drift: one protocol, three backends, and an explicit
:func:`memory_store_from_url` that *raises* on an unknown scheme. Silently
falling back to memory would fake a durability guarantee, which is the one thing
a memory layer must never do.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from os_memory.types import MemoryRecord, MemoryStatus

_DDL = (
    "CREATE TABLE IF NOT EXISTS memory("
    "memory_id TEXT PRIMARY KEY, fingerprint TEXT, key TEXT, status TEXT, "
    "record_json TEXT NOT NULL, updated_at REAL)"
)
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS memory_fingerprint ON memory(fingerprint)",
    "CREATE INDEX IF NOT EXISTS memory_key ON memory(key)",
    "CREATE INDEX IF NOT EXISTS memory_status ON memory(status)",
)


@runtime_checkable
class MemoryStore(Protocol):
    """What every backend must answer. Writes are upserts by ``memory_id``."""

    def save_record(self, record: MemoryRecord) -> None: ...
    def get_record(self, memory_id: str) -> MemoryRecord | None: ...
    def list_records(self, *, statuses: Iterable[str] | None = None) -> list[MemoryRecord]: ...
    def find_by_fingerprint(self, fingerprint: str) -> MemoryRecord | None: ...
    def find_by_key(self, key: str) -> list[MemoryRecord]: ...
    def delete_record(self, memory_id: str) -> bool: ...
    def close(self) -> None: ...


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}
        self._lock = threading.RLock()

    def save_record(self, record: MemoryRecord) -> None:
        with self._lock:
            record.updated_at = time.time()
            self._records[record.memory_id] = record.model_copy(deep=True)

    def get_record(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            found = self._records.get(memory_id)
        return found.model_copy(deep=True) if found else None

    def list_records(self, *, statuses: Iterable[str] | None = None) -> list[MemoryRecord]:
        wanted = {str(s) for s in statuses} if statuses is not None else None
        with self._lock:
            records = list(self._records.values())
        return [
            r.model_copy(deep=True) for r in records if wanted is None or r.status.value in wanted
        ]

    def find_by_fingerprint(self, fingerprint: str) -> MemoryRecord | None:
        with self._lock:
            for record in self._records.values():
                if record.fingerprint == fingerprint:
                    return record.model_copy(deep=True)
        return None

    def find_by_key(self, key: str) -> list[MemoryRecord]:
        with self._lock:
            return [r.model_copy(deep=True) for r in self._records.values() if r.key == key]

    def delete_record(self, memory_id: str) -> bool:
        with self._lock:
            return self._records.pop(memory_id, None) is not None

    def close(self) -> None:
        pass


class SQLiteMemoryStore:
    """One file, WAL, no schema migrations beyond ``CREATE IF NOT EXISTS``.

    Rows keep the whole record as JSON so the model can grow without an
    ALTER TABLE; the indexed columns are exactly the three the store is queried
    by. A corrupt row is *skipped with a count*, not fatal — losing one memory
    is better than losing recall for the whole platform, and
    :meth:`skipped` keeps the loss visible.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.skipped = 0
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.execute(_DDL)
            for statement in _INDEXES:
                self._conn.execute(statement)

    def save_record(self, record: MemoryRecord) -> None:
        record.updated_at = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memory VALUES(?,?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET "
                "fingerprint=excluded.fingerprint, key=excluded.key, status=excluded.status, "
                "record_json=excluded.record_json, updated_at=excluded.updated_at",
                (
                    record.memory_id,
                    record.fingerprint,
                    record.key,
                    record.status.value,
                    record.model_dump_json(),
                    record.updated_at,
                ),
            )

    def get_record(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT record_json FROM memory WHERE memory_id=?", (memory_id,)
            ).fetchone()
        return self._decode(row)

    def list_records(self, *, statuses: Iterable[str] | None = None) -> list[MemoryRecord]:
        wanted = [str(s) for s in statuses] if statuses is not None else None
        with self._lock:
            if wanted:
                marks = ",".join("?" for _ in wanted)
                rows = self._conn.execute(
                    f"SELECT record_json FROM memory WHERE status IN ({marks}) ORDER BY updated_at",
                    wanted,
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT record_json FROM memory ORDER BY updated_at"
                ).fetchall()
        return [record for record in (self._decode(row) for row in rows) if record is not None]

    def find_by_fingerprint(self, fingerprint: str) -> MemoryRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT record_json FROM memory WHERE fingerprint=? LIMIT 1", (fingerprint,)
            ).fetchone()
        return self._decode(row)

    def find_by_key(self, key: str) -> list[MemoryRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_json FROM memory WHERE key=? ORDER BY updated_at", (key,)
            ).fetchall()
        return [record for record in (self._decode(row) for row in rows) if record is not None]

    def delete_record(self, memory_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM memory WHERE memory_id=?", (memory_id,))
        return cur.rowcount > 0

    def _decode(self, row: tuple[object, ...] | None) -> MemoryRecord | None:
        if row is None:
            return None
        try:
            return MemoryRecord.model_validate_json(str(row[0]))
        except Exception:  # one unreadable row must not disable recall
            self.skipped += 1
            return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class PostgresMemoryStore:
    """The same contract over an injected DBAPI factory (mirrors ``os_skills``).

    The connection factory is a parameter on purpose: this prototype must not
    import ``psycopg``, so the driver arrives from the deployment that needs it
    and the tests arrive a fake.
    """

    def __init__(self, connect: object, *, table: str = "memory") -> None:
        self._connect = connect  # type: ignore[assignment]
        self.table = table
        self.skipped = 0
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self.table}("
                "memory_id TEXT PRIMARY KEY, fingerprint TEXT, key TEXT, status TEXT, "
                "record_json TEXT NOT NULL, updated_at DOUBLE PRECISION)"
            )

    def _conn(self):
        return self._connect()  # type: ignore[operator]

    def save_record(self, record: MemoryRecord) -> None:
        record.updated_at = time.time()
        sql = (
            f"INSERT INTO {self.table} (memory_id, fingerprint, key, status, record_json, "
            "updated_at) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (memory_id) DO UPDATE SET "
            "fingerprint = EXCLUDED.fingerprint, key = EXCLUDED.key, status = EXCLUDED.status, "
            "record_json = EXCLUDED.record_json, updated_at = EXCLUDED.updated_at"
        )
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    record.memory_id,
                    record.fingerprint,
                    record.key,
                    record.status.value,
                    record.model_dump_json(),
                    record.updated_at,
                ),
            )
            conn.commit()

    def list_records(self, *, statuses: Iterable[str] | None = None) -> list[MemoryRecord]:
        wanted = [str(s) for s in statuses] if statuses is not None else None
        sql = f"SELECT record_json FROM {self.table}"
        params: tuple[object, ...] = ()
        if wanted:
            marks = ",".join("%s" for _ in wanted)
            sql += f" WHERE status IN ({marks})"
            params = tuple(wanted)
        sql += " ORDER BY updated_at"
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [record for record in (self._decode(row) for row in rows) if record is not None]

    def get_record(self, memory_id: str) -> MemoryRecord | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT record_json FROM {self.table} WHERE memory_id = %s", (memory_id,))
            row = cur.fetchone()
        return self._decode(row)

    def find_by_fingerprint(self, fingerprint: str) -> MemoryRecord | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT record_json FROM {self.table} WHERE fingerprint = %s LIMIT 1",
                (fingerprint,),
            )
            row = cur.fetchone()
        return self._decode(row)

    def find_by_key(self, key: str) -> list[MemoryRecord]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT record_json FROM {self.table} WHERE key = %s ORDER BY updated_at", (key,)
            )
            rows = cur.fetchall()
        return [record for record in (self._decode(row) for row in rows) if record is not None]

    def delete_record(self, memory_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self.table} WHERE memory_id = %s", (memory_id,))
            deleted = cur.rowcount
            conn.commit()
        return bool(deleted)

    def _decode(self, row: object) -> MemoryRecord | None:
        if not row:
            return None
        try:
            return MemoryRecord.model_validate_json(str(row[0]))  # type: ignore[index]
        except Exception:
            self.skipped += 1
            return None

    def close(self) -> None:
        pass


def memory_store_from_url(url: str | None) -> MemoryStore:
    """Build a store from ``memory://`` / ``sqlite:///<path>`` / a bare path.

    Unknown schemes raise. A typo like ``postges://`` that quietly became an
    in-memory store would hand the operator a memory layer that forgets
    everything on restart — the exact failure a durable store exists to prevent.
    """
    if not url or url == "memory://":
        return InMemoryMemoryStore()
    if url.startswith("sqlite:///"):
        return SQLiteMemoryStore(url[len("sqlite:///") :])
    if url.startswith("postgres://") or url.startswith("postgresql://"):
        raise ValueError(
            "Postgres memory needs an injected DBAPI factory: use "
            "PostgresMemoryStore(connect=...) explicitly, as os_skills does."
        )
    if url.startswith("memory:") or url.startswith("sqlite:"):
        raise ValueError(f"unsupported memory store url: {url!r} (use sqlite:///<path>)")
    if "://" in url:
        raise ValueError(f"unsupported memory store url: {url!r}")
    return SQLiteMemoryStore(url)


#: Statuses recall may read. Quarantined/expired are excluded on purpose: they
#: stay in the store for audit, never in the context window.
RECALLABLE: tuple[str, ...] = (MemoryStatus.CANDIDATE.value, MemoryStatus.ACTIVE.value)


__all__ = [
    "RECALLABLE",
    "InMemoryMemoryStore",
    "MemoryStore",
    "PostgresMemoryStore",
    "SQLiteMemoryStore",
    "memory_store_from_url",
]
