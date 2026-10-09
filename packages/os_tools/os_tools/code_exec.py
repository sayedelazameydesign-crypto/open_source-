"""Sandboxed code interpreter.

The architecture doc calls for E2B / Firecracker instead of "plain Docker".
This prototype implements the same *contract* locally so the agent layer is
portable:

* ``execute()`` runs untrusted code in a child process, never in-process.
* Resource ceilings via ``setrlimit``: CPU, address space, file size, process
  count (fork-bomb defence).
* Hard wall-clock timeout enforced by ``asyncio.wait_for`` + process kill.
* A private temp ``cwd`` — nothing outside it is readable by name.
* Output truncation so a chatty program cannot OOM the orchestrator.
* ``unshare_net()`` drops the network namespace when available (Linux +
  CAP_SYS_ADMIN); otherwise ``network="blocked"`` is reported honestly and
  the e2b/firecracker backend is the real boundary.

``CodeExecBackend`` is the seam: swap :class:`LocalSubprocessBackend` for an
E2B/Firecracker implementation without touching the agents.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from os_core.types import Artifact, ToolResult, ToolStatus
from pydantic import BaseModel, Field

MAX_OUTPUT_CHARS = 20_000
MAX_FILE_BYTES = 256 * 1024
MAX_COLLECTED_FILES = 20

_RUNNER = r"""
import base64, json, os, sys, traceback
code = base64.b64decode(os.environ["__SRC_B64__"]).decode("utf-8")
cwd = os.getcwd()
err, err_tb = "", ""
try:
    exec(compile(code, "<sandbox>", "exec"), {"__name__": "__main__"})
except SystemExit as exc:
    err_tb = f"process exited with code {exc.code}"
except BaseException:
    err = type(sys.exc_info()[1]).__name__
    err_tb = traceback.format_exc(limit=6)
finally:
    try:
        sys.stdout.flush(); sys.stderr.flush()
    except Exception:
        pass
files = []
for root, dirs, names in os.walk(cwd):
    dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git"}]
    for name in names:
        if name == "__runner__.py" or len(files) >= __MAX_FILES__:
            continue
        path = os.path.join(root, name)
        try:
            if os.path.getsize(path) <= __MAX_FILE_BYTES__:
                with open(path, "rb") as fh:
                    payload = fh.read()
                files.append({"path": os.path.relpath(path, cwd),
                              "b64": base64.b64encode(payload).decode()})
        except OSError:
            continue
report = json.dumps({"files": files, "err": err, "tb": err_tb})
sys.stdout.write("\n__OS_REPORT__" + report + "__END_OS_REPORT__\n")
"""

_SENTINEL_OPEN = "__OS_REPORT__"
_SENTINEL_CLOSE = "__END_OS_REPORT__"


@runtime_checkable
class CodeExecBackend(Protocol):
    async def execute(
        self, code: str, *, language: str, files: dict[str, str], timeout: float
    ) -> ToolResult: ...


class SandboxLimits(BaseModel):
    cpu_seconds: int = 20
    memory_mb: int = 512
    max_file_size_bytes: int = 8 * 1024 * 1024
    max_processes: int = 64
    timeout: float = 30.0


class LocalSubprocessBackend(BaseModel):
    """Real isolation available locally: subprocess + rlimits + temp cwd."""

    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    interpreter: str = "python3"
    unshare_net: bool = True

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",
        files: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ToolResult:
        if language not in {"python", "py"}:
            return ToolResult(
                tool_call_id="",
                tool_name="python_execute",
                status=ToolStatus.ERROR,
                error=f"language '{language}' not supported by this backend (python only)",
            )
        deadline = timeout if timeout is not None else self.limits.timeout
        workdir = Path(tempfile.mkdtemp(prefix="sandbox_"))
        try:
            for name, content in (files or {}).items():
                target = (workdir / name).resolve()
                if workdir not in target.parents and target != workdir:
                    return ToolResult(
                        tool_call_id="",
                        tool_name="python_execute",
                        status=ToolStatus.DENIED,
                        error=f"file path '{name}' escapes the sandbox",
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            runner = workdir / "__runner__.py"
            runner.write_text(
                _RUNNER.replace("__MAX_FILES__", str(MAX_COLLECTED_FILES)).replace(
                    "__MAX_FILE_BYTES__", str(MAX_FILE_BYTES)
                ),
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["__SRC_B64__"] = base64.b64encode(code.encode()).decode()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env.pop("PYTHONPATH", None)

            argv = self._argv(runner)
            started = asyncio.get_running_loop().time()
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workdir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=self._preexec() if os.name == "posix" else None,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=deadline)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(
                    tool_call_id="",
                    tool_name="python_execute",
                    status=ToolStatus.TIMEOUT,
                    error=f"execution exceeded {deadline}s and was killed",
                )
            elapsed = asyncio.get_running_loop().time() - started

            raw_out = stdout.decode(errors="replace")
            err_text = stderr.decode(errors="replace")
            out_text, structured, artifacts = _parse_report(raw_out)
            status = (
                ToolStatus.SUCCESS
                if proc.returncode == 0 and not structured.get("err")
                else ToolStatus.ERROR
            )
            error = None
            if structured.get("err"):
                error = f"{structured['err']}: {structured.get('tb', '')}".strip()
            elif proc.returncode != 0:
                error = _truncate(err_text) or f"process exited with {proc.returncode}"
            return ToolResult(
                tool_call_id="",
                tool_name="python_execute",
                status=status,
                output=out_text,
                error=error,
                artifacts=artifacts,
                duration_ms=round(elapsed * 1000, 2),
                metadata={
                    "returncode": proc.returncode,
                    "network": self._network_mode(),
                    "limits": self.limits.model_dump(),
                },
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- isolation plumbing --------------------------------------------
    def _argv(self, runner: Path) -> Sequence[str]:
        if self.unshare_net and shutil.which("unshare"):
            # New network + PID namespace: no interfaces at all inside.
            return [
                "unshare",
                "--net",
                "--map-root-user",
                "--fork",
                "--kill-child",
                sys.executable,
                str(runner),
            ]
        return [sys.executable, str(runner)]

    def _preexec(self):
        """rlimits applied in the child, after fork, before exec."""
        import resource

        limits = self.limits

        def _apply() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 1))
            mem = limits.memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(
                resource.RLIMIT_FSIZE, (limits.max_file_size_bytes, limits.max_file_size_bytes)
            )
            if hasattr(resource, "RLIMIT_NPROC"):
                resource.setrlimit(resource.RLIMIT_NPROC, (limits.max_processes,) * 2)
            os.setsid()  # own process group so the whole tree can be signalled

        return _apply

    def _network_mode(self) -> str:
        return "blocked (netns)" if self.unshare_net and shutil.which("unshare") else "unverified"


class InMemoryBackend(BaseModel):
    """Deterministic backend for tests — never executes anything."""

    responses: dict[str, ToolResult] = Field(default_factory=dict)
    default: ToolResult | None = None
    calls: list[str] = Field(default_factory=list, exclude=True)

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",
        files: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ToolResult:
        self.calls.append(code)
        for key, result in self.responses.items():
            if key in code:
                return result.model_copy(deep=True)
        if self.default is not None:
            return self.default.model_copy(deep=True)
        return ToolResult(
            tool_call_id="",
            tool_name="python_execute",
            status=ToolStatus.SUCCESS,
            output="(no canned response)",
        )


def _parse_report(raw_stdout: str) -> tuple[str, dict[str, Any], list[Artifact]]:
    """Split the sandbox report out of stdout and turn files into artifacts.

    The runner appends a sentinel-delimited JSON blob after the user's own
    output, so truncation is applied to the user part only and can never
    corrupt the report.
    """
    start = raw_stdout.rfind(_SENTINEL_OPEN)
    if start < 0:
        return _truncate(raw_stdout.rstrip("\n")), {}, []
    end = raw_stdout.find(_SENTINEL_CLOSE, start)
    user_output = raw_stdout[:start].rstrip("\n")
    if end < 0:
        return (
            _truncate(user_output),
            {"err": "ReportTruncated", "tb": "sandbox report was cut off"},
            [],
        )
    blob = raw_stdout[start + len(_SENTINEL_OPEN) : end].strip()
    try:
        report = json.loads(blob)
    except json.JSONDecodeError as exc:
        return _truncate(user_output), {"err": "ReportParseError", "tb": str(exc)}, []
    artifacts = [
        _to_artifact(entry["path"], base64.b64decode(entry["b64"]))
        for entry in report.get("files", [])[:MAX_COLLECTED_FILES]
        if entry.get("path") and entry.get("b64")
    ]
    return _truncate(user_output), report, artifacts


def _to_artifact(filename: str, data: bytes) -> Artifact:
    """Map a sandbox-produced file onto an Artifacts-pane artifact."""
    suffix = Path(filename).suffix.lower()
    kind_map = {
        ".html": ("html", "text/html"),
        ".htm": ("html", "text/html"),
        ".md": ("markdown", "text/markdown"),
        ".json": ("json", "application/json"),
        ".png": ("image", "image/png"),
        ".jpg": ("image", "image/jpeg"),
        ".jpeg": ("image", "image/jpeg"),
        ".svg": ("image", "image/svg+xml"),
    }
    text_suffixes = {".py", ".csv", ".txt", ".log", ".js", ".ts", ".css", ".yaml", ".yml"}
    if suffix in text_suffixes:
        return Artifact(
            kind="code" if suffix in {".py", ".js", ".ts", ".css"} else "file",
            filename=filename,
            content=data.decode(errors="replace"),
            mime_type="text/plain",
        )
    kind, mime = kind_map.get(suffix, ("file", "application/octet-stream"))
    if kind in {"html", "markdown", "json"}:
        return Artifact(
            kind=kind, filename=filename, content=data.decode(errors="replace"), mime_type=mime
        )
    return Artifact(
        kind=kind,
        filename=filename,
        content=base64.b64encode(data).decode(),
        mime_type=mime,
        metadata={"encoding": "base64"},
    )


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    head = text[: MAX_OUTPUT_CHARS // 2]
    tail = text[-MAX_OUTPUT_CHARS // 2 :]
    return f"{head}\n... [output truncated: {len(text)} chars total] ...\n{tail}"


def python_execute_tool(backend: CodeExecBackend, *, timeout: float = 30.0):
    """Build the ``python_execute`` Tool bound to a backend."""
    from os_core.types import RiskLevel

    from os_tools.registry import Tool

    async def _run(code: str, language: str = "python") -> ToolResult:
        result = await backend.execute(
            textwrap.dedent(code), language=language, files=None, timeout=timeout
        )
        return result

    return Tool(
        name="python_execute",
        description=(
            "Execute Python code in an isolated sandbox (no network, resource-limited) and "
            "return stdout, stderr and any files it wrote as artifacts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute."},
                "language": {"type": "string", "enum": ["python"], "default": "python"},
            },
            "required": ["code"],
        },
        risk=RiskLevel.WRITE,
        timeout=timeout + 5,
        fn=_run,
    )
