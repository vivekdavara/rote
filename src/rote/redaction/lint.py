"""Artifact lint: refuse to save an artifact that carries regulated data.

Placeholders are the structural guarantee: inputs appear as ``{{inputs.x}}``.
The lint is the check that the guarantee held: no literal example values, no
values the run saw on screen as PII, nothing that looks like an SSN, card,
account number or token.
"""

from __future__ import annotations

from collections.abc import Iterable

from rote.redaction.detectors import find
from rote.schema.capability import Capability
from rote.schema.templating import iter_strings


def lint_capability(capability: Capability, sensitive_values: Iterable[str]) -> list[str]:
    values = [v for v in sensitive_values if v and len(v) >= 3]
    problems: list[str] = []
    dumped = capability.model_dump(mode="json", by_alias=True, exclude_none=True)
    for text in iter_strings(dumped):
        for value in values:
            if value in text:
                problems.append(f"a sensitive value appears literally in the artifact (in {text[:40]!r}...)")
        for hit in find(text):
            problems.append(f"{hit}-like text in the artifact: {text[:60]!r}")
    return sorted(set(problems))
