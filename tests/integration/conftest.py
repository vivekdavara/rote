"""Shared fixtures: one mock app and one browser for the whole integration session."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from playwright.async_api import Page, Playwright, async_playwright

from rote.devserver import MockServer, start_mock
from rote.surface.web.session import BrowserOptions, WebSession

PASSWORD = "training-only-2026"


@pytest.fixture(scope="session")
def mock() -> Iterator[MockServer]:
    server = start_mock(seed=7)
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def clean_mock(request: pytest.FixtureRequest) -> Iterator[None]:
    if "mock" in request.fixturenames:
        server: MockServer = request.getfixturevalue("mock")
        server.reset()
    yield


@pytest.fixture(scope="session")
async def playwright() -> AsyncIterator[Playwright]:
    async with async_playwright() as pw:
        yield pw


@pytest.fixture
async def session(playwright: Playwright) -> AsyncIterator[WebSession]:
    web = await WebSession.launch(playwright, BrowserOptions(headless=True))
    yield web
    await web.close()


async def sign_on(page: Page, base_url: str) -> None:
    """Test-only sign-on by position. Real runs sign on through the product profile."""
    await page.goto(f"{base_url}/login")
    await page.locator("input[type=text]").first.fill("svc_rote")
    await page.locator("input[type=password]").fill(PASSWORD)
    await page.get_by_role("button", name="Sign On").click()
    await page.frame_locator("frame[name=main]").get_by_text("Main Menu").wait_for()
