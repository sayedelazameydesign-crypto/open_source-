"""The release client — the only module here that talks to the network.

It reads two sources, in the launcher's own order (``native/src/backend.rs``):

1. **ArtCraft's release feed** — one request for every app, the preferred path;
2. **GitHub** — fallback for apps the feed does not list, entries older than
   24 hours, or when the feed does not answer.

Every request URL is enforced against an
:class:`~os_tools.egress.EgressPolicy` *before* the request leaves
(:func:`os_launcher.security.enforce_url`), and asset URLs additionally have to
be the app's own GitHub release downloads. The transport is injectable — tests
replace the outside world with an ``httpx.MockTransport`` rather than being
trusted not to touch it.
"""

from __future__ import annotations

from typing import Any

import httpx
from os_tools.egress import EgressPolicy, EgressPolicyError
from os_tools.pinned_http import PinnedAsyncHTTPTransport

from os_launcher.catalog import FEED_URL, CraftApp, by_id
from os_launcher.contract import (
    AppRelease,
    FeedSnapshot,
    UpdateInfo,
    parse_feed,
    parse_github_release,
    parse_sha256sums,
)
from os_launcher.security import default_launcher_policy, scrub_credentials

#: Tool name that travels with every policy check this client makes.
TOOL_NAME = "craft_release_client"

#: Feed entries older than this are stale and fall back to GitHub (feed.rs MAX_AGE).
FEED_MAX_AGE = 24 * 3600


class LauncherError(RuntimeError):
    """A client failure with a message already safe to show an operator."""

    def __init__(self, exc: Exception | str) -> None:
        message = f"{type(exc).__name__}: {exc}" if isinstance(exc, Exception) else str(exc)
        super().__init__(scrub_credentials(message))


def version_key(version: str) -> tuple[int, ...]:
    """Orderable key for a version-ish string (``0.5.0``, ``v0.5.0``, ``0.5``).

    Junk sorts below everything real; this is comparison aid only, never a
    claim that two versions are semver-equal.
    """
    cleaned = version.strip().lstrip("vV")
    parts: list[int] = []
    for piece in cleaned.split("."):
        digits = ""
        for char in piece:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else ()


def _is_newer(candidate: str, installed: str) -> bool:
    return version_key(candidate) > version_key(installed)


class CraftReleaseClient:
    """Async reader for the Craft app catalog's releases.

    ``transport`` replaces the network layer; ``policy`` narrows destinations.
    Construction never touches the network.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        policy: EgressPolicy | None = None,
        feed_url: str | None = FEED_URL,
        timeout: float = 20.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self._policy = policy if policy is not None else default_launcher_policy(env)
        self._feed_url = feed_url
        self._timeout = timeout
        self._transport = transport

    async def aclose(self) -> None:
        """Retained for the release-client lifecycle; requests own their transports."""
        return None

    @property
    def policy(self) -> EgressPolicy:
        return self._policy

    async def _get(self, url: str) -> httpx.Response:
        try:
            verdict = self._policy.enforce(
                url,
                tool=TOOL_NAME,
                resolve_dns=not isinstance(self._transport, httpx.MockTransport),
            )
            pinned = PinnedAsyncHTTPTransport(verdict, transport=self._transport)
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=pinned,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                return await client.get(url)
        except (EgressPolicyError, ValueError, httpx.HTTPError) as exc:
            raise LauncherError(exc) from None

    async def fetch_feed(self) -> FeedSnapshot:
        """The release feed, read leniently; unreachable feed yields an empty snapshot."""
        if not self._feed_url:
            return FeedSnapshot()
        try:
            response = await self._get(self._feed_url)
        except LauncherError:
            return FeedSnapshot()
        if response.status_code != 200:
            return FeedSnapshot()
        try:
            payload = response.json()
        except ValueError:
            return FeedSnapshot()
        return parse_feed(payload)

    async def github_latest(
        self, app: CraftApp, *, allow_prerelease: bool = False
    ) -> AppRelease | None:
        """GitHub's answer for the app: ``/releases/latest``, or the release list."""
        base = f"https://api.github.com/repos/{app.repo}/releases"
        if allow_prerelease:
            response = await self._get(base)
        else:
            response = await self._get(f"{base}/latest")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise LauncherError(f"GitHub answered {response.status_code} for {app.repo}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LauncherError(f"unreadable answer from GitHub ({exc})") from None
        if allow_prerelease:
            if not isinstance(payload, list):
                return None
            for item in payload:
                release = parse_github_release(item, app.repo)
                if release is not None:
                    return release
            return None
        return parse_github_release(payload, app.repo)

    async def _with_sums(self, release: AppRelease) -> AppRelease:
        """Fetch the release's ``SHA256SUMS.txt`` and fill in missing checksums.

        A missing sums file is not an error: GitHub's per-asset ``digest`` may
        already carry every checksum, and the launcher treats sums as a fill-in
        too (``Release::apply_sums``).
        """
        sums_asset = release.sums_asset()
        if sums_asset is None or not sums_asset.url:
            return release
        try:
            response = await self._get(sums_asset.url)
        except LauncherError:
            return release
        if response.status_code == 200:
            release.apply_sums(parse_sha256sums(response.text))
        return release

    async def latest_release(
        self,
        app: CraftApp | str,
        *,
        allow_prerelease: bool = False,
        with_sums: bool = True,
    ) -> AppRelease | None:
        """One app's newest release: fresh feed entry first, GitHub second.

        Returns ``None`` for an unknown app id or an app with no release at all;
        raises :class:`LauncherError` when a source answered but unusably.
        """
        if isinstance(app, str):
            resolved = by_id(app)
            if resolved is None:
                return None
            app = resolved

        feed = await self.fetch_feed()
        entry = feed.apps.get(app.id)
        if entry is not None and not feed.stale(max_age=FEED_MAX_AGE) and entry.tag:
            release = feed.release_for(app.id)
            if release is not None:
                return release if not with_sums else await self._with_sums(release)

        release = await self.github_latest(app, allow_prerelease=allow_prerelease)
        if release is None:
            return None
        return await self._with_sums(release) if with_sums else release

    async def check_updates(self, installed: dict[str, str]) -> list[UpdateInfo]:
        """Which installed apps have a newer release (``app_id -> version``).

        An unknown id is reported as a row with ``detail``, never a crash; one
        failing app never spoils the rest of the report.
        """
        results: list[UpdateInfo] = []
        feed = await self.fetch_feed()
        fresh = not feed.stale(max_age=FEED_MAX_AGE)
        for raw_id, version in installed.items():
            app = by_id(str(raw_id))
            if app is None:
                results.append(
                    UpdateInfo(
                        app_id=str(raw_id).lower(),
                        name=str(raw_id),
                        installed=str(version),
                        detail="unknown app id",
                    )
                )
                continue
            try:
                release: AppRelease | None = None
                entry = feed.apps.get(app.id)
                if fresh and entry is not None and entry.tag:
                    release = feed.release_for(app.id)
                if release is None:
                    release = await self.github_latest(app)
            except LauncherError as exc:
                results.append(
                    UpdateInfo(
                        app_id=app.id,
                        name=app.name,
                        installed=str(version),
                        detail=str(exc),
                    )
                )
                continue
            if release is None:
                results.append(
                    UpdateInfo(
                        app_id=app.id,
                        name=app.name,
                        installed=str(version),
                        detail="no release found",
                    )
                )
                continue
            latest = release.version_label()
            results.append(
                UpdateInfo(
                    app_id=app.id,
                    name=app.name,
                    installed=str(version),
                    latest=latest,
                    update_available=_is_newer(latest, str(version)),
                    release=release,
                )
            )
        return results


def catalog_rows() -> list[dict[str, Any]]:
    """The suite as plain rows — the static, zero-network part of every surface."""
    from os_launcher.catalog import SUITE

    return [app.model_dump(mode="json") for app in SUITE]
