"""`cela reach …` — the operator's front door (ADR-0013 phase 3).

Two things are being pinned here: that a host-side capability is never granted
without an explicit, confirmed act, and that the happy path really works — the
`run` test executes a stand-in binary through the whole adapter, so it proves
the argv/env/timeout plumbing rather than mocking it away.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from os_server import cli as platform_cli

REACH_ENV_VARS = (
    "CELA_REACH_CLI_ENABLE",
    "CELA_REACH_CLI_ALLOW_AUTH",
    "CELA_REACH_CONFIG",
    "CELA_REACH_AUDIT_LOG",
    "CELA_REACH_ALLOW_HOSTS",
    "CELA_REACH_DENY_HOSTS",
    "CELA_SANDBOXED",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path) -> Path:
    """No test may read the machine's real reach config, env or audit log."""
    for name in REACH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "reach.toml"
    monkeypatch.setenv("CELA_REACH_CONFIG", str(config))
    return config


@pytest.fixture
def fake_binary(monkeypatch, tmp_path) -> Path:
    """A stand-in `gh` on PATH: proves execution without touching the network."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "gh"
    script.write_text('#!/bin/sh\necho "args: $@"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return script


def run_cli(argv: list[str], capsys) -> tuple[int, str, str]:
    code = platform_cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ----------------------------------------------------------------------- list


def test_list_shows_both_tiers_and_the_bypass_warning(capsys) -> None:
    code, out, _err = run_cli(["reach", "list"], capsys)
    assert code == 0
    assert "reach report (native)" in out
    assert "github_search" in out and "bilibili_search" in out
    assert "verified commands: 6 of 16 upstream channels" in out
    assert "NOT bound by the egress policy" in out


def test_list_never_runs_the_upstream_doctor(capsys) -> None:
    """`list` is a status command: it must not execute a host program."""
    code, out, _err = run_cli(["reach", "list"], capsys)
    assert code == 0
    assert "agent-reach" not in out


def test_list_reports_a_broken_config_instead_of_hiding_it(capsys, clean_env: Path) -> None:
    clean_env.write_text("[cli_channels\n", encoding="utf-8")
    code, out, _err = run_cli(["reach", "list"], capsys)
    assert code == 0
    assert "TOMLDecodeError" in out
    assert "nothing enabled" in out


def test_list_json_is_machine_readable(capsys, clean_env: Path) -> None:
    clean_env.write_text('[cli_channels]\nenabled = ["github_search"]\n', encoding="utf-8")
    code, out, _err = run_cli(["reach", "list", "--json"], capsys)
    payload = json.loads(out)
    assert code == 0
    assert payload["coverage"]["enabled"] == ["github_search"]
    assert set(payload["native"]["channels"]) == {"github", "rss", "web"}


# --------------------------------------------------------------------- enable


def test_enable_writes_the_config_owner_only(capsys, clean_env: Path) -> None:
    code, out, _err = run_cli(["reach", "enable", "github_search", "--yes"], capsys)
    assert code == 0
    assert "enabled: github_search" in out
    body = clean_env.read_text(encoding="utf-8")
    assert "[cli_channels]" in body and 'enabled = ["github_search"]' in body
    assert "EgressPolicy" in body  # the file itself carries the warning
    if os.name != "nt":
        assert stat.S_IMODE(clean_env.stat().st_mode) == 0o600


def test_enable_without_a_tty_and_without_yes_changes_nothing(capsys, clean_env: Path) -> None:
    """The default answer to "may I grant a host capability" is no."""
    code, _out, err = run_cli(["reach", "enable", "github_search"], capsys)
    assert code == 2
    assert "--yes" in err
    assert not clean_env.exists()


def test_enable_asks_and_honours_a_no(monkeypatch, capsys, clean_env: Path) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    code, _out, err = run_cli(["reach", "enable", "github_search"], capsys)
    assert code == 2
    assert "aborted" in err
    assert not clean_env.exists()


def test_enable_asks_and_honours_a_yes(monkeypatch, capsys, clean_env: Path) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    code, out, _err = run_cli(["reach", "enable", "github_search"], capsys)
    assert code == 0
    assert "enabled: github_search" in out
    assert clean_env.exists()


def test_enabling_a_session_channel_says_what_it_costs(capsys, clean_env: Path) -> None:
    code, out, _err = run_cli(["reach", "enable", "twitter_search", "--yes"], capsys)
    assert code == 0
    assert "YOUR session" in out and "--allow-auth" in out
    assert "allow_auth = false" in clean_env.read_text(encoding="utf-8")


def test_allow_auth_is_recorded_only_when_asked(capsys, clean_env: Path) -> None:
    run_cli(["reach", "enable", "twitter_search", "--yes", "--allow-auth"], capsys)
    assert "allow_auth = true" in clean_env.read_text(encoding="utf-8")


def test_an_unknown_channel_is_a_usage_error(capsys, clean_env: Path) -> None:
    code, _out, err = run_cli(["reach", "enable", "nope", "--yes"], capsys)
    assert code == 2
    assert "unknown channel" in err
    assert not clean_env.exists()


# ------------------------------------------------------------------- doctor


def test_doctor_reports_the_upstream_project_without_running_it(capsys) -> None:
    code, out, _err = run_cli(["reach", "doctor"], capsys)
    assert code == 0
    assert "agent_reach: off" in out  # not installed here, and stated plainly


def test_doctor_json_carries_the_same_three_sections(capsys) -> None:
    code, out, _err = run_cli(["reach", "doctor", "--json"], capsys)
    payload = json.loads(out)
    assert code == 0
    assert set(payload) == {"native", "cli", "upstream", "coverage"}


# ---------------------------------------------------------------------- run


def test_run_refuses_without_yes_and_runs_nothing(capsys, clean_env: Path) -> None:
    code, out, err = run_cli(["reach", "run", "github_search", "agents"], capsys)
    assert code == 2
    assert "NOT bound by the egress policy" in out  # the warning comes first
    assert "aborted" in err


def test_run_on_a_disabled_channel_says_which_switch_is_missing(capsys) -> None:
    code, _out, err = run_cli(["reach", "run", "github_search", "agents", "--yes"], capsys)
    assert code == 1
    assert "CLI_CHANNEL_DISABLED" in err
    assert "CELA_REACH_CLI_ENABLE" in err


def test_run_executes_an_enabled_channel_end_to_end(
    capsys, clean_env: Path, fake_binary: Path
) -> None:
    clean_env.write_text('[cli_channels]\nenabled = ["github_search"]\n', encoding="utf-8")
    code, out, _err = run_cli(["reach", "run", "github_search", "agents", "--yes"], capsys)
    assert code == 0
    assert "args: search repos agents --limit 10" in out  # the argv it really built
    assert "── channel: github_search  exit: 0" in out


def test_run_writes_the_audit_trail_it_promises(
    capsys, clean_env: Path, fake_binary: Path, tmp_path
) -> None:
    audit = tmp_path / "audit.jsonl"
    os.environ["CELA_REACH_AUDIT_LOG"] = str(audit)
    try:
        clean_env.write_text('[cli_channels]\nenabled = ["github_search"]\n', encoding="utf-8")
        code, _out, _err = run_cli(
            ["reach", "run", "github_search", "secret query", "--yes"], capsys
        )
    finally:
        os.environ.pop("CELA_REACH_AUDIT_LOG", None)
    assert code == 0
    lines = audit.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["channel"] == "github_search" and record["decision"] == "allowed"
    assert "secret query" not in lines[0]
    assert record["correlation_id"].startswith("cli-")
