"""The result contract every run returns to its caller.

``status`` separates the three things the brief insists must not be conflated:

* ``business_outcome``: a legitimate answer the caller must handle (``outcome.code``)
* ``succeeded`` with ``recoveries``: conditions the run handled on its own
* ``failed``: a hard failure with enough detail to debug (``error``)

plus ``rejected`` (refused before touching the UI), ``preview`` (stopped before
the first irreversible step), and ``needs_intervention`` (an interim status: an
operator holds the live session).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RunStatus = Literal["succeeded", "business_outcome", "failed", "rejected", "preview", "needs_intervention"]
CommitState = Literal["none", "committed", "unknown"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorInfo(_Model):
    code: str
    category: Literal["rejected", "needs_human", "hard_failure"]
    message: str
    step_id: str | None = None
    step_intent: str | None = None
    expected: str | None = None
    observed: dict[str, Any] | None = None
    evidence: list[str] = Field(default_factory=list)
    retryable: bool = False
    hint: str | None = None


class OutcomeInfo(_Model):
    code: str
    message: str | None = None
    step_id: str | None = None


class RecoveryRecord(_Model):
    code: str
    step_id: str | None = None
    attempts: int = 1
    detail: str | None = None


class DriftSignal(_Model):
    step_id: str
    matched_strategy: str
    primary_strategy: str
    note: str | None = None


class HumanAction(_Model):
    at: datetime
    kind: Literal["click", "type", "key", "scroll", "dialog"]
    detail: dict[str, Any] = Field(default_factory=dict)


class InterventionRecord(_Model):
    id: str
    reason_code: str
    reason: str
    step_id: str | None = None
    opened_at: datetime
    resolved_at: datetime | None = None
    resolution: Literal["handed_back", "approved", "rejected", "aborted", "timed_out"] | None = None
    operator: str | None = None
    note: str | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)
    attested_steps: list[str] = Field(default_factory=list)


class PreviewInfo(_Model):
    values: dict[str, Any]
    commit_token: str
    expires_at: datetime


class StepRecord(_Model):
    step_id: str
    status: Literal["ok", "skipped", "performed_by_human"]
    strategy: str | None = None
    duration_ms: int | None = None


class RunResult(_Model):
    run_id: str
    capability: str
    version: str
    content_hash: str
    overlay_hash: str | None = None
    tenant: str
    status: RunStatus
    outputs: dict[str, Any] | None = None
    preview: PreviewInfo | None = None
    outcome: OutcomeInfo | None = None
    error: ErrorInfo | None = None
    commit_state: CommitState = "none"
    steps: list[StepRecord] = Field(default_factory=list)
    recoveries: list[RecoveryRecord] = Field(default_factory=list)
    interventions: list[InterventionRecord] = Field(default_factory=list)
    drift: list[DriftSignal] = Field(default_factory=list)
    idempotent_replay: bool = Field(
        default=False, description="True when this result came from the idempotency ledger, not a fresh run."
    )
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
