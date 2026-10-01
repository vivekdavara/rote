"""The control lease: who may send input to the live session right now.

    AGENT_ACTIVE -> AWAITING_OPERATOR -> HUMAN_ACTIVE -> RESYNCING -> AGENT_ACTIVE
                          |                    |              |
                          +-> AGENT_ACTIVE     +-> ABORTED    +-> AWAITING_OPERATOR (re-escalate)
                          |   (approved)       +-> TIMED_OUT
                          +-> ABORTED / TIMED_OUT

Exactly one party may act at a time. The automation's actuator checks
``agent_may_act()`` before every input. The operator relay checks
``human_may_act()``. Every transition bumps the epoch and is logged with the
actor and the reason, so the evidence shows exactly who held control when.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class LeaseState(StrEnum):
    AGENT_ACTIVE = "AGENT_ACTIVE"
    AWAITING_OPERATOR = "AWAITING_OPERATOR"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    RESYNCING = "RESYNCING"
    ABORTED = "ABORTED"
    TIMED_OUT = "TIMED_OUT"


ALLOWED: dict[LeaseState, set[LeaseState]] = {
    LeaseState.AGENT_ACTIVE: {LeaseState.AWAITING_OPERATOR},
    LeaseState.AWAITING_OPERATOR: {LeaseState.HUMAN_ACTIVE, LeaseState.AGENT_ACTIVE, LeaseState.ABORTED,
                                   LeaseState.TIMED_OUT},
    LeaseState.HUMAN_ACTIVE: {LeaseState.RESYNCING, LeaseState.ABORTED, LeaseState.TIMED_OUT},
    LeaseState.RESYNCING: {LeaseState.AGENT_ACTIVE, LeaseState.AWAITING_OPERATOR, LeaseState.ABORTED},
    LeaseState.ABORTED: set(),
    LeaseState.TIMED_OUT: set(),
}


class LeaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Transition:
    at: datetime
    source: LeaseState
    target: LeaseState
    actor: str
    reason: str
    epoch: int


class ControlLease:
    def __init__(self, on_change: Callable[[Transition], Any] | None = None) -> None:
        self.state = LeaseState.AGENT_ACTIVE
        self.epoch = 0
        self.holder = "agent"
        self.history: list[Transition] = []
        self._on_change = on_change

    def move(self, target: LeaseState, *, actor: str, reason: str) -> Transition:
        if target not in ALLOWED[self.state]:
            raise LeaseError(f"cannot move the lease from {self.state.value} to {target.value}")
        self.epoch += 1
        transition = Transition(datetime.now(UTC), self.state, target, actor, reason, self.epoch)
        self.state = target
        self.holder = actor if target is LeaseState.HUMAN_ACTIVE else ("agent" if target is LeaseState.AGENT_ACTIVE
                                                                        else "nobody")
        self.history.append(transition)
        if self._on_change is not None:
            self._on_change(transition)
        return transition

    def agent_may_act(self) -> bool:
        return self.state is LeaseState.AGENT_ACTIVE

    def human_may_act(self) -> bool:
        return self.state is LeaseState.HUMAN_ACTIVE
