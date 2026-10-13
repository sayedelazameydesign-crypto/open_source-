"""HTTP + WebSocket transport layer."""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from os_server.app import create_app  # noqa: E402
from os_server.demo import build_demo_platform_for  # noqa: E402


@pytest.fixture
def platform_factory():
    # A fresh, mode-correct platform per run — no cross-request config bleed.
    return lambda mode, max_steps: build_demo_platform_for(mode)


@pytest.fixture
def client(platform_factory):
    with TestClient(
        create_app(platform_factory, api_token="test-token"),
        headers={"Authorization": "Bearer test-token"},
    ) as test_client:
        yield test_client


@pytest.fixture
def anonymous_client(platform_factory):
    # The app still has a configured token, but this client sends no credentials.
    with TestClient(create_app(platform_factory, api_token="test-token")) as test_client:
        yield test_client


def test_health_is_public_and_minimal(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


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
    with client.websocket_connect(
        f"/ws/{run_id}", headers={"Authorization": "Bearer test-token"}
    ) as ws:
        while True:
            message = ws.receive_text()
            seen.append(message)
            if "run_finished" in message:
                break
    assert any("tool_result" in m for m in seen)
    assert any("run_finished" in m for m in seen)


def test_websocket_rejects_unknown_run(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/run_nope", headers={"Authorization": "Bearer test-token"}),
    ):
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


def test_protected_routes_require_bearer_auth(anonymous_client: TestClient) -> None:
    requests = (
        ("POST", "/runs", {"json": {"task": "hi", "mode": "react"}}),
        ("GET", "/runs/run_unknown", {}),
        ("GET", "/runs/run_unknown/events", {}),
        (
            "POST",
            "/runs/run_unknown/approve",
            {"json": {"tool_call_id": "x", "decision": "approved"}},
        ),
        ("GET", "/artifacts/run_unknown/nope.txt", {}),
    )
    for method, path, kwargs in requests:
        assert anonymous_client.request(method, path, **kwargs).status_code == 401
        assert (
            anonymous_client.request(
                method, path, headers={"Authorization": "Bearer incorrect"}, **kwargs
            ).status_code
            == 401
        )


def test_api_fails_closed_when_token_is_not_configured(platform_factory) -> None:
    with TestClient(create_app(platform_factory, api_token="")) as unconfigured_client:
        assert unconfigured_client.get("/health").status_code == 200
        assert unconfigured_client.get("/runs/run_unknown").status_code == 503


def test_websocket_rejects_missing_bearer_token(anonymous_client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        anonymous_client.websocket_connect("/ws/run_unknown"),
    ):
        pass
    assert exc_info.value.code == 4401
