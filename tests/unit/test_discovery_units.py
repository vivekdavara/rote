"""Unit tests for discovery helpers: templating, data-free checkpoints, cassette binding."""

from __future__ import annotations

from rote.discovery.cassette import bind, binding_for, templated
from rote.discovery.compiler import _data_free, _discriminates, slug, template_text
from rote.discovery.recorder import ScreenState
from rote.surface.web.observe import FrameView, Observation

INPUTS = {"member_id": "100234"}


def state(main_text: str) -> ScreenState:
    return ScreenState(fingerprint="f", texts={"main": main_text}, headings={}, messages={}, values={})


def test_templating_replaces_input_values() -> None:
    assert template_text("Member 100234 found", INPUTS) == "Member {{inputs.member_id}} found"
    assert templated("100234", INPUTS) == "{{inputs.member_id}}"
    assert templated("View", INPUTS) == "View"


def test_checkpoints_must_be_data_free_except_input_echoes() -> None:
    assert _data_free("Search Results", INPUTS)
    assert _data_free("Member 100234", INPUTS)  # an input echo is allowed
    assert not _data_free("Balance $1,234.56", INPUTS)
    assert not _data_free("Confirmation # 004512", INPUTS)


def test_discrimination_needs_false_before_and_true_after() -> None:
    assert _discriminates("Search Results", state("Member Search"), state("Member Search Search Results"))
    assert not _discriminates("Member Search", state("Member Search"), state("Member Search Search Results"))
    assert not _discriminates("Missing", state("a"), state("b"))


def test_slugs_are_identifiers() -> None:
    assert slug("Member Search") == "member_search"
    assert slug("Account Holder No.") == "account_holder_no"


def element(ref: str, role: str, name: str = "", row: list[str] | None = None, column: str | None = None) -> dict:
    table = {"headers": ["Member #", "Name"], "row": row, "column": column} if row else None
    return {"ref": ref, "role": role, "name": name, "label": None, "table": table}


def test_binding_disambiguates_rows_by_input_value() -> None:
    frames = [FrameView(name="main", url="", path="/search", title="", headings=[], messages=[], text="", elements=[
        element("e1", "link", "View", ["100517", "Jordan Pike"]),
        element("e2", "link", "View", ["100234", "Avery Quill"]),
    ])]
    observation = Observation(frames=frames)
    facts = element("e2", "link", "View", ["100234", "Avery Quill"])
    binding = binding_for(facts, "main", 0, INPUTS)
    assert binding["row_has"] == ["{{inputs.member_id}}"] and "Avery Quill" not in str(binding)
    assert bind(binding, observation, INPUTS) == "e2"
    assert bind(binding, observation, {"member_id": "100517"}) == "e1"
