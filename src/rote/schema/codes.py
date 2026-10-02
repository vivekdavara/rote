"""The error taxonomy, in one table.

Business outcomes are not listed here: each capability declares its own
(MEMBER_NOT_FOUND, VALIDATION_REJECTED, ...) because they belong to the contract.
Everything else a run can report has an entry below. Categories:

* ``rejected``: refused before the UI was touched
* ``recoverable``: handled inside the run and reported in ``recoveries``
* ``needs_human``: escalated to an operator when the run is attended; a hard failure otherwise
* ``hard_failure``: the run stops with evidence
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Category = Literal["rejected", "recoverable", "needs_human", "hard_failure"]


@dataclass(frozen=True)
class CodeInfo:
    category: Category
    retryable: bool
    summary: str


TAXONOMY: dict[str, CodeInfo] = {
    # rejected before touching the UI
    "INVALID_INPUT": CodeInfo("rejected", False, "Inputs failed the capability's input schema."),
    "POLICY_VIOLATION": CodeInfo("rejected", False, "The artifact uses a route or action the tenant policy forbids."),
    "NOT_APPROVED": CodeInfo("rejected", False, "Unattended runs need an approval bound to this exact content hash."),
    "COMMIT_TOKEN_REQUIRED": CodeInfo("rejected", False, "Irreversible capabilities commit only with a preview token."),
    "COMMIT_TOKEN_INVALID": CodeInfo("rejected", False, "Token is expired, forged, or bound to other inputs/artifact."),
    "IDEMPOTENCY_KEY_REQUIRED": CodeInfo("rejected", False, "Irreversible capabilities need an idempotency key."),
    "COMMIT_IN_PROGRESS": CodeInfo("rejected", True, "Another run holds this idempotency key."),
    "OVERLAY_INVALID": CodeInfo("rejected", False, "The tenant overlay no longer fits the base, or would change its contract."),
    # recoverable: handled and reported
    "KNOWN_INTERSTITIAL": CodeInfo("recoverable", True, "A known notice was dismissed."),
    "KNOWN_DIALOG": CodeInfo("recoverable", True, "A known native dialog was answered per the product profile."),
    "SESSION_EXPIRED": CodeInfo("recoverable", True, "Signed in again and restarted from the beginning."),
    "SLOW_LOAD": CodeInfo("recoverable", True, "The app was still loading; waited up to the slow-load cap."),
    "TRANSIENT_APP_ERROR": CodeInfo("recoverable", True, "A transient server error; restarted from the beginning."),
    # needs a human when attended
    "UNKNOWN_MODAL": CodeInfo("needs_human", False, "An unrecognized overlay covers the control the step needs."),
    "UNKNOWN_DIALOG": CodeInfo("needs_human", False, "A native dialog the product profile does not recognize."),
    "INDETERMINATE_COMMIT": CodeInfo(
        "needs_human", False, "An irreversible action ran but its result was never confirmed. Never retried."
    ),
    # hard failures
    "TARGET_NOT_FOUND": CodeInfo("hard_failure", False, "No locator strategy found the control."),
    "TARGET_AMBIGUOUS": CodeInfo("hard_failure", False, "Locators matched more than one control."),
    "UNEXPECTED_STATE": CodeInfo("hard_failure", False, "The page settled somewhere the step did not expect."),
    "LOAD_TIMEOUT": CodeInfo("hard_failure", True, "The app was still loading when the step timed out."),
    "APP_ERROR": CodeInfo("hard_failure", True, "The app returned a server error page."),
    "PERMISSION_DENIED": CodeInfo(
        "hard_failure", False, "The service account lacks a permission: a configuration problem, not a business result."
    ),
    "APP_UNREACHABLE": CodeInfo("hard_failure", True, "The application could not be reached at all."),
    "LOGIN_FAILED": CodeInfo("hard_failure", False, "The product-profile login flow did not reach the home state."),
    "PREVIEW_MISMATCH": CodeInfo(
        "hard_failure", False, "Values on the review screen differ from what the preview token approved."
    ),
    "AMBIGUOUS_EXTRACTION": CodeInfo("hard_failure", False, "An output declared cardinality one matched several."),
    "OUTPUT_INVALID": CodeInfo("hard_failure", False, "An extracted value could not be parsed as its declared type."),
    "CHECKPOINT_FAILED": CodeInfo("hard_failure", False, "The success checkpoint did not hold at the end of the run."),
    "POLICY_BLOCKED": CodeInfo("hard_failure", False, "The policy gate or network allowlist blocked an action at run time."),
    "ESCALATION_TIMEOUT": CodeInfo("hard_failure", True, "No operator resolved the intervention in time."),
    "ESCALATION_ABORTED": CodeInfo("hard_failure", False, "The operator aborted the run."),
    "OPERATOR_REJECTED": CodeInfo("hard_failure", False, "The operator rejected an action that needed approval."),
    "RESYNC_FAILED": CodeInfo("hard_failure", False, "After hand-back, the page matched no known step state."),
}


def info(code: str) -> CodeInfo:
    return TAXONOMY[code]
