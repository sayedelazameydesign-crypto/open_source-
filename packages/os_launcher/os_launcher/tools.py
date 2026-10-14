"""Launcher tools — the catalog and releases as registry tools agents can call.

Every fetching tool carries ``risk=NETWORK`` so the platform's HITL policy sees
what it is, and every failure surfaces as a *scrubbed* message. Wiring is
explicit, never automatic: the platform registry does not grow network tools
just because this package is importable — ``register_launcher_tools`` is
called by the operator (or behind ``CELA_LAUNCHER_ENABLE=1``).

The tools are **read-only by construction**: they list the suite, read release
metadata and compare versions. They never download an installer, never run one,
and can therefore never install third-party software through the model.
"""

from __future__ import annotations

import json

from os_core.types import RiskLevel
from os_tools.registry import Tool, ToolRegistry

from os_launcher.catalog import SUITE, by_id
from os_launcher.client import CraftReleaseClient
from os_launcher.security import scrub_credentials

_APP_ID_SCHEMA = {
    "type": "string",
    "description": "App machine id, e.g. 'photocraft' (or a former id like 'printcraft')",
}
_INSTALLED_SCHEMA = {
    "type": "object",
    "description": 'Map of app id -> installed version, e.g. {"photocraft": "0.5.0"}',
    "additionalProperties": {"type": "string"},
}


def _fail(exc: Exception) -> RuntimeError:
    """Re-raise with the credential-scrubbed message the model is allowed to see."""
    return RuntimeError(scrub_credentials(f"{type(exc).__name__}: {exc}"))


def _catalog_json() -> str:
    rows = [
        {
            "id": app.id,
            "name": app.name,
            "tagline": app.tagline,
            "group": app.group.value,
            "repo": app.repo,
            "releases_url": app.releases_url(),
            "former_ids": app.former_ids,
        }
        for app in SUITE
    ]
    return json.dumps(rows, ensure_ascii=False, indent=2)


def launcher_tools(client: CraftReleaseClient) -> list[Tool]:
    """Build the tool list for a release client (no registration side effect)."""

    async def craft_list_apps() -> str:
        """List every app in the Craft App Suite (no network)."""
        return _catalog_json()

    async def craft_latest_release(app_id: str) -> str:
        """Latest published release of one Craft app: version, assets and checksums."""
        app = by_id(str(app_id))
        if app is None:
            raise RuntimeError(f"unknown app id: {app_id}")
        try:
            release = await client.latest_release(app)
        except Exception as exc:
            raise _fail(exc) from None
        if release is None:
            return json.dumps({"app_id": app.id, "release": None}, indent=2)
        return json.dumps(
            {
                "app_id": app.id,
                "release": release.model_dump(mode="json"),
                "checked_assets": [a.name for a in release.checked_assets()],
            },
            ensure_ascii=False,
            indent=2,
        )

    async def craft_check_updates(installed: dict[str, str]) -> str:
        """Compare installed Craft app versions against the latest releases."""
        if not isinstance(installed, dict) or not installed:
            raise RuntimeError("installed must be a non-empty object of app_id -> version")
        try:
            rows = await client.check_updates({str(k): str(v) for k, v in installed.items()})
        except Exception as exc:
            raise _fail(exc) from None
        return json.dumps([r.model_dump(mode="json", exclude={"release"}) for r in rows], indent=2)

    return [
        Tool(
            name="craft_list_apps",
            description=(
                "List the thirteen apps of the Craft App Suite (ArtCraft): id, name, "
                "tagline and GitHub repository. Local catalog data, no network."
            ),
            parameters={"type": "object", "properties": {}},
            risk=RiskLevel.READ_ONLY,
            timeout=10.0,
            fn=craft_list_apps,
        ),
        Tool(
            name="craft_latest_release",
            description=(
                "Latest published release of one Craft app from its GitHub releases "
                "(or ArtCraft's release feed): tag, version, assets and SHA-256 "
                "checksums. Read-only: never downloads or installs anything."
            ),
            parameters={
                "type": "object",
                "properties": {"app_id": _APP_ID_SCHEMA},
                "required": ["app_id"],
            },
            risk=RiskLevel.NETWORK,
            timeout=30.0,
            fn=craft_latest_release,
        ),
        Tool(
            name="craft_check_updates",
            description=(
                "Given installed Craft app versions, report which apps have a newer "
                "release available. Read-only: never downloads or installs anything."
            ),
            parameters={
                "type": "object",
                "properties": {"installed": _INSTALLED_SCHEMA},
                "required": ["installed"],
            },
            risk=RiskLevel.NETWORK,
            timeout=60.0,
            fn=craft_check_updates,
        ),
    ]


def register_launcher_tools(
    registry: ToolRegistry, client: CraftReleaseClient | None = None
) -> list[str]:
    """Register the launcher tools on *registry*; return the registered names.

    Explicit wiring only — the caller owns the client (and therefore the
    transport and policy). Tools are registered only when this is called.
    """
    resolved = client if client is not None else CraftReleaseClient()
    tools = launcher_tools(resolved)
    registry.extend(tools)
    return [tool.name for tool in tools]
