"""os_launcher — the Craft App Suite (ArtCraft Launcher) adapter for the platform.

Adopted from `storytold/craft-launcher <https://github.com/storytold/craft-launcher>`_
(MIT OR Apache-2.0), vendored at ``packages/craft-launcher`` — see
``docs/VENDORED-craft-launcher.md`` for the hosting terms and the integrity manifest.
Following the platform's adapter precedent (ADR-0013 for Agent-Reach, ADR-0015
for this): the *contract* and the *policy* live here, the Rust workspace stays
upstream-shaped, and nothing is fetched without an
:class:`~os_tools.egress.EgressPolicy` check.

What the adapter exposes:

* :mod:`os_launcher.catalog` — the thirteen-app suite, mirroring ``catalog.rs``;
* :mod:`os_launcher.contract` — releases, ``SHA256SUMS.txt`` and the release
  feed, parsed leniently and never trustingly;
* :mod:`os_launcher.security` — the four-host allow-list policy and the
  "downloads only from the app's own GitHub releases" rule;
* :mod:`os_launcher.client` — feed-first, GitHub-fallback release reader with an
  injectable transport (offline tests replace the network entirely);
* :mod:`os_launcher.tools` — read-only :class:`~os_tools.registry.Tool`\\ s,
  registered only through explicit wiring.

Deliberately absent: anything that downloads or installs software. The launcher
installs desktop apps on a user's machine; the platform never does that through
a model call.
"""

from __future__ import annotations

from os_launcher.catalog import (
    FEED_URL,
    GITHUB_OWNER,
    LAUNCHER_ID,
    LAUNCHER_REPO,
    WEBSITE_URL,
    AppGroup,
    CraftApp,
    by_id,
    suite,
)
from os_launcher.client import CraftReleaseClient, LauncherError, catalog_rows
from os_launcher.contract import (
    AppRelease,
    FeedSnapshot,
    ReleaseAsset,
    UpdateInfo,
    parse_feed,
    parse_github_release,
    parse_sha256sums,
)
from os_launcher.security import (
    DEFAULT_ALLOW_HOSTS,
    default_launcher_policy,
    is_own_release_download,
    scrub_credentials,
)
from os_launcher.tools import launcher_tools, register_launcher_tools

__all__ = [
    "DEFAULT_ALLOW_HOSTS",
    "FEED_URL",
    "GITHUB_OWNER",
    "LAUNCHER_ID",
    "LAUNCHER_REPO",
    "WEBSITE_URL",
    "AppGroup",
    "AppRelease",
    "CraftApp",
    "CraftReleaseClient",
    "LauncherError",
    "ReleaseAsset",
    "UpdateInfo",
    "by_id",
    "catalog_rows",
    "default_launcher_policy",
    "is_own_release_download",
    "launcher_tools",
    "parse_feed",
    "parse_github_release",
    "parse_sha256sums",
    "register_launcher_tools",
    "scrub_credentials",
    "suite",
]
