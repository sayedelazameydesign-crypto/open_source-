"""Reach HTTP surfaces (ADR-0013 phase 3): /api/reach/* and /cela/v1/reach/*.

Every request goes through a ``TestClient`` against the real ``create_app``, and
the outside world is replaced rather than trusted: an ``httpx.MockTransport`` for
the native channels (which also *counts* requests, so "nothing was fetched" is
provable) and an injected runner for the CLI channels.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from os_reach.security import default_reach_policy  # noqa: E402
from os_server.app import create_app  # noqa: E402
from os_server.demo import build_demo_platform_for, demo_egress_policy  # noqa: E402
from os_server.reach_api import (  # noqa: E402
    REACH_API_PREFIX,
    REACH_PRIVATE_PREFIX,
    ReachService,
)
from os_server.runtime import ApiRuntime  # noqa: E402

PLATFORM_TOKEN = "platform-secret-token"
PRIVATE_TOKEN = "cela-private-secret-token"

FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>News</title>
<item><title>First</title><link>https://example.org/1</link></item>
<item><title>Second</title><link>https://example.org/2</link></item>
</channel></rss>"""

REPO = {
    "full_name": "sayedelazameydesign-crypto/open_source-",
    "description": "the platform",
    "stargazers_count": 7,
    "forks_count": 1,
    "language": "Python",
    "html_url": "https://github.com/sayedelazameydesign-crypto/open_source-",
}


class CountingTransport(httpx.MockTransport):
    """MockTransport that records every request it answered."""

    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await super().handle_async_request(request)


def html_response(request: httpx.Request) -> httpx.Response:
    """One deterministic site per path — order matters, the first match wins."""
    path = request.url.path
    if path.endswith("/feed.xml"):
        return httpx.Response(200, text=FEED, headers={"content-type": "application/rss+xml"})
    if request.url.host == "api.github.com" and path.startswith("/search/"):
        return httpx.Response(
            200, json={"total_count": 1, "items": [{"full_name": "x/y", "stars": 3}]}
        )
    if request.url.host == "api.github.com":
        return httpx.Response(200, json=REPO)
    if request.url.host == "example.org":
        return httpx.Response(200, html="<html><body><p>plain words</p></body></html>")
    return httpx.Response(200, text="ok")


class FakeRunner:
    def __init__(self, result=None, raises: Exception | None = None) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[list[str]] = []

    async def __call__(self, argv, env, timeout_s, max_bytes):
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        from os_reach import CLIProcessResult

        return self.result or CLIProcessResult(exit_code=0, stdout="2 results")


@pytest.fixture
def transport() -> CountingTransport:
    return CountingTransport(html_response)


@pytest.fixture
def reach_service(transport: CountingTransport, tmp_path) -> ReachService:
    """An enabled service whose transport and config are entirely local."""
    return ReachService(
        enabled=True,
        policy=default_reach_policy(),
        transport=transport,
        runner=FakeRunner(),
        env={
            "CELA_REACH_CLI_ENABLE": "github_search",
            "CELA_REACH_CONFIG": str(tmp_path / "reach.toml"),
        },
    )


@pytest.fixture
def app(reach_service: ReachService):
    runtime = ApiRuntime(
        lambda mode, max_steps: build_demo_platform_for(mode),
        egress_policy=demo_egress_policy(),
    )
    return create_app(
        runtime.make_platform,
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        runtime=runtime,
        reach_service=reach_service,
    )


@pytest.fixture
def client(app) -> Iterator[Any]:
    with TestClient(app) as tc:
        yield tc


def auth(token: str = PLATFORM_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- auth


@pytest.mark.parametrize("prefix", [REACH_API_PREFIX, REACH_PRIVATE_PREFIX])
def test_an_anonymous_request_is_refused(client, prefix: str) -> None:
    assert client.get(f"{prefix}/channels").status_code == 401


@pytest.mark.parametrize("prefix", [REACH_API_PREFIX, REACH_PRIVATE_PREFIX])
def test_a_wrong_token_is_refused(client, prefix: str) -> None:
    assert client.get(f"{prefix}/channels", headers=auth("nope")).status_code == 401


def test_each_namespace_accepts_only_its_own_token(client) -> None:
    """Reach does not become a second authentication story: parent token, or nothing."""
    assert (
        client.get(f"{REACH_API_PREFIX}/channels", headers=auth(PRIVATE_TOKEN)).status_code == 401
    )
    assert (
        client.get(f"{REACH_PRIVATE_PREFIX}/channels", headers=auth(PLATFORM_TOKEN)).status_code
        == 401
    )


def test_the_private_namespace_accepts_the_private_token(client) -> None:
    response = client.get(f"{REACH_PRIVATE_PREFIX}/channels", headers=auth(PRIVATE_TOKEN))
    assert response.status_code == 200


def test_reach_is_off_by_default(app) -> None:
    """The surface exists, but an unconfigured deployment cannot fetch anything."""
    runtime = ApiRuntime(lambda mode, max_steps: build_demo_platform_for(mode))
    plain = create_app(runtime.make_platform, api_token=PLATFORM_TOKEN, runtime=runtime)
    with TestClient(plain) as anonymous_client:
        response = anonymous_client.get(f"{REACH_API_PREFIX}/channels", headers=auth())
        assert response.status_code == 503
        assert "CELA_REACH_ENABLE" in response.json()["detail"]


# -------------------------------------------------------------------- listing


def test_a_stricter_policy_binds_the_reach_surface_too(tmp_path) -> None:
    """create_app(reach_policy=...) narrows native channels — and is really applied."""
    from os_server.demo import build_demo_platform_for
    from os_server.runtime import ApiRuntime
    from os_tools.egress import EgressPolicy

    narrow = EgressPolicy.allow_hosts(
        "example.org", schemes={"https"}, ports={443}, tools={"web_read"}, resolve_dns=False
    )
    service = ReachService(
        enabled=True,
        policy=narrow,
        transport=CountingTransport(html_response),
        runner=FakeRunner(),
        env={"CELA_REACH_CLI_ENABLE": "", "CELA_REACH_CONFIG": str(tmp_path / "reach.toml")},
    )
    runtime = ApiRuntime(lambda mode, max_steps: build_demo_platform_for(mode))
    with TestClient(
        create_app(
            runtime.make_platform,
            api_token=PLATFORM_TOKEN,
            runtime=runtime,
            reach_service=service,
        )
    ) as session:
        allowed = session.post(
            f"{REACH_API_PREFIX}/read", headers=auth(), json={"url": "https://example.org/a"}
        )
        denied = session.post(
            f"{REACH_API_PREFIX}/read", headers=auth(), json={"url": "https://other.test/a"}
        )
    assert allowed.status_code == 200
    assert denied.status_code == 403
    assert "egress denied" in denied.json()["detail"]


def test_channels_lists_both_tiers_with_their_state(client) -> None:
    payload = client.get(f"{REACH_API_PREFIX}/channels", headers=auth()).json()
    assert payload["enabled"] is True
    assert set(payload["channels"]) == {"github", "rss", "web"}
    assert "github_search" in payload["cli_channels"]
    # A CLI channel that is enabled but whose binary is missing is 'off', not 'ok'.
    assert payload["cli_channels"]["bilibili_search"]["status"] == "off"
    assert payload["coverage"]["verified"] < payload["coverage"]["upstream"]
    assert payload["audit"] == {"allowed": 0, "denied": 0}


def test_channels_never_returns_a_channel_blast_radius(client) -> None:
    """Health messages may describe risk; they must not leak a credential value."""
    payload = client.get(f"{REACH_API_PREFIX}/channels", headers=auth()).json()
    blob = str(payload)
    assert "Bearer " not in blob and "ghp_" not in blob


# --------------------------------------------------------------------- native


def test_read_returns_text_and_touches_only_the_requested_host(client, transport) -> None:
    response = client.post(
        f"{REACH_API_PREFIX}/read", headers=auth(), json={"url": "https://example.org/a"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["channel"] == "web" and "plain words" in body["text"]
    assert [r.url.host for r in transport.requests] == ["example.org"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://[::1]/",
        "file:///etc/passwd",
        "not-a-url",
    ],
)
def test_read_refuses_what_the_policy_refuses_without_fetching(client, transport, url: str) -> None:
    response = client.post(f"{REACH_API_PREFIX}/read", headers=auth(), json={"url": url})
    assert response.status_code in {403, 422}
    assert transport.requests == []


def test_rss_returns_the_feed_items(client) -> None:
    response = client.post(
        f"{REACH_API_PREFIX}/rss",
        headers=auth(),
        json={"url": "https://example.org/feed.xml", "limit": 1},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["channel"] == "rss"
    assert len(body["items"]) == 1 and body["items"][0]["title"] == "First"


def test_rss_validates_its_url_like_read_does(client, transport) -> None:
    """The /rss model shares the URL rule; this pins that it is really applied."""
    response = client.post(
        f"{REACH_API_PREFIX}/rss", headers=auth(), json={"url": "file:///etc/passwd"}
    )
    assert response.status_code == 422
    assert transport.requests == []


def test_github_repo_lookup(client) -> None:
    response = client.post(
        f"{REACH_API_PREFIX}/github",
        headers=auth(),
        json={"repo": "sayedelazameydesign-crypto/open_source-"},
    )
    body = response.json()
    assert body["kind"] == "repo" and body["data"]["stars"] == 7


def test_github_search_through_the_search_endpoint(client) -> None:
    payload = {"query": "agents"}
    response = client.post(f"{REACH_API_PREFIX}/github", headers=auth(), json=payload)
    body = response.json()
    assert body["kind"] == "search"
    assert [item["full_name"] for item in body["data"]] == ["x/y"]
    assert set(body["data"][0]) >= {"full_name", "stars", "language"}


def test_github_needs_a_repo_or_a_query(client) -> None:
    response = client.post(f"{REACH_API_PREFIX}/github", headers=auth(), json={})
    assert response.status_code == 422
    assert "repo" in response.json()["detail"]


# ------------------------------------------------------------------------ cli


def test_an_enabled_cli_channel_runs_and_is_audited(client, reach_service) -> None:
    response = client.post(
        f"{REACH_API_PREFIX}/cli/github_search", headers=auth(), json={"query": "agents"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "2 results" and body["exit_code"] == 0
    entry = reach_service.audit().entries[-1]
    assert entry.channel == "github_search" and entry.decision == "allowed"
    assert entry.query_sha256 and "agents" not in entry.to_jsonl()


def test_a_disabled_channel_is_403_with_its_code(client) -> None:
    response = client.post(
        f"{REACH_API_PREFIX}/cli/bilibili_search", headers=auth(), json={"query": "agents"}
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "CLI_CHANNEL_DISABLED"
    assert "CELA_REACH_CLI_ENABLE" in detail["error"]


def test_an_unknown_channel_is_404_and_an_unimplemented_one_is_501(client) -> None:
    unknown = client.post(f"{REACH_API_PREFIX}/cli/nope", headers=auth(), json={"query": "x"})
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "CLI_CHANNEL_UNKNOWN"

    unimplemented = client.post(f"{REACH_API_PREFIX}/cli/boss", headers=auth(), json={"query": "x"})
    assert unimplemented.status_code == 501
    assert unimplemented.json()["detail"]["code"] == "CLI_CHANNEL_NOT_IMPLEMENTED"


def test_a_channel_that_needs_the_operators_session_is_403_even_when_enabled(app, tmp_path) -> None:
    service = ReachService(
        enabled=True,
        policy=default_reach_policy(),
        transport=CountingTransport(html_response),
        runner=FakeRunner(),
        env={
            "CELA_REACH_CLI_ENABLE": "twitter_search",
            "CELA_REACH_CONFIG": str(tmp_path / "reach.toml"),
        },
    )
    runtime = ApiRuntime(lambda mode, max_steps: build_demo_platform_for(mode))
    with TestClient(
        create_app(
            runtime.make_platform,
            api_token=PLATFORM_TOKEN,
            runtime=runtime,
            reach_service=service,
        )
    ) as session:
        response = session.post(
            f"{REACH_API_PREFIX}/cli/twitter_search", headers=auth(), json={"query": "agents"}
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CLI_CHANNEL_AUTH_NOT_ACKNOWLEDGED"


def test_an_oversized_cli_query_is_rejected_before_anything_runs(client, reach_service) -> None:
    response = client.post(
        f"{REACH_API_PREFIX}/cli/github_search", headers=auth(), json={"query": "x" * 2_001}
    )
    assert response.status_code == 422
    assert reach_service.registry().cli.get("github_search").audit.entries == []


def test_a_timeout_is_504_and_a_nonzero_exit_is_502(app, tmp_path) -> None:
    from os_reach import CLIProcessResult

    cases = [
        (CLIProcessResult(timed_out=True), 504, "CLI_TIMEOUT"),
        (CLIProcessResult(exit_code=1, stderr="boom"), 502, "CLI_NONZERO_EXIT"),
        (CLIProcessResult(exit_code=None, stderr="no such binary"), 502, "CLI_NONZERO_EXIT"),
    ]
    for outcome, expected_status, expected_code in cases:
        service = ReachService(
            enabled=True,
            policy=default_reach_policy(),
            transport=CountingTransport(html_response),
            runner=FakeRunner(outcome),
            env={
                "CELA_REACH_CLI_ENABLE": "github_search",
                "CELA_REACH_CONFIG": str(tmp_path / "reach.toml"),
            },
        )
        runtime = ApiRuntime(lambda mode, max_steps: build_demo_platform_for(mode))
        with TestClient(
            create_app(
                runtime.make_platform,
                api_token=PLATFORM_TOKEN,
                runtime=runtime,
                reach_service=service,
            )
        ) as session:
            response = session.post(
                f"{REACH_API_PREFIX}/cli/github_search", headers=auth(), json={"query": "agents"}
            )
        assert response.status_code == expected_status
        assert response.json()["detail"]["code"] == expected_code


def test_argument_injection_is_400_and_never_executes(app, tmp_path) -> None:
    runner = FakeRunner()
    service = ReachService(
        enabled=True,
        policy=default_reach_policy(),
        transport=CountingTransport(html_response),
        runner=runner,
        env={
            "CELA_REACH_CLI_ENABLE": "github_search",
            "CELA_REACH_CONFIG": str(tmp_path / "reach.toml"),
        },
    )
    runtime = ApiRuntime(lambda mode, max_steps: build_demo_platform_for(mode))
    with TestClient(
        create_app(
            runtime.make_platform, api_token=PLATFORM_TOKEN, runtime=runtime, reach_service=service
        )
    ) as session:
        response = session.post(
            f"{REACH_API_PREFIX}/cli/github_search", headers=auth(), json={"query": "--json"}
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CLI_ARGUMENT_REJECTED"
    assert runner.calls == []


# -------------------------------------------------------------------- shared


def test_both_namespaces_serve_the_same_channels(client) -> None:
    public = client.get(f"{REACH_API_PREFIX}/channels", headers=auth()).json()
    private = client.get(f"{REACH_PRIVATE_PREFIX}/channels", headers=auth(PRIVATE_TOKEN)).json()
    assert public["channels"] == private["channels"]
    assert public["cli_channels"] == private["cli_channels"]


def test_the_audit_summary_grows_with_denials(client) -> None:
    client.post(f"{REACH_API_PREFIX}/cli/bilibili_search", headers=auth(), json={"query": "x"})
    payload = client.get(f"{REACH_API_PREFIX}/channels", headers=auth()).json()
    assert payload["audit"]["denied"] >= 1
    assert payload["audit"].get("allowed", 0) == 0
