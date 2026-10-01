"""Cassettes: a discovery run's decisions, recorded so discovery can be replayed offline.

A cassette stores each decision (tool and arguments), a *binding* for its
target, the screen fingerprint, the serving model, and any operator approval.
It never stores raw screen text or refs. The binding is the element's
durable facts: frame, role, name, label, table column, and the row key, with
input values written as templates. At playback the binding is matched against
the new observation.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from rote.schema.templating import render
from rote.surface.web.observe import Observation


class CassetteEntry(BaseModel):
    step: int
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] | None = None
    fingerprint: str | None = None
    served_by: str | None = None
    approval: Literal["approved", "rejected"] | None = None


class Cassette(BaseModel):
    version: Literal[1] = 1
    capability_id: str
    tenant: str
    model: str | None = None
    recorded_at: datetime
    source: str = Field(description="Where the decisions came from: a live model run, or a hand-written script.")
    entries: list[CassetteEntry]

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path


def load_cassette(path: str | Path) -> Cassette:
    return Cassette.model_validate_json(Path(path).read_text(encoding="utf-8"))


def templated(value: str | None, inputs: dict[str, str]) -> str | None:
    """Replace input example values with placeholders (whole-value match)."""
    if value is None:
        return None
    for name, example in inputs.items():
        if example and value == example:
            return f"{{{{inputs.{name}}}}}"
    return value


def binding_for(facts: dict[str, Any], frame: str | None, nth: int, inputs: dict[str, str]) -> dict[str, Any]:
    binding: dict[str, Any] = {"frame": frame, "role": facts["role"]}
    if facts.get("name"):
        binding["name"] = templated(facts["name"], inputs)
    label = (facts.get("label") or {}).get("text")
    if label and not facts.get("name"):
        binding["label"] = templated(label, inputs)
    table = facts.get("table") or {}
    if table.get("column"):
        binding["column"] = table["column"]
    row_inputs = [templated(v, inputs) for v in table.get("row") or []]
    keys = [v for v in row_inputs if v and v.startswith("{{inputs.")]
    if keys:
        binding["row_has"] = keys
    elif table.get("row") and facts["role"] == "cell":
        binding["row_first"] = table["row"][0]  # e.g. "Share Savings": app vocabulary, not PII
    binding["nth"] = nth
    return binding


def _matches(element: dict[str, Any], binding: dict[str, Any], frame: str | None, inputs: dict[str, str]) -> bool:
    context = {"inputs": inputs}
    if frame != binding.get("frame") or element["role"] != binding["role"]:
        return False
    if "name" in binding and element.get("name") != render(binding["name"], context):
        return False
    if "label" in binding and (element.get("label") or {}).get("text") != render(binding["label"], context):
        return False
    table = element.get("table") or {}
    if "column" in binding and table.get("column") != binding["column"]:
        return False
    row = table.get("row") or []
    if "row_has" in binding and not all(render(k, context) in row for k in binding["row_has"]):
        return False
    return not ("row_first" in binding and (not row or row[0] != binding["row_first"]))


def bind(binding: dict[str, Any], observation: Observation, inputs: dict[str, str]) -> str | None:
    candidates = [
        e["ref"]
        for f in observation.frames
        for e in f.elements
        if _matches(e, binding, f.name, inputs)
    ]
    nth = int(binding.get("nth", 0))
    return candidates[nth] if len(candidates) > nth else None
