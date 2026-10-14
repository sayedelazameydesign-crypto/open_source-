"""Output-boundary helpers for the MCP layer.

Same contract as ``os_reach.security`` / ``os_launcher.security`` (each layer
owns its copy so that ``os_mcp`` depends on ``os_core`` + ``os_tools`` only —
the import-closure test in ``tests/test_mcp.py`` enforces that boundary):

* :func:`scrub_credentials` — every operator-visible string (health messages,
  errors, reports, stderr tails) passes through it because MCP servers echo
  the URLs and commands they were configured with, and those carry secrets.
* :func:`truncate` — hard cap so one tool result cannot swallow a context
  window; the cap is reported, never silent.
"""

from __future__ import annotations

import re

_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s@]*@)")
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:token|key|api_key|apikey|access_token|auth|password|secret|sig|signature)=)[^&\s]+"
)


def scrub_credentials(text: str) -> str:
    """Remove URL credentials and token-ish query values from *text*."""
    cleaned = _USERINFO_RE.sub(r"\1REDACTED@", str(text))
    return _SECRET_QUERY_RE.sub(r"\1REDACTED", cleaned)


def truncate(text: str, limit: int, *, note: str = "\n[... truncated]") -> str:
    """Hard-cap *text*; ``limit <= 0`` disables the cap."""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + note


__all__ = ["scrub_credentials", "truncate"]
