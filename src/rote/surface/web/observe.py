"""Build the observation discovery shows the model: controls per frame, headings,
messages, a text digest, and a screenshot.

Refs (``e12``) are opaque handles, valid only until the next observation. The
recorder never stores a ref; it stores the element's facts and derives durable
locators from them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error, Page

from rote.schema.hashing import sha256_hex
from rote.surface.web.session import call


@dataclass
class FrameView:
    name: str | None
    url: str
    path: str
    title: str
    elements: list[dict[str, Any]]
    headings: list[str]
    messages: list[str]
    text: str


@dataclass
class Observation:
    frames: list[FrameView]
    screenshot: bytes | None = None
    fingerprint: str = ""
    by_ref: dict[str, tuple[FrameView, dict[str, Any]]] = field(default_factory=dict)

    def element(self, ref: str) -> tuple[FrameView, dict[str, Any]] | None:
        return self.by_ref.get(ref)

    def field_values(self) -> dict[str, Any]:
        return {e["ref"]: (e.get("value"), e.get("checked")) for f in self.frames for e in f.elements if "value" in e}


async def observe(page: Page, *, tag: bool = True, screenshot: bool = True, max_text: int = 2500) -> Observation:
    frames: list[FrameView] = []
    counter = 1
    for frame in page.frames:
        try:
            data = await call(frame, "index", {"tag": tag, "maxText": max_text, "prefix": "e", "start": counter})
        except Error:
            continue  # detached or navigating mid-observation; the next observation will see it
        if data["frameset"]:
            continue
        counter += len(data["elements"])
        frames.append(
            FrameView(
                name=frame.name or None,
                url=data["url"],
                path=urlsplit(data["url"]).path,
                title=data["title"],
                elements=data["elements"],
                headings=data["headings"],
                messages=data["messages"],
                text=data["text"],
            )
        )
    shot = await page.screenshot(type="jpeg", quality=70) if screenshot else None
    observation = Observation(frames=frames, screenshot=shot, fingerprint=fingerprint(frames))
    observation.by_ref = {e["ref"]: (f, e) for f in frames for e in f.elements}
    return observation


def fingerprint(frames: list[FrameView]) -> str:
    """Structural identity of a screen: frames, paths, headings, messages and controls.

    Values, tokens, session ids and timestamps are left out, so the same screen
    fingerprints the same across runs. Loop detection and cassette playback rely
    on that.
    """
    skeleton = [
        [
            f.name,
            f.path,
            f.headings,
            f.messages,
            [[e["role"], e["name"], (e.get("label") or {}).get("text")] for e in f.elements if e["role"] != "cell"],
        ]
        for f in frames
    ]
    return sha256_hex(skeleton)[7:23]


FORM_ROLES = frozenset({"textbox", "password", "combobox", "listbox", "checkbox", "radio"})


def _row_summary(table: dict[str, Any] | None) -> str | None:
    if not table or not table.get("headers"):
        return None
    cells = [f"{h}={v}" for h, v in zip(table["headers"], table["row"], strict=False) if h and v]
    return ", ".join(cells) if len(cells) >= 2 else None


def describe_element(e: dict[str, Any]) -> str:
    role = e["role"]
    if role == "cell":
        parts = [f"[{e['ref']}] {json.dumps(e['text'])}"]
        table = e.get("table")
        label = (e.get("label") or {}).get("text")
        if table and table.get("column"):
            parts.append(f"(column {json.dumps(table['column'])}; row: {_row_summary(table) or ''})")
        elif label:
            parts.append(f"(labeled {json.dumps(label)})")
        return " ".join(parts)

    parts = [f"[{e['ref']}] {role}"]
    if e.get("name"):
        parts.append(json.dumps(e["name"]))
    elif role == "button" and e.get("type") == "image":
        parts.append("(image button, no accessible name)")
    label = (e.get("label") or {}).get("text")
    if label and (role in FORM_ROLES or not e.get("name")):
        parts.append(f"label={json.dumps(label)}")
    if "options" in e:
        options = [o["label"] for o in e["options"]]
        selected = next((o["label"] for o in e["options"] if o["selected"]), None)
        parts.append(f"options={json.dumps(options)} selected={json.dumps(selected)}")
    elif "checked" in e:
        parts.append(f"checked={str(e['checked']).lower()}")
    elif "value" in e and role in FORM_ROLES:
        parts.append(f"value={json.dumps(e['value'])}")
    row = _row_summary(e.get("table"))
    if row and role not in FORM_ROLES:
        parts.append(f"(row: {row})")
    if not e.get("enabled", True):
        parts.append("(disabled)")
    return " ".join(parts)


def render(observation: Observation, *, text_frames: tuple[str, ...] = ("main",)) -> str:
    """The text half of the observation, as the model sees it."""
    blocks: list[str] = []
    for frame in observation.frames:
        lines = [f'### Frame "{frame.name or "top"}"  path={frame.path}']
        if frame.headings:
            lines.append("Headings: " + " | ".join(frame.headings))
        if frame.messages:
            lines.append("Messages: " + " | ".join(frame.messages))
        controls = [e for e in frame.elements if e["role"] != "cell"]
        cells = [e for e in frame.elements if e["role"] == "cell"]
        if controls:
            lines.append("Controls:")
            lines.extend("  " + describe_element(e) for e in controls)
        if cells:
            lines.append("Readable values:")
            lines.extend("  " + describe_element(e) for e in cells)
        if (frame.name or "top") in text_frames or len(observation.frames) == 1:
            lines.append("Visible text:")
            lines.extend("  " + line for line in frame.text.splitlines() if line.strip())
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
