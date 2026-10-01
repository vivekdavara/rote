"""The discovery loop and everything after it.

Loop: observe -> stop-condition checks -> the planner decides one action ->
policy gate (an irreversible action needs an operator's approval) -> recorder
builds and validates locators *before* acting -> act -> settle -> snapshot.
It runs until the planner calls ``finish`` or a stop condition fires: step
budget, wall clock, no progress, oscillation, repeated errors, an unknown
dialog, or a request for a human.

After the loop, still in discovery:

1. compile the trace into a draft capability and lint it for regulated data
2. replay the draft on the spec's *second* example (proves parameterization)
3. replay each negative example. The message the app shows becomes that
   outcome's detector only if it never appears on a happy-path screen and a
   second replay then reports exactly that business outcome
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from playwright.async_api import Browser, Playwright
from playwright.async_api import Error as PlaywrightError

from rote.control.protocol import Escalator
from rote.discovery.cassette import Cassette, CassetteEntry, bind, binding_for, templated
from rote.discovery.compiler import CompileError, compile_trace, template_text, with_outcome
from rote.discovery.planner import Decision, Planner, PlannerRequest
from rote.discovery.prompts import goal_block, step_block
from rote.discovery.recorder import ScreenState, TraceStep, build_candidates
from rote.evidence.runlog import new_run_id
from rote.redaction.lint import lint_capability
from rote.registry.store import Workspace
from rote.replay.engine import ReplayEngine, ReplayOptions
from rote.replay.errors import HardFailure
from rote.runtime import Runtime
from rote.schema.capability import Capability, save_capability
from rote.schema.condition import TextVisible
from rote.schema.result import InterventionRecord, RunResult
from rote.schema.spec import GoalSpec
from rote.schema.templating import render
from rote.secrets import resolve_secrets
from rote.surface.web import actions
from rote.surface.web.observe import Observation, describe_element, observe
from rote.surface.web.observe import render as render_observation
from rote.surface.web.resolver import Resolver

TOOL_ACTION = {"click": "click", "type_text": "fill", "select_option": "select", "set_checkbox": "check",
               "press_key": "press", "extract": "extract"}


@dataclass
class DiscoveryOptions:
    attended: bool = False
    headless: bool = True
    max_steps: int | None = None
    verify: bool = True
    learn_outcomes: bool = True
    save: bool = True


@dataclass
class OutcomeLearning:
    code: str
    status: Literal["learned", "already_declared", "not_reproduced", "rejected"]
    detail: str
    after_step: str | None = None
    detector: str | None = None


@dataclass
class DiscoveryResult:
    run_id: str
    status: Literal["compiled", "failed"]
    reason: str | None = None
    capability: Capability | None = None
    capability_path: Path | None = None
    cassette_path: Path | None = None
    verification: RunResult | None = None
    outcomes: list[OutcomeLearning] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    steps_taken: int = 0
    usage: dict[str, Any] = field(default_factory=dict)


class DiscoveryStop(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DiscoveryAgent:
    def __init__(
        self,
        workspace: Workspace,
        spec: GoalSpec,
        planner: Planner,
        options: DiscoveryOptions | None = None,
        *,
        playwright: Playwright | None = None,
        browser: Browser | None = None,
        escalator: Escalator | None = None,
        run_id: str | None = None,
    ) -> None:
        self.workspace = workspace
        self.spec = spec
        self.planner = planner
        self.options = options or DiscoveryOptions()
        self.playwright = playwright
        self.browser = browser
        self.escalator = escalator
        self.tenant = workspace.tenant(spec.tenant)
        self.profile = workspace.profile(spec.product)
        self.policy = workspace.policy(spec.product, spec.tenant)
        self.run_id = run_id or new_run_id("discover")
        self.rt = Runtime(workspace, self.tenant, self.profile, self.policy, run_id=self.run_id,
                          hold_unknown_dialogs=self.options.attended and escalator is not None)
        self.log = self.rt.log
        bind = getattr(escalator, "bind", None)
        if bind is not None:
            bind(self.rt, [])
        self.inputs = spec.example(0)
        for spec_input in spec.inputs.values():
            if spec_input.sensitive:
                for value in spec_input.examples:
                    self.rt.redactor.register(value, spec_input.sensitive)
        for negative in spec.negative_examples:
            for name, value in negative.inputs.items():
                sensitive = spec.inputs.get(name)
                if sensitive and sensitive.sensitive:
                    self.rt.redactor.register(value, sensitive.sensitive)
        self.trace: list[TraceStep] = []
        self.history: list[str] = []
        self.model_notes: str | None = None
        self.entries: list[CassetteEntry] = []
        self.outputs: dict[str, Any] = {}
        self.usage: dict[str, Any] = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "calls": 0}
        self.served_by: list[str] = []
        self.start: ScreenState | None = None
        self._human_steps = 0
        self.resolver: Resolver

    # ============================================================== entry point

    async def run(self) -> DiscoveryResult:
        result = DiscoveryResult(run_id=self.run_id, status="failed")
        self.log.event("run_started", kind="discovery", capability=self.spec.capability_id, tenant=self.tenant.id,
                       planner=self.planner.name, model=self.planner.model, attended=self.options.attended)
        try:
            secrets, missing = resolve_secrets(self.tenant, self.workspace.root)
            if missing:
                raise DiscoveryStop("LOGIN_FAILED", "missing secrets: " + ", ".join(missing))
            async with self.rt.session(playwright=self.playwright, browser=self.browser,
                                       headless=self.options.headless) as web:
                self.resolver = Resolver(web, {"inputs": self.inputs, "tenant": self.tenant.template_vars()},
                                         self.tenant.labels)
                await self.rt.enter(secrets, step_timeout_ms=self.policy.limits.step_timeout_ms,
                                    versions=self.spec.product_versions)
                await self.rt.settle()
                self.start = ScreenState.of(await observe(web.page, tag=False, screenshot=False))
                await self._loop()
            result.steps_taken = len(self.trace)
            await self._finish(result)
        except DiscoveryStop as stop:
            result.reason = f"{stop.code}: {stop.message}"
            self.log.event("note", stop=stop.code, message=stop.message)
        except HardFailure as failure:
            result.reason = f"{failure.error.code}: {failure.error.message}"
        except CompileError as exc:
            result.reason = f"COMPILE_FAILED: {exc}"
        finally:
            result.usage = dict(self.usage)
            result.steps_taken = result.steps_taken or len(self.trace)
            self._write_evidence(result)
            self.log.event("run_finished", status=result.status, reason=result.reason, steps=result.steps_taken)
        return result

    # =================================================================== loop

    async def _loop(self) -> None:
        page = self.rt.web.page
        budget = self.options.max_steps or self.policy.limits.max_steps
        deadline = time.monotonic() + self.policy.limits.max_seconds
        goal_text = goal_block(self.spec.goal_text(),
                               {n: {"example": s.examples[0], "description": s.description or n}
                                for n, s in self.spec.inputs.items()},
                               {n: o.type for n, o in self.spec.outputs.items()})
        errors = 0
        seen: list[tuple[Any, ...]] = []
        for step in range(1, budget + 1):
            if time.monotonic() > deadline:
                raise DiscoveryStop("TIME_BUDGET", f"no finish within {self.policy.limits.max_seconds} s")
            if self.rt.pending_dialog is not None:
                await self._escalate(f"an unrecognized dialog is open: {self.rt.dialog_message!r}", "unexpected_screen")
                continue
            await self.rt.settle()
            observation = await observe(page)
            state = ScreenState.of(observation)
            seen.append((state.fingerprint, tuple(sorted((k, str(v)) for k, v in state.values.items()))))
            if self._stuck(seen):
                await self._escalate("no progress: the last actions left the screen unchanged or oscillating",
                                     "stuck")
                seen.clear()
                continue

            assert observation.screenshot is not None
            prompt = step_block(self.history, self.model_notes, render_observation(observation), step, budget)
            decision = await self.planner.decide(PlannerRequest(step, goal_text, prompt, observation.screenshot,
                                                                observation))
            self._account(decision)
            self.log.event("decision", step=step, tool=decision.tool, served_by=decision.served_by,
                           rationale=decision.args.get("rationale"), expect=decision.args.get("expect"),
                           note=decision.note)

            if decision.tool == "finish":
                missing = [name for name in self.spec.outputs if name not in self.outputs]
                if missing:
                    errors += 1
                    self.history.append(f"{step}. finish rejected: extract {missing} first")
                    continue
                self._record_entry(step, decision, None, observation)
                return
            if decision.tool == "request_human":
                self._record_entry(step, decision, None, observation)
                await self._escalate(str(decision.args.get("reason")), str(decision.args.get("category")))
                continue
            if decision.tool in ("refused", "no_tool"):
                errors += 1
                self.history.append(f"{step}. no action ({decision.tool}: {decision.note})")
            elif decision.tool == "wait":
                await asyncio.sleep(1.5)
                self.history.append(f"{step}. waited for the application to load")
                self._record_entry(step, decision, None, observation)
                continue
            else:
                trace_step = await self._act(step, decision, observation, state)
                errors = errors + 1 if trace_step.result.startswith(("error", "blocked")) else 0
            if errors >= 3:
                await self._escalate("three actions in a row failed or were blocked", "stuck")
                errors = 0
        raise DiscoveryStop("STEP_BUDGET", f"no finish within {budget} steps")

    def _stuck(self, seen: list[tuple[Any, ...]]) -> bool:
        acted = [t for t in self.trace if t.tool != "extract"]
        if len(seen) >= 3 and seen[-1] == seen[-2] == seen[-3] and len(acted) >= 2:
            return True
        return len(seen) >= 4 and seen[-1] == seen[-3] and seen[-2] == seen[-4] and seen[-1] != seen[-2]

    def _account(self, decision: Decision) -> None:
        for key in ("input", "cache_read", "cache_write", "output", "calls"):
            self.usage[key] += int(decision.usage.get(key, 0) or 0)
        if decision.served_by:
            self.served_by.append(decision.served_by)

    # ------------------------------------------------------------------- act

    async def _act(self, step: int, decision: Decision, observation: Observation, before: ScreenState) -> TraceStep:
        tool, args = decision.tool, decision.args
        trace = TraceStep(index=step, tool=tool, args=args, rationale=args.get("rationale"),
                          expect=args.get("expect"), before=before)
        self.trace.append(trace)
        self.model_notes = args.get("notes")
        ref = str(args.get("ref") or "")
        found = observation.element(ref)
        if tool not in TOOL_ACTION:
            trace.result = f"error: unknown tool {tool}"
            self.history.append(f"{step}. {trace.result}")
            return trace
        if found is None:
            trace.result = f"error: there is no element {ref!r} on this screen"
            self.history.append(f"{step}. {tool} -> {trace.result}")
            return trace
        frame_view, facts = found
        frame = self.rt.web.frame(frame_view.name)
        element = None
        if frame is not None:
            try:
                element = await frame.locator(f'[data-rote-ref="{ref}"]').element_handle(timeout=2000)
            except PlaywrightError:
                element = None
        if frame is None or element is None:
            trace.result = f"error: element {ref!r} disappeared before the action"
            self.history.append(f"{step}. {tool} -> {trace.result}")
            return trace
        trace.frame, trace.facts = frame_view.name, facts
        described = f"{describe_element(facts)} in frame {frame_view.name or 'top'}"

        action = TOOL_ACTION[tool]
        effect = self.rt.gate.classify(facts, frame.url) if action in ("click", "press") else "read_only"
        gate = self.rt.gate.check_action(action, facts, frame.url, effect)
        self.log.event("policy_decision", step=step, action=action, allowed=gate.allowed, rule=gate.rule,
                       reason=gate.reason, runtime_effect=effect)
        binding = self._binding(facts, frame_view.name, ref, observation)
        if not gate.allowed:
            trace.result = f"blocked by policy ({gate.rule}): {gate.reason}"
            self.history.append(f"{step}. {tool} {described} -> {trace.result}")
            self._record_entry(step, decision, binding, observation)
            return trace
        trace.effect = effect
        if effect == "irreversible":
            trace.approval = await self._approve(trace, decision, described)

        trace.candidates = await build_candidates(self.resolver, frame, element, ref, facts, self.inputs)
        try:
            if tool == "click":
                await actions.click(element)
            elif tool == "type_text":
                await actions.fill(element, render(str(args["text"]), {"inputs": self.inputs}))
            elif tool == "select_option":
                await actions.select(element, render(str(args["option"]), {"inputs": self.inputs}))
            elif tool == "set_checkbox":
                await actions.set_checked(element, bool(args["checked"]))
            elif tool == "press_key":
                await actions.press(element, str(args["key"]))
            elif tool == "extract":
                trace.result = await self._extract(trace, element, str(args.get("output_name")))
        except (PlaywrightError, actions.OptionNotFound) as exc:
            trace.result = "error: " + str(exc).splitlines()[0]
        if trace.result == "pending":
            trace.result = "ok"
        await self.rt.settle()
        trace.after = ScreenState.of(await observe(self.rt.web.page, tag=False, screenshot=False))
        await self.rt.save_screenshot(f"screens/{step:02d}-{tool}.png")

        expect = trace.expect
        expect_note = ""
        if expect:
            expect_note = f"; expected {expect!r}: {'appeared' if trace.after.shows(expect) else 'did not appear'}"
        self.history.append(f"{step}. {tool} {described} -> {trace.result}{expect_note}")
        self.log.event("action_executed", step=step, tool=tool, target=described, result=trace.result,
                       locators=[c.by for c in trace.candidates], effect=effect, approval=trace.approval)
        self._record_entry(step, decision, binding, observation)
        return trace

    async def _extract(self, trace: TraceStep, element: Any, name: str) -> str:
        if name not in self.spec.outputs:
            return f"error: {name!r} is not one of the requested outputs {list(self.spec.outputs)}"
        raw = await actions.read(element)
        spec = self.spec.outputs[name]
        try:
            value = actions.parse(raw, spec.type)
        except actions.ParseError as exc:
            return f"error: the value does not parse as {spec.type}: {exc}"
        if spec.sensitive:
            if spec.type == "money":
                self.rt.redactor.register_money(raw, spec.sensitive)
            else:
                self.rt.redactor.register(raw, spec.sensitive)
        self.outputs[name] = value
        trace.extracted = raw
        return "extracted"

    def _binding(self, facts: dict[str, Any], frame: str | None, ref: str, observation: Observation) -> dict[str, Any]:
        binding = binding_for(facts, frame, 0, self.inputs)
        for nth in range(0, 50):
            binding["nth"] = nth
            hit = bind(binding, observation, self.inputs)
            if hit is None or hit == ref:
                break
        return binding

    def _record_entry(self, step: int, decision: Decision, binding: dict[str, Any] | None,
                      observation: Observation) -> None:
        args = {k: v for k, v in decision.args.items() if k != "ref"}
        for key in ("text", "option"):
            if isinstance(args.get(key), str):
                args[key] = template_text(args[key], self.inputs)
        for key in ("rationale", "notes", "expect", "reason", "success_description"):
            if isinstance(args.get(key), str):
                args[key] = self.rt.redactor.text(template_text(args[key], self.inputs))
        approval = self.trace[-1].approval if self.trace and self.trace[-1].index == step else None
        self.entries.append(CassetteEntry(step=step, tool=decision.tool, args=args, target=binding,
                                          fingerprint=observation.fingerprint, served_by=decision.served_by,
                                          approval=approval))  # type: ignore[arg-type]

    # ---------------------------------------------------------------- humans

    async def _approve(self, trace: TraceStep, decision: Decision, described: str) -> str:
        if self.policy.on_irreversible.discovery == "block":
            raise DiscoveryStop("POLICY_BLOCKED", f"irreversible actions are blocked during discovery: {described}")
        if decision.approval == "approved":
            self.log.event("policy_decision", step=trace.index, rule="approval", allowed=True,
                           reason="operator approval replayed from the cassette")
            return "approved"
        if decision.approval == "rejected":
            raise DiscoveryStop("OPERATOR_REJECTED", f"the operator rejected {described}")
        if self.options.attended and self.escalator is not None:
            record = InterventionRecord(id="", reason_code="APPROVAL_REQUIRED", step_id=str(trace.index),
                                        reason=f"approve the irreversible action: {described}",
                                        opened_at=datetime.now(UTC))
            resolution = await self.escalator.escalate(record, screenshot=await self.rt.screenshot(),
                                                       dialog_message=None)
            if resolution.kind == "approved":
                self.log.event("policy_decision", step=trace.index, rule="approval", allowed=True,
                               reason="approved by the operator", operator=resolution.operator)
                return "approved"
            raise DiscoveryStop("OPERATOR_REJECTED", f"the operator did not approve {described}")
        raise DiscoveryStop("APPROVAL_REQUIRED",
                            f"the next action is irreversible ({described}); rerun with --attended so an operator can "
                            "approve it")

    async def _escalate(self, reason: str, category: str) -> None:
        if not (self.options.attended and self.escalator is not None):
            raise DiscoveryStop("NEEDS_HUMAN", f"{category}: {reason}")
        await self._hand_to_operator(reason, category)

    async def _hand_to_operator(self, reason: str, category: str) -> None:
        """Give the live session to an operator. What they do through the relay becomes human steps."""
        assert self.escalator is not None
        page = self.rt.web.page
        pending: list[TraceStep] = []

        async def before_human(kind: str, detail: dict[str, Any]) -> None:
            # Runs before the relay dispatches the input, so locators are validated on the
            # same screen the operator sees, as with the model's actions.
            observation = await observe(page, tag=True, screenshot=False)
            facts = detail.get("facts") or {}
            frame = self.rt.web.frame(None if facts.get("frame") in (None, "top") else facts["frame"])
            if frame is None or not facts.get("role"):
                return
            script = ("([x, y]) => { const e = document.elementFromPoint(x, y); return e && "
                      "(e.closest('a[href],button,input,select,textarea,[onclick]') || e); }") if kind == "click" \
                else "() => document.activeElement"
            local = await self._local_point(frame, detail) if kind == "click" else None
            handle = await frame.evaluate_handle(script, local) if kind == "click" else \
                await frame.evaluate_handle(script)
            element = handle.as_element()
            if element is None:
                return
            ref = await element.get_attribute("data-rote-ref") or ""
            self._human_steps += 1
            step = TraceStep(index=1000 + self._human_steps, tool="click" if kind == "click" else "type_text",
                             args={"text": detail.get("text", "")} if kind == "type" else {},
                             rationale="Performed by the operator.", provenance="human",
                             frame=facts.get("frame") if facts.get("frame") != "top" else None, facts=facts,
                             before=ScreenState.of(observation))
            step.effect = self.rt.gate.classify(facts, frame.url) if kind == "click" else "read_only"
            step.candidates = await build_candidates(self.resolver, frame, element, ref, facts, self.inputs) if ref \
                else []
            pending.append(step)

        setattr(self.escalator, "before_human_action", before_human)  # noqa: B010 - optional ControlPlane hook
        record = InterventionRecord(id="", reason_code="NEEDS_HUMAN", reason=f"{category}: {reason}",
                                    step_id=str(len(self.trace)), opened_at=datetime.now(UTC))
        try:
            resolution = await self.escalator.escalate(record, screenshot=await self.rt.screenshot(),
                                                       dialog_message=self.rt.dialog_message)
        finally:
            setattr(self.escalator, "before_human_action", None)  # noqa: B010
        if resolution.kind != "handed_back":
            raise DiscoveryStop("ESCALATION_" + resolution.kind.upper(), f"the operator did not hand back ({reason})")
        await self.rt.settle()
        final = ScreenState.of(await observe(page, tag=False, screenshot=False))
        for i, step in enumerate(pending):
            step.after = pending[i + 1].before if i + 1 < len(pending) else final
            step.result = "ok"
        self.trace.extend(pending)
        self.history.append(f"An operator took over ({category}: {reason}) and performed "
                            f"{len(resolution.human_actions)} action(s). Their note: {resolution.note or 'none'}. "
                            "Continue from the current screen.")
        self.escalator.resume("operator handed back during discovery")

    async def _local_point(self, frame: Any, detail: dict[str, Any]) -> list[float]:
        x, y = float(detail["x"]), float(detail["y"])
        if frame == self.rt.web.page.main_frame:
            return [x, y]
        box = await (await frame.frame_element()).bounding_box()
        return [x - box["x"], y - box["y"]] if box else [x, y]

    # ============================================================ after the loop

    async def _finish(self, result: DiscoveryResult) -> None:
        assert self.start is not None
        compiled = compile_trace(self.spec, self.trace, start=self.start, run_id=self.run_id,
                                 model=self.planner.model, served_by=self.served_by, tenant=self.tenant.id)
        capability = self._clean(compiled.capability)
        result.notes.extend(compiled.notes)
        result.capability = capability
        if self.options.save:
            result.capability_path = save_capability(capability, self.workspace.capability_path(capability.id))
        if self.options.verify:
            result.verification = await self._replay(capability, self.spec.example(1), "verify")
            self.log.event("checkpoint", step="verify-replay", status=result.verification.status,
                           code=result.verification.error.code if result.verification.error else None)
            if result.verification.status not in ("succeeded", "preview"):
                result.reason = "VERIFY_FAILED: the compiled artifact did not replay on the second example"
                return
        if self.options.learn_outcomes:
            capability = await self._learn_outcomes(capability, result)
            result.capability = capability
            if self.options.save:
                result.capability_path = save_capability(capability, self.workspace.capability_path(capability.id))
        result.status = "compiled"

    def _clean(self, capability: Capability) -> Capability:
        """Intents come from the model's rationale; drop any that would leak data, then lint strictly."""
        known = self.rt.redactor.known_values()
        data = capability.model_dump(mode="json", by_alias=True, exclude_none=True)
        for step in data["steps"]:
            intent = step.get("intent") or ""
            if lint_capability_text(intent, known):
                step["intent"] = None
        cleaned = Capability.model_validate(data)
        problems = lint_capability(cleaned, known)
        if problems:
            raise CompileError("artifact lint failed: " + "; ".join(problems))
        return cleaned

    async def _replay(self, capability: Capability, inputs: dict[str, str], label: str) -> RunResult:
        engine = ReplayEngine(
            self.workspace, capability, self.tenant, self.profile, self.policy, inputs,
            ReplayOptions(require_approval=False, headless=self.options.headless,
                          mode="preview" if capability.side_effects == "irreversible" else "run"),
            playwright=self.playwright, browser=self.browser, run_id=f"{self.run_id}-{label}",
        )
        return await engine.run()

    async def _learn_outcomes(self, capability: Capability, result: DiscoveryResult) -> Capability:
        happy = [s for s in [self.start, *(t.before for t in self.trace), *(t.after for t in self.trace)] if s]
        step_ids = [s.id for s in capability.steps]
        for index, negative in enumerate(self.spec.negative_examples, 1):
            if negative.code in capability.outcomes:
                result.outcomes.append(OutcomeLearning(negative.code, "already_declared", "declared already"))
                continue
            run = await self._replay(capability, negative.inputs, f"negative-{index}")
            if run.status == "business_outcome" and run.outcome is not None:
                result.outcomes.append(OutcomeLearning(negative.code, "already_declared",
                                                       f"covered by {run.outcome.code}"))
                continue
            error = run.error
            if run.status != "failed" or error is None or error.code not in (
                    "UNEXPECTED_STATE", "TARGET_NOT_FOUND", "TARGET_AMBIGUOUS"):
                result.outcomes.append(OutcomeLearning(negative.code, "not_reproduced",
                                                       f"replay ended {run.status} {error.code if error else ''}"))
                continue
            messages = [(f["frame"], m) for f in (error.observed or {}).get("frames", []) for m in f["messages"]]
            if not messages or error.step_id not in step_ids:
                result.outcomes.append(OutcomeLearning(negative.code, "not_reproduced", "no message on screen"))
                continue
            frame, message = messages[0]
            if any(state.shows(message) for state in happy):
                result.outcomes.append(OutcomeLearning(negative.code, "rejected",
                                                       "the message also appears on a happy-path screen"))
                continue
            failed_at = step_ids.index(error.step_id)
            after_step = error.step_id if error.code == "UNEXPECTED_STATE" else step_ids[max(failed_at - 1, 0)]
            detector = TextVisible(text_visible=template_text(message, negative.inputs), frame=None if frame == "top"
                                   else frame)
            candidate = with_outcome(capability, negative.code, negative.description, after_step, detector)
            confirm = await self._replay(candidate, negative.inputs, f"negative-{index}-confirm")
            if confirm.status == "business_outcome" and confirm.outcome and confirm.outcome.code == negative.code:
                capability = candidate
                result.outcomes.append(OutcomeLearning(negative.code, "learned", "detector fires here and nowhere "
                                                       "on the happy path", after_step, detector.text_visible))
            else:
                result.outcomes.append(OutcomeLearning(negative.code, "rejected",
                                                       f"confirmation replay ended {confirm.status}"))
        return capability

    # -------------------------------------------------------------- evidence

    def _write_evidence(self, result: DiscoveryResult) -> None:
        cassette = Cassette(capability_id=self.spec.capability_id, tenant=self.tenant.id, model=self.planner.model,
                            recorded_at=datetime.now(UTC).replace(microsecond=0),
                            source=f"{self.planner.name} planner", entries=self.entries)
        result.cassette_path = cassette.save(self.log.dir / "cassette.json")
        trace = [{
            "step": t.index, "tool": t.tool, "frame": t.frame, "result": t.result, "effect": t.effect,
            "provenance": t.provenance, "approval": t.approval, "expect": t.expect,
            "rationale": t.rationale, "locators": [c.model_dump(mode="json", exclude_none=True) for c in t.candidates],
            "target": templated(describe_element(t.facts), self.inputs) if t.facts else None,
        } for t in self.trace]
        self.log.write_json("trace.json", trace)
        self.log.write_json("discovery.json", {
            "run_id": result.run_id, "status": result.status, "reason": result.reason,
            "capability_path": str(result.capability_path) if result.capability_path else None,
            "content_hash": result.capability.content_hash() if result.capability else None,
            "verification": result.verification.model_dump(mode="json") if result.verification else None,
            "outcomes": [o.__dict__ for o in result.outcomes], "notes": result.notes,
            "steps_taken": result.steps_taken, "usage": result.usage, "served_by": sorted(set(self.served_by)),
            "planner": self.planner.name, "model": self.planner.model,
        })


def lint_capability_text(text: str, known: list[str]) -> bool:
    from rote.redaction.detectors import find

    return any(v and len(v) >= 3 and v in text for v in known) or bool(find(text))


def summary_json(result: DiscoveryResult) -> str:
    return json.dumps({"status": result.status, "reason": result.reason, "path": str(result.capability_path)},
                      indent=2)
