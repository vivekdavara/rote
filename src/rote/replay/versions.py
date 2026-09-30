"""Compare an observed product version with a capability's compatible range (">=4.2,<5")."""

from __future__ import annotations

import re

_CLAUSE = re.compile(r"^\s*(>=|<=|==|>|<)\s*(\d+(?:\.\d+)*)\s*$")


def parse(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def in_range(version: str, spec: str) -> bool:
    observed = parse(version)
    for clause in spec.split(","):
        match = _CLAUSE.match(clause)
        if match is None:
            raise ValueError(f"bad version clause {clause!r}")
        op, bound = match.group(1), parse(match.group(2))
        left, right = _pad(observed, bound)
        ok = {
            ">=": left >= right,
            "<=": left <= right,
            ">": left > right,
            "<": left < right,
            "==": left == right,
        }[op]
        if not ok:
            return False
    return True
