"""Shared fixtures: one mock app and one browser for the whole integration session."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Browser, Page, Playwright, async_playwright

from rote.devserver import MockServer, start_mock
from rote.registry.store import Workspace
from rote.replay.engine import ReplayOptions, replay
from rote.schema.result import RunResult
from rote.surface.web.session import BrowserOptions, WebSession

PASSWORD = "training-only-2026"
REPO = Path(__file__).parents[2]
FIXTURES = REPO / "tests" / "fixtures"
BALANCE = "coreone.member.get_savings_balance"


@pytest.fixture
def workspace(tmp_path: Path, mock: MockServer, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A throwaway workspace: the repo's profile, tenants and policies plus the reference artifact."""
    for name in ("apps", "tenants", "policies"):
        shutil.copytree(REPO / name, tmp_path / name)
    capabilities = tmp_path / "capabilities" / "coreone"
    capabilities.mkdir(parents=True)
    shutil.copy(FIXTURES / "artifacts" / "get_savings_balance.authored.yaml",
                capabilities / "member.get_savings_balance.yaml")
    monkeypatch.setenv("COREONE_USERNAME", "svc_rote")
    monkeypatch.setenv("COREONE_PASSWORD", PASSWORD)
    monkeypatch.setenv("ROTE_REDACTION_KEY", "test-redaction-key")
    return Workspace(tmp_path, base_url_overrides={"harbor": mock.base_url("harbor"), "summit": mock.base_url("summit")})


@pytest.fixture(scope="session")
async def browser(playwright: Playwright) -> AsyncIterator[Browser]:
    instance = await playwright.chromium.launch(headless=True)
    yield instance
    await instance.close()


async def run_balance(workspace: Workspace, browser: Browser, member_id: str = "100234",
                      tenant: str = "harbor", **options: Any) -> RunResult:
    options.setdefault("require_approval", False)
    options.setdefault("step_timeout_ms", 4000)
    return await replay(workspace, BALANCE, tenant, {"member_id": member_id}, ReplayOptions(**options), browser=browser)


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
