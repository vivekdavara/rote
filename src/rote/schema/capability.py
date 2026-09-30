"""The capability artifact: what discovery emits, what reviewers approve, and what
replay executes.

One file, two layers:

* the **contract** (``id``, ``version``, ``inputs``, ``outputs``, ``outcomes``,
  ``side_effects``): what a calling agent or a reviewer needs to know
* the **procedure** (``steps``, ``success``): how replay does it

Contract changes bump the major version and procedure-only changes bump the minor
version. Approval state is not stored here: approvals live beside the artifact
and bind to :meth:`Capability.content_hash`, so approving never changes the
artifact's identity and any edit voids the approval.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from rote.schema.condition import Condition
from rote.schema.hashing import sha256_hex
from rote.schema.target import Target
from rote.schema.templating import iter_strings, references

SEMVER = r"^\d+\.\d+\.\d+$"
IDENT = r"^[a-z][a-z0-9_]*$"
OUTCOME_CODE = r"^[A-Z][A-Z0-9_]*$"

Effect = Literal["read_only", "reversible", "irreversible"]
SideEffects = Literal["none", "reversible", "irreversible"]
ParseAs = Literal["string", "integer", "decimal", "money", "date", "boolean"]

_EFFECT_RANK: dict[str, int] = {"read_only": 0, "reversible": 1, "irreversible": 2}
_SIDE_EFFECTS_BY_RANK: dict[int, SideEffects] = {0: "none", 1: "reversible", 2: "irreversible"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- steps


class _StepBase(_Model):
    id: str = Field(pattern=IDENT)
    intent: str | None = None
    effect: Effect = "read_only"
    expect: Condition | None = Field(
        default=None, description="Postcondition checked after the action; must discriminate before/after."
    )
    timeout_ms: int | None = Field(default=None, gt=0)
    provenance: Literal["authored", "llm", "human"] = "authored"


class ClickStep(_StepBase):
    action: Literal["click"] = "click"
    target: Target


class FillStep(_StepBase):
    action: Literal["fill"] = "fill"
    target: Target
    value: str


class SelectStep(_StepBase):
    action: Literal["select"] = "select"
    target: Target
    option: str = Field(description="Visible option label; may be a template.")


class CheckStep(_StepBase):
    action: Literal["check"] = "check"
    target: Target
    checked: bool = True


class PressStep(_StepBase):
    action: Literal["press"] = "press"
    key: str
    target: Target | None = None


class ExtractStep(_StepBase):
    action: Literal["extract"] = "extract"
    target: Target
    output: str = Field(pattern=IDENT)
    parse: ParseAs = "string"


Step = Annotated[
    ClickStep | FillStep | SelectStep | CheckStep | PressStep | ExtractStep,
    Field(discriminator="action"),
]
TargetedStep = ClickStep | FillStep | SelectStep | CheckStep | ExtractStep


# ------------------------------------------------------------------------ contract


class InputSpec(_Model):
    type: Literal["string", "integer", "decimal", "boolean"] = "string"
    description: str | None = None
    pattern: str | None = None
    enum: list[str] | None = None
    sensitive: str | None = Field(
        default=None, description="PII class (e.g. member_id). Sensitive values are pseudonymized in logs."
    )


class OutputSpec(_Model):
    type: ParseAs = "string"
    description: str | None = None
    cardinality: Literal["one", "many"] = "one"
    sensitive: str | None = None


class OutcomeSpec(_Model):
    """An expected business result the caller must handle, e.g. MEMBER_NOT_FOUND.

    Part of the contract, not an exception. ``after_step`` scopes the detector to
    where the outcome can legitimately occur.
    """

    description: str | None = None
    after_step: str | None = None
    when: Condition


class ProductRef(_Model):
    name: str = Field(pattern=IDENT)
    versions: str = Field(description="Compatible product versions, e.g. '>=4.2,<5'.")


class ProvenanceInfo(_Model):
    source: Literal["authored", "discovered"] = "authored"
    run_id: str | None = None
    model: str | None = None
    served_by: list[str] | None = None
    recorded_on: str | None = None
    recorded_at: datetime | None = None
    note: str | None = None


# ---------------------------------------------------------------------- capability


class Capability(_Model):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    version: str = Field(pattern=SEMVER)
    summary: str
    product: ProductRef
    surface: Literal["web", "desktop"] = "web"
    side_effects: SideEffects = "none"
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    preview_outputs: list[str] = Field(
        default_factory=list,
        description="Outputs extracted before the first irreversible step; returned by preview mode.",
    )
    outcomes: dict[str, OutcomeSpec] = Field(default_factory=dict)
    requires: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    success: Condition
    provenance: ProvenanceInfo | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Capability:
        problems: list[str] = []
        step_ids = [step.id for step in self.steps]
        duplicates = sorted({sid for sid in step_ids if step_ids.count(sid) > 1})
        if duplicates:
            problems.append(f"duplicate step ids: {duplicates}")

        for name in list(self.inputs) + list(self.outputs):
            if not _matches(IDENT, name):
                problems.append(f"input/output names must be identifiers: {name!r}")
        for code, outcome in self.outcomes.items():
            if not _matches(OUTCOME_CODE, code):
                problems.append(f"outcome codes must be UPPER_SNAKE: {code!r}")
            if outcome.after_step is not None and outcome.after_step not in step_ids:
                problems.append(f"outcome {code} scoped to unknown step {outcome.after_step!r}")

        extracted = [step.output for step in self.steps if isinstance(step, ExtractStep)]
        for output in extracted:
            if output not in self.outputs:
                problems.append(f"step extracts undeclared output {output!r}")
        for output in self.outputs:
            count = extracted.count(output)
            if count != 1:
                problems.append(f"output {output!r} must be extracted by exactly one step (found {count})")

        highest = max(_EFFECT_RANK[step.effect] for step in self.steps)
        if self.side_effects != _SIDE_EFFECTS_BY_RANK[highest]:
            problems.append(
                f"side_effects is {self.side_effects!r} but the steps imply {_SIDE_EFFECTS_BY_RANK[highest]!r}"
            )

        first_irreversible = self.first_irreversible_index()
        for output in self.preview_outputs:
            if output not in self.outputs:
                problems.append(f"preview output {output!r} is not a declared output")
                continue
            index = next(
                (i for i, s in enumerate(self.steps) if isinstance(s, ExtractStep) and s.output == output), None
            )
            if first_irreversible is None or index is None or index > first_irreversible:
                problems.append(f"preview output {output!r} must be extracted before the first irreversible step")

        dumped = self.model_dump(mode="json", by_alias=True, exclude={"provenance"})
        for text in iter_strings(dumped):
            for namespace, name in references(text):
                if namespace == "secrets":
                    problems.append("capabilities may not reference secrets; login lives in the product profile")
                elif namespace == "inputs" and name not in self.inputs:
                    problems.append(f"template references undeclared input {name!r}")
                elif namespace not in ("inputs", "tenant"):
                    problems.append(f"unknown template namespace {namespace!r}")

        if problems:
            raise ValueError("; ".join(dict.fromkeys(problems)))
        return self

    # -- structure helpers ----------------------------------------------------

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def first_irreversible_index(self) -> int | None:
        return next((i for i, s in enumerate(self.steps) if s.effect == "irreversible"), None)

    # -- identity -------------------------------------------------------------

    def canonical(self) -> dict[str, Any]:
        """Everything that affects behavior or what a reviewer approved. Provenance is excluded."""
        data: dict[str, Any] = self.model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude={"provenance"}
        )
        return data

    def content_hash(self) -> str:
        return sha256_hex(self.canonical())

    def contract(self) -> dict[str, Any]:
        """The caller-facing surface. Tenant overlays must leave this unchanged."""
        return {
            "id": self.id,
            "major": self.version.split(".")[0],
            "side_effects": self.side_effects,
            "inputs": {k: v.model_dump(mode="json", exclude_none=True) for k, v in self.inputs.items()},
            "outputs": {k: v.model_dump(mode="json", exclude_none=True) for k, v in self.outputs.items()},
            "preview_outputs": sorted(self.preview_outputs),
            "outcomes": sorted(self.outcomes),
            "requires": sorted(self.requires),
        }

    def contract_hash(self) -> str:
        return sha256_hex(self.contract())


def _matches(pattern: str, value: str) -> bool:
    return re.fullmatch(pattern, value) is not None


# ---------------------------------------------------------------------------- I/O


def load_capability(path: str | Path) -> Capability:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Capability.model_validate(data)


def dump_capability(capability: Capability) -> str:
    data = capability.model_dump(mode="json", by_alias=True, exclude_none=True)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)


def save_capability(capability: Capability, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_capability(capability), encoding="utf-8")
    return target
