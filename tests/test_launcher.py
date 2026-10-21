"""The os_launcher adapter: catalog mirror, hostile-input parsing, policy, client, tools.

The outside world is replaced, never touched: an ``httpx.MockTransport`` that
also *counts* requests (so "the feed answered, GitHub was never asked" is
provable), and every policy rule is asserted against real
:class:`~os_tools.egress.EgressPolicy` verdicts.
"""

from __future__ import annotations

import json
import socket
import time

import httpx
import pytest
from os_launcher.catalog import GITHUB_OWNER, AppGroup
from os_launcher.client import version_key
from os_launcher.contract import valid_file_name
from os_launcher.tools import _catalog_json
from os_tools.egress import EgressPolicyError
from os_tools.registry import ToolRegistry

from os_launcher import (
    DEFAULT_ALLOW_HOSTS,
    CraftReleaseClient,
    LauncherError,
    by_id,
    default_launcher_policy,
    is_own_release_download,
    launcher_tools,
    parse_feed,
    parse_github_release,
    parse_sha256sums,
    register_launcher_tools,
    scrub_credentials,
    suite,
)

HASH = "a" * 64
HASH2 = "b" * 64


class CountingTransport(httpx.MockTransport):
    """MockTransport that records every request it answered."""

    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await super().handle_async_request(request)


def github_release_json(repo: str, tag: str = "v0.5.0", *, digest: str | None = None) -> dict:
    asset_digest = digest if digest is not None else f"sha256:{HASH}"
    return {
        "tag_name": tag,
        "name": f"{repo.split('/')[1]} {tag}",
        "published_at": "2026-10-01T12:00:00Z",
        "html_url": f"https://github.com/{repo}/releases/tag/{tag}",
        "body": "notes",
        "prerelease": False,
        "assets": [
            {
                "name": "photocraft-0.5.0-macos-universal.dmg",
                "size": 123,
                "browser_download_url": f"https://github.com/{repo}/releases/download/{tag}/photocraft-0.5.0-macos-universal.dmg",
                "digest": asset_digest,
            },
            {
                "name": "SHA256SUMS.txt",
                "size": 45,
                "browser_download_url": f"https://github.com/{repo}/releases/download/{tag}/SHA256SUMS.txt",
            },
        ],
    }


# ------------------------------------------------------------------- catalog


def test_the_suite_is_thirteen_apps_in_display_order() -> None:
    apps = suite()
    assert len(apps) == 13
    assert [a.id for a in apps[:7]] == [
        "photocraft",
        "vectorcraft",
        "filmcraft",
        "lightcraft",
        "pdfcraft",
        "effectcraft",
        "designcraft",
    ]
    assert [a.id for a in apps[7:]] == [
        "wordcraft",
        "deckcraft",
        "gridcraft",
        "cadcraft",
        "soundcraft",
        "figcraft",
    ]


def test_ids_are_unique_and_repos_live_under_storytold() -> None:
    """The Rust SUITE test's invariants, asserted on the Python mirror."""
    ids = [a.id for a in suite()]
    assert len(set(ids)) == len(ids)
    for app in suite():
        assert app.repo == f"{GITHUB_OWNER}/{app.id}"


def test_pdfcraft_still_remembers_being_printcraft() -> None:
    pdf = by_id("pdfcraft")
    assert pdf is not None
    assert pdf.former_ids == ["printcraft"]
    assert pdf.stems() == ["pdfcraft", "printcraft"]
    assert pdf.names() == ["PdfCraft", "PrintCraft"]
    assert by_id("printcraft") is pdf


def test_a_former_id_resolves_and_an_unknown_id_is_none() -> None:
    assert by_id("PhotoCraft") is not None  # lookup is case-insensitive
    assert by_id("not-a-craft-app") is None


def test_bundle_and_web_urls_match_the_rust_helpers() -> None:
    word = by_id("wordcraft")
    assert word is not None
    assert word.bundle_id() == "ai.storyteller.wordcraft"
    assert word.website() == "https://getartcraft.com/apps/wordcraft"
    assert word.releases_url() == "https://github.com/storytold/wordcraft/releases"
    assert word.group == AppGroup.NEW


# ------------------------------------------------------- checksums and names


def test_valid_file_name_refuses_paths_and_control_characters() -> None:
    assert valid_file_name("photocraft-0.5.0.dmg")
    assert not valid_file_name("..")
    assert not valid_file_name("../escape")
    assert not valid_file_name("/etc/passwd")
    assert not valid_file_name(".hidden")
    assert not valid_file_name("a/b")
    assert not valid_file_name("bad\nname")
    assert not valid_file_name("")


def test_parse_sha256sums_reads_both_separator_styles_and_skips_junk() -> None:
    text = f"{HASH}  a.dmg\n{HASH2.upper()} *b.zip\nnot a line\n{HASH}  ../escape\n{HASH}\n"
    assert parse_sha256sums(text) == [(HASH, "a.dmg"), (HASH2, "b.zip")]


# ------------------------------------------------------------ GitHub parsing


def test_parse_github_release_reads_a_whole_release_with_digest() -> None:
    release = parse_github_release(
        github_release_json("storytold/photocraft"), "storytold/photocraft"
    )
    assert release is not None
    assert release.tag == "v0.5.0"
    assert release.version_label() == "0.5.0"
    assert release.published_at == 1790856000
    assert release.assets[0].sha256 == HASH
    assert release.sums_asset() is not None
    assert [a.name for a in release.checked_assets()] == ["photocraft-0.5.0-macos-universal.dmg"]


def test_a_bad_digest_is_ignored_not_trusted() -> None:
    release = parse_github_release(
        github_release_json("storytold/photocraft", digest="sha256:zz"), "storytold/photocraft"
    )
    assert release is not None
    assert release.assets[0].sha256 is None
    assert release.checked_assets() == []


def test_an_asset_outside_the_own_release_downloads_is_dropped() -> None:
    data = github_release_json("storytold/photocraft")
    data["assets"][0]["browser_download_url"] = "https://evil.example/photocraft.dmg"
    release = parse_github_release(data, "storytold/photocraft")
    assert release is not None
    assert [a.name for a in release.assets] == ["SHA256SUMS.txt"]


def test_an_unreadable_release_is_none_and_a_malformed_asset_is_skipped() -> None:
    assert parse_github_release("not a dict", "storytold/photocraft") is None
    assert parse_github_release({"tag_name": "v1"}, "storytold/photocraft") is not None
    data = github_release_json("storytold/photocraft")
    data["assets"] = ["junk", {"name": "../evil"}, data["assets"][0]]
    release = parse_github_release(data, "storytold/photocraft")
    assert release is not None
    assert [a.name for a in release.assets] == ["photocraft-0.5.0-macos-universal.dmg"]


def test_apply_sums_fills_in_only_missing_checksums() -> None:
    release = parse_github_release(
        github_release_json("storytold/photocraft", digest=f"sha256:{HASH2}"),
        "storytold/photocraft",
    )
    assert release is not None
    release.apply_sums([(HASH, "photocraft-0.5.0-macos-universal.dmg"), (HASH2, "SHA256SUMS.txt")])
    assert release.assets[0].sha256 == HASH2  # digest wins over sums
    assert release.assets[1].sha256 == HASH2  # sums fill in the missing one


# ---------------------------------------------------------------- feed input


def test_the_feed_is_read_leniently_and_one_bad_entry_never_spoils_the_others() -> None:
    snapshot = parse_feed(
        {
            "apps": {
                "photocraft": {
                    "tag": "v0.5.0",
                    "published_at": 1_790_875_200,
                    "assets": [
                        {
                            "name": "photocraft-0.5.0.dmg",
                            "url": "https://github.com/storytold/photocraft/releases/download/v0.5.0/photocraft-0.5.0.dmg",
                            "sha256": HASH,
                        }
                    ],
                    "notes": 12,  # wrong type -> ignored
                },
                "gridcraft": "not a dict",  # one bad entry
                "wordcraft": {"tag": "v0.1.0", 42: "unknown key ignored"},
            },
            "last_fetched": 1_790_875_200,
            "unknown_top_level": "ignored",
        }
    )
    assert set(snapshot.apps) == {"photocraft", "wordcraft"}
    photo = snapshot.apps["photocraft"]
    assert photo.tag == "v0.5.0"
    assert photo.notes == ""
    assert photo.assets[0].sha256 == HASH
    assert snapshot.last_fetched == 1_790_875_200
    assert not snapshot.stale(now=1_790_875_200 + 60)


def test_feed_download_links_must_point_at_the_own_github_releases() -> None:
    snapshot = parse_feed(
        {
            "apps": {
                "photocraft": {
                    "tag": "v0.5.0",
                    "assets": [
                        {
                            "name": "photocraft.dmg",
                            "url": "https://evil.example/photocraft.dmg",
                            "sha256": HASH,
                        },
                        {
                            "name": "photocraft-ok.dmg",
                            "url": "https://github.com/storytold/photocraft/releases/download/v0.5.0/photocraft-ok.dmg",
                            "sha256": HASH,
                        },
                    ],
                }
            }
        }
    )
    assert [a.name for a in snapshot.apps["photocraft"].assets] == ["photocraft-ok.dmg"]


def test_repo_renames_are_honoured_only_under_storytold() -> None:
    snapshot = parse_feed(
        {
            "repo_renames": [
                {"from": "storytold/printcraft", "to": "storytold/pdfcraft"},
                {"from": "evil/printcraft", "to": "evil/hijacked"},
            ]
        }
    )
    assert snapshot.repo_renames == {"storytold/printcraft": "storytold/pdfcraft"}


def test_a_stale_feed_reads_as_stale_and_a_missing_timestamp_too() -> None:
    assert parse_feed({"last_fetched": 100}).stale(now=100 + 24 * 3600 + 1)
    assert parse_feed({}).stale()
    assert not parse_feed({"last_fetched": 100}).stale(now=100 + 10)


# ------------------------------------------------------------------ security


def test_the_default_policy_allows_only_the_four_launcher_hosts() -> None:
    policy = default_launcher_policy(env={})
    for host in DEFAULT_ALLOW_HOSTS:
        policy.enforce(f"https://{host}/path", tool="t", resolve_dns=False)
    with pytest.raises(EgressPolicyError):
        policy.enforce("https://example.org/", tool="t", resolve_dns=False)
    with pytest.raises(EgressPolicyError):
        policy.enforce("http://127.0.0.1/", tool="t", resolve_dns=False)


def test_the_ssrf_floor_survives_an_allow_hosts_override() -> None:
    """``CELA_LAUNCHER_ALLOW_HOSTS`` narrows/replaces, but never lifts the floor."""
    policy = default_launcher_policy(
        env={"CELA_LAUNCHER_ALLOW_HOSTS": "api.github.com,169.254.169.254"}
    )
    policy.enforce("https://api.github.com/", tool="t", resolve_dns=False)
    with pytest.raises(EgressPolicyError):
        policy.enforce("https://169.254.169.254/", tool="t", resolve_dns=False)


def test_deny_wins_over_allow() -> None:
    policy = default_launcher_policy(env={"CELA_LAUNCHER_DENY_HOSTS": "api.github.com"})
    with pytest.raises(EgressPolicyError):
        policy.enforce(
            "https://api.github.com/repos/storytold/photocraft", tool="t", resolve_dns=False
        )


def test_own_release_downloads_narrow_to_the_exact_repo() -> None:
    assert is_own_release_download(
        "https://github.com/storytold/photocraft/releases/download/v1/a.dmg",
        "storytold/photocraft",
    )
    assert not is_own_release_download(
        "https://github.com/storytold/photocraft/releases/download/v1/a.dmg",
        "storytold/wordcraft",  # someone else's repo
    )
    assert not is_own_release_download("https://evil.example/a.dmg", "storytold/photocraft")
    assert not is_own_release_download(
        "https://github.com/evil/photocraft/releases/download/v1/a.dmg", "evil/photocraft"
    )


def test_scrub_credentials_removes_the_things_a_url_can_carry() -> None:
    scrubbed = scrub_credentials("failed for https://user:pass@example.org/x?api_key=SECRET&ok=1")
    assert "pass" not in scrubbed and "SECRET" not in scrubbed
    assert "REDACTED" in scrubbed and "ok=1" in scrubbed


# -------------------------------------------------------------------- client


def release_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.storyteller.ai":
        return httpx.Response(200, json={"last_fetched": 1_790_875_200, "apps": {}})
    if request.url.path.endswith("/SHA256SUMS.txt"):
        return httpx.Response(
            200,
            text=f"{HASH}  photocraft-0.5.0-macos-universal.dmg\n",
        )
    if request.url.host == "api.github.com":
        return httpx.Response(200, json=github_release_json("storytold/photocraft"))
    return httpx.Response(404)


@pytest.fixture
def transport() -> CountingTransport:
    return CountingTransport(release_handler)


@pytest.fixture
def client(transport: CountingTransport) -> CraftReleaseClient:
    return CraftReleaseClient(transport=transport, policy=default_launcher_policy(env={}))


async def test_feed_dns_failure_is_treated_as_an_unreachable_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_dns(*_args, **_kwargs):
        raise socket.gaierror("temporary resolver failure")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    client = CraftReleaseClient(policy=default_launcher_policy(env={}))

    snapshot = await client.fetch_feed()

    assert snapshot.apps == {}
    assert snapshot.last_fetched is None


async def test_latest_release_falls_back_to_github_and_fills_checksums(
    client: CraftReleaseClient, transport: CountingTransport
) -> None:
    def no_digest(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.storyteller.ai":
            return httpx.Response(200, json={"last_fetched": 1_790_875_200, "apps": {}})
        if request.url.path.endswith("/SHA256SUMS.txt"):
            return httpx.Response(200, text=f"{HASH}  photocraft-0.5.0-macos-universal.dmg\n")
        if request.url.host == "api.github.com":
            # No usable digest on the asset — SHA256SUMS.txt must fill it in.
            return httpx.Response(
                200, json=github_release_json("storytold/photocraft", digest="sha256:zz")
            )
        return httpx.Response(404)

    sums_client = CraftReleaseClient(
        transport=CountingTransport(no_digest), policy=default_launcher_policy(env={})
    )
    release = await sums_client.latest_release("photocraft")
    assert release is not None
    assert release.tag == "v0.5.0"
    assert release.source == "github"
    assert release.assets[0].sha256 == HASH  # filled from SHA256SUMS.txt
    hosts = {r.url.host for r in transport.requests}
    assert hosts <= {"api.github.com", "github.com", "api.storyteller.ai"}


async def test_a_fresh_feed_entry_wins_and_github_is_never_asked() -> None:
    def feed_first(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.storyteller.ai":
            return httpx.Response(
                200,
                json={
                    "last_fetched": int(time.time()),  # fresh: the feed is trusted as-is
                    "apps": {
                        "photocraft": {
                            "tag": "v0.6.0",
                            "assets": [
                                {
                                    "name": "photocraft-0.6.0.dmg",
                                    "url": "https://github.com/storytold/photocraft/releases/download/v0.6.0/photocraft-0.6.0.dmg",
                                    "sha256": HASH2,
                                }
                            ],
                        }
                    },
                },
            )
        return httpx.Response(404)

    feed_transport = CountingTransport(feed_first)
    feed_client = CraftReleaseClient(
        transport=feed_transport, policy=default_launcher_policy(env={})
    )
    release = await feed_client.latest_release("photocraft")
    assert release is not None
    assert release.tag == "v0.6.0"
    assert release.source == "feed"
    assert [r.url.host for r in feed_transport.requests] == ["api.storyteller.ai"]


async def test_an_unknown_app_is_none_without_any_request(
    client: CraftReleaseClient, transport: CountingTransport
) -> None:
    assert await client.latest_release("not-a-craft-app") is None
    assert transport.requests == []


async def test_a_404_from_github_is_none(client: CraftReleaseClient) -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    gone = CraftReleaseClient(
        transport=CountingTransport(missing), policy=default_launcher_policy(env={})
    )
    assert await gone.latest_release("photocraft") is None


async def test_a_blocked_destination_raises_before_the_request_leaves(
    transport: CountingTransport,
) -> None:
    strict = CraftReleaseClient(transport=transport, policy=default_launcher_policy(env={}))
    with pytest.raises(LauncherError, match="EgressPolicyError"):
        await strict._get("https://example.org/exfil")
    assert transport.requests == []


async def test_check_updates_compares_versions_and_survives_one_failing_app(
    client: CraftReleaseClient,
) -> None:
    rows = await client.check_updates({"photocraft": "0.4.0", "nope": "1.0"})
    by_app = {r.app_id: r for r in rows}
    assert by_app["photocraft"].update_available is True
    assert by_app["photocraft"].latest == "0.5.0"
    assert by_app["nope"].detail == "unknown app id"


def test_version_key_orders_versions_and_junk_sorts_low() -> None:
    assert version_key("v0.5.0") == (0, 5, 0)
    assert version_key("0.10.0") > version_key("0.9.0")
    assert version_key("junk") == ()
    assert version_key("junk") < version_key("0.0.1")


# --------------------------------------------------------------------- tools


def test_launcher_tools_are_read_only_and_named_for_the_model() -> None:
    tools = launcher_tools(CraftReleaseClient(transport=CountingTransport(release_handler)))
    assert [t.name for t in tools] == [
        "craft_list_apps",
        "craft_latest_release",
        "craft_check_updates",
    ]
    assert tools[0].risk.value == "read_only"
    assert tools[1].risk.value == "network"
    specs = {s["function"]["name"] for s in (t.spec() for t in tools)}
    assert specs == {"craft_list_apps", "craft_latest_release", "craft_check_updates"}


def test_registration_is_explicit_and_reports_names() -> None:
    registry = ToolRegistry()
    assert registry.names() == []
    names = register_launcher_tools(
        registry, CraftReleaseClient(transport=CountingTransport(release_handler))
    )
    assert names == ["craft_list_apps", "craft_latest_release", "craft_check_updates"]
    assert registry.names() == sorted(names)


def test_the_catalog_tool_returns_thirteen_rows_as_json() -> None:
    rows = json.loads(_catalog_json())
    assert len(rows) == 13
    assert rows[0]["id"] == "photocraft"


async def test_tool_failures_are_error_results_with_scrubbed_text() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    registry = ToolRegistry()
    register_launcher_tools(
        registry,
        CraftReleaseClient(
            transport=CountingTransport(boom), policy=default_launcher_policy(env={})
        ),
    )
    from os_core.types import ToolCall

    call = ToolCall(id="1", name="craft_latest_release", args={"app_id": "photocraft"})
    outcome = await registry.execute(call)
    assert outcome.status.value == "error"
    assert outcome.error is not None and "ConnectError" in outcome.error
