"""Discovery end to end from recorded decisions (no model): trace -> compile -> verify -> outcomes."""

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
# Recorded from a real `rote discover --live` run, once one exists (see evidence/01-discovery-live/).
LIVE = FIXTURES / "cassettes" / "get_savings_balance.live.json"


@pytest.fixture
def discovery_workspace(workspace: Workspace) -> Workspace:
    shutil.rmtree(workspace.root / "capabilities")  # discovery must produce the artifact itself
    shutil.copytree(REPO / "specs", workspace.root / "specs")
    return workspace


async def discover(workspace: Workspace, browser: Browser, cassette: Path = SCRIPTED) -> object:
    spec = load_spec(workspace.root / "specs" / "get_savings_balance.yaml")
    planner = CassettePlanner(load_cassette(cassette), spec.example(0))
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


@pytest.mark.skipif(not LIVE.exists(), reason="no live discovery cassette yet (see evidence/01-discovery-live/)")
async def test_live_model_decisions_still_compile_to_a_working_capability(
    discovery_workspace: Workspace, browser: Browser, mock: MockServer
) -> None:
    """The real model's recorded decisions, replayed offline against a fresh mock (another seed)."""
    result = await discover(discovery_workspace, browser, LIVE)
    assert result.status == "compiled", result.reason  # type: ignore[attr-defined]
    verification = result.verification  # type: ignore[attr-defined]
    assert verification.status == "succeeded"
    assert verification.outputs == {"savings_balance": {"amount": "318.02", "currency": "USD"}}
    for member, status, code in [("100234", "succeeded", None), ("999999", "business_outcome", "MEMBER_NOT_FOUND"),
                                 ("100900", "business_outcome", "ACCESS_RESTRICTED")]:
        run = await run_balance(discovery_workspace, browser, member)
        assert run.status == status, run.error
        assert (run.outcome.code if run.outcome else None) == code


async def test_discovering_an_irreversible_capability(discovery_workspace: Workspace, browser: Browser,
                                                      mock: MockServer) -> None:
    spec = load_spec(discovery_workspace.root / "specs" / "open_sub_account.yaml")
    cassette = load_cassette(FIXTURES / "cassettes" / "open_sub_account.scripted.json")
    agent = DiscoveryAgent(discovery_workspace, spec, CassettePlanner(cassette, spec.example(0)), DiscoveryOptions(),
                           browser=browser)
    result = await agent.run()
    assert result.status == "compiled", result.reason
    capability = result.capability
    assert capability.side_effects == "irreversible"
    assert capability.preview_outputs == ["review_share_type", "review_deposit", "review_funding_account"]
    confirm = next(s for s in capability.steps if s.effect == "irreversible")
    assert confirm.target.locators[0].model_dump(exclude_none=True) == {"by": "role", "role": "button",
                                                                        "name": "Confirm", }
    funding = next(s for s in capability.steps if getattr(s, "option", None) and "funding" in s.option)
    assert funding.option == "{{inputs.funding_suffix}}"  # bound by value, not "S00 - Primary Savings"

    # Discovery committed once (with the recorded approval); verification only previewed.
    assert mock.state()["harbor"]["commits"] == 1
    assert result.verification.status == "preview"
    assert result.verification.preview.values["review_share_type"] == "Regular Savings"
    assert result.verification.preview.values["review_funding_account"] == "S10 - Everyday Checking"

    # Two negative examples with one code: a single outcome whose detector covers both messages.
    rejected = capability.outcomes["VALIDATION_REJECTED"]
    assert rejected.after_step == next(s.id for s in capability.steps if getattr(s, "expect", None) is not None
                                       and "review" in str(s.expect.model_dump()))
    assert len(rejected.when.any) == 2
