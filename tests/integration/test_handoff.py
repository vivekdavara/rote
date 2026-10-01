"""Human handoff on the live session, driven by a scripted operator through the real console API.

The operator acts only through the console's input relay, exactly as a person
clicking on the live view would. The only test-only shortcut is how the
operator finds where to click: it reads the button's position from the page,
where a person would read it off the live image.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from playwright.async_api import Browser

from rote.control.plane import ControlPlane
from rote.control.protocol import OperatorResolution
from rote.devserver import MockServer, free_port
from rote.registry.store import Workspace
from rote.replay.engine import ReplayEngine, ReplayOptions
from rote.schema.result import RunResult

from .conftest import BALANCE

pytestmark = pytest.mark.integration

Operator = Callable[[httpx.AsyncClient, ReplayEngine], Awaitable[None]]


def fail_fast(plane: ControlPlane, driver: asyncio.Task[None]) -> None:
    """If the scripted operator dies, abort the intervention so the run (and its error) surfaces now."""
    def done(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            plane._resolve(OperatorResolution("aborted", note=f"scripted operator failed: {task.exception()!r}"))
    driver.add_done_callback(done)


def say(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} operator: {message}", flush=True)


async def attended_run(workspace: Workspace, browser: Browser, operator: Operator,
                       timeout_s: int | None = None) -> tuple[RunResult, ControlPlane]:
    plane = ControlPlane(port=free_port(), timeout_s=timeout_s or 60, announce=lambda message: None)
    capability = workspace.capability(BALANCE)
    engine = ReplayEngine(
        workspace, capability, workspace.tenant("harbor"), workspace.profile("coreone"),
        workspace.policy("coreone", "harbor"), {"member_id": "100234"},
        ReplayOptions(attended=True, require_approval=False, step_timeout_ms=4000),
        browser=browser, escalator=plane,
    )
    await plane.start()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{plane.port}",
                                 headers={"x-rote-token": plane.token}, timeout=10) as client:
        driver = asyncio.create_task(operator(client, engine))
        fail_fast(plane, driver)
        try:
            result = await engine.run()
            await asyncio.wait_for(driver, timeout=10)
        finally:
            driver.cancel()
            await plane.stop()
    return result, plane


async def wait_for_intervention(client: httpx.AsyncClient) -> dict[str, Any]:
    for _ in range(900):  # up to ~90 s: CI runners are much slower than a laptop
        state = (await client.get("/api/state")).json()
        if state["intervention"]:
            return state
        await asyncio.sleep(0.1)
    raise AssertionError("no intervention was opened")


async def click_button(client: httpx.AsyncClient, engine: ReplayEngine, name: str) -> None:
    frame = engine.web.page.frame(name="main")
    assert frame is not None
    box = await frame.get_by_role("button", name=name).bounding_box()
    assert box is not None
    response = await client.post("/api/input", json={"kind": "click", "x": box["x"] + box["width"] / 2,
                                                     "y": box["y"] + box["height"] / 2})
    response.raise_for_status()


async def supervisor_override(client: httpx.AsyncClient, engine: ReplayEngine) -> None:
    state = await wait_for_intervention(client)
    assert state["intervention"]["reason_code"] == "UNKNOWN_MODAL"
    early = await client.post("/api/input", json={"kind": "click", "x": 10, "y": 10})
    assert early.status_code == 409  # the relay refuses input until the operator holds the lease
    (await client.post("/api/take", json={"operator": "Test Operator"})).raise_for_status()
    await click_button(client, engine, "Supervisor Override")
    (await client.post("/api/handback", json={"note": "Supervisor override applied"})).raise_for_status()


async def test_operator_takes_over_the_live_session_and_hands_back(workspace: Workspace, browser: Browser,
                                                                   mock: MockServer) -> None:
    mock.fault("unknown_modal", page="search")
    result, plane = await attended_run(workspace, browser, supervisor_override)

    assert result.status == "succeeded", result.error
    assert result.outputs == {"savings_balance": {"amount": "1234.56", "currency": "USD"}}
    [intervention] = result.interventions
    assert intervention.reason_code == "UNKNOWN_MODAL" and intervention.step_id == "enter_member_id"
    assert intervention.resolution == "handed_back" and intervention.operator == "Test Operator"
    clicks = [a for a in intervention.human_actions if a.kind == "click"]
    assert clicks and clicks[0].detail["name"] == "Supervisor Override" and clicks[0].detail["frame"] == "main"

    states = [(t.source.value, t.target.value) for t in plane.lease.history]
    assert states == [("AGENT_ACTIVE", "AWAITING_OPERATOR"), ("AWAITING_OPERATOR", "HUMAN_ACTIVE"),
                      ("HUMAN_ACTIVE", "RESYNCING"), ("RESYNCING", "AGENT_ACTIVE")]

    # While the operator held the lease, the automation sent no input at all.
    events = [json.loads(line) for line in (workspace.runs / result.run_id / "events.jsonl").read_text().splitlines()]
    human_window, automation_inputs = False, []
    for event in events:
        if event["type"] == "lease_changed":
            human_window = event["data"]["target"] == "HUMAN_ACTIVE"
        elif human_window and event["type"] == "action_executed":
            automation_inputs.append(event)
    assert automation_inputs == []
    assert any(e["type"] == "human_action" for e in events) and any(e["type"] == "resync" for e in events)


async def test_console_requires_its_token(workspace: Workspace, browser: Browser, mock: MockServer) -> None:
    plane = ControlPlane(port=free_port(), announce=lambda message: None)
    await plane.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{plane.port}") as client:
            assert (await client.get("/api/state")).status_code == 403
            assert (await client.get("/api/state?t=wrong")).status_code == 403
            assert (await client.get(f"/?t={plane.token}")).status_code == 200
    finally:
        await plane.stop()


async def operator_aborts(client: httpx.AsyncClient, engine: ReplayEngine) -> None:
    await wait_for_intervention(client)
    (await client.post("/api/abort", json={"operator": "Test Operator", "note": "not safe"})).raise_for_status()


async def test_operator_can_abort(workspace: Workspace, browser: Browser, mock: MockServer) -> None:
    mock.fault("unknown_modal", page="search")
    result, _ = await attended_run(workspace, browser, operator_aborts)
    assert result.status == "failed" and result.error is not None
    assert result.error.code == "ESCALATION_ABORTED"
    assert result.interventions[0].resolution == "aborted"


async def answer_dialog(client: httpx.AsyncClient, engine: ReplayEngine) -> None:
    try:
        state = await wait_for_intervention(client)
    except AssertionError as exc:
        events = [json.loads(line)["type"] for line in (engine.log.dir / "events.jsonl").read_text().splitlines()[-4:]]
        stacks = []
        for task in asyncio.all_tasks():
            lines, coro = [], task.get_coro()
            while coro is not None and hasattr(coro, "cr_frame") and coro.cr_frame is not None:
                frame = coro.cr_frame
                lines.append(f"  {frame.f_code.co_filename.split('/rote/')[-1].split('site-packages/')[-1]}:"
                             f"{frame.f_lineno} {frame.f_code.co_name}")
                coro = coro.cr_await
            if lines:
                stacks.append(f"--- {task.get_name()} awaiting {coro!r}\n" + "\n".join(lines))
        raise AssertionError(f"no intervention; dialog pending={engine.rt.pending_dialog is not None}; "
                             f"last events: {events}\nsuspended tasks:\n" + "\n".join(stacks)) from exc
    say(f"intervention {state['intervention']['reason_code']}: {state['intervention']['reason']}; dialog={state['dialog']!r}")
    assert state["intervention"]["reason_code"] == "UNKNOWN_DIALOG"
    assert "wire transfer" in (state["dialog"] or "")
    (await client.post("/api/take", json={"operator": "Test Operator"})).raise_for_status()
    say("took control; answering the dialog")
    (await client.post("/api/dialog", json={"accept": True})).raise_for_status()
    say("dialog accepted; handing back")
    (await client.post("/api/handback", json={"note": "acknowledged the wire review notice"})).raise_for_status()
    say("handed back")


async def test_unknown_native_dialog_is_held_for_the_operator(workspace: Workspace, browser: Browser,
                                                              mock: MockServer) -> None:
    mock.fault("js_dialog", page="member", message="This member has a pending wire transfer review.")
    result, _ = await attended_run(workspace, browser, answer_dialog)
    assert result.status == "succeeded", result.error
    [intervention] = result.interventions
    assert [a.kind for a in intervention.human_actions] == ["dialog"]


# --------------------------------------------------------------- discovery handoff


async def demonstrate_search(client: httpx.AsyncClient, agent: object) -> None:
    """The model asks for help; the operator shows it how to search, through the relay."""
    await wait_for_intervention(client)
    (await client.post("/api/take", json={"operator": "Test Operator"})).raise_for_status()
    page = agent.rt.web.page  # type: ignore[attr-defined]
    main = page.frame(name="main")
    field = await main.locator("input[type=text]").first.bounding_box()
    (await client.post("/api/input", json={"kind": "click", "x": field["x"] + 5, "y": field["y"] + 5})).raise_for_status()
    (await client.post("/api/input", json={"kind": "type", "text": "100234"})).raise_for_status()
    button = await main.get_by_role("button", name="Search").bounding_box()
    (await client.post("/api/input", json={"kind": "click", "x": button["x"] + 5,
                                           "y": button["y"] + 5})).raise_for_status()
    await asyncio.sleep(0.5)
    (await client.post("/api/handback", json={"note": "Type the member number, then press Search."})).raise_for_status()


async def test_operator_demonstration_becomes_human_steps_in_the_capability(
    workspace: Workspace, browser: Browser, mock: MockServer
) -> None:
    import shutil
    from datetime import UTC, datetime

    from rote.discovery.agent import DiscoveryAgent, DiscoveryOptions
    from rote.discovery.cassette import Cassette, CassetteEntry
    from rote.discovery.planner import CassettePlanner
    from rote.schema.capability import ClickStep, FillStep
    from rote.schema.spec import load_spec

    from .conftest import REPO

    shutil.rmtree(workspace.root / "capabilities")
    spec = load_spec(REPO / "specs" / "get_savings_balance.yaml")
    entries = [
        CassetteEntry(step=1, tool="click", args={"rationale": "Open member search.", "expect": "Last Name"},
                      target={"frame": "nav", "role": "link", "name": "Member Search", "nth": 0}),
        CassetteEntry(step=2, tool="request_human", args={"reason": "I cannot tell how to search here.",
                                                          "category": "stuck"}),
        CassetteEntry(step=3, tool="click", args={"rationale": "Open the member.", "expect": "Member Detail"},
                      target={"frame": "main", "role": "link", "name": "View", "row_has": ["{{inputs.member_id}}"],
                              "nth": 0}),
        CassetteEntry(step=4, tool="extract", args={"output_name": "savings_balance", "rationale": "Read it."},
                      target={"frame": "main", "role": "cell", "column": "Balance", "row_first": "Share Savings",
                              "nth": 0}),
        CassetteEntry(step=5, tool="finish", args={"success_description": "Balance shown.", "rationale": "Done."}),
    ]
    cassette = Cassette(capability_id=spec.capability_id, tenant="harbor", recorded_at=datetime.now(UTC),
                        source="hand-written script (test fixture)", entries=entries)
    plane = ControlPlane(port=free_port(), timeout_s=60, announce=lambda message: None)
    agent = DiscoveryAgent(workspace, spec, CassettePlanner(cassette, spec.example(0)),
                           DiscoveryOptions(attended=True), browser=browser, escalator=plane)
    await plane.start()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{plane.port}",
                                 headers={"x-rote-token": plane.token}, timeout=10) as client:
        driver = asyncio.create_task(demonstrate_search(client, agent))
        fail_fast(plane, driver)
        try:
            result = await agent.run()
            await asyncio.wait_for(driver, timeout=10)
        finally:
            driver.cancel()
            await plane.stop()

    assert result.status == "compiled", result.reason
    steps = result.capability.steps
    human = [s for s in steps if s.provenance == "human"]
    assert [type(s).__name__ for s in human] == ["FillStep", "ClickStep"]
    fill = human[0]
    assert isinstance(fill, FillStep) and fill.value == "{{inputs.member_id}}"
    assert isinstance(human[1], ClickStep) and human[1].target.locators[0].by == "role"
    assert result.verification is not None and result.verification.status == "succeeded"
