"""Launcher contract — releases, checksums and the release feed, read leniently.

Mirrors the pure parsing half of the launcher's ``crates/core``:
``release.rs`` (GitHub releases + ``SHA256SUMS.txt``) and ``feed.rs`` (the
storyteller release feed). Adopted exactly where the security rules live:

* every download URL must sit under the app's own GitHub releases
  (``https://github.com/storytold/<app>/releases/download/…``) — a feed entry
  pointing anywhere else is dropped, never trusted;
* a checksum must be 64 hex characters and a file name must be a bare name
  (no paths, no control characters, no ``..``);
* feed input is hostile: every field is optional, unknown fields are ignored,
  a wrong-typed field reads as missing, and one bad entry never spoils the
  others.

Nothing in this module touches the network: parsers turn data into models,
and the client (:mod:`os_launcher.client`) is the only thing that fetches.
"""

from __future__ import annotations

import re
import time
from typing import Any

from pydantic import BaseModel, Field

from os_launcher.catalog import GITHUB_OWNER

#: Longest release-notes body we will keep (matches core::release's MAX_NOTES).
MAX_NOTES = 64 * 1024
#: Longest app id / tag we will read from a feed (matches core::feed's caps).
MAX_FEED_STR = 200

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_TAG_OK_RE = re.compile(r"^[A-Za-z0-9._\-+]{1,64}$")
#: ``v0.5.0`` or ``0.5.0`` — the version part of a release tag.
_VERSION_IN_TAG_RE = re.compile(r"^[vV]?(\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.\-+]+)?)$")


def is_hex64(value: str) -> bool:
    """True for exactly 64 hex characters (a SHA-256 in lowercase or mixed case)."""
    return bool(_HEX64_RE.fullmatch(value))


def valid_file_name(name: str) -> bool:
    """A bare file name we would be willing to save: no paths, no control characters."""
    return (
        bool(name)
        and len(name) <= 200
        and name not in {".", ".."}
        and not name.startswith(".")
        and not any(c in '/\\:<>"|?*' or ord(c) < 0x20 or ord(c) == 0x7F for c in name)
    )


def download_prefix(repo: str) -> str:
    """The prefix every download of *repo* must start with."""
    return f"https://github.com/{repo}/releases/download/"


def parse_sha256sums(text: str) -> list[tuple[str, str]]:
    """Read ``sha256sum`` output: ``<hex>  <name>`` (or ``<hex> *<name>``) per line.

    Junk lines are skipped, names are validated with :func:`valid_file_name`
    (so ``../escape`` is refused), and hashes come back lowercased.
    """
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        hash_hex, rest = parts
        name = rest.lstrip().lstrip("*")
        if is_hex64(hash_hex) and valid_file_name(name):
            pairs.append((hash_hex.lower(), name))
    return pairs


def parse_tag_version(tag: str) -> str | None:
    """The version part of a release tag (``v0.5.0`` -> ``0.5.0``), or ``None``."""
    match = _VERSION_IN_TAG_RE.fullmatch(tag.strip())
    return match.group(1) if match else None


class ReleaseAsset(BaseModel):
    """One downloadable file on a release (mirrors ``release.rs``'s ``Asset``)."""

    name: str
    size: int = 0
    url: str = ""
    #: Lowercase hex SHA-256, when GitHub's ``digest`` or ``SHA256SUMS.txt`` told us.
    sha256: str | None = None


class AppRelease(BaseModel):
    """One published release of one Craft app (mirrors ``release.rs``'s ``Release``)."""

    repo: str
    tag: str
    #: ``PhotoCraft v0.5.0`` — display name, may be empty.
    name: str = ""
    #: Unix seconds; ``None`` when only the tag is known.
    published_at: int | None = None
    html_url: str = ""
    #: Release notes (Markdown), capped at :data:`MAX_NOTES`.
    notes: str = ""
    prerelease: bool = False
    assets: list[ReleaseAsset] = Field(default_factory=list)
    #: Where this answer came from: ``"github"`` or ``"feed"``.
    source: str = "github"

    def version_label(self) -> str:
        """The parsed version (``0.5.0``), falling back to the raw tag."""
        return parse_tag_version(self.tag) or self.tag

    def sums_asset(self) -> ReleaseAsset | None:
        for asset in self.assets:
            if asset.name == "SHA256SUMS.txt":
                return asset
        return None

    def apply_sums(self, sums: list[tuple[str, str]]) -> None:
        """Fill in checksums from a ``SHA256SUMS.txt`` for assets that lack one."""
        for asset in self.assets:
            if asset.sha256 is None:
                asset.sha256 = next((h for h, n in sums if n == asset.name), None)

    def checked_assets(self) -> list[ReleaseAsset]:
        """Assets that carry a SHA-256 — the only ones safe to install."""
        return [a for a in self.assets if a.sha256 and valid_file_name(a.name)]


class UpdateInfo(BaseModel):
    """One row of an update check: installed version vs the latest release."""

    app_id: str
    name: str
    installed: str
    latest: str | None = None
    update_available: bool = False
    release: AppRelease | None = None
    detail: str = ""


def _str_or_none(value: Any, *, cap: int = MAX_FEED_STR) -> str | None:
    """A feed string: wrong type or over-long reads as missing (feed.rs leniency)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > cap:
        return None
    return value


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass — never a size or timestamp
        return None
    return value if isinstance(value, int) and value >= 0 else None


def parse_github_release(data: Any, repo: str) -> AppRelease | None:
    """Read one ``GET /repos/<repo>/releases/latest`` answer (or one list element).

    Lenient like the Rust reader: unreadable structure yields ``None``; a
    malformed asset is skipped, never fatal; a ``digest`` only counts when it is
    ``sha256:`` plus 64 hex characters. Download URLs must live under the app's
    own GitHub releases, exactly as ``core::release`` demands.
    """
    if not isinstance(data, dict):
        return None
    tag = _str_or_none(data.get("tag_name"), cap=64)
    if tag is None or not _TAG_OK_RE.fullmatch(tag):
        return None

    assets: list[ReleaseAsset] = []
    raw_assets = data.get("assets")
    if isinstance(raw_assets, list):
        prefix = download_prefix(repo)
        for raw in raw_assets:
            if not isinstance(raw, dict):
                continue
            name = _str_or_none(raw.get("name"), cap=200)
            if name is None or not valid_file_name(name):
                continue
            url = _str_or_none(raw.get("browser_download_url"), cap=2000) or ""
            if url and not url.startswith(prefix):
                continue
            sha256: str | None = None
            digest = raw.get("digest")
            if isinstance(digest, str):
                hash_part = digest.removeprefix("sha256:")
                if is_hex64(hash_part) and digest != hash_part:
                    sha256 = hash_part.lower()
            size = _int_or_none(raw.get("size")) or 0
            assets.append(ReleaseAsset(name=name, size=size, url=url, sha256=sha256))

    notes = data.get("body")
    notes = notes if isinstance(notes, str) else ""
    published_at = _int_or_none(data.get("published_at_unix")) or _published_unix(
        data.get("published_at")
    )
    return AppRelease(
        repo=repo,
        tag=tag,
        name=_str_or_none(data.get("name"), cap=200) or "",
        published_at=published_at,
        html_url=_str_or_none(data.get("html_url"), cap=2000) or "",
        notes=notes[:MAX_NOTES],
        prerelease=bool(data.get("prerelease")),
        assets=assets,
        source="github",
    )


def _published_unix(value: Any) -> int | None:
    """GitHub's RFC-3339 ``published_at`` -> Unix seconds; junk reads as missing."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp())


class FeedApp(BaseModel):
    """One app's entry in the release feed — every field optional (feed.rs)."""

    app_id: str
    repo: str | None = None
    tag: str | None = None
    name: str = ""
    published_at: int | None = None
    html_url: str = ""
    notes: str = ""
    assets: list[ReleaseAsset] = Field(default_factory=list)


class Notice(BaseModel):
    kind: str
    message: str


class FeedSnapshot(BaseModel):
    """The release feed, read leniently (mirrors ``feed.rs``'s ``Feed``)."""

    apps: dict[str, FeedApp] = Field(default_factory=dict)
    last_fetched: int | None = None
    app_renames: dict[str, str] = Field(default_factory=dict)
    #: Old repo -> new repo, only ever under :data:`GITHUB_OWNER`.
    repo_renames: dict[str, str] = Field(default_factory=dict)
    announcements: list[Notice] = Field(default_factory=list)
    alerts: list[Notice] = Field(default_factory=list)
    generated_at: float = Field(default_factory=time.time)

    def stale(self, *, max_age: int = 24 * 3600, now: float | None = None) -> bool:
        """True when the feed is too old to trust (``MAX_AGE`` in feed.rs)."""
        if self.last_fetched is None:
            return True
        moment = time.time() if now is None else now
        return moment - self.last_fetched > max_age

    def release_for(self, app_id: str) -> AppRelease | None:
        """Turn one feed entry into an :class:`AppRelease`, or ``None``."""
        entry = self.apps.get(app_id.lower())
        if entry is None:
            return None
        return AppRelease(
            repo=entry.repo or f"{GITHUB_OWNER}/{app_id.lower()}",
            tag=entry.tag or "",
            name=entry.name,
            published_at=entry.published_at,
            html_url=entry.html_url,
            notes=entry.notes[:MAX_NOTES],
            prerelease=False,
            assets=list(entry.assets),
            source="feed",
        )


def _feed_assets(raw: Any, repo: str) -> list[ReleaseAsset]:
    """Feed assets: must point at the app's own GitHub releases, or they are dropped."""
    assets: list[ReleaseAsset] = []
    if not isinstance(raw, list):
        return assets
    prefix = download_prefix(repo)
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _str_or_none(item.get("name"), cap=200)
        url = _str_or_none(item.get("url"), cap=2000) or ""
        if name is None or not valid_file_name(name) or not url.startswith(prefix):
            continue
        sha256: str | None = None
        digest = item.get("sha256") or item.get("digest")
        if isinstance(digest, str):
            hash_part = digest.removeprefix("sha256:")
            if is_hex64(hash_part):
                sha256 = hash_part.lower()
        assets.append(
            ReleaseAsset(
                name=name, size=_int_or_none(item.get("size")) or 0, url=url, sha256=sha256
            )
        )
    return assets


def parse_feed(data: Any) -> FeedSnapshot:
    """Read the release feed: hostile input is the norm, so nothing here raises.

    * every field is optional and unknown fields are ignored;
    * a field of the wrong type reads as missing;
    * one bad entry never spoils the others;
    * download links must still point at the app's own GitHub releases;
    * repo renames are honoured only under :data:`GITHUB_OWNER` — a rename
      anywhere else is dropped (feed.rs's rule).
    """
    snapshot = FeedSnapshot()
    if not isinstance(data, dict):
        return snapshot

    raw_apps = data.get("apps")
    if isinstance(raw_apps, dict):
        for raw_id, raw_entry in raw_apps.items():
            app_id = _str_or_none(raw_id, cap=MAX_FEED_STR)
            if app_id is None or not isinstance(raw_entry, dict):
                continue
            app_id = app_id.lower()
            repo = _str_or_none(raw_entry.get("repo"), cap=MAX_FEED_STR)
            if repo is not None and not repo.startswith(f"{GITHUB_OWNER}/"):
                repo = None  # a catalog row outside storytold/ is never honoured
            effective_repo = repo or f"{GITHUB_OWNER}/{app_id}"
            tag = _str_or_none(raw_entry.get("tag"), cap=64)
            if tag is not None and not _TAG_OK_RE.fullmatch(tag):
                tag = None
            entry = FeedApp(
                app_id=app_id,
                repo=repo,
                tag=tag,
                name=_str_or_none(raw_entry.get("name"), cap=200) or "",
                published_at=_int_or_none(raw_entry.get("published_at")),
                html_url=_str_or_none(raw_entry.get("html_url"), cap=2000) or "",
                notes=(_str_or_none(raw_entry.get("notes"), cap=MAX_NOTES) or ""),
                assets=_feed_assets(raw_entry.get("assets"), effective_repo),
            )
            snapshot.apps[app_id] = entry

    snapshot.last_fetched = _int_or_none(data.get("last_fetched"))

    raw_renames = data.get("app_renames")
    if isinstance(raw_renames, list):
        for item in raw_renames:
            if isinstance(item, dict):
                old = _str_or_none(item.get("from"), cap=MAX_FEED_STR)
                new = _str_or_none(item.get("to"), cap=MAX_FEED_STR)
                if old and new:
                    snapshot.app_renames[old.lower()] = new.lower()

    raw_repo_renames = data.get("repo_renames")
    if isinstance(raw_repo_renames, list):
        for item in raw_repo_renames:
            if isinstance(item, dict):
                old = _str_or_none(item.get("from"), cap=MAX_FEED_STR)
                new = _str_or_none(item.get("to"), cap=MAX_FEED_STR)
                if (
                    old
                    and new
                    and old.startswith(f"{GITHUB_OWNER}/")
                    and new.startswith(f"{GITHUB_OWNER}/")
                ):
                    snapshot.repo_renames[old] = new

    for key, kind in (("announcements", "announcement"), ("alerts", "alert")):
        raw_notices = data.get(key)
        if isinstance(raw_notices, list):
            collected: list[Notice] = []
            for item in raw_notices:
                if isinstance(item, dict):
                    message = _str_or_none(item.get("message"), cap=2000)
                    if message:
                        collected.append(Notice(kind=kind, message=message))
            setattr(snapshot, key, collected)
    return snapshot
