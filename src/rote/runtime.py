"""What every run shares, whether it is discovery or replay.

* a fresh browser context per run, with the in-page library installed
* the network allowlist, enforced on every request the page makes
* native-dialog handling: known dialogs answered per the product profile;
  unknown ones held open for an operator (attended) or dismissed and reported
* sign-on through the product profile, with secrets resolved at run time
* the product-version fingerprint check
* redacted evidence: the event log, masked screenshots, failure snapshots
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Browser, Dialog, Playwright, Route
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel

from rote.evidence.runlog import RunLog
from rote.policy.gate import PolicyGate
from rote.redaction.redactor import Redactor, load_key
from rote.registry.store import Workspace
from rote.replay.errors import HardFailure
from rote.replay.versions import in_range
from rote.schema.capability import CheckStep, ClickStep, FillStep, PressStep, SelectStep, Step
from rote.schema.codes import TAXONOMY
from rote.schema.config import Policy, ProductProfile, TenantConfig
from rote.schema.result import ErrorInfo
from rote.surface.web import actions
from rote.surface.web.conditions import ConditionEvaluator, describe
from rote.surface.web.masking import masked_screenshot
from rote.surface.web.observe import observe
from rote.surface.web.observe import render as render_observation
from rote.surface.web.resolver import ResolutionFailure, Resolved, Resolver
from rote.surface.web.session import BrowserOptions, WebSession, call


def make_error(code: str, message: str, *, step_id: str | None = None, expected: str | None = None,
               evidence: list[str] | None = None) -> ErrorInfo:
    info = TAXONOMY.get(code)
    category = "needs_human" if info is not None and info.category == "needs_human" else "hard_failure"
    return ErrorInfo(code=code, category=category, message=message, step_id=step_id, expected=expected,  # type: ignore[arg-type]
                     evidence=evidence or [], retryable=bool(info and info.retryable))


class Runtime:
    def __init__(
        self,
        workspace: Workspace,
        tenant: TenantConfig,
        profile: ProductProfile,
        policy: Policy,
        *,
        run_id: str,
        hold_unknown_dialogs: bool = False,
    ) -> None:
        self.workspace = workspace
        self.tenant = tenant
        self.profile = profile
        self.policy = policy
        self.limits = policy.limits
        self.redactor = Redactor(load_key(workspace.root))
        self.log = RunLog(workspace.runs, run_id, self.redactor)
        self.gate = PolicyGate(policy, tenant.template_vars())
        self.hold_unknown_dialogs = hold_unknown_dialogs
        self.pending_dialog: Dialog | None = None
        self.dialog_message: str | None = None
        self.on_known_dialog: Callable[[str, str], None] | None = None
        self.warnings: list[str] = []
        self._background: list[asyncio.Future[Any]] = []
        self.web: WebSession

    # ------------------------------------------------------------------ session

    @asynccontextmanager
    async def session(
        self, *, playwright: Playwright | None = None, browser: Browser | None = None, headless: bool = True
    ) -> AsyncIterator[WebSession]:
        options = BrowserOptions(headless=headless)
        if browser is not None:
            web = await WebSession.open(browser, options)
        elif playwright is not None:
            web = await WebSession.launch(playwright, options)
        else:
            raise RuntimeError("a run needs a Playwright instance or a browser")
        self.web = web
        await web.context.route("**/*", self._route)
        web.page.on("dialog", self._on_dialog)
        try:
            yield web
        finally:
            for task in self._background:
                task.cancel()
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
            self.log.event("dialog", message=message, handled=rule.respond, rule=rule.message_contains)
            if self.on_known_dialog is not None:
                self.on_known_dialog(message, rule.respond)
            return
        self.dialog_message = message
        self.pending_dialog = dialog
        self.log.event("dialog", message=message, handled="held" if self.hold_unknown_dialogs else "dismissed")
        if not self.hold_unknown_dialogs:
            self._background.append(asyncio.ensure_future(dialog.dismiss()))

    async def answer_dialog(self, accept: bool) -> None:
        """Operator's answer to a held dialog."""
        dialog, self.pending_dialog = self.pending_dialog, None
        self.dialog_message = None
        if dialog is not None:
            await (dialog.accept() if accept else dialog.dismiss())

    # ------------------------------------------------------------ entry & login

    async def enter(self, secrets: dict[str, str], *, step_timeout_ms: int, versions: str | None) -> None:
        url = self.tenant.base_url.rstrip("/") + self.profile.entry_path
        try:
            await self.web.page.goto(url)
        except PlaywrightError as exc:
            raise HardFailure(make_error("APP_UNREACHABLE", f"{url}: {str(exc).splitlines()[0]}")) from exc
        context = {"secrets": secrets, "tenant": self.tenant.template_vars()}
        resolver = Resolver(self.web, context, self.tenant.labels)
        for step in self.profile.login:
            target = getattr(step, "target", None)
            if target is None:
                continue
            try:
                resolved = await resolver.resolve(target, step_timeout_ms)
            except ResolutionFailure as exc:
                raise HardFailure(make_error("LOGIN_FAILED", f"login step {step.id}: {exc}")) from exc
            await self.perform(step, resolved, resolver, secret=True)
        evaluator = ConditionEvaluator(resolver, {})
        if not await self.poll(evaluator, self.profile.login_success, step_timeout_ms):
            raise HardFailure(make_error(
                "LOGIN_FAILED", "sign-on did not reach the home screen",
                expected=describe(self.profile.login_success), evidence=await self.capture_failure(),
            ))
        self.log.event("checkpoint", step="login", condition=describe(self.profile.login_success), held=True)
        if versions is not None:
            await self._read_version(versions)

    async def _read_version(self, versions: str) -> None:
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
            self.warnings.append("could not read the product version from the screen")
            return
        observed = match.group(1)
        if in_range(observed, versions):
            self.log.event("note", product_version=observed)
            return
        self.warnings.append(f"product version {observed} is outside the capability's range {versions}")
        self.log.event("drift", kind="product_version", observed=observed, expected=versions)

    # --------------------------------------------------------------------- act

    async def perform(self, step: Step, resolved: Resolved, resolver: Resolver, *, secret: bool = False) -> None:
        """Run one action. Raises Playwright errors / OptionNotFound to the caller to classify."""
        element = resolved.element
        detail: dict[str, Any] = {"step": step.id, "action": step.action, "strategy": resolved.strategy,
                                  "frame": resolved.frame.name or "top"}
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
        self.log.event("action_executed", **detail)

    async def poll(self, evaluator: ConditionEvaluator, condition: BaseModel, timeout_ms: int) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if await evaluator.holds(condition):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def settle(self, quiet_ms: int = 300, timeout_ms: int = 8000) -> None:
        """Wait until no frame is loading for ``quiet_ms`` (used between discovery steps)."""
        deadline = time.monotonic() + timeout_ms / 1000
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            if self.web.navigating:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif (time.monotonic() - quiet_since) * 1000 >= quiet_ms:
                return
            await asyncio.sleep(0.05)

    # ---------------------------------------------------------------- evidence

    async def observed(self) -> dict[str, Any]:
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

    async def collect_screen_pii(self) -> None:
        labels = self.profile.redaction.mask_labels
        if not labels:
            return
        for frame in self.web.page.frames:
            try:
                for item in await call(frame, "labeledValues", labels):
                    self.redactor.register(item["text"], re.sub(r"\W+", "_", item["label"].lower()).strip("_"))
            except PlaywrightError:
                continue

    async def screenshot(self) -> bytes | None:
        """Masked screenshot, or None while a native dialog blocks the page (Chromium cannot capture then)."""
        if self.pending_dialog is not None:
            return None
        await self.collect_screen_pii()
        return await masked_screenshot(
            self.web.page,
            mask_labels=self.profile.redaction.mask_labels,
            mask_columns=self.profile.redaction.mask_columns,
            sensitive_values=self.redactor.known_values(),
        )

    async def save_screenshot(self, name: str) -> str | None:
        try:
            image = await self.screenshot()
        except PlaywrightError:
            return None
        return self.log.save_bytes(name, image) if image is not None else None

    async def capture_failure(self) -> list[str]:
        paths: list[str] = []
        try:
            image = await self.screenshot()
            if image is not None:
                paths.append(self.log.save_bytes("failure/screenshot.png", image))
            if self.pending_dialog is not None:
                return paths  # the page cannot be read while a native dialog blocks it
            observation = await observe(self.web.page, tag=False, screenshot=False, max_text=1500)
            snapshot = self.log.write_text("failure/snapshot.txt", render_observation(observation))
            paths.append(str(snapshot.relative_to(self.log.dir)))
        except PlaywrightError:
            pass
        return paths
