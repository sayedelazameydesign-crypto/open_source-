"""Sandboxed code interpreter — real subprocess isolation, asserted for real."""

from __future__ import annotations

import pytest
from os_core.types import ToolStatus
from os_tools.code_exec import (
    LocalSubprocessBackend,
    SandboxLimits,
    _parse_report,
    netns_unshare_available,
)


@pytest.fixture
def sandbox() -> LocalSubprocessBackend:
    return LocalSubprocessBackend(limits=SandboxLimits(cpu_seconds=5, memory_mb=256, timeout=8.0))


async def test_executes_python_and_returns_stdout(sandbox: LocalSubprocessBackend) -> None:
    result = await sandbox.execute("print(6 * 7)")
    assert result.ok
    assert result.output.strip() == "42"
    assert result.error is None


async def test_exception_is_captured_not_raised(sandbox: LocalSubprocessBackend) -> None:
    result = await sandbox.execute("raise ValueError('boom')")
    assert result.status is ToolStatus.ERROR
    assert "ValueError" in (result.error or "")
    assert "boom" in (result.error or "")


async def test_files_written_become_artifacts(sandbox: LocalSubprocessBackend) -> None:
    code = "open('chart.html','w').write('<h1>hi</h1>')\nprint('wrote')"
    result = await sandbox.execute(code)
    assert result.ok
    names = {a.filename for a in result.artifacts}
    assert "chart.html" in names
    html = next(a for a in result.artifacts if a.filename == "chart.html")
    assert html.kind == "html"
    assert "<h1>hi</h1>" in html.content


async def test_wall_clock_timeout_kills_the_process() -> None:
    tight = LocalSubprocessBackend(limits=SandboxLimits(timeout=1.0))
    result = await tight.execute("import time\nwhile True: time.sleep(0.05)")
    assert result.status is ToolStatus.TIMEOUT
    assert "killed" in (result.error or "")


async def test_memory_limit_is_enforced() -> None:
    tight = LocalSubprocessBackend(limits=SandboxLimits(memory_mb=128, timeout=15.0))
    result = await tight.execute("x = bytearray(600 * 1024 * 1024)\nprint(len(x))")
    assert result.status is ToolStatus.ERROR


async def test_non_python_language_is_refused(sandbox: LocalSubprocessBackend) -> None:
    result = await sandbox.execute("console.log(1)", language="javascript")
    assert result.status is ToolStatus.ERROR
    assert "not supported" in (result.error or "")


async def test_path_traversal_in_staged_files_is_denied(sandbox: LocalSubprocessBackend) -> None:
    result = await sandbox.execute("print(1)", files={"../../etc/evil": "x"})
    assert result.status is ToolStatus.DENIED
    assert "escapes the sandbox" in (result.error or "")


async def test_staged_files_are_visible_to_the_code(sandbox: LocalSubprocessBackend) -> None:
    result = await sandbox.execute(
        "print(open('data.txt').read().upper())", files={"data.txt": "hello"}
    )
    assert result.ok
    assert "HELLO" in result.output


def test_output_is_truncated_not_unbounded() -> None:
    user_output, report, artifacts = _parse_report("x" * 100_000)
    assert len(user_output) < 100_000
    assert "truncated" in user_output
    assert report == {}
    assert artifacts == []


def test_report_without_sentinel_is_treated_as_plain_output() -> None:
    user_output, report, artifacts = _parse_report("just stdout\n")
    assert user_output == "just stdout"
    assert report == {}
    assert artifacts == []


def test_corrupt_report_is_reported_as_error() -> None:
    user_output, report, _ = _parse_report("hi\n__OS_REPORT__{not json__END_OS_REPORT__\n")
    assert user_output == "hi"
    assert report["err"] == "ReportParseError"


async def test_sandbox_cannot_reach_the_network(sandbox: LocalSubprocessBackend) -> None:
    """With a netns available the sandbox has no interfaces at all."""

    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
        "    print('NETWORK_REACHABLE')\n"
        "except OSError as exc:\n"
        "    print('NETWORK_BLOCKED', type(exc).__name__)\n"
    )
    if not netns_unshare_available():
        pytest.skip(
            "this host cannot create a network namespace (unshare rejected); "
            "egress isolation is the e2b/firecracker backend's job"
        )
    result = await sandbox.execute(code)
    assert result.ok
    assert "NETWORK_BLOCKED" in result.output
    assert result.metadata["network"] == "blocked (netns)"
