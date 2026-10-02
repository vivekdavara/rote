from __future__ import annotations

from pathlib import Path

import pytest

from rote.schema.capability import load_capability
from rote.schema.overlay import Overlay, OverlayError, apply_overlay, load_overlay

REPO = Path(__file__).parents[2]
BASE = load_capability(REPO / "tests" / "fixtures" / "artifacts" / "get_savings_balance.discovered.yaml")
SUMMIT = load_overlay(REPO / "overlays" / "summit" / "coreone.member.get_savings_balance.yaml")


def test_overlay_inserts_a_step_and_keeps_the_contract() -> None:
    effective = apply_overlay(BASE, SUMMIT)
    ids = [s.id for s in effective.steps]
    assert ids.index("choose_branch") == ids.index("enter_member_number") + 1
    assert effective.contract_hash() == BASE.contract_hash()
    assert effective.content_hash() != BASE.content_hash()


def test_overlay_hash_changes_with_any_edit() -> None:
    edited = SUMMIT.model_copy(update={"reason": "edited"})
    assert edited.content_hash() != SUMMIT.content_hash()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"overlay_for": "coreone.member.other"}, "not 'coreone.member.get_savings_balance'"),
        ({"base_major": 7}, "major version 7"),
    ],
)
def test_overlay_must_target_this_base(change: dict[str, object], message: str) -> None:
    with pytest.raises(OverlayError, match=message):
        apply_overlay(BASE, SUMMIT.model_copy(update=change))


def test_patch_naming_a_missing_step_is_refused() -> None:
    data = SUMMIT.model_dump(mode="json", by_alias=True, exclude_none=True)
    data["patches"][0]["step"] = "enter_member_id"
    with pytest.raises(OverlayError, match="no longer has"):
        apply_overlay(BASE, Overlay.model_validate(data))


def test_removing_a_step_an_outcome_depends_on_is_refused() -> None:
    data = SUMMIT.model_dump(mode="json", by_alias=True, exclude_none=True)
    data["patches"] = [{"op": "remove_step", "step": "open_search"}]  # MEMBER_NOT_FOUND is scoped to it
    with pytest.raises(OverlayError, match="invalid"):
        apply_overlay(BASE, Overlay.model_validate(data))
