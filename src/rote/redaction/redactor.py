"""Pseudonymize known sensitive values and scrub detector hits before anything is persisted.

A known value (a member number passed as input, an extracted balance, a name the
profile flags on screen) becomes ``[member_id#3f9c21aa]``: a keyed HMAC prefix.
Two log lines about the same member can still be correlated, but without the key
the value cannot be recovered by brute force. A plain salted hash of a 6-digit
member number would be trivial to reverse.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from rote.redaction.detectors import scrub


def load_key(workspace: Path, name: str = "redaction", env: str = "ROTE_REDACTION_KEY") -> bytes:
    """A per-install secret kept in .rote/ (gitignored), or taken from the environment."""
    if os.environ.get(env):
        return os.environ[env].encode()
    path = workspace / ".rote" / f"{name}.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip().encode()


def money_forms(value: str) -> set[str]:
    """The ways an amount shows up on screen and in results: 1234.56, 1,234.56, $1,234.56."""
    try:
        amount = Decimal(value.replace("$", "").replace(",", ""))
    except InvalidOperation:
        return {value}
    plain = f"{abs(amount):.2f}"
    grouped = f"{abs(amount):,.2f}"
    return {plain, grouped, f"${grouped}", value}


class Redactor:
    def __init__(self, key: bytes) -> None:
        self._key = key
        self._values: dict[str, str] = {}

    def register(self, value: Any, cls: str) -> None:
        text = str(value).strip() if value is not None else ""
        if len(text) < 3:
            return
        self._values[text] = cls

    def register_money(self, value: str, cls: str) -> None:
        for form in money_forms(value):
            self.register(form, cls)

    def known_values(self) -> list[str]:
        return list(self._values)

    def pseudonym(self, value: str, cls: str) -> str:
        digest = hmac.new(self._key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:8]
        return f"[{cls}#{digest}]"

    def text(self, value: str) -> str:
        for known in sorted(self._values, key=len, reverse=True):
            if known in value:
                value = value.replace(known, self.pseudonym(known, self._values[known]))
        return scrub(value)

    def data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.data(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.data(v) for v in value]
        return value
