"""Deterministic replay of the reference artifact against the live mock."""

from __future__ import annotations

import json

import pytest
from playwright.async_api import Browser

from rote.devserver import MockServer
from rote.registry.store import Workspace

from .conftest import run_balance

pytestmark = pytest.mark.integration


async def test_happy_path_replays_and_returns_typed_output(workspace: Workspace, browser: Browser,
                                                          mock: MockServer) -> None:
    result = await run_balance(workspace, browser)
    assert result.status == "succeeded", result.error
    assert result.outputs == {"savings_balance": {"amount": "1234.56", "currency": "USD"}}
    assert [s.step_id for s in result.steps] == [
        "open_search", "enter_member_id", "submit_search", "open_member", "read_balance",
    ]
    assert result.drift == [] and result.recoveries == []

    run_dir = workspace.runs / result.run_id
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert "policy_decision" in kinds and "checkpoint" in kinds
    saved = json.loads((run_dir / "result.json").read_text())
    assert saved["outputs"]["savings_balance"]["amount"].startswith("[balance#")
    assert len(list((run_dir / "screens").glob("*.png"))) == 5
