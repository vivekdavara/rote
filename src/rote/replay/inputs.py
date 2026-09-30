"""Validate invocation inputs against the capability contract before touching the UI."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from rote.schema.capability import Capability


def validate_inputs(capability: Capability, inputs: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Return (template values, problems). Problems never echo sensitive values."""
    problems: list[str] = []
    values: dict[str, str] = {}
    for name in inputs:
        if name not in capability.inputs:
            problems.append(f"unknown input {name!r}")
    for name, spec in capability.inputs.items():
        if name not in inputs or inputs[name] is None or str(inputs[name]).strip() == "":
            problems.append(f"missing input {name!r}")
            continue
        raw = inputs[name]
        shown = "the value" if spec.sensitive else repr(raw)
        text = str(raw).strip()
        if spec.type == "integer":
            if not re.fullmatch(r"-?\d+", text):
                problems.append(f"{name}: {shown} is not an integer")
                continue
        elif spec.type == "decimal":
            try:
                Decimal(text.replace(",", ""))
            except InvalidOperation:
                problems.append(f"{name}: {shown} is not a decimal number")
                continue
        elif spec.type == "boolean":
            if text.lower() not in ("true", "false"):
                problems.append(f"{name}: {shown} is not true/false")
                continue
            text = text.lower()
        if spec.pattern is not None and not re.fullmatch(spec.pattern, text):
            problems.append(f"{name}: {shown} does not match {spec.pattern}")
            continue
        if spec.enum is not None and text not in spec.enum:
            problems.append(f"{name}: {shown} is not one of {spec.enum}")
            continue
        values[name] = text
    return values, problems
