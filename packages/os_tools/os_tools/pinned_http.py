"""HTTP transports that keep a policy-approved destination pinned to its IP.

HTTPX normally resolves a hostname when the socket is opened, after an egress
policy may already have resolved it. That second lookup leaves a DNS-rebinding
window. These transports preserve the request URL, Host header, TLS certificate
validation, and SNI, while replacing only the socket-level DNS destination with
the validated IP set returned by :class:`~os_tools.egress.EgressPolicy`. They
try later approved addresses only after connect failures and never resolve again.

An injected ``httpx.MockTransport`` is the sole exception: it performs no socket
I/O and is used as the hermetic test seam throughout this repository. Other
custom transports are rejected because their connection behavior cannot be
pinned or verified here.
"""

from __future__ import annotations

import ipaddress
import ssl
from typing import Any

import httpcore
import httpx

from os_tools.egress import EgressVerdict, parse_http_target


def _host_key(host: str) -> str:
    """Canonical comparison key for URL/HTTPCore host spellings."""
    candidate = host.strip().lower().rstrip(".")
    try:
        return ipaddress.ip_address(candidate).compressed.lower()
    except ValueError:
        try:
            return candidate.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError(f"invalid destination hostname {host!r}") from exc


def _validated_tls_verify(
    verify: bool | str | ssl.SSLContext,
) -> bool | str | ssl.SSLContext:
    """Allow custom trust roots without permitting a TLS-verification bypass."""
    if verify is False:
        raise ValueError("TLS certificate verification cannot be disabled on a pinned transport")
    if isinstance(verify, ssl.SSLContext) and (
        verify.verify_mode != ssl.CERT_REQUIRED or not verify.check_hostname
    ):
        raise ValueError("custom TLS contexts must require certificates and check hostnames")
    return verify


class _PinnedBackend:
    """Route one authorized HTTPCore origin to its already-approved IP set."""

    def __init__(self, delegate: Any, verdict: EgressVerdict) -> None:
        self._delegate = delegate
        self._host = _host_key(verdict.host)
        self._port = verdict.port
        raw_ips = verdict.connect_ips or ([verdict.connect_ip] if verdict.connect_ip else [])
        if not raw_ips:
            raise ValueError("a resolved connect_ip is required for a network transport")
        approved: list[str] = []
        for raw_ip in raw_ips:
            try:
                address = ipaddress.ip_address(raw_ip)
            except ValueError as exc:
                raise ValueError(f"invalid approved connect_ip {raw_ip!r}") from exc
            if getattr(address, "scope_id", None):
                raise ValueError("scoped IP addresses cannot be used as pinned destinations")
            canonical = address.compressed
            if canonical not in approved:
                approved.append(canonical)
        self._ips = tuple(approved)
        # Keep the historical attribute for callers and tests that inspect the
        # first candidate; network connection attempts iterate the full set.
        self._ip = self._ips[0]

    def _assert_target(self, host: str, port: int) -> None:
        try:
            host_key = _host_key(host)
        except ValueError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        if host_key != self._host or port != self._port:
            raise httpcore.ConnectError(
                f"pinned transport refused unverified destination {host}:{port}"
            )


class _PinnedAsyncBackend(_PinnedBackend):
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors the HTTPCore protocol
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        self._assert_target(host, port)
        failures: list[BaseException] = []
        for ip in self._ips:
            try:
                return await self._delegate.connect_tcp(
                    ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                failures.append(exc)
        summary = "; ".join(
            f"{ip}: {type(exc).__name__}" for ip, exc in zip(self._ips, failures, strict=True)
        )
        raise httpcore.ConnectError(
            f"all approved addresses failed for {self._host}:{port} ({summary})"
        ) from (failures[-1] if failures else None)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors the HTTPCore protocol
        socket_options: Any = None,
    ) -> Any:
        raise httpcore.ConnectError("Unix sockets are not permitted by a pinned HTTP transport")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class _PinnedSyncBackend(_PinnedBackend):
    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        self._assert_target(host, port)
        failures: list[BaseException] = []
        for ip in self._ips:
            try:
                return self._delegate.connect_tcp(
                    ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                failures.append(exc)
        summary = "; ".join(
            f"{ip}: {type(exc).__name__}" for ip, exc in zip(self._ips, failures, strict=True)
        )
        raise httpcore.ConnectError(
            f"all approved addresses failed for {self._host}:{port} ({summary})"
        ) from (failures[-1] if failures else None)

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> Any:
        raise httpcore.ConnectError("Unix sockets are not permitted by a pinned HTTP transport")

    def sleep(self, seconds: float) -> None:
        self._delegate.sleep(seconds)


def _validate_request(request: httpx.Request, verdict: EgressVerdict) -> None:
    try:
        scheme, host, port = parse_http_target(str(request.url))
    except ValueError as exc:
        raise httpx.ConnectError(
            f"pinned transport refused invalid URL: {exc}", request=request
        ) from None
    if (scheme, _host_key(host), port) != (
        verdict.scheme,
        _host_key(verdict.host),
        verdict.port,
    ):
        raise httpx.ConnectError(
            f"pinned transport refused unverified origin {scheme}://{host}:{port}",
            request=request,
        )


class PinnedAsyncHTTPTransport(httpx.AsyncBaseTransport):
    """Async HTTPX transport with a policy-approved DNS pin set."""

    def __init__(
        self,
        verdict: EgressVerdict,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: bool | str | ssl.SSLContext = True,
    ) -> None:
        if not verdict.allowed:
            raise ValueError("cannot create a pinned transport from a denied verdict")
        verify = _validated_tls_verify(verify)
        if transport is None and not (verdict.connect_ips or verdict.connect_ip):
            raise ValueError("a resolved connect_ip is required for a network transport")
        self.verdict = verdict
        if transport is not None:
            if not isinstance(transport, httpx.MockTransport):
                raise TypeError("only httpx.MockTransport may bypass the pinned network backend")
            self._transport: httpx.AsyncBaseTransport = transport
            self._mock = True
            return

        self._transport = httpx.AsyncHTTPTransport(
            verify=verify,
            trust_env=False,
            proxy=None,
        )
        self._mock = False
        pool = getattr(self._transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if pool is None or backend is None:
            raise RuntimeError("HTTPX no longer exposes the HTTPCore network backend")
        pool._network_backend = _PinnedAsyncBackend(backend, verdict)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _validate_request(request, self.verdict)
        if not self._mock and not (self.verdict.connect_ips or self.verdict.connect_ip):
            raise httpx.ConnectError(
                "pinned transport requires an approved connect_ip", request=request
            )
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


class PinnedHTTPTransport(httpx.BaseTransport):
    """Sync HTTPX transport with a policy-approved DNS pin set."""

    def __init__(
        self,
        verdict: EgressVerdict,
        *,
        transport: httpx.BaseTransport | None = None,
        verify: bool | str | ssl.SSLContext = True,
    ) -> None:
        if not verdict.allowed:
            raise ValueError("cannot create a pinned transport from a denied verdict")
        verify = _validated_tls_verify(verify)
        if transport is None and not (verdict.connect_ips or verdict.connect_ip):
            raise ValueError("a resolved connect_ip is required for a network transport")
        self.verdict = verdict
        if transport is not None:
            if not isinstance(transport, httpx.MockTransport):
                raise TypeError("only httpx.MockTransport may bypass the pinned network backend")
            self._transport: httpx.BaseTransport = transport
            self._mock = True
            return

        self._transport = httpx.HTTPTransport(
            verify=verify,
            trust_env=False,
            proxy=None,
        )
        self._mock = False
        pool = getattr(self._transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if pool is None or backend is None:
            raise RuntimeError("HTTPX no longer exposes the HTTPCore network backend")
        pool._network_backend = _PinnedSyncBackend(backend, verdict)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        _validate_request(request, self.verdict)
        if not self._mock and not (self.verdict.connect_ips or self.verdict.connect_ip):
            raise httpx.ConnectError(
                "pinned transport requires an approved connect_ip", request=request
            )
        return self._transport.handle_request(request)

    def close(self) -> None:
        self._transport.close()
