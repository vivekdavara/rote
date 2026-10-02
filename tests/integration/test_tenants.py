"""One capability, two institutions running the same vendor product, configured differently.

Harbor runs CoreOne 4.2 with the default labels. Summit runs 4.3, relabels
member search and the member-number field, and requires a Branch. The base
artifact is reused on Summit through a label dictionary (pure relabels) and an
overlay (the structural difference), never re-recorded.
"""

from __future__ import annotations

import shutil

import pytest
import yaml
from playwright.async_api import Browser

from rote.devserver import MockServer
from rote.registry.approvals import record_approval
from rote.registry.store import Workspace

from .conftest import BALANCE, FIXTURES, REPO, run_balance

pytestmark = pytest.mark.integration


@pytest.fixture
def tenants_workspace(workspace: Workspace) -> Workspace:
    shutil.copy(FIXTURES / "artifacts" / "get_savings_balance.discovered.yaml", workspace.capability_path(BALANCE))
    shutil.copytree(REPO / "overlays", workspace.root / "overlays")
    return workspace


def without(workspace: Workspace, *, labels: bool = False, overlay: bool = False) -> None:
    if labels:
        path = workspace.root / "tenants" / "summit.yaml"
        data = yaml.safe_load(path.read_text())
        data["labels"] = {}
        path.write_text(yaml.safe_dump(data))
    if overlay:
        shutil.rmtree(workspace.root / "overlays")


async def test_base_artifact_on_summit_reports_drift_then_fails_at_the_real_difference(
    tenants_workspace: Workspace, browser: Browser, mock: MockServer
) -> None:
    without(tenants_workspace, labels=True, overlay=True)
    result = await run_balance(tenants_workspace, browser, tenant="summit", step_timeout_ms=1500)
    # The relabeled steps survive only on their brittle CSS fallbacks, and the result says so...
    assert [(d.step_id, d.primary_strategy, d.matched_strategy) for d in result.drift] == [
        ("open_member_search", "role", "css"), ("enter_member_number", "label", "css"),
    ]
    # ...then the structural difference (a required Branch) stops the run where it happens.
    assert result.status == "failed" and result.error is not None
    assert result.error.step_id == "open_search" and "Branch is required." in str(result.error.observed)


async def test_relabel_without_a_fallback_gets_a_did_you_mean_hint(workspace: Workspace, browser: Browser,
                                                                   mock: MockServer) -> None:
    path = workspace.root / "tenants" / "summit.yaml"  # the hand-written artifact has role-only locators here
    data = yaml.safe_load(path.read_text())
    data["labels"] = {}
    path.write_text(yaml.safe_dump(data))
    result = await run_balance(workspace, browser, tenant="summit", step_timeout_ms=1500)
    assert result.status == "failed" and result.error is not None
    assert (result.error.code, result.error.step_id) == ("TARGET_NOT_FOUND", "open_search")
    assert result.error.hint is not None and "'Find Member'" in result.error.hint


async def test_labels_alone_get_past_relabeling_but_not_the_structural_difference(
    tenants_workspace: Workspace, browser: Browser, mock: MockServer
) -> None:
    without(tenants_workspace, overlay=True)
    result = await run_balance(tenants_workspace, browser, tenant="summit", step_timeout_ms=1500)
    assert result.status == "failed" and result.error is not None
    assert result.error.step_id == "open_search"
    assert "Branch is required." in str(result.error.observed)


async def test_labels_and_overlay_run_the_same_capability_on_summit(
    tenants_workspace: Workspace, browser: Browser, mock: MockServer
) -> None:
    harbor = await run_balance(tenants_workspace, browser, tenant="harbor")
    summit = await run_balance(tenants_workspace, browser, tenant="summit")
    assert harbor.status == summit.status == "succeeded", summit.error
    assert summit.outputs == harbor.outputs == {"savings_balance": {"amount": "1234.56", "currency": "USD"}}
    assert harbor.overlay_hash is None and summit.overlay_hash is not None
    assert summit.content_hash == harbor.content_hash  # same reviewed base on both tenants
    assert [s.step_id for s in summit.steps] == [
        "open_member_search", "enter_member_number", "choose_branch", "open_search", "open_view",
        "read_savings_balance",
    ]


async def test_an_overlay_may_not_change_the_contract(tenants_workspace: Workspace, browser: Browser,
                                                      mock: MockServer) -> None:
    path = tenants_workspace.root / "overlays" / "summit" / f"{BALANCE}.yaml"
    data = yaml.safe_load(path.read_text())
    data["patches"][0]["new_step"] = {"id": "read_extra", "action": "extract", "output": "extra",
                                      "target": {"frame": "main", "locators": [{"by": "css", "css": "td"}]}}
    path.write_text(yaml.safe_dump(data))
    result = await run_balance(tenants_workspace, browser, tenant="summit")
    assert result.status == "rejected" and result.error is not None and result.error.code == "OVERLAY_INVALID"


async def test_approval_for_the_base_does_not_cover_a_tenant_overlay(tenants_workspace: Workspace,
                                                                     browser: Browser, mock: MockServer) -> None:
    base = tenants_workspace.capability(BALANCE)
    record_approval(tenants_workspace.root, base, reviewer="Reviewer One")  # base, no overlay
    harbor = await run_balance(tenants_workspace, browser, tenant="harbor", require_approval=True)
    summit = await run_balance(tenants_workspace, browser, tenant="summit", require_approval=True)
    assert harbor.status == "succeeded"
    assert summit.status == "rejected" and summit.error is not None and summit.error.code == "NOT_APPROVED"

    overlay = tenants_workspace.overlay(BALANCE, "summit")
    assert overlay is not None
    record_approval(tenants_workspace.root, base, reviewer="Reviewer One", tenant="summit",
                    overlay_hash=overlay.content_hash())
    summit = await run_balance(tenants_workspace, browser, tenant="summit", require_approval=True)
    assert summit.status == "succeeded"
