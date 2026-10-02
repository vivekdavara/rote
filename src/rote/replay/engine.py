"""Deterministic replay: run a capability artifact with no model in the loop.

The engine is the production path: the thing an agent calls. Per step:

1. check interrupts: product-level conditions (known notices, session expiry,
   error pages) and the capability's own business outcomes, scoped to the steps
   where they can occur
2. resolve the target with ranked locators (exactly one match, or drift)
3. hit-test it: an unrecognized overlay covering the control is UNKNOWN_MODAL
4. pass the policy gate (allowlist, route, runtime risk vs declared effect)
5. act, through the control lease when a human might hold the session
6. wait for the step's postcondition while polling the same detectors

Nothing here imports a model client. ``tests/unit/test_no_llm_in_replay.py``
keeps it that way.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from playwright.async_api import Browser, Playwright
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel

from rote.control.protocol import Escalator, OperatorResolution
from rote.evidence.runlog import new_run_id
from rote.redaction.redactor import load_key
from rote.registry.approvals import find_approval, load_approvals
from rote.registry.store import Workspace
from rote.replay import commit
from rote.replay.errors import BusinessOutcome, HardFailure, NeedsHuman, PreviewReady, Rejected, Restart
from rote.replay.inputs import validate_inputs
from rote.replay.ledger import Ledger
from rote.runtime import Runtime
from rote.schema.capability import (
    Capability,
    ExtractStep,
    PressStep,
    Step,
)
from rote.schema.codes import TAXONOMY
from rote.schema.config import InterruptSpec, Policy, ProductProfile, TenantConfig
from rote.schema.overlay import OverlayError
from rote.schema.result import (
    DriftSignal,
    ErrorInfo,
    InterventionRecord,
    OutcomeInfo,
    PreviewInfo,
    RecoveryRecord,
    RunResult,
    StepRecord,
)
from rote.schema.target import Target
from rote.secrets import resolve_secrets
from rote.surface.web import actions
from rote.surface.web.conditions import ConditionEvaluator, describe
from rote.surface.web.resolver import ResolutionFailure, Resolved, Resolver, element_facts, hit_test
from rote.surface.web.session import WebSession

Mode = Literal["run", "preview", "commit"]

# Conditions an operator can fix on the live session. Everything else fails fast.
ESCALATABLE = frozenset(
    {"UNKNOWN_MODAL", "UNKNOWN_DIALOG", "INDETERMINATE_COMMIT", "UNEXPECTED_STATE", "TARGET_NOT_FOUND"}
)


@dataclass
class ReplayOptions:
    mode: Mode = "run"
    attended: bool = False
    require_approval: bool = True
    headless: bool = True
    screenshots: bool = True
    commit_token: str | None = None
    idempotency_key: str | None = None
    step_timeout_ms: int | None = None


class ReplayEngine:
    def __init__(
        self,
        workspace: Workspace,
        capability: Capability,
        tenant: TenantConfig,
        profile: ProductProfile,
        policy: Policy,
        inputs: dict[str, Any],
        options: ReplayOptions | None = None,
        *,
        playwright: Playwright | None = None,
        browser: Browser | None = None,
        escalator: Escalator | None = None,
        overlay_hash: str | None = None,
        base: Capability | None = None,
        run_id: str | None = None,
    ) -> None:
        self.workspace = workspace
        self.capability = capability  # what runs (the base, or the base with this tenant's overlay)
        self.base = base or capability  # what was reviewed: its hash plus the overlay hash is the identity
        self.tenant = tenant
        self.profile = profile
        self.policy = policy
        self.raw_inputs = inputs
        self.options = options or ReplayOptions()
        self.playwright = playwright
        self.browser = browser
        self.escalator = escalator
        self.overlay_hash = overlay_hash
        self.limits = policy.limits
        self.step_timeout = self.options.step_timeout_ms or self.limits.step_timeout_ms

        self.run_id = run_id or new_run_id("replay")
        self.rt = Runtime(workspace, tenant, profile, policy, run_id=self.run_id,
                          hold_unknown_dialogs=self.options.attended and escalator is not None)
        self.rt.on_known_dialog = self._known_dialog
        bind = getattr(escalator, "bind", None)
        if bind is not None:
            bind(self.rt, [(s.id, s.intent or "") for s in capability.steps])
        self.redactor = self.rt.redactor
        self.log = self.rt.log
        self.gate = self.rt.gate
        self.result = RunResult(
            run_id=self.run_id,
            capability=capability.id,
            version=capability.version,
            content_hash=self.base.content_hash(),
            overlay_hash=overlay_hash,
            tenant=tenant.id,
            status="failed",
            started_at=datetime.now(UTC),
        )
        self.outputs: dict[str, Any] = {}
        self.completed: list[str] = []
        self.irreversible_done = False
        self.current_step: str | None = None
        self._recovery_counts: dict[tuple[str, str], int] = {}
        self._escalations: dict[str, int] = {}
        self._ledger = Ledger(workspace.root)
        self._signing_key = load_key(workspace.root, "signing", "ROTE_SIGNING_KEY")
        self._claims: commit.TokenClaims | None = None
        self._claimed: str | None = None
        self._replayed: RunResult | None = None
        self._inputs: dict[str, str] = {}
        self._secrets: dict[str, str] = {}
        self.web: WebSession
        self.resolver: Resolver
        self.cond: ConditionEvaluator

    # ============================================================== public entry

    async def run(self) -> RunResult:
        started = time.monotonic()
        self.log.event(
            "run_started",
            capability=self.capability.id,
            version=self.capability.version,
            content_hash=self.result.content_hash,
            tenant=self.tenant.id,
            mode=self.options.mode,
            attended=self.options.attended,
        )
        try:
            self._preflight()
            if self._replayed is not None:
                self.result = self._replayed  # the recorded result for this idempotency key
                return self.result
            async with self.rt.session(playwright=self.playwright, browser=self.browser,
                                       headless=self.options.headless) as web:
                self.web = web
                context = {"inputs": self._inputs, "tenant": self.tenant.template_vars()}
                self.resolver = Resolver(web, context, self.tenant.labels)
                self.cond = ConditionEvaluator(self.resolver, self.outputs)
                await self._enter()
                await self._execute()
        except Rejected as exc:
            self.result.status = "rejected"
            self.result.error = exc.error
        except BusinessOutcome as exc:
            self.result.status = "business_outcome"
            self.result.outcome = exc.outcome
        except PreviewReady:
            self.result.status = "preview"
        except HardFailure as exc:
            self.result.status = "failed"
            self.result.error = exc.error
        finally:
            self.result.warnings.extend(w for w in self.rt.warnings if w not in self.result.warnings)
            self.result.finished_at = datetime.now(UTC)
            self.result.duration_ms = int((time.monotonic() - started) * 1000)
            if self._replayed is None and self.result.status in ("succeeded", "preview"):
                self.result.outputs = dict(self.outputs)
            if self._claimed:
                if self.irreversible_done or self.result.commit_state != "none":
                    self._ledger.record(self.capability.id, self.tenant.id, self._claimed, self.result)
                else:
                    self._ledger.release(self.capability.id, self.tenant.id, self._claimed)
            self.log.write_json("result.json", self.result.model_dump(mode="json"))
            code = self.result.error.code if self.result.error else None
            code = code or (self.result.outcome.code if self.result.outcome else None)
            self.log.event("run_finished", status=self.result.status, code=code, duration_ms=self.result.duration_ms)
        return self.result

    # ================================================================ pre-flight

    def _reject(self, code: str, message: str) -> Rejected:
        return Rejected(ErrorInfo(code=code, category="rejected", message=message, retryable=TAXONOMY[code].retryable))

    def _preflight(self) -> None:
        values, problems = validate_inputs(self.capability, self.raw_inputs)
        if problems:
            raise self._reject("INVALID_INPUT", "; ".join(problems))
        self._inputs = values
        for name, spec in self.capability.inputs.items():
            if spec.sensitive:
                self.redactor.register(values[name], spec.sensitive)

        violations = self.gate.static_violations(self.capability)
        if violations:
            raise self._reject("POLICY_VIOLATION", "; ".join(violations))

        if self.options.require_approval and not self.options.attended:
            approvals = load_approvals(self.workspace.root, self.capability.id)
            approval = find_approval(approvals, self.base, tenant=self.tenant.id, overlay_hash=self.overlay_hash)
            if approval is None:
                raise self._reject(
                    "NOT_APPROVED",
                    f"no approval matches content hash {self.result.content_hash}; review it, then `rote approve`",
                )
            self.log.event("policy_decision", rule="approval", allowed=True, reviewer=approval.reviewer)

        if self.capability.side_effects == "irreversible" and self.options.mode == "run":
            self.options.mode = "preview"
            self.result.warnings.append("irreversible capability: ran in preview mode; committing needs a preview token")

        if self.options.mode == "commit":
            self._preflight_commit()

        secrets, missing = resolve_secrets(self.tenant, self.workspace.root)
        if missing:
            raise HardFailure(self._error("LOGIN_FAILED", "missing secrets: " + ", ".join(missing), None))
        self._secrets = secrets

    def _preflight_commit(self) -> None:
        if self.capability.side_effects != "irreversible":
            raise self._reject("POLICY_VIOLATION", "commit mode is only for irreversible capabilities")
        if not self.options.idempotency_key:
            raise self._reject("IDEMPOTENCY_KEY_REQUIRED", "an irreversible commit needs an idempotency key")
        if not self.options.commit_token:
            raise self._reject("COMMIT_TOKEN_REQUIRED", "run a preview first and pass its commit token")
        try:
            self._claims = commit.verify(
                self._signing_key, self.options.commit_token, capability=self.capability.id,
                content_hash=self.result.content_hash, overlay_hash=self.overlay_hash, tenant=self.tenant.id,
                inputs=self._inputs,
            )
        except commit.TokenError as exc:
            raise self._reject("COMMIT_TOKEN_INVALID", str(exc)) from exc
        key = self.options.idempotency_key
        known = self._ledger.lookup(self.capability.id, self.tenant.id, key)
        if known is not None:
            state, recorded = known
            if recorded is None:
                raise self._reject("COMMIT_IN_PROGRESS", f"idempotency key {key!r} is held by a run in progress")
            self._replayed = recorded.model_copy(update={"idempotent_replay": True})
            self.log.event("note", idempotency_key=key, ledger_state=state, replayed_run=recorded.run_id)
            return
        if not self._ledger.claim(self.capability.id, self.tenant.id, key):
            raise self._reject("COMMIT_IN_PROGRESS", f"idempotency key {key!r} was just claimed by another run")
        self._claimed = key

    # ================================================================== session

    # ============================================================ entry & login

    async def _enter(self) -> None:
        await self.rt.enter(self._secrets, step_timeout_ms=self.step_timeout, versions=self.capability.product.versions)

    def _known_dialog(self, message: str, response: str) -> None:
        self.result.recoveries.append(RecoveryRecord(code="KNOWN_DIALOG", step_id=self.current_step,
                                                     detail=f"{response}: {message}"))

    # ================================================================ execution

    async def _execute(self) -> None:
        steps = self.capability.steps
        restarts = 0
        index = 0
        while index < len(steps):
            step = steps[index]
            if step.effect == "irreversible" and not self.irreversible_done:
                if self.options.mode == "preview":
                    self._build_preview()
                    raise PreviewReady
                if self.options.mode == "commit":
                    self._check_preview(step)
            try:
                await self._run_step(step)
            except Restart as exc:
                if self.irreversible_done or restarts >= self.limits.max_restarts:
                    raise HardFailure(self._error(
                        exc.code, f"{exc.code} recurred after {restarts} restart(s); not restarting again", step.id,
                    )) from exc
                restarts += 1
                self.outputs.clear()
                self.completed.clear()
                self.log.event("recovery_applied", code=exc.code, action="restart", restarts=restarts)
                await self._enter()
                index = 0
                continue
            except NeedsHuman as exc:
                index = await self._needs_human(exc, index)
                continue
            self.completed.append(step.id)
            index += 1

        if not await self._poll(self.capability.success, 3000):
            raise HardFailure(self._error(
                "CHECKPOINT_FAILED", "the success checkpoint did not hold at the end of the run", None,
                expected=describe(self.capability.success), observed=await self._observed(),
                evidence=await self._capture_failure(),
            ))
        self.log.event("checkpoint", step="success", condition=describe(self.capability.success), held=True)
        self.result.status = "succeeded"
        if self.irreversible_done:
            self.result.commit_state = "committed"

    async def _run_step(self, step: Step) -> None:
        started = time.monotonic()
        self.current_step = step.id
        await self._settle_interrupts(step, after=False)
        target = getattr(step, "target", None)
        resolved = await self._acquire(step, target) if target is not None else None

        if isinstance(step, ExtractStep):
            assert resolved is not None
            await self._extract(step, resolved)
        else:
            self._require_lease(step)
            if resolved is not None:
                await self._perform(step, resolved, self.resolver)
            elif isinstance(step, PressStep):
                await self.web.page.keyboard.press(step.key)
            if step.effect == "irreversible":
                self.irreversible_done = True
                self.result.commit_state = "unknown"

        if step.expect is not None:
            await self._await_postcondition(step)
        self.result.steps.append(StepRecord(
            step_id=step.id, status="ok", strategy=resolved.strategy if resolved else None,
            duration_ms=int((time.monotonic() - started) * 1000),
        ))
        await self._step_evidence(step)

    # ----------------------------------------------------- resolve, hit-test, gate

    async def _acquire(self, step: Step, target: Target) -> Resolved:
        many = isinstance(step, ExtractStep)  # extraction resolves all matches so cardinality can be enforced

        async def poll() -> None:
            await self._settle_interrupts(step, after=False)

        for _attempt in range(3):
            try:
                resolved = await self.resolver.resolve(target, self.step_timeout, many=many, on_poll=poll)
            except ResolutionFailure as exc:
                raise NeedsHuman(exc.code, f"step {step.id}: {exc}", step_id=step.id, hint=exc.hint,
                                 observed=await self._observed()) from exc
            hit = await hit_test(resolved)
            if hit["ok"]:
                break
            if await self._settle_interrupts(step, after=False):
                continue  # a known notice covered the control and was dismissed; resolve again
            raise NeedsHuman(
                "UNKNOWN_MODAL",
                f"step {step.id}: the control is covered by an unrecognized overlay: {hit.get('covering')!r}",
                step_id=step.id, observed=await self._observed(),
            )
        else:
            raise NeedsHuman("UNKNOWN_MODAL", f"step {step.id}: the control stayed covered", step_id=step.id)

        if resolved.rank > 0:
            primary = target.locators[0].by
            self.result.drift.append(DriftSignal(
                step_id=step.id, matched_strategy=resolved.strategy, primary_strategy=primary,
                note=f"the primary {primary} locator no longer matches; {resolved.strategy} did",
            ))
            self.log.event("drift", step=step.id, matched=resolved.strategy, primary=primary)

        facts = await element_facts(resolved)
        decision = self.gate.check_action(step.action, facts, resolved.frame.url, step.effect)
        self.log.event("policy_decision", step=step.id, action=step.action, allowed=decision.allowed,
                       rule=decision.rule, reason=decision.reason, runtime_effect=decision.runtime_effect)
        if not decision.allowed:
            raise HardFailure(self._error("POLICY_BLOCKED", f"step {step.id}: {decision.reason}", step.id))
        return resolved

    def _require_lease(self, step: Step) -> None:
        if self.escalator is not None and not self.escalator.agent_may_act():
            raise HardFailure(self._error("ESCALATION_ABORTED", "automation does not hold the control lease", step.id))

    # ------------------------------------------------------------------ act

    async def _perform(self, step: Step, resolved: Resolved, resolver: Resolver) -> None:
        try:
            await self.rt.perform(step, resolved, resolver)
        except (PlaywrightError, actions.OptionNotFound) as exc:
            if self.rt.pending_dialog is not None:
                # The action stalled behind a native dialog (a click waits for the navigation it started).
                raise NeedsHuman("UNKNOWN_DIALOG", f"unrecognized dialog during {step.id}: "
                                 f"{self.rt.dialog_message!r}", step_id=step.id) from exc
            raise NeedsHuman("UNEXPECTED_STATE", f"step {step.id}: {step.action} failed: {str(exc).splitlines()[0]}",
                             step_id=step.id, observed=await self._observed()) from exc

    async def _extract(self, step: ExtractStep, resolved: Resolved) -> None:
        spec = self.capability.outputs[step.output]
        if spec.cardinality == "one" and len(resolved.elements) > 1:
            raise HardFailure(self._error(
                "AMBIGUOUS_EXTRACTION",
                f"step {step.id}: {len(resolved.elements)} values match output {step.output!r}, which expects one",
                step.id, evidence=await self._capture_failure(),
            ))
        elements = resolved.elements if spec.cardinality == "many" else resolved.elements[:1]
        values = []
        for element in elements:
            raw = await actions.read(element)
            if spec.sensitive and spec.type == "money":
                self.redactor.register_money(raw, spec.sensitive)
            elif spec.sensitive:
                self.redactor.register(raw, spec.sensitive)
            try:
                values.append(actions.parse(raw, step.parse))
            except actions.ParseError as exc:
                raise HardFailure(self._error("OUTPUT_INVALID", f"step {step.id}: {exc}", step.id)) from exc
        self.outputs[step.output] = values if spec.cardinality == "many" else values[0]
        self.log.event("output_extracted", step=step.id, output=step.output, value=self.outputs[step.output],
                       strategy=resolved.strategy)

    # ------------------------------------------------------------ postcondition

    async def _await_postcondition(self, step: Step) -> None:
        assert step.expect is not None
        timeout = (step.timeout_ms or self.step_timeout) / 1000
        start = time.monotonic()
        deadline = start + timeout
        extended = False
        target = getattr(step, "target", None)
        self.cond.timeouts = 0
        while True:
            if self.rt.pending_dialog is not None:
                # Escalate on sight: any page read now would only wait out its timeout.
                raise NeedsHuman("UNKNOWN_DIALOG", f"unrecognized dialog after {step.id}: {self.rt.dialog_message!r}",
                                 step_id=step.id)
            if await self.cond.holds(step.expect, target):
                self.log.event("checkpoint", step=step.id, condition=describe(step.expect), held=True)
                return
            await self._settle_interrupts(step, after=True)
            if time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                continue
            loading = self.web.navigating or (
                self.profile.loading is not None and await self.cond.holds(self.profile.loading)
            )
            if loading and not extended:
                extended = True
                deadline = start + self.limits.slow_load_cap_ms / 1000
                self.result.recoveries.append(RecoveryRecord(
                    code="SLOW_LOAD", step_id=step.id, detail=f"still loading after {int(timeout * 1000)} ms"
                ))
                self.log.event("recovery_applied", code="SLOW_LOAD", step=step.id)
                continue
            expected = describe(step.expect)
            self.log.event("checkpoint", step=step.id, condition=expected, held=False)
            if self.cond.timeouts and self.rt.pending_dialog is None:
                # Reads timed out: the page is probably blocked by a dialog whose event is still
                # in flight (it can land seconds late on slower machines). Give it a moment.
                for _ in range(50):
                    if self.rt.pending_dialog is not None:
                        break
                    await asyncio.sleep(0.1)
            if self.rt.pending_dialog is not None:
                raise NeedsHuman("UNKNOWN_DIALOG", f"unrecognized dialog after {step.id}: {self.rt.dialog_message!r}",
                                 step_id=step.id, expected=expected)
            if step.effect == "irreversible":
                # Loading or not, the commit may already have happened: never call it a load timeout.
                raise NeedsHuman("INDETERMINATE_COMMIT", f"step {step.id} ran but its result was never confirmed",
                                 step_id=step.id, expected=expected, observed=await self._observed())
            if loading:
                raise HardFailure(self._error(
                    "LOAD_TIMEOUT", f"step {step.id}: still loading after {self.limits.slow_load_cap_ms} ms",
                    step.id, expected=expected, observed=await self._observed(),
                    evidence=await self._capture_failure(),
                ))
            raise NeedsHuman("UNEXPECTED_STATE", f"step {step.id}: expected {expected}", step_id=step.id,
                             expected=expected, observed=await self._observed())

    async def _poll(self, condition: BaseModel, timeout_ms: int) -> bool:
        return await self.rt.poll(self.cond, condition, timeout_ms)

    # ------------------------------------------------------------- interrupts

    async def _settle_interrupts(self, step: Step | None, *, after: bool) -> bool:
        """Handle every recoverable interrupt present; raise for anything terminal.

        Returns True when at least one recovery ran (the caller should re-check the page).
        """
        recovered = False
        for _ in range(4):
            if not await self._check_interrupts(step, after=after):
                return recovered
            recovered = True
        return recovered

    async def _check_interrupts(self, step: Step | None, *, after: bool) -> bool:
        step_id = step.id if step else None
        if self.rt.pending_dialog is not None:
            raise NeedsHuman("UNKNOWN_DIALOG", f"unrecognized dialog: {self.rt.dialog_message!r}", step_id=step_id)
        for spec in self.profile.interrupts:
            if not await self.cond.holds(spec.when):
                continue
            self.log.event("interrupt_detected", code=spec.code, kind=spec.kind, step=step_id)
            if spec.kind == "failure":
                raise HardFailure(self._error(
                    spec.code, spec.description or TAXONOMY[spec.code].summary, step_id,
                    observed=await self._observed(), evidence=await self._capture_failure(),
                ))
            await self._recover(spec, step)
            return True
        completed = set(self.completed) | ({step_id} if after and step_id else set())
        for code, outcome in self.capability.outcomes.items():
            if outcome.after_step is not None and outcome.after_step not in completed:
                continue
            if await self.cond.holds(outcome.when):
                self.log.event("interrupt_detected", code=code, kind="business_outcome", step=step_id)
                observed = await self._observed()
                shown = [m for f in observed["frames"] for m in f["messages"]]
                raise BusinessOutcome(OutcomeInfo(code=code, message=outcome.description, step_id=step_id,
                                                  messages=shown))
        return False

    async def _recover(self, spec: InterruptSpec, step: Step | None) -> None:
        step_id = step.id if step else None
        key = (spec.code, step_id or "")
        count = self._recovery_counts.get(key, 0) + 1
        self._recovery_counts[key] = count
        if count > spec.max_attempts:
            raise NeedsHuman("UNEXPECTED_STATE", f"{spec.code} kept recurring after {count - 1} recoveries",
                             step_id=step_id)
        record = RecoveryRecord(code=spec.code, step_id=step_id, attempts=count, detail=spec.description)
        if spec.handle == "steps":
            for handler in spec.steps:
                target = getattr(handler, "target", None)
                if target is None:
                    continue
                resolved = await self.resolver.resolve(target, 3000)
                self._require_lease(handler)
                await self._perform(handler, resolved, self.resolver)
            self.result.recoveries.append(record)
            self.log.event("recovery_applied", code=spec.code, step=step_id, action="steps")
            await asyncio.sleep(0.1)
            return
        if spec.handle in ("relogin_restart", "restart"):
            if self.irreversible_done:
                raise NeedsHuman("UNEXPECTED_STATE",
                                 f"{spec.code} after an irreversible step; restarting could repeat it", step_id=step_id)
            self.result.recoveries.append(record)
            raise Restart(spec.code, relogin=spec.handle == "relogin_restart")
        raise NeedsHuman("UNEXPECTED_STATE", f"{spec.code} has no handler", step_id=step_id)

    # ----------------------------------------------------------------- humans

    async def _needs_human(self, exc: NeedsHuman, index: int) -> int:
        """Escalate when attended; otherwise fail with the same code and full evidence."""
        if self.rt.pending_dialog is not None and exc.code != "UNKNOWN_DIALOG":
            # Whatever went wrong, a held native dialog is what blocks the page: name it.
            exc = NeedsHuman("UNKNOWN_DIALOG", f"unrecognized dialog: {self.rt.dialog_message!r} ({exc.message})",
                             step_id=exc.step_id, expected=exc.expected)
        if self.options.attended and self.escalator is not None and exc.code in ESCALATABLE:
            return await self._escalate(exc, index)
        raise HardFailure(self._error(
            exc.code, exc.message, exc.step_id, expected=exc.expected, observed=exc.observed, hint=exc.hint,
            evidence=await self._capture_failure(),
        ))

    async def _escalate(self, exc: NeedsHuman, index: int) -> int:
        """Hand the live session to an operator, wait, then resync and return the step to resume at."""
        assert self.escalator is not None
        step = self.capability.steps[index]
        self._escalations[step.id] = self._escalations.get(step.id, 0) + 1
        if self._escalations[step.id] > 2:
            raise HardFailure(self._error(exc.code, f"{exc.message} (still unresolved after two handoffs)", step.id,
                                          evidence=await self._capture_failure()))
        record = InterventionRecord(id="", reason_code=exc.code, reason=exc.message, step_id=exc.step_id or step.id,
                                    opened_at=datetime.now(UTC))
        self.result.status = "needs_intervention"
        self.log.write_json("result.json", self.result.model_dump(mode="json"))  # interim status for pollers
        resolution = await self.escalator.escalate(record, screenshot=await self.rt.screenshot(),
                                                   dialog_message=self.rt.dialog_message)
        self.result.status = "failed"
        self.result.interventions.append(record)
        if resolution.kind == "aborted":
            raise HardFailure(self._error("ESCALATION_ABORTED", f"operator aborted at {step.id}: {resolution.note or ''}",
                                          step.id))
        if resolution.kind == "timed_out":
            raise HardFailure(self._error("ESCALATION_TIMEOUT", f"no operator resolved {exc.code} at {step.id}",
                                          step.id))
        if resolution.kind == "rejected":
            raise HardFailure(self._error("OPERATOR_REJECTED", f"operator rejected at {step.id}", step.id))
        return await self._resync(index, resolution)

    async def _resync(self, index: int, resolution: OperatorResolution) -> int:
        """Work out where the run stands after the operator hands back.

        1. Anything terminal on screen (a business outcome, an error page) wins.
        2. A resume step named by the operator is honored if the step before it held.
        3. Otherwise: if the current step's postcondition already holds, the operator
           did that step: advance. If not, retry it, but only if it is reversible.
        An irreversible step is never skipped unless the operator attests it was done.
        """
        assert self.escalator is not None
        steps = self.capability.steps
        attested = set(resolution.attested_steps)
        self.log.event("resync", at_step=steps[index].id, note=resolution.note, resume_step=resolution.resume_step,
                       attested=sorted(attested))
        await self._settle_interrupts(None, after=False)

        if resolution.resume_step:
            ids = [s.id for s in steps]
            if resolution.resume_step not in ids:
                raise HardFailure(self._error("RESYNC_FAILED", f"unknown resume step {resolution.resume_step!r}", None))
            target = ids.index(resolution.resume_step)
            previous = steps[target - 1] if target > 0 else None
            if previous is not None and previous.expect is not None and not await self.cond.holds(
                    previous.expect, getattr(previous, "target", None)):
                raise HardFailure(self._error("RESYNC_FAILED", f"cannot resume at {resolution.resume_step}: "
                                              f"{previous.id}'s postcondition does not hold", previous.id))
            for skipped in steps[index:target]:
                if skipped.effect == "irreversible" and skipped.id not in attested:
                    raise HardFailure(self._error("RESYNC_FAILED", f"resuming at {resolution.resume_step} would skip "
                                                  f"irreversible step {skipped.id} that nobody attested", skipped.id))
                self._mark_human(skipped, attested)
            self.escalator.resume(f"resumed at {resolution.resume_step} (operator's choice)")
            return target

        step = steps[index]
        if step.expect is not None and await self.cond.holds(step.expect, getattr(step, "target", None)):
            self._mark_human(step, attested | ({step.id} if step.effect == "irreversible" else set()))
            self.escalator.resume(f"{step.id} was completed by the operator")
            return index + 1
        if step.effect == "irreversible" and step.id not in attested:
            raise HardFailure(self._error("RESYNC_FAILED", f"{step.id} is irreversible and its outcome is unknown; "
                                          "not retrying it", step.id, evidence=await self._capture_failure()))
        if step.id in attested:
            self._mark_human(step, attested)
            self.escalator.resume(f"{step.id} attested by the operator")
            return index + 1
        self.escalator.resume(f"retrying {step.id}")
        return index

    def _mark_human(self, step: Step, attested: set[str]) -> None:
        self.completed.append(step.id)
        self.result.steps.append(StepRecord(step_id=step.id, status="performed_by_human"))
        if step.effect == "irreversible" and step.id in attested:
            self.irreversible_done = True
            self.result.commit_state = "committed"

    # ================================================================ evidence

    def _intent(self, step_id: str | None) -> str | None:
        if step_id is None:
            return None
        try:
            return self.capability.step(step_id).intent
        except KeyError:
            return None

    def _error(self, code: str, message: str, step_id: str | None, *, expected: str | None = None,
               observed: dict[str, Any] | None = None, evidence: list[str] | None = None,
               hint: str | None = None) -> ErrorInfo:
        info = TAXONOMY.get(code)
        category: Literal["rejected", "needs_human", "hard_failure"] = "hard_failure"
        if info is not None and info.category == "needs_human":
            category = "needs_human"
        return ErrorInfo(
            code=code, category=category, message=message, step_id=step_id, step_intent=self._intent(step_id),
            expected=expected, observed=observed, evidence=evidence or [], hint=hint,
            retryable=bool(info and info.retryable and not self.irreversible_done),
        )

    async def _observed(self) -> dict[str, Any]:
        return await self.rt.observed()

    async def _step_evidence(self, step: Step) -> None:
        if self.options.screenshots:
            await self.rt.save_screenshot(f"screens/{len(self.result.steps):02d}-{step.id}.png")

    async def _capture_failure(self) -> list[str]:
        return await self.rt.capture_failure()

    def _preview_values(self) -> dict[str, Any]:
        return {name: self.outputs.get(name) for name in self.capability.preview_outputs}

    def _build_preview(self) -> None:
        values = self._preview_values()
        token, expires = commit.issue(
            self._signing_key, capability=self.capability.id, content_hash=self.result.content_hash,
            overlay_hash=self.overlay_hash, tenant=self.tenant.id, inputs=self._inputs, preview=values,
        )
        self.result.preview = PreviewInfo(values=values, commit_token=token,
                                          expires_at=datetime.fromtimestamp(expires, UTC))
        self.log.event("checkpoint", step="preview", condition="stopped before the first irreversible step",
                       held=True)

    def _check_preview(self, step: Step) -> None:
        """Commit only what the preview showed: the review screen must read the same as before."""
        assert self._claims is not None
        if commit.digest(self._signing_key, self._preview_values()) != self._claims.preview_digest:
            raise HardFailure(self._error(
                "PREVIEW_MISMATCH", f"the review screen before {step.id} no longer matches the preview; nothing "
                "was committed. Run the preview again and confirm the new values.", step.id,
            ))
        self.log.event("policy_decision", step=step.id, rule="commit_token", allowed=True,
                       reason="review values match the preview the token approved")


async def replay(
    workspace: Workspace,
    capability_id: str,
    tenant_id: str,
    inputs: dict[str, Any],
    options: ReplayOptions | None = None,
    **kwargs: Any,
) -> RunResult:
    try:
        capability, base, overlay = workspace.effective(capability_id, tenant_id)
    except OverlayError as exc:
        now = datetime.now(UTC)
        return RunResult(run_id=new_run_id("replay"), capability=capability_id, version="?", content_hash="?",
                         tenant=tenant_id, status="rejected", started_at=now, finished_at=now, duration_ms=0,
                         error=ErrorInfo(code="OVERLAY_INVALID", category="rejected", message=str(exc)))
    engine = ReplayEngine(
        workspace,
        capability,
        workspace.tenant(tenant_id),
        workspace.profile(capability.product.name),
        workspace.policy(capability.product.name, tenant_id),
        inputs,
        options,
        base=base,
        overlay_hash=overlay.content_hash() if overlay else None,
        **kwargs,
    )
    return await engine.run()
