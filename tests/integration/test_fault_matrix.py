"""The fault matrix: each runtime condition maps to its own status and code.

This is the evidence for "detects and responds deliberately". Business outcomes
return cleanly, recoverable conditions are handled and reported, and hard
failures stop with a specific, debuggable code. Nothing collapses into a generic
failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from playwright.async_api import Browser

from rote.devserver import MockServer
from rote.registry.store import Workspace

from .conftest import run_balance

pytestmark = pytest.mark.integration


@dataclass
class Case:
    name: str
    status: str
    code: str | None = None
    member: str = "100234"
    fault: dict[str, Any] | None = None
    recoveries: list[str] = field(default_factory=list)
    limits: dict[str, int] | None = None
    options: dict[str, Any] = field(default_factory=dict)


CASES = [
    Case("happy path", "succeeded"),
    Case("member not found", "business_outcome", "MEMBER_NOT_FOUND", member="999999"),
    Case("restricted member", "business_outcome", "ACCESS_RESTRICTED", member="100900"),
    Case("malformed input", "rejected", "INVALID_INPUT", member="12ab"),
    Case("known security notice", "succeeded", fault={"fault": "interstitial", "page": "search"},
         recoveries=["KNOWN_INTERSTITIAL"]),
    Case("session expiry", "succeeded", fault={"fault": "session_expire", "page": "results"},
         recoveries=["SESSION_EXPIRED"]),
    Case("transient 503", "succeeded", fault={"fault": "app_unavailable", "page": "results"},
         recoveries=["TRANSIENT_APP_ERROR"]),
    Case("known native dialog", "succeeded", fault={"fault": "js_dialog", "page": "member"},
         recoveries=["KNOWN_DIALOG"]),
    Case("slow load within the cap", "succeeded", fault={"fault": "latency", "page": "results", "ms": 3500},
         recoveries=["SLOW_LOAD"], options={"step_timeout_ms": 1500}),
    Case("slow load beyond the cap", "failed", "LOAD_TIMEOUT", fault={"fault": "latency", "page": "results", "ms": 7000},
         recoveries=["SLOW_LOAD"], limits={"slow_load_cap_ms": 3000}, options={"step_timeout_ms": 1500}),
    Case("server error page", "failed", "APP_ERROR", fault={"fault": "app_error", "page": "results"}),
    Case("unknown modal, unattended", "failed", "UNKNOWN_MODAL", fault={"fault": "unknown_modal", "page": "search"}),
    Case("two Share Savings rows", "failed", "AMBIGUOUS_EXTRACTION", member="100733"),
    Case("unapproved artifact", "rejected", "NOT_APPROVED", options={"require_approval": True}),
]


def edit_policy(workspace: Workspace, tenant: str, **changes: Any) -> None:
    path = workspace.root / "policies" / f"coreone.{tenant}.yaml"
    data = yaml.safe_load(path.read_text())
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
async def test_fault_matrix(case: Case, workspace: Workspace, browser: Browser, mock: MockServer) -> None:
    if case.limits:
        edit_policy(workspace, "harbor", limits=case.limits)
    if case.fault:
        mock.fault(**case.fault)
    result = await run_balance(workspace, browser, case.member, **case.options)

    assert result.status == case.status, (result.error, result.outcome)
    code = result.error.code if result.error else (result.outcome.code if result.outcome else None)
    assert code == case.code
    assert [r.code for r in result.recoveries] == case.recoveries
    if case.status == "failed":
        assert result.error is not None and result.error.message
        assert result.error.step_id is not None or result.error.code in ("APP_ERROR",)
    if case.status == "succeeded":
        assert result.outputs == {"savings_balance": {"amount": "1234.56", "currency": "USD"}}


async def test_unknown_modal_reports_what_covers_the_control(workspace: Workspace, browser: Browser,
                                                             mock: MockServer) -> None:
    mock.fault("unknown_modal", page="search")
    result = await run_balance(workspace, browser)
    assert result.error is not None
    assert result.error.category == "needs_human"
    assert result.error.step_id == "enter_member_id"
    assert "Fraud Alert" in result.error.message
    run_dir = workspace.runs / result.run_id
    assert (run_dir / "failure" / "screenshot.png").exists()
    assert "Fraud Alert" in (run_dir / "failure" / "snapshot.txt").read_text()


async def test_policy_violation_is_rejected_before_touching_the_ui(workspace: Workspace, browser: Browser,
                                                                  mock: MockServer) -> None:
    edit_policy(workspace, "harbor", allowed_actions=["click", "extract"])
    result = await run_balance(workspace, browser)
    assert result.status == "rejected" and result.error is not None
    assert result.error.code == "POLICY_VIOLATION" and "fill" in result.error.message
    assert result.steps == []


async def test_runtime_risk_mismatch_is_blocked(workspace: Workspace, browser: Browser, mock: MockServer) -> None:
    # A tenant rule classifies the Search button as irreversible. The artifact declares
    # it read_only, so the gate refuses to click it rather than trusting the artifact.
    edit_policy(workspace, "harbor", risk_rules=[{"role": "button", "name_pattern": "^Search$", "effect": "irreversible"}])
    result = await run_balance(workspace, browser)
    assert result.status == "failed" and result.error is not None
    assert result.error.code == "POLICY_BLOCKED" and result.error.step_id == "submit_search"


def mutate_artifact(workspace: Workspace, change: Any) -> None:
    path: Path = workspace.capability_path("coreone.member.get_savings_balance")
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


async def test_permission_denied_is_a_configuration_failure(workspace: Workspace, browser: Browser,
                                                            mock: MockServer) -> None:
    def to_admin(data: dict[str, Any]) -> None:
        data["steps"][0]["target"]["locators"][0]["name"] = "Administration"

    mutate_artifact(workspace, to_admin)
    result = await run_balance(workspace, browser)
    assert result.status == "failed" and result.error is not None
    assert result.error.code == "PERMISSION_DENIED"
    assert result.error.category == "hard_failure" and result.error.retryable is False
