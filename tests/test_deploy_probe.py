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

import pytest
import uvicorn
from os_server.app import create_app
from os_server.demo import build_demo_platform_for
from os_server.deploy_probe import (
    ProbeConfigurationError,
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
    report = verify_published_service(preview_service, TOKEN, expected_kind="scripted")

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
    report = verify_published_service(preview_service, TOKEN, expected_kind="provider_adapter")

    assert not report.ok
    failure = next(r for r in report.failures if "execution.kind" in r.name)
    assert "expected 'provider_adapter'" in failure.detail
    assert "scripted fallback" in failure.detail


def test_probe_reports_an_unreachable_service_instead_of_raising() -> None:
    # Port 1 on loopback: nothing listens there, so every request fails.
    report = verify_published_service("http://127.0.0.1:1", TOKEN, timeout=2.0)

    assert not report.ok
    network_checks = [r for r in report.results if r.name != "no response echoes the API token"]
    assert all("request failed" in result.detail for result in network_checks)


def test_probe_refuses_to_send_the_token_over_cleartext_http() -> None:
    with pytest.raises(ProbeConfigurationError) as excinfo:
        verify_published_service("http://celia-api.onrender.com", TOKEN)

    assert "https" in str(excinfo.value)


def test_probe_requires_a_token() -> None:
    with pytest.raises(ProbeConfigurationError):
        verify_published_service("https://celia-api.onrender.com", "")


def test_probe_rejects_an_unknown_expectation() -> None:
    with pytest.raises(ProbeConfigurationError):
        verify_published_service("https://celia-api.onrender.com", TOKEN, expected_kind="gpt")


def test_normalize_base_url_keeps_loopback_http_and_drops_the_trailing_slash() -> None:
    assert normalize_base_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    assert normalize_base_url("https://celia-api.onrender.com/") == "https://celia-api.onrender.com"
    with pytest.raises(ProbeConfigurationError):
        normalize_base_url("")


def test_the_report_never_carries_the_token(preview_service: str) -> None:
    report = verify_published_service(preview_service, TOKEN, expected_kind="scripted")

    assert TOKEN not in json.dumps(report.as_dict())


def test_cli_passes_against_a_published_preview_service(preview_service: str) -> None:
    proc = _run_cli(
        "--api",
        preview_service,
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
        "--expect-kind",
        "provider_adapter",
        env={"CELA_API_TOKEN": TOKEN},
    )

    assert proc.returncode == 1
    assert "FAIL" in proc.stdout
    assert TOKEN not in proc.stdout


def test_cli_reports_the_report_as_json(preview_service: str) -> None:
    proc = _run_cli("--api", preview_service, "--json", env={"CELA_API_TOKEN": TOKEN})

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
