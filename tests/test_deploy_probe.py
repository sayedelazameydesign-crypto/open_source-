"""The post-deploy probe must be able to prove — and disprove — a deployment.

The runbooks promise a published service answers ``GET /health`` with
``{"status":"ok"}`` and answers ``GET /runtime-info`` with the identity of its
executor (``execution.kind == "scripted"`` in preview mode, ``"provider_adapter"``
once the live provider is enabled). These tests run that exact check against a
real HTTP server built from the real application, so a passing run is evidence
about the deployed surface and not about a mock.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from os_server.app import create_app
from os_server.demo import build_demo_platform_for
from os_server.deploy_probe import (
    DEFAULT_VERIFY_ALLOWED_HOSTS,
    ProbeConfigurationError,
    authorize_probe_destination,
    normalize_base_url,
    verify_published_service,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_deployment.py"
TOKEN = "probe-test-token"


@contextmanager
def _serving(app) -> Iterator[str]:
    """Serve *app* on a loopback port for the duration of the block."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        raise RuntimeError("the probe test server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()


@pytest.fixture
def preview_service() -> Iterator[str]:
    """A published service in preview mode: the scripted executor."""
    app = create_app(lambda m, n: build_demo_platform_for(m, max_steps=n), api_token=TOKEN)
    with _serving(app) as url:
        yield url
    app.state.runtime.close()


def _run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    child_env = dict(os.environ)
    child_env.pop("CELA_API_TOKEN", None)
    child_env.pop("CELA_API_URL", None)
    child_env.pop("CELA_BACKEND_URL", None)
    child_env.update(env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=child_env,
    )


def test_probe_passes_against_a_published_preview_service(preview_service: str) -> None:
    report = verify_published_service(
        preview_service, TOKEN, expected_kind="scripted", allow_loopback=True
    )

    assert report.ok, [result.detail for result in report.failures]
    names = [result.name for result in report.results]
    assert names == [
        'health answers {"status":"ok"} without a token',
        "runtime-info requires the platform token",
        'runtime-info reports execution.kind == "scripted"',
        "no response echoes the API token",
    ]
    identity = next(r for r in report.results if "execution.kind" in r.name)
    assert "execution.kind=scripted" in identity.detail
    assert "goal_verification=not_verified" in identity.detail


def test_probe_detects_a_live_provider_where_preview_was_promised(preview_service: str) -> None:
    """A preview deployment that suddenly answers "provider_adapter" is a failure."""
    report = verify_published_service(
        preview_service, TOKEN, expected_kind="provider_adapter", allow_loopback=True
    )

    assert not report.ok
    failure = next(r for r in report.failures if "execution.kind" in r.name)
    assert "expected 'provider_adapter'" in failure.detail
    assert "scripted fallback" in failure.detail


def test_probe_reports_an_unreachable_service_instead_of_raising() -> None:
    # Port 1 on loopback: nothing listens there, so every request fails.
    report = verify_published_service("http://127.0.0.1:1", TOKEN, timeout=2.0, allow_loopback=True)

    assert not report.ok
    network_checks = [r for r in report.results if r.name != "no response echoes the API token"]
    assert all("request failed" in result.detail for result in network_checks)


def test_probe_refuses_loopback_http_without_explicit_test_opt_in() -> None:
    with pytest.raises(ProbeConfigurationError, match="requires HTTPS"):
        verify_published_service("http://127.0.0.1:1", TOKEN)


def test_probe_refuses_to_send_the_token_over_cleartext_http() -> None:
    with pytest.raises(ProbeConfigurationError) as excinfo:
        verify_published_service("http://celia-api.onrender.com", TOKEN)

    assert "https" in str(excinfo.value)


def test_probe_default_allowlist_rejects_other_service_hosts_before_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden_dns(host: str, *args, **kwargs):
        calls.append(host)
        raise AssertionError("a non-allowlisted host must not even be resolved")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    assert DEFAULT_VERIFY_ALLOWED_HOSTS == ("*.onrender.com",)
    with pytest.raises(ProbeConfigurationError, match="egress policy"):
        verify_published_service("https://api.example.com", TOKEN)
    assert calls == []


def test_probe_allows_an_operator_supplied_api_host_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    verdict = authorize_probe_destination(
        "https://api.example.com", allowed_hosts=("*.example.com",)
    )
    assert verdict.allowed
    assert verdict.connect_ip == "93.184.216.34"


@pytest.mark.parametrize(
    "location",
    [
        "http://api.onrender.com/health",  # scheme downgrade
        "https://api.onrender.com:8443/health",  # port change
    ],
)
def test_probe_checks_health_first_without_token_and_does_not_follow_redirects(
    monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization")
        seen.append((request.url.path, auth))
        if request.url.path == "/health":
            return httpx.Response(
                307,
                headers={"Location": location},
                request=request,
            )
        if auth is None:
            return httpx.Response(401, request=request)
        return httpx.Response(
            200,
            json={
                "execution": {
                    "kind": "scripted",
                    "goal_verification": "not_verified",
                    "model_name": "scripted-demo",
                }
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        report = verify_published_service("https://api.onrender.com", TOKEN, client=client)

    assert not report.ok  # a 307 is evidence of an invalid health endpoint
    assert seen == [
        ("/health", None),
        ("/runtime-info", None),
        ("/runtime-info", f"Bearer {TOKEN}"),
    ]
    assert report.results[1].ok and report.results[2].ok
    assert all(path != "/health" or auth is None for path, auth in seen)


def test_probe_redacts_token_echoes_and_encoded_forms_from_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.parse import quote

    token = "probe/token+secret"
    encoded_token = quote(token, safe="").lower()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200, json={"status": "leak", "detail": encoded_token}, request=request
            )
        if request.headers.get("Authorization") is None:
            return httpx.Response(401, request=request)
        return httpx.Response(
            200,
            json={
                "execution": {
                    "kind": "scripted",
                    "goal_verification": "not_verified",
                    "model_name": token,
                }
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        report = verify_published_service("https://api.onrender.com", token, client=client)

    report_text = json.dumps(report.as_dict())
    assert token not in report_text
    assert encoded_token not in report_text
    assert "[REDACTED]" in report_text
    assert not report.ok
    assert not report.results[-1].ok


def test_probe_redacts_token_from_http_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "probe-test-token"
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"}, request=request)
        if request.headers.get("Authorization") is None:
            return httpx.Response(401, request=request)
        raise httpx.ConnectError(f"simulated failure included {token}", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        report = verify_published_service("https://api.onrender.com", token, client=client)

    serialized = json.dumps(report.as_dict())
    assert token not in serialized
    assert "[REDACTED]" in serialized


def test_probe_rejects_an_injected_client_that_follows_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)), follow_redirects=True
    )
    try:
        with pytest.raises(ProbeConfigurationError, match="must not follow redirects"):
            verify_published_service("https://api.onrender.com", TOKEN, client=client)
    finally:
        client.close()


def test_probe_refuses_nonstandard_https_ports_before_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("must reject the port before DNS"),
    )
    with pytest.raises(ProbeConfigurationError, match="port 443"):
        verify_published_service("https://celia-api.onrender.com:8443", TOKEN)


def test_probe_requires_a_token() -> None:
    with pytest.raises(ProbeConfigurationError):
        verify_published_service("https://celia-api.onrender.com", "")


def test_probe_rejects_an_unknown_expectation() -> None:
    with pytest.raises(ProbeConfigurationError):
        verify_published_service("https://celia-api.onrender.com", TOKEN, expected_kind="gpt")


def test_probe_redacts_token_from_invalid_expectation_error() -> None:
    token = "secret-probe-token"
    with pytest.raises(ProbeConfigurationError) as excinfo:
        verify_published_service("https://celia-api.onrender.com", token, expected_kind=token)

    assert token not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)


def test_normalize_base_url_keeps_loopback_http_and_drops_the_trailing_slash() -> None:
    assert normalize_base_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    assert normalize_base_url("https://celia-api.onrender.com/") == "https://celia-api.onrender.com"
    with pytest.raises(ProbeConfigurationError):
        normalize_base_url("")
    for suffix in ("?token=not-a-base-url", "#runtime-info"):
        with pytest.raises(ProbeConfigurationError, match="query or fragment"):
            normalize_base_url(f"https://celia-api.onrender.com{suffix}")


def test_the_report_never_carries_the_token(preview_service: str) -> None:
    report = verify_published_service(
        preview_service, TOKEN, expected_kind="scripted", allow_loopback=True
    )

    assert TOKEN not in json.dumps(report.as_dict())


def test_probe_ignores_proxy_environment(
    preview_service: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    report = verify_published_service(preview_service, TOKEN, allow_loopback=True)

    assert report.ok, [result.detail for result in report.failures]


def test_cli_passes_against_a_published_preview_service(preview_service: str) -> None:
    proc = _run_cli(
        "--api",
        preview_service,
        "--allow-loopback",
        "--expect-kind",
        "scripted",
        env={"CELA_API_TOKEN": TOKEN},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS" in proc.stdout
    assert "execution.kind=scripted" in proc.stdout
    assert TOKEN not in proc.stdout
    assert TOKEN not in proc.stderr
    # The evidence names the tree it belongs to.
    assert "commit  :" in proc.stdout


def test_cli_fails_when_the_deployment_does_not_match_the_promise(preview_service: str) -> None:
    proc = _run_cli(
        "--api",
        preview_service,
        "--allow-loopback",
        "--expect-kind",
        "provider_adapter",
        env={"CELA_API_TOKEN": TOKEN},
    )

    assert proc.returncode == 1
    assert "FAIL" in proc.stdout
    assert TOKEN not in proc.stdout


def test_cli_reports_the_report_as_json(preview_service: str) -> None:
    proc = _run_cli(
        "--api", preview_service, "--allow-loopback", "--json", env={"CELA_API_TOKEN": TOKEN}
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["expected_kind"] == "scripted"
    assert TOKEN not in proc.stdout


def test_cli_exits_two_without_a_token(preview_service: str) -> None:
    proc = _run_cli("--api", preview_service)

    assert proc.returncode == 2
    assert "no API token" in proc.stderr


def test_cli_exits_two_without_a_service_url() -> None:
    proc = _run_cli(env={"CELA_API_TOKEN": TOKEN})

    assert proc.returncode == 2
    assert "no service URL" in proc.stderr


def test_cli_refuses_cleartext_http_for_a_remote_host() -> None:
    proc = _run_cli(
        "--api",
        "http://celia-api.onrender.com",
        env={"CELA_API_TOKEN": TOKEN},
    )

    assert proc.returncode == 2
    assert "https" in proc.stderr
    assert TOKEN not in proc.stderr
