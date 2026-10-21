"""Native reach channels — the zero-config tier, httpx and the stdlib only.

These are the channels that need no external CLI and no account: reading a
public web page, an RSS/Atom feed, and public GitHub data. They are also the
*tier-0 fallback* that keeps the platform useful when Agent-Reach is not
installed, and the offline-testable core of the reach layer: every network
call goes through :class:`ReachFetcher`, which

* authorizes **every hop** against the egress policy (a redirect cannot smuggle
  you to a host the policy never saw),
* streams with a hard byte cap, so one page cannot swallow a token budget, and
* reads secrets from the environment at request time (ADR-0011 style).

No third-party parsing library is used: stripping HTML and reading RSS/Atom is
a few dozen lines, and that keeps the reach layer dependency-free.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from os_tools.egress import EgressPolicy, EgressVerdict, parse_http_target
from os_tools.pinned_http import PinnedAsyncHTTPTransport
from pydantic import BaseModel, Field

from os_reach.contract import ChannelHealth, ReachState, ReachTier
from os_reach.security import default_reach_policy, truncate

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_SCRIPT_RE = re.compile(r"(?is)<(script|style|noscript|template|svg)\b.*?</\1\s*>")
_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_BLOCK_TAGS = (
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "li",
    "tr",
    "br",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
)
_BLOCK_TAGS_ALT = "|".join(_BLOCK_TAGS)
_BLOCK_RE = re.compile(rf"(?i)<\s*/?\s*(?:{_BLOCK_TAGS_ALT})\b[^>]*>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


class FetchResult(BaseModel):
    """One successful fetch: where it ended, how big it was, what it said."""

    url: str
    status_code: int
    text: str
    bytes_read: int
    truncated: bool = False


class ReachFetcher(BaseModel):
    """Policy-gated HTTP GET/JSON with redirect re-authorization and byte caps."""

    policy: EgressPolicy = Field(default_factory=default_reach_policy)
    tool: str = "web_read"
    client_timeout: float = 20.0
    max_bytes: int = 200_000
    max_redirects: int = 3
    user_agent: str = "cela-reach/0.1 (+https://github.com/sayedelazameydesign-crypto/open_source-)"
    transport: Any = Field(default=None, exclude=True)  # httpx transport, tests only

    model_config = {"arbitrary_types_allowed": True}

    def authorize(self, url: str) -> EgressVerdict:
        """Authorize one hop and return the IP the network transport must use."""
        import httpx

        return self.policy.enforce(
            url,
            tool=self.tool,
            # MockTransport has no socket and intentionally keeps tests offline.
            resolve_dns=not isinstance(self.transport, httpx.MockTransport),
        )

    async def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    ) -> FetchResult:
        """GET *url*, following at most ``max_redirects`` hops, each authorized."""
        import httpx

        request_headers = {"User-Agent": self.user_agent, "Accept": accept}
        if headers:
            request_headers.update(headers)
        caller_has_sensitive_headers = any(
            name.lower() not in {"accept", "user-agent"} for name in (headers or {})
        )

        current = url
        for _ in range(self.max_redirects + 1):
            verdict = self.authorize(current)
            pinned_transport = PinnedAsyncHTTPTransport(verdict, transport=self.transport)
            async with (
                httpx.AsyncClient(
                    timeout=self.client_timeout,
                    transport=pinned_transport,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream("GET", current, headers=request_headers) as resp,
            ):
                if resp.status_code in _REDIRECT_CODES:
                    nxt = resp.next_request
                    if nxt is None:  # pragma: no cover - httpx always builds one
                        raise RuntimeError(f"redirect without a target from {current}")
                    # The next hop is separately resolved, checked and pinned;
                    # DNS cannot change between authorization and connect. Never
                    # forward caller-supplied credentials/custom headers to a
                    # different origin, even when both origins are allow-listed.
                    next_url = str(nxt.url)
                    if caller_has_sensitive_headers and parse_http_target(
                        current
                    ) != parse_http_target(next_url):
                        raise RuntimeError(
                            "refusing cross-origin redirect with caller-provided headers"
                        )
                    current = next_url
                    continue
                resp.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.max_bytes:
                        break
                encoding = resp.encoding or "utf-8"
                raw = b"".join(chunks)
                truncated = size > self.max_bytes
                return FetchResult(
                    url=str(resp.url),
                    status_code=resp.status_code,
                    text=raw[: self.max_bytes].decode(encoding, errors="replace"),
                    bytes_read=min(size, self.max_bytes),
                    truncated=truncated,
                )
        raise RuntimeError(f"too many redirects (>{self.max_redirects}) starting at {url}")

    async def fetch_json(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        """Fetch and parse a JSON document, capped like any other body."""
        merged = {"Accept": "application/json"}
        if headers:
            merged.update(headers)
        result = await self.fetch(url, headers=merged)
        try:
            return json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"expected JSON from {result.url}: {exc}") from None


def html_to_text(html: str) -> str:
    """Reduce HTML to readable text: no scripts, no tags, no entity soup."""
    text = _COMMENT_RE.sub(" ", html)
    text = _SCRIPT_RE.sub(" ", text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = unicodedata.normalize("NFKC", text)
    text = _WS_RE.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines).strip())


class FeedEntry(BaseModel):
    """One RSS item / Atom entry."""

    title: str = ""
    link: str = ""
    published: str = ""
    summary: str = ""


def parse_feed(text: str, *, limit: int = 10) -> list[FeedEntry]:
    """Parse RSS 2.0 or Atom without feedparser.

    ``<!DOCTYPE``/``<!ENTITY`` documents are refused outright: entity expansion
    is the one classic XML attack a stdlib parser will happily perform, and no
    legitimate feed needs it.
    """
    lowered = text[:4096].lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise RuntimeError("refusing a feed that declares a DOCTYPE or ENTITY (XML entity attack)")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RuntimeError(f"feed is not valid XML: {exc}") from None

    atom = "{http://www.w3.org/2005/Atom}"
    entries: list[FeedEntry] = []
    items = root.iter("item") if root.tag != f"{atom}feed" else root.iter(f"{atom}entry")
    for item in items:
        if len(entries) >= limit:
            break
        if item.tag.startswith(atom):
            link = ""
            for node in item.iter(f"{atom}link"):
                href = node.get("href")
                if href:
                    link = href
                    break
            entries.append(
                FeedEntry(
                    title=(item.findtext(f"{atom}title") or "").strip(),
                    link=link.strip(),
                    published=(
                        item.findtext(f"{atom}published") or item.findtext(f"{atom}updated") or ""
                    ).strip(),
                    summary=html_to_text(item.findtext(f"{atom}summary") or "")[:400],
                )
            )
            continue
        entries.append(
            FeedEntry(
                title=(item.findtext("title") or "").strip(),
                link=(item.findtext("link") or "").strip(),
                published=(item.findtext("pubDate") or "").strip(),
                summary=html_to_text(item.findtext("description") or "")[:400],
            )
        )
    return entries


class WebChannel(BaseModel):
    """Read any public page as text — the catch-all channel, registered last."""

    name: str = "web"
    description: str = "any public web page (httpx → text)"
    tier: ReachTier = ReachTier.ZERO_CONFIG
    backends: list[str] = Field(default_factory=lambda: ["httpx"])
    fetcher: ReachFetcher = Field(default_factory=lambda: ReachFetcher(tool="web_read"))

    def can_handle(self, url: str) -> bool:
        return True

    def probe(self) -> ChannelHealth:
        return ChannelHealth(
            channel=self.name,
            status=ReachState.OK,
            message="no setup needed; every destination is checked against the egress policy",
            label=self.description,
            tier=self.tier,
            backends=list(self.backends),
            active_backend=self.backends[0],
        )

    async def read(self, url: str, *, max_chars: int = 20_000) -> str:
        result = await self.fetcher.fetch(url)
        body = html_to_text(result.text) if "<" in result.text[:512] else result.text
        return truncate(body, max_chars)


class RssChannel(BaseModel):
    """Read an RSS/Atom feed's latest entries."""

    name: str = "rss"
    description: str = "RSS/Atom feeds (stdlib XML)"
    tier: ReachTier = ReachTier.ZERO_CONFIG
    backends: list[str] = Field(default_factory=lambda: ["stdlib-xml"])
    fetcher: ReachFetcher = Field(default_factory=lambda: ReachFetcher(tool="rss_read"))

    def can_handle(self, url: str) -> bool:
        lowered = url.lower()
        return any(marker in lowered for marker in ("/feed", "/rss", ".xml", "atom"))

    def probe(self) -> ChannelHealth:
        return ChannelHealth(
            channel=self.name,
            status=ReachState.OK,
            message="no setup needed; DOCTYPE/ENTITY feeds are refused",
            label=self.description,
            tier=self.tier,
            backends=list(self.backends),
            active_backend=self.backends[0],
        )

    async def read(self, url: str, *, limit: int = 10) -> list[FeedEntry]:
        result = await self.fetcher.fetch(
            url, accept="application/rss+xml,application/atom+xml,*/*"
        )
        return parse_feed(result.text, limit=limit)


class GitHubChannel(BaseModel):
    """Public GitHub data over the REST API."""

    name: str = "github"
    description: str = "public repositories and code search (REST API)"
    tier: ReachTier = ReachTier.ZERO_CONFIG
    backends: list[str] = Field(default_factory=lambda: ["rest-api"])
    fetcher: ReachFetcher = Field(default_factory=lambda: ReachFetcher(tool="github_read"))

    def can_handle(self, url: str) -> bool:
        host = urllib.parse.urlparse(url).hostname or ""
        return host.lower() in {"github.com", "www.github.com", "api.github.com"}

    def probe(self) -> ChannelHealth:
        has_token = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
        message = (
            "token present in the environment"
            if has_token
            else "no token: 60 requests/hour, read-only public data"
        )
        return ChannelHealth(
            channel=self.name,
            status=ReachState.OK,
            message=message,
            label=self.description,
            tier=self.tier,
            backends=list(self.backends),
            active_backend=self.backends[0],
        )

    @staticmethod
    def _token_headers() -> dict[str, str]:
        """Read the token *at request time*; it is never stored or serialised."""
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        return {"Authorization": f"Bearer {token}"} if token else {}

    @staticmethod
    def normalise_repo(repo: str) -> str:
        """Accept ``owner/name``, a github.com URL, or an api.github.com URL."""
        cleaned = repo.strip()
        for prefix in ("https://api.github.com/repos/", "http://api.github.com/repos/"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :]
        for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :]
        cleaned = cleaned.removesuffix(".git").strip("/")
        if cleaned.count("/") != 1 or not all(cleaned.split("/")):
            raise ValueError(f"{repo!r} is not an 'owner/name' repository")
        return cleaned

    @staticmethod
    def _summarise_repo(payload: dict[str, Any]) -> dict[str, Any]:
        license_info = payload.get("license") or {}
        return {
            "full_name": payload.get("full_name", ""),
            "description": payload.get("description") or "",
            "stars": payload.get("stargazers_count", 0),
            "forks": payload.get("forks_count", 0),
            "open_issues": payload.get("open_issues_count", 0),
            "language": payload.get("language") or "",
            "license": license_info.get("spdx_id") or "",
            "topics": payload.get("topics") or [],
            "archived": bool(payload.get("archived")),
            "updated_at": payload.get("updated_at", ""),
            "url": payload.get("html_url", ""),
        }

    async def repo(self, repo: str) -> dict[str, Any]:
        slug = self.normalise_repo(repo)
        payload = await self.fetcher.fetch_json(
            f"https://api.github.com/repos/{slug}", headers=self._token_headers()
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"unexpected GitHub payload for {slug}")
        return self._summarise_repo(payload)

    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if not query.strip():
            raise ValueError("search needs a non-empty query")
        bounded = max(1, min(int(limit), 20))
        url = (
            "https://api.github.com/search/repositories"
            f"?q={urllib.parse.quote(query.strip())}&sort=stars&order=desc&per_page={bounded}"
        )
        payload = await self.fetcher.fetch_json(url, headers=self._token_headers())
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("GitHub search returned no 'items' list")
        return [self._summarise_repo(item) for item in items if isinstance(item, dict)][:bounded]


def native_channels(
    *,
    policy: EgressPolicy | None = None,
    transport: Any = None,
) -> list[Any]:
    """The tier-0 channels, in routing order (``web`` last: it matches anything)."""
    shared = policy if policy is not None else default_reach_policy()
    return [
        GitHubChannel(fetcher=ReachFetcher(policy=shared, tool="github_read", transport=transport)),
        RssChannel(fetcher=ReachFetcher(policy=shared, tool="rss_read", transport=transport)),
        WebChannel(fetcher=ReachFetcher(policy=shared, tool="web_read", transport=transport)),
    ]
