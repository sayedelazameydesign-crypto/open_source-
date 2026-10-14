"""Reach surfaces — ``/api/reach/*`` and ``/cela/v1/reach/*`` (ADR-0013 phase 3).

Two tiers, two stories, one authentication story:

* **Native channels** (web, rss, github) answer URLs. Every hop is checked
  against an :class:`~os_tools.egress.EgressPolicy` — by default the *reach*
  policy (public internet minus the SSRF floor, narrowed with
  ``CELA_REACH_ALLOW_HOSTS``/``CELA_REACH_DENY_HOSTS``), which is deliberately
  separate from the browser's policy and is the same one the reach *tools* use.
  A deployment that wants reach bound to a stricter list passes
  ``create_app(..., reach_policy=...)``; there is no path where a URL is fetched
  without a policy check.
* **CLI channels** answer named commands and run host-side programs. They cannot
  be bound by that policy — a subprocess opens its own sockets — so they are
  gated by activation (``CELA_REACH_CLI_ENABLE`` / ``reach.toml``), refuse to run
  inside the sandbox, and audit every call. The router does not soften any of
  that; it maps the adapter's refusal codes onto HTTP status codes rather than
  flattening them into one generic error.

Both namespaces are mounted behind the bearer token that already guards their
parent API, so enabling reach never invents a second way to authenticate.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from os_tools.egress import EgressPolicy
from os_tools.ssrf import BlockedURLError
from pydantic import BaseModel, Field, field_validator

#: Public platform namespace (same token as ``/runs``).
REACH_API_PREFIX = "/api/reach"
#: Private namespace (same token as ``/cela/v1``).
REACH_PRIVATE_PREFIX = "/cela/v1/reach"

REACH_ENABLE_ENV = "CELA_REACH_ENABLE"

try:  # the package ships with the repository, but the surface must degrade loudly
    from os_reach.adapters.cli import (
        CLI_ARGUMENT_REJECTED,
        CLI_CHANNEL_AUTH_NOT_ACKNOWLEDGED,
        CLI_CHANNEL_DISABLED,
        CLI_CHANNEL_NOT_IMPLEMENTED,
        CLI_CHANNEL_SANDBOXED,
        CLI_CHANNEL_UNKNOWN,
        CLI_NONZERO_EXIT,
        CLI_RUNNER_ERROR,
        CLI_TIMEOUT,
        CLILayer,
    )
    from os_reach.audit import ReachAuditLog, default_audit_path

    from os_reach import build_reach_layer

    REACH_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - only when os_reach is absent
    REACH_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

#: Which refusal means what on the wire. The codes are the contract; the status
#: is the transport's way of saying it.
_CLI_HTTP_STATUS: dict[str, int] = {
    CLI_CHANNEL_DISABLED: 403,
    CLI_CHANNEL_AUTH_NOT_ACKNOWLEDGED: 403,
    CLI_CHANNEL_SANDBOXED: 403,
    CLI_CHANNEL_UNKNOWN: 404,
    CLI_CHANNEL_NOT_IMPLEMENTED: 501,
    CLI_ARGUMENT_REJECTED: 400,
    CLI_TIMEOUT: 504,
    CLI_NONZERO_EXIT: 502,
    CLI_RUNNER_ERROR: 502,
}


def reach_enabled_from_env(env: dict[str, str] | None = None) -> bool:
    """Reach is opt-in: only ``CELA_REACH_ENABLE=1`` turns the surface on."""
    source = env if env is not None else dict(os.environ)
    return source.get(REACH_ENABLE_ENV, "").strip() == "1"


class ReachService:
    """The reach layer behind the HTTP surface, built on first use.

    Deferred construction keeps ``/health`` fast and lets the router report
    "reach is off" as a normal answer instead of failing app creation. The
    ``runner``/``env`` parameters exist for the same reason ``transport`` does
    in :mod:`os_reach.native`: tests must be able to replace the outside world
    rather than be trusted not to touch it.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        policy: EgressPolicy | None = None,
        transport: Any = None,
        runner: Any = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._enabled = enabled
        self._policy = policy
        self._transport = transport
        self._runner = runner
        self._env = env
        self._registry: Any = None
        self._audit: Any = None

    @property
    def enabled(self) -> bool:
        return self._enabled and not REACH_IMPORT_ERROR

    def audit(self) -> Any:
        """The shared audit log: one ring for both the tool path and HTTP path."""
        if self._audit is None:
            self._audit = ReachAuditLog(path=default_audit_path())
        return self._audit

    def registry(self) -> Any:
        if self._registry is None:
            layer = None
            if self._runner is not None or self._env is not None:
                layer = CLILayer.from_env(
                    audit=self.audit(),
                    runner=self._runner,
                    env=self._env if self._env is not None else dict(os.environ),
                )
            self._registry = build_reach_layer(
                policy=self._policy,
                transport=self._transport,
                audit=self.audit(),
                cli=layer,
            )
        return self._registry

    def cli(self) -> Any:
        return self.registry().cli


def _validated_url(value: str) -> str:
    """Reject a malformed URL as 422 before any channel, probe or run is touched."""
    from os_tools.egress import parse_http_target

    parse_http_target(value)
    return value.strip()


class ReadRequest(BaseModel):
    url: str
    max_chars: int = Field(default=20_000, ge=1, le=200_000)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return _validated_url(value)


class RssRequest(BaseModel):
    url: str
    limit: int = Field(default=10, ge=1, le=50)

    # Declared on each model on purpose: assigning another model's
    # ``field_validator`` object only *looks* shared — pydantic v2 binds
    # validators at class creation, so a copy would silently validate nothing.
    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return _validated_url(value)


class GithubRequest(BaseModel):
    repo: str = ""
    query: str = ""
    limit: int = Field(default=5, ge=1, le=20)


class CliRunRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)


def _require_enabled(service: ReachService) -> Any:
    if not service.enabled:
        reason = (
            f"os_reach is unavailable: {REACH_IMPORT_ERROR}"
            if REACH_IMPORT_ERROR
            else f"reach is disabled; set {REACH_ENABLE_ENV}=1 to enable the native channels"
        )
        raise HTTPException(status_code=503, detail=reason)
    return service.registry()


def _egress_denied(exc: Exception) -> HTTPException:
    from os_reach.security import scrub_credentials

    return HTTPException(status_code=403, detail=f"egress denied: {scrub_credentials(str(exc))}")


def create_reach_router(
    *,
    service: ReachService,
    require_auth: Any,
    prefix: str = REACH_API_PREFIX,
) -> APIRouter:
    """Build the reach router for one namespace.

    ``require_auth`` is the namespace's existing dependency: ``/api/reach``
    passes the platform guard, ``/cela/v1/reach`` the private one.
    """
    router = APIRouter(prefix=prefix, tags=["reach"], dependencies=[Depends(require_auth)])

    @router.get("/channels")
    async def channels() -> dict[str, Any]:
        """Every channel this deployment has: native, CLI, and why each is as it is."""
        registry = _require_enabled(service)
        native = registry.report(source="api")
        cli = registry.cli
        return {
            "enabled": True,
            "channels": {name: health.model_dump() for name, health in native.channels.items()},
            "cli_channels": (
                {name: health.model_dump() for name, health in cli.report().channels.items()}
                if cli is not None
                else {}
            ),
            "coverage": cli.coverage() if cli is not None else {},
            # The audit promise, made visible: counts only, never query text.
            "audit": service.audit().summary(),
        }

    @router.post("/read")
    async def read(req: ReadRequest) -> dict[str, Any]:
        registry = _require_enabled(service)
        web = registry.get("web")
        if web is None:  # pragma: no cover - the native tier always registers web
            raise HTTPException(status_code=503, detail="the web channel is not registered")
        try:
            text = await web.read(req.url, max_chars=req.max_chars)
        except BlockedURLError as exc:
            raise _egress_denied(exc) from None
        return {
            "channel": "web",
            "url": req.url,
            "text": text,
            "chars": len(text),
        }

    @router.post("/rss")
    async def rss(req: RssRequest) -> dict[str, Any]:
        registry = _require_enabled(service)
        channel = registry.get("rss")
        if channel is None:  # pragma: no cover - the native tier always registers rss
            raise HTTPException(status_code=503, detail="the rss channel is not registered")
        try:
            entries = await channel.read(req.url, limit=req.limit)
        except BlockedURLError as exc:
            raise _egress_denied(exc) from None
        return {
            "channel": "rss",
            "url": req.url,
            "items": [entry.model_dump() for entry in entries],
        }

    @router.post("/github")
    async def github(req: GithubRequest) -> dict[str, Any]:
        registry = _require_enabled(service)
        channel = registry.get("github")
        if channel is None:  # pragma: no cover - the native tier always registers github
            raise HTTPException(status_code=503, detail="the github channel is not registered")
        if not req.repo and not req.query:
            raise HTTPException(status_code=422, detail="provide 'repo' or 'query'")
        try:
            if req.repo:
                return {
                    "channel": "github",
                    "kind": "repo",
                    "data": await channel.repo(req.repo),
                }
            return {
                "channel": "github",
                "kind": "search",
                "data": await channel.search(req.query, limit=req.limit),
            }
        except BlockedURLError as exc:
            raise _egress_denied(exc) from None

    @router.post("/cli/{channel}")
    async def run_cli(channel: str, req: CliRunRequest) -> dict[str, Any]:
        """Run one CLI channel — enabled-only, host-side, and audited either way."""
        registry = _require_enabled(service)
        cli = registry.cli
        if cli is None:  # pragma: no cover - build_reach_layer always attaches a layer
            raise HTTPException(status_code=503, detail="no CLI layer is attached")
        result = await cli.run(channel, req.query, tool=f"reach_cli_{channel}")
        if result.ok:
            return {
                "channel": result.channel,
                "text": result.text,
                "items": result.items,
                "exit_code": result.exit_code,
                "bytes_out": result.bytes_out,
                "truncated": result.truncated,
                "duration_ms": result.duration_ms,
                "correlation_id": result.correlation_id,
            }
        raise HTTPException(
            status_code=_CLI_HTTP_STATUS.get(result.code, 502),
            detail={
                "code": result.code or "error",
                "error": result.error,
                "channel": result.channel,
                "correlation_id": result.correlation_id,
            },
        )

    return router
