"""Irreversible capabilities: preview -> commit token -> idempotent commit, and every way it refuses."""

from __future__ import annotations

import shutil
from typing import Any

import pytest
from playwright.async_api import Browser

from rote.devserver import MockServer
from rote.registry.store import Workspace
from rote.replay.engine import ReplayOptions, replay
from rote.schema.result import RunResult

from .conftest import FIXTURES

pytestmark = pytest.mark.integration

SUB = "coreone.member.open_sub_account"
INPUTS = {"member_id": "100234", "share_type": "Holiday Club", "nickname": "Holiday 2026", "deposit": "25.00",
          "funding_suffix": "S00"}


@pytest.fixture
def sub_workspace(workspace: Workspace) -> Workspace:
    shutil.copy(FIXTURES / "artifacts" / "open_sub_account.authored.yaml", workspace.capability_path(SUB))
    return workspace


async def run(workspace: Workspace, browser: Browser, inputs: dict[str, str] | None = None, **options: Any) -> RunResult:
    options.setdefault("require_approval", False)
    options.setdefault("step_timeout_ms", 4000)
    return await replay(workspace, SUB, "harbor", inputs or INPUTS, ReplayOptions(**options), browser=browser)


async def test_preview_then_commit_then_idempotent_repeat(sub_workspace: Workspace, browser: Browser,
                                                          mock: MockServer) -> None:
    preview = await run(sub_workspace, browser, mode="preview")
    assert preview.status == "preview", preview.error
    assert preview.preview is not None and preview.commit_state == "none"
    assert preview.preview.values == {
        "review_share_type": "Holiday Club",
        "review_deposit": {"amount": "25.00", "currency": "USD"},
        "review_funding_account": "S00 - Primary Savings",
    }
    assert mock.state()["harbor"]["commits"] == 0  # a preview never commits

    token = preview.preview.commit_token
    committed = await run(sub_workspace, browser, mode="commit", commit_token=token, idempotency_key="req-1")
    assert committed.status == "succeeded", committed.error
    assert committed.commit_state == "committed"
    assert committed.outputs is not None and committed.outputs["confirmation_number"].isdigit()
    assert mock.state()["harbor"]["commits"] == 1

    again = await run(sub_workspace, browser, mode="commit", commit_token=token, idempotency_key="req-1")
    assert again.idempotent_replay is True
    assert again.status == "succeeded" and again.outputs == committed.outputs
    assert mock.state()["harbor"]["commits"] == 1  # the repeat did not commit again


async def test_plain_run_of_an_irreversible_capability_only_previews(sub_workspace: Workspace, browser: Browser,
                                                                    mock: MockServer) -> None:
    result = await run(sub_workspace, browser)
    assert result.status == "preview" and result.warnings
    assert mock.state()["harbor"]["commits"] == 0


@pytest.mark.parametrize(
    ("token", "key", "inputs", "code"),
    [
        (None, "req-x", INPUTS, "COMMIT_TOKEN_REQUIRED"),
        ("valid", None, INPUTS, "IDEMPOTENCY_KEY_REQUIRED"),
        ("tampered", "req-x", INPUTS, "COMMIT_TOKEN_INVALID"),
        ("valid", "req-x", {**INPUTS, "deposit": "40.00"}, "COMMIT_TOKEN_INVALID"),
    ],
    ids=["no token", "no idempotency key", "tampered token", "inputs changed after preview"],
)
async def test_commit_refusals_touch_nothing(token: str | None, key: str | None, inputs: dict[str, str], code: str,
                                             sub_workspace: Workspace, browser: Browser, mock: MockServer) -> None:
    preview = await run(sub_workspace, browser, mode="preview")
    assert preview.preview is not None
    real = preview.preview.commit_token
    chosen = {"valid": real, "tampered": real[:-4] + ("AAAA" if not real.endswith("AAAA") else "BBBB"),
              None: None}[token]
    result = await run(sub_workspace, browser, inputs, mode="commit", commit_token=chosen, idempotency_key=key)
    assert result.status == "rejected" and result.error is not None and result.error.code == code
    assert mock.state()["harbor"]["commits"] == 0


async def test_review_screen_changed_since_preview(sub_workspace: Workspace, browser: Browser,
                                                   mock: MockServer) -> None:
    preview = await run(sub_workspace, browser, mode="preview")
    assert preview.preview is not None
    mock.fault("review_drift")  # the review now shows something the member never approved
    result = await run(sub_workspace, browser, mode="commit", commit_token=preview.preview.commit_token,
                       idempotency_key="req-drift")
    assert result.status == "failed" and result.error is not None and result.error.code == "PREVIEW_MISMATCH"
    assert result.commit_state == "none"
    assert mock.state()["harbor"]["commits"] == 0


async def test_unconfirmed_commit_is_indeterminate_and_never_retried(sub_workspace: Workspace, browser: Browser,
                                                                     mock: MockServer) -> None:
    from .test_fault_matrix import edit_policy

    edit_policy(sub_workspace, "harbor", limits={"slow_load_cap_ms": 4000})
    preview = await run(sub_workspace, browser, mode="preview")
    assert preview.preview is not None
    mock.fault("confirm_timeout", ms=20_000)  # CoreOne commits, then the confirmation page stalls past the cap
    token = preview.preview.commit_token
    result = await run(sub_workspace, browser, mode="commit", commit_token=token, idempotency_key="req-stall",
                       step_timeout_ms=2500)
    assert result.status == "failed" and result.error is not None
    assert result.error.code == "INDETERMINATE_COMMIT" and result.error.retryable is False
    assert result.commit_state == "unknown"
    assert mock.state()["harbor"]["commits"] == 1

    again = await run(sub_workspace, browser, mode="commit", commit_token=token, idempotency_key="req-stall")
    assert again.idempotent_replay is True and again.commit_state == "unknown"
    assert mock.state()["harbor"]["commits"] == 1  # never retried


async def test_validation_rejection_is_a_business_outcome_with_the_apps_message(
    sub_workspace: Workspace, browser: Browser, mock: MockServer
) -> None:
    result = await run(sub_workspace, browser, {**INPUTS, "deposit": "1.00"}, mode="preview")
    assert result.status == "business_outcome" and result.outcome is not None
    assert result.outcome.code == "VALIDATION_REJECTED"
    assert result.outcome.messages == ["Initial deposit must be at least $5.00."]
