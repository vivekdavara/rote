"""Pattern detectors for regulated data that must not reach logs or artifacts.

Detectors are a backstop, not the main defense. The main defense is structural:
inputs are placeholders in artifacts, and known sensitive values are
pseudonymized by exact match (see ``redactor.py``). Regexes miss free-text PII
such as names, which is why the product profile also declares the on-screen
fields that hold PII.
"""

from __future__ import annotations

import re

DETECTORS: list[tuple[str, re.Pattern[str]]] = [
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone", re.compile(r"\(?\b\d{3}\)?[-. ]?\d{3}[-. ]\d{4}\b")),
    ("dob", re.compile(r"\b(?:0[1-9]|1[0-2])/(?:0[1-9]|[12]\d|3[01])/(?:19|20)\d{2}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("account_number", re.compile(r"\b\d{9,17}\b")),
]


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def scrub(text: str) -> str:
    for name, pattern in DETECTORS:
        if name == "card":
            def replace_card(match: re.Match[str]) -> str:
                digits = re.sub(r"\D", "", match.group(0))
                return "[card]" if 13 <= len(digits) <= 19 and luhn_ok(digits) else match.group(0)

            text = pattern.sub(replace_card, text)
        else:
            text = pattern.sub(f"[{name}]", text)
    return text


def find(text: str) -> list[str]:
    """Names of detectors that fire on ``text`` (used by the artifact lint)."""
    hits = []
    for name, pattern in DETECTORS:
        for match in pattern.finditer(text):
            if name == "card":
                digits = re.sub(r"\D", "", match.group(0))
                if not (13 <= len(digits) <= 19 and luhn_ok(digits)):
                    continue
            hits.append(name)
            break
    return hits
