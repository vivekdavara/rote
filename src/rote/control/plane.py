"""The control plane: lease, interventions, and the operator console for one run.

The console is served from the run process itself, because that process holds
the live browser session. An operator gets:

* the intervention: which capability and step, why it stopped, and a pending
  native dialog if there is one
* a live view of the *same* page the automation was driving, refreshed every second
* an input relay: clicks, typing, keys and scrolling on the live view are
  replayed into that page, but only while the operator holds the lease
* Take control / Hand back (with a note, an optional resume step, and "I
  completed step X" attestations) / Approve / Reject / Abort

The relay is the only enforced way a human acts. That is what makes the lease
real: input from anywhere else cannot be stopped, but it also cannot be
recorded or blamed on the right party. Every relayed action is logged with
facts about the element under the pointer.

Production would put the same lease protocol behind a session-broker service,
a CDP screencast or WebRTC stream, SSO and roles. That design is in REPORT.md;
this is the minimal real version.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame

from rote.control.lease import ControlLease, LeaseError, LeaseState, Transition
from rote.control.protocol import OperatorResolution
from rote.runtime import Runtime
from rote.schema.result import HumanAction, InterventionRecord
from rote.surface.web.session import call

CONSOLE_HTML = (Path(__file__).parent / "console.html").read_text(encoding="utf-8")

HumanHook = Callable[[str, dict[str, Any]], Awaitable[None]]


class ControlPlane:
    def __init__(
        self,
        *,
        port: int = 8765,
        timeout_s: int | None = None,
        announce: Callable[[str], None] | None = None,
    ) -> None:
        self.port = port
        self._timeout_override = timeout_s
        self.timeout_s = timeout_s or 900
        self.steps: list[tuple[str, str]] = []
        self.subject: dict[str, str] = {}  # which capability or goal is stuck: id, version, tenant, summary
        self.token = secrets.token_urlsafe(18)
        self.lease = ControlLease(self._on_transition)
        self.current: InterventionRecord | None = None
        self.actions: list[HumanAction] = []
        self.before_human_action: HumanHook | None = None  # discovery records human steps through this
        self._resolution: asyncio.Future[OperatorResolution] | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._announce = announce or (lambda message: print(message, flush=True))
        self.app = self._build_app()

    # ----------------------------------------------------------------- lifecycle

    def bind(self, runtime: Runtime, steps: list[tuple[str, str]] | None = None,
             subject: dict[str, str] | None = None) -> None:
        """Attach to the run that owns the live session (called by the engine or the discovery agent)."""
        self.rt = runtime
        self.steps = steps or []
        self.subject = dict(subject or {})
        self.timeout_s = self._timeout_override or runtime.limits.escalation_timeout_s

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    async def start(self) -> None:
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                return
            await asyncio.sleep(0.02)
        raise RuntimeError(f"operator console did not start on port {self.port}")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await asyncio.wait([self._task], timeout=3)

    # ------------------------------------------------------------ Escalator API

    def agent_may_act(self) -> bool:
        return self.lease.agent_may_act()

    async def escalate(
        self, record: InterventionRecord, *, screenshot: bytes | None, dialog_message: str | None
    ) -> OperatorResolution:
        record.id = record.id or f"int-{secrets.token_hex(3)}"
        self.current = record
        self.actions = []
        self._resolution = asyncio.get_running_loop().create_future()
        self.lease.move(LeaseState.AWAITING_OPERATOR, actor="agent", reason=f"{record.reason_code}: {record.reason}")
        self.rt.log.event("escalation_requested", intervention=record.id, code=record.reason_code,
                          reason=record.reason, step=record.step_id, dialog=dialog_message,
                          console=f"http://127.0.0.1:{self.port}/")
        if screenshot is not None:
            self.rt.log.save_bytes(f"interventions/{record.id}.png", screenshot)
        what = self.subject.get("capability", "the run")
        self._announce(f"Operator needed for {what} at {record.step_id or 'its current step'} "
                       f"({record.reason_code}): open {self.url}")
        try:
            resolution = await asyncio.wait_for(asyncio.shield(self._resolution), timeout=self.timeout_s)
        except TimeoutError:
            if self.lease.state in (LeaseState.AWAITING_OPERATOR, LeaseState.HUMAN_ACTIVE):
                self.lease.move(LeaseState.TIMED_OUT, actor="system", reason=f"no resolution in {self.timeout_s} s")
            resolution = OperatorResolution("timed_out")
        record.resolved_at = datetime.now(UTC)
        record.resolution = resolution.kind
        record.operator = resolution.operator
        record.note = resolution.note
        record.attested_steps = list(resolution.attested_steps)
        record.human_actions = list(self.actions)
        resolution.human_actions = list(self.actions)
        self.rt.log.write_json(f"interventions/{record.id}.json", record.model_dump(mode="json"))
        self.current = None
        return resolution

    def resume(self, reason: str) -> None:
        if self.lease.state is LeaseState.RESYNCING or self.lease.state is LeaseState.AWAITING_OPERATOR:
            self.lease.move(LeaseState.AGENT_ACTIVE, actor="agent", reason=reason)

    # -------------------------------------------------------------- internals

    def _on_transition(self, transition: Transition) -> None:
        self.rt.log.event("lease_changed", source=transition.source.value, target=transition.target.value,
                          actor=transition.actor, reason=transition.reason, epoch=transition.epoch)

    def _resolve(self, resolution: OperatorResolution) -> None:
        if self._resolution is not None and not self._resolution.done():
            self._resolution.set_result(resolution)

    async def _frame_at(self, x: float, y: float) -> tuple[Frame, float, float]:
        page = self.rt.web.page
        best: tuple[Frame, float, float] = (page.main_frame, x, y)
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                element = await frame.frame_element()
                box = await element.bounding_box()
            except PlaywrightError:
                continue
            if box and box["x"] <= x < box["x"] + box["width"] and box["y"] <= y < box["y"] + box["height"]:
                best = (frame, x - box["x"], y - box["y"])
        return best

    async def _facts_at(self, x: float, y: float) -> dict[str, Any]:
        frame, lx, ly = await self._frame_at(x, y)
        try:
            facts: dict[str, Any] | None = await call(frame, "elementAt", lx, ly)
        except PlaywrightError:
            facts = None
        return {"frame": frame.name or "top", **(facts or {})}

    async def _focused(self) -> dict[str, Any]:
        for frame in self.rt.web.page.frames:
            try:
                facts = await call(frame, "focused")
            except PlaywrightError:
                continue
            if facts:
                return {"frame": frame.name or "top", **facts}
        return {}

    def _record(self, kind: str, detail: dict[str, Any]) -> None:
        action = HumanAction(at=datetime.now(UTC), kind=kind, detail=detail)  # type: ignore[arg-type]
        self.actions.append(action)
        self.rt.log.event("human_action", kind=kind, intervention=self.current.id if self.current else None,
                          epoch=self.lease.epoch, **detail)

    @staticmethod
    def _summary(facts: dict[str, Any]) -> dict[str, Any]:
        return {k: facts.get(k) for k in ("frame", "role", "name", "tag") if facts.get(k)} | (
            {"label": facts["label"]["text"]} if facts.get("label") else {})

    # ------------------------------------------------------------------ console

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        def authorize(request: Request) -> None:
            token = request.query_params.get("t") or request.headers.get("x-rote-token")
            if not token or not secrets.compare_digest(token, self.token):
                raise HTTPException(status_code=403, detail="missing or wrong console token")

        @app.get("/")
        async def console(request: Request) -> Response:
            authorize(request)
            return HTMLResponse(CONSOLE_HTML.replace("__TOKEN__", self.token))

        @app.get("/api/state")
        async def state(request: Request) -> Response:
            authorize(request)
            record = self.current
            return JSONResponse({
                "lease": self.lease.state.value,
                "holder": self.lease.holder,
                "epoch": self.lease.epoch,
                "run_id": self.rt.log.run_id,
                "subject": self.subject,
                "intervention": record.model_dump(mode="json") if record else None,
                "dialog": self.rt.dialog_message,
                "human_actions": len(self.actions),
                "steps": [{"id": sid, "intent": intent} for sid, intent in self.steps],
            })

        @app.get("/api/live.png")
        async def live(request: Request) -> Response:
            authorize(request)
            if self.rt.pending_dialog is not None:
                raise HTTPException(status_code=409, detail="a native dialog is open: answer it in the panel")
            try:
                image = await self.rt.web.page.screenshot(type="png", timeout=8000)  # live view; never persisted
            except PlaywrightError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            return Response(image, media_type="image/png", headers={"cache-control": "no-store"})

        @app.post("/api/take")
        async def take(request: Request) -> Response:
            authorize(request)
            body = await request.json()
            operator = str(body.get("operator") or "operator")
            try:
                self.lease.move(LeaseState.HUMAN_ACTIVE, actor=operator, reason="operator took control")
            except LeaseError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return JSONResponse({"lease": self.lease.state.value})

        @app.post("/api/input")
        async def relay(request: Request) -> Response:
            authorize(request)
            if not self.lease.human_may_act():
                raise HTTPException(status_code=409, detail=f"lease is {self.lease.state.value}; take control first")
            body = await request.json()
            kind = body.get("kind")
            page = self.rt.web.page
            target: str | None = None  # what the click landed on, echoed to the operator's action list
            if kind == "click":
                x, y = float(body["x"]), float(body["y"])
                facts = await self._facts_at(x, y)
                if self.before_human_action is not None:
                    await self.before_human_action("click", {"x": x, "y": y, "facts": facts})
                await page.mouse.click(x, y)
                summary = self._summary(facts)
                self._record("click", {"x": round(x), "y": round(y), **summary})
                if summary.get("role") and summary.get("name"):  # redacted like the event log (the live view isn't)
                    target = self.rt.redactor.text(f'{summary["role"]} "{summary["name"]}"')
            elif kind == "type":
                text = str(body.get("text", ""))
                facts = await self._focused()
                if self.before_human_action is not None:
                    await self.before_human_action("type", {"text": text, "facts": facts})
                await page.keyboard.type(text)
                shown = "********" if facts.get("type") == "password" else text
                self._record("type", {"text": shown, **self._summary(facts)})
            elif kind == "key":
                key = str(body.get("key"))
                if key not in ("Enter", "Tab", "Escape", "Backspace", "ArrowUp", "ArrowDown"):
                    raise HTTPException(status_code=400, detail=f"key {key!r} is not relayed")
                await page.keyboard.press(key)
                self._record("key", {"key": key})
            elif kind == "scroll":
                dy = float(body.get("dy", 300))
                await page.mouse.wheel(0, dy)
                self._record("scroll", {"dy": dy})
            else:
                raise HTTPException(status_code=400, detail=f"unknown input kind {kind!r}")
            return JSONResponse({"ok": True, "target": target})

        @app.post("/api/dialog")
        async def dialog(request: Request) -> Response:
            authorize(request)
            if not self.lease.human_may_act():
                raise HTTPException(status_code=409, detail="take control first")
            body = await request.json()
            accept = bool(body.get("accept"))
            message = self.rt.dialog_message
            await self.rt.answer_dialog(accept)
            self._record("dialog", {"message": message, "answer": "accept" if accept else "dismiss"})
            return JSONResponse({"ok": True})

        @app.post("/api/handback")
        async def handback(request: Request) -> Response:
            authorize(request)
            body = await request.json()
            try:
                self.lease.move(LeaseState.RESYNCING, actor=self.lease.holder, reason="operator handed back")
            except LeaseError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            self._resolve(OperatorResolution(
                "handed_back", operator=self.lease.history[-2].actor if len(self.lease.history) >= 2 else None,
                note=body.get("note"), resume_step=body.get("resume_step") or None,
                attested_steps=list(body.get("attested_steps") or []),
            ))
            return JSONResponse({"lease": self.lease.state.value})

        @app.post("/api/approve")
        async def approve(request: Request) -> Response:
            authorize(request)
            body = await request.json()
            operator = str(body.get("operator") or "operator")
            if self.lease.state is not LeaseState.AWAITING_OPERATOR:
                raise HTTPException(status_code=409, detail="nothing is waiting for approval")
            self.lease.move(LeaseState.AGENT_ACTIVE, actor="agent", reason=f"approved by {operator}")
            self._resolve(OperatorResolution("approved", operator=operator, note=body.get("note")))
            return JSONResponse({"lease": self.lease.state.value})

        @app.post("/api/reject")
        async def reject(request: Request) -> Response:
            authorize(request)
            body = await request.json()
            operator = str(body.get("operator") or "operator")
            self.lease.move(LeaseState.ABORTED, actor=operator, reason="operator rejected")
            self._resolve(OperatorResolution("rejected", operator=operator, note=body.get("note")))
            return JSONResponse({"lease": self.lease.state.value})

        @app.post("/api/abort")
        async def abort(request: Request) -> Response:
            authorize(request)
            body = await request.json()
            operator = str(body.get("operator") or "operator")
            try:
                self.lease.move(LeaseState.ABORTED, actor=operator, reason="operator aborted the run")
            except LeaseError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            self._resolve(OperatorResolution("aborted", operator=operator, note=body.get("note")))
            return JSONResponse({"lease": self.lease.state.value})

        return app
