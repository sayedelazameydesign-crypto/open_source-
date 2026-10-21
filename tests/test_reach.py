"""Reach layer (ADR-0013): contract, routing, policy enforcement, native channels.

Everything here is offline. The channels accept an injectable ``httpx``
transport exactly like :class:`~os_core.models.OpenAICompatModel` does, so the
*policy* is exercised for real — a blocked destination is asserted to never
reach the transport at all — while no socket is ever opened.
"""

from __future__ import annotations

import json
from typing import ClassVar

import httpx
import pytest
from os_core.types import ToolCall, ToolStatus
from os_tools.egress import EgressPolicy, EgressPolicyError
from os_tools.registry import ToolRegistry

from os_reach import (
    ChannelHealth,
    ReachFetcher,
    ReachState,
    ReachTier,
    build_native_registry,
    default_reach_policy,
    html_to_text,
    native_channels,
    parse_doctor_json,
    parse_feed,
    reach_tools,
    register_reach_tools,
    scrub_credentials,
)

REPO_PAYLOAD = {
    "full_name": "Panniantong/Agent-Reach",
    "description": "Give your AI Agent eyes to see the entire internet.",
    "stargazers_count": 4321,
    "forks_count": 210,
    "open_issues_count": 12,
    "language": "Python",
    "license": {"spdx_id": "MIT"},
    "topics": ["ai-agent", "mcp"],
    "archived": False,
    "updated_at": "2026-10-14T00:00:00Z",
    "html_url": "https://github.com/Panniantong/Agent-Reach",
}


class CountingTransport(httpx.MockTransport):
    """MockTransport that records requests — lets a test prove *no* call happened.

    Records in ``handle_async_request``: an ``httpx.AsyncClient`` never calls the
    sync hook, and a counter that silently stays empty would make every
    "nothing was fetched" assertion pass for the wrong reason.
    """

    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await super().handle_async_request(request)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return super().handle_request(request)


def html_transport(body: str = "<html><body><h1>Hello</h1><p>World</p></body></html>", **kw):
    return CountingTransport(lambda request: httpx.Response(200, text=body, **kw))


def json_transport(payload: dict) -> CountingTransport:
    return CountingTransport(lambda request: httpx.Response(200, json=payload))


def policy_for(*hosts: str) -> EgressPolicy:
    return EgressPolicy.allow_hosts(*hosts, resolve_dns=False)


# --------------------------------------------------------------------------- scrubbing


def test_scrub_credentials_removes_userinfo_and_token_values() -> None:
    text = "GET https://user:secret@proxy.example/path?token=abc123&x=1 failed"
    cleaned = scrub_credentials(text)
    assert "secret" not in cleaned
    assert "abc123" not in cleaned
    assert "REDACTED" in cleaned
    assert "x=1" in cleaned  # innocent parameters survive


def test_scrub_credentials_leaves_plain_urls_alone() -> None:
    url = "https://example.org/rss/feed.xml"
    assert scrub_credentials(url) == url


# --------------------------------------------------------------------------- contract


def test_parse_doctor_json_reads_the_upstream_shape() -> None:
    payload = {
        "youtube": {
            "status": "ok",
            "name": "YouTube video and subtitles",
            "message": "can extract info and subtitles",
            "tier": 0,
            "backends": ["yt-dlp"],
            "active_backend": "yt-dlp",
        },
        "reddit": {
            "status": "off",
            "name": "Reddit",
            "message": "no backend; proxy was https://user:pw@proxy.example",
            "tier": 2,
            "backends": ["opencli", "rdt"],
            "active_backend": None,
        },
    }
    report = parse_doctor_json(payload, source="agent-reach:1.5.0")
    assert report.source == "agent-reach:1.5.0"
    assert report.usable_channels() == ["youtube"]
    assert report.summary()["ok"] == 1 and report.summary()["off"] == 1

    youtube = report.channels["youtube"]
    assert youtube.tier is ReachTier.ZERO_CONFIG
    assert youtube.active_backend == "yt-dlp"
    assert youtube.usable

    reddit = report.channels["reddit"]
    assert reddit.status is ReachState.OFF
    assert reddit.tier is ReachTier.SETUP
    assert reddit.active_backend is None
    # A channel message can echo a configured URL; credentials must not survive.
    assert "pw" not in reddit.message
    assert "REDACTED" in reddit.message
    assert "agent-reach" in report.render()


def test_parse_doctor_json_fails_closed_on_unknown_status_or_shape() -> None:
    report = parse_doctor_json(
        {
            "future_channel": {"status": "degraded", "name": "x", "tier": "not-a-tier"},
            "malformed": "not-an-object",
            "partial": {"status": "warn"},
        }
    )
    assert report.channels["future_channel"].status is ReachState.ERROR
    assert report.channels["future_channel"].tier is ReachTier.SETUP
    assert report.channels["malformed"].status is ReachState.ERROR
    assert "malformed" in report.channels["malformed"].message
    partial = report.channels["partial"]
    assert partial.status is ReachState.WARN and partial.usable


def test_report_render_lists_every_channel_with_its_backend() -> None:
    report = parse_doctor_json(
        {"web": {"status": "ok", "backends": ["Jina Reader"], "active_backend": "Jina Reader"}}
    )
    rendered = report.render()
    assert "web: ok [Jina Reader]" in rendered


# --------------------------------------------------------------------------- registry


class _BrokenChannel:
    name: ClassVar[str] = "broken"
    description: ClassVar[str] = "raises on probe"
    tier: ClassVar[ReachTier] = ReachTier.SETUP
    backends: ClassVar[list[str]] = ["nope"]

    def can_handle(self, url: str) -> bool:
        raise RuntimeError("matcher exploded")

    def probe(self) -> ChannelHealth:  # pragma: no cover - never reached
        raise RuntimeError("probe exploded")


class _ExplodingProbeChannel:
    name: ClassVar[str] = "exploding"
    description: ClassVar[str] = "raises on probe"
    tier: ClassVar[ReachTier] = ReachTier.SETUP
    backends: ClassVar[list[str]] = ["nope"]

    def can_handle(self, url: str) -> bool:
        return url.endswith("exploding")

    def probe(self) -> ChannelHealth:
        raise RuntimeError("probe exploded")


def test_probe_survives_one_channel_that_raises() -> None:
    registry = build_native_registry(transport=html_transport())
    registry.register(_ExplodingProbeChannel())
    report = registry.report()
    assert report.channels["exploding"].status is ReachState.ERROR
    assert "probe exploded" in report.channels["exploding"].message
    # The other channels still reported health: one bad channel, not a bad report.
    assert report.channels["github"].status is ReachState.OK
    assert report.channels["web"].status is ReachState.OK


def test_routing_prefers_the_specific_channel_and_keeps_web_as_catch_all() -> None:
    registry = build_native_registry(transport=html_transport())
    assert registry.names() == ["github", "rss", "web"]
    assert registry.channel_for("https://github.com/psf/requests").name == "github"
    assert registry.channel_for("https://example.org/rss/feed.xml").name == "rss"
    assert registry.channel_for("https://example.org/about").name == "web"


def test_routing_skips_a_matcher_that_raises() -> None:
    registry = build_native_registry(transport=html_transport())
    registry.register(_BrokenChannel())  # registered after, so web still answers
    assert registry.channel_for("https://example.org/about").name == "web"


def test_native_channels_are_the_zero_config_tier() -> None:
    channels = native_channels(transport=html_transport())
    assert [c.name for c in channels] == ["github", "rss", "web"]
    assert all(c.tier is ReachTier.ZERO_CONFIG for c in channels)
    assert all(c.probe().usable for c in channels)


# --------------------------------------------------------------------------- policy


async def test_fetch_refuses_a_destination_outside_the_policy_before_contacting_it() -> None:
    transport = html_transport()
    fetcher = ReachFetcher(policy=policy_for("example.org"), transport=transport)
    with pytest.raises(EgressPolicyError):
        await fetcher.fetch("https://elsewhere.test/page")
    assert transport.requests == []  # the refusal happened *before* any socket work


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",  # loopback
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.1.2.3/internal",  # RFC1918
        "http://intranet/",  # single label
        "http://2130706433/",  # ambiguous numeric spelling of loopback
    ],
)
async def test_star_policy_still_cannot_reach_internal_space(url: str) -> None:
    transport = html_transport()
    fetcher = ReachFetcher(policy=default_reach_policy(), transport=transport)
    with pytest.raises(EgressPolicyError):
        await fetcher.fetch(url)
    assert transport.requests == []


def test_default_policy_narrows_and_denies_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("CELA_REACH_ALLOW_HOSTS", "example.org,*.example.org")
    monkeypatch.setenv("CELA_REACH_DENY_HOSTS", "admin.example.org")
    policy = default_reach_policy()
    assert policy.check("https://example.org/ok", tool="web_read").allowed
    assert policy.check("https://docs.example.org/ok", tool="web_read").allowed
    assert not policy.check("https://admin.example.org/no", tool="web_read").allowed
    assert not policy.check("https://elsewhere.test/no", tool="web_read").allowed


def test_enforce_records_the_tool_that_asked() -> None:
    policy = policy_for("example.org")
    fetcher = ReachFetcher(policy=policy, tool="web_read", transport=html_transport())
    fetcher.authorize("https://example.org/page")
    assert policy.audit_log[-1].tool == "web_read"
    assert policy.audit_log[-1].matched_rule == "allow:example.org"


async def test_the_fetcher_reauthorises_every_redirect_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://example.org/"):
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
        return httpx.Response(200, text="should never be fetched")

    transport = CountingTransport(handler)
    fetcher = ReachFetcher(policy=policy_for("example.org"), transport=transport)
    with pytest.raises(EgressPolicyError):
        await fetcher.fetch("https://example.org/redirect-me")
    assert len(transport.requests) == 1  # the redirect target was refused, not fetched


async def test_an_allowed_redirect_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, text="<p>arrived</p>")

    transport = CountingTransport(handler)
    fetcher = ReachFetcher(policy=policy_for("example.org"), transport=transport)
    result = await fetcher.fetch("https://example.org/start")
    assert result.text == "<p>arrived</p>"
    assert str(result.url) == "https://example.org/final"
    assert len(transport.requests) == 2


async def test_cross_origin_redirect_refuses_to_forward_caller_headers() -> None:
    transport = CountingTransport(
        lambda request: httpx.Response(
            302,
            headers={"location": "https://other.example/final"},
            request=request,
        )
    )
    fetcher = ReachFetcher(
        policy=policy_for("example.org", "other.example"),
        transport=transport,
    )

    with pytest.raises(RuntimeError, match="cross-origin redirect"):
        await fetcher.fetch("https://example.org/start", headers={"Authorization": "Bearer secret"})

    assert len(transport.requests) == 1
    assert transport.requests[0].headers["Authorization"] == "Bearer secret"


async def test_a_redirect_loop_is_refused_rather_than_followed_forever() -> None:
    transport = CountingTransport(
        lambda request: httpx.Response(302, headers={"location": "https://example.org/loop"})
    )
    fetcher = ReachFetcher(policy=policy_for("example.org"), transport=transport, max_redirects=2)
    with pytest.raises(RuntimeError, match="too many redirects"):
        await fetcher.fetch("https://example.org/loop")
    assert len(transport.requests) == 3  # initial + 2 allowed hops, then stop


async def test_the_body_is_capped_and_truncation_is_reported() -> None:
    transport = html_transport(body="x" * 5_000)
    fetcher = ReachFetcher(policy=policy_for("example.org"), transport=transport, max_bytes=100)
    result = await fetcher.fetch("https://example.org/big")
    assert result.truncated is True
    assert result.bytes_read == 100
    assert len(result.text) == 100


async def test_fetch_json_reports_a_non_json_body() -> None:
    transport = html_transport(body="<html>not json</html>")
    fetcher = ReachFetcher(policy=policy_for("example.org"), transport=transport)
    with pytest.raises(RuntimeError, match="expected JSON"):
        await fetcher.fetch_json("https://example.org/api")


# --------------------------------------------------------------------------- parsing


def test_html_to_text_drops_scripts_styles_and_markup() -> None:
    html = """
    <html><head><style>body { color: red }</style>
    <script>alert("nope")</script></head>
    <body><h1>Title</h1><p>First&nbsp;paragraph &amp; more</p>
    <!-- a comment -->
    <div>Second line</div></body></html>
    """
    text = html_to_text(html)
    assert "alert" not in text and "color: red" not in text and "comment" not in text
    assert "Title" in text
    assert "First paragraph & more" in text
    assert "Second line" in text


RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
  <item><title>First</title><link>https://example.org/1</link>
    <pubDate>Wed, 14 Oct 2026 00:00:00 GMT</pubDate>
    <description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description></item>
  <item><title>Second</title><link>https://example.org/2</link></item>
  <item><title>Third</title><link>https://example.org/3</link></item>
</channel></rss>
"""

ATOM_SAMPLE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom feed</title>
  <entry><title>Entry</title><link href="https://example.org/a"/>
    <updated>2026-10-14T00:00:00Z</updated><summary>Plain summary</summary></entry>
</feed>
"""


def test_parse_feed_reads_rss_and_atom_and_honours_the_limit() -> None:
    entries = parse_feed(RSS_SAMPLE)
    assert [e.title for e in entries] == ["First", "Second", "Third"]
    assert entries[0].link == "https://example.org/1"
    assert "Hello world" in entries[0].summary  # description markup reduced to text
    assert [e.title for e in parse_feed(RSS_SAMPLE, limit=2)] == ["First", "Second"]

    atom = parse_feed(ATOM_SAMPLE)
    assert [e.title for e in atom] == ["Entry"]
    assert atom[0].link == "https://example.org/a"
    assert atom[0].published.startswith("2026-10-14")


def test_parse_feed_refuses_entity_declarations() -> None:
    billion_laughs = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;">]><rss><channel><item><title>&lol2;</title>'
        "</item></channel></rss>"
    )
    with pytest.raises(RuntimeError, match="ENTITY"):
        parse_feed(billion_laughs)
    with pytest.raises(RuntimeError, match="not valid XML"):
        parse_feed("not xml at all")


# --------------------------------------------------------------------------- github


async def test_github_repo_summarises_the_payload() -> None:
    registry = build_native_registry(transport=json_transport(REPO_PAYLOAD))
    channel = registry.get("github")
    summary = await channel.repo("Panniantong/Agent-Reach")
    assert summary["full_name"] == "Panniantong/Agent-Reach"
    assert summary["stars"] == 4321
    assert summary["license"] == "MIT"
    assert summary["topics"] == ["ai-agent", "mcp"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("psf/requests", "psf/requests"),
        ("https://github.com/psf/requests", "psf/requests"),
        ("https://github.com/psf/requests.git", "psf/requests"),
        ("git@github.com:psf/requests.git", "psf/requests"),
        ("https://api.github.com/repos/psf/requests", "psf/requests"),
    ],
)
def test_github_repo_normalisation(raw: str, expected: str) -> None:
    channels = {c.name: c for c in native_channels(transport=html_transport())}
    assert channels["github"].normalise_repo(raw) == expected


@pytest.mark.parametrize("raw", ["just-a-name", "a/b/c", "https://github.com/only-one", ""])
def test_github_repo_normalisation_rejects_junk(raw: str) -> None:
    channels = {c.name: c for c in native_channels(transport=html_transport())}
    with pytest.raises(ValueError, match="owner/name"):
        channels["github"].normalise_repo(raw)


async def test_github_reads_the_token_from_the_env_and_never_returns_it(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test-token-value")
    transport = json_transport(REPO_PAYLOAD)
    registry = build_native_registry(transport=transport)
    channel = registry.get("github")
    summary = await channel.repo("Panniantong/Agent-Reach")

    assert transport.requests[0].headers["authorization"] == "Bearer ghp_test-token-value"
    assert "ghp_test-token-value" not in json.dumps(summary)
    assert "ghp_test-token-value" not in channel.model_dump_json()  # never stored
    assert channel.probe().message == "token present in the environment"


def test_github_without_a_token_says_so_instead_of_failing(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    channels = {c.name: c for c in native_channels(transport=html_transport())}
    health = channels["github"].probe()
    assert health.status is ReachState.OK
    assert "60 requests/hour" in health.message


async def test_github_search_bounds_the_results() -> None:
    payload = {"items": [dict(REPO_PAYLOAD, full_name=f"o/r{i}") for i in range(5)]}
    registry = build_native_registry(transport=json_transport(payload))
    found = await registry.get("github").search("agent", limit=2)
    assert [item["full_name"] for item in found] == ["o/r0", "o/r1"]


# --------------------------------------------------------------------------- tools


def test_reach_tools_carry_network_risk_and_valid_specs() -> None:
    registry = build_native_registry(transport=html_transport())
    tools = {tool.name: tool for tool in reach_tools(registry)}
    assert set(tools) == {"web_read", "rss_read", "github_repo", "github_search", "reach_doctor"}

    platform = ToolRegistry(default_timeout=30.0)
    platform.extend(list(tools.values()))
    for spec in platform.specs():
        assert spec["type"] == "function"
        assert spec["function"]["parameters"]["type"] == "object"
        assert spec["function"]["description"]

    from os_core.types import RiskLevel

    assert tools["web_read"].risk is RiskLevel.NETWORK
    assert tools["rss_read"].risk is RiskLevel.NETWORK
    assert tools["github_repo"].risk is RiskLevel.NETWORK
    assert tools["reach_doctor"].risk is RiskLevel.READ_ONLY


async def test_web_read_tool_runs_end_to_end_through_the_platform_registry() -> None:
    transport = html_transport("<html><body><p>Reach works</p></body></html>")
    platform = ToolRegistry(default_timeout=30.0)
    register_reach_tools(platform, reach=build_native_registry(transport=transport))

    result = await platform.execute(
        ToolCall(name="web_read", args={"url": "https://example.org/page"})
    )
    assert result.status is ToolStatus.SUCCESS
    assert "Reach works" in result.output
    assert len(transport.requests) == 1


async def test_web_read_tool_reports_a_blocked_destination_without_fetching() -> None:
    transport = html_transport()
    platform = ToolRegistry(default_timeout=30.0)
    register_reach_tools(platform, reach=build_native_registry(transport=transport))

    result = await platform.execute(
        ToolCall(name="web_read", args={"url": "http://169.254.169.254/latest/meta-data/"})
    )
    assert result.status is ToolStatus.ERROR
    assert "EgressPolicyError" in result.error
    assert transport.requests == []


async def test_tool_errors_never_leak_credentials_from_a_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Upstream errors really do echo the URL they were configured with —
        # that is the whole reason every operator-visible string is scrubbed.
        raise httpx.ConnectError(f"connection refused for {request.url}", request=request)

    platform = ToolRegistry(default_timeout=30.0)
    register_reach_tools(
        platform, reach=build_native_registry(transport=CountingTransport(handler))
    )

    result = await platform.execute(
        ToolCall(name="web_read", args={"url": "https://example.org/x?token=SECRET-VALUE"})
    )
    assert result.status is ToolStatus.ERROR
    assert "SECRET-VALUE" not in result.error
    assert "REDACTED" in result.error


async def test_reach_doctor_tool_reports_channels_without_fetching() -> None:
    transport = html_transport()
    platform = ToolRegistry(default_timeout=30.0)
    register_reach_tools(platform, reach=build_native_registry(transport=transport))

    result = await platform.execute(ToolCall(name="reach_doctor", args={}))
    assert result.status is ToolStatus.SUCCESS
    report = json.loads(result.output)
    assert report["source"] == "local"
    assert set(report["channels"]) == {"github", "rss", "web"}
    assert transport.requests == []


def test_reach_tools_are_opt_in_for_the_demo_registry(monkeypatch) -> None:
    from os_server.demo import build_registry

    monkeypatch.delenv("CELA_REACH_ENABLE", raising=False)
    registry, _guard, _browser = build_registry()
    assert "web_read" not in registry.names()

    monkeypatch.setenv("CELA_REACH_ENABLE", "1")
    registry, _guard, _browser = build_registry()
    assert {"web_read", "rss_read", "github_repo", "github_search", "reach_doctor"} <= set(
        registry.names()
    )
