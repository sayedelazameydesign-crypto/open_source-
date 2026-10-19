"""Whole-system secret file (``.env``) loading — Render Secret Files (ADR-0021).

Render secret files are plain files an operator uploads in the Dashboard
(Blueprints cannot declare their contents). A file named ``.env`` is available
during builds and at runtime from the app's root, and at the absolute path
``/etc/secrets/.env``. This module is the single bridge from that file to the
rest of the system: every subsystem already reads its configuration from
``os.environ`` (tokens in :mod:`os_server.runtime`, provider keys in
``os_server.demo``, WaAPI credentials, the run-store URL, …), so the loader
copies file entries into the environment **without overwriting values that are
already set**, and from then on the existing env-based code paths see them.

Rules, all deliberate:

* **Real environment wins.** A variable already present — even on a platform
  that also mounts the file at both paths — is never replaced by the file.
  Render env vars / ``sync: false`` secrets therefore keep working unchanged,
  and the file is only a fallback source for what the environment lacks.
* **Candidates, in order:** ``$CELA_SECRET_FILE`` (an explicit path; ``off``
  disables loading), ``./.env`` (app root / working directory), then
  ``/etc/secrets/.env``. When both default paths exist, the earlier one wins
  per key and the later one only fills gaps — on Render they are the same
  uploaded file, so this ordering is unobservable there.
* **Never logged, returned or committed.** The load result carries key
  *names* and paths only. Parse errors name the file and line number but
  never the line's content, which may be a credential.
* **Fail closed only when told to.** A missing *default* file is the normal
  env-only deployment and is a silent no-op. An explicit ``$CELA_SECRET_FILE``
  that is missing or malformed raises :class:`SecretFileError` — the operator
  named that file, so starting without it would be a silent misconfiguration.
* **Small, literal format.** ``KEY=VALUE`` lines, ``#`` comments, blank lines,
  an optional ``export `` prefix, and single/double quotes. No variable
  expansion and no shell evaluation: values are literal credentials, not code.
"""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

SECRET_FILE_ENV_VAR: str = "CELA_SECRET_FILE"
SECRET_FILE_NAME: str = ".env"
SECRET_FILE_DIR: Path = Path("/etc/secrets")

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DISABLE_VALUES = {"off", "none", "disabled"}


class SecretFileError(ValueError):
    """A secret file was requested but could not be loaded (never echoes values)."""


@dataclass(frozen=True)
class SecretFileLoad:
    """What was loaded: file paths and key *names* only, so it is safe to log."""

    paths: tuple[Path, ...] = ()
    keys: tuple[str, ...] = ()

    @property
    def loaded(self) -> bool:
        return bool(self.paths)


def _unescape_double_quoted(body: str, *, source: str, lineno: int) -> str:
    out: list[str] = []
    escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "$": "$"}
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        if i + 1 >= len(body) or body[i + 1] not in escapes:
            raise SecretFileError(f"{source}:{lineno}: unsupported escape in quoted value")
        out.append(escapes[body[i + 1]])
        i += 2
    return "".join(out)


def _parse_value(raw: str, *, source: str, lineno: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        end = value.find(quote, 1) if quote == "'" else _find_double_quote_end(value)
        if end == -1:
            raise SecretFileError(f"{source}:{lineno}: unterminated quoted value")
        trailing = value[end + 1 :].strip()
        if trailing and not trailing.startswith("#"):
            raise SecretFileError(f"{source}:{lineno}: unexpected text after quoted value")
        body = value[1:end]
        return body if quote == "'" else _unescape_double_quoted(body, source=source, lineno=lineno)
    comment = value.find(" #")
    if comment != -1:
        value = value[:comment]
    return value.strip()


def _find_double_quote_end(value: str) -> int:
    i = 1
    while i < len(value):
        if value[i] == "\\":
            i += 2
            continue
        if value[i] == '"':
            return i
        i += 1
    return -1


def parse_secret_file(text: str, *, source: str = "<secret file>") -> dict[str, str]:
    """Parse ``.env`` content into a mapping. Values are literals; later keys win.

    Raises :class:`SecretFileError` naming only the source and line number —
    never the offending line, which may itself be a credential.
    """
    parsed: dict[str, str] = {}
    for lineno, raw_line in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, raw_value = line.partition("=")
        if not separator:
            raise SecretFileError(f"{source}:{lineno}: expected KEY=VALUE")
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            raise SecretFileError(f"{source}:{lineno}: invalid variable name")
        parsed[key] = _parse_value(raw_value, source=source, lineno=lineno)
    return parsed


def candidate_secret_files(
    env: MutableMapping[str, str] | None = None,
    *,
    cwd: str | Path | None = None,
) -> tuple[Path, ...]:
    """The files to try, in precedence order (see the module docstring)."""
    source = os.environ if env is None else env
    base = Path.cwd() if cwd is None else Path(cwd)
    explicit = (source.get(SECRET_FILE_ENV_VAR) or "").strip()
    if explicit:
        if explicit.lower() in _DISABLE_VALUES:
            return ()
        path = Path(explicit).expanduser()
        return (path if path.is_absolute() else base / path,)
    candidates: list[Path] = []
    for path in (base / SECRET_FILE_NAME, SECRET_FILE_DIR / SECRET_FILE_NAME):
        if path not in candidates:
            candidates.append(path)
    return tuple(candidates)


def load_secret_files(
    env: MutableMapping[str, str] | None = None,
    *,
    cwd: str | Path | None = None,
) -> SecretFileLoad:
    """Fill missing entries of *env* (default ``os.environ``) from the secret file.

    Existing entries always win; among files, earlier candidates win per key.
    A missing default candidate is skipped silently; an explicit
    ``$CELA_SECRET_FILE`` that is missing or unreadable raises
    :class:`SecretFileError` without echoing any file content.
    """
    target: MutableMapping[str, str] = os.environ if env is None else env
    paths = candidate_secret_files(target, cwd=cwd)
    explicit = bool((target.get(SECRET_FILE_ENV_VAR) or "").strip()) and bool(paths)
    loaded_paths: list[Path] = []
    loaded_keys: list[str] = []
    for path in paths:
        if not path.is_file():
            if explicit:
                raise SecretFileError(f"secret file not found: {path}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SecretFileError(f"secret file is not readable: {path}") from exc
        for key, value in parse_secret_file(text, source=str(path)).items():
            if key not in target:
                target[key] = value
                loaded_keys.append(key)
        loaded_paths.append(path)
    return SecretFileLoad(paths=tuple(loaded_paths), keys=tuple(loaded_keys))


__all__ = [
    "SECRET_FILE_DIR",
    "SECRET_FILE_ENV_VAR",
    "SECRET_FILE_NAME",
    "SecretFileError",
    "SecretFileLoad",
    "candidate_secret_files",
    "load_secret_files",
    "parse_secret_file",
]
