"""Reach tools — the channels as registry tools the agents can actually call.

Every fetching tool carries ``risk=NETWORK`` so the platform's HITL policy sees
what it is, and every failure surfaces as a *scrubbed* message: an upstream
error can echo the URL it was configured with, and that URL can carry
credentials.

Wiring is explicit, never automatic: the platform registry does not grow
network tools just because this package is importable. ``register_reach_tools``
is called by the operator (or by ``build_registry`` behind ``CELA_REACH_ENABLE=1``).
"""

from __future__ import annotations

import json

from os_core.types import RiskLevel
from os_tools.registry import Tool, ToolRegistry

from os_reach.contract import ReachReport
from os_reach.registry import ReachRegistry, build_native_registry
from os_reach.security import scrub_credentials

_REPO_SCHEMA = {
    "type": "object",
    "properties": {
        "repo": {"type": "string", "description": "Repository as 'owner/name' or a GitHub URL"}
    },
    "required": ["repo"],
}
_URL_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "Absolute http(s) URL"}},
    "required": ["url"],
}


def _fail(exc: Exception) -> RuntimeError:
    """Re-raise with the credential-scrubbed message the model is allowed to see."""
    return RuntimeError(scrub_credentials(f"{type(exc).__name__}: {exc}"))


def reach_tools(registry: ReachRegistry) -> list[Tool]:
    """Build the tool list for a reach registry (no registration side effect)."""
    web = registry.get("web")
    rss = registry.get("rss")
    github = registry.get("github")
    tools: list[Tool] = []

    if web is not None:

        async def web_read(url: str, max_chars: int = 20_000) -> str:
            """Read a public web page and return it as plain text."""
            try:
                return await web.read(url, max_chars=int(max_chars))
            except Exception as exc:
                raise _fail(exc) from None

        tools.append(
            Tool(
                name="web_read",
                description=(
                    "Read a public http(s) page and return plain text. Destinations are "
                    "checked against the egress policy; loopback, private and cloud-metadata "
                    "addresses are always refused, and non-standard ports need an explicit rule."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Absolute http(s) URL"},
                        "max_chars": {
                            "type": "integer",
                            "description": "Truncate the returned text at this many characters",
                            "default": 20_000,
                        },
                    },
                    "required": ["url"],
                },
                risk=RiskLevel.NETWORK,
                timeout=30.0,
                fn=web_read,
            )
        )

    if rss is not None:

        async def rss_read(url: str, limit: int = 10) -> str:
            """Read an RSS/Atom feed's latest entries as JSON."""
            try:
                entries = await rss.read(url, limit=int(limit))
                return json.dumps([e.model_dump() for e in entries], ensure_ascii=False, indent=2)
            except Exception as exc:
                raise _fail(exc) from None

        tools.append(
            Tool(
                name="rss_read",
                description=(
                    "Read an RSS 2.0 or Atom feed and return its latest entries "
                    "(title, link, date, summary)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Absolute http(s) feed URL"},
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of entries to return",
                            "default": 10,
                        },
                    },
                    "required": ["url"],
                },
                risk=RiskLevel.NETWORK,
                timeout=30.0,
                fn=rss_read,
            )
        )

    if github is not None:

        async def github_repo(repo: str) -> str:
            """Summarise a public GitHub repository (stars, language, topics)."""
            try:
                return json.dumps(await github.repo(repo), ensure_ascii=False, indent=2)
            except Exception as exc:
                raise _fail(exc) from None

        async def github_search(query: str, limit: int = 5) -> str:
            """Search public repositories by keyword, best-starred first."""
            try:
                found = await github.search(query, limit=int(limit))
                return json.dumps(found, ensure_ascii=False, indent=2)
            except Exception as exc:
                raise _fail(exc) from None

        tools.extend(
            [
                Tool(
                    name="github_repo",
                    description=(
                        "Summarise a public GitHub repository: description, stars, language, "
                        "licence, topics, last update. Uses GITHUB_TOKEN from the environment "
                        "when present; the token is never returned or logged."
                    ),
                    parameters=_REPO_SCHEMA,
                    risk=RiskLevel.NETWORK,
                    timeout=30.0,
                    fn=github_repo,
                ),
                Tool(
                    name="github_search",
                    description=(
                        "Search public GitHub repositories by keyword, most-starred first."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search keywords"},
                            "limit": {
                                "type": "integer",
                                "description": "Maximum results (1-20)",
                                "default": 5,
                            },
                        },
                        "required": ["query"],
                    },
                    risk=RiskLevel.NETWORK,
                    timeout=30.0,
                    fn=github_search,
                ),
            ]
        )

    async def reach_doctor() -> str:
        """Report reach channel health as JSON (local probes; no fetching)."""
        report: ReachReport = registry.report()
        return report.model_dump_json(indent=2)

    tools.append(
        Tool(
            name="reach_doctor",
            description=(
                "List the content channels available to this agent, their configuration tier "
                "and their current health. Local checks only — nothing is fetched."
            ),
            parameters={"type": "object", "properties": {}},
            risk=RiskLevel.READ_ONLY,
            timeout=15.0,
            fn=reach_doctor,
        )
    )
    return tools


def register_reach_tools(
    registry: ToolRegistry,
    *,
    reach: ReachRegistry | None = None,
) -> ToolRegistry:
    """Add the reach tools to a platform :class:`ToolRegistry`."""
    channels = reach if reach is not None else build_native_registry()
    registry.extend(reach_tools(channels))
    return registry
