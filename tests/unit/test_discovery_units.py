"""Unit tests for discovery helpers: templating, data-free checkpoints, fallback postconditions, binding."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rote.discovery.cassette import bind, binding_for, templated
from rote.discovery.compiler import _data_free, _discriminates, _fresh_ui_text, slug, template_text, versioned
from rote.discovery.recorder import ScreenState
from rote.schema.capability import load_capability
from rote.surface.web.observe import FrameView, Observation

INPUTS = {"member_id": "100234"}
AUTHORED = Path(__file__).parents[1] / "fixtures" / "artifacts" / "get_savings_balance.authored.yaml"


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


def screen(main: str, *, headings: Sequence[str] = (), labels: Sequence[str] = (), columns: Sequence[str] = ()) -> ScreenState:
    return ScreenState(fingerprint="f", texts={"nav": "Member Search", "main": main}, headings={"main": list(headings)},
                       messages={}, values={}, labels={"main": list(labels)}, columns={"main": list(columns)})


HOME = screen("Main Menu Bulletins", headings=["Main Menu"])
SEARCH = screen("Member Search Member Number: Last Name: Search", headings=["Member Search"],
                labels=["Member Number", "Last Name"])
RESULTS = screen("Member Search Member Number: Last Name: Search Results Member # Name 100234 Avery Quill",
                 headings=["Member Search"], labels=["Member Number", "Last Name"], columns=["Member #", "Name"])


def test_fallback_uses_a_new_field_label_when_the_heading_was_already_visible() -> None:
    # "Member Search" heads the new page but was already on screen as the nav link.
    assert _fresh_ui_text(HOME, SEARCH, INPUTS) == ("main", "Member Number")


def test_fallback_uses_a_new_column_title_and_never_a_value() -> None:
    assert _fresh_ui_text(SEARCH, RESULTS, INPUTS) == ("main", "Member #")


def test_fallback_prefers_a_new_heading_and_skips_data() -> None:
    detail = screen("Member Detail Balance $1,234.56", headings=["Member Detail"], columns=["Balance $1,234.56"])
    assert _fresh_ui_text(RESULTS, detail, INPUTS) == ("main", "Member Detail")
    no_heading = screen("Balance $1,234.56", columns=["Balance $1,234.56"])
    assert _fresh_ui_text(RESULTS, no_heading, INPUTS) is None


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


def test_rediscovery_versions_against_the_capability_it_replaces() -> None:
    previous = load_capability(AUTHORED).model_copy(update={"version": "1.2.0"})
    draft = previous.model_copy(update={"version": "0.1.0"})
    assert versioned(draft, None).version == "0.1.0"  # a first discovery is a 0.x draft

    same = versioned(draft, previous)  # nothing changed: same version, so the approval still matches
    assert same.version == "1.2.0" and same.content_hash() == previous.content_hash()

    steps = [s.model_copy(update={"intent": "Open the search screen."}) if i == 0 else s
             for i, s in enumerate(draft.steps)]
    assert versioned(draft.model_copy(update={"steps": steps}), previous).version == "1.3.0"  # procedure

    inputs = {"member_id": draft.inputs["member_id"].model_copy(update={"pattern": "^[0-9]{7}$"})}
    assert versioned(draft.model_copy(update={"inputs": inputs}), previous).version == "2.0.0"  # contract
