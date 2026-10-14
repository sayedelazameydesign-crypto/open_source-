"""Launcher security — the policy envelope around every outbound call.

Three rules govern everything in :mod:`os_launcher`:

1. **Destination policy first.** The client may only fetch URLs that
   :class:`~os_tools.egress.EgressPolicy` permits, which can never override the
   :class:`~os_tools.ssrf.SSRFGuard` floor (no loopback, RFC1918, link-local or
   cloud metadata, ever). The default policy is an *allow-list of four hosts* —
   the GitHub API, GitHub releases, GitHub's asset CDN and ArtCraft's release
   feed — because the launcher only ever talks to those.
2. **Downloads come from the app's own GitHub releases**, exactly as the
   launcher's ``AGENTS.md`` demands: ``https://github.com/storytold/<app>/releases/download/…``.
   A feed entry pointing anywhere else is not "also allowed" — it is dropped.
3. **Secrets never travel.** Every operator-visible string passes through
   :func:`scrub_credentials` first (the same contract as ``os_reach.security``).

Operators narrow the envelope with ``CELA_LAUNCHER_ALLOW_HOSTS`` and
``CELA_LAUNCHER_DENY_HOSTS`` (comma-separated host patterns, read at
construction time, never from tool arguments).
"""

from __future__ import annotations

import os
import re

from os_tools.egress import EgressPolicy, EgressRule
from os_tools.ssrf import SSRFGuard

from os_launcher.catalog import GITHUB_OWNER

#: Hosts the launcher client may talk to when nothing narrows the policy.
LAUNCHER_ALLOW_HOSTS_ENV = "CELA_LAUNCHER_ALLOW_HOSTS"
LAUNCHER_DENY_HOSTS_ENV = "CELA_LAUNCHER_DENY_HOSTS"

#: The only hosts a correct launcher client ever needs.
DEFAULT_ALLOW_HOSTS: tuple[str, ...] = (
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "api.storyteller.ai",
)

_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\\-]*://)([^/\s@]*@)")
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:token|key|api_key|apikey|access_token|auth|password|secret|sig|signature)=)[^&\s]+"
)


def scrub_credentials(text: str) -> str:
    """Remove URL credentials and token-ish query values from *text*.

    Same contract as ``os_reach.security.scrub_credentials``: applied at every
    output boundary because upstream errors echo the URL they were configured
    with, and a URL can carry credentials.
    """
    cleaned = _USERINFO_RE.sub(r"\1REDACTED@", str(text))
    return _SECRET_QUERY_RE.sub(r"\1REDACTED", cleaned)


def _split_hosts(raw: str) -> list[str]:
    return [host.strip() for host in raw.split(",") if host.strip()]


def default_launcher_policy(env: dict[str, str] | None = None) -> EgressPolicy:
    """The four-host allow-list policy, narrowable by environment.

    * ``CELA_LAUNCHER_ALLOW_HOSTS`` — when set, *replaces* the four-host default
      (a deployment behind a GitHub Enterprise mirror would set it).
    * ``CELA_LAUNCHER_DENY_HOSTS`` — denied before any allow rule matches.

    Only http/https on 80/443; the SSRF floor is always on.
    """
    source = env if env is not None else dict(os.environ)
    allow_hosts = _split_hosts(source.get(LAUNCHER_ALLOW_HOSTS_ENV, "")) or list(
        DEFAULT_ALLOW_HOSTS
    )
    deny_hosts = _split_hosts(source.get(LAUNCHER_DENY_HOSTS_ENV, ""))
    rules = [
        EgressRule(action="deny", host=host, schemes={"http", "https"}, ports={80, 443})
        for host in deny_hosts
    ]
    rules.extend(
        EgressRule(action="allow", host=host, schemes={"http", "https"}, ports={80, 443})
        for host in allow_hosts
    )
    return EgressPolicy(rules=rules, guard=SSRFGuard(resolve_dns=False))


def enforce_url(policy: EgressPolicy, url: str, *, tool: str) -> None:
    """Raise :class:`~os_tools.egress.EgressPolicyError` unless *url* is allowed.

    The *tool* name always travels with the check — an audit line without the
    tool that asked is useless.
    """
    policy.enforce(url, tool=tool)


def is_own_release_download(url: str, repo: str) -> bool:
    """True only for ``https://github.com/<repo>/releases/download/…``.

    The launcher installs other people's software; this is the rule that keeps
    a hostile feed from redirecting a download to a lookalike host. ``repo``
    itself must live under :data:`GITHUB_OWNER` or the answer is ``False``.
    """
    if not repo.startswith(f"{GITHUB_OWNER}/") or "/" in repo[len(GITHUB_OWNER) + 1 :]:
        return False
    return url.startswith(f"https://github.com/{repo}/releases/download/")
