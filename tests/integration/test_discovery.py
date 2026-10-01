"""Discovery end to end with a scripted cassette (no model): trace -> compile -> verify -> outcomes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from playwright.async_api import Browser

from rote.devserver import MockServer
from rote.discovery.agent import DiscoveryAgent, DiscoveryOptions
from rote.discovery.cassette import load_cassette
from rote.discovery.planner import CassettePlanner
from rote.registry.store import Workspace
from rote.schema.capability import ClickStep, ExtractStep, FillStep, load_capability
from rote.schema.condition import AllOf, TextVisible
from rote.schema.spec import load_spec
from rote.schema.target import TableCellLocator

from .conftest import FIXTURES, REPO, run_balance

pytestmark = pytest.mark.integration

SCRIPTED = FIXTURES / "cassettes" / "get_savings_balance.scripted.json"


@pytest.fixture
def discovery_workspace(workspace: Workspace) -> Workspace:
    shutil.rmtree(workspace.root / "capabilities")  # discovery must produce the artifact itself
    shutil.copytree(REPO / "specs", workspace.root / "specs")
    return workspace


async def discover(workspace: Workspace, browser: Browser) -> object:
    spec = load_spec(workspace.root / "specs" / "get_savings_balance.yaml")
    planner = CassettePlanner(load_cassette(SCRIPTED), spec.example(0))
    agent = DiscoveryAgent(workspace, spec, planner, DiscoveryOptions(), browser=browser)
    return await agent.run()


async def test_discovery_end_to_end(discovery_workspace: Workspace, browser: Browser, mock: MockServer) -> None:
    result = await discover(discovery_workspace, browser)
    assert result.status == "compiled", result.reason  # type: ignore[attr-defined]
    capability = load_capability(result.capability_path)  # type: ignore[attr-defined]

    # The compiled steps match what a person would have written by hand.
    assert [type(s).__name__ for s in capability.steps] == [
        "ClickStep", "FillStep", "ClickStep", "ClickStep", "ExtractStep",
    ]
    fill = capability.steps[1]
    assert isinstance(fill, FillStep) and fill.value == "{{inputs.member_id}}"
    view = capability.steps[3]
    assert isinstance(view, ClickStep)
    first = view.target.locators[0]
    assert isinstance(first, TableCellLocator) and first.row == {"Member #": "{{inputs.member_id}}"}
    assert isinstance(view.expect, AllOf)
    assert {c.text_visible for c in view.expect.all if isinstance(c, TextVisible)} == {
        "Member Detail", "{{inputs.member_id}}"
    }
    read = capability.steps[4]
    assert isinstance(read, ExtractStep) and read.output == "savings_balance" and read.parse == "money"
    assert read.target.locators[0].by == "table_cell"
    assert capability.provenance is not None and capability.provenance.source == "discovered"

    # Verified on the spec's second example; both outcomes learned and scoped.
    verification = result.verification  # type: ignore[attr-defined]
    assert verification.status == "succeeded"
    assert verification.outputs == {"savings_balance": {"amount": "318.02", "currency": "USD"}}
    learned = {o.code: o for o in result.outcomes}  # type: ignore[attr-defined]
    assert learned["MEMBER_NOT_FOUND"].status == "learned"
    assert learned["ACCESS_RESTRICTED"].status == "learned"
    assert capability.outcomes["MEMBER_NOT_FOUND"].after_step == capability.steps[2].id
    assert capability.outcomes["ACCESS_RESTRICTED"].after_step == capability.steps[3].id

    # The compiled artifact replays like the reference: success and both outcomes.
    for member, status, code in [("100234", "succeeded", None), ("999999", "business_outcome", "MEMBER_NOT_FOUND"),
                                 ("100900", "business_outcome", "ACCESS_RESTRICTED")]:
        run = await run_balance(discovery_workspace, browser, member)
        assert run.status == status, run.error
        assert (run.outcome.code if run.outcome else None) == code

    # No literal example values or PII in the artifact, the cassette, or any evidence file.
    run_dir = discovery_workspace.runs / result.run_id  # type: ignore[attr-defined]
    summary = json.loads((run_dir / "discovery.json").read_text())
    assert summary["status"] == "compiled" and summary["planner"] == "cassette"
    files = [Path(result.capability_path)] + [  # type: ignore[attr-defined]
        p for p in run_dir.rglob("*") if p.suffix in (".json", ".jsonl", ".txt")
    ]
    for path in files:
        body = path.read_text()
        for value in ("100234", "100517", "999999", "100900", "1234.56", "Avery Quill", "12 Sample Lane"):
            assert value not in body, f"{value} leaked into {path.name}"
