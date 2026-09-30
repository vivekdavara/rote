"""Structured run events, one JSON object per line in ``events.jsonl``."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "run_started",
    "observation",
    "decision",
    "policy_decision",
    "action_executed",
    "checkpoint",
    "interrupt_detected",
    "recovery_applied",
    "drift",
    "escalation_requested",
    "lease_changed",
    "human_action",
    "resync",
    "output_extracted",
    "network_blocked",
    "dialog",
    "note",
    "run_finished",
]


class Event(BaseModel):
    seq: int
    ts: datetime
    run_id: str
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
