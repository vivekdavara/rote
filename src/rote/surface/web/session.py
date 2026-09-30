"""Browser session management and access to the in-page helper library."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Error, Frame, JSHandle, Page, Playwright

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
    label inference and screenshots come out the same on every run.
    """

    def __init__(self, browser: Browser, context: BrowserContext, page: Page) -> None:
        self.browser = browser
        self.context = context
        self.page = page

    @classmethod
    async def launch(cls, playwright: Playwright, options: BrowserOptions | None = None) -> WebSession:
        options = options or BrowserOptions()
        browser = await playwright.chromium.launch(headless=options.headless)
        context = await browser.new_context(
            viewport={"width": options.width, "height": options.height},
            device_scale_factor=1,
            timezone_id=options.timezone,
            locale=options.locale,
        )
        await context.add_init_script(LIB_SOURCE)
        page = await context.new_page()
        return cls(browser, context, page)

    async def close(self) -> None:
        await self.context.close()
        await self.browser.close()

    def frame(self, name: str | None) -> Frame | None:
        """Look frames up by name on every call: a navigation can replace a frame."""
        if not name:
            return self.page.main_frame
        return self.page.frame(name=name)


async def call(frame: Frame, function: str, *args: Any) -> Any:
    """Call ``window.__rote.<function>(*args)`` in a frame, installing the library if needed."""
    script = f"(args) => window.__rote.{function}(...args)"
    try:
        return await frame.evaluate(script, list(args))
    except Error as exc:
        if "__rote" not in str(exc):
            raise
        await frame.evaluate(_LIB_EXPRESSION)
        return await frame.evaluate(script, list(args))


async def call_handle(frame: Frame, function: str, *args: Any) -> JSHandle:
    script = f"(args) => window.__rote.{function}(...args)"
    try:
        return await frame.evaluate_handle(script, list(args))
    except Error as exc:
        if "__rote" not in str(exc):
            raise
        await frame.evaluate(_LIB_EXPRESSION)
        return await frame.evaluate_handle(script, list(args))
