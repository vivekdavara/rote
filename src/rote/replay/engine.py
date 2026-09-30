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
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from playwright.async_api import Browser, Dialog, Playwright, Route
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel

from rote.control.protocol import Escalator
from rote.evidence.runlog import RunLog, new_run_id
from rote.policy.gate import PolicyGate
from rote.redaction.redactor import Redactor, load_key
from rote.registry.approvals import find_approval, load_approvals
from rote.registry.store import Workspace
from rote.replay.errors import BusinessOutcome, HardFailure, NeedsHuman, PreviewReady, Rejected, Restart
from rote.replay.inputs import validate_inputs
from rote.replay.versions import in_range
from rote.schema.capability import (
    Capability,
    CheckStep,
    ClickStep,
    ExtractStep,
    FillStep,
    PressStep,
    SelectStep,
    Step,
)
from rote.schema.codes import TAXONOMY
from rote.schema.config import InterruptSpec, Policy, ProductProfile, TenantConfig
from rote.schema.result import (
    DriftSignal,
    ErrorInfo,
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
from rote.surface.web.masking import masked_screenshot
from rote.surface.web.observe import observe
from rote.surface.web.observe import render as render_observation
from rote.surface.web.resolver import ResolutionFailure, Resolved, Resolver, element_facts, hit_test
from rote.surface.web.session import BrowserOptions, WebSession, call

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
        run_id: str | None = None,
    ) -> None:
        self.workspace = workspace
        self.capability = capability
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

        self.redactor = Redactor(load_key(workspace.root))
        self.run_id = run_id or new_run_id("replay")
        self.log = RunLog(workspace.runs, self.run_id, self.redactor)
        self.gate = PolicyGate(policy, tenant.template_vars())
        self.result = RunResult(
            run_id=self.run_id,
            capability=capability.id,
            version=capability.version,
            content_hash=capability.content_hash(),
            overlay_hash=overlay_hash,
            tenant=tenant.id,
            status="failed",
            started_at=datetime.now(UTC),
        )
        self.outputs: dict[str, Any] = {}
        self.completed: list[str] = []
        self.irreversible_done = False
        self.current_step: str | None = None
        self.pending_dialog: Dialog | None = None
        self.dialog_message: str | None = None
        self._recovery_counts: dict[tuple[str, str], int] = {}
        self._background: list[asyncio.Future[Any]] = []
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
            async with self._session():
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
            for task in self._background:
                task.cancel()
            self.result.finished_at = datetime.now(UTC)
            self.result.duration_ms = int((time.monotonic() - started) * 1000)
            if self.result.status in ("succeeded", "preview"):
                self.result.outputs = dict(self.outputs)
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
            approval = find_approval(approvals, self.capability, tenant=self.tenant.id, overlay_hash=self.overlay_hash)
            if approval is None:
                raise self._reject(
                    "NOT_APPROVED",
                    f"no approval matches content hash {self.result.content_hash}; review it, then `rote approve`",
                )
            self.log.event("policy_decision", rule="approval", allowed=True, reviewer=approval.reviewer)

        if self.capability.side_effects == "irreversible" and self.options.mode == "run":
            self.options.mode = "preview"
            self.result.warnings.append("irreversible capability: ran in preview mode; committing needs a preview token")

        secrets, missing = resolve_secrets(self.tenant, self.workspace.root)
        if missing:
            raise HardFailure(self._error("LOGIN_FAILED", "missing secrets: " + ", ".join(missing), None))
        self._secrets = secrets

    # ================================================================== session

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[None]:
        browser_options = BrowserOptions(headless=self.options.headless)
        if self.browser is not None:
            web = await WebSession.open(self.browser, browser_options)
        elif self.playwright is not None:
            web = await WebSession.launch(self.playwright, browser_options)
        else:
            raise RuntimeError("ReplayEngine needs a Playwright instance or a browser")
        self.web = web
        context = {"inputs": self._inputs, "tenant": self.tenant.template_vars()}
        self.resolver = Resolver(web, context, self.tenant.labels)
        self.cond = ConditionEvaluator(self.resolver, self.outputs)
        await web.context.route("**/*", self._route)
        web.page.on("dialog", self._on_dialog)
        try:
            yield
        finally:
            await web.close()

    async def _route(self, route: Route) -> None:
        url = route.request.url
        if self.gate.origin_allowed(url):
            await route.continue_()
            return
        self.log.event("network_blocked", url=url, rule="allowed_origins")
        await route.abort("blockedbyclient")

    def _on_dialog(self, dialog: Dialog) -> None:
        message = dialog.message
        rule = next((r for r in self.profile.dialogs if r.message_contains in message), None)
        if rule is not None:
            handler = dialog.accept() if rule.respond == "accept" else dialog.dismiss()
            self._background.append(asyncio.ensure_future(handler))
            step = self.current_step
            self.result.recoveries.append(
                RecoveryRecord(code="KNOWN_DIALOG", step_id=step, detail=f"{rule.respond}: {rule.message_contains}")
            )
            self.log.event("dialog", message=message, handled=rule.respond, rule=rule.message_contains)
            return
        self.dialog_message = message
        self.pending_dialog = dialog
        self.log.event("dialog", message=message, handled="unknown")
        if not (self.options.attended and self.escalator is not None):
            self._background.append(asyncio.ensure_future(dialog.dismiss()))

    # ============================================================ entry & login

    async def _enter(self) -> None:
        await self.web.page.goto(self.tenant.base_url.rstrip("/") + self.profile.entry_path)
        await self._login()
        await self._read_version()

    async def _login(self) -> None:
        context = {"secrets": self._secrets, "tenant": self.tenant.template_vars()}
        login_resolver = Resolver(self.web, context, self.tenant.labels)
        for step in self.profile.login:
            target = getattr(step, "target", None)
            if target is None:
                continue
            try:
                resolved = await login_resolver.resolve(target, self.step_timeout)
            except ResolutionFailure as exc:
                raise HardFailure(self._error("LOGIN_FAILED", f"login step {step.id}: {exc}", None)) from exc
            await self._perform(step, resolved, login_resolver, secret=True)
        if not await self._poll(self.profile.login_success, self.step_timeout):
            raise HardFailure(self._error(
                "LOGIN_FAILED", "sign-on did not reach the home screen", None,
                expected=describe(self.profile.login_success), evidence=await self._capture_failure(),
            ))
        self.log.event("checkpoint", step="login", condition=describe(self.profile.login_success), held=True)

    async def _read_version(self) -> None:
        fingerprint = self.profile.fingerprint
        frame = self.web.frame(fingerprint.frame) if fingerprint else None
        if fingerprint is None or frame is None:
            return
        try:
            text: str = await call(frame, "bodyText")
        except PlaywrightError:
            return
        match = re.search(fingerprint.version_regex, text)
        if match is None:
            self.result.warnings.append("could not read the product version from the screen")
            return
        observed = match.group(1)
        if in_range(observed, self.capability.product.versions):
            self.log.event("note", product_version=observed)
            return
        note = f"product version {observed} is outside the capability's range {self.capability.product.versions}"
        self.result.warnings.append(note)
        self.log.event("drift", kind="product_version", observed=observed, expected=self.capability.product.versions)

    # ================================================================ execution

    async def _execute(self) -> None:
        steps = self.capability.steps
        restarts = 0
        index = 0
        while index < len(steps):
            step = steps[index]
            if step.effect == "irreversible" and self.options.mode == "preview":
                self._build_preview()
                raise PreviewReady
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

    async def _perform(self, step: Step, resolved: Resolved, resolver: Resolver, *, secret: bool = False) -> None:
        element = resolved.element
        detail: dict[str, Any] = {"step": step.id, "action": step.action, "strategy": resolved.strategy,
                                  "frame": resolved.frame.name or "top"}
        try:
            if isinstance(step, ClickStep):
                await actions.click(element)
            elif isinstance(step, FillStep):
                await actions.fill(element, resolver.text(step.value))
                detail["value"] = "[secret]" if secret else step.value  # the template, never the value
            elif isinstance(step, SelectStep):
                detail["matched_by"] = await actions.select(element, resolver.text(step.option))
                detail["option"] = step.option
            elif isinstance(step, CheckStep):
                await actions.set_checked(element, step.checked)
            elif isinstance(step, PressStep):
                await actions.press(element, step.key)
        except (PlaywrightError, actions.OptionNotFound) as exc:
            raise NeedsHuman("UNEXPECTED_STATE", f"step {step.id}: {step.action} failed: {str(exc).splitlines()[0]}",
                             step_id=step.id, observed=await self._observed()) from exc
        self.log.event("action_executed", **detail)

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
        while True:
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
            if loading:
                raise HardFailure(self._error(
                    "LOAD_TIMEOUT", f"step {step.id}: still loading after {self.limits.slow_load_cap_ms} ms",
                    step.id, expected=expected, observed=await self._observed(),
                    evidence=await self._capture_failure(),
                ))
            if step.effect == "irreversible":
                raise NeedsHuman("INDETERMINATE_COMMIT", f"step {step.id} ran but its result was never confirmed",
                                 step_id=step.id, expected=expected, observed=await self._observed())
            raise NeedsHuman("UNEXPECTED_STATE", f"step {step.id}: expected {expected}", step_id=step.id,
                             expected=expected, observed=await self._observed())

    async def _poll(self, condition: BaseModel, timeout_ms: int) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if await self.cond.holds(condition):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

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
        if self.pending_dialog is not None:
            raise NeedsHuman("UNKNOWN_DIALOG", f"unrecognized dialog: {self.dialog_message!r}", step_id=step_id)
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
                raise BusinessOutcome(OutcomeInfo(code=code, message=outcome.description, step_id=step_id))
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
        if self.options.attended and self.escalator is not None and exc.code in ESCALATABLE:
            return await self._escalate(exc, index)
        raise HardFailure(self._error(
            exc.code, exc.message, exc.step_id, expected=exc.expected, observed=exc.observed, hint=exc.hint,
            evidence=await self._capture_failure(),
        ))

    async def _escalate(self, exc: NeedsHuman, index: int) -> int:
        raise HardFailure(self._error(exc.code, exc.message + " (escalation not wired yet)", exc.step_id))

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
        frames = []
        for frame in self.web.page.frames:
            try:
                summary = await call(frame, "summary")
            except PlaywrightError:
                continue
            if frame != self.web.page.main_frame and not (summary["headings"] or summary["messages"]):
                continue
            frames.append({
                "frame": frame.name or "top",
                "path": urlsplit(summary["url"]).path,
                "headings": summary["headings"],
                "messages": summary["messages"],
            })
        return {"frames": frames}

    async def _collect_screen_pii(self) -> None:
        labels = self.profile.redaction.mask_labels
        if not labels:
            return
        for frame in self.web.page.frames:
            try:
                for item in await call(frame, "labeledValues", labels):
                    self.redactor.register(item["text"], re.sub(r"\W+", "_", item["label"].lower()).strip("_"))
            except PlaywrightError:
                continue

    async def _screenshot(self) -> bytes:
        await self._collect_screen_pii()
        return await masked_screenshot(
            self.web.page,
            mask_labels=self.profile.redaction.mask_labels,
            mask_columns=self.profile.redaction.mask_columns,
            sensitive_values=self.redactor.known_values(),
        )

    async def _step_evidence(self, step: Step) -> None:
        if not self.options.screenshots:
            return
        try:
            image = await self._screenshot()
        except PlaywrightError:
            return
        self.log.save_bytes(f"screens/{len(self.result.steps):02d}-{step.id}.png", image)

    async def _capture_failure(self) -> list[str]:
        paths: list[str] = []
        try:
            paths.append(self.log.save_bytes("failure/screenshot.png", await self._screenshot()))
            observation = await observe(self.web.page, tag=False, screenshot=False, max_text=1500)
            await self._collect_screen_pii()
            snapshot = self.log.write_text("failure/snapshot.txt", render_observation(observation))
            paths.append(str(snapshot.relative_to(self.log.dir)))
        except PlaywrightError:
            pass
        return paths

    def _build_preview(self) -> None:
        values = {name: self.outputs.get(name) for name in self.capability.preview_outputs}
        self.result.preview = PreviewInfo(values=values, commit_token="", expires_at=datetime.now(UTC))


async def replay(
    workspace: Workspace,
    capability_id: str,
    tenant_id: str,
    inputs: dict[str, Any],
    options: ReplayOptions | None = None,
    **kwargs: Any,
) -> RunResult:
    capability = workspace.capability(capability_id)
    engine = ReplayEngine(
        workspace,
        capability,
        workspace.tenant(tenant_id),
        workspace.profile(capability.product.name),
        workspace.policy(capability.product.name, tenant_id),
        inputs,
        options,
        **kwargs,
    )
    return await engine.run()
