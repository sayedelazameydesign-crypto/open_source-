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
        asyncio.run(PlaywrightBrowser().navigate("https://example.org", connect_ip="93.184.216.34"))


def test_playwright_browser_fails_closed_without_an_approved_ip() -> None:
    import asyncio

    from os_tools.browser import PlaywrightBrowser

    with pytest.raises(RuntimeError, match="DNS-authorized"):
        asyncio.run(PlaywrightBrowser().navigate("https://example.org"))


@pytest.mark.parametrize(
    "connect_ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "169.254.169.254",
        "100.100.100.200",
        "::1",
        "::ffff:127.0.0.1",
        "fd00:ec2::254",
    ],
)
def test_playwright_browser_rejects_non_public_connection_pins(connect_ip: str) -> None:
    import asyncio

    from os_tools.browser import PlaywrightBrowser

    with pytest.raises(RuntimeError, match="globally routable"):
        asyncio.run(PlaywrightBrowser().navigate("https://example.org", connect_ip=connect_ip))


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.1/",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_playwright_browser_rejects_ssrf_hosts_even_with_a_public_pin(url: str) -> None:
    import asyncio

    from os_tools.browser import PlaywrightBrowser

    with pytest.raises(RuntimeError, match="SSRF boundary"):
        asyncio.run(PlaywrightBrowser().navigate(url, connect_ip="93.184.216.34"))


def test_existing_playwright_page_cannot_navigate_without_a_fresh_pin() -> None:
    import asyncio

    from os_tools.browser import PlaywrightBrowser

    browser = PlaywrightBrowser()
    browser._page = object()
    with pytest.raises(RuntimeError, match="approved connect_ip"):
        asyncio.run(browser.navigate("https://example.org/changed"))


async def test_snapshot_tool_shape() -> None:
    backend = FakeBrowser(
        pages={"https://example.org/": PageSnapshot(url="https://example.org/", title="Home")}
    )
    tool = BrowserTool(backend=backend, policy=RetryPolicy())
    await tool.navigate("https://example.org/")
    page = await tool.backend.snapshot()
    assert page.title == "Home"


def test_fake_browser_center_is_deterministic_int_pair(fake_browser: FakeBrowser) -> None:
    x1, y1 = fake_browser.center("#go")
    x2, y2 = fake_browser.center("#go")
    assert isinstance(x1, int) and isinstance(y1, int)
    assert (x1, y1) == (x2, y2)
    assert 0 <= x1 <= fake_browser.viewport[0]
    assert 0 <= y1 <= fake_browser.viewport[1]


def test_fake_browser_different_selectors_get_different_centers(
    fake_browser: FakeBrowser,
) -> None:
    assert fake_browser.center("#submit") != fake_browser.center("#cancel")


async def test_click_returns_non_null_integer_coordinates(
    browser_tool: BrowserTool,
) -> None:
    await browser_tool.navigate("https://example.org/search")
    result = await browser_tool.act("click", "#go")
    assert result.ok
    assert isinstance(result.metadata.get("x"), int)
    assert isinstance(result.metadata.get("y"), int)
    assert 0 <= result.metadata["x"] <= 1280
    assert 0 <= result.metadata["y"] <= 800


async def test_fill_returns_non_null_integer_coordinates(
    browser_tool: BrowserTool,
) -> None:
    await browser_tool.navigate("https://example.org/search")
    result = await browser_tool.act("fill", "#q", value="hello")
    assert result.ok
    assert isinstance(result.metadata.get("x"), int)
    assert isinstance(result.metadata.get("y"), int)


@pytest.mark.parametrize(
    "name,kwargs",
    [
        ("goto", {"url": "https://example.org/search"}),
        ("browser_navigate", {"url": "https://example.org/search"}),
        ("click", {"selector": "#go"}),
        ("browser_click", {"selector": "#go"}),
        ("fill", {"selector": "#q", "value": "v"}),
        ("type", {"selector": "#q", "text": "v"}),
        ("scroll", {"selector": "body", "dx": 0, "dy": 100}),
        ("key", {"key": "Enter"}),
    ],
)
async def test_browser_action_routes_every_action(
    browser_tool: BrowserTool, name: str, kwargs: dict
) -> None:
    if name not in {"goto", "browser_navigate"}:
        await browser_tool.navigate("https://example.org/search")
    result = await browser_tool.browser_action(name, **kwargs)
    assert result.ok
    assert result.metadata["action"] in {"goto", "click", "fill", "type", "scroll", "key"}


def test_browser_tools_registers_all_expected_action_names() -> None:
    from os_tools.browser import browser_tools as make_tools

    backend = FakeBrowser(pages={"https://example.org/": PageSnapshot(url="https://example.org/")})
    tool = BrowserTool(backend=backend, policy=RetryPolicy(max_attempts=1, backoff=0.0))
    names = {t.name for t in make_tools(tool)}
    for expected in (
        "browser_navigate",
        "browser_click",
        "browser_fill",
        "browser_type",
        "browser_scroll",
        "browser_key",
        "browser_snapshot",
        "browser_action",
    ):
        assert expected in names


class _MockLocator:
    def __init__(self, box):
        self._box = dict(box) if box is not None else None
        self.first = self
        self._clicked = False
        self._filled_with = None
        self._typed = None

    async def bounding_box(self):
        return dict(self._box) if self._box is not None else None

    async def click(self):
        self._clicked = True

    async def fill(self, value):
        self._filled_with = value

    async def press_sequentially(self, text):
        self._typed = text


class _MockPage:
    def __init__(self, url: str = "https://example.org"):
        self.url = url
        self._locators: dict[str, _MockLocator] = {}

    def add_locator(self, selector: str, box) -> None:
        self._locators[selector] = _MockLocator(box)

    def locator(self, selector: str):
        if selector not in self._locators:
            self._locators[selector] = _MockLocator(
                {"x": 10.0, "y": 20.0, "width": 80.0, "height": 30.0}
            )
        return self._locators[selector]

    async def goto(self, url, wait_until=None):
        self.url = url

    async def click(self, selector):
        await self.locator(selector).click()

    async def fill(self, selector, value):
        await self.locator(selector).fill(value)

    async def content(self):
        return "<html></html>"

    async def title(self):
        return "mock"

    async def evaluate(self, *_a, **_k):
        return []

    async def screenshot(self):
        return b"\x89PNG"


async def test_playwright_browser_click_records_bounding_box_center() -> None:
    """Mocked locator verifies click() reads bounding_box and rounds to ints."""
    from os_tools.browser import PlaywrightBrowser

    pb = PlaywrightBrowser()
    page = _MockPage()
    page.add_locator("#btn", {"x": 100.0, "y": 200.0, "width": 60.0, "height": 30.0})
    # center -> (130, 215)
    pb._page = page  # bypasses _ensure_page which would launch real chromium
    snap = await pb.click("#btn")
    assert pb.center("#btn") == (130, 215)
    assert isinstance(snap, PageSnapshot)


async def test_playwright_browser_fill_records_bounding_box_center() -> None:
    from os_tools.browser import PlaywrightBrowser

    pb = PlaywrightBrowser()
    page = _MockPage()
    page.add_locator("#q", {"x": 0.0, "y": 0.0, "width": 201.0, "height": 41.0})
    pb._page = page
    await pb.fill("#q", "hi")
    cx, cy = pb.center("#q")  # type: ignore[misc]
    assert isinstance(cx, int) and isinstance(cy, int)
    assert 0 <= cx <= 201 and 0 <= cy <= 41
