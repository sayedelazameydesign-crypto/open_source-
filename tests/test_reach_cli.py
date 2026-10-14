"""CLI channel adapter (ADR-0013 phase 2): gates, argv safety, caps, audit.

No process is ever spawned here: the adapter takes an injectable ``runner``
exactly like the native channels take an httpx transport, so "it refused" and
"it never executed" are both *provable* rather than assumed — every refusal test
also asserts that the runner saw zero calls.
"""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest
from os_core.types import RiskLevel, ToolCall, ToolStatus
from os_reach.adapters.cli import (
    CLI_ARGUMENT_REJECTED,
    CLI_CHANNEL_AUTH_NOT_ACKNOWLEDGED,
    CLI_CHANNEL_NOT_IMPLEMENTED,
    CLI_CHANNEL_UNKNOWN,
    CLI_NONZERO_EXIT,
    CLI_RUNNER_ERROR,
    CLI_TIMEOUT,
    SANDBOX_MARKER_ENV,
)
from os_tools.registry import ToolRegistry

from os_reach import (
    CLI_CHANNEL_DISABLED,
    CLI_CHANNEL_SANDBOXED,
    CLI_CHANNEL_SPECS,
    KNOWN_UPSTREAM_CHANNELS,
    AgentReachBridge,
    CLIChannelAdapter,
    CLILayer,
    CLIProcessResult,
    ReachAuditEntry,
    ReachAuditLog,
    cli_reach_tools,
    query_fingerprint,
)


class FakeRunner:
    """Records every invocation; returns (or raises) whatever the test configured."""

    def __init__(self, result: CLIProcessResult | None = None, raises: Exception | None = None):
        self.result = result or CLIProcessResult(exit_code=0, stdout="ok")
        self.raises = raises
        self.calls: list[dict] = []

    async def __call__(self, argv, env, timeout_s, max_bytes):
        self.calls.append(
            {"argv": list(argv), "env": dict(env), "timeout": timeout_s, "max_bytes": max_bytes}
        )
        if self.raises is not None:
            raise self.raises
        return self.result

    @property
    def argv(self) -> list[str]:
        return self.calls[-1]["argv"]


@pytest.fixture
def binary_present(monkeypatch):
    """Every probed binary appears to be installed."""
    monkeypatch.setattr(shutil, "which", lambda binary: f"/usr/bin/{binary}")
    return True


@pytest.fixture
def binary_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda binary: None)
    return True


def adapter_for(name: str = "github_search", **kwargs) -> CLIChannelAdapter:
    return CLIChannelAdapter(spec=CLI_CHANNEL_SPECS[name], **kwargs)


#: Never points at a real file: the suite must not read a developer's own
#: ~/.config/cela/reach.toml — or the machine running CI would change the results.
NO_CONFIG = Path("/nonexistent-cela-reach/reach.toml")


def layer(enable: str = "", runner=None, **kwargs) -> CLILayer:
    return CLILayer.from_env(
        env={"CELA_REACH_CLI_ENABLE": enable} if enable is not None else {},
        runner=runner,
        config_path=NO_CONFIG,
        **kwargs,
    )


# ------------------------------------------------------------------ enablement


async def test_a_disabled_channel_is_refused_and_never_executes() -> None:
    runner = FakeRunner()
    adapter = adapter_for(enabled=False, runner=runner)
    result = await adapter.run("agents")
    assert result.ok is False
    assert result.code == CLI_CHANNEL_DISABLED
    assert "CELA_REACH_CLI_ENABLE" in result.error
    assert runner.calls == []


def test_layer_from_env_enables_only_what_is_named() -> None:
    built = layer("github_search,bilibili_search")
    assert built.enabled_channels() == ["github_search", "bilibili_search"]
    assert built.get("twitter_search").enabled is False
    assert built.is_active() is True


def test_layer_is_off_without_the_switch() -> None:
    built = layer("")
    assert built.enabled_channels() == []
    assert built.is_active() is False
    assert all(not a.enabled for a in built.adapters.values())


def test_star_enables_every_implemented_channel() -> None:
    built = layer("*")
    assert sorted(built.enabled_channels()) == sorted(CLI_CHANNEL_SPECS)


def test_whitespace_and_empty_entries_are_ignored() -> None:
    built = layer(" github_search , , ")
    assert built.enabled_channels() == ["github_search"]


async def test_enabling_is_not_enough_for_a_session_borrowing_channel() -> None:
    """Twitter runs with the operator's identity: that is a second decision."""
    runner = FakeRunner(result=CLIProcessResult(exit_code=0, stdout="tweets"))
    built = CLILayer.from_env(
        env={"CELA_REACH_CLI_ENABLE": "twitter_search"}, runner=runner, config_path=NO_CONFIG
    )
    result = await built.run("twitter_search", "opensource")
    assert result.ok is False
    assert result.code == CLI_CHANNEL_AUTH_NOT_ACKNOWLEDGED
    assert "CELA_REACH_CLI_ALLOW_AUTH" in result.error
    assert runner.calls == []

    acknowledged = CLILayer.from_env(
        env={"CELA_REACH_CLI_ENABLE": "twitter_search", "CELA_REACH_CLI_ALLOW_AUTH": "1"},
        runner=runner,
        config_path=NO_CONFIG,
    )
    assert acknowledged.get("twitter_search").auth_acknowledged is True
    assert (await acknowledged.run("twitter_search", "opensource")).ok is True
    assert len(runner.calls) == 1


async def test_sandboxed_code_cannot_borrow_a_host_capability(monkeypatch) -> None:
    """The sandbox marks its children; the adapter refuses to run there."""
    monkeypatch.setenv(SANDBOX_MARKER_ENV, "1")
    runner = FakeRunner()
    adapter = adapter_for(enabled=True, runner=runner)
    result = await adapter.run("agents")
    assert result.ok is False
    assert result.code == CLI_CHANNEL_SANDBOXED
    assert runner.calls == []
    assert adapter.audit.entries[-1].decision == "denied"


def test_the_sandbox_actually_sets_that_marker() -> None:
    """Otherwise the refusal above is theatre: check the producer, not the consumer."""
    from os_tools.code_exec import _sandbox_env

    assert _sandbox_env("print(1)")[SANDBOX_MARKER_ENV] == "1"


async def test_unknown_and_unimplemented_channels_say_which_they_are() -> None:
    built = layer("*", runner=FakeRunner())
    unknown = await built.run("not_a_channel", "x")
    assert unknown.code == CLI_CHANNEL_UNKNOWN
    assert "github_search" in unknown.error

    assert "boss" in KNOWN_UPSTREAM_CHANNELS  # upstream supports it, we have no command
    unimplemented = await built.run("boss", "engineer")
    assert unimplemented.code == CLI_CHANNEL_NOT_IMPLEMENTED
    assert "REACH_PLAN" in unimplemented.error


def test_coverage_is_reported_honestly() -> None:
    coverage = layer("").coverage()
    assert coverage["verified"] == len(CLI_CHANNEL_SPECS)
    assert coverage["upstream"] == 16
    assert coverage["verified"] < coverage["upstream"]


# ------------------------------------------------------------------------ argv


@pytest.mark.parametrize(
    ("name", "query", "expected"),
    [
        ("github_search", "agents", ["gh", "search", "repos", "agents", "--limit", "10"]),
        ("bilibili_search", "Rust", ["bili", "search", "Rust", "--type", "video", "-n", "5"]),
        ("twitter_search", "llm", ["twitter", "search", "llm", "-n", "10"]),
        ("reddit_search", "bug", ["rdt", "search", "bug", "--limit", "10"]),
        (
            "exa_search",
            "vector db",
            ["mcporter", "call", "exa.web_search_exa", "query=vector db", "numResults=5"],
        ),
        (
            "youtube_metadata",
            "https://www.youtube.com/watch?v=abc",
            [
                "yt-dlp",
                "--dump-single-json",
                "--skip-download",
                "--no-playlist",
                "https://www.youtube.com/watch?v=abc",
            ],
        ),
    ],
)
def test_argv_is_the_documented_command(name: str, query: str, expected: list[str]) -> None:
    assert adapter_for(name).argv_for(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "agents; rm -rf /",
        "agents | cat /etc/passwd",
        "agents $(whoami)",
        "agents `id`",
        "agents && curl http://evil.test",
        "C++ \"quoted\" and 'single'",
        "agents > /tmp/x",
    ],
)
def test_shell_metacharacters_are_inert_arguments_not_commands(query: str) -> None:
    """No shell is involved, so the payload travels as ONE argv element.

    Blacklisting ``;`` or ``|`` would reject legitimate searches like
    ``C++ | Rust`` while proving nothing about execution: with
    ``create_subprocess_exec`` there is no second command to inject into. The
    test therefore asserts the strong property — the payload is a single
    argument — not a blocklist's absence.
    """
    argv = adapter_for().argv_for(query)
    assert argv.count(query) == 1
    assert argv[3] == query
    assert len(argv) == 6  # gh search repos <query> --limit 10


@pytest.mark.parametrize(
    "query",
    [
        "--json",
        "-n 100",
        "-",
        "",
        "   ",
        "agents\nrm -rf /",
        "agents\x00hidden",
        "x" * 2_001,
    ],
)
def test_rejected_queries_never_reach_the_runner(query: str) -> None:
    with pytest.raises(ValueError):
        adapter_for().argv_for(query)


@pytest.mark.parametrize(
    ("name", "query"),
    [
        ("github_search", "--config=/etc/passwd"),
        ("bilibili_search", "-o"),
        ("exa_search", "--token x"),
    ],
)
async def test_option_injection_is_denied_and_audited(name: str, query: str) -> None:
    runner = FakeRunner()
    adapter = adapter_for(name, enabled=True, runner=runner)
    result = await adapter.run(query)
    assert result.code == CLI_ARGUMENT_REJECTED
    assert runner.calls == []
    entry = adapter.audit.entries[-1]
    assert entry.decision == "denied" and entry.code == CLI_ARGUMENT_REJECTED
    assert query not in entry.to_jsonl()  # the rejected text is not logged either


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "javascript:alert(1)", "https://user:pw@example.org/v", "not-a-url"],
)
async def test_url_channels_validate_their_target(url: str) -> None:
    adapter = adapter_for("youtube_metadata", enabled=True, runner=FakeRunner())
    assert (await adapter.run(url)).code == CLI_ARGUMENT_REJECTED


# ------------------------------------------------------------------- execution


async def test_a_successful_run_returns_text_and_audits_the_call() -> None:
    runner = FakeRunner(CLIProcessResult(exit_code=0, stdout="found 3 repos", bytes_read=13))
    adapter = adapter_for(enabled=True, runner=runner)
    result = await adapter.run("agents", correlation_id="run-42")
    assert result.ok is True and result.text == "found 3 repos"
    assert result.bytes_out == 13 and result.correlation_id == "run-42"
    assert runner.calls[0]["timeout"] == adapter.timeout
    assert runner.calls[0]["max_bytes"] == adapter.max_bytes

    entry = adapter.audit.entries[-1]
    assert entry.decision == "allowed" and entry.exit_code == 0
    assert entry.query_chars == len("agents")
    assert entry.query_sha256 == query_fingerprint("agents")[0]
    assert "agents" not in entry.to_jsonl()  # the query itself is never stored
    assert entry.correlation_id == "run-42"
    assert entry.channel == "github_search" and entry.tool == "reach_cli"


async def test_a_nonzero_exit_is_a_failure_with_the_scrubbed_stderr_tail() -> None:
    runner = FakeRunner(
        CLIProcessResult(
            exit_code=1, stderr="HTTP 403 calling https://api.github.com/search?token=SECRET-TOKEN"
        )
    )
    adapter = adapter_for(enabled=True, runner=runner)
    result = await adapter.run("agents")
    assert result.ok is False
    assert result.code == CLI_NONZERO_EXIT
    assert "SECRET-TOKEN" not in result.error  # scrubbed on the way out
    assert "token=REDACTED" in result.error
    assert adapter.audit.entries[-1].decision == "denied"


async def test_a_timeout_is_reported_as_such() -> None:
    adapter = adapter_for(enabled=True, runner=FakeRunner(CLIProcessResult(timed_out=True)))
    result = await adapter.run("agents")
    assert result.code == CLI_TIMEOUT
    assert "timed out" in result.error


async def test_a_broken_runner_fails_closed_and_is_audited() -> None:
    adapter = adapter_for(enabled=True, runner=FakeRunner(raises=RuntimeError("exec failed")))
    result = await adapter.run("agents")
    assert result.ok is False
    assert result.code == CLI_RUNNER_ERROR
    assert adapter.audit.entries[-1].decision == "denied"


async def test_the_returned_text_is_capped_even_when_the_budget_is_large() -> None:
    runner = FakeRunner(CLIProcessResult(exit_code=0, stdout="x" * 5_000, bytes_read=5_000))
    adapter = adapter_for(enabled=True, runner=runner, max_chars=100)
    result = await adapter.run("agents")
    assert result.ok is True
    assert len(result.text) < 200
    assert "truncated" in result.text
    assert result.bytes_out == 5_000  # the audit records what the process produced


async def test_the_runner_receives_an_allow_listed_environment(monkeypatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_operator-token")
    runner = FakeRunner()
    adapter = adapter_for(enabled=True, runner=runner)
    await adapter.run("agents")
    child_env = runner.calls[0]["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in child_env
    assert child_env["GITHUB_TOKEN"] == "ghp_operator-token"  # the tool's own credential
    assert child_env["PYTHONNOUSERSITE"] == "1"
    assert set(child_env) <= {*adapter.env_allow, *adapter.spec.env_passthrough, "PYTHONNOUSERSITE"}


async def test_json_output_is_parsed_for_the_model() -> None:
    payload = json.dumps({"id": "abc", "title": "Video"})
    adapter = adapter_for(
        "youtube_metadata",
        enabled=True,
        runner=FakeRunner(CLIProcessResult(exit_code=0, stdout=payload)),
    )
    result = await adapter.run("https://www.youtube.com/watch?v=abc")
    assert result.items == [{"id": "abc", "title": "Video"}]


async def test_non_json_output_degrades_to_text_instead_of_failing() -> None:
    adapter = adapter_for(
        "youtube_metadata",
        enabled=True,
        runner=FakeRunner(CLIProcessResult(exit_code=0, stdout="not json")),
    )
    result = await adapter.run("https://www.youtube.com/watch?v=abc")
    assert result.ok is True and result.items == [] and result.text == "not json"


async def test_a_binary_that_vanishes_is_a_clean_failure() -> None:
    """FileNotFoundError from the runner: reported, audited, not a traceback."""
    adapter = adapter_for(enabled=True, runner=FakeRunner(raises=FileNotFoundError("gh")))
    result = await adapter.run("agents")
    assert result.code == CLI_RUNNER_ERROR
    assert "FileNotFoundError" in result.error


# ------------------------------------------------------------------- reporting


def test_probe_says_what_the_channel_is_and_where_it_runs(binary_present) -> None:
    enabled = adapter_for(enabled=True).probe()
    assert enabled.status.value == "ok"
    assert "outside EgressPolicy" in enabled.message  # the bypass is stated, not hidden
    assert "not inside the" in enabled.message

    disabled = adapter_for().probe()
    assert disabled.status.value == "off" and "disabled" in disabled.message

    auth = adapter_for("reddit_search", enabled=True).probe()
    assert auth.status.value == "warn" and "session" in auth.message


def test_probe_marks_a_missing_binary_as_off(binary_missing) -> None:
    health = adapter_for(enabled=True).probe()
    assert health.status.value == "off"
    assert "not on PATH" in health.message


def test_layer_report_covers_every_implemented_channel() -> None:
    report = layer("*").report()
    assert set(report.channels) == set(CLI_CHANNEL_SPECS)
    assert report.source == "cli"


# ----------------------------------------------------------------------- audit


async def test_audit_jsonl_is_written_owner_only(tmp_path: Path) -> None:
    log_path = tmp_path / "nested" / "reach-audit.jsonl"
    audit = ReachAuditLog(path=log_path)
    adapter = adapter_for(
        enabled=True, runner=FakeRunner(CLIProcessResult(exit_code=0, stdout="ok")), audit=audit
    )
    await adapter.run("secret project name")
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["channel"] == "github_search" and record["decision"] == "allowed"
    assert "secret project name" not in lines[0]
    if __import__("os").name != "nt":
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


async def test_the_audit_sink_hook_receives_every_decision_including_denials() -> None:
    seen: list[ReachAuditEntry] = []
    audit = ReachAuditLog(sink=seen.append)
    adapter = adapter_for(enabled=False, runner=FakeRunner(), audit=audit)
    await adapter.run("agents")
    assert seen and seen[-1].decision == "denied" and seen[-1].code == CLI_CHANNEL_DISABLED


async def test_the_sink_is_only_configured_by_the_caller_never_guessed() -> None:
    """No audit file appears unless CELA_REACH_AUDIT_LOG says so."""
    from os_reach.audit import default_audit_path

    assert default_audit_path() is None


async def test_a_raising_sink_does_not_break_the_call() -> None:
    def bad_sink(entry) -> None:
        raise RuntimeError("event bus down")

    audit = ReachAuditLog(sink=bad_sink)
    adapter = adapter_for(enabled=True, runner=FakeRunner(), audit=audit)
    result = await adapter.run("agents")
    assert result.ok is True
    assert "event bus down" in audit.entries[-1].sink_error


def test_audit_summary_counts_decisions() -> None:
    audit = ReachAuditLog()
    audit.record(ReachAuditEntry.for_call(channel="github_search", tool="reach_cli", query="a"))
    denied = ReachAuditEntry.for_call(channel="github_search", tool="reach_cli", query="b")
    denied.decision = "denied"
    audit.record(denied)
    assert audit.summary() == {"allowed": 1, "denied": 1}


def test_audit_ring_is_bounded() -> None:
    audit = ReachAuditLog(max_entries=3)
    for i in range(10):
        audit.record(ReachAuditEntry.for_call(channel="c", tool="t", query=str(i)))
    assert len(audit.entries) == 3


# ---------------------------------------------------------------------- bridge


DOCTOR_PAYLOAD = {
    "web": {
        "status": "ok",
        "name": "any web page",
        "message": "ready",
        "tier": 0,
        "backends": ["Jina Reader"],
        "active_backend": "Jina Reader",
    },
    "github": {
        "status": "off",
        "name": "GitHub",
        "message": "gh missing",
        "tier": 0,
        "backends": ["gh"],
        "active_backend": None,
    },
}


def test_bridge_probe_is_off_until_the_binary_is_installed(binary_missing) -> None:
    health = AgentReachBridge(layer=layer("")).probe()
    assert health.status.value == "off"
    assert "reach" in health.message


def test_bridge_probe_needs_a_switch_even_when_installed(binary_present) -> None:
    installed_only = AgentReachBridge(layer=layer("")).probe()
    assert installed_only.status.value == "warn"  # installed, but not switched on
    assert AgentReachBridge(layer=layer("github_search")).probe().status.value == "ok"


async def test_bridge_doctor_is_gated_by_the_switch(binary_present) -> None:
    runner = FakeRunner()
    bridge = AgentReachBridge(layer=layer(""), runner=runner)
    with pytest.raises(RuntimeError, match="gated"):
        await bridge.doctor()
    assert runner.calls == []  # nothing executed
    assert bridge.layer.audit.entries[-1].decision == "denied"


async def test_bridge_doctor_parses_the_upstream_report(binary_present) -> None:
    runner = FakeRunner(CLIProcessResult(exit_code=0, stdout=json.dumps(DOCTOR_PAYLOAD)))
    built = layer("*", runner=runner)
    bridge = AgentReachBridge(layer=built, runner=runner)
    report = await bridge.doctor(correlation_id="run-9")
    assert runner.argv == ["agent-reach", "doctor", "--json"]
    assert report.channels["web"].status.value == "ok"
    assert report.channels["github"].status.value == "off"
    entry = built.audit.entries[-1]
    assert entry.channel == "agent_reach" and entry.exit_code == 0
    assert entry.correlation_id == "run-9"
    assert "channels off: github" in entry.detail  # the finding stays visible


async def test_bridge_doctor_reports_a_nonzero_exit_instead_of_parsing(binary_present) -> None:
    built = layer("*")
    bridge = AgentReachBridge(
        layer=built, runner=FakeRunner(CLIProcessResult(exit_code=2, stderr="boom"))
    )
    with pytest.raises(RuntimeError, match="exited with 2"):
        await bridge.doctor()
    assert built.audit.entries[-1].decision == "denied"


async def test_bridge_doctor_refuses_non_json_output(binary_present) -> None:
    bridge = AgentReachBridge(
        layer=layer("*"), runner=FakeRunner(CLIProcessResult(exit_code=0, stdout="hello"))
    )
    with pytest.raises(RuntimeError, match="did not emit JSON"):
        await bridge.doctor()


# ----------------------------------------------------------------------- tools


def test_only_enabled_channels_become_tools() -> None:
    tools = {tool.name: tool for tool in cli_reach_tools(layer("github_search,bilibili_search"))}
    expected = {"reach_cli_github_search", "reach_cli_bilibili_search", "reach_cli_doctor"}
    assert expected <= set(tools)
    assert "reach_cli_twitter_search" not in tools  # not enabled
    assert tools["reach_cli_github_search"].risk is RiskLevel.DANGEROUS
    assert tools["reach_cli_doctor"].risk is RiskLevel.NETWORK


def test_the_tool_description_says_it_leaves_the_sandbox() -> None:
    tools = {tool.name: tool for tool in cli_reach_tools(layer("github_search"))}
    description = tools["reach_cli_github_search"].description
    assert "EgressPolicy" in description and "sandbox" in description


async def test_the_doctor_tool_lists_channels_without_executing_anything(binary_present) -> None:
    from os_reach import build_reach_layer

    runner = FakeRunner()
    reach = build_reach_layer(cli=layer("", runner=runner))
    registry = ToolRegistry(default_timeout=10.0)
    registry.extend(cli_reach_tools(reach.cli))
    result = await registry.execute(ToolCall(name="reach_cli_doctor", args={}))
    assert result.status is ToolStatus.SUCCESS
    assert "github_search" in result.output  # every implemented channel is listed
    assert "agent_reach: warn" in result.output  # installed, but doctor not allowed
    assert runner.calls == []  # probe() only: the doctor never started a process


async def test_an_enabled_channel_runs_through_the_registry() -> None:
    runner = FakeRunner(CLIProcessResult(exit_code=0, stdout="repo list"))
    built = layer("github_search", runner=runner)
    registry = ToolRegistry(default_timeout=15.0)
    registry.extend(cli_reach_tools(built))
    call = ToolCall(name="reach_cli_github_search", args={"query": "agents"})
    result = await registry.execute(call)
    assert result.status is ToolStatus.SUCCESS
    assert "repo list" in result.output


async def test_a_denied_channel_surfaces_its_code_as_a_tool_error() -> None:
    """A gate closed *after* the tool was registered still refuses the call."""
    built = layer("github_search", runner=FakeRunner())
    registry = ToolRegistry(default_timeout=15.0)
    registry.extend(cli_reach_tools(built))
    built.get("github_search").enabled = False  # operator switched it off mid-session
    call = ToolCall(name="reach_cli_github_search", args={"query": "agents"})
    result = await registry.execute(call)
    assert result.status is ToolStatus.ERROR
    assert result.error is not None and CLI_CHANNEL_DISABLED in result.error


def test_register_reach_tools_keeps_native_and_cli_together() -> None:
    from os_reach import build_native_registry, register_reach_tools

    reach = build_native_registry(transport=None)
    reach.cli = layer("github_search")
    registry = ToolRegistry(default_timeout=30.0)
    register_reach_tools(registry, reach=reach)
    names = set(registry.names())
    assert {"web_read", "reach_doctor", "reach_cli_github_search"} <= names
    assert "reach_cli_twitter_search" not in names  # not enabled -> not even exposed


# ------------------------------------------------------- the real subprocess


async def test_the_real_runner_executes_without_a_shell(tmp_path) -> None:
    """The one place a process actually starts: argv in, bytes out, no shell."""
    import sys

    from os_reach.adapters.cli import _default_runner

    code = "import sys\nprint('out', flush=True)\nprint('err', file=sys.stderr)\nsys.exit(3)"
    outcome = await _default_runner(
        [sys.executable, "-c", code], {"PATH": "/usr/bin:/bin"}, 30.0, 1 << 20
    )
    assert outcome.exit_code == 3
    assert outcome.stdout.strip() == "out"
    assert outcome.stderr.strip() == "err"
    assert outcome.bytes_read > 0 and outcome.truncated is False


async def test_the_real_runner_kills_a_process_that_exceeds_the_byte_budget() -> None:
    """The cap must be enforced while reading, not after buffering."""
    import sys

    from os_reach.adapters.cli import _default_runner

    code = "import sys\nwhile True: sys.stdout.write('x' * 65536)"
    outcome = await _default_runner(
        [sys.executable, "-c", code], {"PATH": "/usr/bin:/bin"}, 30.0, 256 * 1024
    )
    assert outcome.truncated is True
    assert outcome.bytes_read <= 256 * 1024
    assert "budget" in outcome.stderr


async def test_the_real_runner_enforces_the_deadline() -> None:
    import sys
    import time

    from os_reach.adapters.cli import _default_runner

    started = time.monotonic()
    outcome = await _default_runner(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        {"PATH": "/usr/bin:/bin"},
        0.4,
        1 << 20,
    )
    assert outcome.timed_out is True
    assert time.monotonic() - started < 10  # killed, not waited on


async def test_the_real_runner_reports_a_missing_binary_cleanly() -> None:
    from os_reach.adapters.cli import _default_runner

    outcome = await _default_runner(
        ["/nonexistent/cela-binary", "x"], {"PATH": "/usr/bin"}, 5.0, 1024
    )
    assert outcome.exit_code is None
    assert "binary not found" in outcome.stderr


async def test_shell_metacharacters_really_are_inert_end_to_end() -> None:
    """The claim, executed rather than asserted: the payload is data, never code."""
    import sys

    from os_reach.adapters.cli import _default_runner

    marker = Path(__file__).parent / ".." / "cli-escape-proof-marker"
    marker = marker.resolve()
    payload = f"x; touch {marker}; `touch {marker}`; $(touch {marker}); | touch {marker}"
    if marker.exists():  # pragma: no cover - defensive
        marker.unlink()
    try:
        outcome = await _default_runner(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", payload],
            {"PATH": "/usr/bin:/bin"},
            30.0,
            1 << 20,
        )
        assert outcome.exit_code == 0
        assert outcome.stdout.strip() == payload  # arrived as ONE argument, byte for byte
        assert not marker.exists()  # and executed nothing
    finally:
        if marker.exists():
            marker.unlink()


def test_build_reach_layer_keeps_the_bridge_reachable_from_both_halves() -> None:
    from os_reach import build_reach_layer

    reach = build_reach_layer(cli=layer(""))
    assert reach.cli is not None and reach.bridge is not None
    assert reach.cli.bridge is reach.bridge  # the tools and the registry see one bridge
    assert set(reach.names()) == {"github", "rss", "web"}  # the native tier is untouched
    assert reach.cli_report().source == "cli"


def test_a_registry_without_a_cli_layer_reports_an_empty_cli_report() -> None:
    from os_reach import ReachRegistry

    assert ReachRegistry().cli_report().channels == {}


# ---------------------------------------------------- file-based enablement


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "reach.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_config_file_can_enable_a_channel(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        '[cli_channels]\nenabled = ["github_search", "bilibili_search"]\n',
    )
    built = CLILayer.from_env(env={}, config_path=path)
    assert built.enabled_channels() == ["github_search", "bilibili_search"]
    assert built.notes == []
    assert built.coverage()["config_path"] == str(path)


def test_the_config_file_can_acknowledge_the_operator_session(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, '[cli_channels]\nenabled = ["twitter_search"]\nallow_auth = true\n'
    )
    built = CLILayer.from_env(env={}, config_path=path)
    assert built.get("twitter_search").auth_acknowledged is True


def test_env_and_config_are_unioned_not_ranked(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[cli_channels]\nenabled = ["github_search"]\n')
    built = CLILayer.from_env(env={"CELA_REACH_CLI_ENABLE": "bilibili_search"}, config_path=path)
    assert built.enabled_channels() == ["github_search", "bilibili_search"]


def test_a_broken_config_enables_nothing_and_says_so(tmp_path: Path) -> None:
    path = write_config(tmp_path, "[cli_channels\nenabled = [\n")  # not TOML
    built = CLILayer.from_env(env={}, config_path=path)
    assert built.enabled_channels() == []
    assert built.is_active() is False
    assert built.notes and "TOMLDecodeError" in built.notes[0]

    report = built.report()
    assert report.channels["cli_config"].status.value == "error"
    assert "TOMLDecodeError" in report.channels["cli_config"].message


def test_a_misspelled_config_key_is_reported_not_ignored(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[cli_channels]\nenable = ["github_search"]\n')
    built = CLILayer.from_env(env={}, config_path=path)
    # The typo means nothing was enabled — and the operator can see why the
    # channel they believe they turned on is off (the channel's own message).
    assert built.enabled_channels() == []
    assert built.get("github_search").probe().status.value == "off"


def test_a_config_section_of_the_wrong_type_is_reported(tmp_path: Path) -> None:
    path = write_config(tmp_path, 'cli_channels = "github_search"\n')
    built = CLILayer.from_env(env={}, config_path=path)
    assert built.enabled_channels() == []
    assert "must be a table" in built.notes[0]


def test_a_missing_config_file_is_not_a_problem(tmp_path: Path) -> None:
    built = CLILayer.from_env(env={}, config_path=tmp_path / "absent.toml")
    assert built.notes == [] and built.enabled_channels() == []


def test_the_config_env_var_selects_the_file(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[cli_channels]\nenabled = ["reddit_search"]\n')
    built = CLILayer.from_env(env={"CELA_REACH_CONFIG": str(path)})
    assert built.enabled_channels() == ["reddit_search"]
