"""Edges of the result contract: every run ends in a structured result, approvals bind, drafts don't clobber.

Each test pins a fix from the pre-submission review: a known interrupt whose dismiss button is gone used to
escape as a traceback; attended runs skipped approval silently; re-discovery overwrote the approved file
before verifying the new one.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from playwright.async_api import Browser

from rote.devserver import MockServer
from rote.discovery.agent import DiscoveryAgent, DiscoveryOptions
from rote.discovery.cassette import load_cassette
from rote.discovery.planner import CassettePlanner
from rote.registry.store import Workspace
from rote.replay.engine import ReplayEngine, ReplayOptions, replay
from rote.schema.spec import load_spec

from .conftest import FIXTURES, REPO, run_balance
from .test_commit_protocol import INPUTS as SUB_INPUTS
from .test_commit_protocol import SUB

pytestmark = pytest.mark.integration


async def test_an_interrupt_its_handler_cannot_clear_ends_in_a_structured_result(
    workspace: Workspace, browser: Browser, mock: MockServer
) -> None:
    profile = workspace.root / "apps" / "coreone" / "profile.yaml"
    profile.write_text(profile.read_text().replace("name: Acknowledge}", "name: Acknowledge All}"))
    mock.fault("interstitial", page="search")
    result = await run_balance(workspace, browser)  # unattended: no one to hand the screen to
    assert result.status == "failed" and result.error is not None
    assert result.error.code == "UNKNOWN_MODAL" and result.error.step_id == "open_search"
    assert "KNOWN_INTERSTITIAL" in result.error.message and result.error.evidence


async def test_attended_run_may_trial_an_unapproved_read_only_capability(
    workspace: Workspace, browser: Browser, mock: MockServer
) -> None:
    unattended = await run_balance(workspace, browser, require_approval=True)
    assert unattended.status == "rejected" and unattended.error and unattended.error.code == "NOT_APPROVED"
    trial = await run_balance(workspace, browser, require_approval=True, attended=True)
    assert trial.status == "succeeded"
    assert "not approved: ran as an attended trial" in trial.warnings


async def test_attended_run_still_needs_approval_for_an_irreversible_capability(
    workspace: Workspace, browser: Browser, mock: MockServer
) -> None:
    shutil.copy(FIXTURES / "artifacts" / "open_sub_account.authored.yaml", workspace.capability_path(SUB))
    result = await replay(workspace, SUB, "harbor", SUB_INPUTS,
                          ReplayOptions(mode="preview", attended=True, require_approval=True, step_timeout_ms=4000),
                          browser=browser)
    assert result.status == "rejected" and result.error and result.error.code == "NOT_APPROVED"


async def test_rediscovery_that_fails_verification_leaves_the_registry_alone(
    workspace: Workspace, browser: Browser, mock: MockServer
) -> None:
    approved = workspace.capability_path("coreone.member.get_savings_balance")
    before = approved.read_text()
    spec_path = workspace.root / "specs" / "get_savings_balance.yaml"
    shutil.copytree(REPO / "specs", workspace.root / "specs")
    spec_data = yaml.safe_load(spec_path.read_text())
    spec_data["inputs"]["member_id"]["examples"] = ["100234", "999999"]  # verification will meet "not found"
    spec_path.write_text(yaml.safe_dump(spec_data, sort_keys=False))
    spec = load_spec(spec_path)
    planner = CassettePlanner(load_cassette(FIXTURES / "cassettes" / "get_savings_balance.scripted.json"),
                              spec.example(0))
    result = await DiscoveryAgent(workspace, spec, planner, DiscoveryOptions(), browser=browser).run()
    assert result.status != "compiled" and result.reason.startswith("VERIFY_FAILED")
    assert approved.read_text() == before  # the approved capability is untouched
    draft = Path(result.capability_path)
    assert draft.name == "artifact.draft.yaml" and draft.parent == workspace.runs / result.run_id


async def test_a_bug_in_rote_itself_still_returns_a_structured_result(
    workspace: Workspace, browser: Browser, mock: MockServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: ReplayEngine) -> None:
        raise RuntimeError("member 100234 broke something")  # the message carries an input: it must be redacted

    monkeypatch.setattr(ReplayEngine, "_execute", broken)
    result = await run_balance(workspace, browser)
    assert result.status == "failed" and result.error is not None and result.error.code == "INTERNAL_ERROR"
    assert "100234" not in result.error.message
    traceback = (workspace.runs / result.run_id / result.error.evidence[0]).read_text()  # paths are run-relative
    assert "RuntimeError" in traceback and "100234" not in traceback
