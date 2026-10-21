"""Tests for socket-level DNS pinning while preserving the requested origin."""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpcore
import httpx
import pytest
from os_tools.egress import EgressPolicy, EgressVerdict
from os_tools.pinned_http import PinnedAsyncHTTPTransport, PinnedHTTPTransport


def _resolved_public(host: str, port: int, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _verdict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolved_public)
    return EgressPolicy.allow_hosts("example.org").enforce("https://example.org/data")


async def test_async_transport_pins_the_first_dns_result_and_preserves_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_calls = 0

    def rebind_after_authorization(host: str, port: int, *args, **kwargs):
        nonlocal dns_calls
        dns_calls += 1
        address = "93.184.216.34" if dns_calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", rebind_after_authorization)
    verdict = EgressPolicy.allow_hosts("example.org").enforce("https://example.org/data")
    assert verdict.connect_ip == "93.184.216.34"

    transport = PinnedAsyncHTTPTransport(verdict)
    backend = transport._transport._pool._network_backend

    class RecordingBackend:
        calls: list[tuple[str, int]]

        def __init__(self) -> None:
            self.calls = []

        async def connect_tcp(self, host: str, port: int, **kwargs):
            self.calls.append((host, port))
            return object()

        async def sleep(self, seconds: float) -> None:
            return None

    recording = RecordingBackend()
    backend._delegate = recording
    await backend.connect_tcp("example.org", 443)

    assert recording.calls == [("93.184.216.34", 443)]
    assert dns_calls == 1, "the connection path must not perform a second DNS lookup"
    with pytest.raises(httpcore.ConnectError, match="unverified destination"):
        await backend.connect_tcp("127.0.0.1", 443)

    await transport.aclose()

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    mock_transport = PinnedAsyncHTTPTransport(verdict, transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=mock_transport, trust_env=False) as client:
        response = await client.get("https://example.org/data")

    assert response.text == "ok"
    assert seen[0].url.host == "example.org"
    assert seen[0].headers["host"] == "example.org"


def test_sync_transport_uses_pinned_backend_and_keeps_host_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = _verdict(monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    transport = PinnedHTTPTransport(verdict, transport=httpx.MockTransport(handler))
    with httpx.Client(transport=transport, trust_env=False) as client:
        response = client.get("https://example.org/data")

    assert response.text == "ok"
    assert seen[0].url.host == "example.org"
    assert seen[0].headers["host"] == "example.org"


def _start_local_http_server(
    seen: list[tuple[str, str]],
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            seen.append((self.headers["Host"], self.path))
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker


def _local_verdict(port: int) -> EgressVerdict:
    return EgressVerdict(
        allowed=True,
        url=f"http://example.org:{port}/pinned",
        host="example.org",
        connect_ip="127.0.0.1",
        reason="isolated transport test",
        scheme="http",
        port=port,
        tool="test",
    )


def _make_test_certificate(tmp_path: Path) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL is required to generate an ephemeral TLS test certificate")
    cert_path = tmp_path / "server-cert.pem"
    key_path = tmp_path / "server-key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "365",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-subj",
            "/CN=example.org",
            "-addext",
            "subjectAltName=DNS:example.org",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return cert_path, key_path


def _start_local_https_server(
    seen: list[tuple[str, str]],
    sni_names: list[str | None],
    cert_path: Path,
    key_path: Path,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            seen.append((self.headers["Host"], self.path))
            body = b"tls ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    context.set_servername_callback(
        lambda _ssl_socket, server_name, _context: sni_names.append(server_name)
    )
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker


def _tls_verdict(host: str, port: int) -> EgressVerdict:
    return EgressVerdict(
        allowed=True,
        url=f"https://{host}:{port}/pinned",
        host=host,
        connect_ip="127.0.0.1",
        reason="isolated TLS transport test",
        scheme="https",
        port=port,
        tool="test",
    )


def test_tls_pinning_preserves_sni_host_and_certificate_validation(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []
    sni_names: list[str | None] = []
    cert_path, key_path = _make_test_certificate(tmp_path)
    server, worker = _start_local_https_server(seen, sni_names, cert_path, key_path)
    port = server.server_address[1]
    trusted_context = ssl.create_default_context(cafile=cert_path)
    try:
        transport = PinnedHTTPTransport(_tls_verdict("example.org", port), verify=trusted_context)
        with httpx.Client(transport=transport, trust_env=False) as client:
            response = client.get(f"https://example.org:{port}/pinned")
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()

    assert response.status_code == 200
    assert response.text == "tls ok"
    assert seen == [(f"example.org:{port}", "/pinned")]
    assert sni_names == ["example.org"]


async def test_async_tls_pinning_preserves_sni_host_and_certificate_validation(
    tmp_path: Path,
) -> None:
    seen: list[tuple[str, str]] = []
    sni_names: list[str | None] = []
    cert_path, key_path = _make_test_certificate(tmp_path)
    server, worker = _start_local_https_server(seen, sni_names, cert_path, key_path)
    port = server.server_address[1]
    trusted_context = ssl.create_default_context(cafile=cert_path)
    try:
        transport = PinnedAsyncHTTPTransport(
            _tls_verdict("example.org", port), verify=trusted_context
        )
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            response = await client.get(f"https://example.org:{port}/pinned")
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()

    assert response.status_code == 200
    assert response.text == "tls ok"
    assert seen == [(f"example.org:{port}", "/pinned")]
    assert sni_names == ["example.org"]


def test_tls_pinning_rejects_a_trusted_certificate_for_the_wrong_hostname(
    tmp_path: Path,
) -> None:
    seen: list[tuple[str, str]] = []
    sni_names: list[str | None] = []
    cert_path, key_path = _make_test_certificate(tmp_path)
    server, worker = _start_local_https_server(seen, sni_names, cert_path, key_path)
    port = server.server_address[1]
    trusted_context = ssl.create_default_context(cafile=cert_path)
    try:
        transport = PinnedHTTPTransport(
            _tls_verdict("wrong.example.org", port), verify=trusted_context
        )
        with (
            httpx.Client(transport=transport, trust_env=False) as client,
            pytest.raises(httpx.ConnectError),
        ):
            client.get(f"https://wrong.example.org:{port}/pinned")
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()

    assert sni_names == ["wrong.example.org"]
    assert seen == []


def test_pinned_transport_never_disables_tls_certificate_verification() -> None:
    with pytest.raises(ValueError, match="cannot be disabled"):
        PinnedHTTPTransport(_tls_verdict("example.org", 443), verify=False)


def test_real_http_socket_connects_to_the_pin_but_keeps_the_original_host() -> None:
    seen: list[tuple[str, str]] = []
    server, worker = _start_local_http_server(seen)
    port = server.server_address[1]
    try:
        with httpx.Client(transport=PinnedHTTPTransport(_local_verdict(port))) as client:
            response = client.get(f"http://example.org:{port}/pinned")
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()

    assert response.status_code == 200
    assert response.text == "ok"
    assert seen == [(f"example.org:{port}", "/pinned")]


async def test_real_async_http_socket_connects_to_the_pin_but_keeps_the_original_host() -> None:
    seen: list[tuple[str, str]] = []
    server, worker = _start_local_http_server(seen)
    port = server.server_address[1]
    try:
        async with httpx.AsyncClient(
            transport=PinnedAsyncHTTPTransport(_local_verdict(port))
        ) as client:
            response = await client.get(f"http://example.org:{port}/pinned")
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()

    assert response.status_code == 200
    assert response.text == "ok"
    assert seen == [(f"example.org:{port}", "/pinned")]


@pytest.mark.parametrize("change", ["scheme", "port"])
def test_pinned_transport_rejects_redirects_that_change_scheme_or_port(change: str) -> None:
    port = 43123
    if change == "scheme":
        location = f"https://example.org:{port}/second"
    else:
        location = f"http://example.org:{port + 1}/second"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(307, headers={"Location": location}, request=request)

    transport = PinnedHTTPTransport(_local_verdict(port), transport=httpx.MockTransport(handler))
    with (
        httpx.Client(transport=transport, follow_redirects=True) as client,
        pytest.raises(httpx.ConnectError, match="unverified origin"),
    ):
        client.get(f"http://example.org:{port}/first")

    assert len(seen) == 1, "the redirected request must not reach the transport backend"


def _two_public_dns_answers(host: str, port: int | None, *args, **kwargs):
    return [
        (
            socket.AF_INET6,
            socket.SOCK_STREAM,
            6,
            "",
            ("2606:2800:220:1:248:1893:25c8:1946", 0, 0, 0),
        ),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 0)),
    ]


def test_sync_transport_falls_back_to_another_validated_dns_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _two_public_dns_answers)
    verdict = EgressPolicy.allow_hosts("example.org").enforce("https://example.org/data")
    assert verdict.connect_ips == ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.35"]

    transport = PinnedHTTPTransport(verdict)
    backend = transport._transport._pool._network_backend

    class RecordingBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def connect_tcp(self, host: str, port: int, **kwargs):
            self.calls.append((host, port))
            if len(self.calls) == 1:
                raise httpcore.ConnectError("first address is unavailable")
            return "connected on fallback"

    recording = RecordingBackend()
    backend._delegate = recording
    assert backend.connect_tcp("example.org", 443) == "connected on fallback"
    assert recording.calls == [("2606:2800:220:1:248:1893:25c8:1946", 443), ("93.184.216.35", 443)]
    transport.close()


async def test_async_transport_falls_back_to_another_validated_dns_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _two_public_dns_answers)
    verdict = EgressPolicy.allow_hosts("example.org").enforce("https://example.org/data")
    assert verdict.connect_ips == ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.35"]

    transport = PinnedAsyncHTTPTransport(verdict)
    backend = transport._transport._pool._network_backend

    class RecordingBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def connect_tcp(self, host: str, port: int, **kwargs):
            self.calls.append((host, port))
            if len(self.calls) == 1:
                raise httpcore.ConnectError("first address is unavailable")
            return "connected on fallback"

        async def sleep(self, seconds: float) -> None:
            return None

    recording = RecordingBackend()
    backend._delegate = recording
    assert await backend.connect_tcp("example.org", 443) == "connected on fallback"
    assert recording.calls == [("2606:2800:220:1:248:1893:25c8:1946", 443), ("93.184.216.35", 443)]
    await transport.aclose()


def test_pinned_transport_refuses_an_unresolved_real_network_verdict() -> None:
    verdict = EgressPolicy.allow_hosts("example.org", resolve_dns=False).enforce(
        "https://example.org/data", resolve_dns=False
    )
    assert verdict.allowed and verdict.connect_ip is None

    with pytest.raises(ValueError, match="connect_ip is required"):
        PinnedAsyncHTTPTransport(verdict)
