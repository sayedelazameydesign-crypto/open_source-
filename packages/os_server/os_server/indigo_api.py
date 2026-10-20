"""Optional AI Indigo external API integration — ``/api/indigo/*`` and ``/cela/v1/indigo/*``.

Design & security boundaries (ADR-0021):

* **Completely independent from core platform startup.** ``os_server``, Render
  (C5), and PostgreSQL persistence (C6) do **not** require ``INDIGO_API_KEY``.
  When ``CELA_INDIGO_ENABLE`` is unset or ``0``, no client is constructed and
  every route except none — even ``/status`` reports ``enabled=False`` cleanly
  while data routes fail closed with ``503``.
* **Secret stays in process environment only (ADR-0011).** The key is read on
  demand from ``env[credential_var]`` (default ``INDIGO_API_KEY``), never stored
  on a serialized model, never echoed in ``repr()``, and scrubbed from every
  error message (both shape-based via :func:`os_core.redact.scrub_credentials`
  and literal replacement of the active key value).
* **Pinned HTTPS egress.** Outbound calls pass through
  :class:`~os_tools.egress.EgressPolicy` + :class:`~os_tools.ssrf.SSRFGuard`
  allowing only ``aiindigo.com`` on HTTPS port ``443`` by default, with HTTP
  redirects disabled (``follow_redirects=False``) so ``X-API-Key`` is never
  forwarded to another host.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from os_core.redact import scrub_credentials
from os_tools.egress import EgressPolicy, EgressRule
from os_tools.ssrf import BlockedURLError, SSRFGuard
from pydantic import BaseModel

#: Public platform namespace (guarded by ``CELA_API_TOKEN`` / ``OS_API_TOKEN``).
INDIGO_API_PREFIX = "/api/indigo"
#: Private namespace (guarded by ``CELA_PRIVATE_API_TOKEN``).
INDIGO_PRIVATE_PREFIX = "/cela/v1/indigo"

INDIGO_ENABLE_ENV = "CELA_INDIGO_ENABLE"
INDIGO_CREDENTIAL_VAR_ENV = "CELA_INDIGO_CREDENTIAL_VAR"
INDIGO_BASE_URL_ENV = "CELA_INDIGO_BASE_URL"

DEFAULT_INDIGO_CREDENTIAL_VAR = "INDIGO_API_KEY"
DEFAULT_INDIGO_BASE_URL = "https://aiindigo.com/api/v1"
DEFAULT_INDIGO_HOST = "aiindigo.com"


def indigo_enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    """AI Indigo surface is opt-in: only ``CELA_INDIGO_ENABLE=1`` enables it."""
    source = env if env is not None else os.environ
    return source.get(INDIGO_ENABLE_ENV, "").strip() == "1"


def resolve_indigo_credential_var(env: Mapping[str, str] | None = None) -> str:
    """Return the environment variable name holding the AI Indigo API key."""
    source = env if env is not None else os.environ
    configured = source.get(INDIGO_CREDENTIAL_VAR_ENV, "").strip()
    return configured or DEFAULT_INDIGO_CREDENTIAL_VAR


def scrub_indigo_secret(text: str, secret: str = "") -> str:
    """Scrub credential patterns plus any literal or URL-encoded *secret* value."""
    raw = str(text)
    if secret:
        raw = raw.replace(secret, "REDACTED_KEY")
        encoded = quote(secret, safe="")
        if encoded and encoded != secret:
            raw = raw.replace(encoded, "REDACTED_KEY")
    return scrub_credentials(raw)


def default_indigo_policy(*, resolve_dns: bool = False) -> EgressPolicy:
    """Allow only HTTPS port 443 to ``aiindigo.com`` above the SSRF floor."""
    rules = [
        EgressRule(
            action="allow",
            host=DEFAULT_INDIGO_HOST,
            schemes={"https"},
            ports={443},
            tools={"indigo_api"},
        )
    ]
    return EgressPolicy(rules=rules, guard=SSRFGuard(resolve_dns=resolve_dns))


class IndigoRateLimit(BaseModel):
    """Rate-limit metadata parsed from ``X-RateLimit-*`` response headers."""

    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None


class IndigoError(Exception):
    """Structured error raised by :class:`IndigoClient` with scrubbed details."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        detail: str,
        rate_limit: IndigoRateLimit | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.rate_limit = rate_limit


def _parse_int_header(headers: httpx.Headers, name: str) -> int | None:
    raw = headers.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_rate_limit_headers(headers: httpx.Headers) -> IndigoRateLimit:
    """Extract optional ``X-RateLimit-*`` integer headers without raising."""
    return IndigoRateLimit(
        limit=_parse_int_header(headers, "X-RateLimit-Limit"),
        remaining=_parse_int_header(headers, "X-RateLimit-Remaining"),
        reset=_parse_int_header(headers, "X-RateLimit-Reset"),
    )


def _normalize_base_url(raw: str) -> str:
    candidate = raw.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(
            "AI Indigo base_url must be a clean https:// URL without embedded credentials"
        )
    return candidate


class IndigoClient:
    """Typed async client for the external AI Indigo REST API v1."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        credential_var: str | None = None,
        policy: EgressPolicy | None = None,
        transport: Any = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._env = env
        raw_base = (
            base_url
            if base_url is not None
            else (env if env is not None else os.environ).get(
                INDIGO_BASE_URL_ENV, DEFAULT_INDIGO_BASE_URL
            )
        )
        self.base_url = _normalize_base_url(raw_base or DEFAULT_INDIGO_BASE_URL)
        self.credential_var = (
            credential_var.strip()
            if credential_var and credential_var.strip()
            else resolve_indigo_credential_var(env)
        )
        self.policy = policy if policy is not None else default_indigo_policy()
        self._transport = transport
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None

    def _read_secret(self) -> str:
        source = self._env if self._env is not None else os.environ
        return source.get(self.credential_var, "").strip()

    @property
    def has_key(self) -> bool:
        """True when a non-empty key exists in ``env[credential_var]``."""
        return bool(self._read_secret())

    def __repr__(self) -> str:
        safe_url = scrub_credentials(self.base_url)
        return (
            f"IndigoClient(base_url={safe_url!r}, "
            f"credential_var={self.credential_var!r}, configured={self.has_key})"
        )

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
                headers={"User-Agent": "cela-indigo-client/0.1", "Accept": "application/json"},
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def request_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        require_key: bool = True,
    ) -> tuple[dict[str, Any], IndigoRateLimit]:
        """Issue one validated GET against ``base_url + path`` and parse JSON."""
        secret = self._read_secret()
        if require_key and not secret:
            raise IndigoError(
                status_code=503,
                code="indigo_unconfigured",
                detail=(
                    f"AI Indigo credential variable {self.credential_var} is unset; "
                    "set it in the server environment before calling key-protected endpoints"
                ),
            )

        clean_path = "/" + path.lstrip("/")
        url = f"{self.base_url}{clean_path}"
        try:
            self.policy.enforce(url, tool="indigo_api")
        except BlockedURLError as exc:
            raise IndigoError(
                status_code=403,
                code="egress_denied",
                detail=f"egress denied: {scrub_indigo_secret(str(exc), secret)}",
            ) from None

        headers: dict[str, str] = {}
        if secret and require_key:
            headers["X-API-Key"] = secret

        filtered_params = {
            k: v for k, v in (params or {}).items() if v is not None and v != ""
        }
        try:
            response = await self._client().get(url, params=filtered_params, headers=headers)
        except Exception as exc:
            raise IndigoError(
                status_code=502,
                code="indigo_upstream_unreachable",
                detail=scrub_indigo_secret(f"{type(exc).__name__}: {exc}", secret),
            ) from None

        rate = parse_rate_limit_headers(response.headers)
        if 300 <= response.status_code < 400:
            raise IndigoError(
                status_code=502,
                code="indigo_unexpected_redirect",
                detail=(
                    f"AI Indigo upstream returned redirect HTTP {response.status_code}; "
                    "refusing to follow redirects with API credentials"
                ),
                rate_limit=rate,
            )

        if response.status_code == 401:
            raise IndigoError(
                status_code=502,
                code="indigo_upstream_unauthorized",
                detail=(
                    f"AI Indigo rejected {self.credential_var} with HTTP 401; "
                    "verify that the API key is active"
                ),
                rate_limit=rate,
            )
        if response.status_code == 403:
            raise IndigoError(
                status_code=403,
                code="indigo_scope_denied",
                detail=(
                    "AI Indigo returned HTTP 403 (tier/scope does not include this endpoint)"
                ),
                rate_limit=rate,
            )
        if response.status_code == 404:
            raise IndigoError(
                status_code=404,
                code="indigo_not_found",
                detail="requested AI Indigo resource was not found",
                rate_limit=rate,
            )
        if response.status_code == 429:
            raise IndigoError(
                status_code=429,
                code="indigo_rate_limited",
                detail="AI Indigo rate limit reached; check rate_limit.reset before retrying",
                rate_limit=rate,
            )
        if response.status_code >= 400:
            raise IndigoError(
                status_code=502,
                code="indigo_upstream_error",
                detail=f"AI Indigo upstream answered HTTP {response.status_code}",
                rate_limit=rate,
            )

        try:
            payload = response.json()
        except ValueError:
            raise IndigoError(
                status_code=502,
                code="indigo_invalid_json",
                detail="AI Indigo upstream returned non-JSON body",
                rate_limit=rate,
            ) from None

        if not isinstance(payload, dict):
            payload = {"data": payload}
        return payload, rate

    async def list_tools(
        self,
        *,
        category: str | None = None,
        pricing: str | None = None,
        sort: str | None = None,
        page: int = 1,
        per_page: int = 20,
        fields: str | None = None,
    ) -> tuple[dict[str, Any], IndigoRateLimit]:
        return await self.request_json(
            "/tools",
            params={
                "category": category,
                "pricing": pricing,
                "sort": sort,
                "page": page,
                "per_page": per_page,
                "fields": fields,
            },
        )

    async def get_tool(self, slug: str) -> tuple[dict[str, Any], IndigoRateLimit]:
        return await self.request_json(f"/tools/{quote(slug.strip(), safe='')}")

    async def compare_tools(self, a: str, b: str) -> tuple[dict[str, Any], IndigoRateLimit]:
        return await self.request_json(
            "/tools/compare",
            params={"a": a.strip(), "b": b.strip()},
        )

    async def search(
        self,
        query: str,
        *,
        mode: str = "keyword",
        category: str | None = None,
        pricing: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[dict[str, Any], IndigoRateLimit]:
        return await self.request_json(
            "/search",
            params={
                "q": query,
                "mode": mode,
                "category": category,
                "pricing": pricing,
                "limit": limit,
                "offset": offset,
            },
        )

    async def list_categories(self) -> tuple[dict[str, Any], IndigoRateLimit]:
        return await self.request_json("/categories")

    async def get_category(self, slug: str) -> tuple[dict[str, Any], IndigoRateLimit]:
        return await self.request_json(f"/categories/{quote(slug.strip(), safe='')}")

    async def trending(
        self,
        *,
        limit: int = 20,
        category: str | None = None,
    ) -> tuple[dict[str, Any], IndigoRateLimit]:
        return await self.request_json(
            "/intelligence/trending",
            params={"limit": limit, "category": category},
            require_key=False,
        )


class IndigoService:
    """Lazy, opt-in service wrapper behind ``/api/indigo/*`` and ``/cela/v1/indigo/*``."""

    def __init__(
        self,
        *,
        enabled: bool,
        policy: EgressPolicy | None = None,
        transport: Any = None,
        base_url: str | None = None,
        credential_var: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._enabled = enabled
        self._policy = policy
        self._transport = transport
        self._base_url = base_url
        self._credential_var = credential_var
        self._env = env
        self._client: IndigoClient | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def client(self) -> IndigoClient:
        if self._client is None:
            self._client = IndigoClient(
                base_url=self._base_url,
                credential_var=self._credential_var,
                policy=self._policy,
                transport=self._transport,
                env=self._env,
            )
        return self._client

    def status(self) -> dict[str, Any]:
        """Secret-safe readiness snapshot; never echoes credential values."""
        credential_var = (
            self._credential_var.strip()
            if self._credential_var and self._credential_var.strip()
            else resolve_indigo_credential_var(self._env)
        )
        source = self._env if self._env is not None else os.environ
        base = (
            self._base_url
            if self._base_url is not None
            else source.get(INDIGO_BASE_URL_ENV, DEFAULT_INDIGO_BASE_URL)
        ) or DEFAULT_INDIGO_BASE_URL
        configured = bool(source.get(credential_var, "").strip())
        return {
            "enabled": self.enabled,
            "configured": configured,
            "credential_var": credential_var,
            "base_url": scrub_credentials(base.strip().rstrip("/")),
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _require_enabled(service: IndigoService) -> IndigoClient:
    if not service.enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "indigo_disabled",
                "detail": (
                    f"the AI Indigo surface is disabled; set {INDIGO_ENABLE_ENV}=1 to enable it"
                ),
            },
        )
    return service.client()


def _to_http_exception(exc: IndigoError) -> HTTPException:
    body: dict[str, Any] = {"code": exc.code, "detail": exc.detail}
    if exc.rate_limit is not None:
        body["rate_limit"] = exc.rate_limit.model_dump()
    return HTTPException(status_code=exc.status_code, detail=body)


def _envelope(payload: dict[str, Any], rate: IndigoRateLimit) -> dict[str, Any]:
    return {
        "upstream": payload,
        "rate_limit": rate.model_dump(),
    }


def create_indigo_router(
    *,
    service: IndigoService,
    require_auth: Any,
    prefix: str = INDIGO_API_PREFIX,
) -> APIRouter:
    """Build the authenticated AI Indigo router for one API namespace."""
    router = APIRouter(prefix=prefix, tags=["indigo"], dependencies=[Depends(require_auth)])

    @router.get("/status")
    async def status() -> dict[str, Any]:
        return service.status()

    @router.get("/tools")
    async def tools(
        category: str | None = Query(default=None, max_length=64),
        pricing: str | None = Query(default=None, pattern="^(free|freemium|paid)$"),
        sort: str | None = Query(
            default=None, pattern="^(popularity|name|rating|newest|reviews)$"
        ),
        page: int = Query(default=1, ge=1, le=1000),
        per_page: int = Query(default=20, ge=1, le=100),
        fields: str | None = Query(default=None, max_length=256),
    ) -> dict[str, Any]:
        client = _require_enabled(service)
        try:
            payload, rate = await client.list_tools(
                category=category,
                pricing=pricing,
                sort=sort,
                page=page,
                per_page=per_page,
                fields=fields,
            )
        except IndigoError as exc:
            raise _to_http_exception(exc) from None
        return _envelope(payload, rate)

    # Declared before ``/tools/{slug}`` so ``compare`` never binds to ``{slug}``.
    @router.get("/tools/compare")
    async def compare(
        a: str = Query(min_length=1, max_length=128),
        b: str = Query(min_length=1, max_length=128),
    ) -> dict[str, Any]:
        client = _require_enabled(service)
        try:
            payload, rate = await client.compare_tools(a, b)
        except IndigoError as exc:
            raise _to_http_exception(exc) from None
        return _envelope(payload, rate)

    @router.get("/tools/{slug}")
    async def tool_detail(slug: str) -> dict[str, Any]:
        client = _require_enabled(service)
        try:
            payload, rate = await client.get_tool(slug)
        except IndigoError as exc:
            raise _to_http_exception(exc) from None
        return _envelope(payload, rate)

    @router.get("/search")
    async def search(
        q: str = Query(min_length=1, max_length=200),
        mode: str = Query(default="keyword", pattern="^(keyword|semantic|hybrid)$"),
        category: str | None = Query(default=None, max_length=64),
        pricing: str | None = Query(default=None, pattern="^(free|freemium|paid)$"),
        limit: int = Query(default=10, ge=1, le=50),
        offset: int = Query(default=0, ge=0, le=10_000),
    ) -> dict[str, Any]:
        client = _require_enabled(service)
        try:
            payload, rate = await client.search(
                q,
                mode=mode,
                category=category,
                pricing=pricing,
                limit=limit,
                offset=offset,
            )
        except IndigoError as exc:
            raise _to_http_exception(exc) from None
        return _envelope(payload, rate)

    @router.get("/categories")
    async def categories() -> dict[str, Any]:
        client = _require_enabled(service)
        try:
            payload, rate = await client.list_categories()
        except IndigoError as exc:
            raise _to_http_exception(exc) from None
        return _envelope(payload, rate)

    @router.get("/categories/{slug}")
    async def category_detail(slug: str) -> dict[str, Any]:
        client = _require_enabled(service)
        try:
            payload, rate = await client.get_category(slug)
        except IndigoError as exc:
            raise _to_http_exception(exc) from None
        return _envelope(payload, rate)

    @router.get("/trending")
    async def trending(
        limit: int = Query(default=20, ge=1, le=50),
        category: str | None = Query(default=None, max_length=64),
    ) -> dict[str, Any]:
        client = _require_enabled(service)
        try:
            payload, rate = await client.trending(limit=limit, category=category)
        except IndigoError as exc:
            raise _to_http_exception(exc) from None
        return _envelope(payload, rate)

    return router


__all__ = [
    "DEFAULT_INDIGO_BASE_URL",
    "DEFAULT_INDIGO_CREDENTIAL_VAR",
    "DEFAULT_INDIGO_HOST",
    "INDIGO_API_PREFIX",
    "INDIGO_BASE_URL_ENV",
    "INDIGO_CREDENTIAL_VAR_ENV",
    "INDIGO_ENABLE_ENV",
    "INDIGO_PRIVATE_PREFIX",
    "IndigoClient",
    "IndigoError",
    "IndigoRateLimit",
    "IndigoService",
    "create_indigo_router",
    "default_indigo_policy",
    "indigo_enabled_from_env",
    "parse_rate_limit_headers",
    "resolve_indigo_credential_var",
    "scrub_indigo_secret",
]
