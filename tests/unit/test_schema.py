from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from rote.cli import SCHEMA_PATH, capability_json_schema
from rote.registry.approvals import find_approval, load_approvals, record_approval
from rote.schema.capability import Capability, dump_capability, load_capability
from rote.schema.templating import TemplateError, render

FIXTURE = Path(__file__).parents[1] / "fixtures" / "artifacts" / "get_savings_balance.authored.yaml"


def raw() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    return data


def build(data: dict[str, Any]) -> Capability:
    return Capability.model_validate(data)


def test_reference_artifact_validates() -> None:
    capability = load_capability(FIXTURE)
    assert capability.id == "coreone.member.get_savings_balance"
    assert [s.id for s in capability.steps] == [
        "open_search",
        "enter_member_id",
        "submit_search",
        "open_member",
        "read_balance",
    ]
    assert capability.side_effects == "none"
    assert capability.outcomes["MEMBER_NOT_FOUND"].after_step == "submit_search"


def test_yaml_round_trip_preserves_identity() -> None:
    capability = load_capability(FIXTURE)
    again = Capability.model_validate(yaml.safe_load(dump_capability(capability)))
    assert again == capability
    assert again.content_hash() == capability.content_hash()


def test_hash_ignores_provenance_but_not_behavior() -> None:
    base = build(raw())
    relabeled = raw()
    relabeled["provenance"] = {"source": "discovered", "run_id": "run-123"}
    assert build(relabeled).content_hash() == base.content_hash()

    edited = raw()
    edited["steps"][2]["target"]["locators"][0]["name"] = "Find"
    assert build(edited).content_hash() != base.content_hash()


def test_contract_hash_ignores_procedure_changes() -> None:
    base = build(raw())
    procedure_only = raw()
    procedure_only["steps"][0]["target"]["locators"][0]["name"] = "Find Member"
    assert build(procedure_only).contract_hash() == base.contract_hash()

    new_input = raw()
    new_input["inputs"]["branch"] = {"type": "string"}
    assert build(new_input).contract_hash() != base.contract_hash()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["steps"].append(copy.deepcopy(d["steps"][0])), "duplicate step ids"),
        (lambda d: d["steps"][1].update(value="{{inputs.account}}"), "undeclared input"),
        (lambda d: d["steps"][1].update(value="{{secrets.coreone.password}}"), "may not reference secrets"),
        (lambda d: d["steps"][2].update(effect="irreversible"), "side_effects"),
        (lambda d: d["outputs"].update(extra={"type": "string"}), "exactly one step"),
        (lambda d: d["outcomes"]["MEMBER_NOT_FOUND"].update(after_step="nope"), "unknown step"),
        (lambda d: d["outcomes"].update(not_upper={"when": {"text_visible": "x"}}), "UPPER_SNAKE"),
        (lambda d: d.update(preview_outputs=["savings_balance"]), "before the first irreversible step"),
    ],
)
def test_inconsistent_artifacts_are_rejected(mutate: Any, message: str) -> None:
    data = raw()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        build(data)


def test_condition_needs_exactly_one_kind() -> None:
    data = raw()
    data["success"] = {"text_visible": "Member Detail", "output_present": "savings_balance"}
    with pytest.raises(ValidationError, match="exactly one kind key"):
        build(data)


def test_table_cell_needs_one_destination() -> None:
    data = raw()
    locator = data["steps"][4]["target"]["locators"][0]
    locator["then"] = {"role": "link", "name": "View"}
    with pytest.raises(ValidationError, match="exactly one of 'column' or 'then'"):
        build(data)


def test_templating() -> None:
    context = {"inputs": {"member_id": "100234"}, "tenant": {"default_branch": "Main"}}
    assert render("Member {{ inputs.member_id }} at {{tenant.default_branch}}", context) == "Member 100234 at Main"
    with pytest.raises(TemplateError, match="no value"):
        render("{{inputs.missing}}", context)
    with pytest.raises(TemplateError, match="unknown template namespace"):
        render("{{env.HOME}}", context)


def test_committed_json_schema_is_current() -> None:
    assert SCHEMA_PATH.read_text(encoding="utf-8") == capability_json_schema(), "run `rote schema`"


def test_approval_binds_to_exact_content(tmp_path: Path) -> None:
    capability = build(raw())
    assert find_approval(load_approvals(tmp_path, capability.id), capability, tenant="harbor", overlay_hash=None) is None

    record_approval(tmp_path, capability, reviewer="Reviewer One")
    approvals = load_approvals(tmp_path, capability.id)
    assert find_approval(approvals, capability, tenant="harbor", overlay_hash=None) is not None
    assert find_approval(approvals, capability, tenant="summit", overlay_hash="sha256:overlay") is None

    edited = raw()
    edited["steps"][0]["intent"] = "Open the search page"
    assert find_approval(approvals, build(edited), tenant="harbor", overlay_hash=None) is None
