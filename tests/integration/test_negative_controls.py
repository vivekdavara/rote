"""Negative controls: deliberately broken artifacts must fail, and fail with the right code.

A replay that passes proves little unless the same checks demonstrably fail when
they should. The same idea keeps an eval suite honest: grade it against a
fixture that must go red.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.async_api import Browser

from rote.devserver import MockServer
from rote.registry.store import Workspace

from .conftest import run_balance
from .test_fault_matrix import mutate_artifact

pytestmark = pytest.mark.integration


def wrong_postcondition(data: dict[str, Any]) -> None:
    data["steps"][2]["expect"] = {"text_visible": "Search Outcome", "frame": "main"}


def missing_control(data: dict[str, Any]) -> None:
    data["steps"][2]["target"]["locators"][0]["name"] = "Serch"


def removed_step(data: dict[str, Any]) -> None:
    del data["steps"][1]  # never types the member number


def wrong_member_typed(data: dict[str, Any]) -> None:
    # Types a hard-coded member number: the fill step's value check catches it immediately.
    data["steps"][1]["value"] = "100517"
    data["steps"][3]["target"]["locators"][0]["row"] = {"Member #": "100517"}


def wrong_member_reached(data: dict[str, Any]) -> None:
    # Same, with the value check removed. The input-bound checkpoint on Member Detail
    # ({{inputs.member_id}} must be visible) is the second line of defense.
    wrong_member_typed(data)
    del data["steps"][1]["expect"]


MUTANTS = [
    ("postcondition that never holds", wrong_postcondition, "UNEXPECTED_STATE", "submit_search"),
    ("locator to a missing control", missing_control, "TARGET_NOT_FOUND", "submit_search"),
    ("step removed", removed_step, "UNEXPECTED_STATE", "submit_search"),
    ("wrong member typed", wrong_member_typed, "UNEXPECTED_STATE", "enter_member_id"),
    ("wrong member reached", wrong_member_reached, "UNEXPECTED_STATE", "open_member"),
]


@pytest.mark.parametrize(("name", "mutate", "code", "step"), MUTANTS, ids=[m[0] for m in MUTANTS])
async def test_mutant_fails_with_specific_code(name: str, mutate: Any, code: str, step: str,
                                               workspace: Workspace, browser: Browser, mock: MockServer) -> None:
    mutate_artifact(workspace, mutate)
    result = await run_balance(workspace, browser, step_timeout_ms=2000)
    assert result.status == "failed", f"{name}: mutant was not caught"
    assert result.error is not None
    assert (result.error.code, result.error.step_id) == (code, step)


async def test_missing_control_gets_a_did_you_mean_hint(workspace: Workspace, browser: Browser,
                                                        mock: MockServer) -> None:
    mutate_artifact(workspace, missing_control)
    result = await run_balance(workspace, browser, step_timeout_ms=1500)
    assert result.error is not None and result.error.hint is not None
    assert "'Search'" in result.error.hint
