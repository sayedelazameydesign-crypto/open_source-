"""Explicit network egress policy tests (default-deny + SSRF boundary floor)."""

from __future__ import annotations

import pytest
from os_core.types import ToolStatus
from os_server.demo import build_registry, demo_browser, demo_egress_policy
from os_tools.egress import (
    EgressPolicy,
    EgressPolicyError,
    EgressRule,
    EgressVerdict,
    parse_http_target,
)
from os_tools.ssrf import BlockedURLError, SSRFGuard


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.org", ("https", "example.org", 443)),
        ("https://example.org/search?q=1", ("https", "example.org", 443)),
        ("http://example.org", ("http", "example.org", 80)),
        ("HTTPS://Sub.Example.ORG./path", ("https", "sub.example.org", 443)),
        ("https://example.org:8443/api", ("https", "example.org", 8443)),
        ("http://example.org:8080/", ("http", "example.org", 8080)),
        ("  https://example.org/trimmed  ", ("https", "example.org", 443)),
        ("http://93.184.216.34/index.html", ("http", "93.184.216.34", 80)),
        ("https://example.org:1/", ("https", "example.org", 1)),
        ("https://example.org:65535/", ("https", "example.org", 65535)),
    ],
)
def test_parse_http_target_normalises_valid_urls(url: str, expected: tuple[str, str, int]) -> None:
    assert parse_http_target(url) == expected


@pytest.mark.parametrize(
    "bad_url",
    [
        "ftp://example.org",
        "file:///etc/passwd",
        "gopher://example.org:70/",
        "javascript:alert(1)",
        "ws://example.org/ws",
        "wss://example.org/ws",
        "data:text/html,<h1>hi</h1>",
        "mailto:root@example.org",
        "//example.org/path",
        "example.org/path",
        "https://",
        "http:///no-host",
        "",
        "   ",
        "https://example.org/has space",
        "https://user:pass@example.org/",
        "https://user@example.org/",
        "https://example.org:0/",
        "https://example.org:65536/",
        "https://example.org:notaport/",
    ],
)
def test_parse_http_target_rejects_invalid_urls(bad_url: str) -> None:
    with pytest.raises(ValueError, match=r"is not an http\(s\) URL with a host"):
        parse_http_target(bad_url)


def test_parse_http_target_rejects_non_string() -> None:
    with pytest.raises(ValueError, match=r"is not an http\(s\) URL with a host"):
        parse_http_target(None)  # type: ignore[arg-type]


def test_ftp_error_message_matches_contract_verbatim() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_http_target("ftp://example.org")
    assert str(exc_info.value) == "'ftp://example.org' is not an http(s) URL with a host"


@pytest.mark.parametrize(
    ("pattern", "candidate", "matches"),
    [
        ("example.org", "example.org", True),
        ("example.org", "EXAMPLE.ORG.", True),
        ("example.org", "sub.example.org", False),
        ("example.org", "badexample.org", False),
        ("*.example.org", "sub.example.org", True),
        ("*.example.org", "a.b.example.org", True),
        ("*.example.org", "example.org", False),
        ("*.example.org", "evil-example.org", False),
        ("*", "example.org", True),
        ("*", "anything.test", True),
    ],
)
def test_rule_host_matching(pattern: str, candidate: str, matches: bool) -> None:
    rule = EgressRule(action="allow", host=pattern)
    assert rule.matches(scheme="https", host=candidate, port=443, tool="browser") is matches


@pytest.mark.parametrize(
    "bad_host",
    [
        "",
        "   ",
        "*.",
        "*...",
        ".",
        "bad host.example.org",
        "sub.*.example.org",
        "*.*.example.org",
        "exa*mple.org",
    ],
)
def test_rule_rejects_invalid_host_pattern(bad_host: str) -> None:
    with pytest.raises(ValueError):
        EgressRule(host=bad_host)


@pytest.mark.parametrize(
    "bad_schemes",
    [set(), {"ftp"}, {"http", "ws"}, {"javascript"}],
)
def test_rule_rejects_invalid_schemes(bad_schemes: set[str]) -> None:
    with pytest.raises(ValueError, match="schemes"):
        EgressRule(host="example.org", schemes=bad_schemes)


@pytest.mark.parametrize(
    "bad_ports",
    [set(), {0}, {65536}, {-1}, {80, 70000}],
)
def test_rule_rejects_invalid_ports(bad_ports: set[int]) -> None:
    with pytest.raises(ValueError, match="ports"):
        EgressRule(host="example.org", ports=bad_ports)


def test_empty_policy_denies_by_default() -> None:
    policy = EgressPolicy()
    verdict = policy.check("https://evil.net")
    assert not verdict.allowed
    assert verdict.reason == "no allow rule permits https://evil.net:443 for 'browser'"


def test_allow_hosts_permits_listed_and_blocks_unlisted() -> None:
    policy = EgressPolicy.allow_hosts("example.org", "*.example.org")
    assert policy.check("https://example.org/search").allowed
    assert policy.check("https://docs.example.org/guide").allowed
    denied = policy.check("https://evil.net/phish")
    assert not denied.allowed
    assert denied.reason == "no allow rule permits https://evil.net:443 for 'browser'"


def test_scheme_and_port_restrictions_are_enforced() -> None:
    policy = EgressPolicy.allow_hosts("example.org", schemes={"https"}, ports={443})
    assert policy.check("https://example.org/ok").allowed

    http_verdict = policy.check("http://example.org/insecure")
    assert not http_verdict.allowed
    assert http_verdict.reason == "no allow rule permits http://example.org:80 for 'browser'"

    port_verdict = policy.check("https://example.org:8443/alt")
    assert not port_verdict.allowed
    assert port_verdict.reason == "no allow rule permits https://example.org:8443 for 'browser'"


def test_tool_scoping_restricts_rules_to_named_tool() -> None:
    policy = EgressPolicy.allow_hosts("example.org", tools={"browser"})
    assert policy.check("https://example.org/", tool="browser").allowed
    other = policy.check("https://example.org/", tool="webhook")
    assert not other.allowed
    assert other.reason == "no allow rule permits https://example.org:443 for 'webhook'"


def test_deny_rules_override_allow_rules_regardless_of_order() -> None:
    policy = EgressPolicy(
        rules=[
            EgressRule(action="allow", host="*.example.org"),
            EgressRule(action="deny", host="admin.example.org"),
        ]
    )
    assert policy.check("https://docs.example.org/").allowed
    blocked = policy.check("https://admin.example.org/")
    assert not blocked.allowed
    assert blocked.matched_rule == "deny:admin.example.org"
    assert "deny rule 'admin.example.org' blocks https://admin.example.org:443" in blocked.reason


@pytest.mark.parametrize(
    "internal_url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/admin",
        "http://localhost:8080/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://[::1]/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://100.100.100.200/latest/meta-data/",
        "http://0.0.0.0/",
    ],
)
def test_wildcard_allow_rule_never_overrides_ssrf_network_boundary(internal_url: str) -> None:
    """Even host='*' across all ports and schemes cannot bypass SSRFGuard."""
    permissive = EgressPolicy(
        rules=[
            EgressRule(action="allow", host="*", schemes={"http", "https"}, ports={80, 443, 8080})
        ]
    )
    verdict = permissive.check(internal_url)
    assert not verdict.allowed
    assert verdict.reason.startswith("network boundary: ")


def test_metadata_ip_error_message_matches_contract_verbatim() -> None:
    policy = EgressPolicy.allow_hosts("*")
    verdict = policy.check("http://169.254.169.254/latest/meta-data/")
    assert not verdict.allowed
    assert verdict.reason == "network boundary: 169.254.169.254 is a cloud metadata endpoint"


def test_dns_rebinding_is_stopped_at_network_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
    )
    policy = EgressPolicy(
        rules=[EgressRule(action="allow", host="rebind.example.org")],
        guard=SSRFGuard(resolve_dns=True),
    )
    verdict = policy.check("https://rebind.example.org/")
    assert not verdict.allowed
    assert verdict.reason.startswith("network boundary: ")
    assert "127.0.0.1" in verdict.reason


def test_enforce_raises_egress_policy_error_which_is_a_blocked_url_error() -> None:
    policy = EgressPolicy.allow_hosts("example.org")
    verdict = policy.enforce("https://example.org/search", resolve_dns=False)
    assert isinstance(verdict, EgressVerdict)
    assert verdict.allowed

    with pytest.raises(EgressPolicyError) as exc_info:
        policy.enforce("https://evil.net/", resolve_dns=False)
    assert isinstance(exc_info.value, BlockedURLError)
    assert str(exc_info.value) == "no allow rule permits https://evil.net:443 for 'browser'"


def test_enforce_raises_value_error_on_bad_scheme() -> None:
    policy = EgressPolicy.allow_hosts("example.org")
    with pytest.raises(ValueError) as exc_info:
        policy.enforce("ftp://example.org")
    assert str(exc_info.value) == "'ftp://example.org' is not an http(s) URL with a host"


def test_check_records_audit_log() -> None:
    policy = EgressPolicy.allow_hosts("example.org")
    policy.check("https://example.org/a")
    policy.check("https://evil.net/b")
    policy.check("ftp://example.org")
    assert len(policy.audit_log) == 3
    assert [v.allowed for v in policy.audit_log] == [True, False, False]


def test_allowed_hosts_summary_filters_by_tool_and_action() -> None:
    policy = EgressPolicy(
        rules=[
            EgressRule(action="allow", host="example.org", tools={"browser"}),
            EgressRule(action="deny", host="bad.example.org", tools={"browser"}),
            EgressRule(action="allow", host="api.example.org", tools={"http"}),
            EgressRule(action="allow", host="shared.example.org", tools=None),
        ]
    )
    assert policy.allowed_hosts_summary(tool="browser") == ["example.org", "shared.example.org"]
    assert policy.allowed_hosts_summary(tool="http") == ["api.example.org", "shared.example.org"]


async def test_browser_tool_enforces_egress_policy() -> None:
    browser = demo_browser()
    policy = demo_egress_policy()
    fake_check = policy.for_tool("browser", resolve_dns=False)
    ok = await browser.navigate("https://example.org/search", ssrf_check=fake_check)
    assert ok.status is ToolStatus.SUCCESS

    denied = await browser.navigate("https://evil.net/login", ssrf_check=fake_check)
    assert denied.status is ToolStatus.DENIED
    assert "no allow rule permits https://evil.net:443 for 'browser'" in (denied.error or "")
    assert "https://evil.net/login" in browser.blocked_urls


async def test_browser_hook_passes_approved_dns_address_to_backend(monkeypatch) -> None:
    import socket

    def resolve_public_ip(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve_public_ip)
    browser = demo_browser()
    policy = EgressPolicy.allow_hosts("example.org", resolve_dns=True)
    result = await browser.navigate(
        "https://example.org/search", ssrf_check=policy.for_tool("browser")
    )

    assert result.status is ToolStatus.SUCCESS
    assert browser.backend.navigated_connect_ips == ["93.184.216.34"]


async def test_build_registry_wires_default_deny_egress_into_browser_navigate() -> None:
    registry, _guard, _browser = build_registry()
    nav = registry.tools["browser_navigate"]
    ok = await nav.fn(url="https://example.org/search")
    assert ok.status is ToolStatus.SUCCESS

    denied = await nav.fn(url="https://unlisted.example.com/")
    assert denied.status is ToolStatus.DENIED
    assert "no allow rule permits https://unlisted.example.com:443 for 'browser'" in (
        denied.error or ""
    )


@pytest.mark.parametrize(
    ("url", "expected_allowed", "expected_rule"),
    [
        ("https://example.org/", True, "allow:example.org"),
        ("https://example.org:443/search", True, "allow:example.org"),
        ("https://sub.example.org/page", True, "allow:*.example.org"),
        ("https://deep.sub.example.org/page", True, "allow:*.example.org"),
        ("http://example.org/insecure", False, None),
        ("https://example.org:8443/custom-port", False, None),
        ("https://example.com/other-tld", False, None),
        ("https://notexample.org/prefix", False, None),
    ],
)
def test_demo_egress_policy_matrix(
    url: str, expected_allowed: bool, expected_rule: str | None
) -> None:
    verdict = demo_egress_policy().check(url)
    assert verdict.allowed is expected_allowed
    assert verdict.matched_rule == expected_rule


def test_raise_if_blocked_and_raise_if_denied_are_equivalent() -> None:
    policy = EgressPolicy.allow_hosts("example.org")
    denied = policy.check("https://evil.net")
    with pytest.raises(EgressPolicyError):
        denied.raise_if_blocked()
    with pytest.raises(EgressPolicyError):
        denied.raise_if_denied()


def test_verdict_preserves_connect_ip_on_boundary_denial() -> None:
    policy = EgressPolicy.allow_hosts("*")
    verdict = policy.check("http://127.0.0.1:8080/internal")
    assert not verdict.allowed
    assert verdict.connect_ip == "127.0.0.1"
    assert verdict.reason == "network boundary: 127.0.0.1 is loopback"


def test_custom_egress_policy_can_be_passed_to_build_registry() -> None:
    custom = EgressPolicy.allow_hosts("custom.org", schemes={"https"}, ports={443})
    registry, _guard, _browser = build_registry(egress_policy=custom)
    assert "browser_navigate" in registry.tools
