from __future__ import annotations

import sys

import pytest
from playwright.async_api import Browser

from rote.devserver import MockServer
from rote.registry.store import Workspace

from .conftest import run_balance

pytestmark = pytest.mark.integration


async def test_replay_succeeds_with_the_model_client_unimportable(
    workspace: Workspace, browser: Browser, mock: MockServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)  # any import of it would now raise
    result = await run_balance(workspace, browser)
    assert result.status == "succeeded"
