"""Planners decide the next action. Discovery never cares which one it has.

* ``AnthropicPlanner``: the live model. Each step is a stateless request: a frozen
  cached prefix (tools, system prompt, goal) plus a history our code writes and
  the current observation. Thinking blocks are never replayed and history is
  never edited, so the transcript is not the source of truth; the trace is.
* ``CassettePlanner``: replays a recorded run's decisions offline (CI, reviewers
  without an API key). Recorded refs are meaningless across runs, so each
  decision is re-bound to the current screen by the element's facts.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from rote.discovery.cassette import Cassette, bind
from rote.discovery.prompts import MODEL, SYSTEM_PROMPT, TOOLS
from rote.surface.web.observe import Observation


@dataclass
class PlannerRequest:
    step: int
    goal_text: str
    step_text: str
    screenshot: bytes
    observation: Observation


@dataclass
class Decision:
    tool: str
    args: dict[str, Any]
    served_by: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    note: str | None = None
    approval: str | None = None  # a cassette replays the operator's recorded approval


class Planner(Protocol):
    name: str
    model: str | None

    async def decide(self, request: PlannerRequest) -> Decision: ...


class AnthropicPlanner:
    name = "anthropic"

    def __init__(self, model: str = MODEL, effort: str = "high") -> None:
        import anthropic  # imported here: replay must never pull in a model client

        self.client = anthropic.AsyncAnthropic()
        self.model = model
        self.effort = effort

    async def decide(self, request: PlannerRequest) -> Decision:
        image = base64.b64encode(request.screenshot).decode()
        content: list[dict[str, Any]] = [
            {"type": "text", "text": request.goal_text, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": request.step_text},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}},
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        usage: dict[str, Any] = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "calls": 0}
        served_by: str | None = None
        for _attempt in range(2):
            started = time.monotonic()
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,  # type: ignore[arg-type]
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},  # type: ignore[typeddict-item]
                messages=messages,  # type: ignore[arg-type]
            )
            served_by = response.model
            usage["calls"] += 1
            usage["input"] += response.usage.input_tokens
            usage["cache_read"] += response.usage.cache_read_input_tokens or 0
            usage["cache_write"] += response.usage.cache_creation_input_tokens or 0
            usage["output"] += response.usage.output_tokens
            usage["seconds"] = round(usage.get("seconds", 0) + time.monotonic() - started, 1)
            if response.stop_reason == "refusal":
                return Decision("refused", {}, served_by, usage, note=str(response.stop_details))
            tool_use = next((b for b in response.content if b.type == "tool_use"), None)
            if tool_use is not None:
                return Decision(tool_use.name, dict(tool_use.input), served_by, usage)  # type: ignore[arg-type]
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": "Call exactly one tool."})
        return Decision("no_tool", {}, served_by, usage, note="the model answered without calling a tool twice")


class CassettePlanner:
    name = "cassette"

    def __init__(self, cassette: Cassette, inputs: dict[str, str]) -> None:
        self.cassette = cassette
        self.inputs = inputs
        self.model = cassette.model
        self._index = 0

    async def decide(self, request: PlannerRequest) -> Decision:
        if self._index >= len(self.cassette.entries):
            return Decision("request_human", {"reason": "the cassette ran out of recorded decisions",
                                              "category": "stuck"}, "cassette")
        entry = self.cassette.entries[self._index]
        self._index += 1
        args = dict(entry.args)
        note = None
        if entry.fingerprint and entry.fingerprint != request.observation.fingerprint:
            note = f"screen fingerprint differs from the recording ({entry.fingerprint})"
        if entry.target is not None:
            ref = bind(entry.target, request.observation, self.inputs)
            if ref is None:
                return Decision("request_human", {
                    "reason": f"recorded {entry.tool} target is not on this screen: {entry.target}",
                    "category": "unexpected_screen",
                }, "cassette", note=note)
            args["ref"] = ref
        return Decision(entry.tool, args, entry.served_by or "cassette", note=note, approval=entry.approval)
