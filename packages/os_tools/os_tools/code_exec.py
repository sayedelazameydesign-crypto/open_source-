"""Sandboxed code interpreter.

The architecture doc calls for E2B / Firecracker instead of "plain Docker".
This prototype implements the same *contract* locally so the agent layer is
portable:

* ``execute()`` runs untrusted code in a child process, never in-process.
* Resource ceilings via ``setrlimit``: CPU, address space, file size, process
  count (fork-bomb defence).
* Hard wall-clock timeout enforced by ``asyncio.wait_for`` + process kill.
* A clean, allow-listed environment: the parent process environment is
  *not* inherited, so API keys and other server secrets cannot leak into
  untrusted code. Only harmless runtime variables (``PATH``, locale,
  temp-dir and Windows system variables) are passed through.
* A private temp ``cwd`` as the working directory. This is *not*
  filesystem isolation: untrusted code can still read host files by
  absolute path, so production deployments need a microVM/container
  backend (E2B/Firecracker) as the real boundary.
* ``CELA_SANDBOXED=1`` in the child environment, so host-side capabilities
  that refuse to run inside a sandbox (the reach CLI adapters, ADR-0013) can
  actually enforce that refusal instead of trusting a convention.
* Output truncation so a chatty program cannot OOM the orchestrator.
* ``unshare_net()`` drops the network namespace when available (Linux +
  CAP_SYS_ADMIN); otherwise ``network="unverified (netns unavailable on
  this host)"`` is reported honestly — isolation is never claimed when it
  is not actually in place — and the e2b/firecracker backend is the real
  boundary.

``CodeExecBackend`` is the seam: swap :class:`LocalSubprocessBackend` for an
E2B/Firecracker implementation without touching the agents.

Production isolation (step 2): :class:`E2BBackend` and
:class:`FirecrackerBackend` execute only inside a remote microVM/sandbox
service. They are **fail-closed**: missing SDK, missing endpoint, an
unreachable manager or a provider error all return ``DENIED`` — they never
fall back to the local subprocess backend. Selection is via
``CELA_CODE_EXEC_MODE=production`` + ``CELA_CODE_EXEC_BACKEND=e2b|firecracker``
(see :func:`code_exec_backend_from_env`); the local backend remains the
explicit default for offline development and tests.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import json
import os
import shutil
import subprocess
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

# The only parent-process variables the sandbox child may inherit.
# Deny-by-default: anything not listed here — API keys, tokens, database
# URLs, cloud credentials — never crosses into untrusted code. ``PATH`` is
# needed to locate helpers such as ``unshare``; the rest keep the
# interpreter, locale and temp-file handling working across platforms.
_INHERITED_ENV_VARS: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TEMP",
    "TMP",
    "TMPDIR",
    # Windows interpreter / DLL loading essentials.
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)


def _sandbox_env(code: str) -> dict[str, str]:
    """Build the child's environment from an allow-list, never by inheritance.

    The previous implementation copied ``os.environ`` wholesale (minus
    ``PYTHONPATH``), so any secret configured on the server — model API
    keys, database URLs, cloud credentials — was one ``os.environ`` read
    away from untrusted code. Here only :data:`_INHERITED_ENV_VARS` are
    passed through, plus the sandbox's own variables.
    """
    env = {name: os.environ[name] for name in _INHERITED_ENV_VARS if name in os.environ}
    env["__SRC_B64__"] = base64.b64encode(code.encode()).decode()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"  # never load user site-packages from the host
    # Marker, not isolation: sandboxed code cannot borrow a host-side CLI
    # capability, because the reach adapter refuses to run when it sees this
    # (ADR-0013). Anything executing untrusted code outside this backend must
    # set the same marker to keep that promise.
    env["CELA_SANDBOXED"] = "1"
    return env


@runtime_checkable
class CodeExecBackend(Protocol):
    async def execute(
        self, code: str, *, language: str, files: dict[str, str], timeout: float
    ) -> ToolResult: ...


# Namespace flags we need for real network isolation. `--map-root-user` is
# what makes an unprivileged netns possible, and it is exactly the flag that
# restricted CI runners refuse.
_NETNS_FLAGS = ("--net", "--map-root-user", "--fork", "--kill-child")


@functools.lru_cache(maxsize=1)
def _release_transport(proc: Any) -> None:
    """Close a finished subprocess transport instead of leaving it to the GC.

    An unclosed transport is finalized at an arbitrary later moment; if its
    event loop has already closed, ``__del__`` raises "Event loop is closed" as
    an unraisable exception, which surfaces as a warning inside whatever
    unrelated code happens to trigger the collection. asyncio exposes no public
    close, so the transport is closed directly and errors are ignored: this runs
    on a path that has already produced its result.
    """
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()


def netns_unshare_available() -> bool:
    """Can this host *actually* create a network namespace?

    Presence of the ``unshare`` binary proves nothing: GitHub-hosted runners
    ship it but reject the uid_map write with ``Operation not permitted``,
    which previously broke every single sandboxed execution. So the real
    command is run once and the verdict cached.
    """
    if not shutil.which("unshare"):
        return False
    try:
        completed = subprocess.run(
            ["unshare", *_NETNS_FLAGS, "true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


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
            env = _sandbox_env(code)

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
            finally:
                # Every way out of this block — success, timeout, and the case
                # that used to leak: the caller's task being cancelled mid-run,
                # which left the child running and its transport unclosed.
                if proc.returncode is None:
                    proc.kill()
                _release_transport(proc)
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
        if self.unshare_net and netns_unshare_available():
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
        """Report egress state truthfully — never claim isolation we lack."""
        if not self.unshare_net:
            return "disabled by config"
        if netns_unshare_available():
            return "blocked (netns)"
        return "unverified (netns unavailable on this host)"


def _denied(backend: str, reason: str) -> ToolResult:
    """Fail closed: report *no* execution happened, never degrade locally."""
    return ToolResult(
        tool_call_id="",
        tool_name="python_execute",
        status=ToolStatus.DENIED,
        error=f"[{backend}] execution refused (fail-closed): {reason}",
        metadata={"isolation": backend, "executed": False, "network": "not started"},
    )


class FirecrackerBackend(BaseModel):
    """Execute via a Firecracker microVM manager service (HTTP contract).

    The manager (outside this repo — e.g. a host-side agent that boots a
    microVM per request) must expose ``POST {endpoint}/execute`` accepting
    ``{code, language, files, timeout}`` and returning
    ``{output, error, status, files:[{path, b64}]}``. Without a reachable
    manager this backend denies every request; it never runs code locally.
    """

    endpoint: str | None = None
    endpoint_env: str = "CELA_FIRECRACKER_ENDPOINT"
    api_token_env: str = "CELA_FIRECRACKER_API_TOKEN"
    transport: Any = Field(default=None, exclude=True)

    model_config = {"arbitrary_types_allowed": True}

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",
        files: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ToolResult:
        endpoint = self.endpoint or os.environ.get(self.endpoint_env)
        if not endpoint:
            return _denied("firecracker", f"${self.endpoint_env} is not configured")
        try:
            import httpx
        except ImportError:
            return _denied("firecracker", "httpx is not installed")
        headers = {}
        token = os.environ.get(self.api_token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=timeout or 30.0
            ) as client:
                resp = await client.post(
                    f"{endpoint.rstrip('/')}/execute",
                    json={
                        "code": code,
                        "language": language,
                        "files": files or {},
                        "timeout": timeout or 30.0,
                    },
                    headers=headers,
                )
        except Exception as exc:  # unreachable manager -> fail closed
            return _denied("firecracker", f"manager unreachable: {exc}")
        if resp.status_code != 200:
            return _denied("firecracker", f"manager returned HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            return _denied("firecracker", "manager returned a non-JSON response")
        artifacts = [
            _to_artifact(entry["path"], base64.b64decode(entry["b64"]))
            for entry in payload.get("files", [])
            if entry.get("path") and entry.get("b64")
        ]
        status = (
            ToolStatus.SUCCESS
            if payload.get("status", "success") == "success"
            else ToolStatus.ERROR
        )
        return ToolResult(
            tool_call_id="",
            tool_name="python_execute",
            status=status,
            output=_truncate(str(payload.get("output", ""))),
            error=payload.get("error"),
            artifacts=artifacts,
            metadata={
                "isolation": "firecracker-microvm",
                "executed": True,
                "network": "manager-policy",
            },
        )


class E2BBackend(BaseModel):
    """Execute inside an E2B sandbox (microVM) via the optional E2B SDK.

    Requires ``pip install 'cela-use-browser[sandbox]'`` and ``E2B_API_KEY``.
    Any missing piece denies execution — there is deliberately no local
    fallback path in this class.
    """

    api_key_env: str = "E2B_API_KEY"
    template: str | None = None
    sandbox_factory: Any = Field(default=None, exclude=True)

    model_config = {"arbitrary_types_allowed": True}

    async def _make_sandbox(self) -> Any:
        if self.sandbox_factory is not None:
            return await asyncio.to_thread(self.sandbox_factory)
        try:
            from e2b_code_interpreter import Sandbox
        except ImportError:
            raise RuntimeError("e2b-code-interpreter is not installed") from None
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"${self.api_key_env} is not set")
        kwargs: dict[str, Any] = {"api_key": api_key}
        if self.template:
            kwargs["template"] = self.template
        return await asyncio.to_thread(lambda: Sandbox.create(**kwargs))

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",
        files: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ToolResult:
        if language not in {"python", "py"}:
            return _denied("e2b", f"language '{language}' not supported (python only)")
        try:
            sandbox = await self._make_sandbox()
        except Exception as exc:
            return _denied("e2b", str(exc))
        try:
            for name, content in (files or {}).items():
                await asyncio.to_thread(sandbox.files.write, name, content)
            execution = await asyncio.to_thread(sandbox.run_code, code)
            logs = getattr(execution, "logs", None)
            stdout = "".join(getattr(logs, "stdout", []) or [])
            error_obj = getattr(execution, "error", None)
            text = getattr(execution, "text", None)
            output = stdout or (text if isinstance(text, str) else "")
            if error_obj is not None:
                return ToolResult(
                    tool_call_id="",
                    tool_name="python_execute",
                    status=ToolStatus.ERROR,
                    output=_truncate(output),
                    error=f"{getattr(error_obj, 'name', 'Error')}: "
                    f"{getattr(error_obj, 'value', '')}",
                    metadata={
                        "isolation": "e2b-microvm",
                        "executed": True,
                        "network": "sandbox-policy",
                    },
                )
            return ToolResult(
                tool_call_id="",
                tool_name="python_execute",
                status=ToolStatus.SUCCESS,
                output=_truncate(output),
                metadata={
                    "isolation": "e2b-microvm",
                    "executed": True,
                    "network": "sandbox-policy",
                },
            )
        except Exception as exc:  # provider failure mid-run: report, never rerun locally
            return _denied("e2b", f"sandbox execution failed: {exc}")
        finally:
            with contextlib.suppress(Exception):
                kill = getattr(sandbox, "kill", None)
                if callable(kill):
                    await asyncio.to_thread(kill)


def code_exec_backend_from_env(env: dict[str, str] | None = None) -> CodeExecBackend:
    """Select the code-execution backend from the environment.

    ``CELA_CODE_EXEC_MODE``: ``local`` (default) | ``production`` | ``e2b`` |
    ``firecracker``. Production refuses to construct a local backend: it
    requires ``CELA_CODE_EXEC_BACKEND=e2b|firecracker`` and raises otherwise,
    so a misconfigured production deployment fails at startup instead of
    silently executing untrusted code on the host.
    """
    cfg = os.environ if env is None else env
    mode = cfg.get("CELA_CODE_EXEC_MODE", "local").lower()
    if mode == "local":
        return LocalSubprocessBackend()
    if mode in {"e2b", "production"} and (
        mode == "e2b" or cfg.get("CELA_CODE_EXEC_BACKEND") == "e2b"
    ):
        return E2BBackend()
    if mode in {"firecracker", "production"} and (
        mode == "firecracker" or cfg.get("CELA_CODE_EXEC_BACKEND") == "firecracker"
    ):
        return FirecrackerBackend()
    if mode == "production":
        raise RuntimeError(
            "CELA_CODE_EXEC_MODE=production requires CELA_CODE_EXEC_BACKEND=e2b|firecracker; "
            "refusing to fall back to the local subprocess backend"
        )
    raise RuntimeError(f"unknown CELA_CODE_EXEC_MODE={mode!r}")


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
            "Execute Python code in a resource-limited sandbox subprocess (clean "
            "allow-listed environment, rlimits, wall-clock timeout; network is "
            "isolated only when the host supports a network namespace — check the "
            "result metadata) and return stdout, stderr and any files it wrote "
            "as artifacts."
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
