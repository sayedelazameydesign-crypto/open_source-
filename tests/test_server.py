"""HTTP + WebSocket transport layer."""

from __future__ import annotations

import json
import time

import pytest

from conftest import await_until, wait_until

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from os_server.app import create_app, public_docs_enabled  # noqa: E402
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


def test_start_run_rejects_max_steps_outside_the_supported_range(client: TestClient) -> None:
    for max_steps in (0, 101):
        response = client.post(
            "/runs", json={"task": "bounded task", "mode": "flow", "max_steps": max_steps}
        )
        assert response.status_code == 422


def test_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/runs/run_nope").status_code == 404


def test_run_snapshot_after_completion(client: TestClient) -> None:
    run_id = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"}).json()["run_id"]
    body = _wait_for_terminal_snapshot(client, run_id)
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


def _terminal(snapshot: dict) -> dict | None:
    return snapshot if snapshot.get("status") in {"completed", "failed"} else None


def _wait_for_terminal_snapshot(client: TestClient, run_id: str) -> dict:
    return wait_until(
        lambda: _terminal(client.get(f"/runs/{run_id}").json()),
        what=f"run {run_id} reaching a terminal state",
    )


def _buffered_event_ids(client: TestClient, run_id: str) -> list[str]:
    return [event.id for event in client.app.state.runtime.runs[run_id].bus.buffer]


def test_websocket_hands_a_late_client_the_full_history_exactly_once(client: TestClient) -> None:
    """A client that connects after the run finished still gets every event.

    The replay buffer is the only copy of the transient events (thoughts, tool
    calls), so the WebSocket stream must equal it — one delivery each, in order.
    """
    run_id = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"}).json()["run_id"]
    _wait_for_terminal_snapshot(client, run_id)
    expected = _buffered_event_ids(client, run_id)

    seen: list[str] = []
    with client.websocket_connect(
        f"/ws/{run_id}", headers={"Authorization": "Bearer test-token"}
    ) as ws:
        while True:
            message = json.loads(ws.receive_text())
            seen.append(message["id"])
            if message["type"] == "run_finished":
                break

    assert seen == expected
    assert len(seen) == len(set(seen))


def test_sse_hands_a_late_client_the_full_history_exactly_once(client: TestClient) -> None:
    """SSE shares the WebSocket's replay-then-follow semantics, gap included."""
    run_id = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"}).json()["run_id"]
    _wait_for_terminal_snapshot(client, run_id)
    expected = _buffered_event_ids(client, run_id)

    seen: list[str] = []
    with client.stream("GET", f"/runs/{run_id}/events") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            message = json.loads(line.removeprefix("data: "))
            seen.append(message["id"])
            if message["type"] == "run_finished":
                break

    assert seen == expected
    assert len(seen) == len(set(seen))


def _sse_ids_and_seqs(client: TestClient, run_id: str, query: str = "") -> tuple[list, list]:
    ids: list[str] = []
    seqs: list[int] = []
    with client.stream("GET", f"/runs/{run_id}/events{query}") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            message = json.loads(line.removeprefix("data: "))
            ids.append(message["id"])
            seqs.append(message["seq"])
            if message["type"] == "run_finished":
                break
    return ids, seqs


def test_sse_resumes_from_a_sequence_cursor(client: TestClient) -> None:
    """A reconnecting client sends the last seq it saw and gets only what it missed."""
    run_id = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"}).json()["run_id"]
    _wait_for_terminal_snapshot(client, run_id)
    buffered = client.app.state.runtime.runs[run_id].bus.buffer
    cursor = buffered[1].seq

    ids, seqs = _sse_ids_and_seqs(client, run_id, f"?after_seq={cursor}")

    assert ids == [event.id for event in buffered[2:]]
    assert seqs == [event.seq for event in buffered[2:]]


def test_websocket_resumes_from_a_sequence_cursor(client: TestClient) -> None:
    run_id = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"}).json()["run_id"]
    _wait_for_terminal_snapshot(client, run_id)
    buffered = client.app.state.runtime.runs[run_id].bus.buffer
    cursor = buffered[1].seq

    seen: list[str] = []
    with client.websocket_connect(
        f"/ws/{run_id}?after_seq={cursor}", headers={"Authorization": "Bearer test-token"}
    ) as ws:
        while True:
            message = json.loads(ws.receive_text())
            seen.append(message["id"])
            if message["type"] == "run_finished":
                break

    assert seen == [event.id for event in buffered[2:]]


def test_a_cursor_at_the_end_returns_nothing_instead_of_hanging(client: TestClient) -> None:
    """The run is over and the client has everything: close, do not subscribe forever."""
    from starlette.websockets import WebSocketDisconnect

    run_id = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"}).json()["run_id"]
    _wait_for_terminal_snapshot(client, run_id)
    runtime = client.app.state.runtime
    last = runtime.runs[run_id].bus.last_seq

    assert runtime.stream_exhausted(run_id, last) is True
    assert runtime.stream_exhausted(run_id, 0) is False  # a fresh client still gets history

    with client.stream("GET", f"/runs/{run_id}/events?after_seq={last}") as resp:
        assert resp.status_code == 200
        assert list(resp.iter_lines()) == []

    with (
        client.websocket_connect(
            f"/ws/{run_id}?after_seq={last}", headers={"Authorization": "Bearer test-token"}
        ) as ws,
        pytest.raises(WebSocketDisconnect),
    ):
        ws.receive_text()


def test_sse_rejects_a_malformed_or_negative_cursor(client: TestClient) -> None:
    run_id = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"}).json()["run_id"]

    assert client.get(f"/runs/{run_id}/events?after_seq=abc").status_code == 422
    assert client.get(f"/runs/{run_id}/events?after_seq=-1").status_code == 422


def test_websocket_closes_with_4422_on_a_malformed_cursor(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    run_id = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"}).json()["run_id"]

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(
            f"/ws/{run_id}?after_seq=abc", headers={"Authorization": "Bearer test-token"}
        ),
    ):
        pass

    assert excinfo.value.code == 4422


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
        ("POST", "/runs/run_unknown/ws-ticket", {}),
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


def test_ws_ticket_minting_requires_existing_run(client: TestClient) -> None:
    assert client.post("/runs/run_unknown/ws-ticket").status_code == 404


def test_ws_ticket_minting_returns_ticket_and_ttl(client: TestClient) -> None:
    run_id = client.post("/runs", json={"task": "Sum 0..100", "mode": "react"}).json()["run_id"]
    resp = client.post(f"/runs/{run_id}/ws-ticket")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == run_id
    assert isinstance(body["ticket"], str) and len(body["ticket"]) >= 16
    assert body["expires_in"] == 60.0


def test_websocket_accepts_valid_ticket_without_auth_header(
    anonymous_client: TestClient,
) -> None:
    auth = {"Authorization": "Bearer test-token"}
    run_id = anonymous_client.post(
        "/runs", json={"task": "Sum 0..100", "mode": "react"}, headers=auth
    ).json()["run_id"]
    ticket = anonymous_client.post(f"/runs/{run_id}/ws-ticket", headers=auth).json()["ticket"]
    seen: list[str] = []
    # Connect without Authorization header using the query-string ticket.
    with anonymous_client.websocket_connect(f"/ws/{run_id}?ticket={ticket}") as ws:
        while True:
            msg = ws.receive_text()
            seen.append(msg)
            if "run_finished" in msg:
                break
    assert any("run_started" in m for m in seen)
    assert any("run_finished" in m for m in seen)


def test_ws_ticket_is_single_use(anonymous_client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    auth = {"Authorization": "Bearer test-token"}
    run_id = anonymous_client.post(
        "/runs", json={"task": "Sum 0..100", "mode": "react"}, headers=auth
    ).json()["run_id"]
    ticket = anonymous_client.post(f"/runs/{run_id}/ws-ticket", headers=auth).json()["ticket"]
    with anonymous_client.websocket_connect(f"/ws/{run_id}?ticket={ticket}") as ws:
        assert "run_started" in ws.receive_text()

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        anonymous_client.websocket_connect(f"/ws/{run_id}?ticket={ticket}"),
    ):
        pass
    assert exc_info.value.code == 4401


def test_ws_ticket_is_bound_to_its_run_id(anonymous_client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    auth = {"Authorization": "Bearer test-token"}
    run_a = anonymous_client.post(
        "/runs", json={"task": "Sum 0..100", "mode": "react"}, headers=auth
    ).json()["run_id"]
    run_b = anonymous_client.post(
        "/runs", json={"task": "Sum 0..100", "mode": "react"}, headers=auth
    ).json()["run_id"]
    ticket_a = anonymous_client.post(f"/runs/{run_a}/ws-ticket", headers=auth).json()["ticket"]

    # Presenting run_a's ticket on run_b fails and burns the ticket.
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        anonymous_client.websocket_connect(f"/ws/{run_b}?ticket={ticket_a}"),
    ):
        pass
    assert exc_info.value.code == 4401

    with (
        pytest.raises(WebSocketDisconnect) as exc_info2,
        anonymous_client.websocket_connect(f"/ws/{run_a}?ticket={ticket_a}"),
    ):
        pass
    assert exc_info2.value.code == 4401


def test_ws_ticket_expires_after_ttl(platform_factory) -> None:

    from starlette.websockets import WebSocketDisconnect

    auth = {"Authorization": "Bearer test-token"}
    app = create_app(platform_factory, api_token="test-token", ws_ticket_ttl=0.02)
    with TestClient(app) as tc:
        run_id = tc.post(
            "/runs", json={"task": "Sum 0..100", "mode": "react"}, headers=auth
        ).json()["run_id"]
        ticket = tc.post(f"/runs/{run_id}/ws-ticket", headers=auth).json()["ticket"]
        time.sleep(0.05)
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            tc.websocket_connect(f"/ws/{run_id}?ticket={ticket}"),
        ):
            pass
        assert exc_info.value.code == 4401


def test_ws_rejects_unknown_and_empty_ticket(anonymous_client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    for query in ("?ticket=", "?ticket=forged-ticket"):
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            anonymous_client.websocket_connect(f"/ws/run_unknown{query}"),
        ):
            pass
        assert exc_info.value.code == 4401


def test_ws_rejects_when_unconfigured_with_1011(platform_factory) -> None:
    from starlette.websockets import WebSocketDisconnect

    with (
        TestClient(create_app(platform_factory, api_token="")) as unconfigured,
        pytest.raises(WebSocketDisconnect) as exc_info,
        unconfigured.websocket_connect("/ws/run_unknown"),
    ):
        pass
    assert exc_info.value.code == 1011


def test_create_app_rejects_non_positive_ticket_ttl(platform_factory) -> None:
    with pytest.raises(ValueError, match="ws_ticket_ttl"):
        create_app(platform_factory, api_token="test-token", ws_ticket_ttl=0.0)
    with pytest.raises(ValueError, match="ws_ticket_ttl"):
        create_app(platform_factory, api_token="test-token", ws_ticket_ttl=-5.0)


def test_multiple_tickets_for_same_run_are_independent(anonymous_client: TestClient) -> None:
    auth = {"Authorization": "Bearer test-token"}
    run_id = anonymous_client.post(
        "/runs", json={"task": "Sum 0..100", "mode": "react"}, headers=auth
    ).json()["run_id"]
    t1 = anonymous_client.post(f"/runs/{run_id}/ws-ticket", headers=auth).json()["ticket"]
    t2 = anonymous_client.post(f"/runs/{run_id}/ws-ticket", headers=auth).json()["ticket"]
    assert t1 != t2
    with anonymous_client.websocket_connect(f"/ws/{run_id}?ticket={t1}") as ws1:
        assert "run_started" in ws1.receive_text()
    with anonymous_client.websocket_connect(f"/ws/{run_id}?ticket={t2}") as ws2:
        assert "run_started" in ws2.receive_text()


def test_bearer_header_takes_precedence_and_spares_ticket(anonymous_client: TestClient) -> None:
    auth = {"Authorization": "Bearer test-token"}
    run_id = anonymous_client.post(
        "/runs", json={"task": "Sum 0..100", "mode": "react"}, headers=auth
    ).json()["run_id"]
    ticket = anonymous_client.post(f"/runs/{run_id}/ws-ticket", headers=auth).json()["ticket"]
    # Connect with valid Bearer header; ticket should remain unconsumed.
    with anonymous_client.websocket_connect(f"/ws/{run_id}?ticket={ticket}", headers=auth) as ws:
        assert "run_started" in ws.receive_text()
    with anonymous_client.websocket_connect(f"/ws/{run_id}?ticket={ticket}") as ws2:
        assert "run_started" in ws2.receive_text()


def test_case_insensitive_bearer_scheme(platform_factory) -> None:
    app = create_app(platform_factory, api_token="test-token")
    with TestClient(app, headers={"Authorization": "bearer test-token"}) as lower_client:
        resp = lower_client.post("/runs", json={"task": "Sum 0..100", "mode": "react"})
        assert resp.status_code == 202


def test_malformed_authorization_headers_are_rejected(platform_factory) -> None:
    app = create_app(platform_factory, api_token="test-token")
    with TestClient(app) as anon:
        for header in ("Basic dGVzdA==", "Bearer", "Bearer ", "Token test-token"):
            resp = anon.get("/runs/run_x", headers={"Authorization": header})
            assert resp.status_code == 401


def test_env_token_fallback_when_api_token_is_none(
    platform_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OS_API_TOKEN", "from-env-token")
    with TestClient(
        create_app(platform_factory),
        headers={"Authorization": "Bearer from-env-token"},
    ) as env_client:
        assert env_client.get("/runs/run_x").status_code == 404


def test_serve_refuses_to_start_without_token(
    platform_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from os_server.app import serve

    monkeypatch.delenv("OS_API_TOKEN", raising=False)
    monkeypatch.delenv("CELA_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="API authentication is required"):
        serve(platform_factory("react", 5), api_token="")


def test_cli_serve_exits_2_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from os_server.cli import main

    monkeypatch.delenv("OS_API_TOKEN", raising=False)
    monkeypatch.delenv("CELA_API_TOKEN", raising=False)
    assert main(["serve", "--demo"]) == 2


async def test_real_socket_e2e_bearer_and_ws_ticket(platform_factory) -> None:
    """Real TCP socket E2E via uvicorn + httpx + websockets."""
    import socket

    import httpx
    import uvicorn
    import websockets
    from websockets.exceptions import InvalidStatus

    app = create_app(platform_factory, api_token="wire-secret-token")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    import asyncio

    serve_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await await_until(lambda: server.started, what="uvicorn accepting connections")

        base_http = f"http://127.0.0.1:{port}"
        base_ws = f"ws://127.0.0.1:{port}"
        auth_headers = {"Authorization": "Bearer wire-secret-token"}

        async with httpx.AsyncClient(base_url=base_http) as http:
            assert (await http.get("/health")).json() == {"status": "ok"}
            assert (await http.post("/runs", json={"task": "Sum 0..100"})).status_code == 401

            start = await http.post(
                "/runs", json={"task": "Sum 0..100", "mode": "react"}, headers=auth_headers
            )
            assert start.status_code == 202
            run_id = start.json()["run_id"]

            ticket_resp = await http.post(f"/runs/{run_id}/ws-ticket", headers=auth_headers)
            assert ticket_resp.status_code == 200
            ticket = ticket_resp.json()["ticket"]

        # 1) Unauthenticated WS handshake is rejected before accept() -> HTTP 403
        with pytest.raises(InvalidStatus) as exc_info:
            async with websockets.connect(f"{base_ws}/ws/{run_id}"):
                pass
        assert exc_info.value.response.status_code == 403

        # 2) Browser-style ?ticket= WS handshake succeeds without Authorization header
        async with websockets.connect(f"{base_ws}/ws/{run_id}?ticket={ticket}") as ws:
            first_frame = await ws.recv()
            assert "run_started" in first_frame

        # 3) Replaying the single-use ticket over the wire is rejected -> HTTP 403
        with pytest.raises(InvalidStatus) as replay_exc:
            async with websockets.connect(f"{base_ws}/ws/{run_id}?ticket={ticket}"):
                pass
        assert replay_exc.value.response.status_code == 403
    finally:
        server.should_exit = True
        await serve_task
        sock.close()


def test_public_docs_are_served_by_default(client: TestClient) -> None:
    """The deploy runbook's `/docs` ⇒ 200 smoke signal must stay true by default."""
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_public_docs_can_be_switched_off_per_deployment(
    platform_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CELA_PUBLIC_DOCS=0` removes the anonymous route map (review 2026-10-21, S1).

    The API itself is unaffected: `/health` still answers and the schema is
    still generated in-process (`app.openapi()`), so internal callers and the
    existing contract tests keep working.
    """
    monkeypatch.setenv("CELA_PUBLIC_DOCS", "0")
    app = create_app(platform_factory, api_token="test-token")
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as test_client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert test_client.get(path).status_code == 404, path
        assert test_client.get("/health").status_code == 200
        assert test_client.get("/runs").status_code == 200
    assert "/runs" in app.openapi()["paths"]


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", ""])
def test_public_docs_flag_defaults_to_on_for_unknown_values(
    platform_factory, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("CELA_PUBLIC_DOCS", raw)
    assert public_docs_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "no", " FALSE "])
def test_public_docs_flag_parses_explicit_off(
    platform_factory, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("CELA_PUBLIC_DOCS", raw)
    assert public_docs_enabled() is False
