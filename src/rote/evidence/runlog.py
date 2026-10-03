"""Per-run evidence: structured events, the result, screenshots, failure snapshots.

Everything written here passes through the run's redactor first.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rote.redaction.redactor import Redactor
from rote.schema.events import Event, EventType


def new_run_id(prefix: str = "run") -> str:
    # The "T" matters: "20261003-143942" is 14 digits joined by a dash, which looks like a card number to the
    # redactor and the artifact lint whenever it passes the Luhn check (one timestamp in ten).
    return f"{prefix}-{datetime.now():%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


class RunLog:
    def __init__(self, root: Path, run_id: str, redactor: Redactor) -> None:
        self.run_id = run_id
        self.dir = root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self._seq = 0
        self._events = self.dir / "events.jsonl"

    def event(self, type: EventType, **data: Any) -> None:
        self._seq += 1
        record = Event(
            seq=self._seq,
            ts=datetime.now(UTC),
            run_id=self.run_id,
            type=type,
            data=self.redactor.data(data),
        )
        with self._events.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")

    def write_json(self, name: str, value: Any) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.redactor.data(value), indent=2, default=str) + "\n", encoding="utf-8")
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.redactor.text(text), encoding="utf-8")
        return path

    def save_bytes(self, name: str, data: bytes) -> str:
        """Binary evidence (masked screenshots). Returns the path relative to the run dir."""
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return name
