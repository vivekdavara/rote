"""The seam between a run and the human-in-the-loop control plane.

A run holds the live session. When it needs a person, it opens an intervention
and waits. The control plane moves the control lease to the operator, relays the
operator's input into the same page, and eventually hands control back with a
resolution. The replay engine depends only on this protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from rote.schema.result import HumanAction, InterventionRecord

Resolution = Literal["handed_back", "approved", "rejected", "aborted", "timed_out"]


@dataclass
class OperatorResolution:
    kind: Resolution
    operator: str | None = None
    note: str | None = None
    resume_step: str | None = None
    attested_steps: list[str] = field(default_factory=list)
    human_actions: list[HumanAction] = field(default_factory=list)


class Escalator(Protocol):
    def agent_may_act(self) -> bool:
        """True only while the automation holds the control lease."""
        ...

    async def escalate(
        self, record: InterventionRecord, *, screenshot: bytes | None, dialog_message: str | None
    ) -> OperatorResolution:
        """Open an intervention and wait until the operator resolves it or it times out."""
        ...
