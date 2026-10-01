"""Browser session management and access to the in-page helper library."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Error, Frame, JSHandle, Page, Playwright, Request

LIB_SOURCE = (Path(__file__).parent / "rote_lib.js").read_text(encoding="utf-8")
_LIB_EXPRESSION = LIB_SOURCE.strip().rstrip(";")


@dataclass(frozen=True)
class BrowserOptions:
    headless: bool = True
    width: int = 1280
    height: int = 800
    timezone: str = "America/New_York"
    locale: str = "en-US"


class WebSession:
    """One browser context and page, with the helper library in every frame.

    The viewport, clock zone, locale and scale factor are pinned so geometry-based
    label inference and screenshots come out the same on every run. Each run gets
    a fresh context, so no cookies or state leak between runs.
    """

    def __init__(self, browser: Browser, context: BrowserContext, page: Page, *, owns_browser: bool) -> None:
        self.browser = browser
        self.context = context
        self.page = page
        self._owns_browser = owns_browser
        self._pending_documents: set[Request] = set()
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_request_done)
        page.on("requestfailed", self._on_request_done)

    @classmethod
    async def launch(cls, playwright: Playwright, options: BrowserOptions | None = None) -> WebSession:
        options = options or BrowserOptions()
        browser = await playwright.chromium.launch(headless=options.headless)
        return await cls.open(browser, options, owns_browser=True)

    @classmethod
    async def open(
        cls, browser: Browser, options: BrowserOptions | None = None, *, owns_browser: bool = False
    ) -> WebSession:
        options = options or BrowserOptions()
        context = await browser.new_context(
            viewport={"width": options.width, "height": options.height},
            device_scale_factor=1,
            timezone_id=options.timezone,
            locale=options.locale,
        )
        await context.add_init_script(LIB_SOURCE)
        page = await context.new_page()
        return cls(browser, context, page, owns_browser=owns_browser)

    async def close(self) -> None:
        await self.context.close()
        if self._owns_browser:
            await self.browser.close()

    def _on_request(self, request: Request) -> None:
        if request.resource_type == "document":
            self._pending_documents.add(request)

    def _on_request_done(self, request: Request) -> None:
        self._pending_documents.discard(request)

    @property
    def navigating(self) -> bool:
        """True while any frame is still waiting for a document: the app is loading."""
        return bool(self._pending_documents)

    def frame(self, name: str | None) -> Frame | None:
        """Look frames up by name on every call: a navigation can replace a frame."""
        if not name:
            return self.page.main_frame
        return self.page.frame(name=name)


class EvaluateTimeout(Error):
    """An in-page call did not return: typically a native dialog is blocking the page."""


CALL_TIMEOUT_S = 5.0


async def _bounded(awaitable: Any) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=CALL_TIMEOUT_S)
    except TimeoutError as exc:
        raise EvaluateTimeout("in-page call timed out (is a native dialog open?)") from exc


async def call(frame: Frame, function: str, *args: Any) -> Any:
    """Call ``window.__rote.<function>(*args)`` in a frame, installing the library if needed.

    Bounded by a timeout, because a native alert blocks the page's JavaScript and an
    unbounded evaluate would stop the engine from ever noticing the dialog.
    """
    script = f"(args) => window.__rote.{function}(...args)"
    try:
        return await _bounded(frame.evaluate(script, list(args)))
    except EvaluateTimeout:
        raise
    except Error as exc:
        if "__rote" not in str(exc):
            raise
        await _bounded(frame.evaluate(_LIB_EXPRESSION))
        return await _bounded(frame.evaluate(script, list(args)))


async def call_handle(frame: Frame, function: str, *args: Any) -> JSHandle:
    script = f"(args) => window.__rote.{function}(...args)"
    try:
        return await frame.evaluate_handle(script, list(args))
    except Error as exc:
        if "__rote" not in str(exc):
            raise
        await frame.evaluate(_LIB_EXPRESSION)
        return await frame.evaluate_handle(script, list(args))
