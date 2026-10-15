"""Durable, idempotent outbox primitives for the optional WaAPI connector.

The queue intentionally does not retry uncertain deliveries. WaAPI's response
contract says ``data.status`` is authoritative, but the provider does not offer
an idempotency contract in the documentation currently available to this
adapter. Retrying after a timeout or process crash could therefore send a
customer the same message twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class OutboxError(RuntimeError):
    """Base class for outbox errors."""


class IdempotencyConflict(OutboxError):
    """An idempotency key was reused for a different message."""


class InvalidOutboxTransition(OutboxError):
    """A delivery result attempted an invalid state transition."""


class OutboxStatus(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class DeliveryOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    message_id: str
    instance_id: str
    idempotency_key: str = field(repr=False)
    chat_id: str = field(repr=False)
    text: str = field(repr=False)
    status: OutboxStatus
    attempts: int
    created_at: float
    updated_at: float
    provider_message_id: str | None = None
    error_code: str | None = None

    def public_status(self) -> dict[str, object]:
        """Safe queue metadata; never includes message text or credentials."""
        return {
            "message_id": self.message_id,
            "instance_id": self.instance_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provider_message_id": self.provider_message_id,
            "error_code": self.error_code,
        }


_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _required_text(name: str, value: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be empty")
    if len(cleaned) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    if _CONTROL_CHARS.search(cleaned):
        raise ValueError(f"{name} contains control characters")
    return cleaned


def _message_body(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("text must be text")
    if not value.strip():
        raise ValueError("text must not be empty")
    if len(value) > 10_000:
        raise ValueError("text exceeds 10000 characters")
    # Newlines and tabs are valid WhatsApp message content; reject only NUL and
    # other non-whitespace C0 controls that cannot be useful in a text message.
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value) or "\x7f" in value:
        raise ValueError("text contains unsupported control characters")
    return value


class SQLiteWaapiOutbox:
    """A single-file WAL outbox with idempotent enqueue and explicit outcomes.

    Message text and recipient IDs are persisted so queued work survives a
    restart. The WaAPI bearer token is never passed to or stored by this class.
    The default database belongs under ``.cela/``, which is gitignored.
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
                CREATE TABLE IF NOT EXISTS waapi_outbox (
                    message_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'sending', 'sent', 'rejected', 'unknown')
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    provider_message_id TEXT,
                    error_code TEXT,
                    UNIQUE(instance_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS waapi_outbox_status_created
                    ON waapi_outbox(status, created_at);
                """
            )

    @staticmethod
    def _payload_hash(chat_id: str, text: str) -> str:
        return hashlib.sha256(f"{chat_id}\0{text}".encode()).hexdigest()

    @staticmethod
    def _from_row(row: tuple[object, ...] | None) -> OutboxMessage | None:
        if row is None:
            return None
        return OutboxMessage(
            message_id=str(row[0]),
            instance_id=str(row[1]),
            idempotency_key=str(row[2]),
            chat_id=str(row[3]),
            text=str(row[4]),
            status=OutboxStatus(str(row[5])),
            attempts=int(row[6]),
            created_at=float(row[7]),
            updated_at=float(row[8]),
            provider_message_id=str(row[9]) if row[9] is not None else None,
            error_code=str(row[10]) if row[10] is not None else None,
        )

    @staticmethod
    def _select_columns() -> str:
        return (
            "message_id, instance_id, idempotency_key, chat_id, message_text, status, "
            "attempts, created_at, updated_at, provider_message_id, error_code"
        )

    def enqueue(
        self,
        *,
        instance_id: str,
        idempotency_key: str,
        chat_id: str,
        text: str,
    ) -> tuple[OutboxMessage, bool]:
        """Queue a message once; return ``(record, inserted)``.

        Reusing a key for the same instance and payload returns the original row.
        Reusing it for different content is rejected rather than silently
        changing a pending or already-sent notification.
        """
        instance = _required_text("instance_id", instance_id, max_length=128)
        if not _INSTANCE_ID.fullmatch(instance):
            raise ValueError("instance_id must contain only letters, digits, '_' or '-'")
        key = _required_text("idempotency_key", idempotency_key, max_length=200)
        recipient = _required_text("chat_id", chat_id, max_length=255)
        body = _message_body(text)
        digest = self._payload_hash(recipient, body)
        now = time.time()
        message_id = f"wam_{uuid.uuid4().hex}"

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    f"SELECT {self._select_columns()}, payload_hash FROM waapi_outbox "
                    "WHERE instance_id=? AND idempotency_key=?",
                    (instance, key),
                ).fetchone()
                if existing is not None:
                    if str(existing[11]) != digest:
                        raise IdempotencyConflict(
                            "idempotency key already belongs to a different message"
                        )
                    self._conn.commit()
                    return self._from_row(existing[:11]), False  # type: ignore[return-value]

                self._conn.execute(
                    "INSERT INTO waapi_outbox (message_id, instance_id, idempotency_key, "
                    "payload_hash, chat_id, message_text, status, attempts, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)",
                    (message_id, instance, key, digest, recipient, body, now, now),
                )
                row = self._conn.execute(
                    f"SELECT {self._select_columns()} FROM waapi_outbox WHERE message_id=?",
                    (message_id,),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        record = self._from_row(row)
        assert record is not None
        return record, True

    def claim_next(self) -> OutboxMessage | None:
        """Atomically claim the oldest queued item for a single delivery attempt."""
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    f"SELECT {self._select_columns()} FROM waapi_outbox "
                    "WHERE status='queued' ORDER BY created_at, message_id LIMIT 1"
                ).fetchone()
                if row is None:
                    self._conn.commit()
                    return None
                message_id = str(row[0])
                changed = self._conn.execute(
                    "UPDATE waapi_outbox SET status='sending', attempts=attempts+1, "
                    "updated_at=?, error_code=NULL WHERE message_id=? AND status='queued'",
                    (now, message_id),
                ).rowcount
                if changed != 1:
                    self._conn.rollback()
                    return None
                claimed = self._conn.execute(
                    f"SELECT {self._select_columns()} FROM waapi_outbox WHERE message_id=?",
                    (message_id,),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self._from_row(claimed)

    def mark_sent(self, message_id: str, *, provider_message_id: str | None = None) -> None:
        provider_id = provider_message_id.strip() if provider_message_id else None
        if provider_id is not None and (
            len(provider_id) > 255 or _CONTROL_CHARS.search(provider_id)
        ):
            raise ValueError("provider_message_id is invalid")
        self._transition(
            message_id,
            OutboxStatus.SENT,
            error_code=None,
            provider_message_id=provider_id,
        )

    def mark_rejected(self, message_id: str, *, error_code: str = "provider_rejected") -> None:
        self._transition(
            message_id,
            OutboxStatus.REJECTED,
            error_code=self._safe_error_code(error_code),
        )

    def mark_unknown(
        self, message_id: str, *, error_code: str = "delivery_outcome_unknown"
    ) -> None:
        self._transition(
            message_id,
            OutboxStatus.UNKNOWN,
            error_code=self._safe_error_code(error_code),
        )

    def _transition(
        self,
        message_id: str,
        status: OutboxStatus,
        *,
        error_code: str | None,
        provider_message_id: str | None = None,
    ) -> None:
        if status not in {OutboxStatus.SENT, OutboxStatus.REJECTED, OutboxStatus.UNKNOWN}:
            raise ValueError("terminal status required")
        with self._lock, self._conn:
            changed = self._conn.execute(
                "UPDATE waapi_outbox SET status=?, updated_at=?, error_code=?, "
                "provider_message_id=COALESCE(?, provider_message_id) "
                "WHERE message_id=? AND status='sending'",
                (status.value, time.time(), error_code, provider_message_id, message_id),
            ).rowcount
        if changed != 1:
            raise InvalidOutboxTransition("message is not in the sending state")

    @staticmethod
    def _safe_error_code(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", value):
            return "delivery_error"
        return value

    def get(self, message_id: str) -> OutboxMessage | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._select_columns()} FROM waapi_outbox WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return self._from_row(row)

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM waapi_outbox WHERE status='queued'"
            ).fetchone()
        return int(row[0]) if row else 0

    def mark_stale_sending_unknown(self, *, older_than_seconds: float) -> int:
        """Close crashed-worker leases without retrying possibly sent messages."""
        if older_than_seconds < 0:
            raise ValueError("older_than_seconds must be non-negative")
        cutoff = time.time() - older_than_seconds
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE waapi_outbox SET status='unknown', updated_at=?, "
                "error_code='worker_lease_expired' "
                "WHERE status='sending' AND updated_at<=?",
                (time.time(), cutoff),
            )
        return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


@dataclass(frozen=True, slots=True)
class ActionResult:
    outcome: DeliveryOutcome
    error_code: str | None = None


class MessageSender(Protocol):
    async def send_message(self, message: OutboxMessage) -> ActionResult: ...


class WaapiOutboxWorker:
    """Drain one queue item and persist the provider's authoritative outcome."""

    def __init__(
        self,
        outbox: SQLiteWaapiOutbox,
        sender: MessageSender,
        *,
        lease_timeout_seconds: float = 300.0,
    ) -> None:
        if lease_timeout_seconds <= 0:
            raise ValueError("lease_timeout_seconds must be positive")
        self.outbox = outbox
        self.sender = sender
        self.lease_timeout_seconds = lease_timeout_seconds

    async def deliver_one(self) -> OutboxMessage | None:
        self.outbox.mark_stale_sending_unknown(older_than_seconds=self.lease_timeout_seconds)
        message = self.outbox.claim_next()
        if message is None:
            return None
        try:
            result = await self.sender.send_message(message)
        except asyncio.CancelledError:
            # Cancellation may happen after the provider accepted the request.
            # Leave the row in `sending`; lease recovery will mark it unknown.
            raise
        except Exception:
            # Do not persist exception text: it can contain request data, URLs,
            # or credentials. Unknown means manual reconciliation, not retry.
            self.outbox.mark_unknown(message.message_id)
            return self.outbox.get(message.message_id)

        if result.outcome is DeliveryOutcome.ACCEPTED:
            self.outbox.mark_sent(message.message_id)
        elif result.outcome is DeliveryOutcome.REJECTED:
            self.outbox.mark_rejected(
                message.message_id, error_code=result.error_code or "provider_rejected"
            )
        else:
            self.outbox.mark_unknown(
                message.message_id, error_code=result.error_code or "delivery_outcome_unknown"
            )
        return self.outbox.get(message.message_id)


__all__ = [
    "ActionResult",
    "DeliveryOutcome",
    "IdempotencyConflict",
    "InvalidOutboxTransition",
    "MessageSender",
    "OutboxError",
    "OutboxMessage",
    "OutboxStatus",
    "SQLiteWaapiOutbox",
    "WaapiOutboxWorker",
]
