"""Tests for the optional AI Indigo external API surface (ADR-0021).

Every test runs completely offline against an ``httpx.MockTransport`` that counts
requests, proving both sides of the isolation contract:

* core platform startup, ``/health``, and ``deploy_doctor`` require zero knowledge
  of ``INDIGO_API_KEY`` when ``CELA_INDIGO_ENABLE`` is unset or ``0``;
* disabled and missing-key requests fail closed with ``503`` before any outbound
  socket is opened, and credentials never leak into logs, ``repr()``, or errors.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from os_core.redact import contains_secret, scrub_credentials  # noqa: E402
from os_server.app import create_app  # noqa: E402
from os_server.automation import check_deployment_readiness  # noqa: E402
from os_server.indigo_api import (  # noqa: E402
    DEFAULT_INDIGO_BASE_URL,
    INDIGO_API_PREFIX,
    INDIGO_PRIVATE_PREFIX,
    IndigoClient,
    IndigoError,
    IndigoService,
    RawIndigoResponse,
    default_indigo_policy,
    scrub_indigo_secret,
)

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_TOKEN = "platform-test-token-indigo"
PRIVATE_TOKEN = "private-test-token-indigo"
TEST_INDIGO_KEY = "ai_live_test_secret_token_9876543210abcdef"


class CountingTransport(httpx.MockTransport):
    def __init__(self, handler: Any) -> None:
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await super().handle_async_request(request)


def indigo_stub(request: httpx.Request) -> httpx.Response:
    headers = {
        "X-RateLimit-Limit": "100",
        "X-RateLimit-Remaining": "97",
        "X-RateLimit-Reset": "1792500000",
    }
    path = request.url.path
    if path == "/api/v1/intelligence/trending":
        return httpx.Response(
            200,
            headers=headers,
            json={"data": [{"tool_slug": "cursor", "name": "Cursor", "trend": "rising"}]},
        )

    api_key = request.headers.get("X-API-Key", "")
    if api_key != TEST_INDIGO_KEY:
        return httpx.Response(401, headers=headers, json={"error": "No or invalid API key"})

    if path == "/api/v1/search":
        q = request.url.params.get("q", "")
        if request.url.params.get("mode") == "semantic":
            return httpx.Response(403, headers=headers, json={"error": "Scope denied"})
        return httpx.Response(
            200,
            headers=headers,
            json={
                "data": [{"slug": "cursor", "name": "Cursor", "category": "coding"}],
                "meta": {"total": 1, "query": q},
            },
        )
    if path == "/api/v1/tools":
        return httpx.Response(
            200,
            headers=headers,
            json={
                "data": [{"slug": "github-copilot", "name": "GitHub Copilot"}],
                "meta": {"page": 1, "per_page": 20, "total": 1},
            },
        )
    if path == "/api/v1/tools/compare":
        return httpx.Response(
            200,
            headers=headers,
            json={
                "tool_a": {"slug": request.url.params.get("a")},
                "tool_b": {"slug": request.url.params.get("b")},
                "diff": {"pricing": "equal"},
            },
        )
    if path == "/api/v1/tools/missing-tool":
        return httpx.Response(404, headers=headers, json={"error": "Not found"})
    if path == "/api/v1/tools/rate-limited":
        return httpx.Response(429, headers=headers, json={"error": "Rate limited"})
    if path == "/api/v1/tools/waf-challenge":
        return httpx.Response(
            403,
            headers={"content-type": "text/html; charset=UTF-8", "cf-mitigated": "challenge"},
            text="<!DOCTYPE html><title>Just a moment...</title>",
        )
    if path == "/api/v1/tools/redirect-trap":
        return httpx.Response(302, headers={**headers, "Location": "https://evil.example/steal"})
    if path.startswith("/api/v1/tools/"):
        slug = path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            headers=headers,
            json={"data": {"slug": slug, "name": slug.title(), "category": "coding"}},
        )
    if path == "/api/v1/categories":
        return httpx.Response(
            200,
            headers=headers,
            json={"data": [{"slug": "coding", "name": "Coding", "tool_count": 389}]},
        )
    if path.startswith("/api/v1/categories/"):
        slug = path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            headers=headers,
            json={"data": {"slug": slug, "name": slug.title(), "top_tools": ["cursor"]}},
        )
    return httpx.Response(404, headers=headers, json={"error": "Unknown route"})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_core_app_and_doctor_stay_independent_of_indigo_api_key() -> None:
    report = check_deployment_readiness({}, root=ROOT)
    assert report["ready"] is True
    assert report["errors"] == []
    assert report["warnings"] == []

    transport = CountingTransport(indigo_stub)
    service = IndigoService(enabled=False, transport=transport, env={})
    app = create_app(
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        indigo_service=service,
    )
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        status = client.get(f"{INDIGO_API_PREFIX}/status", headers=_auth(PLATFORM_TOKEN))
        assert status.status_code == 200
        assert status.json() == {
            "enabled": False,
            "configured": False,
            "credential_var": "INDIGO_API_KEY",
            "base_url": DEFAULT_INDIGO_BASE_URL,
        }
        disabled = client.get(
            f"{INDIGO_API_PREFIX}/search",
            params={"q": "cursor"},
            headers=_auth(PLATFORM_TOKEN),
        )
        assert disabled.status_code == 503
        assert disabled.json()["detail"]["code"] == "indigo_disabled"
    assert transport.requests == []


def test_enabled_without_key_fails_closed_before_opening_socket() -> None:
    transport = CountingTransport(indigo_stub)
    service = IndigoService(
        enabled=True,
        policy=default_indigo_policy(),
        transport=transport,
        env={},
    )
    app = create_app(
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        indigo_service=service,
    )
    with TestClient(app) as client:
        resp = client.get(
            f"{INDIGO_API_PREFIX}/search",
            params={"q": "cursor"},
            headers=_auth(PLATFORM_TOKEN),
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "indigo_unconfigured"
        assert transport.requests == []

        # Public trending route works without an API key when surface is enabled.
        trending = client.get(
            f"{INDIGO_API_PREFIX}/trending",
            params={"limit": 3},
            headers=_auth(PLATFORM_TOKEN),
        )
        assert trending.status_code == 200
        assert trending.json()["upstream"]["data"][0]["tool_slug"] == "cursor"
        assert len(transport.requests) == 1
        assert "X-API-Key" not in transport.requests[0].headers


def test_authenticated_endpoints_across_public_and_private_namespaces() -> None:
    transport = CountingTransport(indigo_stub)
    service = IndigoService(
        enabled=True,
        policy=default_indigo_policy(),
        transport=transport,
        env={"INDIGO_API_KEY": TEST_INDIGO_KEY},
    )
    app = create_app(
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        indigo_service=service,
    )
    with TestClient(app) as client:
        # Namespace isolation: platform token rejected on /cela/v1/indigo and vice versa.
        assert (
            client.get(f"{INDIGO_PRIVATE_PREFIX}/status", headers=_auth(PLATFORM_TOKEN)).status_code
            == 401
        )
        assert (
            client.get(f"{INDIGO_API_PREFIX}/status", headers=_auth(PRIVATE_TOKEN)).status_code
            == 401
        )

        status = client.get(f"{INDIGO_API_PREFIX}/status", headers=_auth(PLATFORM_TOKEN))
        assert status.status_code == 200
        assert status.json()["configured"] is True
        assert TEST_INDIGO_KEY not in json.dumps(status.json())

        search = client.get(
            f"{INDIGO_API_PREFIX}/search",
            params={"q": "cursor", "limit": 5},
            headers=_auth(PLATFORM_TOKEN),
        )
        assert search.status_code == 200
        assert search.json()["upstream"]["data"][0]["slug"] == "cursor"
        assert search.json()["rate_limit"] == {
            "limit": 100,
            "remaining": 97,
            "reset": 1792500000,
        }

        priv_search = client.get(
            f"{INDIGO_PRIVATE_PREFIX}/search",
            params={"q": "cursor"},
            headers=_auth(PRIVATE_TOKEN),
        )
        assert priv_search.status_code == 200

        tools = client.get(
            f"{INDIGO_API_PREFIX}/tools",
            params={"pricing": "free", "per_page": 5},
            headers=_auth(PLATFORM_TOKEN),
        )
        assert tools.status_code == 200
        assert tools.json()["upstream"]["data"][0]["slug"] == "github-copilot"

        # Route precedence: /tools/compare hits comparison handler, not /tools/{slug}.
        compare = client.get(
            f"{INDIGO_API_PREFIX}/tools/compare",
            params={"a": "cursor", "b": "windsurf"},
            headers=_auth(PLATFORM_TOKEN),
        )
        assert compare.status_code == 200
        assert compare.json()["upstream"]["diff"] == {"pricing": "equal"}

        detail = client.get(
            f"{INDIGO_API_PREFIX}/tools/cursor",
            headers=_auth(PLATFORM_TOKEN),
        )
        assert detail.status_code == 200
        assert detail.json()["upstream"]["data"]["slug"] == "cursor"

        categories = client.get(
            f"{INDIGO_API_PREFIX}/categories",
            headers=_auth(PLATFORM_TOKEN),
        )
        assert categories.status_code == 200
        assert categories.json()["upstream"]["data"][0]["slug"] == "coding"

        cat_detail = client.get(
            f"{INDIGO_API_PREFIX}/categories/coding",
            headers=_auth(PLATFORM_TOKEN),
        )
        assert cat_detail.status_code == 200
        assert cat_detail.json()["upstream"]["data"]["top_tools"] == ["cursor"]


def test_upstream_errors_and_redirects_fail_safely_without_leaking_key() -> None:
    transport = CountingTransport(indigo_stub)
    service = IndigoService(
        enabled=True,
        policy=default_indigo_policy(),
        transport=transport,
        env={"INDIGO_API_KEY": TEST_INDIGO_KEY},
    )
    app = create_app(
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        indigo_service=service,
    )
    with TestClient(app) as client:
        denied = client.get(
            f"{INDIGO_API_PREFIX}/search",
            params={"q": "cursor", "mode": "semantic"},
            headers=_auth(PLATFORM_TOKEN),
        )
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "indigo_scope_denied"

        missing = client.get(
            f"{INDIGO_API_PREFIX}/tools/missing-tool",
            headers=_auth(PLATFORM_TOKEN),
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "indigo_not_found"

        limited = client.get(
            f"{INDIGO_API_PREFIX}/tools/rate-limited",
            headers=_auth(PLATFORM_TOKEN),
        )
        assert limited.status_code == 429
        assert limited.json()["detail"]["rate_limit"]["reset"] == 1792500000

        waf = client.get(
            f"{INDIGO_API_PREFIX}/tools/waf-challenge",
            headers=_auth(PLATFORM_TOKEN),
        )
        assert waf.status_code == 502
        assert waf.json()["detail"]["code"] == "indigo_waf_challenge"

        redirect = client.get(
            f"{INDIGO_API_PREFIX}/tools/redirect-trap",
            headers=_auth(PLATFORM_TOKEN),
        )
        assert redirect.status_code == 502
        assert redirect.json()["detail"]["code"] == "indigo_unexpected_redirect"
        # Redirect target was never followed.
        assert all(req.url.host == "aiindigo.com" for req in transport.requests)

    bad_service = IndigoService(
        enabled=True,
        policy=default_indigo_policy(),
        transport=transport,
        env={"INDIGO_API_KEY": "wrong-indigo-key-value"},
    )
    app_bad = create_app(
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        indigo_service=bad_service,
    )
    with TestClient(app_bad) as client:
        bad = client.get(
            f"{INDIGO_API_PREFIX}/search",
            params={"q": "cursor"},
            headers=_auth(PLATFORM_TOKEN),
        )
        assert bad.status_code == 502
        assert bad.json()["detail"]["code"] == "indigo_upstream_unauthorized"
        assert "wrong-indigo-key-value" not in json.dumps(bad.json())


@pytest.mark.asyncio
async def test_egress_policy_and_scheme_pinning_block_host_redirection() -> None:
    with pytest.raises(ValueError, match="https://"):
        IndigoClient(base_url="http://aiindigo.com/api/v1", env={"INDIGO_API_KEY": TEST_INDIGO_KEY})

    transport = CountingTransport(indigo_stub)
    client = IndigoClient(
        base_url="https://attacker.example/api/v1",
        policy=default_indigo_policy(),
        transport=transport,
        env={"INDIGO_API_KEY": TEST_INDIGO_KEY},
    )
    with pytest.raises(IndigoError) as exc_info:
        await client.search("cursor")
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "egress_denied"
    assert transport.requests == []
    assert TEST_INDIGO_KEY not in repr(client)


def test_redaction_and_deploy_doctor_cover_indigo_secrets() -> None:
    for token in (
        "ai_live_1234567890abcdef",
        "aik_live_1234567890abcdef",
        "ai_live_pro_1234567890abcdef",
    ):
        assert contains_secret(token) is True
        assert token not in scrub_credentials(f"sent {token} in header")

    custom = "custom-indigo-secret-xyz"
    assert custom not in scrub_indigo_secret(f"echo {custom}", custom)

    warn_report = check_deployment_readiness({"CELA_INDIGO_ENABLE": "1"}, root=ROOT)
    assert warn_report["ready"] is True
    assert [w["id"] for w in warn_report["warnings"]] == ["indigo_api_key_unset"]

    strict_report = check_deployment_readiness(
        {
            "CELA_API_TOKEN": PLATFORM_TOKEN,
            "CELA_PRIVATE_API_TOKEN": PRIVATE_TOKEN,
            "CELA_CODE_EXEC_MODE": "disabled",
            "CELA_INDIGO_ENABLE": "1",
        },
        root=ROOT,
        strict_production=True,
    )
    assert strict_report["ready"] is False
    assert [e["id"] for e in strict_report["errors"]] == ["indigo_api_key_unset"]

    ok_report = check_deployment_readiness(
        {"CELA_INDIGO_ENABLE": "1", "INDIGO_API_KEY": TEST_INDIGO_KEY}, root=ROOT
    )
    assert ok_report["ready"] is True
    assert ok_report["warnings"] == []
    assert TEST_INDIGO_KEY not in json.dumps(ok_report)


def test_verify_indigo_script_offline_mode_binds_sha_and_passes() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/verify_indigo_api.py", "--offline-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "commit sha: " in proc.stdout
    assert "[PASS] Core C5/C6 startup stays independent of INDIGO_API_KEY" in proc.stdout
    assert "[PASS] Missing INDIGO_API_KEY fails closed before opening any socket" in proc.stdout
    assert "[PASS] Credential redaction strips AI Indigo keys and active secrets" in proc.stdout


def test_browser_fallback_recovers_from_cloudflare_waf_challenge() -> None:
    seen_browser_calls: list[tuple[str, dict[str, str]]] = []

    async def fake_browser_fetcher(url: str, headers: dict[str, str]) -> RawIndigoResponse:
        seen_browser_calls.append((url, headers))
        return RawIndigoResponse(
            status_code=200,
            headers=httpx.Headers(
                {"content-type": "application/json", "X-RateLimit-Remaining": "99"}
            ),
            text=json.dumps({"data": {"slug": "waf-challenge", "name": "Recovered"}}),
            transport_used="chromium",
        )

    transport = CountingTransport(indigo_stub)
    service = IndigoService(
        enabled=True,
        policy=default_indigo_policy(),
        transport=transport,
        browser_fetcher=fake_browser_fetcher,
        env={"INDIGO_API_KEY": TEST_INDIGO_KEY},
    )
    app = create_app(
        api_token=PLATFORM_TOKEN,
        private_api_token=PRIVATE_TOKEN,
        indigo_service=service,
    )
    with TestClient(app) as client:
        ok = client.get(
            f"{INDIGO_API_PREFIX}/tools/waf-challenge",
            headers=_auth(PLATFORM_TOKEN),
        )
        assert ok.status_code == 200
        assert ok.json()["upstream"]["data"]["name"] == "Recovered"
        assert ok.json()["rate_limit"]["remaining"] == 99
    assert len(seen_browser_calls) == 1
    assert seen_browser_calls[0][1] == {"X-API-Key": TEST_INDIGO_KEY}
