"""Conditions: the checkpoint vocabulary shared by step postconditions, success
checks, business-outcome detectors and product-level interrupt detectors.

Each condition kind is a one-key mapping so the YAML reads naturally, e.g.
``{text_visible: "Member Detail", frame: main}`` or ``{all: [...]}``.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

from rote.schema.target import Target


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TextVisible(_Model):
    """Text is rendered and visible (``display:none`` / ``visibility:hidden`` text does not count).

    ``frame`` omitted means any frame. Templates such as ``{{inputs.member_id}}`` are allowed.
    """

    text_visible: str
    frame: str | None = None


class ElementPresent(_Model):
    element_present: Target


class ElementAbsent(_Model):
    element_absent: Target


class LocationMatches(_Model):
    """The frame's URL path matches a glob. Query strings (session ids, tokens) are ignored."""

    location_matches: str
    frame: str | None = None


class ValueEquals(_Model):
    """A form control holds this value. ``target`` defaults to the step's own target."""

    value_equals: str
    target: Target | None = None


class OutputPresent(_Model):
    output_present: str


class AllOf(_Model):
    all: list[Condition] = Field(min_length=1)


class AnyOf(_Model):
    any: list[Condition] = Field(min_length=1)


class Not(_Model):
    not_: Condition = Field(alias="not")


_TAGS: tuple[str, ...] = (
    "text_visible",
    "element_present",
    "element_absent",
    "location_matches",
    "value_equals",
    "output_present",
    "all",
    "any",
    "not",
)

_CLASS_TAGS: dict[type[BaseModel], str] = {
    TextVisible: "text_visible",
    ElementPresent: "element_present",
    ElementAbsent: "element_absent",
    LocationMatches: "location_matches",
    ValueEquals: "value_equals",
    OutputPresent: "output_present",
    AllOf: "all",
    AnyOf: "any",
    Not: "not",
}


def _condition_tag(value: Any) -> str | None:
    if isinstance(value, dict):
        present = [tag for tag in _TAGS if tag in value or (tag == "not" and "not_" in value)]
        return present[0] if len(present) == 1 else None
    return _CLASS_TAGS.get(type(value))


Condition = Annotated[
    Annotated[TextVisible, Tag("text_visible")]
    | Annotated[ElementPresent, Tag("element_present")]
    | Annotated[ElementAbsent, Tag("element_absent")]
    | Annotated[LocationMatches, Tag("location_matches")]
    | Annotated[ValueEquals, Tag("value_equals")]
    | Annotated[OutputPresent, Tag("output_present")]
    | Annotated[AllOf, Tag("all")]
    | Annotated[AnyOf, Tag("any")]
    | Annotated[Not, Tag("not")],
    Discriminator(
        _condition_tag,
        custom_error_type="invalid_condition",
        custom_error_message="A condition must have exactly one kind key (text_visible, all, not, ...)",
    ),
]

for _model in (AllOf, AnyOf, Not):
    _model.model_rebuild()


def condition_kind(condition: BaseModel) -> str:
    return _CLASS_TAGS[type(condition)]
