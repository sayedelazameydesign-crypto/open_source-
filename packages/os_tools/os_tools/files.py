"""Read-only exploration tools — a sub-agent's hands, and the only ones it gets.

The explorer sub-agent (ADR-0020) exists so the main agent can delegate the
routine work — walking the tree, grepping for a symbol — to a cheap local model.
Delegation is only safe if the delegate *cannot* widen its mandate, so both
tools here are:

* **root-confined** — every path resolves under one workspace root, and the
  check runs on the resolved path, so ``..`` and symlink escapes both fail;
* **denylisted for secrets** — ``.env``, key material and credential stores are
  refused before the read, and what is returned still passes through
  :func:`~os_core.redact.scrub_credentials` (defence in depth: a secret pasted
  into a tracked file must not reach a model prompt through ``file_reader``);
* **bounded** — capped bytes in, capped hits out; a grep that may return the
  whole repository is a denial-of-service against the caller's context window.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from pathlib import Path

from os_core.redact import scrub_credentials

#: Names that are never readable through these tools, in any directory.
_SENSITIVE_NAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".gitconfig",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "secrets.json",
        "credentials.json",
    }
)
_SENSITIVE_SUFFIXES: frozenset[str] = frozenset({".pem", ".key", ".p12", ".pfx", ".kdbx"})
#: ``.env.example`` and friends are templates — reading them is documentation,
#: not exfiltration. The bare ``.env`` and ``.env.production`` are not.
_ENV_TEMPLATE_SUFFIXES: tuple[str, ...] = (".example", ".sample", ".template")
#: Directories the walkers never enter. ``.git`` holds credentials on some
#: setups (``.git/credentials``) and is noise for exploration anyway.
_SKIPPED_DIRS: frozenset[str] = frozenset({".git", ".hg", ".svn", "__pycache__", "node_modules"})

MAX_READ_BYTES = 256_000
MAX_GREP_FILE_BYTES = 1_000_000
MAX_GREP_HITS = 200
DEFAULT_GREP_HITS = 50


def _is_sensitive(path: Path) -> bool:
    name = path.name
    if name in _SENSITIVE_NAMES:
        return True
    if name.startswith(".env") and not name.endswith(_ENV_TEMPLATE_SUFFIXES):
        return True
    return path.suffix.lower() in _SENSITIVE_SUFFIXES


def resolve_under(root: str | Path, relative: str) -> Path:
    """Resolve *relative* under *root*, or refuse.

    Refusal is the feature: a sub-agent given ``file_reader`` may read the
    workspace and nothing else — not ``/etc``, not the parent checkout, and not
    a symlink that points there.
    """
    if not relative or not relative.strip():
        raise ValueError("path is required")
    base = Path(root).resolve()
    candidate = Path(relative)
    resolved = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if resolved != base and base not in resolved.parents:
        raise PermissionError(f"path escapes the workspace root: {relative}")
    return resolved


def _readable_text_file(path: Path, *, max_bytes: int) -> tuple[str, bool]:
    raw = path.read_bytes()
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    if b"\x00" in raw:
        raise ValueError(f"binary file refused: {path.name}")
    return raw.decode("utf-8", errors="replace"), truncated


def _walk_files(root: Path, glob: str | None) -> Iterable[Path]:
    for current, dirs, files in os_walk_sorted(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIPPED_DIRS)
        for name in files:
            if glob and not fnmatch.fnmatch(name, glob):
                continue
            path = Path(current) / name
            if _is_sensitive(path):
                continue
            yield path


def os_walk_sorted(root: Path) -> Iterable[tuple[str, list[str], list[str]]]:
    """``os.walk`` with deterministic ordering — replayable tool output."""
    import os

    for current, dirs, files in os.walk(root):
        dirs.sort()
        files.sort()
        yield current, dirs, files


def file_reader_tool(
    root: str | Path = ".",
    *,
    max_bytes: int = MAX_READ_BYTES,
    redact: bool = True,
):
    """Build the ``file_reader`` tool confined to *root*."""
    from os_core.types import RiskLevel

    from os_tools.registry import Tool

    workspace = Path(root).resolve()

    def file_reader(path: str, offset: int = 1, limit: int = 400) -> dict[str, object]:
        """Read a text file from the workspace (line window; read-only)."""
        resolved = resolve_under(workspace, path)
        if not resolved.is_file():
            raise ValueError(f"not a file: {path}")
        if _is_sensitive(resolved):
            raise PermissionError(f"refused (sensitive file): {path}")
        text, truncated = _readable_text_file(resolved, max_bytes=max_bytes)
        if redact:
            text = scrub_credentials(text)
        lines = text.splitlines()
        start = max(1, int(offset)) - 1
        window = int(limit) if int(limit) > 0 else len(lines)
        excerpt = lines[start : start + window]
        return {
            "path": str(path),
            "line_count": len(lines),
            "offset": start + 1,
            "returned_lines": len(excerpt),
            "truncated": truncated or (start + window < len(lines)),
            "text": "\n".join(excerpt),
        }

    return Tool(
        name="file_reader",
        description=(
            "Read a text file from the workspace root (read-only, line-windowed). "
            "Secrets and paths outside the root are refused."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "path relative to the workspace root"},
                "offset": {"type": "integer", "description": "1-based first line (default 1)"},
                "limit": {"type": "integer", "description": "max lines to return (default 400)"},
            },
            "required": ["path"],
        },
        risk=RiskLevel.READ_ONLY,
        fn=file_reader,
    )


def grep_search_tool(
    root: str | Path = ".",
    *,
    max_hits: int = DEFAULT_GREP_HITS,
    max_file_bytes: int = MAX_GREP_FILE_BYTES,
    redact: bool = True,
):
    """Build the ``grep_search`` tool confined to *root*."""
    from os_core.types import RiskLevel

    from os_tools.registry import Tool

    workspace = Path(root).resolve()
    ceiling = max(1, min(int(max_hits), MAX_GREP_HITS))

    def grep_search(pattern: str, glob: str = "*", max_hits: int = ceiling) -> dict[str, object]:
        """Regex-search text files under the workspace root (read-only)."""
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        cap = max(1, min(int(max_hits), MAX_GREP_HITS))
        hits: list[dict[str, object]] = []
        scanned = 0
        for path in _walk_files(workspace, glob):
            try:
                if path.stat().st_size > max_file_bytes:
                    continue
                text, _ = _readable_text_file(path, max_bytes=max_file_bytes)
            except (OSError, ValueError):
                continue  # unreadable or binary: the walk must not die on one file
            scanned += 1
            rel = path.relative_to(workspace)
            for line_no, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    payload = scrub_credentials(line) if redact else line
                    hits.append({"path": str(rel), "line": line_no, "text": payload.strip()[:300]})
                    if len(hits) >= cap:
                        return {
                            "pattern": pattern,
                            "hits": hits,
                            "truncated": True,
                            "scanned": scanned,
                        }
        return {"pattern": pattern, "hits": hits, "truncated": False, "scanned": scanned}

    return Tool(
        name="grep_search",
        description=(
            "Regex-search text files under the workspace root (read-only). "
            "Returns path + line number + line text, capped and secret-scrubbed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regex applied per line"},
                "glob": {
                    "type": "string",
                    "description": "filename glob, e.g. '*.py' (default '*')",
                },
                "max_hits": {"type": "integer", "description": "cap on returned hits"},
            },
            "required": ["pattern"],
        },
        risk=RiskLevel.READ_ONLY,
        fn=grep_search,
    )


__all__ = [
    "file_reader_tool",
    "grep_search_tool",
    "resolve_under",
]
