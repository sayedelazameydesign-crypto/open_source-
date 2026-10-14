"""Durable skill storage: in-memory (default) and SQLite (durable).

The Postgres composition reuses the same single-table contract through
``PostgresSkillStore`` with an injectable DBAPI connection factory, mirroring
``os_server.storage``. Selection is explicit via :func:`skill_store_from_url`;
unknown backends fail loudly instead of silently losing learned skills.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from os_skills.types import Skill

_DDL = (
    "CREATE TABLE IF NOT EXISTS skills("
    "skill_id TEXT PRIMARY KEY, signature TEXT, skill_json TEXT NOT NULL, updated_at REAL)"
)


@runtime_checkable
class SkillStore(Protocol):
    def save_skill(self, skill: Skill) -> None: ...
    def get_skill(self, skill_id: str) -> Skill | None: ...
    def list_skills(self) -> list[Skill]: ...
    def delete_skill(self, skill_id: str) -> bool: ...
    def find_by_signature(self, signature: str) -> Skill | None: ...
    def close(self) -> None: ...


class InMemorySkillStore:
    def __init__(self) -> None:
        self.skills: dict[str, Skill] = {}

    def save_skill(self, skill: Skill) -> None:
        skill.updated_at = time.time()
        self.skills[skill.skill_id] = skill

    def get_skill(self, skill_id: str) -> Skill | None:
        return self.skills.get(skill_id)

    def list_skills(self) -> list[Skill]:
        return list(self.skills.values())

    def delete_skill(self, skill_id: str) -> bool:
        return self.skills.pop(skill_id, None) is not None

    def find_by_signature(self, signature: str) -> Skill | None:
        return next((s for s in self.skills.values() if s.signature == signature), None)

    def close(self) -> None:
        pass


class SQLiteSkillStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.execute(_DDL)

    def save_skill(self, skill: Skill) -> None:
        skill.updated_at = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO skills VALUES(?,?,?,?) ON CONFLICT(skill_id) DO UPDATE SET "
                "signature=excluded.signature, skill_json=excluded.skill_json, "
                "updated_at=excluded.updated_at",
                (skill.skill_id, skill.signature, skill.model_dump_json(), skill.updated_at),
            )

    def get_skill(self, skill_id: str) -> Skill | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT skill_json FROM skills WHERE skill_id=?", (skill_id,)
            ).fetchone()
        return Skill.model_validate_json(row[0]) if row else None

    def list_skills(self) -> list[Skill]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT skill_json FROM skills ORDER BY updated_at"
            ).fetchall()
        return [Skill.model_validate_json(r[0]) for r in rows]

    def delete_skill(self, skill_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM skills WHERE skill_id=?", (skill_id,))
        return cur.rowcount > 0

    def find_by_signature(self, signature: str) -> Skill | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT skill_json FROM skills WHERE signature=?", (signature,)
            ).fetchone()
        return Skill.model_validate_json(row[0]) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class PostgresSkillStore:
    """Postgres skills table (optional ``[distributed]`` extra, lazy driver)."""

    def __init__(self, dsn: str | None = None, *, connection_factory: Any = None) -> None:
        self.dsn = dsn
        self._factory = connection_factory
        self._conn: Any = None
        self._lock = threading.RLock()

    def _connection(self) -> Any:
        if self._conn is None:
            if self._factory is not None:
                self._conn = self._factory()
            else:
                try:
                    import psycopg
                except ImportError:
                    raise RuntimeError(
                        "psycopg is not installed; run: pip install 'cela-use-browser[distributed]'"
                    ) from None
                self._conn = psycopg.connect(self.dsn) if self.dsn else psycopg.connect()
            self._conn.execute(_DDL.replace("REAL)", "DOUBLE PRECISION)"))
            self._conn.commit()
        return self._conn

    def save_skill(self, skill: Skill) -> None:
        skill.updated_at = time.time()
        with self._lock:
            conn = self._connection()
            conn.execute(
                "INSERT INTO skills VALUES(%s,%s,%s,%s) ON CONFLICT(skill_id) DO UPDATE SET "
                "signature=EXCLUDED.signature, skill_json=EXCLUDED.skill_json, "
                "updated_at=EXCLUDED.updated_at",
                (skill.skill_id, skill.signature, skill.model_dump_json(), skill.updated_at),
            )
            conn.commit()

    def get_skill(self, skill_id: str) -> Skill | None:
        with self._lock:
            row = (
                self._connection()
                .execute("SELECT skill_json FROM skills WHERE skill_id=%s", (skill_id,))
                .fetchone()
            )
        return Skill.model_validate_json(row[0]) if row else None

    def list_skills(self) -> list[Skill]:
        with self._lock:
            rows = (
                self._connection()
                .execute("SELECT skill_json FROM skills ORDER BY updated_at")
                .fetchall()
            )
        return [Skill.model_validate_json(r[0]) for r in rows]

    def delete_skill(self, skill_id: str) -> bool:
        with self._lock:
            conn = self._connection()
            cur = conn.execute("DELETE FROM skills WHERE skill_id=%s", (skill_id,))
            conn.commit()
        return bool(getattr(cur, "rowcount", 0))

    def find_by_signature(self, signature: str) -> Skill | None:
        with self._lock:
            row = (
                self._connection()
                .execute("SELECT skill_json FROM skills WHERE signature=%s", (signature,))
                .fetchone()
            )
        return Skill.model_validate_json(row[0]) if row else None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def skill_store_from_url(url: str | None) -> SkillStore:
    if not url or url == "memory://":
        return InMemorySkillStore()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresSkillStore(url)
    if url.startswith("sqlite:///"):
        return SQLiteSkillStore(url[len("sqlite:///") :])
    if url.startswith("sqlite://"):
        raise ValueError("use sqlite:///<path> (three slashes) or a bare filesystem path")
    if "://" in url:
        raise RuntimeError(f"unsupported skill store backend in {url!r}")
    return SQLiteSkillStore(url)


__all__ = [
    "InMemorySkillStore",
    "PostgresSkillStore",
    "SQLiteSkillStore",
    "SkillStore",
    "skill_store_from_url",
]
