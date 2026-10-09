"""Browser tool: DOM-mutation retry, text relocation and vision fallback."""

from __future__ import annotations

import pytest
from os_core.types import ToolStatus
from os_tools.browser import BrowserTool, FakeBrowser, PageSnapshot, RetryPolicy
from os_tools.ssrf import SSRFGuard


@pytest.fixture
def vision_calls() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def vision_tool(fake_browser: FakeBrowser, vision_calls: list) -> BrowserTool:
    async def vision(screenshot: str, selector: str) -> str:
        vision_calls.append((screenshot, selector))
        return "x=412,y=318"

    return BrowserTool(
        backend=fake_browser,
        policy=RetryPolicy(max_attempts=1, backoff=0.0),
        vision=vision,
    )


async def test_navigate_returns_page_structure(browser_tool: BrowserTool) -> None:
    result = await browser_tool.navigate("https://example.org/search")
    assert result.ok
    assert "https://example.org/search" in result.output
    assert "Search" in result.output  # the button's accessible text


async def test_navigate_emits_screenshot_artifact(browser_tool: BrowserTool) -> None:
    result = await browser_tool.navigate("https://example.org/search")
    assert [a.kind for a in result.artifacts] == ["image"]


async def test_navigate_to_unknown_page_fails_gracefully(browser_tool: BrowserTool) -> None:
    result = await browser_tool.navigate("https://example.org/missing")
    assert result.status is ToolStatus.ERROR
    assert "failed" in (result.error or "")


async def test_ssrf_guard_blocks_navigation_to_metadata(browser_tool: BrowserTool) -> None:
    guard = SSRFGuard(resolve_dns=False)
    result = await browser_tool.navigate(
        "http://169.254.169.254/latest/meta-data/", ssrf_check=guard.check
    )
    assert result.status is ToolStatus.DENIED
    assert "SSRF" in (result.error or "")
    assert browser_tool.blocked_urls == ["http://169.254.169.254/latest/meta-data/"]


async def test_retry_recovers_after_transient_dom_mutation(fake_browser: FakeBrowser) -> None:
    fake_browser.fail_until_attempt = {"#go": 1}  # first attempt fails, second succeeds
    tool = BrowserTool(backend=fake_browser, policy=RetryPolicy(max_attempts=3, backoff=0.0))
    result = await tool.act("click", "#go")
    assert result.ok
    assert "attempt 2" in result.output
    assert fake_browser.attempts["#go"] == 2


async def test_permanently_broken_selector_falls_back_to_vision(
    vision_tool: BrowserTool, vision_calls: list
) -> None:
    """Both the CSS path and its text relocation are dead -> vision must take over."""
    vision_tool.backend.fail_selectors = {"#submit", "text=submit"}  # type: ignore[attr-defined]
    result = await vision_tool.act("click", "#submit")
    assert result.ok
    assert vision_calls and vision_calls[0][1] == "#submit"
    assert result.metadata["strategy"] == "vision"
    assert result.metadata["coords"] == "x=412,y=318"
    assert "css_error" in result.metadata


async def test_text_relocation_is_tried_before_vision(fake_browser: FakeBrowser) -> None:
    """A selector with a known accessible text is relocated instead of dropped."""
    fake_browser.fail_selectors = {"#go"}
    tool = BrowserTool(
        backend=fake_browser,
        policy=RetryPolicy(max_attempts=1, backoff=0.0),
    )
    # Seed history as if '#go' had been seen with accessible text 'go'.
    tool.history.append({"selector": "#go", "text": "go"})
    result = await tool.act("click", "#go")
    assert result.ok
    assert tool.history[-1]["strategy"] == "text"
    assert fake_browser.actions == ["click:text=go"]


async def test_all_strategies_exhausted_reports_error(fake_browser: FakeBrowser) -> None:
    fake_browser.fail_selectors = {"#nope"}
    tool = BrowserTool(
        backend=fake_browser,
        policy=RetryPolicy(max_attempts=2, backoff=0.0, enable_text_relocation=False),
    )
    result = await tool.act("click", "#nope")
    assert result.status is ToolStatus.ERROR
    assert result.metadata["fallbacks_exhausted"] is True
    assert "2 attempts" in (result.error or "")


async def test_fill_passes_the_value_through(
    browser_tool: BrowserTool, fake_browser: FakeBrowser
) -> None:
    await browser_tool.navigate("https://example.org/search")
    result = await browser_tool.act("fill", "#q", value="DeepSeek-R1")
    assert result.ok
    assert "fill:#q=DeepSeek-R1" in fake_browser.actions


async def test_unsupported_action_is_an_error(browser_tool: BrowserTool) -> None:
    result = await browser_tool.act("hover", "#go")
    assert result.status is ToolStatus.ERROR
    assert "unsupported browser action" in (result.error or "")


def test_playwright_backend_reports_missing_dependency_clearly() -> None:
    """Playwright is optional; the error must say how to install it."""
    import importlib.util

    from os_tools.browser import PlaywrightBrowser

    if importlib.util.find_spec("playwright") is not None:
        pytest.skip("playwright is installed in this environment")
    import asyncio

    with pytest.raises(RuntimeError, match="pip install playwright"):
        asyncio.run(PlaywrightBrowser().navigate("https://example.org"))


async def test_snapshot_tool_shape() -> None:
    backend = FakeBrowser(
        pages={"https://example.org/": PageSnapshot(url="https://example.org/", title="Home")}
    )
    tool = BrowserTool(backend=backend, policy=RetryPolicy())
    await tool.navigate("https://example.org/")
    page = await tool.backend.snapshot()
    assert page.title == "Home"
