"""Post-deploy verification of a published Celia.sh service.

The deployment runbooks state the post-deploy check as two curls::

    API=https://<service>.onrender.com
    curl -sS $API/health                                    # {"status":"ok"}
    curl -sS -H "Authorization: Bearer $CELA_API_TOKEN" $API/runtime-info
    # preview mode (CELA_USE_LIVE_PROVIDER=0) must answer execution.kind == "scripted"

Runbooks are read by people; this module is the *same check executed by a
machine*, so a deployment claim can be produced by a command instead of
asserted. The rules it keeps:

* the bearer token is never printed, never stored in a report, and never sent
  over cleartext ``http://`` to a non-loopback host;
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

import json
from dataclasses import dataclass
from typing import Any

import httpx

from os_server.automation import backend_url_problem

#: The execution kinds the server may report (see ``os_core.types.ExecutionInfo``).
VALID_EXECUTION_KINDS: tuple[str, ...] = ("scripted", "provider_adapter", "unknown")

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
    return candidate.rstrip("/")


def _short(body: str, limit: int = 200) -> str:
    text = " ".join(body.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _result(name: str, ok: bool, detail: str) -> ProbeResult:
    return ProbeResult(name=name, ok=ok, detail=detail)


def _check_health(http: httpx.Client, url: str, bodies: list[str]) -> ProbeResult:
    """``GET /health`` must answer ``{"status":"ok"}`` without any token."""
    name = 'health answers {"status":"ok"} without a token'
    try:
        response = http.get(f"{url}/health")
    except httpx.HTTPError as exc:
        return _result(name, False, f"request failed: {type(exc).__name__}: {exc}")
    bodies.append(response.text)
    if response.status_code != 200:
        return _result(name, False, f"expected HTTP 200, got HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        return _result(name, False, f"body is not JSON: {_short(response.text)!r}")
    if payload != {"status": "ok"}:
        return _result(
            name,
            False,
            f'expected {{"status": "ok"}}, got {json.dumps(payload, ensure_ascii=False)[:200]}',
        )
    return _result(name, True, 'HTTP 200 with {"status": "ok"}')


def _check_runtime_info_requires_auth(
    http: httpx.Client, url: str, bodies: list[str]
) -> ProbeResult:
    """``/runtime-info`` must stay behind the platform token."""
    name = "runtime-info requires the platform token"
    try:
        response = http.get(f"{url}/runtime-info")
    except httpx.HTTPError as exc:
        return _result(name, False, f"request failed: {type(exc).__name__}: {exc}")
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
        return _result(name, False, f"request failed: {type(exc).__name__}: {exc}")
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
        return _result(name, False, f"body is not JSON: {_short(response.text)!r}")
    execution = payload.get("execution") if isinstance(payload, dict) else None
    if not isinstance(execution, dict):
        return _result(name, False, "the response carries no 'execution' object")
    kind = execution.get("kind")
    if kind not in VALID_EXECUTION_KINDS:
        return _result(name, False, f"unknown execution.kind {kind!r}")
    if expected_kind != ANY_KIND and kind != expected_kind:
        hint = _KIND_HINTS.get((expected_kind, kind), "")
        detail = f"execution.kind is {kind!r}, expected {expected_kind!r}"
        return _result(name, False, f"{detail}; {hint}" if hint else detail)
    if execution.get("goal_verification") != "not_verified":
        return _result(
            name,
            False,
            "execution.goal_verification must be 'not_verified', got "
            f"{execution.get('goal_verification')!r}",
        )
    model_name = execution.get("model_name")
    if not model_name:
        return _result(name, False, "execution.model_name is missing")
    return _result(
        name,
        True,
        f"execution.kind={kind}; model_name={model_name}; goal_verification=not_verified",
    )


def _check_no_credential_echo(token: str, bodies: list[str]) -> ProbeResult:
    """No response body may echo the token back."""
    name = "no response echoes the API token"
    if any(token and token in body for body in bodies):
        return _result(name, False, "the API token appeared verbatim in a response body")
    return _result(name, True, "the API token appears in no response body")


def verify_published_service(
    base_url: str,
    token: str,
    *,
    expected_kind: str = "scripted",
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> ProbeReport:
    """Run the post-deploy criteria against *base_url* and return the report.

    ``expected_kind`` is ``"scripted"`` (the preview deployment the runbooks
    describe), ``"provider_adapter"`` (the live one), or ``"any"`` to accept
    whichever kind the service reports.

    Raises :class:`ProbeConfigurationError` when the probe cannot run: no
    token, an unusable URL, or an unknown expectation.
    """
    if not (token or "").strip():
        raise ProbeConfigurationError("no API token was given (set CELA_API_TOKEN)")
    expectation = (expected_kind or ANY_KIND).strip().lower()
    if expectation not in (ANY_KIND, *VALID_EXECUTION_KINDS):
        raise ProbeConfigurationError(
            f"unknown expectation {expected_kind!r}; use one of "
            f"{', '.join((ANY_KIND, *VALID_EXECUTION_KINDS))}"
        )
    url = normalize_base_url(base_url)

    bodies: list[str] = []
    owns_client = client is None
    http = client if client is not None else httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        results = [
            _check_health(http, url, bodies),
            _check_runtime_info_requires_auth(http, url, bodies),
            _check_execution_identity(http, url, token, expectation, bodies),
            _check_no_credential_echo(token, bodies),
        ]
    finally:
        if owns_client:
            http.close()
    return ProbeReport(base_url=url, expected_kind=expectation, results=tuple(results))
