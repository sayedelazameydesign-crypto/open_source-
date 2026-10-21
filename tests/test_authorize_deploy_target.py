"""The deploy target must be authorized before any request leaves the runner.

``scripts/authorize_deploy_target.py`` is the first step of the CI gate. It is
deliberately offline — it decides whether the probe may resolve a host, not
where that host points — so every rule can be tested without a network.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "authorize_deploy_target.py"

_spec = importlib.util.spec_from_file_location("authorize_deploy_target", SCRIPT)
module = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(module)

TargetError = module.TargetError
authorize_target = module.authorize_target
parse_allowlist = module.parse_allowlist


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_accepts_a_provider_host_covered_by_a_wildcard() -> None:
    result = authorize_target("https://celia.clawcloudrun.com", "*.clawcloudrun.com")

    assert result == {
        "url": "https://celia.clawcloudrun.com",
        "host": "celia.clawcloudrun.com",
        "allowed_host": "*.clawcloudrun.com",
        "allow_hosts": ["*.clawcloudrun.com"],
    }


def test_accepts_an_exact_host_and_normalizes_case_and_trailing_slash() -> None:
    result = authorize_target("https://API.Example.APP/", "api.example.app, *.railway.app")

    assert result["url"] == "https://api.example.app"
    assert result["allowed_host"] == "api.example.app"
    assert result["allow_hosts"] == ["api.example.app", "*.railway.app"]


def test_wildcard_does_not_cover_the_apex_or_a_lookalike_suffix() -> None:
    for url in ("https://example.app", "https://api.example.app.evil.test"):
        with pytest.raises(TargetError, match="not covered"):
            authorize_target(url, "*.example.app")


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("", "empty"),
        ("http://api.example.app", "https"),
        ("https://user:pass@api.example.app", "credentials"),
        ("https://api.example.app:8443", "443"),
        ("https://api.example.app/health", "no path"),
        ("https://api.example.app/?token=x", "query or fragment"),
        ("https://127.0.0.1", "IP literal"),
        ("https://localhost", "loopback"),
        ("https://api.example.app extra", "whitespace"),
    ],
)
def test_refuses_a_target_the_probe_must_not_touch(url: str, message: str) -> None:
    with pytest.raises(TargetError, match=message):
        authorize_target(url, "*.example.app")


@pytest.mark.parametrize(
    "allowlist",
    ["", "   ", "api.*.example.app", "a..example.app", "https://api.example.app", "*"],
)
def test_refuses_a_malformed_allowlist(allowlist: str) -> None:
    with pytest.raises(TargetError):
        parse_allowlist(allowlist)


def test_allowlist_is_deduplicated_in_order() -> None:
    assert parse_allowlist(" *.Example.app ,api.example.app,*.example.app ") == (
        "*.example.app",
        "api.example.app",
    )


def test_env_format_emits_only_the_url_the_workflow_reads() -> None:
    proc = _run(
        "--url",
        "https://celia.example.app",
        "--allowed-hosts",
        "*.example.app",
        "--format",
        "env",
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "API_URL=https://celia.example.app\n"
    assert "DEPLOY_ALLOWED_HOSTS" not in proc.stdout, (
        "the allowlist must stay the validated repository variable, not a rewritten copy"
    )


def test_json_format_is_machine_readable() -> None:
    proc = _run("--url", "https://celia.example.app", "--allowed-hosts", "*.example.app")

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["allowed_host"] == "*.example.app"


def test_cli_refuses_with_exit_2_and_an_annotation() -> None:
    proc = _run("--url", "https://celia.other.test", "--allowed-hosts", "*.example.app")

    assert proc.returncode == 2
    assert "::error title=Deploy target refused::" in proc.stdout
    assert "not covered by the allowlist" in proc.stderr
