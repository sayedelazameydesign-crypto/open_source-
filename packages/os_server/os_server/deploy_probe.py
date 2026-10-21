"""Post-deploy verification of a published Celia.sh service.

The deployment runbooks state the post-deploy check as two curls::

    API=https://<service>.<provider-domain>        # any HTTPS host on 443
    curl -sS $API/health                                    # {"status":"ok"}
    curl -sS -H "Authorization: Bearer $CELA_API_TOKEN" $API/runtime-info
    # preview mode (CELA_USE_LIVE_PROVIDER=0) must answer execution.kind == "scripted"

The provider is a *configuration value*, never a constant: the CI gate reads the
URL and the host allowlist from repository variables and passes them in
explicitly. ``DEFAULT_VERIFY_ALLOWED_HOSTS`` keeps the historical Render
pattern only so existing runbooks still work when no allowlist is supplied; a
gate must supply one.

Runbooks are read by people; this module is the *same check executed by a
machine*, so a deployment claim can be produced by a command instead of
asserted. The rules it keeps:

* the bearer token is never printed or stored in a report; production requests
  are HTTPS/443-only, use an explicit API-host allowlist, bypass proxy
  environment variables, and pin every resolved public IP;
* redirects are not followed, and the destination is authorized before the
  client is created or any request (especially the token-bearing request) is sent;
* every criterion is named and independently pass/fail, so a failure says
  *which* promise of the deployment broke;
* the expectation is explicit. A preview deployment must answer
  ``execution.kind == "scripted"`` and a live one ``"provider_adapter"``; the
  probe never guesses, because guessing is how a demo service gets announced
  as a live one (or the other way round).
* ``/runtime-info`` is asked with the token *and* without it: an endpoint
  that answers the identity of the deployment to anonymous callers is a
  different failure from one that answers "scripted" when "provider_adapter"
  was promised, and both must be visible.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from os_tools.egress import EgressPolicy, EgressPolicyError, EgressVerdict, parse_http_target
from os_tools.pinned_http import PinnedHTTPTransport

from os_server.automation import backend_url_problem

#: The execution kinds the server may report (see ``os_core.types.ExecutionInfo``).
VALID_EXECUTION_KINDS: tuple[str, ...] = ("scripted", "provider_adapter", "unknown")

#: The published-service checker is an API probe, not a general-purpose client.
VERIFY_TOOL = "verify_deployment"
DEFAULT_VERIFY_ALLOWED_HOSTS: tuple[str, ...] = ("*.onrender.com",)

#: ``"any"`` accepts whichever kind the service reports, as long as it is one
#: of :data:`VALID_EXECUTION_KINDS`.
ANY_KIND = "any"

#: How a mismatched kind is explained, per (expected, actual) pair.
_KIND_HINTS: dict[tuple[str, str], str] = {
    ("scripted", "provider_adapter"): (
        "the service is in live-provider mode; pass --expect-kind provider_adapter "
        "if that is what the deployment is supposed to be"
    ),
    ("provider_adapter", "scripted"): (
        "the service is answering from the scripted fallback; check "
        "CELA_USE_LIVE_PROVIDER and the provider credentials in the service logs"
    ),
}


class ProbeConfigurationError(RuntimeError):
    """The probe cannot run at all: no token, unusable URL, unknown expectation."""


@dataclass(frozen=True)
class ProbeResult:
    """One named criterion and the evidence behind it."""

    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class ProbeReport:
    """The outcome of every criterion of one probe run."""

    base_url: str
    expected_kind: str
    results: tuple[ProbeResult, ...]

    @property
    def ok(self) -> bool:
        """True when every criterion passed."""
        return all(result.ok for result in self.results)

    @property
    def failures(self) -> tuple[ProbeResult, ...]:
        return tuple(result for result in self.results if not result.ok)

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "expected_kind": self.expected_kind,
            "ok": self.ok,
            "results": [result.as_dict() for result in self.results],
        }


def normalize_base_url(raw: str) -> str:
    """Return the base URL to probe.

    Raises :class:`ProbeConfigurationError` for an empty URL or one that must
    not carry a bearer token (``http://`` outside loopback, embedded
    credentials, a non-HTTP scheme).
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise ProbeConfigurationError("no service URL was given (--api or CELA_API_URL)")
    problem = backend_url_problem(candidate, var_name="the service URL")
    if problem is not None:
        raise ProbeConfigurationError(f"{problem} (refused: {candidate})")
    if "?" in candidate or "#" in candidate:
        raise ProbeConfigurationError("the service base URL must not contain a query or fragment")
    return candidate.rstrip("/")


def _explicit_loopback_verdict(base_url: str) -> EgressVerdict:
    """Authorize the opt-in local-test exception without weakening public rules."""
    try:
        scheme, host, port = parse_http_target(base_url)
    except ValueError as exc:
        raise ProbeConfigurationError(f"invalid local service URL: {exc}") from None
    if scheme != "http":
        raise ProbeConfigurationError("the local-loopback exception is for http:// only")

    try:
        address = ipaddress.ip_address(host)
        addresses = [address]
    except ValueError:
        if host not in {"localhost", "localhost.localdomain"}:
            raise ProbeConfigurationError(
                "--allow-loopback accepts only localhost or a canonical loopback IP"
            ) from None
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        except (OSError, IndexError, TypeError, ValueError) as exc:
            raise ProbeConfigurationError(f"could not resolve local loopback host: {exc}") from None

    if not addresses or any(not address.is_loopback for address in addresses):
        raise ProbeConfigurationError(
            "--allow-loopback refused a name that does not resolve exclusively to loopback"
        )
    return EgressVerdict(
        allowed=True,
        url=base_url,
        host=host,
        connect_ip=addresses[0].compressed,
        connect_ips=list(dict.fromkeys(address.compressed for address in addresses)),
        reason="explicit loopback-only test exception",
        scheme=scheme,
        port=port,
        tool=VERIFY_TOOL,
        matched_rule="allow:loopback",
    )


def authorize_probe_destination(
    base_url: str,
    *,
    allowed_hosts: Sequence[str] | None = None,
    allow_loopback: bool = False,
) -> EgressVerdict:
    """Apply HTTPS/API-host policy, resolve safely, and return the pinned IP.

    The default is deliberately narrow: HTTPS on port 443 for a subdomain of
    ``onrender.com``. Alternate API domains must be named explicitly by the
    operator (or a future provider adapter). The local exception exists only
    for tests and requires an explicit opt-in.
    """
    try:
        scheme, host, port = parse_http_target(base_url)
    except ValueError as exc:
        raise ProbeConfigurationError(f"invalid service URL: {exc}") from None

    if (
        allow_loopback
        and scheme == "http"
        and (host in {"localhost", "localhost.localdomain"} or _is_loopback_literal(host))
    ):
        return _explicit_loopback_verdict(base_url)

    if scheme != "https":
        raise ProbeConfigurationError("published-service verification requires HTTPS")
    if port != 443:
        raise ProbeConfigurationError("published-service verification permits HTTPS port 443 only")

    if allowed_hosts is None:
        patterns = DEFAULT_VERIFY_ALLOWED_HOSTS
    elif isinstance(allowed_hosts, str):
        patterns = (allowed_hosts,)
    else:
        patterns = tuple(allowed_hosts)
    if not patterns:
        raise ProbeConfigurationError("at least one explicit API service host must be allowed")
    try:
        policy = EgressPolicy.allow_hosts(
            *patterns,
            schemes={"https"},
            ports={443},
            tools={VERIFY_TOOL},
            resolve_dns=True,
            default_tool=VERIFY_TOOL,
        )
        # Match the host/scheme/port before resolving it. This avoids even a
        # DNS query for a host that is outside the operator's allowlist.
        policy.check(base_url, tool=VERIFY_TOOL, resolve_dns=False).raise_if_denied()
        return policy.enforce(base_url, tool=VERIFY_TOOL, resolve_dns=True)
    except (EgressPolicyError, ValueError) as exc:
        raise ProbeConfigurationError(
            f"service destination refused by egress policy: {exc}"
        ) from None


def _is_loopback_literal(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _short(body: str, limit: int = 200) -> str:
    text = " ".join(body.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _token_variants(token: str) -> tuple[str, ...]:
    """Return the supplied token and common URL-encoded forms."""
    from urllib.parse import quote, quote_plus

    return tuple(
        sorted(
            {item for item in (token, quote(token, safe=""), quote_plus(token, safe="")) if item},
            key=len,
            reverse=True,
        )
    )


def _contains_token(text: str, token: str) -> bool:
    """Detect the raw token and common URL encodings, including hex case variants."""
    if not token:
        return False
    from urllib.parse import unquote, unquote_plus

    if any(variant in text for variant in _token_variants(token)):
        return True
    return token in unquote(text) or token in unquote_plus(text)


def _redact_token(text: str, token: str) -> str:
    """Remove the supplied token and common URL-encoded forms from output."""
    cleaned = str(text)
    for variant in _token_variants(token):
        cleaned = cleaned.replace(variant, "[REDACTED]")
    # URL percent escapes are case-insensitive; if an encoded variant remains,
    # redact the whole excerpt rather than accidentally echoing its decoded form.
    return "[REDACTED]" if _contains_token(cleaned, token) else cleaned


def _safe_excerpt(body: str, token: str) -> str:
    return _short(_redact_token(body, token))


def _result(name: str, ok: bool, detail: str) -> ProbeResult:
    return ProbeResult(name=name, ok=ok, detail=detail)


def _check_health(http: httpx.Client, url: str, token: str, bodies: list[str]) -> ProbeResult:
    """``GET /health`` must answer ``{"status":"ok"}`` without any token."""
    name = 'health answers {"status":"ok"} without a token'
    try:
        response = http.get(f"{url}/health")
    except httpx.HTTPError as exc:
        detail = _redact_token(str(exc), token)
        return _result(name, False, f"request failed: {type(exc).__name__}: {detail}")
    bodies.append(response.text)
    if response.status_code != 200:
        return _result(name, False, f"expected HTTP 200, got HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        return _result(name, False, f"body is not JSON: {_safe_excerpt(response.text, token)!r}")
    if payload != {"status": "ok"}:
        safe_payload = _safe_excerpt(json.dumps(payload, ensure_ascii=False), token)
        return _result(
            name,
            False,
            f'expected {{"status": "ok"}}, got {safe_payload}',
        )
    return _result(name, True, 'HTTP 200 with {"status": "ok"}')


def _check_runtime_info_requires_auth(
    http: httpx.Client, url: str, token: str, bodies: list[str]
) -> ProbeResult:
    """``/runtime-info`` must stay behind the platform token."""
    name = "runtime-info requires the platform token"
    try:
        response = http.get(f"{url}/runtime-info")
    except httpx.HTTPError as exc:
        detail = _redact_token(str(exc), token)
        return _result(name, False, f"request failed: {type(exc).__name__}: {detail}")
    bodies.append(response.text)
    if response.status_code != 401:
        return _result(
            name,
            False,
            f"expected HTTP 401 without a token, got HTTP {response.status_code}",
        )
    return _result(name, True, "HTTP 401 without a token")


def _check_execution_identity(
    http: httpx.Client,
    url: str,
    token: str,
    expected_kind: str,
    bodies: list[str],
) -> ProbeResult:
    """``/runtime-info`` must describe the executor the deployment promised."""
    name = (
        "runtime-info reports a known execution.kind"
        if expected_kind == ANY_KIND
        else f'runtime-info reports execution.kind == "{expected_kind}"'
    )
    try:
        response = http.get(
            f"{url}/runtime-info",
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        detail = _redact_token(str(exc), token)
        return _result(name, False, f"request failed: {type(exc).__name__}: {detail}")
    bodies.append(response.text)
    if response.status_code != 200:
        return _result(
            name,
            False,
            f"expected HTTP 200 with the platform token, got HTTP {response.status_code}",
        )
    try:
        payload = response.json()
    except ValueError:
        return _result(name, False, f"body is not JSON: {_safe_excerpt(response.text, token)!r}")
    execution = payload.get("execution") if isinstance(payload, dict) else None
    if not isinstance(execution, dict):
        return _result(name, False, "the response carries no 'execution' object")
    kind = execution.get("kind")
    if kind not in VALID_EXECUTION_KINDS:
        safe_kind = _redact_token(repr(kind), token)
        return _result(name, False, f"unknown execution.kind {safe_kind}")
    if expected_kind != ANY_KIND and kind != expected_kind:
        hint = _KIND_HINTS.get((expected_kind, kind), "")
        detail = f"execution.kind is {kind!r}, expected {expected_kind!r}"
        return _result(name, False, f"{detail}; {hint}" if hint else detail)
    goal_verification = execution.get("goal_verification")
    if goal_verification != "not_verified":
        safe_goal = _redact_token(repr(goal_verification), token)
        return _result(
            name,
            False,
            f"execution.goal_verification must be 'not_verified', got {safe_goal}",
        )
    model_name = execution.get("model_name")
    if not model_name:
        return _result(name, False, "execution.model_name is missing")
    safe_model_name = _redact_token(str(model_name), token)
    return _result(
        name,
        True,
        f"execution.kind={kind}; model_name={safe_model_name}; goal_verification=not_verified",
    )


def _check_private_surface(
    http: httpx.Client,
    url: str,
    token: str,
    private_token: str,
    bodies: list[str],
) -> ProbeResult:
    """The private surface must accept its own token and refuse the public one.

    The deployment has two credentials; checking only the public one leaves the
    private API either unreachable (503 when unset) or, worse, reachable with
    the public token. Both directions are asserted in one criterion.
    """
    name = "/cela/v1/whoami accepts the private token and refuses the public one"
    try:
        public_response = http.get(
            f"{url}/cela/v1/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )
        bodies.append(public_response.text)
        private_response = http.get(
            f"{url}/cela/v1/whoami",
            headers={"Authorization": f"Bearer {private_token}"},
        )
        bodies.append(private_response.text)
    except httpx.HTTPError as exc:
        detail = _redact_token(str(exc), private_token)
        detail = _redact_token(detail, token)
        return _result(name, False, f"request failed: {type(exc).__name__}: {detail}")

    if public_response.status_code != 401:
        return _result(
            name,
            False,
            "the public token was not refused by /cela/v1/whoami "
            f"(expected HTTP 401, got HTTP {public_response.status_code})",
        )
    if private_response.status_code != 200:
        return _result(
            name,
            False,
            "the private token did not open /cela/v1/whoami "
            f"(expected HTTP 200, got HTTP {private_response.status_code})",
        )
    try:
        payload = private_response.json()
    except ValueError:
        return _result(name, False, "whoami did not answer JSON")
    surface = payload.get("surface") if isinstance(payload, dict) else None
    if surface != "private":
        safe_surface = _redact_token(repr(surface), private_token)
        return _result(name, False, f"whoami reports surface={safe_surface}, expected 'private'")
    return _result(
        name,
        True,
        "HTTP 401 for the public token; HTTP 200 and surface=private for the private token",
    )


def _check_durable_store(
    http: httpx.Client,
    url: str,
    token: str,
    bodies: list[str],
) -> ProbeResult:
    """``/platform/info`` must report a run store that survives a restart.

    This is a process-report check, not a write test: it proves the running
    service *configured* a durable store (Postgres/SQLite) instead of the
    in-memory default. Proving writes survive a real reconnect is the separate
    C6 acceptance script against a dedicated database.
    """
    name = "platform-info reports a durable run store"
    try:
        response = http.get(
            f"{url}/platform/info",
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        detail = _redact_token(str(exc), token)
        return _result(name, False, f"request failed: {type(exc).__name__}: {detail}")
    bodies.append(response.text)
    if response.status_code != 200:
        return _result(
            name,
            False,
            f"expected HTTP 200 with the platform token, got HTTP {response.status_code}",
        )
    try:
        payload = response.json()
    except ValueError:
        return _result(name, False, f"body is not JSON: {_safe_excerpt(response.text, token)!r}")
    store = payload.get("store") if isinstance(payload, dict) else None
    if not isinstance(store, dict):
        return _result(name, False, "the response carries no 'store' object")
    if store.get("durable") is not True:
        backend = _redact_token(str(store.get("backend")), token)
        scope = _redact_token(str(store.get("scope")), token)
        return _result(
            name,
            False,
            f"store.durable is not true (backend={backend}, scope={scope}); "
            "the service is on the in-memory store",
        )
    backend = _redact_token(str(store.get("backend")), token)
    scope = _redact_token(str(store.get("scope")), token)
    return _result(name, True, f"store.durable=true; backend={backend}; scope={scope}")


def _check_no_credential_echo(tokens: Sequence[str], bodies: list[str]) -> ProbeResult:
    """No response body may echo a token back, including URL-encoded forms."""
    name = "no response echoes the API token"
    for token in tokens:
        if token and any(_contains_token(body, token) for body in bodies):
            return _result(name, False, "an API token appeared in a response body")
    return _result(name, True, "the API token appears in no response body")


def verify_published_service(
    base_url: str,
    token: str,
    *,
    expected_kind: str = "scripted",
    timeout: float = 20.0,
    allowed_hosts: Sequence[str] | None = None,
    allow_loopback: bool = False,
    private_token: str | None = None,
    require_durable_store: bool = False,
    client: httpx.Client | None = None,
) -> ProbeReport:
    """Run the post-deploy criteria against *base_url* and return the report.

    ``expected_kind`` is ``"scripted"`` (the preview deployment the runbooks
    describe), ``"provider_adapter"`` (the live one), or ``"any"`` to accept
    whichever kind the service reports.

    Two criteria are optional because not every deployment promises them:

    * ``private_token`` adds a check that ``/cela/v1/whoami`` accepts the
      private token and refuses the public one;
    * ``require_durable_store`` adds a check that ``/platform/info`` reports
      ``store.durable == true`` (a Postgres or on-disk store, not memory).

    Raises :class:`ProbeConfigurationError` when the probe cannot run: no
    token, an unusable or disallowed URL, an unknown expectation, or a private
    token identical to the public one. ``allowed_hosts`` defaults to the legacy
    Render pattern; the deploy workflow always passes the repository-variable
    allowlist explicitly, so no provider is hardcoded into the gate. Set
    ``allow_loopback=True`` only for local tests. An injected client is accepted
    only when backed by ``httpx.MockTransport`` (a socket-free test seam).
    """
    token = (token or "").strip()
    if not token:
        raise ProbeConfigurationError("no API token was given (set CELA_API_TOKEN)")
    private = (private_token or "").strip()
    if private and private == token:
        raise ProbeConfigurationError(
            "the private token must differ from the public token (CELA_PRIVATE_API_TOKEN)"
        )
    expectation = (expected_kind or ANY_KIND).strip().lower()
    if expectation not in (ANY_KIND, *VALID_EXECUTION_KINDS):
        message = (
            f"unknown expectation {expected_kind!r}; use one of "
            f"{', '.join((ANY_KIND, *VALID_EXECUTION_KINDS))}"
        )
        raise ProbeConfigurationError(_redact_token(message, token))
    try:
        url = normalize_base_url(base_url)
        if _contains_token(url, token) or (private and _contains_token(url, private)):
            raise ProbeConfigurationError("the service base URL must not contain an API token")
        verdict = authorize_probe_destination(
            url,
            allowed_hosts=allowed_hosts,
            allow_loopback=allow_loopback,
        )
    except ProbeConfigurationError as exc:
        detail = _redact_token(str(exc), token)
        raise ProbeConfigurationError(_redact_token(detail, private)) from None

    owns_client = client is None
    if client is not None:
        if not isinstance(getattr(client, "_transport", None), httpx.MockTransport):
            raise ProbeConfigurationError(
                "injected clients must use httpx.MockTransport; "
                "network clients require pinned transport"
            )
        if getattr(client, "follow_redirects", False):
            raise ProbeConfigurationError("injected clients must not follow redirects")
    http = (
        client
        if client is not None
        else httpx.Client(
            timeout=timeout,
            transport=PinnedHTTPTransport(verdict),
            follow_redirects=False,
            trust_env=False,
        )
    )
    bodies: list[str] = []
    try:
        results = [
            _check_health(http, url, token, bodies),
            _check_runtime_info_requires_auth(http, url, token, bodies),
            _check_execution_identity(http, url, token, expectation, bodies),
        ]
        if private:
            results.append(_check_private_surface(http, url, token, private, bodies))
        if require_durable_store:
            results.append(_check_durable_store(http, url, token, bodies))
        results.append(_check_no_credential_echo((token, private), bodies))
    finally:
        if owns_client:
            http.close()
    safe_url = _redact_token(_redact_token(url, token), private)
    return ProbeReport(base_url=safe_url, expected_kind=expectation, results=tuple(results))
