"""Goal specs: what a discovery run is asked to produce.

The spec names the capability and its contract (typed inputs with synthetic
examples, typed outputs). It also lists negative examples: inputs that should
end in a declared business outcome. Discovery learns the happy path from the
first example, verifies the compiled artifact against the second, and learns
each outcome's on-screen signature from the negative examples.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from rote.schema.capability import IDENT, OUTCOME_CODE, InputSpec, OutputSpec


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpecInput(_Model):
    type: Literal["string", "integer", "decimal", "boolean"] = "string"
    description: str | None = None
    pattern: str | None = None
    enum: list[str] | None = None
    sensitive: str | None = None
    examples: list[str] = Field(min_length=1, description="Synthetic values. The first drives discovery; the second verifies.")

    def contract(self) -> InputSpec:
        return InputSpec(type=self.type, description=self.description, pattern=self.pattern, enum=self.enum,
                         sensitive=self.sensitive)


class NegativeExample(_Model):
    code: str = Field(pattern=OUTCOME_CODE)
    description: str | None = None
    inputs: dict[str, str]


class GoalSpec(_Model):
    capability_id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    summary: str
    goal: str = Field(description="Natural-language goal; {name} placeholders refer to inputs.")
    tenant: str
    product: str = Field(pattern=IDENT)
    product_versions: str
    inputs: dict[str, SpecInput]
    outputs: dict[str, OutputSpec]
    negative_examples: list[NegativeExample] = Field(default_factory=list)

    def example(self, index: int = 0) -> dict[str, str]:
        return {name: spec.examples[min(index, len(spec.examples) - 1)] for name, spec in self.inputs.items()}

    def goal_text(self) -> str:
        return self.goal.format(**{name: f"{{{{inputs.{name}}}}}" for name in self.inputs})


def load_spec(path: str | Path) -> GoalSpec:
    return GoalSpec.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
