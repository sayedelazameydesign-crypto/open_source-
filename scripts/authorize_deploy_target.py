#!/usr/bin/env python3
"""Authorize the deployment-verification target — before any request is sent.

The CI gate takes its URL and host allowlist from repository configuration
(``vars.CELA_DEPLOY_URL`` and ``vars.CELA_DEPLOY_ALLOWED_HOSTS``). This script
turns those two values into a decision, with no network access and no DNS
lookup: either the destination is acceptable and the normalized values are
printed, or the run stops with one actionable line and exit code 2.

Why this exists separately from the probe
-----------------------------------------
``scripts/verify_deployment.py`` already refuses a destination outside its
allowlist before sending the token. This script makes the *workflow* fail with
a clear preflight message (unset variable, ``http://`` URL, credentials in the
URL, a host outside the allowlist) instead of surfacing a probe exception, and
it emits the exact ``--allow-host`` values so the two layers cannot disagree.

Exit codes: 0 = authorized · 2 = refused (the message says why).
"""

from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import json
import re
import sys
from urllib.parse import urlsplit

#: Host patterns are deliberately a small alphabet: a provider subdomain, a
#: wildcard prefix, or an exact name. No ports, paths, schemes or userinfo.
_HOST_PATTERN = re.compile(r"^(?:\*\.)?[a-z0-9](?:[a-z0-9.*_-]*[a-z0-9])?$")

#: A hostname label per RFC 1123 (underscores are tolerated by some providers).
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$"
)


class TargetError(ValueError):
    """The configured target must not be probed."""


def parse_allowlist(raw: str) -> tuple[str, ...]:
    """Parse ``a.example, *.example`` into validated, lower-cased patterns.

    An empty list is refused: a gate with no allowlist would accept whichever
    URL the configuration points at, which is the same as no gate.
    """
    patterns: list[str] = []
    for part in (raw or "").replace("\n", ",").split(","):
        candidate = part.strip().lower().rstrip(".")
        if not candidate:
            continue
        if not _HOST_PATTERN.match(candidate):
            raise TargetError(
                f"invalid allowlist entry {part.strip()!r}: use a host pattern like "
                "'*.example.app' or 'api.example.app'"
            )
        if candidate.count("*") > 1 or ("*" in candidate and not candidate.startswith("*.")):
            raise TargetError(
                f"invalid allowlist entry {part.strip()!r}: a wildcard is only allowed "
                "as a leading '*.' label"
            )
        if ".." in candidate:
            raise TargetError(f"invalid allowlist entry {part.strip()!r}: empty label")
        if candidate not in patterns:
            patterns.append(candidate)
    if not patterns:
        raise TargetError(
            "the host allowlist is empty — set CELA_DEPLOY_ALLOWED_HOSTS "
            "(for example '*.your-provider.app')"
        )
    return tuple(patterns)


def match_host(host: str, patterns: tuple[str, ...]) -> str:
    """Return the pattern that authorizes *host*, or raise :class:`TargetError`."""
    for pattern in patterns:
        if fnmatch.fnmatchcase(host, pattern):
            return pattern
    raise TargetError(f"host {host!r} is not covered by the allowlist ({', '.join(patterns)})")


def authorize_target(url: str, allowlist_raw: str) -> dict[str, object]:
    """Validate *url* against the allowlist and return the normalized target."""
    patterns = parse_allowlist(allowlist_raw)
    candidate = (url or "").strip()
    if not candidate:
        raise TargetError("the deploy URL is empty — set CELA_DEPLOY_URL")
    if any(character.isspace() for character in candidate):
        raise TargetError("the deploy URL must not contain whitespace")

    parts = urlsplit(candidate)
    if parts.scheme != "https":
        raise TargetError("the deploy URL must use https:// — tokens never travel in cleartext")
    if parts.username or parts.password:
        raise TargetError("the deploy URL must not embed credentials")
    if parts.query or parts.fragment:
        raise TargetError("the deploy URL must not contain a query or fragment")
    if parts.path not in ("", "/"):
        raise TargetError(
            "the deploy URL must be the scheme and host only (no path); "
            "the probe appends /health and /runtime-info itself"
        )
    try:
        port = parts.port
    except ValueError:
        raise TargetError("the deploy URL has an invalid port") from None
    if port not in (None, 443):
        raise TargetError("the deploy URL must use the default HTTPS port (443)")

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise TargetError("the deploy URL has no host")
    if host == "localhost" or host.endswith(".localhost"):
        raise TargetError("the deploy URL must not be a loopback name")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        raise TargetError(
            "the deploy URL must use a DNS name, not an IP literal "
            "(a certificate cannot pin an address the way an allowlist pins a name)"
        )
    if not _HOSTNAME.match(host):
        raise TargetError(f"the deploy URL host {host!r} is not a valid public hostname")

    matched = match_host(host, patterns)
    normalized = f"https://{host}"
    return {
        "url": normalized,
        "host": host,
        "allowed_host": matched,
        "allow_hosts": list(patterns),
    }


def _emit_env(result: dict[str, object]) -> str:
    """Emit the ``KEY=value`` line the workflow appends to ``$GITHUB_ENV``.

    Only the normalized URL is exported. The allowlist stays the repository
    variable the caller already validated, so a later step cannot silently
    probe a different pattern set than the one the preflight approved.
    """
    return f"API_URL={result['url']}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Authorize a deployment-verification URL against an explicit host allowlist.",
    )
    parser.add_argument("--url", required=True, help="the deploy base URL (https, host only)")
    parser.add_argument(
        "--allowed-hosts",
        required=True,
        help="comma-separated host allowlist (exact names or a leading '*.' wildcard)",
    )
    parser.add_argument(
        "--format",
        choices=("json", "env"),
        default="json",
        help="json (default) for tests and humans; env for $GITHUB_ENV",
    )
    args = parser.parse_args(argv)

    try:
        result = authorize_target(args.url, args.allowed_hosts)
    except TargetError as exc:
        print(f"::error title=Deploy target refused::{exc}")
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.format == "env":
        sys.stdout.write(_emit_env(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
