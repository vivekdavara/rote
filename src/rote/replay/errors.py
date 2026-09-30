"""Control-flow exceptions inside a replay. Each maps to one RunResult status."""

from __future__ import annotations

from typing import Any

from rote.schema.result import ErrorInfo, OutcomeInfo


class Rejected(Exception):
    """Refused before touching the UI (status: rejected)."""

    def __init__(self, error: ErrorInfo) -> None:
        super().__init__(error.message)
        self.error = error


class BusinessOutcome(Exception):
    """A declared business outcome was observed (status: business_outcome)."""

    def __init__(self, outcome: OutcomeInfo) -> None:
        super().__init__(outcome.code)
        self.outcome = outcome


class HardFailure(Exception):
    """Stop with evidence (status: failed)."""

    def __init__(self, error: ErrorInfo) -> None:
        super().__init__(error.message)
        self.error = error


class NeedsHuman(Exception):
    """A condition an operator can resolve on the live session. Unattended runs fail with it."""

    def __init__(self, code: str, message: str, *, step_id: str | None, expected: str | None = None,
                 observed: dict[str, Any] | None = None, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.step_id = step_id
        self.expected = expected
        self.observed = observed
        self.hint = hint


class Restart(Exception):
    """Start the capability over from the beginning (only while nothing irreversible has run)."""

    def __init__(self, code: str, relogin: bool) -> None:
        super().__init__(code)
        self.code = code
        self.relogin = relogin


class PreviewReady(Exception):
    """Preview mode reached the first irreversible step (status: preview)."""
