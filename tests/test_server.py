"""HTTP + WebSocket transport layer."""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from os_server.app import create_app  # noqa: E402
from os_server.demo import build_demo_platform_for  # noqa: E402


@pytest.fixture
def client():
    # A fresh, mode-correct platform per run — no cross-request config bleed.
    factory = lambda mode, max_steps: build_demo_platform_for(mode)  # noqa: E731
    with TestClient(create_app(factory)) as test_client:
        yield test_client


def test_health_reports_tools(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "python_execute" in body["tools"]


def test_start_run_returns_an_id(client: TestClient) -> None:
    resp = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"})
    assert resp.status_code == 202
    assert resp.json()["run_id"].startswith("run_")


def test_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/runs/run_nope").status_code == 404


def test_run_snapshot_after_completion(client: TestClient) -> None:
    run_id = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"}).json()["run_id"]
    for _ in range(100):
        body = client.get(f"/runs/{run_id}").json()
        if body.get("status") in {"completed", "failed"}:
            break
        import time

        time.sleep(0.05)
    assert body["status"] == "completed", body
    assert "5050" in body["final_answer"]
    assert body["mode"] == "react"


def test_event_stream_emits_sse(client: TestClient) -> None:
    run_id = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"}).json()["run_id"]
    with client.stream("GET", f"/runs/{run_id}/events") as resp:
        assert resp.status_code == 200
        chunk = next(resp.iter_text())
    assert "run_started" in chunk


def test_websocket_streams_events(client: TestClient) -> None:
    run_id = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"}).json()["run_id"]
    seen: list[str] = []
    with client.websocket_connect(f"/ws/{run_id}") as ws:
        while True:
            message = ws.receive_text()
            seen.append(message)
            if "run_finished" in message:
                break
    assert any("tool_result" in m for m in seen)
    assert any("run_finished" in m for m in seen)


def test_websocket_rejects_unknown_run(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/run_nope"):
        pass


def test_approve_endpoint_requires_a_queue_approver(client: TestClient) -> None:
    """The demo platform answers by timeout, not by an interactive queue."""
    run_id = client.post("/runs", json={"task": "hi", "mode": "react"}).json()["run_id"]
    resp = client.post(
        f"/runs/{run_id}/approve", json={"tool_call_id": "call_x", "decision": "approved"}
    )
    assert resp.status_code in {200, 409}


def test_approve_rejects_bad_decision(client: TestClient) -> None:
    run_id = client.post("/runs", json={"task": "hi", "mode": "react"}).json()["run_id"]
    resp = client.post(
        f"/runs/{run_id}/approve", json={"tool_call_id": "call_x", "decision": "maybe"}
    )
    assert resp.status_code == 422


def test_artifact_endpoint_404_for_unknown(client: TestClient) -> None:
    assert client.get("/artifacts/run_x/nope.png").status_code == 404
