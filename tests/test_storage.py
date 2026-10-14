"""Durable run storage: repository seam, SQLite durability, restart replay."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from os_core.events import Event, EventType
from os_core.state import RunState, RunStatus
from os_server.demo import build_demo_platform_for, demo_egress_policy
from os_server.runtime import ApiRuntime
from os_server.storage import InMemoryRunStore, SQLiteRunStore, run_store_from_url


@pytest.fixture
def factory():
    return lambda mode, max_steps: build_demo_platform_for(mode)


def _finished_state(run_id: str) -> RunState:
    state = RunState(run_id=run_id, task="Sum 0..100", mode="react")
    state.mark_finished(RunStatus.COMPLETED, answer="5050")
    return state


def test_sqlite_roundtrip_state_events_artifacts(tmp_path) -> None:
    path = tmp_path / "runs.db"
    store = SQLiteRunStore(path)
    store.save_state(_finished_state("run_a"))
    store.save_event(
        Event(type=EventType.RUN_FINISHED, run_id="run_a", seq=3, data={"status": "completed"})
    )
    store.save_event(Event(type=EventType.THOUGHT, run_id="run_a", seq=1, data={"text": "hi"}))
    store.save_artifact("run_a", "chart.html", {"filename": "chart.html", "kind": "html"})
    store.close()

    reopened = SQLiteRunStore(path)
    assert reopened.get_state("run_a").final_answer == "5050"
    assert [e.seq for e in reopened.list_events("run_a")] == [1, 3]
    assert [e.seq for e in reopened.list_events("run_a", after_seq=1)] == [3]
    assert reopened.last_seq("run_a") == 3
    assert reopened.get_artifact("run_a", "chart.html")["kind"] == "html"
    assert reopened.has_run("run_a")
    reopened.close()


def test_sqlite_events_are_keyed_by_seq_and_never_duplicated(tmp_path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.db")
    event = Event(type=EventType.LOG, run_id="run_a", seq=1, data={"text": "once"})
    store.save_event(event)
    store.save_event(event)
    assert len(store.list_events("run_a")) == 1
    store.close()


def test_tickets_are_single_use_expiring_and_hashed_at_rest(tmp_path) -> None:
    path = tmp_path / "runs.db"
    store = SQLiteRunStore(path)
    store.save_ticket("secret-ticket", "run_a", expires_at=9_999_999_999.0)
    store.save_ticket("expired-ticket", "run_a", expires_at=1.0)
    store.close()

    raw = sqlite3.connect(path).execute("SELECT ticket_hash FROM ws_tickets").fetchall()
    assert all("secret-ticket" not in row[0] and "expired" not in row[0] for row in raw)

    reopened = SQLiteRunStore(path)
    reopened.prune_tickets(now=2.0)
    assert reopened.pop_ticket("expired-ticket") is None
    ticket = reopened.pop_ticket("secret-ticket")
    assert ticket is not None and ticket.run_id == "run_a"
    assert reopened.pop_ticket("secret-ticket") is None  # single use
    reopened.close()


def test_run_store_from_url_rejects_unimplemented_backends_loudly() -> None:
    assert isinstance(run_store_from_url(None), InMemoryRunStore)
    assert isinstance(run_store_from_url("memory://"), InMemoryRunStore)
    with pytest.raises(RuntimeError, match="not implemented"):
        run_store_from_url("postgres://localhost/cela")
    with pytest.raises(RuntimeError, match="not implemented"):
        run_store_from_url("redis://localhost:6379/0")


async def test_runtime_recovers_finished_run_after_restart(tmp_path, factory) -> None:
    """A new ApiRuntime over the same SQLite file serves the old run fully."""
    url = f"sqlite:///{tmp_path / 'runs.db'}"
    first = ApiRuntime(factory, egress_policy=demo_egress_policy(), store_url=url)
    handle, _mode = first.start_run("Sum the integers from 0 to 100", mode="react")
    state = await handle.task
    assert state.status is RunStatus.COMPLETED
    run_id = handle.run_id
    last_seq = handle.bus.last_seq
    assert last_seq > 0
    first.close()

    # Simulated restart: no live handle, no in-process dicts — store only.
    second = ApiRuntime(factory, egress_policy=demo_egress_policy(), store_url=url)
    assert second.get_handle(run_id) is None
    assert second.has_run(run_id)

    snap = second.get_run_snapshot(run_id)
    assert snap is not None and snap["status"] == "completed"
    assert any(r["run_id"] == run_id for r in second.list_runs())
    assert second.list_artifacts(run_id) is not None
    assert second.stream_exhausted(run_id, after_seq=last_seq) is True
    assert second.stream_exhausted(run_id, after_seq=0) is False

    replayed = [line async for line in second.sse_events(run_id, after_seq=0)]
    assert replayed, "durable replay must return the persisted event log"
    resumed = [line async for line in second.sse_events(run_id, after_seq=last_seq)]
    assert resumed == []
    second.close()


async def test_ws_ticket_survives_restart_and_stays_single_use(tmp_path, factory) -> None:
    url = f"sqlite:///{tmp_path / 'runs.db'}"
    first = ApiRuntime(factory, egress_policy=demo_egress_policy(), store_url=url)
    handle, _ = first.start_run("Sum the integers from 0 to 100", mode="react")
    await handle.task
    issued = first.issue_ws_ticket(handle.run_id)
    assert issued is not None
    first.close()

    second = ApiRuntime(factory, egress_policy=demo_egress_policy(), store_url=url)
    assert second.consume_ws_ticket(issued["ticket"], handle.run_id) is True
    assert second.consume_ws_ticket(issued["ticket"], handle.run_id) is False
    second.close()


def test_default_runtime_stays_in_memory(factory) -> None:
    runtime = ApiRuntime(factory, egress_policy=demo_egress_policy())
    assert isinstance(runtime.store, InMemoryRunStore)
    asyncio.get_event_loop_policy()  # no-op: keeps the hermetic default explicit


def test_http_surface_recovers_finished_run_after_restart(tmp_path, factory) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    TestClient = fastapi_testclient.TestClient
    from os_server.app import create_app

    url = f"sqlite:///{tmp_path / 'runs.db'}"
    headers = {"Authorization": "Bearer test-token"}
    app1 = create_app(factory, api_token="test-token", store_url=url)
    with TestClient(app1) as client:
        run_id = client.post(
            "/runs", json={"task": "Sum 0..100", "mode": "react"}, headers=headers
        ).json()["run_id"]
        for _ in range(100):
            body = client.get(f"/runs/{run_id}", headers=headers).json()
            if body.get("status") == "completed":
                break
        assert body["status"] == "completed"

    # New app + new runtime over the same file == a restarted server process.
    app2 = create_app(factory, api_token="test-token", store_url=url)
    with TestClient(app2) as client:
        snap = client.get(f"/runs/{run_id}", headers=headers)
        assert snap.status_code == 200 and snap.json()["status"] == "completed"
        events = client.get(f"/runs/{run_id}/events", headers=headers)
        assert events.status_code == 200 and "run_finished" in events.text
        ticket = client.post(f"/runs/{run_id}/ws-ticket", headers=headers).json()["ticket"]
        with client.websocket_connect(f"/ws/{run_id}?ticket={ticket}") as ws:
            first = ws.receive_text()
            assert "run_started" in first
