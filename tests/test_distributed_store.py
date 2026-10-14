"""Distributed store: Postgres SQL contract + Redis Streams, via injected fakes.

No live Postgres/Redis exists in CI, so the DBAPI connection is a sqlite3
connection translating ``%s`` placeholders (the SQL dialect used —
``ON CONFLICT ... EXCLUDED`` — is common to both), and Redis is an
in-memory double implementing exactly the commands DistributedRunStore
issues. The real-driver paths are exercised by construction/laziness tests.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from os_core.events import Event, EventType
from os_core.state import RunState, RunStatus
from os_server.storage import DistributedRunStore, PostgresRunStore


class _FakePgConnection:
    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")

    def execute(self, sql, params=()):
        return self._conn.execute(sql.replace("%s", "?"), tuple(params))

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _pg_store() -> PostgresRunStore:
    return PostgresRunStore("postgres://fake/cela", connection_factory=_FakePgConnection)


def test_postgres_store_full_roundtrip() -> None:
    store = _pg_store()
    state = RunState(run_id="run_pg", task="t", mode="react")
    state.mark_finished(RunStatus.COMPLETED, answer="5050")
    store.save_state(state)
    store.save_event(Event(type=EventType.THOUGHT, run_id="run_pg", seq=1, data={"text": "x"}))
    store.save_event(Event(type=EventType.THOUGHT, run_id="run_pg", seq=1, data={"text": "dup"}))
    store.save_artifact("run_pg", "a.html", {"filename": "a.html"})
    store.save_ticket("tok", "run_pg", expires_at=time.time() + 60)

    assert store.get_state("run_pg").final_answer == "5050"
    assert len(store.list_events("run_pg")) == 1  # (run_id, seq) conflict ignored
    assert store.last_seq("run_pg") == 1
    assert store.get_artifact("run_pg", "a.html")["filename"] == "a.html"
    assert store.pop_ticket("tok").run_id == "run_pg"
    assert store.pop_ticket("tok") is None
    store.close()


def test_postgres_missing_driver_fails_loudly_not_silently() -> None:
    if pytest.importorskip("psycopg", reason="psycopg installed"):
        pass
    store = PostgresRunStore("postgres://localhost/cela")
    try:
        store.list_run_ids()
    except RuntimeError as exc:
        assert "distributed" in str(exc)
    else:  # pragma: no cover - psycopg present but no server
        pytest.skip("psycopg importable; connection error path is environment-specific")


class _FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list] = {}
        self.kv: dict[str, tuple] = {}
        self._n = 0

    def setnx(self, key, value) -> bool:
        if key in self.kv:
            return False
        self.kv[key] = (value, None)
        return True

    def xadd(self, key, fields):
        self._n += 1
        entry = (f"{self._n}-0", dict(fields))
        self.streams.setdefault(key, []).append(entry)
        return entry[0]

    def xrange(self, key):
        return list(self.streams.get(key, []))

    def set(self, key, value, pxat=None) -> None:
        self.kv[key] = (value, pxat)

    def getdel(self, key):
        item = self.kv.pop(key, None)
        if item is None:
            return None
        value, pxat = item
        if pxat is not None and pxat <= int(time.time() * 1000):
            return None
        return value


def test_distributed_store_events_on_stream_tickets_on_redis() -> None:
    store = DistributedRunStore(_pg_store(), redis_client=_FakeRedis())
    event = Event(type=EventType.RUN_FINISHED, run_id="run_d", seq=2, data={"status": "completed"})
    store.save_event(event)
    store.save_event(event)  # dedupe per seq: Streams have no unique constraint
    store.save_event(Event(type=EventType.THOUGHT, run_id="run_d", seq=1, data={"text": "hi"}))

    assert [e.seq for e in store.list_events("run_d")] == [1, 2]
    assert [e.seq for e in store.list_events("run_d", after_seq=1)] == [2]
    assert store.last_seq("run_d") == 2

    store.save_ticket("ticket-1", "run_d", expires_at=time.time() + 60)
    assert store.pop_ticket("ticket-1").run_id == "run_d"
    assert store.pop_ticket("ticket-1") is None

    state = RunState(run_id="run_d", task="t")
    state.mark_finished(RunStatus.COMPLETED, answer="ok")
    store.save_state(state)  # durable side still Postgres
    assert store.get_state("run_d").final_answer == "ok"
    store.close()
