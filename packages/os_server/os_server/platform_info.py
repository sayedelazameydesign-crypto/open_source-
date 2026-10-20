"""Read-only deployment facts for the four platform console pages.

The web console shows four sections — App Launchpad, Devbox, Database and
Image Registry. None of them is a control plane: this repository cannot
create ClawCloud apps, start devboxes, create databases or push images from
a browser. What it *can* do is state, truthfully and without secrets, what
the running process actually is:

* which image/build metadata the container was started with,
* which code-execution backend is selected and whether it executes anything,
* which ``RunStore`` is in use and how durable it is,
* where the outcome ledger lives and how much it holds.

Every value below is either read from the process environment or from an
object that already exists in the process. Nothing is probed over the
network, nothing is written, and no environment variable is echoed unless it
passes :func:`_safe_text` / :func:`describe_image_reference` (no userinfo, no
whitespace, bounded length). Unknown stays unknown rather than guessed: a
missing image reference is reported as unconfigured, never invented from a
tag that "probably" exists.
"""

from __future__ import annotations

import os
import platform as platform_module
import re
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from os_core.ledger import RunLedger
from os_core.models import ExecutionInfo

from os_server.storage import (
    DistributedRunStore,
    PostgresRunStore,
    SQLiteRunStore,
)

__all__ = ["collect_platform_info", "describe_image_reference"]

#: Approximate process start. Set once at import time, which is close enough
#: to uvicorn's start for an "uptime" label that never claims more precision.
_PROCESS_STARTED_AT = datetime.now(UTC)
_PROCESS_STARTED_MONOTONIC = time.monotonic()

_MAX_TEXT = 200
_IMAGE_REF_RE = re.compile(
    r"^(?:(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::\d{1,5})?)/)?"
    r"(?P<repository>[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*)"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]{0,127}))?"
    r"(?:@(?P<digest>sha256:[a-f0-9]{64}))?$"
)
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


def _safe_text(value: object, *, max_len: int = _MAX_TEXT) -> str | None:
    """A bounded, single-token string; anything else is treated as absent.

    ``@`` is rejected outright because it is the userinfo separator in a URL
    and a credential separator everywhere else — no environment value that
    carries it is safe to echo in a UI.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > max_len:
        return None
    if "@" in text:
        return None
    if any(character.isspace() or ord(character) < 32 for character in text):
        return None
    return text


def _safe_sha(value: object) -> str | None:
    text = _safe_text(value, max_len=40)
    if text is None or not _SHA_RE.match(text):
        return None
    return text


def _safe_timestamp(value: object) -> str | None:
    text = _safe_text(value, max_len=40)
    if text is None or not _ISO_RE.match(text):
        return None
    return text


def describe_image_reference(reference: object) -> dict[str, Any]:
    """Split an OCI image reference into host / repository / tag / digest.

    Returns ``{"valid": False, "reference": None}`` for anything that is not
    a syntactically valid, credential-free reference. The raw value is never
    echoed when it fails validation.
    """
    if reference is None:
        return {"valid": False, "reference": None}
    text = str(reference).strip()
    match = _IMAGE_REF_RE.fullmatch(text)
    if match is None or len(text) > 300:
        return {"valid": False, "reference": None}
    return {
        "valid": True,
        "reference": text,
        "host": match.group("host"),
        "repository": match.group("repository"),
        "tag": match.group("tag"),
        "digest": match.group("digest"),
    }


def _registry_info(env: Mapping[str, str]) -> dict[str, Any]:
    parsed = describe_image_reference(env.get("CELA_IMAGE_REF"))
    explicit_invalid = bool(env.get("CELA_IMAGE_REF") and not parsed["valid"])
    if not parsed["valid"]:
        return {
            "configured": False,
            "source": "CELA_IMAGE_REF" if env.get("CELA_IMAGE_REF") else None,
            "ignored_invalid_reference": explicit_invalid,
            "reference": None,
            "host": None,
            "repository": None,
            "tag": None,
            "digest": None,
            "build_sha": _safe_sha(env.get("CELA_BUILD_SHA") or env.get("GITHUB_SHA")),
            "build_time": _safe_timestamp(env.get("CELA_BUILD_TIME")),
        }
    tag = parsed["tag"] or _safe_text(env.get("CELA_IMAGE_TAG"), max_len=128)
    digest = parsed["digest"] or _safe_sha256_digest(env.get("CELA_IMAGE_DIGEST"))
    return {
        "configured": True,
        "source": "CELA_IMAGE_REF",
        "ignored_invalid_reference": False,
        "reference": parsed["reference"],
        "host": parsed["host"],
        "repository": parsed["repository"],
        "tag": tag,
        "digest": digest,
        "build_sha": _safe_sha(env.get("CELA_BUILD_SHA") or env.get("GITHUB_SHA")),
        "build_time": _safe_timestamp(env.get("CELA_BUILD_TIME")),
    }


def _safe_sha256_digest(value: object) -> str | None:
    text = _safe_text(value, max_len=71)
    if text is None or not text.startswith("sha256:"):
        return None
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", text):
        return None
    return text


def _sandbox_info(env: Mapping[str, str]) -> dict[str, Any]:
    mode = (env.get("CELA_CODE_EXEC_MODE") or "local").strip().lower()
    requested_backend = (env.get("CELA_CODE_EXEC_BACKEND") or "").strip().lower() or None
    backend: str | None = None
    error: str | None = None
    try:
        # Imported lazily: the server package must not depend on os_tools'
        # optional SDKs just to answer a status request.
        from os_tools.code_exec import code_exec_backend_from_env

        backend = type(code_exec_backend_from_env(dict(env))).__name__
    except Exception:  # pragma: no cover - misconfiguration is reported, not raised
        error = "code-execution backend could not be resolved from the environment"
    profiles = {
        "disabled": ("none", False, "n/a"),
        "local": ("subprocess-setrlimit", True, "unverified"),
        "e2b": ("e2b-microvm", True, "sandbox-policy"),
        "firecracker": ("firecracker-microvm", True, "sandbox-policy"),
        "production": ("remote-microvm", True, "sandbox-policy"),
    }
    isolation, executes, network = profiles.get(
        mode, ("unknown", False, "unknown")
    )
    if error is not None:
        isolation, executes, network = "unknown", False, "unknown"
    return {
        "code_exec_mode": mode,
        "requested_backend": requested_backend,
        "backend": backend,
        "configuration_error": error,
        "executes_untrusted_code": executes,
        "isolation": isolation,
        "network_isolation": network,
    }


def _safe_dsn(dsn: str) -> str | None:
    """Scheme + host + port + path only.

    The userinfo (``user:password@``) and the whole query string are dropped
    rather than redacted in place: a redacted DSN still reads like a usable
    connection string, while this one names the target and proves nothing
    about credentials.
    """
    try:
        parts = urlsplit(dsn)
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    netloc = parts.hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    return _safe_text(f"{parts.scheme}://{netloc}{parts.path}", max_len=300)


def _store_target(store: object) -> str | None:
    if isinstance(store, DistributedRunStore):
        return _store_target(store.durable)
    if isinstance(store, PostgresRunStore):
        return _safe_dsn(store.dsn) if store.dsn else None
    if isinstance(store, SQLiteRunStore):
        return _safe_text(str(store.path), max_len=300)
    return None


def _store_info(store: object, ledger: RunLedger | None) -> dict[str, Any]:
    if isinstance(store, DistributedRunStore):
        scope, durable = "postgres+redis", True
    elif isinstance(store, PostgresRunStore):
        scope, durable = "postgres", True
    elif isinstance(store, SQLiteRunStore):
        scope, durable = "disk", True
    else:
        scope, durable = "process", False
    return {
        "backend": type(store).__name__,
        "scope": scope,
        "durable": durable,
        "target": _store_target(store),
        "ledger": ledger.describe() if ledger is not None else {"enabled": False},
    }


def _deployment_info(env: Mapping[str, str], *, live_provider: bool) -> dict[str, Any]:
    raw_port = env.get("PORT") or env.get("CELA_PORT") or "8080"
    try:
        listen_port: int | None = int(raw_port)
    except (TypeError, ValueError):
        listen_port = None
    return {
        "mode": "live" if live_provider else "demo",
        "app_name": _safe_text(env.get("CELA_APP_NAME"), max_len=64) or "celia-mission-os",
        "listen_port": listen_port,
        "region": _safe_text(env.get("CELA_REGION"), max_len=64)
        or _safe_text(env.get("VERCEL_REGION"), max_len=64),
        "public_url_configured": bool(env.get("CELA_PUBLIC_URL")),
    }


def collect_platform_info(
    *,
    store: object,
    ledger: RunLedger | None,
    execution: ExecutionInfo,
    live_provider: bool,
    version: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the console payload from the live process (never from a guess)."""
    source = os.environ if env is None else env
    uptime = max(0.0, time.monotonic() - _PROCESS_STARTED_MONOTONIC)
    return {
        "status": "ok",
        "product": "Celia.sh — Mission OS",
        "version": version,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "process": {
            "started_at": _PROCESS_STARTED_AT.isoformat().replace("+00:00", "Z"),
            "uptime_seconds": round(uptime, 1),
            "python_version": platform_module.python_version(),
            "python_implementation": sys.implementation.name,
            "platform": platform_module.system().lower(),
            "machine": _safe_text(platform_module.machine(), max_len=32),
        },
        "deployment": _deployment_info(source, live_provider=live_provider),
        "execution": execution.model_dump(),
        "sandbox": _sandbox_info(source),
        "store": _store_info(store, ledger),
        "registry": _registry_info(source),
    }
