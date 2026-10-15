"""Reach security helpers — the policy envelope around every outbound call.

Two rules govern everything in :mod:`os_reach`:

1. **Destination policy first.** A reach tool may only fetch a URL that
   :class:`~os_tools.egress.EgressPolicy` permits, which in turn can never
   override the :class:`~os_tools.ssrf.SSRFGuard` floor (no loopback, RFC1918,
   link-local or cloud metadata, ever).
2. **Secrets never travel.** Credentials live in the *process environment* and
   are read at request time; every operator-visible string passes through
   :func:`scrub_credentials` first, and nothing is stored on a model object.
   This is the same contract ADR-0011 set for model API keys.

The default reach policy is ``allow *`` **for the reach tools only** — that is
the public internet minus the SSRF floor, which is exactly what an unfettered
"read this public URL" tool must be. Operators narrow it with
``CELA_REACH_ALLOW_HOSTS`` and carve exceptions out with ``CELA_REACH_DENY_HOSTS``;
both are read at construction time, never from tool arguments.
"""

from __future__ import annotations

import os
import re

from os_core.redact import scrub_credentials as _scrub_credentials
from os_tools.egress import EgressPolicy, EgressRule
from os_tools.ssrf import SSRFGuard

#: Hosts the reach tools may talk to when nothing narrows the policy.
REACH_ALLOW_HOSTS_ENV = "CELA_REACH_ALLOW_HOSTS"
REACH_DENY_HOSTS_ENV = "CELA_REACH_DENY_HOSTS"

_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s@]*@)")
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:token|key|api_key|apikey|access_token|auth|password|secret|sig|signature)=)[^&\s]+"
)


def scrub_credentials(text: str) -> str:
    """Remove URL credentials and token-ish query values from *text*.

    Applied at every output boundary (health messages, errors, reports) because
    upstream tooling echoes the URL it was configured with.

    The canonical implementation moved to :mod:`os_core.redact` when memory and
    knowledge layers needed the same guarantee; this re-export keeps every
    ``from os_reach.security import scrub_credentials`` call site working, and
    the two regexes stay here because reach tests pin their exact behaviour.
    """
    return _scrub_credentials(text)


def truncate(text: str, limit: int, *, note: str = "\n[... truncated]") -> str:
    """Hard-cap *text* so one page cannot swallow a whole budget."""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + note


def _split_hosts(raw: str) -> list[str]:
    return [host.strip() for host in raw.split(",") if host.strip()]


def hosts_from_env(env: str) -> list[str]:
    return _split_hosts(os.environ.get(env, ""))


def default_reach_policy() -> EgressPolicy:
    """Public-internet policy for reach tools, narrowable by environment.

    * ``CELA_REACH_ALLOW_HOSTS`` — comma-separated host patterns; when set they
      replace the ``*`` default (narrowing, the common case in production).
    * ``CELA_REACH_DENY_HOSTS`` — comma-separated patterns denied before any
      allow rule matches.

    Only http/https on 80/443 are reachable; a non-standard port needs an
    explicit rule, so ``http://host:8080`` is refused by default.
    """
    allow_hosts = hosts_from_env(REACH_ALLOW_HOSTS_ENV) or ["*"]
    deny_hosts = hosts_from_env(REACH_DENY_HOSTS_ENV)

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

    Kept as a function so the *tool* name always travels with the check — an
    audit line without the tool that asked is useless.
    """
    policy.enforce(url, tool=tool)
