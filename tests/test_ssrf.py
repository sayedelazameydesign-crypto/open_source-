"""SSRF guard — the network egress boundary of the sandbox."""

from __future__ import annotations

import pytest
from os_tools.ssrf import SSRFGuard


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://127.0.0.1:8080/admin",
        "http://localhost:3000/",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://[::1]/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/router",
        "http://172.16.4.9/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
    ],
)
def test_internal_targets_are_blocked(guard: SSRFGuard, url: str) -> None:
    verdict = guard.check(url)
    assert not verdict.allowed, url
    assert verdict.reason


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://internal:25/",
        "ftp://127.0.0.1/",
        "javascript:alert(1)",
        "dict://x/y",
    ],
)
def test_non_http_schemes_are_blocked(guard: SSRFGuard, url: str) -> None:
    assert not guard.check(url).allowed


@pytest.mark.parametrize("url", ["//example.org/x", "example.org/x", "http://"])
def test_malformed_urls_are_blocked(guard: SSRFGuard, url: str) -> None:
    assert not guard.check(url).allowed


@pytest.mark.parametrize(
    "url", ["https://example.org/", "https://8.8.8.8/lookup", "http://93.184.216.34/"]
)
def test_public_targets_are_allowed(guard: SSRFGuard, url: str) -> None:
    verdict = guard.check(url)
    assert verdict.allowed, verdict.reason


def test_raise_if_blocked_raises() -> None:
    from os_tools.ssrf import BlockedURLError

    with pytest.raises(BlockedURLError):
        SSRFGuard(resolve_dns=False).check("http://127.0.0.1/").raise_if_blocked()


def test_allowlist_mode_rejects_unlisted_host() -> None:
    guard = SSRFGuard(resolve_dns=False, allowed_hosts={"example.org"})
    assert guard.check("https://example.org/ok").allowed
    assert not guard.check("https://evil.test/").allowed


def test_extra_blocked_hosts_extends_the_deny_list() -> None:
    guard = SSRFGuard(resolve_dns=False, extra_blocked_hosts={"corp.example"})
    assert not guard.check("https://corp.example/").allowed


def test_dns_rebinding_to_internal_ip_is_blocked() -> None:
    """A public-looking name that resolves to loopback must be refused."""

    class RebindingGuard(SSRFGuard):
        pass

    guard = RebindingGuard(resolve_dns=True)
    import socket

    original = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    socket.getaddrinfo = fake_getaddrinfo  # type: ignore[assignment]
    try:
        verdict = guard.check("https://looks-public.example/")
    finally:
        socket.getaddrinfo = original  # type: ignore[assignment]
    assert not verdict.allowed
    assert verdict.connect_ip == "127.0.0.1"
    assert "SSRF" in verdict.reason


def test_dns_failure_is_reported_not_raised() -> None:
    guard = SSRFGuard(resolve_dns=True)
    import socket

    original = socket.getaddrinfo

    def boom(*args, **kwargs):
        raise socket.gaierror("no such host")

    socket.getaddrinfo = boom  # type: ignore[assignment]
    try:
        verdict = guard.check("https://definitely-not-real.invalid/")
    finally:
        socket.getaddrinfo = original  # type: ignore[assignment]
    assert not verdict.allowed
    assert "DNS resolution failed" in verdict.reason


def test_allow_private_mode_still_blocks_metadata() -> None:
    """Dev convenience must never re-open the cloud-metadata hole."""
    guard = SSRFGuard(resolve_dns=False, allow_private=True)
    assert guard.check("http://10.0.0.5/").allowed
    assert not guard.check("http://169.254.169.254/latest/meta-data/").allowed
    assert not guard.check("http://127.0.0.1/").allowed
