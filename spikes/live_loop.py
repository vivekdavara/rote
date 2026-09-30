"""Spike (milestone 2.5): a bare observe -> decide -> act loop against the CoreOne mock.

No recorder and no artifact. This settles the observation format before the real
discovery loop exists. Can the model finish the balance goal on the frameset
from this view? How many steps and tokens does it take? How often is its
"expect" (the proposed postcondition) actually right?

    # Live: needs an Anthropic API key, run from your own terminal
    ANTHROPIC_API_KEY=... .venv/bin/python spikes/live_loop.py [--headed] [--effort high]

    # No key: a scripted planner drives the same harness
    .venv/bin/python spikes/live_loop.py --scripted

Transcripts (decisions, token usage, served model; no screenshots) go to spikes/out/.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from rote.devserver import start_mock
from rote.discovery.prompts import MODEL, SYSTEM_PROMPT, TOOLS, goal_block, step_block
from rote.schema.templating import render as render_template
from rote.surface.web.observe import Observation, describe_element, observe, render
from rote.surface.web.session import BrowserOptions, WebSession, call

GOAL = "Look up member {member_id} and read their current Share Savings balance."
INPUTS = {"member_id": {"example": "100234", "description": "six-digit member number"}}
OUTPUTS = {"savings_balance": "money"}
OUT = Path(__file__).parent / "out"
Decision = tuple[str, dict[str, Any], dict[str, Any]]


class LivePlanner:
    def __init__(self, effort: str) -> None:
        import anthropic

        self.client = anthropic.Anthropic()
        self.effort = effort

    def decide(self, goal_text: str, step_text: str, screenshot: bytes, observation: Observation) -> Decision:
        content: list[dict[str, Any]] = [
            {"type": "text", "text": goal_text, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": step_text},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(screenshot).decode()},
            },
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        meta: dict[str, Any] = {"calls": []}
        for _attempt in range(2):
            started = time.monotonic()
            response = self.client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,  # type: ignore[arg-type]
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                messages=messages,  # type: ignore[arg-type]
            )
            usage = response.usage
            meta["calls"].append(
                {
                    "served_by": response.model,
                    "stop_reason": response.stop_reason,
                    "input": usage.input_tokens,
                    "cache_read": usage.cache_read_input_tokens,
                    "cache_write": usage.cache_creation_input_tokens,
                    "output": usage.output_tokens,
                    "seconds": round(time.monotonic() - started, 1),
                    "request_id": getattr(response, "_request_id", None),
                }
            )
            if response.stop_reason == "refusal":
                return "refused", {"reason": str(response.stop_details)}, meta
            tool_use = next((b for b in response.content if b.type == "tool_use"), None)
            if tool_use is not None:
                return tool_use.name, dict(tool_use.input), meta  # type: ignore[arg-type]
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": "Call exactly one tool."})
        return "no_tool", {}, meta


class ScriptedPlanner:
    """Picks elements by their facts (role, name, label, table row), as a model would. No API calls."""

    SCRIPT: list[tuple[str, dict[str, str] | None, dict[str, Any]]] = [
        ("click", {"role": "link", "name": "Member Search"}, {"expect": "Last Name"}),
        ("type_text", {"role": "textbox", "label": "Member Number"}, {"text": "{{inputs.member_id}}", "expect": None}),
        ("click", {"role": "button", "name": "Search"}, {"expect": "Search Results"}),
        ("click", {"role": "link", "name": "View", "row": "100234"}, {"expect": "Member Detail"}),
        ("extract", {"role": "cell", "column": "Balance", "row": "Share Savings"}, {"output_name": "savings_balance"}),
        ("finish", None, {"success_description": "Member Detail shows the Share Savings balance."}),
    ]

    def __init__(self) -> None:
        self.index = 0

    def decide(self, goal_text: str, step_text: str, screenshot: bytes, observation: Observation) -> Decision:
        tool, spec, args = self.SCRIPT[self.index]
        self.index += 1
        args = {**args, "rationale": f"scripted step {self.index}", "notes": None}
        if spec is not None:
            args["ref"] = find_ref(observation, spec)
        return tool, args, {"calls": []}


def find_ref(observation: Observation, spec: dict[str, str]) -> str:
    for frame in observation.frames:
        for e in frame.elements:
            if e["role"] != spec["role"]:
                continue
            if "name" in spec and e.get("name") != spec["name"]:
                continue
            if "label" in spec and (e.get("label") or {}).get("text") != spec["label"]:
                continue
            table = e.get("table") or {}
            if "column" in spec and table.get("column") != spec["column"]:
                continue
            if "row" in spec and spec["row"] not in (table.get("row") or []):
                continue
            return str(e["ref"])
    raise LookupError(f"no element matches {spec}")


async def settle(page: Page) -> None:
    with contextlib.suppress(PlaywrightError):
        await page.wait_for_load_state("networkidle", timeout=5000)
    await page.wait_for_timeout(150)


async def any_frame_shows(page: Page, text: str) -> bool:
    for frame in page.frames:
        try:
            if await call(frame, "textVisible", text):
                return True
        except PlaywrightError:
            continue
    return False


async def act(page: Page, observation: Observation, tool: str, args: dict[str, Any], outputs: dict[str, str]) -> str:
    if tool == "wait":
        await page.wait_for_timeout(1500)
        return "waited"
    ref = args.get("ref")
    if tool == "press_key" and not ref:
        await page.keyboard.press(args["key"])
        await settle(page)
        return "ok"
    found = observation.element(ref or "")
    if found is None:
        return f"error: no element {ref} on this screen"
    frame_view, _facts = found
    frame = page.frame(name=frame_view.name) if frame_view.name else page.main_frame
    if frame is None:
        return f"error: frame {frame_view.name} is gone"
    locator = frame.locator(f'[data-rote-ref="{ref}"]')
    values = {"inputs": {k: v["example"] for k, v in INPUTS.items()}}
    try:
        if tool == "click":
            await locator.click(timeout=5000)
        elif tool == "type_text":
            await locator.fill(render_template(args["text"], values), timeout=5000)
        elif tool == "select_option":
            await locator.select_option(label=args["option"], timeout=5000)
        elif tool == "set_checkbox":
            await locator.set_checked(bool(args["checked"]), timeout=5000)
        elif tool == "press_key":
            await locator.press(args["key"], timeout=5000)
        elif tool == "extract":
            outputs[args["output_name"]] = (await locator.inner_text(timeout=5000)).strip()
            return f"extracted {args['output_name']}"
        else:
            return f"error: unknown tool {tool}"
    except PlaywrightError as exc:
        return "error: " + str(exc).splitlines()[0]
    await settle(page)
    return "ok"


async def sign_on(page: Page, base_url: str) -> None:
    # Spike shortcut. Real runs sign on through the product profile before the model starts.
    await page.goto(f"{base_url}/login")
    await page.locator("input[type=text]").first.fill("svc_rote")
    await page.locator("input[type=password]").fill("training-only-2026")
    await page.get_by_role("button", name="Sign On").click()
    await page.frame_locator("frame[name=main]").get_by_text("Main Menu").wait_for()


async def run(planner: LivePlanner | ScriptedPlanner, headed: bool, max_steps: int) -> int:
    mock = start_mock(seed=random.randrange(10**6))
    transcript: dict[str, Any] = {"goal": GOAL, "model": MODEL, "planner": type(planner).__name__, "steps": []}
    outputs: dict[str, str] = {}
    history: list[str] = []
    notes: str | None = None
    outcome = "budget exhausted"
    try:
        async with async_playwright() as pw:
            web = await WebSession.launch(pw, BrowserOptions(headless=not headed))
            page = web.page
            await sign_on(page, mock.base_url())
            goal_text = goal_block(GOAL, INPUTS, OUTPUTS)
            for step in range(1, max_steps + 1):
                observation = await observe(page)
                assert observation.screenshot is not None
                prompt = step_block(history, notes, render(observation), step, max_steps)
                tool, args, meta = planner.decide(goal_text, prompt, observation.screenshot, observation)
                found = observation.element(str(args.get("ref") or ""))
                target = describe_element(found[1]) + f" in {found[0].name}" if found else ""
                record: dict[str, Any] = {
                    "step": step, "tool": tool, "args": args, "target": target,
                    "fingerprint": observation.fingerprint, "meta": meta,
                }
                if tool in ("finish", "request_human", "refused", "no_tool"):
                    outcome = tool if tool != "finish" else "finished"
                    transcript["steps"].append(record)
                    print(f"step {step}: {tool} {json.dumps({k: v for k, v in args.items() if k != 'ref'})}")
                    break
                result = await act(page, observation, tool, args, outputs)
                expect = args.get("expect")
                expect_ok = await any_frame_shows(page, expect) if expect else None
                record.update(result=result, expect_ok=expect_ok)
                transcript["steps"].append(record)
                usage = meta["calls"][-1] if meta["calls"] else {}
                print(
                    f"step {step}: {tool} {target} -> {result}"
                    + (f" | expect {expect!r} {'✓' if expect_ok else '✗'}" if expect else "")
                    + (f" | in={usage['input']} cache_read={usage['cache_read']} out={usage['output']}"
                       f" served_by={usage['served_by']} {usage['seconds']}s" if usage else "")
                )
                history.append(f"{step}. {tool} {target} -> {result}. Rationale: {args.get('rationale')}")
                notes = args.get("notes")
            await web.close()
    finally:
        mock.stop()

    calls = [c for s in transcript["steps"] for c in s["meta"]["calls"]]
    transcript.update(outcome=outcome, outputs=outputs, totals={
        "steps": len(transcript["steps"]),
        "api_calls": len(calls),
        "input_tokens": sum(c["input"] for c in calls),
        "cache_read_tokens": sum(c["cache_read"] or 0 for c in calls),
        "output_tokens": sum(c["output"] for c in calls),
        "expect_right": sum(1 for s in transcript["steps"] if s.get("expect_ok")),
        "expect_given": sum(1 for s in transcript["steps"] if s.get("expect_ok") is not None),
    })
    OUT.mkdir(exist_ok=True)
    path = OUT / f"spike-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(transcript, indent=2, default=str), encoding="utf-8")
    print(f"\noutcome: {outcome}  outputs: {outputs}  totals: {transcript['totals']}\ntranscript: {path}")
    return 0 if outcome == "finished" and "savings_balance" in outputs else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scripted", action="store_true", help="drive the harness without calling the API")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--max-steps", type=int, default=20)
    args = parser.parse_args()
    planner = ScriptedPlanner() if args.scripted else LivePlanner(args.effort)
    raise SystemExit(asyncio.run(run(planner, args.headed, args.max_steps)))


if __name__ == "__main__":
    main()
