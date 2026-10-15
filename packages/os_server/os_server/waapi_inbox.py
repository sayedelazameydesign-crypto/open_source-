"""Persistent, deduplicated inbox for authenticated WaAPI message events."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from os_server.waapi_outbox import _message_body, _required_text


class InboxIdempotencyConflict(RuntimeError):
    """A provider message ID was reused with a different event payload."""


class InboxStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    REPLY_QUEUED = "reply_queued"
    UNKNOWN = "unknown"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class InboxMessage:
    inbox_id: str
    instance_id: str
    provider_message_id: str
    chat_id: str = field(repr=False)
    text: str = field(repr=False)
    timestamp: int | None
    status: InboxStatus
    attempts: int
    created_at: float
    updated_at: float
    run_id: str | None = None
    reply_message_id: str | None = None
    error_code: str | None = None

    def public_status(self) -> dict[str, object]:
        return {
            "inbox_id": self.inbox_id,
            "instance_id": self.instance_id,
            "provider_message_id": self.provider_message_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "run_id": self.run_id,
            "reply_message_id": self.reply_message_id,
            "error_code": self.error_code,
        }


_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class SQLiteWaapiInbox:
    """WAL-backed inbox; event IDs are unique per WaAPI instance.

    Accepted events are committed before the webhook responds. Work left in
    ``processing`` after a crash is converted to ``unknown`` (never re-run),
    because the agent may already have performed side effects.
    """

    def __init__(self, path: str | Path = ".cela/waapi-outbox.sqlite3") -> None:
        raw_path = str(path)
        self.path = Path(raw_path)
        if raw_path != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(raw_path, check_same_thread=False, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS waapi_inbox (
                    inbox_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    provider_message_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    event_timestamp INTEGER,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'processing', 'reply_queued', 'unknown', 'failed')
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    run_id TEXT,
                    reply_message_id TEXT,
                    error_code TEXT,
                    UNIQUE(instance_id, provider_message_id)
                );
                CREATE INDEX IF NOT EXISTS waapi_inbox_status_created
                    ON waapi_inbox(status, created_at);
                """
            )

    @staticmethod
    def _columns() -> str:
        return (
            "inbox_id, instance_id, provider_message_id, chat_id, message_text, "
            "event_timestamp, status, attempts, created_at, updated_at, run_id, "
            "reply_message_id, error_code"
        )

    @staticmethod
    def _payload_hash(chat_id: str, text: str) -> str:
        # Timestamp is provider metadata; it can be omitted on a redelivery while
        # the original message ID, recipient, and body remain the same.
        material = f"{chat_id}\0{text}"
        return hashlib.sha256(material.encode()).hexdigest()

    @staticmethod
    def _from_row(row: tuple[object, ...] | None) -> InboxMessage | None:
        if row is None:
            return None
        return InboxMessage(
            inbox_id=str(row[0]),
            instance_id=str(row[1]),
            provider_message_id=str(row[2]),
            chat_id=str(row[3]),
            text=str(row[4]),
            timestamp=int(row[5]) if row[5] is not None else None,
            status=InboxStatus(str(row[6])),
            attempts=int(row[7]),
            created_at=float(row[8]),
            updated_at=float(row[9]),
            run_id=str(row[10]) if row[10] is not None else None,
            reply_message_id=str(row[11]) if row[11] is not None else None,
            error_code=str(row[12]) if row[12] is not None else None,
        )

    def enqueue(
        self,
        *,
        instance_id: str,
        provider_message_id: str,
        chat_id: str,
        text: str,
        timestamp: int | None,
    ) -> tuple[InboxMessage, bool]:
        instance = _required_text("instance_id", instance_id, max_length=128)
        if not _INSTANCE_ID.fullmatch(instance):
            raise ValueError("instance_id must contain only letters, digits, '_' or '-'")
        event_id = _required_text("provider_message_id", provider_message_id, max_length=255)
        if _CONTROL_CHARS.search(event_id):
            raise ValueError("provider_message_id contains control characters")
        chat = _required_text("chat_id", chat_id, max_length=255)
        body = _message_body(text)
        if timestamp is not None and not 0 <= timestamp <= 4_102_444_800:
            raise ValueError("timestamp is outside the supported range")
        digest = self._payload_hash(chat, body)
        inbox_id = f"wai_{uuid.uuid4().hex}"
        now = time.time()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    f"SELECT {self._columns()}, payload_hash FROM waapi_inbox "
                    "WHERE instance_id=? AND provider_message_id=?",
                    (instance, event_id),
                ).fetchone()
                if existing is not None:
                    if str(existing[13]) != digest:
                        self._conn.rollback()
                        raise InboxIdempotencyConflict(
                            "provider message ID was reused with different content"
                        )
                    self._conn.commit()
                    return self._from_row(existing[:13]), False  # type: ignore[return-value]
                self._conn.execute(
                    "INSERT INTO waapi_inbox (inbox_id, instance_id, provider_message_id, "
                    "payload_hash, chat_id, message_text, event_timestamp, status, attempts, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)",
                    (inbox_id, instance, event_id, digest, chat, body, timestamp, now, now),
                )
                row = self._conn.execute(
                    f"SELECT {self._columns()} FROM waapi_inbox WHERE inbox_id=?", (inbox_id,)
                ).fetchone()
                self._conn.commit()
            except Exception:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise
        record = self._from_row(row)
        assert record is not None
        return record, True

    def claim_next(self) -> InboxMessage | None:
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    f"SELECT {self._columns()} FROM waapi_inbox WHERE status='queued' "
                    "ORDER BY created_at, inbox_id LIMIT 1"
                ).fetchone()
                if row is None:
                    self._conn.commit()
                    return None
                inbox_id = str(row[0])
                changed = self._conn.execute(
                    "UPDATE waapi_inbox SET status='processing', attempts=attempts+1, "
                    "updated_at=?, error_code=NULL WHERE inbox_id=? AND status='queued'",
                    (now, inbox_id),
                ).rowcount
                if changed != 1:
                    self._conn.rollback()
                    return None
                claimed = self._conn.execute(
                    f"SELECT {self._columns()} FROM waapi_inbox WHERE inbox_id=?", (inbox_id,)
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self._from_row(claimed)

    def mark_run_started(self, inbox_id: str, run_id: str) -> None:
        value = _required_text("run_id", run_id, max_length=128)
        self._transition(
            inbox_id,
            "run_id=?, updated_at=?",
            (value, time.time()),
        )

    def mark_reply_queued(self, inbox_id: str, reply_message_id: str) -> None:
        value = _required_text("reply_message_id", reply_message_id, max_length=128)
        self._transition(
            inbox_id,
            "status='reply_queued', reply_message_id=?, updated_at=?, error_code=NULL",
            (value, time.time()),
        )

    def mark_failed(self, inbox_id: str, *, error_code: str = "agent_run_failed") -> None:
        safe_code = (
            error_code if re.fullmatch(r"[a-z0-9_]{1,80}", error_code) else "agent_run_failed"
        )
        self._transition(
            inbox_id,
            "status='failed', error_code=?, updated_at=?",
            (safe_code, time.time()),
        )

    def _transition(self, inbox_id: str, update: str, values: tuple[object, ...]) -> None:
        with self._lock, self._conn:
            changed = self._conn.execute(
                f"UPDATE waapi_inbox SET {update} WHERE inbox_id=? AND status='processing'",
                (*values, inbox_id),
            ).rowcount
        if changed != 1:
            raise RuntimeError("inbox message is not in the processing state")

    def get(self, inbox_id: str) -> InboxMessage | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._columns()} FROM waapi_inbox WHERE inbox_id=?", (inbox_id,)
            ).fetchone()
        return self._from_row(row)

    def mark_stale_processing_unknown(self, *, older_than_seconds: float) -> int:
        if older_than_seconds < 0:
            raise ValueError("older_than_seconds must be non-negative")
        cutoff = time.time() - older_than_seconds
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE waapi_inbox SET status='unknown', updated_at=?, "
                "error_code='worker_lease_expired' "
                "WHERE status='processing' AND updated_at<=?",
                (time.time(), cutoff),
            )
        return cursor.rowcount

    def touch_processing(self, inbox_id: str) -> None:
        with self._lock, self._conn:
            changed = self._conn.execute(
                "UPDATE waapi_inbox SET updated_at=? WHERE inbox_id=? AND status='processing'",
                (time.time(), inbox_id),
            ).rowcount
        if changed != 1:
            raise RuntimeError("inbox message is not in the processing state")

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM waapi_inbox WHERE status='queued'"
            ).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = [
    "InboxIdempotencyConflict",
    "InboxMessage",
    "InboxStatus",
    "SQLiteWaapiInbox",
]
