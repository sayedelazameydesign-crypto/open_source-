"""CLI channel adapter — host-side upstream tools behind explicit gates.

These channels give the agent what Agent-Reach routes to: `gh`, `yt-dlp`,
`mcporter`, `bili`, `twitter`, `rdt` … tools that already hold the operator's
sessions and reach the network on their own terms.

**They are not sandboxed and cannot be.** No subprocess can be held to
:class:`~os_tools.egress.EgressPolicy` — it opens its own sockets. That is why
every gate here is explicit and none has a default that enables anything:

1. **Per-channel enablement** — ``CELA_REACH_CLI_ENABLE=github_search,bilibili_search``
   (or ``*``), or the same list as ``[cli_channels] enabled`` in
   ``~/.config/cela/reach.toml`` (``cli reach enable`` writes it, with a
   confirmation). A channel that is named in neither place is *off*, and asking
   for it returns ``CLI_CHANNEL_DISABLED`` rather than a silent no-op.
2. **Authentication acknowledgement** — a channel that runs with the operator's
   logged-in session (Twitter, Reddit …) additionally needs
   ``CELA_REACH_CLI_ALLOW_AUTH=1``. Enabling a search command and lending it
   your identity are two different decisions.
3. **Never inside the sandbox** — the sandbox marks its children with
   ``CELA_SANDBOXED=1``; the adapter refuses to run when it sees that marker, so
   sandboxed code cannot borrow a host capability.
4. **No shell, ever** — argv is built as a list and executed with
   ``create_subprocess_exec``. ``;``, ``|``, ``$()`` and backticks are therefore
   *literal characters* inside a single argument, which is exactly what a search
   for ``C++ | Rust`` needs. What is rejected instead is what a list cannot
   neutralise: a query starting with ``-`` (option injection into the upstream
   CLI), control characters, and anything over the length cap.
5. **A cap on everything** — wall-clock timeout, streamed byte budget (the
   process is killed when it is exceeded), returned-text cap, and an
   allow-listed environment so the rest of the server's secrets never reach the
   child.

Every call — allowed or denied — leaves one :class:`~os_reach.audit.ReachAuditEntry`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from os_tools.egress import parse_http_target
from pydantic import BaseModel, Field

from os_reach.audit import ReachAuditEntry, ReachAuditLog, default_audit_path
from os_reach.contract import (
    ChannelHealth,
    ReachReport,
    ReachState,
    ReachTier,
    parse_doctor_json,
)
from os_reach.native import native_channels
from os_reach.registry import ReachRegistry
from os_reach.security import scrub_credentials, truncate

# --- environment knobs (all read at construction time, never per call) -------
CLI_ENABLE_ENV = "CELA_REACH_CLI_ENABLE"
CLI_AUTH_ACK_ENV = "CELA_REACH_CLI_ALLOW_AUTH"
CLI_DOCTOR_ENV = "CELA_REACH_CLI_DOCTOR"
CLI_CONFIG_ENV = "CELA_REACH_CONFIG"
SANDBOX_MARKER_ENV = "CELA_SANDBOXED"
CLI_TIMEOUT_ENV = "CELA_REACH_CLI_TIMEOUT"

# --- failure codes (stable strings: surfaces and tests assert on them) -------
CLI_CHANNEL_DISABLED = "CLI_CHANNEL_DISABLED"
CLI_CHANNEL_UNKNOWN = "CLI_CHANNEL_UNKNOWN"
CLI_CHANNEL_NOT_IMPLEMENTED = "CLI_CHANNEL_NOT_IMPLEMENTED"
CLI_CHANNEL_AUTH_NOT_ACKNOWLEDGED = "CLI_CHANNEL_AUTH_NOT_ACKNOWLEDGED"
CLI_CHANNEL_SANDBOXED = "CLI_CHANNEL_SANDBOXED"
CLI_ARGUMENT_REJECTED = "CLI_ARGUMENT_REJECTED"
CLI_BINARY_MISSING = "CLI_BINARY_MISSING"
CLI_TIMEOUT = "CLI_TIMEOUT"
CLI_NONZERO_EXIT = "CLI_NONZERO_EXIT"
CLI_RUNNER_ERROR = "CLI_RUNNER_ERROR"

#: Environment passed to a child. Deliberately small: PATH to find the binary,
#: HOME/LANG/TZ because these tools keep their own state and dates, temp dirs so
#: they can write scratch files. Nothing else from the server's environment.
DEFAULT_ENV_ALLOW: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "WINDIR",
)

#: Default byte budget for one CLI call (the spec's 10 MB), and the much smaller
#: cap on what is handed back to the model.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_CHARS = 20_000
DEFAULT_TIMEOUT = 60.0
MAX_QUERY_CHARS = 2_000

#: Where the operator's enablement lives when it is not in the environment:
#: ``~/.config/cela/reach.toml``
#:
#:     [cli_channels]
#:     enabled = ["github_search", "bilibili_search"]
#:     allow_auth = false
#:
#: Same shape as the environment variables, on purpose: one list, one
#: acknowledgement, so there is exactly one thing to reason about.
CANONICAL_CONFIG_PATH = "~/.config/cela/reach.toml"
CONFIG_SECTION = "cli_channels"
CONFIG_ENABLED_KEY = "enabled"
CONFIG_ALLOW_AUTH_KEY = "allow_auth"


class CLIProcessResult(BaseModel):
    """What a runner observed — the seam tests replace with a fake."""

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    bytes_read: int = 0
    truncated: bool = False


Runner = Callable[[list[str], dict[str, str], float, int], Awaitable[CLIProcessResult]]


class ReachResult(BaseModel):
    """Unified result of one reach call, native or CLI."""

    channel: str
    ok: bool
    text: str = ""
    items: list[dict[str, Any]] = Field(default_factory=list)
    code: str = ""
    error: str = ""
    exit_code: int | None = None
    bytes_out: int = 0
    truncated: bool = False
    duration_ms: float = 0.0
    correlation_id: str = ""

    @property
    def status(self) -> str:
        return "ok" if self.ok else (self.code or "error")


class CLIChannelSpec(BaseModel):
    """A curated upstream command: fixed argv, one slot for the query.

    ``argv`` is ``[binary, *base_args] + [query or template] + tail_args``: there
    is exactly one place the caller's text lands, and it lands as a single
    argument. A spec is only added here when its command can be pointed at in
    the upstream project's own documentation (``docs`` keeps the pointer for
    review) — inventing commands for the other channels would be guesswork.
    """

    name: str
    description: str
    binary: str
    base_args: list[str] = Field(default_factory=list)
    tail_args: list[str] = Field(default_factory=list)
    query_kind: Literal["text", "url"] = "text"
    #: Optional single-element template, e.g. ``"query={query}"`` for mcporter.
    query_template: str | None = None
    requires_auth: bool = False
    json_output: bool = False
    #: Extra parent-environment variables this tool needs (its own tokens).
    env_passthrough: list[str] = Field(default_factory=list)
    tier: ReachTier = ReachTier.SETUP
    docs: str = ""


#: The channels whose exact command we can justify today, taken from Agent-Reach
#: v1.5.0 ``skill/SKILL.md`` / ``skill/references/*.md``. Everything else is
#: health-only through the bridge below and returns CLI_CHANNEL_NOT_IMPLEMENTED.
CLI_CHANNEL_SPECS: dict[str, CLIChannelSpec] = {
    "github_search": CLIChannelSpec(
        name="github_search",
        description="Search public GitHub repositories with the gh CLI",
        binary="gh",
        base_args=["search", "repos"],
        tail_args=["--limit", "10"],
        env_passthrough=["GITHUB_TOKEN", "GH_TOKEN"],
        docs="agent_reach/skill/references/dev.md",
    ),
    "youtube_metadata": CLIChannelSpec(
        name="youtube_metadata",
        description="Read a YouTube video's metadata and subtitle availability with yt-dlp",
        binary="yt-dlp",
        base_args=["--dump-single-json", "--skip-download", "--no-playlist"],
        query_kind="url",
        json_output=True,
        docs="agent_reach/skill/references/video.md",
    ),
    "bilibili_search": CLIChannelSpec(
        name="bilibili_search",
        description="Search Bilibili with bili-cli (no login required)",
        binary="bili",
        base_args=["search"],
        tail_args=["--type", "video", "-n", "5"],
        docs="agent_reach/skill/references/social.md",
    ),
    "exa_search": CLIChannelSpec(
        name="exa_search",
        description="Web search through the exa MCP server via mcporter",
        binary="mcporter",
        base_args=["call", "exa.web_search_exa"],
        query_template="query={query}",
        tail_args=["numResults=5"],
        docs="agent_reach/skill/references/search.md",
    ),
    "twitter_search": CLIChannelSpec(
        name="twitter_search",
        description="Search Twitter/X with twitter-cli (uses the operator's session)",
        binary="twitter",
        base_args=["search"],
        tail_args=["-n", "10"],
        requires_auth=True,
        env_passthrough=["TWITTER_AUTH_TOKEN", "TWITTER_CT0"],
        docs="agent_reach/skill/references/social.md",
    ),
    "reddit_search": CLIChannelSpec(
        name="reddit_search",
        description="Search Reddit with rdt-cli (uses the operator's session)",
        binary="rdt",
        base_args=["search"],
        tail_args=["--limit", "10"],
        requires_auth=True,
        docs="agent_reach/skill/references/social.md",
    ),
}

#: The upstream project's 16 platforms. Kept so the surfaces can say honestly:
#: "N of these have a verified command here, 16 have health monitoring".
KNOWN_UPSTREAM_CHANNELS: tuple[str, ...] = (
    "web",
    "youtube",
    "rss",
    "exa_search",
    "github",
    "twitter",
    "bilibili",
    "reddit",
    "facebook",
    "instagram",
    "xiaohongshu",
    "linkedin",
    "boss",
    "v2ex",
    "xueqiu",
    "xiaoyuzhou",
)


def render_config(enabled: list[str], allow_auth: bool) -> str:
    """The ``reach.toml`` body for *enabled* — comments explain the two gates."""
    names = ", ".join(f'"{name}"' for name in enabled)
    return (
        "# Created by `cela reach enable`. Read at startup (and by the HTTP reach"
        " surface).\n"
        "#\n"
        "# Channels listed here run host-side programs: they do NOT go through\n"
        "# EgressPolicy and they do NOT run inside the sandbox. Each call is audited.\n"
        "[cli_channels]\n"
        f"enabled = [{names}]\n"
        f"allow_auth = {'true' if allow_auth else 'false'}\n"
    )


def write_cli_config(
    path: Path,
    *,
    enable: list[str] | None = None,
    disable: list[str] | None = None,
    allow_auth: bool | None = None,
) -> tuple[list[str], bool]:
    """Add/remove channels in the operator's config; return the new state.

    Written atomically and owner-only (0600), and it refuses to write through a
    symlink: a file that grants host-side execution should not be plantable by
    anything but the operator. Only names that exist as specs are accepted, so a
    typo cannot look like a successful enable.
    """
    enabled, auth, _note = load_cli_config(path)
    current = list(enabled)
    for name in enable or []:
        if name not in CLI_CHANNEL_SPECS:
            raise ValueError(
                f"unknown channel '{name}'; known: {', '.join(sorted(CLI_CHANNEL_SPECS))}"
            )
        if name not in current:
            current.append(name)
    for name in disable or []:
        if name in current:
            current.remove(name)
    if allow_auth is not None:
        auth = allow_auth

    if path.is_symlink():
        raise ValueError(f"refusing to write through the symlink {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = render_config(current, auth)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:  # never leave a half-written config behind
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return current, auth


def parse_channel_list(raw: str) -> list[str]:
    """Split an enablement list; ``*`` means every implemented channel."""
    names = [part.strip() for part in (raw or "").split(",") if part.strip()]
    return names


def default_config_path(env: dict[str, str] | None = None) -> Path:
    """``$CELA_REACH_CONFIG`` when set, else ``~/.config/cela/reach.toml``."""
    source = env if env is not None else dict(os.environ)
    configured = source.get(CLI_CONFIG_ENV, "").strip()
    return Path(configured).expanduser() if configured else Path(CANONICAL_CONFIG_PATH).expanduser()


def load_cli_config(path: Path) -> tuple[list[str], bool, str]:
    """Read ``[cli_channels]`` from *path*: (enabled, allow_auth, note).

    A missing file is not an error (most deployments use the environment). A
    file that is present but unreadable or malformed is reported in ``note`` and
    enables *nothing*: a config the operator cannot express clearly must not
    become a capability by accident.
    """
    if not path.exists():
        return [], False, ""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], False, f"{path}: {type(exc).__name__}: {exc}"

    section = data.get(CONFIG_SECTION)
    if section is None:
        return [], False, ""
    if not isinstance(section, dict):
        return [], False, f"{path}: [{CONFIG_SECTION}] must be a table"
    raw_enabled = section.get(CONFIG_ENABLED_KEY, [])
    if isinstance(raw_enabled, str):
        raw_enabled = [raw_enabled]
    if not isinstance(raw_enabled, list) or not all(isinstance(n, str) for n in raw_enabled):
        return [], False, f"{path}: {CONFIG_ENABLED_KEY} must be a list of channel names"
    return (
        parse_channel_list(",".join(raw_enabled)),
        bool(section.get(CONFIG_ALLOW_AUTH_KEY, False)),
        "",
    )


class CLIChannelAdapter(BaseModel):
    """One enabled-or-not upstream command, with its own gate checks."""

    spec: CLIChannelSpec
    enabled: bool = False
    auth_acknowledged: bool = False
    audit: ReachAuditLog = Field(default_factory=ReachAuditLog)
    runner: Runner | None = Field(default=None, exclude=True)
    timeout: float = DEFAULT_TIMEOUT
    max_bytes: int = DEFAULT_MAX_BYTES
    max_chars: int = DEFAULT_MAX_CHARS
    env_allow: tuple[str, ...] = DEFAULT_ENV_ALLOW
    user: str = ""
    model_config = {"arbitrary_types_allowed": True}

    # -- identity ---------------------------------------------------------
    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def description(self) -> str:
        return f"{self.spec.description} (CLI, bypasses EgressPolicy)"

    @property
    def tier(self) -> ReachTier:
        return self.spec.tier

    @property
    def backends(self) -> list[str]:
        return [self.spec.binary]

    def can_handle(self, url: str) -> bool:
        if self.spec.query_kind != "url":
            return False
        try:
            parse_http_target(url)
        except ValueError:
            return False
        return True

    def probe(self) -> ChannelHealth:
        """Local health: is the binary here, and is the channel switched on?"""
        if shutil.which(self.spec.binary) is None:
            return ChannelHealth(
                channel=self.name,
                status=ReachState.OFF,
                message=(
                    f"'{self.spec.binary}' is not on PATH — install the upstream tool first "
                    f"(see {self.spec.docs})"
                ),
                label=self.spec.description,
                tier=self.tier,
                backends=list(self.backends),
                active_backend=None,
            )
        if not self.enabled:
            return ChannelHealth(
                channel=self.name,
                status=ReachState.OFF,
                message=(
                    f"'{self.spec.binary}' is available but the channel is disabled — "
                    f"set {CLI_ENABLE_ENV}={self.name}"
                ),
                label=self.spec.description,
                tier=self.tier,
                backends=list(self.backends),
                active_backend=None,
            )
        if self.spec.requires_auth and not self.auth_acknowledged:
            return ChannelHealth(
                channel=self.name,
                status=ReachState.WARN,
                message=(
                    "enabled, but it runs with the operator's session — "
                    f"acknowledge with {CLI_AUTH_ACK_ENV}=1"
                ),
                label=self.spec.description,
                tier=self.tier,
                backends=list(self.backends),
                active_backend=self.backends[0],
            )
        return ChannelHealth(
            channel=self.name,
            status=ReachState.OK,
            message=(
                "enabled — runs on the host, outside EgressPolicy, and not inside the "
                "network-isolated sandbox"
            ),
            label=self.spec.description,
            tier=self.tier,
            backends=list(self.backends),
            active_backend=self.backends[0],
        )

    # -- argv -------------------------------------------------------------
    @staticmethod
    def validate_query(query: str, *, kind: str) -> str:
        """Reject what an argv list cannot make safe, accept everything else."""
        text = (query or "").strip()
        if not text:
            raise ValueError("query must not be empty")
        if len(text) > MAX_QUERY_CHARS:
            raise ValueError(f"query is longer than {MAX_QUERY_CHARS} characters")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
            raise ValueError("query must not contain control characters or newlines")
        if text.startswith("-"):
            # 'gh search repos --json' style option injection: the upstream tool
            # would read our argument as a flag, so it is refused, not escaped.
            raise ValueError("query must not start with '-' (option injection)")
        if kind == "url":
            scheme, _host, _port = parse_http_target(text)
            if scheme not in {"http", "https"}:  # pragma: no cover - parse guarantees it
                raise ValueError("only http(s) URLs are accepted")
        return text

    def argv_for(self, query: str) -> list[str]:
        """Build the argv for *query* — one element, no shell, no interpolation."""
        text = self.validate_query(query, kind=self.spec.query_kind)
        if self.spec.query_template is not None:
            template = self.spec.query_template
            if template.count("{query}") != 1:
                raise ValueError("query_template must contain exactly one {query}")
            slot = template.replace("{query}", text)
        else:
            slot = text
        return [self.spec.binary, *self.spec.base_args, slot, *self.spec.tail_args]

    # -- execution --------------------------------------------------------
    def _child_env(self, parent: dict[str, str] | None = None) -> dict[str, str]:
        source = parent if parent is not None else dict(os.environ)
        env = {name: source[name] for name in self.env_allow if name in source}
        for name in self.spec.env_passthrough:
            if name in source:
                env[name] = source[name]
        env["PYTHONNOUSERSITE"] = "1"
        return env

    def _deny(self, entry: ReachAuditEntry, code: str, error: str) -> ReachResult:
        entry.decision = "denied"
        entry.code = code
        entry.detail = scrub_credentials(error)[:200]
        self.audit.record(entry)
        return ReachResult(
            channel=self.name,
            ok=False,
            code=code,
            error=scrub_credentials(error),
            correlation_id=entry.correlation_id,
        )

    async def run(
        self,
        query: str,
        *,
        tool: str = "reach_cli",
        correlation_id: str = "",
        env: dict[str, str] | None = None,
    ) -> ReachResult:
        """Run the channel's command for *query*, audited, gated, and capped."""
        entry = ReachAuditEntry.for_call(
            channel=self.name,
            tool=tool,
            query=query or "",
            user=self.user,
            correlation_id=correlation_id,
        )
        if os.environ.get(SANDBOX_MARKER_ENV) == "1":
            return self._deny(
                entry,
                CLI_CHANNEL_SANDBOXED,
                "refused: CLI channels are host-side capabilities and never run inside "
                f"the sandbox ({SANDBOX_MARKER_ENV}=1)",
            )
        if not self.enabled:
            return self._deny(
                entry,
                CLI_CHANNEL_DISABLED,
                f"channel '{self.name}' is disabled; enable it with {CLI_ENABLE_ENV}={self.name}",
            )
        if self.spec.requires_auth and not self.auth_acknowledged:
            return self._deny(
                entry,
                CLI_CHANNEL_AUTH_NOT_ACKNOWLEDGED,
                f"channel '{self.name}' runs with the operator's session; acknowledge "
                f"with {CLI_AUTH_ACK_ENV}=1",
            )
        try:
            argv = self.argv_for(query)
        except ValueError as exc:
            return self._deny(entry, CLI_ARGUMENT_REJECTED, f"rejected query: {exc}")

        runner = self.runner or _default_runner
        started = asyncio.get_running_loop().time()
        try:
            outcome = await runner(argv, self._child_env(env), self.timeout, self.max_bytes)
        except Exception as exc:  # a broken runner is a failed call, not a crash
            entry.decision = "denied"
            entry.code = CLI_RUNNER_ERROR
            entry.detail = f"{type(exc).__name__}: {exc}"
            self.audit.record(entry)
            return ReachResult(
                channel=self.name,
                ok=False,
                code=CLI_RUNNER_ERROR,
                error=scrub_credentials(f"{type(exc).__name__}: {exc}"),
                correlation_id=correlation_id,
            )

        duration_ms = round((asyncio.get_running_loop().time() - started) * 1000, 2)
        return self._finish(entry, outcome, duration_ms)

    def _finish(
        self, entry: ReachAuditEntry, outcome: CLIProcessResult, duration_ms: float
    ) -> ReachResult:
        entry.exit_code = outcome.exit_code
        entry.bytes_out = outcome.bytes_read
        entry.duration_ms = duration_ms
        text = truncate(scrub_credentials(outcome.stdout), self.max_chars)
        items: list[dict[str, Any]] = []
        if self.spec.json_output and outcome.stdout.strip():
            try:
                parsed = json.loads(outcome.stdout)
                items = [parsed] if isinstance(parsed, dict) else list(parsed)
            except json.JSONDecodeError:
                items = []

        code = ""
        error = ""
        ok = outcome.exit_code == 0
        if outcome.timed_out:
            ok, code = False, CLI_TIMEOUT
            error = f"'{self.spec.binary}' timed out after {self.timeout}s"
        elif not ok:
            code = CLI_NONZERO_EXIT
            stderr_tail = truncate(scrub_credentials(outcome.stderr.strip()), 400)
            error = f"'{self.spec.binary}' exited with {outcome.exit_code}: {stderr_tail}"

        entry.decision = "allowed" if ok else "denied"
        entry.code = code
        entry.detail = error[:200]
        self.audit.record(entry)
        return ReachResult(
            channel=self.name,
            ok=ok,
            text=text,
            items=items,
            code=code,
            error=error,
            exit_code=outcome.exit_code,
            bytes_out=outcome.bytes_read,
            truncated=outcome.truncated,
            duration_ms=duration_ms,
            correlation_id=entry.correlation_id,
        )


async def _default_runner(
    argv: list[str], env: dict[str, str], timeout_s: float, max_bytes: int
) -> CLIProcessResult:
    """Execute *argv* with no shell, a byte budget and a deadline.

    Output is read in chunks and the process is killed the moment the budget is
    exceeded — ``communicate()`` would buffer an unbounded stream in memory
    first, which is the failure mode this cap exists to prevent.
    """
    kwargs: dict[str, Any] = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True  # kill the whole group, not just the leader
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=tempfile.gettempdir(),
            **kwargs,
        )
    except FileNotFoundError:
        return CLIProcessResult(exit_code=None, stderr=f"binary not found: {argv[0]}")

    async def drain(stream: Any, budget: int) -> tuple[bytes, bool]:
        if stream is None:  # pragma: no cover - PIPE always yields a stream
            return b"", False
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > budget:
                return b"".join(chunks)[:budget], True
        return b"".join(chunks), False

    async def collect() -> tuple[bytes, bytes, bool]:
        stdout, over = await drain(process.stdout, max_bytes)
        if over:
            return stdout, b"", True
        stderr, _ = await drain(process.stderr, max_bytes)
        return stdout, stderr, False

    try:
        stdout_raw, stderr_raw, over = await asyncio.wait_for(collect(), timeout=timeout_s)
        if over:
            await _kill_and_reap(process)
            return CLIProcessResult(
                exit_code=None,
                stdout=stdout_raw.decode("utf-8", errors="replace"),
                stderr=f"output exceeded the {max_bytes}-byte budget; process killed",
                bytes_read=len(stdout_raw),
                truncated=True,
            )
        exit_code = await process.wait()
        _close_transport(process)
    except TimeoutError:
        await _kill_and_reap(process)
        return CLIProcessResult(exit_code=None, timed_out=True)
    except BaseException:
        # Cancelled or failing caller: do not leave a host-side program running
        # (or its transport open) just because nobody is waiting for the result.
        if process.returncode is None:
            _terminate(process)
        _close_transport(process)
        raise
    return CLIProcessResult(
        exit_code=exit_code,
        stdout=stdout_raw.decode("utf-8", errors="replace"),
        stderr=stderr_raw.decode("utf-8", errors="replace"),
        bytes_read=len(stdout_raw) + len(stderr_raw),
    )


def _close_transport(process: Any) -> None:
    """Release the subprocess transport before its loop goes away.

    Without this, a transport that outlives the event loop is finalized at some
    later, unrelated moment and raises "Event loop is closed" from ``__del__`` —
    an unraisable exception that surfaces as a warning in whatever code happens
    to trigger the collection. The asyncio API has no public close, so the
    transport is closed directly.
    """
    transport = getattr(process, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()


def _terminate(process: Any) -> None:
    with contextlib.suppress(ProcessLookupError):  # already gone
        process.kill()


async def _kill_and_reap(process: Any, *, grace: float = 5.0) -> int | None:
    """Kill *process* and reap it without deadlocking on a paused pipe.

    asyncio pauses a subprocess pipe when its reader stops consuming. A paused
    transport never observes EOF, so ``await process.wait()`` would block for
    good even though the child is dead — the exact failure mode that makes a
    "bounded" output cap hang the server instead of protecting it. Draining to
    EOF first is cheap (the child is gone; only the pipe buffer is left) and
    lets the transport finish.
    """
    _terminate(process)

    async def settle() -> None:
        for stream in (process.stdout, process.stderr):
            if stream is None:  # pragma: no cover - PIPE always yields a stream
                continue
            while await stream.read(65536):
                pass
        await process.wait()

    # The kill already happened: a slow reap must never turn into an error.
    with contextlib.suppress(Exception):
        await asyncio.wait_for(settle(), timeout=grace)
    _close_transport(process)
    return process.returncode


class CLILayer(BaseModel):
    """The CLI side of the reach layer: specs, gates, audit, one report."""

    adapters: dict[str, CLIChannelAdapter] = Field(default_factory=dict)
    audit: ReachAuditLog = Field(default_factory=ReachAuditLog)
    user: str = ""
    #: Where enablement was read from (written to by ``cli reach enable``).
    config_path: Path | None = None
    #: Human-readable problems with that file; surfaced, never raised.
    notes: list[str] = Field(default_factory=list)
    #: Set by :func:`build_reach_layer`; typed ``Any`` because the bridge holds a
    #: reference to this very layer.
    bridge: Any = None
    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def from_env(
        cls,
        *,
        audit: ReachAuditLog | None = None,
        runner: Runner | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        config_path: Path | None = None,
    ) -> CLILayer:
        """Build the layer from the environment and ``reach.toml``.

        Both sources are unioned: a channel enabled in either place is on, and
        neither place can overrule the other's *absence* into enablement. With
        both empty the layer is inert — that is the default state.
        """
        source = env if env is not None else dict(os.environ)
        path = config_path if config_path is not None else default_config_path(source)
        from_config, config_auth, note = load_cli_config(path)
        requested = [*from_config, *parse_channel_list(source.get(CLI_ENABLE_ENV, ""))]
        enable_all = "*" in requested
        auth_ack = config_auth or source.get(CLI_AUTH_ACK_ENV, "").strip() in {"1", "true", "yes"}
        user = source.get("CELA_REACH_USER") or source.get("USER") or source.get("USERNAME") or ""
        resolved_timeout = timeout
        if resolved_timeout is None:
            try:
                resolved_timeout = float(source.get(CLI_TIMEOUT_ENV, DEFAULT_TIMEOUT))
            except ValueError:
                resolved_timeout = DEFAULT_TIMEOUT

        log = audit if audit is not None else ReachAuditLog(path=default_audit_path())
        adapters = {
            name: CLIChannelAdapter(
                spec=spec,
                enabled=enable_all or name in requested,
                auth_acknowledged=auth_ack,
                audit=log,
                runner=runner,
                timeout=resolved_timeout,
                user=user,
            )
            for name, spec in CLI_CHANNEL_SPECS.items()
        }
        return cls(
            adapters=adapters,
            audit=log,
            user=user,
            config_path=path,
            notes=[note] if note else [],
        )

    def names(self) -> list[str]:
        return list(self.adapters)

    def get(self, name: str) -> CLIChannelAdapter | None:
        return self.adapters.get(name)

    def enabled_channels(self) -> list[str]:
        return [name for name, adapter in self.adapters.items() if adapter.enabled]

    def is_active(self) -> bool:
        """True when the operator has switched any CLI channel on."""
        return bool(self.enabled_channels())

    def coverage(self) -> dict[str, Any]:
        """Honest coverage: verified commands vs upstream channels vs monitored."""
        return {
            "verified_commands": sorted(self.adapters),
            "upstream_channels": list(KNOWN_UPSTREAM_CHANNELS),
            "verified": len(self.adapters),
            "upstream": len(KNOWN_UPSTREAM_CHANNELS),
            "enabled": self.enabled_channels(),
            "config_path": str(self.config_path) if self.config_path else "",
            "notes": list(self.notes),
        }

    def report(self) -> ReachReport:
        """Health of every CLI channel; a config problem is added as its own row."""
        report = ReachReport(
            channels={a.name: a.probe() for a in self.adapters.values()},
            source="cli",
        )
        if self.notes:
            report.channels["cli_config"] = ChannelHealth(
                channel="cli_config",
                status=ReachState.ERROR,
                message="; ".join(self.notes),
                label=str(self.config_path or ""),
                tier=ReachTier.SETUP,
                backends=[str(self.config_path or "")],
                active_backend=None,
            )
        return report

    async def run(
        self, channel: str, query: str, *, tool: str = "reach_cli", correlation_id: str = ""
    ) -> ReachResult:
        adapter = self.adapters.get(channel)
        if adapter is None:
            if channel in KNOWN_UPSTREAM_CHANNELS:
                return ReachResult(
                    channel=channel,
                    ok=False,
                    code=CLI_CHANNEL_NOT_IMPLEMENTED,
                    error=(
                        f"'{channel}' has no verified command in this build; run the upstream "
                        "tool directly or add a spec (see docs/REACH_PLAN.md §6)"
                    ),
                )
            return ReachResult(
                channel=channel,
                ok=False,
                code=CLI_CHANNEL_UNKNOWN,
                error=(f"unknown channel '{channel}'; known: {', '.join(sorted(self.adapters))}"),
            )
        return await adapter.run(query, tool=tool, correlation_id=correlation_id)


class AgentReachBridge(BaseModel):
    """Read the upstream project's own health report (``doctor --json``).

    Doctor executes the upstream binary, so it is behind the same switch as the
    channels themselves: the operator who never enabled a CLI channel has not
    agreed to run one — not even for a status check.
    """

    binary: str = "agent-reach"
    layer: CLILayer = Field(default_factory=CLILayer)
    runner: Runner | None = Field(default=None, exclude=True)
    timeout: float = 120.0
    max_bytes: int = 2 * 1024 * 1024
    model_config = {"arbitrary_types_allowed": True}

    def installed(self) -> bool:
        return shutil.which(self.binary) is not None

    def allowed(self) -> bool:
        return self.layer.is_active() or os.environ.get(CLI_DOCTOR_ENV, "").strip() in {
            "1",
            "true",
            "yes",
        }

    def probe(self) -> ChannelHealth:
        if not self.installed():
            return ChannelHealth(
                channel="agent_reach",
                status=ReachState.OFF,
                message=(
                    f"'{self.binary}' is not installed; install the optional extra with "
                    'pip install -e ".[dev,server,reach]"'
                ),
                label="upstream channel health",
                tier=ReachTier.SETUP,
                backends=[self.binary],
                active_backend=None,
            )
        return ChannelHealth(
            channel="agent_reach",
            status=ReachState.OK if self.allowed() else ReachState.WARN,
            message=(
                "installed; doctor is allowed"
                if self.allowed()
                else f"installed; doctor needs {CLI_ENABLE_ENV} or {CLI_DOCTOR_ENV}=1"
            ),
            label="upstream channel health",
            tier=ReachTier.SETUP,
            backends=[self.binary],
            active_backend=self.binary,
        )

    async def doctor(self, *, correlation_id: str = "") -> ReachReport:
        """Run ``agent-reach doctor --json`` and parse it into our report."""
        entry = ReachAuditEntry.for_call(
            channel="agent_reach",
            tool="reach_doctor",
            query="doctor --json",
            user=self.layer.user,
            correlation_id=correlation_id,
        )
        if not self.allowed():
            entry.decision = "denied"
            entry.code = CLI_CHANNEL_DISABLED
            self.layer.audit.record(entry)
            raise RuntimeError(
                f"agent-reach doctor is gated: set {CLI_ENABLE_ENV}=<channel,...> "
                f"or {CLI_DOCTOR_ENV}=1 first"
            )
        if not self.installed():
            entry.decision = "denied"
            entry.code = CLI_BINARY_MISSING
            self.layer.audit.record(entry)
            raise RuntimeError(
                f"'{self.binary}' is not installed; install the optional extra with "
                'pip install -e ".[dev,server,reach]"'
            )

        parent_env = dict(os.environ)
        env = {name: parent_env[name] for name in DEFAULT_ENV_ALLOW if name in parent_env}
        runner = self.runner or _default_runner
        outcome = await runner([self.binary, "doctor", "--json"], env, self.timeout, self.max_bytes)
        entry.exit_code = outcome.exit_code
        entry.bytes_out = outcome.bytes_read
        entry.decision = "allowed" if outcome.exit_code == 0 else "denied"
        entry.code = "" if outcome.exit_code == 0 else CLI_NONZERO_EXIT
        entry.detail = truncate(scrub_credentials(outcome.stderr.strip()), 200)
        self.layer.audit.record(entry)

        if outcome.exit_code != 0:
            raise RuntimeError(
                f"'{self.binary} doctor --json' exited with {outcome.exit_code}: "
                f"{truncate(scrub_credentials(outcome.stderr.strip()), 200)}"
            )
        try:
            payload = json.loads(outcome.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"'{self.binary} doctor --json' did not emit JSON: {exc}") from None
        if not isinstance(payload, dict):
            raise RuntimeError(f"'{self.binary} doctor --json' emitted {type(payload).__name__}")
        report = parse_doctor_json(payload, source=f"{self.binary}:doctor")
        missing = [
            name for name in report.channels if report.channels[name].status is ReachState.OFF
        ]
        if missing:  # keep the finding visible instead of silently dropping it
            entry.detail = truncate(f"channels off: {', '.join(sorted(missing))}", 200)
        return report


def build_reach_layer(
    *,
    policy: Any = None,
    transport: Any = None,
    audit: ReachAuditLog | None = None,
    cli: CLILayer | None = None,
    bridge: AgentReachBridge | None = None,
) -> ReachRegistry:
    """Native channels plus a CLI layer attached to the same registry.

    The registry keeps routing: native channels answer URLs, CLI channels answer
    named commands. ``reach.cli`` is where the CLI side lives for the callers.
    """
    layer = cli if cli is not None else CLILayer.from_env(audit=audit)
    upstream = bridge if bridge is not None else AgentReachBridge(layer=layer)
    layer.bridge = upstream
    registry = ReachRegistry(cli=layer, bridge=upstream)
    for channel in native_channels(policy=policy, transport=transport):
        registry.register(channel)
    return registry
