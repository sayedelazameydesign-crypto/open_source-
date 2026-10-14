"""Launcher HTTP surfaces (ADR-0015): /api/launcher/* and /cela/v1/launcher/*.

Every request goes through a ``TestClient`` against the real ``create_app``, and
the outside world is replaced rather than trusted: an ``httpx.MockTransport``
that counts requests — "the disabled surface fetched nothing" must be provable,
not assumed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from os_launcher.security import default_launcher_policy  # noqa: E402
from os_server.app import create_app  # noqa: E402
from os_server.demo import build_demo_platform_for, demo_egress_policy  # noqa: E402
from os_server.launcher_api import (  # noqa: E402
    LAUNCHER_API_PREFIX,
    LAUNCHER_PRIVATE_PREFIX,
    LauncherService,
)
from os_server.runtime import ApiRuntime  # noqa: E402

PLATFORM_TOKEN = "platform-secret-token"
PRIVATE_TOKEN = "cela-private-secret-token"

HASH = "a" * 64


class CountingTransport(httpx.MockTransport):
    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await super().handle_async_request(request)


def release_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.storyteller.ai":
        return httpx.Response(200, json={"last_fetched": 1_790_875_200, "apps": {}})
    if request.url.path.endswith("/SHA256SUMS.txt"):
        return httpx.Response(200, text=f"{HASH}  photocraft-0.5.0-macos-universal.dmg\n")
    if request.url.host == "api.github.com":
        if "/photocraft/" not in request.url.path:
            return httpx.Response(404)  # only PhotoCraft has a release in this world
        return httpx.Response(
            200,
            json={
                "tag_name": "v0.5.0",
                "name": "PhotoCraft v0.5.0",
                "published_at": "2026-10-01T12:00:00Z",
                "html_url": "https://github.com/storytold/photocraft/releases/tag/v0.5.0",
                "body": "notes",
                "prerelease": False,
                "assets": [
                    {
                        "name": "photocraft-0.5.0-macos-universal.dmg",
                        "size": 123,
                        "browser_download_url": (
                            "https://github.com/storytold/photocraft/releases/download/"
                            "v0.5.0/photocraft-0.5.0-macos-universal.dmg"
                        ),
                        "digest": f"sha256:{HASH}",
                    }
                ],
            },
        )
    return httpx.Response(404)


@pytest.fixture
def transport() -> CountingTransport:
    return CountingTransport(release_handler)


@pytest.fixture
def launcher_service(transport: CountingTransport) -> LauncherService:
    """An enabled service whose transport and policy are entirely local."""
    return LauncherService(
        enabled=True,
        policy=default_launcher_policy(env={}),
        transport=transport,
    )


@pytest.fixture
def app(launcher_service: LauncherService):
    runtime = ApiRuntime(
        lambda mode, max_steps: build_demo_platform_for(mode),
        egress_policy=demo_egress_policy(),
    )
    return create_app(
        runtime.make_platform,
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        runtime=runtime,
        launcher_service=launcher_service,
    )


@pytest.fixture
def client(app) -> Iterator[Any]:
    with TestClient(app) as tc:
        yield tc


def auth(token: str = PLATFORM_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- auth


@pytest.mark.parametrize("prefix", [LAUNCHER_API_PREFIX, LAUNCHER_PRIVATE_PREFIX])
def test_an_anonymous_request_is_refused(client, prefix: str) -> None:
    assert client.get(f"{prefix}/catalog").status_code == 401


@pytest.mark.parametrize("prefix", [LAUNCHER_API_PREFIX, LAUNCHER_PRIVATE_PREFIX])
def test_a_wrong_token_is_refused(client, prefix: str) -> None:
    assert client.get(f"{prefix}/catalog", headers=auth("nope")).status_code == 401


def test_each_namespace_accepts_only_its_own_token(client) -> None:
    """The launcher does not become a second authentication story."""
    assert (
        client.get(f"{LAUNCHER_API_PREFIX}/catalog", headers=auth(PRIVATE_TOKEN)).status_code == 401
    )
    assert (
        client.get(f"{LAUNCHER_PRIVATE_PREFIX}/catalog", headers=auth(PLATFORM_TOKEN)).status_code
        == 401
    )


def test_the_private_namespace_accepts_the_private_token(client) -> None:
    response = client.get(f"{LAUNCHER_PRIVATE_PREFIX}/catalog", headers=auth(PRIVATE_TOKEN))
    assert response.status_code == 200


# ----------------------------------------------------------------- disabled


def test_the_surface_is_off_by_default_and_fetches_nothing(transport) -> None:
    """An unconfigured deployment answers 503 — and cannot fetch through the surface."""
    runtime = ApiRuntime(lambda mode, max_steps: build_demo_platform_for(mode))
    plain = create_app(
        runtime.make_platform,
        api_token=PLATFORM_TOKEN,
        runtime=runtime,
        launcher_service=LauncherService(
            enabled=False, policy=default_launcher_policy(env={}), transport=transport
        ),
    )
    with TestClient(plain) as anonymous_client:
        response = anonymous_client.get(f"{LAUNCHER_API_PREFIX}/catalog", headers=auth())
        assert response.status_code == 503
        assert "CELA_LAUNCHER_ENABLE" in response.json()["detail"]
    assert transport.requests == []


def test_launcher_enabled_from_env_reads_only_the_one_variable() -> None:
    from os_server.launcher_api import launcher_enabled_from_env

    assert launcher_enabled_from_env({"CELA_LAUNCHER_ENABLE": "1"}) is True
    assert launcher_enabled_from_env({"CELA_LAUNCHER_ENABLE": "0"}) is False
    assert launcher_enabled_from_env({}) is False


# ------------------------------------------------------------------- catalog


def test_the_catalog_lists_thirteen_apps(client) -> None:
    response = client.get(f"{LAUNCHER_API_PREFIX}/catalog", headers=auth())
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 13
    assert body["apps"][0]["id"] == "photocraft"
    assert all(app_row["repo"].startswith("storytold/") for app_row in body["apps"])


# ------------------------------------------------------------------ releases


def test_the_release_endpoint_reports_one_apps_latest(client, transport) -> None:
    response = client.get(f"{LAUNCHER_API_PREFIX}/apps/photocraft/release", headers=auth())
    assert response.status_code == 200
    body = response.json()
    assert body["app_id"] == "photocraft"
    assert body["release"]["tag"] == "v0.5.0"
    assert transport.requests  # the network really was consulted (mocked)


def test_an_unknown_app_is_404_without_touching_the_network(client, transport) -> None:
    response = client.get(f"{LAUNCHER_API_PREFIX}/apps/not-a-craft-app/release", headers=auth())
    assert response.status_code == 404
    assert transport.requests == []


def test_check_compares_versions_and_names_the_updates(client) -> None:
    response = client.post(
        f"{LAUNCHER_API_PREFIX}/check",
        headers=auth(),
        json={"installed": {"photocraft": "0.4.0", "wordcraft": "0.1.0"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["updates"] == ["photocraft"]
    rows = {row["app_id"]: row for row in body["apps"]}
    assert rows["photocraft"]["update_available"] is True
    assert rows["photocraft"]["latest"] == "0.5.0"


def test_an_empty_check_body_is_rejected(client) -> None:
    assert client.post(f"{LAUNCHER_API_PREFIX}/check", headers=auth(), json={}).status_code == 422


def test_a_source_failure_is_502_with_a_scrubbed_detail() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cannot reach api.github.com")

    service = LauncherService(
        enabled=True, policy=default_launcher_policy(env={}), transport=CountingTransport(boom)
    )
    runtime = ApiRuntime(lambda mode, max_steps: build_demo_platform_for(mode))
    failing = create_app(
        runtime.make_platform,
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        runtime=runtime,
        launcher_service=service,
    )
    with TestClient(failing) as failing_client:
        response = failing_client.get(
            f"{LAUNCHER_API_PREFIX}/apps/photocraft/release", headers=auth()
        )
        assert response.status_code == 502
        assert "ConnectError" in response.json()["detail"]


def test_the_same_surface_serves_the_private_namespace(client) -> None:
    response = client.post(
        f"{LAUNCHER_PRIVATE_PREFIX}/check",
        headers=auth(PRIVATE_TOKEN),
        json={"installed": {"photocraft": "0.4.0"}},
    )
    assert response.status_code == 200
    assert response.json()["updates"] == ["photocraft"]
