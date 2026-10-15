"""Tests for cela_use_browser: identity, ApiRuntime, CelaBrowser facade and /cela/v1."""

from __future__ import annotations

import asyncio
import importlib
import json
import tomllib
from pathlib import Path

import pytest

from conftest import await_until

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from os_core.hitl import QueueApprover  # noqa: E402
from os_core.models import ModelResponse, ScriptedModel, Usage  # noqa: E402
from os_core.state import RunStatus  # noqa: E402
from os_core.types import RiskLevel, ToolCall  # noqa: E402
from os_orchestration.platform import Platform, PlatformConfig, build_platform  # noqa: E402
from os_server.app import create_app  # noqa: E402
from os_server.cela import PRIVATE_API_PREFIX, CelaBrowser  # noqa: E402
from os_server.demo import (  # noqa: E402
    build_demo_platform_for,
    build_registry,
    demo_egress_policy,
)
from os_server.runtime import (  # noqa: E402
    PRODUCT_NAME,
    ApiRuntime,
    resolve_private_token,
    resolve_token,
)
from os_tools.egress import EgressPolicy, EgressPolicyError  # noqa: E402

PLATFORM_TOKEN = "platform-secret-token"
PRIVATE_TOKEN = "cela-private-secret-token"


@pytest.fixture
def platform_factory():
    return lambda mode, max_steps: build_demo_platform_for(mode)


@pytest.fixture
def runtime(platform_factory) -> ApiRuntime:
    return ApiRuntime(platform_factory, egress_policy=demo_egress_policy())


@pytest.fixture
def facade(runtime: ApiRuntime) -> CelaBrowser:
    return CelaBrowser(runtime=runtime)


@pytest.fixture
def dual_app(runtime: ApiRuntime):
    return create_app(
        runtime.make_platform,
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        runtime=runtime,
    )


@pytest.fixture
def client(dual_app):
    with TestClient(dual_app) as tc:
        yield tc


# ---------------------------------------------------------------------------
# 1. Product identity & non-breaking package layout
# ---------------------------------------------------------------------------


def test_the_rename_did_not_move_the_importable_packages() -> None:
    for pkg in ("os_core", "os_agents", "os_tools", "os_orchestration", "os_server"):
        mod = importlib.import_module(pkg)
        assert mod is not None

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["name"] == "cela-use-browser"


def test_fastapi_title_and_product_name(dual_app) -> None:
    assert PRODUCT_NAME == "cela_use_browser"
    assert PRIVATE_API_PREFIX == "/cela/v1"
    assert dual_app.title == "cela_use_browser private + platform API"


def test_existing_platform_routes_and_websocket_stay_intact(client: TestClient) -> None:
    paths = set(client.app.openapi()["paths"].keys())  # type: ignore[union-attr]
    expected_http = {
        "/health",
        "/runs",
        "/runs/{run_id}",
        "/runs/{run_id}/events",
        "/runs/{run_id}/ws-ticket",
        "/runs/{run_id}/approve",
        "/artifacts/{run_id}/{filename}",
    }
    assert expected_http <= paths

    # /ws/{run_id} is a WebSocket route (not in OpenAPI); verify it behaviorally.
    p_headers = {"Authorization": f"Bearer {PLATFORM_TOKEN}"}
    run_id = client.post(
        "/runs", json={"task": "Sum 0..100", "mode": "react"}, headers=p_headers
    ).json()["run_id"]
    with client.websocket_connect(f"/ws/{run_id}", headers=p_headers) as ws:
        assert "run_started" in ws.receive_text()


# ---------------------------------------------------------------------------
# 2. Token resolution & strict two-token isolation
# ---------------------------------------------------------------------------


def test_resolve_token_prefers_cela_api_token_over_os_api_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OS_API_TOKEN", "legacy-token")
    monkeypatch.setenv("CELA_API_TOKEN", "new-cela-token")
    assert resolve_token() == "new-cela-token"


def test_resolve_token_falls_back_to_os_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CELA_API_TOKEN", raising=False)
    monkeypatch.setenv("OS_API_TOKEN", "legacy-token")
    assert resolve_token() == "legacy-token"


def test_resolve_token_honours_explicit_override_including_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CELA_API_TOKEN", "env-token")
    assert resolve_token("explicit-token") == "explicit-token"
    assert resolve_token("") == ""


def test_resolve_private_token_reads_cela_private_api_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CELA_API_TOKEN", "platform-env")
    monkeypatch.delenv("CELA_PRIVATE_API_TOKEN", raising=False)
    assert resolve_private_token() is None
    monkeypatch.setenv("CELA_PRIVATE_API_TOKEN", "private-env")
    assert resolve_private_token() == "private-env"


def test_create_app_rejects_identical_platform_and_private_tokens(platform_factory) -> None:
    with pytest.raises(ValueError, match="private_api_token must differ from api_token"):
        create_app(platform_factory, api_token="same-secret", private_api_token="same-secret")


def test_two_tokens_two_surfaces_cross_rejection(client: TestClient) -> None:
    platform_auth = {"Authorization": f"Bearer {PLATFORM_TOKEN}"}
    private_auth = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}

    # Platform route + platform token -> 404 (auth passed, unknown run)
    assert client.get("/runs/run_x", headers=platform_auth).status_code == 404
    # Platform route + private token -> 401
    assert client.get("/runs/run_x", headers=private_auth).status_code == 401
    # Private route + platform token -> 401
    assert client.get("/cela/v1/whoami", headers=platform_auth).status_code == 401
    # Private route + private token -> 200 with product identity
    resp = client.get("/cela/v1/whoami", headers=private_auth)
    assert resp.status_code == 200
    assert resp.json()["product"] == "cela_use_browser"


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/cela/v1/whoami", None),
        ("POST", "/cela/v1/egress/check", {"url": "https://example.org/search"}),
        (
            "POST",
            "/cela/v1/browse",
            {"url": "https://example.org/search", "goal": "Search"},
        ),
        ("GET", "/cela/v1/runs", None),
        ("GET", "/cela/v1/runs/run_x", None),
        ("POST", "/cela/v1/runs/run_x/wait", {"timeout": 1.0}),
        ("GET", "/cela/v1/runs/run_x/events", None),
        (
            "POST",
            "/cela/v1/runs/run_x/approve",
            {"tool_call_id": "call_1", "decision": "approved"},
        ),
        ("GET", "/cela/v1/runs/run_x/artifacts", None),
        ("GET", "/cela/v1/runs/run_x/artifacts/screenshot.png", None),
    ],
)
def test_every_private_route_enforces_router_level_guard_before_validation_or_handler(
    client: TestClient,
    method: str,
    path: str,
    payload: dict | None,
) -> None:
    kwargs = {"json": payload} if payload is not None else {}
    # Anonymous -> 401
    assert client.request(method, path, **kwargs).status_code == 401
    # Wrong token -> 401
    assert (
        client.request(
            method, path, headers={"Authorization": "Bearer wrong-token"}, **kwargs
        ).status_code
        == 401
    )
    # Public platform token -> 401
    assert (
        client.request(
            method, path, headers={"Authorization": f"Bearer {PLATFORM_TOKEN}"}, **kwargs
        ).status_code
        == 401
    )


def test_private_api_fails_closed_with_503_when_private_token_is_unconfigured(
    platform_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CELA_PRIVATE_API_TOKEN", raising=False)
    app = create_app(platform_factory, api_token=PLATFORM_TOKEN, private_api_token="")
    with TestClient(app) as tc:
        resp = tc.get("/cela/v1/whoami", headers={"Authorization": f"Bearer {PRIVATE_TOKEN}"})
        assert resp.status_code == 503
        assert "CELA_PRIVATE_API_TOKEN" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 3. Pre-flight egress enforcement & parity between CelaBrowser and /cela/v1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("denied_url", "expected_reason"),
    [
        (
            "https://evil.net",
            "no allow rule permits https://evil.net:443 for 'browser'",
        ),
        (
            "http://169.254.169.254/latest/meta-data/",
            "network boundary: 169.254.169.254 is a cloud metadata endpoint",
        ),
        (
            "http://127.0.0.1:8080/admin",
            "network boundary: 127.0.0.1 is loopback",
        ),
        (
            "http://example.org/insecure",
            "no allow rule permits http://example.org:80 for 'browser'",
        ),
    ],
)
async def test_the_facade_and_the_http_namespace_agree_on_the_verdict(
    facade: CelaBrowser,
    client: TestClient,
    denied_url: str,
    expected_reason: str,
) -> None:
    with pytest.raises(EgressPolicyError) as exc_info:
        await facade.browse(denied_url, "Attempt navigation")
    assert str(exc_info.value) == expected_reason

    resp = client.post(
        "/cela/v1/browse",
        json={"url": denied_url, "goal": "Attempt navigation"},
        headers={"Authorization": f"Bearer {PRIVATE_TOKEN}"},
    )
    assert resp.status_code == 403
    http_detail = resp.json()["detail"]
    assert http_detail == f"egress denied: {exc_info.value}"
    # Neither door started a run when egress was denied.
    assert facade.list_runs() == []


@pytest.mark.parametrize(
    "bad_url",
    [
        "ftp://example.org",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "not-a-url",
        "https://",
    ],
)
async def test_bad_scheme_rejected_before_run_in_both_facade_and_http(
    facade: CelaBrowser,
    client: TestClient,
    bad_url: str,
) -> None:
    expected_msg = f"{bad_url!r} is not an http(s) URL with a host"
    with pytest.raises(ValueError) as exc_info:
        await facade.browse(bad_url)
    assert str(exc_info.value) == expected_msg

    resp = client.post(
        "/cela/v1/browse",
        json={"url": bad_url},
        headers={"Authorization": f"Bearer {PRIVATE_TOKEN}"},
    )
    assert resp.status_code == 422
    assert expected_msg in resp.text
    assert facade.list_runs() == []


def test_egress_check_endpoint_and_facade_method(facade: CelaBrowser, client: TestClient) -> None:
    private_auth = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}
    ok_facade = facade.check_url("https://example.org/search")
    ok_http = client.post(
        "/cela/v1/egress/check",
        json={"url": "https://example.org/search"},
        headers=private_auth,
    )
    assert ok_http.status_code == 200
    assert ok_http.json()["allowed"] is True
    assert ok_http.json()["reason"] == ok_facade.reason

    deny_facade = facade.check_url("https://evil.net/")
    deny_http = client.post(
        "/cela/v1/egress/check",
        json={"url": "https://evil.net/"},
        headers=private_auth,
    )
    assert deny_http.status_code == 200
    assert deny_http.json()["allowed"] is False
    assert deny_http.json()["reason"] == deny_facade.reason


# ---------------------------------------------------------------------------
# 4. End-to-end browse runs, shared runtime, waiting, SSE and artifacts
# ---------------------------------------------------------------------------


async def test_facade_browse_and_wait_round_trip(facade: CelaBrowser) -> None:
    started = await facade.browse(
        "https://example.org/search", "Check the open-source model list", mode="flow"
    )
    assert started["run_id"].startswith("run_")
    assert started["host"] == "example.org"

    completed = await facade.wait(started["run_id"], timeout=10.0)
    assert completed["status"] == "completed"
    assert completed["target_url"] == "https://example.org/search"
    artifacts = facade.list_artifacts(started["run_id"])
    assert any(a["filename"] == "screenshot.png" for a in artifacts)
    shot = facade.get_artifact(started["run_id"], "screenshot.png")
    assert shot["mime_type"] == "image/png"


async def test_facade_browse_with_wait_true_returns_completed_snapshot(
    facade: CelaBrowser,
) -> None:
    result = await facade.browse(
        "https://example.org/search",
        "Sum 0..100",
        mode="react",
        wait=True,
        timeout=10.0,
    )
    assert result["status"] == "completed"
    assert "5050" in result["final_answer"]
    assert result["url"] == "https://example.org/search"


def test_http_browse_wait_true_returns_200_and_shares_state_with_platform(
    client: TestClient, facade: CelaBrowser
) -> None:
    private_auth = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}
    platform_auth = {"Authorization": f"Bearer {PLATFORM_TOKEN}"}

    resp = client.post(
        "/cela/v1/browse",
        json={
            "url": "https://example.org/search",
            "goal": "Execute the plan",
            "mode": "flow",
            "wait": True,
            "timeout": 10.0,
        },
        headers=private_auth,
    )
    assert resp.status_code == 200
    body = resp.json()
    run_id = body["run_id"]
    assert body["status"] == "completed"

    # Visible through /cela/v1/runs, /cela/v1/runs/{id}, platform /runs/{id}, and CelaBrowser!
    assert any(
        r["run_id"] == run_id
        for r in client.get("/cela/v1/runs", headers=private_auth).json()["runs"]
    )
    assert (
        client.get(f"/cela/v1/runs/{run_id}", headers=private_auth).json()["status"] == "completed"
    )
    assert client.get(f"/runs/{run_id}", headers=platform_auth).json()["status"] == "completed"
    assert facade.get_run(run_id)["status"] == "completed"

    # Artifacts visible through /cela/v1/runs/{id}/artifacts and /cela/v1/runs/{id}/artifacts/{name}
    art_list = client.get(f"/cela/v1/runs/{run_id}/artifacts", headers=private_auth)
    assert art_list.status_code == 200
    assert any(a["filename"] == "screenshot.png" for a in art_list.json()["artifacts"])

    art_item = client.get(f"/cela/v1/runs/{run_id}/artifacts/screenshot.png", headers=private_auth)
    assert art_item.status_code == 200
    assert art_item.json()["filename"] == "screenshot.png"


def test_http_browse_async_then_wait_and_sse_events(client: TestClient) -> None:
    private_auth = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}
    start = client.post(
        "/cela/v1/browse",
        json={"url": "https://example.org/search", "goal": "Sum 0..100", "mode": "react"},
        headers=private_auth,
    )
    assert start.status_code == 202
    run_id = start.json()["run_id"]

    waited = client.post(
        f"/cela/v1/runs/{run_id}/wait",
        json={"timeout": 10.0},
        headers=private_auth,
    )
    assert waited.status_code == 200
    assert waited.json()["status"] == "completed"

    with client.stream("GET", f"/cela/v1/runs/{run_id}/events", headers=private_auth) as sse:
        assert sse.status_code == 200
        chunks = list(sse.iter_text())
    joined = "".join(chunks)
    assert "run_started" in joined
    assert "run_finished" in joined


def test_private_sse_resumes_from_a_sequence_cursor(client: TestClient) -> None:
    """`/cela/v1` forwards the same resume cursor as the platform surface."""
    private_auth = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}
    start = client.post(
        "/cela/v1/browse",
        json={"url": "https://example.org/search", "goal": "Sum 0..100", "mode": "react"},
        headers=private_auth,
    )
    assert start.status_code == 202
    run_id = start.json()["run_id"]
    waited = client.post(
        f"/cela/v1/runs/{run_id}/wait", json={"timeout": 10.0}, headers=private_auth
    )
    assert waited.status_code == 200

    buffered = client.app.state.runtime.runs[run_id].bus.buffer
    cursor = buffered[1].seq

    seen: list[str] = []
    with client.stream(
        "GET", f"/cela/v1/runs/{run_id}/events?after_seq={cursor}", headers=private_auth
    ) as sse:
        assert sse.status_code == 200
        for line in sse.iter_lines():
            if not line.startswith("data: "):
                continue
            message = json.loads(line.removeprefix("data: "))
            seen.append(message["id"])
            if message["type"] == "run_finished":
                break

    assert seen == [event.id for event in buffered[2:]]


async def test_wait_timeout_is_shielded_and_does_not_cancel_run() -> None:
    gate = asyncio.Event()

    def slow_platform_factory(mode: str, max_steps: int) -> Platform:
        registry, _guard, _browser = build_registry()

        @registry.register(
            "slow_op",
            "Waits for gate.",
            {"type": "object", "properties": {}},
        )
        async def slow_op() -> str:
            await gate.wait()
            return "finished-slow-op"

        model = ScriptedModel(
            script=[
                ModelResponse(
                    content="Calling slow_op",
                    tool_calls=[ToolCall(name="slow_op", args={})],
                    usage=Usage(prompt_tokens=10, completion_tokens=5),
                )
            ],
            final_content="slow op completed",
        )
        return build_platform(model, registry=registry, mode="react")

    browser = CelaBrowser(slow_platform_factory)
    started = await browser.browse("https://example.org/search", "Run slow op")
    run_id = started["run_id"]

    with pytest.raises(TimeoutError):
        await browser.wait(run_id, timeout=0.02)

    # The underlying task was shielded and is still running!
    assert browser.get_run(run_id)["status"] == RunStatus.RUNNING.value
    gate.set()
    final = await browser.wait(run_id, timeout=5.0)
    assert final["status"] == RunStatus.COMPLETED.value


def test_private_approve_endpoint_and_error_codes(client: TestClient) -> None:
    private_auth = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}
    start = client.post(
        "/cela/v1/browse",
        json={"url": "https://example.org/search", "goal": "Sum 0..100"},
        headers=private_auth,
    )
    run_id = start.json()["run_id"]

    # Bad decision -> 422
    bad = client.post(
        f"/cela/v1/runs/{run_id}/approve",
        json={"tool_call_id": "call_1", "decision": "not-a-decision"},
        headers=private_auth,
    )
    assert bad.status_code == 422

    # Valid decision on QueueApprover -> 200
    ok = client.post(
        f"/cela/v1/runs/{run_id}/approve",
        json={"tool_call_id": "call_nonexistent", "decision": "approved"},
        headers=private_auth,
    )
    assert ok.status_code == 200
    assert ok.json() == {"delivered": False}


def test_private_approve_returns_409_when_platform_has_non_interactive_approver() -> None:
    from os_core.hitl import DenyApprover

    registry, _guard, _browser = build_registry()
    plat = Platform(
        model=ScriptedModel(final_content="ok"),
        registry=registry,
        config=PlatformConfig(mode="react"),
        approver=DenyApprover(),
    )
    app = create_app(plat, api_token=PLATFORM_TOKEN, private_api_token=PRIVATE_TOKEN)
    private_auth = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}
    with TestClient(app) as tc:
        run_id = tc.post(
            "/cela/v1/browse",
            json={"url": "https://example.org/search", "goal": "hi"},
            headers=private_auth,
        ).json()["run_id"]
        resp = tc.post(
            f"/cela/v1/runs/{run_id}/approve",
            json={"tool_call_id": "call_1", "decision": "approved"},
            headers=private_auth,
        )
        assert resp.status_code == 409


def test_private_unknown_run_and_artifact_return_404(
    client: TestClient, facade: CelaBrowser
) -> None:
    private_auth = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}
    assert client.get("/cela/v1/runs/run_missing", headers=private_auth).status_code == 404
    assert (
        client.post(
            "/cela/v1/runs/run_missing/wait", json={"timeout": 1.0}, headers=private_auth
        ).status_code
        == 404
    )
    assert client.get("/cela/v1/runs/run_missing/events", headers=private_auth).status_code == 404
    assert (
        client.post(
            "/cela/v1/runs/run_missing/approve",
            json={"tool_call_id": "x", "decision": "approved"},
            headers=private_auth,
        ).status_code
        == 404
    )
    assert (
        client.get("/cela/v1/runs/run_missing/artifacts", headers=private_auth).status_code == 404
    )
    assert (
        client.get("/cela/v1/runs/run_missing/artifacts/nope.png", headers=private_auth).status_code
        == 404
    )

    with pytest.raises(KeyError):
        facade.get_run("run_missing")
    with pytest.raises(KeyError):
        facade.list_artifacts("run_missing")
    with pytest.raises(KeyError):
        facade.get_artifact("run_missing", "nope.png")


async def test_interactive_hitl_approval_through_facade() -> None:
    registry, _guard, _browser = build_registry()
    registry.tools["python_execute"].risk = RiskLevel.DANGEROUS
    call = ToolCall(id="call_hitl_1", name="python_execute", args={"code": "print(42)"})
    model = ScriptedModel(
        script=[
            ModelResponse(
                content="Need dangerous code execution",
                tool_calls=[call],
                usage=Usage(prompt_tokens=20, completion_tokens=10),
            )
        ],
        final_content="Approved and executed: 42",
    )
    plat = Platform(
        model=model,
        registry=registry,
        config=PlatformConfig(mode="react", max_steps=5),
        approver=QueueApprover(timeout=5.0),
    )
    browser = CelaBrowser(plat)
    started = await browser.browse("https://example.org/search", "Run dangerous step")
    run_id = started["run_id"]

    # Event-driven: wait for the prompt itself, then check the facade shows it.
    prompt = await asyncio.wait_for(plat.approver.wait_for_request(), 5.0)
    assert prompt.id == "call_hitl_1"
    assert browser.get_run(run_id).get("pending_approvals")

    delivered = browser.approve(run_id, "call_hitl_1", "approved")
    assert delivered == {"delivered": True}
    final = await browser.wait(run_id, timeout=5.0)
    assert final["status"] == "completed"


def test_custom_egress_policy_overrides_default_on_facade() -> None:
    custom = EgressPolicy.allow_hosts("docs.example.com", schemes={"https"}, ports={443})
    browser = CelaBrowser(egress_policy=custom)
    assert browser.whoami()["allowed_hosts"] == ["docs.example.com"]
    assert browser.check_url("https://docs.example.com/intro").allowed
    assert not browser.check_url("https://example.org/search").allowed


def test_http_wait_timeout_returns_504_without_cancelling_run() -> None:
    gate = asyncio.Event()

    def slow_factory(mode: str, max_steps: int) -> Platform:
        registry, _guard, _browser = build_registry()

        @registry.register("slow", "Wait on gate", {"type": "object", "properties": {}})
        async def slow() -> str:
            await gate.wait()
            return "ok"

        model = ScriptedModel(
            script=[
                ModelResponse(
                    content="slow",
                    tool_calls=[ToolCall(name="slow", args={})],
                    usage=Usage(prompt_tokens=5, completion_tokens=5),
                )
            ],
            final_content="done",
        )
        return build_platform(model, registry=registry, mode="react")

    app = create_app(slow_factory, api_token=PLATFORM_TOKEN, private_api_token=PRIVATE_TOKEN)
    private_auth = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}
    with TestClient(app) as tc:
        run_id = tc.post(
            "/cela/v1/browse",
            json={"url": "https://example.org/search", "goal": "slow"},
            headers=private_auth,
        ).json()["run_id"]
        timed_out = tc.post(
            f"/cela/v1/runs/{run_id}/wait",
            json={"timeout": 0.02},
            headers=private_auth,
        )
        assert timed_out.status_code == 504
        gate.set()
        done = tc.post(
            f"/cela/v1/runs/{run_id}/wait",
            json={"timeout": 5.0},
            headers=private_auth,
        )
        assert done.status_code == 200
        assert done.json()["status"] == "completed"


def test_http_browse_wait_timeout_returns_504() -> None:
    gate = asyncio.Event()

    def slow_factory(mode: str, max_steps: int) -> Platform:
        registry, _guard, _browser = build_registry()

        @registry.register("slow", "Wait on gate", {"type": "object", "properties": {}})
        async def slow() -> str:
            await gate.wait()
            return "ok"

        model = ScriptedModel(
            script=[
                ModelResponse(
                    content="slow",
                    tool_calls=[ToolCall(name="slow", args={})],
                    usage=Usage(prompt_tokens=5, completion_tokens=5),
                )
            ],
            final_content="done",
        )
        return build_platform(model, registry=registry, mode="react")

    app = create_app(slow_factory, api_token=PLATFORM_TOKEN, private_api_token=PRIVATE_TOKEN)
    private_auth = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}
    with TestClient(app) as tc:
        resp = tc.post(
            "/cela/v1/browse",
            json={
                "url": "https://example.org/search",
                "goal": "slow",
                "wait": True,
                "timeout": 0.02,
            },
            headers=private_auth,
        )
        assert resp.status_code == 504
        gate.set()


def test_egress_check_rejects_bad_scheme_with_422(client: TestClient) -> None:
    private_auth = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}
    resp = client.post(
        "/cela/v1/egress/check",
        json={"url": "ftp://example.org"},
        headers=private_auth,
    )
    assert resp.status_code == 422
    assert "'ftp://example.org' is not an http(s) URL with a host" in resp.text


def test_platform_accepts_cela_api_token_from_environment(
    platform_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CELA_API_TOKEN", "cela-env-token")
    monkeypatch.setenv("CELA_PRIVATE_API_TOKEN", "cela-priv-env-token")
    app = create_app(platform_factory)
    with TestClient(app) as tc:
        assert (
            tc.get("/runs/run_x", headers={"Authorization": "Bearer cela-env-token"}).status_code
            == 404
        )
        assert (
            tc.get(
                "/cela/v1/whoami", headers={"Authorization": "Bearer cela-priv-env-token"}
            ).status_code
            == 200
        )


async def test_facade_default_constructor_uses_demo_platform() -> None:
    browser = CelaBrowser()
    assert browser.whoami()["product"] == "cela_use_browser"
    res = await browser.browse("https://example.org/search", "Sum 0..100", wait=True)
    assert res["status"] == "completed"


def test_facade_approve_rejects_invalid_decision_with_value_error(
    facade: CelaBrowser,
) -> None:
    with pytest.raises(ValueError, match="decision must be 'approved' or 'denied'"):
        facade.approve("run_any", "call_1", "invalid-decision")


async def test_facade_wait_raises_key_error_for_unknown_run(facade: CelaBrowser) -> None:
    with pytest.raises(KeyError):
        await facade.wait("run_does_not_exist")


def test_artifact_404_when_run_exists_but_filename_missing(
    client: TestClient, facade: CelaBrowser
) -> None:
    private_auth = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}
    run_id = client.post(
        "/cela/v1/browse",
        json={"url": "https://example.org/search", "goal": "Sum 0..100", "wait": True},
        headers=private_auth,
    ).json()["run_id"]
    assert (
        client.get(
            f"/cela/v1/runs/{run_id}/artifacts/missing.txt", headers=private_auth
        ).status_code
        == 404
    )
    with pytest.raises(KeyError):
        facade.get_artifact(run_id, "missing.txt")


async def test_real_socket_e2e_dual_surface_and_dual_tokens(platform_factory) -> None:
    """Over-the-wire uvicorn test proving both surfaces and both tokens."""
    import socket

    import httpx
    import uvicorn

    app = create_app(
        platform_factory,
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    serve_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await await_until(lambda: server.started, what="uvicorn accepting connections")

        p_headers = {"Authorization": f"Bearer {PLATFORM_TOKEN}"}
        c_headers = {"Authorization": f"Bearer {PRIVATE_TOKEN}"}

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            assert (await http.get("/runs/run_x", headers=p_headers)).status_code == 404
            assert (await http.get("/runs/run_x", headers=c_headers)).status_code == 401
            assert (await http.get("/cela/v1/whoami", headers=p_headers)).status_code == 401

            who = await http.get("/cela/v1/whoami", headers=c_headers)
            assert who.status_code == 200
            assert who.json()["product"] == "cela_use_browser"

            denied = await http.post(
                "/cela/v1/browse",
                json={"url": "https://evil.net", "goal": "test"},
                headers=c_headers,
            )
            assert denied.status_code == 403
            assert (
                denied.json()["detail"]
                == "egress denied: no allow rule permits https://evil.net:443 for 'browser'"
            )

            ok = await http.post(
                "/cela/v1/browse",
                json={
                    "url": "https://example.org/search",
                    "goal": "Sum 0..100",
                    "mode": "react",
                    "wait": True,
                },
                headers=c_headers,
            )
            assert ok.status_code == 200
            assert ok.json()["status"] == "completed"
    finally:
        server.should_exit = True
        await serve_task
        sock.close()
