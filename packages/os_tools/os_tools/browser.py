"""Browser automation with dynamic retry and vision fallback.

The edge case named in the design doc: a selector that was valid when the plan
was written stops matching after a DOM mutation. The strategy implemented
here, in order:

1. **Retry with re-resolution** — re-query the selector (React re-renders can
   make a later attempt succeed).
2. **Text-based relocation** — if the element carried accessible text, find
   any element with the same text/role instead of the brittle CSS path.
3. **Vision fallback** — hand the screenshot to the VLM tier and let it return
   coordinates, so the flow survives a total selector collapse.

Two backends implement the same protocol: :class:`PlaywrightBrowser` (real
headless Chromium, needs ``pip install playwright && playwright install
chromium``) and :class:`FakeBrowser` (in-memory DOM, used by the test-suite so
the retry/fallback logic is exercised deterministically and offline).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol, runtime_checkable

from os_core.types import Artifact, ToolResult, ToolStatus
from pydantic import BaseModel, Field, PrivateAttr

SelectorStrategy = Literal["css", "text", "vision"]


class PageSnapshot(BaseModel):
    url: str
    title: str = ""
    html: str = ""
    elements: list[dict[str, Any]] = Field(default_factory=list)
    screenshot_b64: str | None = None


class StaleElementError(RuntimeError):
    """Raised when a selector no longer resolves (DOM mutation)."""


@runtime_checkable
class BrowserBackend(Protocol):
    async def navigate(self, url: str) -> PageSnapshot: ...
    async def click(self, selector: str) -> PageSnapshot: ...
    async def fill(self, selector: str, value: str) -> PageSnapshot: ...
    async def snapshot(self) -> PageSnapshot: ...
    async def close(self) -> None: ...


class FakeBrowser(BaseModel):
    """In-memory DOM with scriptable failures — how the fallbacks get tested."""

    pages: dict[str, PageSnapshot] = Field(default_factory=dict)
    fail_selectors: set[str] = Field(default_factory=set)
    fail_until_attempt: dict[str, int] = Field(default_factory=dict)
    attempts: dict[str, int] = Field(default_factory=dict)
    current: PageSnapshot | None = None
    actions: list[str] = Field(default_factory=list)
    navigated: list[str] = Field(default_factory=list)

    async def navigate(self, url: str) -> PageSnapshot:
        self.navigated.append(url)
        page = self.pages.get(url)
        if page is None:
            raise StaleElementError(f"no such page in fake browser: {url}")
        self.current = page
        return page

    async def click(self, selector: str) -> PageSnapshot:
        self._bump(selector)
        if self._should_fail(selector):
            raise StaleElementError(f"selector '{selector}' did not resolve (DOM mutated)")
        self.actions.append(f"click:{selector}")
        return self.current or PageSnapshot(url="about:blank")

    async def fill(self, selector: str, value: str) -> PageSnapshot:
        self._bump(selector)
        if self._should_fail(selector):
            raise StaleElementError(f"selector '{selector}' did not resolve (DOM mutated)")
        self.actions.append(f"fill:{selector}={value}")
        return self.current or PageSnapshot(url="about:blank")

    async def snapshot(self) -> PageSnapshot:
        return self.current or PageSnapshot(url="about:blank")

    async def close(self) -> None:
        self.current = None

    def _bump(self, selector: str) -> None:
        self.attempts[selector] = self.attempts.get(selector, 0) + 1

    def _should_fail(self, selector: str) -> bool:
        if selector in self.fail_selectors:
            return True
        threshold = self.fail_until_attempt.get(selector)
        if threshold is None:
            return False
        return self.attempts[selector] <= threshold


class PlaywrightBrowser(BaseModel):
    """Real headless Chromium. Imported lazily so Playwright stays optional."""

    headless: bool = True
    viewport: tuple[int, int] = (1280, 800)
    timeout_ms: int = 15_000
    _pw: Any = PrivateAttr(default=None)
    _browser: Any = PrivateAttr(default=None)
    _page: Any = PrivateAttr(default=None)

    model_config = {"arbitrary_types_allowed": True}

    async def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Playwright is not installed. Run: pip install playwright && "
                "playwright install chromium"
            ) from exc
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        context = await self._browser.new_context(
            viewport={"width": self.viewport[0], "height": self.viewport[1]}
        )
        self._page = await context.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        return self._page

    async def navigate(self, url: str) -> PageSnapshot:
        page = await self._ensure_page()
        await page.goto(url, wait_until="domcontentloaded")
        return await self._snapshot(page)

    async def click(self, selector: str) -> PageSnapshot:
        page = await self._ensure_page()
        try:
            await page.click(selector)
        except Exception as exc:
            raise StaleElementError(f"click failed for '{selector}': {exc}") from exc
        return await self._snapshot(page)

    async def fill(self, selector: str, value: str) -> PageSnapshot:
        page = await self._ensure_page()
        try:
            await page.fill(selector, value)
        except Exception as exc:
            raise StaleElementError(f"fill failed for '{selector}': {exc}") from exc
        return await self._snapshot(page)

    async def snapshot(self) -> PageSnapshot:
        page = await self._ensure_page()
        return await self._snapshot(page)

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
            self._page = None

    async def _snapshot(self, page: Any) -> PageSnapshot:
        import base64

        elements = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a,button,input,[role=button]'))
                .slice(0, 200).map((el, i) => ({
                    index: i, tag: el.tagName.toLowerCase(),
                    text: (el.innerText || el.value || '').trim().slice(0, 120),
                    role: el.getAttribute('role') || '',
                    id: el.id || '', cls: el.className || ''
                }))"""
        )
        shot = await page.screenshot()
        return PageSnapshot(
            url=page.url,
            title=await page.title(),
            html=await page.content(),
            elements=elements,
            screenshot_b64=base64.b64encode(shot).decode(),
        )


class RetryPolicy(BaseModel):
    """Dynamic retry & fallback configuration."""

    max_attempts: int = 3
    backoff: float = 0.2
    enable_text_relocation: bool = True
    enable_vision_fallback: bool = True


VisionFn = Callable[[str, str], Awaitable[str]]


class BrowserTool(BaseModel):
    """The agent-facing browser capability."""

    backend: BrowserBackend
    policy: RetryPolicy = Field(default_factory=RetryPolicy)
    vision: VisionFn | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    blocked_urls: list[str] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}

    async def navigate(
        self, url: str, *, ssrf_check: Callable[[str], Any] | None = None
    ) -> ToolResult:
        if ssrf_check is not None:
            verdict = ssrf_check(url)
            if not getattr(verdict, "allowed", True):
                self.blocked_urls.append(url)
                return ToolResult(
                    tool_call_id="",
                    tool_name="browser_navigate",
                    status=ToolStatus.DENIED,
                    error=f"SSRF guard blocked navigation: {getattr(verdict, 'reason', '')}",
                )
        try:
            page = await self.backend.navigate(url)
        except Exception as exc:
            return ToolResult(
                tool_call_id="",
                tool_name="browser_navigate",
                status=ToolStatus.ERROR,
                error=f"navigation to {url} failed: {exc}",
            )
        return self._page_result("browser_navigate", page, note=f"navigated to {url}")

    async def act(self, action: str, selector: str, *, value: str = "") -> ToolResult:
        """Click/fill with the full retry -> relocate -> vision chain."""
        if action not in {"click", "fill"}:
            return ToolResult(
                tool_call_id="",
                tool_name=f"browser_{action}",
                status=ToolStatus.ERROR,
                error=f"unsupported browser action '{action}' (supported: click, fill)",
            )
        last_error = "unknown error"
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                page = await self._dispatch(action, selector, value)
                self.history.append(
                    {"action": action, "selector": selector, "attempt": attempt, "strategy": "css"}
                )
                return self._page_result(
                    f"browser_{action}",
                    page,
                    note=f"{action} '{selector}' ok on attempt {attempt}",
                )
            except StaleElementError as exc:
                last_error = str(exc)
                if attempt < self.policy.max_attempts:
                    await asyncio.sleep(self.policy.backoff * attempt)

        # Fallback 1: relocate by accessible text instead of a brittle path.
        if self.policy.enable_text_relocation:
            relocated = self._relocate_by_text(selector)
            if relocated:
                try:
                    page = await self._dispatch(action, relocated, value)
                    self.history.append(
                        {
                            "action": action,
                            "selector": relocated,
                            "strategy": "text",
                            "original": selector,
                        }
                    )
                    return self._page_result(
                        f"browser_{action}",
                        page,
                        note=f"relocated '{selector}' -> '{relocated}' by accessible text",
                    )
                except StaleElementError as exc:
                    last_error = str(exc)

        # Fallback 2: ask the VLM for coordinates from the screenshot.
        if self.policy.enable_vision_fallback and self.vision is not None:
            try:
                snap = await self.backend.snapshot()
            except Exception:
                snap = PageSnapshot(url="about:blank")
            coords = await self.vision(snap.screenshot_b64 or "", selector)
            self.history.append(
                {"action": action, "selector": selector, "strategy": "vision", "coords": coords}
            )
            return ToolResult(
                tool_call_id="",
                tool_name=f"browser_{action}",
                status=ToolStatus.SUCCESS,
                output=f"vision fallback produced target '{coords}' for '{selector}'",
                metadata={
                    "strategy": "vision",
                    "coords": coords,
                    "css_error": last_error,
                    "url": snap.url,
                },
            )

        return ToolResult(
            tool_call_id="",
            tool_name=f"browser_{action}",
            status=ToolStatus.ERROR,
            error=f"selector '{selector}' failed after {self.policy.max_attempts} attempts "
            f"and both fallbacks: {last_error}",
            metadata={"attempts": self.policy.max_attempts, "fallbacks_exhausted": True},
        )

    async def close(self) -> None:
        await self.backend.close()

    # -- internals ------------------------------------------------------
    async def _dispatch(self, action: str, selector: str, value: str) -> PageSnapshot:
        if action == "click":
            return await self.backend.click(selector)
        if action == "fill":
            return await self.backend.fill(selector, value)
        raise ValueError(f"unsupported browser action '{action}'")

    def _relocate_by_text(self, selector: str) -> str | None:
        """Turn '#submit-btn' into a text locator when the element had text."""
        if selector.startswith("text="):
            return None
        for entry in self.history:
            if entry.get("selector") == selector and entry.get("text"):
                return f"text={entry['text']}"
        cleaned = selector.lstrip("#.").replace("-", " ").replace("_", " ").strip()
        if not cleaned or " " in cleaned:
            return None
        return f"text={cleaned}"

    def _page_result(self, tool_name: str, page: PageSnapshot, *, note: str = "") -> ToolResult:
        artifacts: list[Artifact] = []
        if page.screenshot_b64:
            artifacts.append(
                Artifact(
                    kind="image",
                    filename="screenshot.png",
                    content=page.screenshot_b64,
                    mime_type="image/png",
                    metadata={"encoding": "base64"},
                )
            )
        elements = page.elements[:40]
        summary = (
            "\n".join(
                f"- [{el.get('tag', '?')}] {(el.get('text') or el.get('id') or '')[:60]}"
                for el in elements
            )
            or "(no interactive elements)"
        )
        return ToolResult(
            tool_call_id="",
            tool_name=tool_name,
            status=ToolStatus.SUCCESS,
            output=f"{note}\nURL: {page.url}\nTitle: {page.title}\n"
            f"Interactive elements ({len(page.elements)}):\n{summary}",
            artifacts=artifacts,
            metadata={"url": page.url, "title": page.title, "element_count": len(page.elements)},
        )


def browser_tools(tool: BrowserTool, *, ssrf_check: Callable[[str], Any] | None = None):
    """Expose the browser as registry tools."""
    from os_core.types import RiskLevel

    from os_tools.registry import Tool

    async def _navigate(url: str) -> ToolResult:
        return await tool.navigate(url, ssrf_check=ssrf_check)

    async def _click(selector: str) -> ToolResult:
        return await tool.act("click", selector)

    async def _fill(selector: str, value: str) -> ToolResult:
        return await tool.act("fill", selector, value=value)

    async def _snapshot() -> ToolResult:
        page = await tool.backend.snapshot()
        return tool._page_result("browser_snapshot", page, note="snapshot of current page")

    return [
        Tool(
            name="browser_navigate",
            description=(
                "Open a URL in the sandboxed headless browser and return the page structure."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            risk=RiskLevel.NETWORK,
            fn=_navigate,
        ),
        Tool(
            name="browser_click",
            description="Click an element. Uses retry, text relocation and vision fallback.",
            parameters={
                "type": "object",
                "properties": {"selector": {"type": "string"}},
                "required": ["selector"],
            },
            risk=RiskLevel.WRITE,
            fn=_click,
        ),
        Tool(
            name="browser_fill",
            description="Type a value into an input element.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["selector", "value"],
            },
            risk=RiskLevel.WRITE,
            fn=_fill,
        ),
        Tool(
            name="browser_snapshot",
            description="Return the current page structure and a screenshot artifact.",
            parameters={"type": "object", "properties": {}},
            risk=RiskLevel.READ_ONLY,
            fn=_snapshot,
        ),
    ]
