"""Opt-in Chromium check against a real page with cross-origin resources.

Run explicitly with ``pytest -m 'browser and network' tests/test_browser_live.py``.
The default offline suite deselects this test. CI's browser job installs Chromium
and runs it so the same-origin egress rule is measured against a live page, not
only the in-memory demo or ``example.org``.
"""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlsplit

import pytest
from os_tools.browser import PlaywrightBrowser
from os_tools.egress import EgressPolicy, parse_http_target


@pytest.mark.browser
@pytest.mark.network
async def test_chromium_loads_real_python_page_and_blocks_cross_origin_cdn() -> None:
    url = "https://www.python.org/"
    policy = EgressPolicy.allow_hosts(
        "www.python.org",
        schemes={"https"},
        ports={443},
        tools={"live_browser_check"},
        resolve_dns=True,
        default_tool="live_browser_check",
    )
    verdict = policy.enforce(url, tool="live_browser_check")
    approved = verdict.connect_ips or ([verdict.connect_ip] if verdict.connect_ip else [])
    # Prefer an already-validated IPv4 answer for runners without IPv6 egress.
    # This does not introduce another lookup or widen the origin allowlist.
    connect_ip = next(
        (candidate for candidate in approved if ipaddress.ip_address(candidate).version == 4),
        approved[0] if approved else None,
    )
    assert connect_ip is not None, "the authorized hostname produced no connectable IP"

    browser = PlaywrightBrowser(timeout_ms=25_000)
    cross_origin_requests: list[tuple[str, str]] = []
    blocked_cross_origin: list[tuple[str, str | None, str]] = []
    origin = parse_http_target(url)

    def is_cross_origin(request_url: str) -> bool:
        try:
            return parse_http_target(request_url) != origin
        except ValueError:
            return True

    try:
        page = await browser._ensure_page(url, connect_ip)
        page.on(
            "request",
            lambda request: (
                cross_origin_requests.append((request.url, request.resource_type))
                if is_cross_origin(request.url)
                else None
            ),
        )
        page.on(
            "requestfailed",
            lambda request: (
                blocked_cross_origin.append((request.url, request.failure, request.resource_type))
                if is_cross_origin(request.url)
                else None
            ),
        )
        snapshot = await browser.navigate(url, connect_ip=connect_ip)
        body_text = await page.locator("body").inner_text()
    finally:
        await browser.close()

    assert snapshot.url.startswith("https://www.python.org/")
    assert "Python" in snapshot.title or "Python" in body_text
    assert len(body_text.strip()) > 100, "the live page rendered no useful main-origin content"
    assert cross_origin_requests, "the selected real page no longer exercises cross-origin requests"
    assert blocked_cross_origin, "cross-origin requests were not blocked by the egress route"
    assert any(
        resource_type in {"font", "image", "script", "stylesheet"}
        for _, resource_type in cross_origin_requests
    ), "the page no longer requests a cross-origin CDN-style asset"
    assert all("ERR_BLOCKED_BY_CLIENT" in (failure or "") for _, failure, _ in blocked_cross_origin)

    # CI keeps a compact, non-sensitive summary in the test log so the impact
    # (main document works; cross-origin assets fail closed) is observable per run.
    print(
        json.dumps(
            {
                "url": snapshot.url,
                "title": snapshot.title,
                "main_origin_text_chars": len(body_text),
                "cross_origin_requests": len(cross_origin_requests),
                "blocked_cross_origin": len(blocked_cross_origin),
                "blocked_examples": [
                    {"host": urlsplit(url).hostname, "resource_type": resource_type}
                    for url, _failure, resource_type in blocked_cross_origin[:5]
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
