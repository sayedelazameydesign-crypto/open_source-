"""Launcher surfaces — ``/api/launcher/*`` and ``/cela/v1/launcher/*`` (ADR-0015).

Read-only windows onto the Craft App Suite: the catalog (local data), the
latest release of one app, and an update check over installed versions. The
same three rules as the tools apply on the wire:

* the surface is **opt-in** (``CELA_LAUNCHER_ENABLE=1``) and answers 503 with
  the variable name when it is off — a deployment that never asked for it
  cannot fetch through it;
* every outbound URL is enforced against an
  :class:`~os_tools.egress.EgressPolicy` before the request leaves, and asset
  URLs additionally have to be the app's own GitHub release downloads;
* there is no endpoint here that downloads, installs or runs anything.

Both namespaces are mounted behind the bearer token that already guards their
parent API, so the launcher never invents a second way to authenticate.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from os_tools.egress import EgressPolicy
from pydantic import BaseModel, Field

#: Public platform namespace (same token as ``/runs``).
LAUNCHER_API_PREFIX = "/api/launcher"
#: Private namespace (same token as ``/cela/v1``).
LAUNCHER_PRIVATE_PREFIX = "/cela/v1/launcher"

LAUNCHER_ENABLE_ENV = "CELA_LAUNCHER_ENABLE"

try:  # the package ships with the repository, but the surface must degrade loudly
    from os_launcher import (
        CraftReleaseClient,
        LauncherError,
        by_id,
        catalog_rows,
        default_launcher_policy,
    )

    LAUNCHER_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - only when os_launcher is absent
    LAUNCHER_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def launcher_enabled_from_env(env: dict[str, str] | None = None) -> bool:
    """The launcher surface is opt-in: only ``CELA_LAUNCHER_ENABLE=1`` turns it on."""
    source = env if env is not None else dict(os.environ)
    return source.get(LAUNCHER_ENABLE_ENV, "").strip() == "1"


class LauncherService:
    """The release client behind the HTTP surface, built on first use.

    Deferred construction keeps ``/health`` fast and lets the router report
    "the launcher surface is off" as a normal answer instead of failing app
    creation. ``transport`` exists so tests can replace the outside world
    rather than be trusted not to touch it.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        policy: EgressPolicy | None = None,
        transport: Any = None,
        feed_url: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._enabled = enabled
        self._policy = policy
        self._transport = transport
        self._feed_url = feed_url
        self._env = env
        self._client: CraftReleaseClient | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled and not LAUNCHER_IMPORT_ERROR

    def client(self) -> CraftReleaseClient:
        """The shared release client, built once."""
        if self._client is None:
            from os_launcher.catalog import FEED_URL

            kwargs: dict[str, Any] = {
                "transport": self._transport,
                "policy": self._policy
                if self._policy is not None
                else default_launcher_policy(self._env),
            }
            if self._feed_url is not None:
                kwargs["feed_url"] = self._feed_url
            elif self._env is not None:
                kwargs["feed_url"] = self._env.get("CELA_LAUNCHER_FEED_URL", FEED_URL)
            self._client = CraftReleaseClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class CheckRequest(BaseModel):
    """``{"installed": {"photocraft": "0.5.0", …}}`` — what the operator has."""

    installed: dict[str, str] = Field(min_length=1, max_length=64)


def _require_enabled(service: LauncherService) -> LauncherService:
    if not service.enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "the launcher surface is disabled; set CELA_LAUNCHER_ENABLE=1 to enable it"
                if not LAUNCHER_IMPORT_ERROR
                else f"os_launcher is unavailable: {LAUNCHER_IMPORT_ERROR}"
            ),
        )
    return service


def _launcher_unavailable(exc: Exception) -> HTTPException:
    """Map a client failure onto 502 with a scrubbed, operator-safe message."""
    from os_launcher.security import scrub_credentials

    return HTTPException(status_code=502, detail=scrub_credentials(str(exc)))


def create_launcher_router(
    *,
    service: LauncherService,
    require_auth: Any,
    prefix: str = LAUNCHER_API_PREFIX,
) -> APIRouter:
    """Build the launcher router for one namespace.

    The router does not soften anything: disabled stays 503, unknown apps 404,
    source failures 502 — the codes are the contract.
    """
    router = APIRouter(prefix=prefix, tags=["launcher"], dependencies=[Depends(require_auth)])

    @router.get("/catalog")
    async def catalog() -> dict[str, Any]:
        """The thirteen-app suite (local data — enabled surface required, no network)."""
        _require_enabled(service)
        rows = catalog_rows()
        return {"apps": rows, "count": len(rows)}

    @router.get("/apps/{app_id}/release")
    async def app_release(app_id: str) -> dict[str, Any]:
        """Latest release of one app: tag, version, assets, checksums."""
        _require_enabled(service)
        app = by_id(app_id)
        if app is None:
            raise HTTPException(status_code=404, detail=f"unknown app id: {app_id}")
        try:
            release = await service.client().latest_release(app)
        except LauncherError as exc:
            raise _launcher_unavailable(exc) from None
        except Exception as exc:  # pragma: no cover - defensive
            raise _launcher_unavailable(exc) from None
        return {
            "app_id": app.id,
            "release": release.model_dump(mode="json") if release is not None else None,
        }

    @router.post("/check")
    async def check_updates(req: CheckRequest) -> dict[str, Any]:
        """Which installed apps have a newer release (read-only comparison)."""
        _require_enabled(service)
        try:
            rows = await service.client().check_updates(dict(req.installed))
        except LauncherError as exc:
            raise _launcher_unavailable(exc) from None
        except Exception as exc:  # pragma: no cover - defensive
            raise _launcher_unavailable(exc) from None
        return {
            "apps": [r.model_dump(mode="json", exclude={"release"}) for r in rows],
            "updates": [r.app_id for r in rows if r.update_available],
        }

    return router
