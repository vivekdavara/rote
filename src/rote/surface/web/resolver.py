"""Deterministic target resolution: ranked strategies, exactly-one-match, drift signals.

Replay never guesses. For each locator, in rank order, it asks the page how many
visible elements match. Exactly one is a hit. Several means the strategy is
ambiguous here, so the next one is tried. None means the next one is tried.
If a lower-ranked strategy wins, the run still proceeds, but it reports drift.
If none wins before the timeout, the failure names the step, the strategies
tried, and a "did you mean" hint computed from the page (never from a model).
"""

from __future__ import annotations

import asyncio
import difflib
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import ElementHandle, Error, Frame, JSHandle

from rote.schema.target import CssLocator, LabelLocator, Locator, RoleLocator, TableCellLocator, Target
from rote.schema.templating import render
from rote.surface.web.session import WebSession, call, call_handle


@dataclass
class Resolved:
    frame: Frame
    element: ElementHandle
    elements: list[ElementHandle]
    strategy: str
    rank: int


@dataclass
class Attempt:
    strategy: str
    matches: int


@dataclass
class ResolutionFailure(Exception):
    code: str
    message: str
    attempts: list[Attempt] = field(default_factory=list)
    hint: str | None = None

    def __str__(self) -> str:
        return self.message


Poll = Callable[[], Awaitable[None]]


class Resolver:
    def __init__(
        self,
        session: WebSession,
        context: Mapping[str, Mapping[str, str]],
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.session = session
        self.context = context
        self.labels = dict(labels or {})

    # -- text preparation: templates first, then the tenant's label dictionary ---------

    def text(self, value: str) -> str:
        rendered = render(value, self.context)
        return self.labels.get(rendered, rendered)

    # -- strategies ---------------------------------------------------------------------

    async def _elements(self, handle: JSHandle) -> list[ElementHandle]:
        properties = await handle.get_properties()
        elements = [p.as_element() for p in properties.values()]
        await handle.dispose()
        return [e for e in elements if e is not None]

    async def _visible(self, handles: list[ElementHandle]) -> list[ElementHandle]:
        return [h for h in handles if await h.is_visible()]

    async def candidates(self, frame: Frame, locator: Locator) -> list[ElementHandle]:
        if isinstance(locator, RoleLocator):
            if locator.name is None:
                found = frame.get_by_role(locator.role)  # type: ignore[arg-type]
            else:
                found = frame.get_by_role(locator.role, name=self.text(locator.name), exact=True)  # type: ignore[arg-type]
            return await self._visible(await found.element_handles())
        if isinstance(locator, LabelLocator):
            handle = await call_handle(frame, "resolveLabel", self.text(locator.text), locator.role)
            return await self._elements(handle)
        if isinstance(locator, TableCellLocator):
            spec: dict[str, Any] = {
                "headers": [self.text(h) for h in locator.headers],
                "row": {self.text(k): self.text(v) for k, v in locator.row.items()},
                "column": self.text(locator.column) if locator.column else None,
                "then": (
                    {"role": locator.then.role, "name": self.text(locator.then.name) if locator.then.name else None}
                    if locator.then
                    else None
                ),
            }
            handle = await call_handle(frame, "resolveTableCell", spec)
            return await self._elements(handle)
        if isinstance(locator, CssLocator):
            return await self._visible(await frame.locator(locator.css).element_handles())
        raise TypeError(f"unknown locator {locator!r}")

    # -- resolution ---------------------------------------------------------------------

    def _frames(self, target: Target) -> list[Frame]:
        """``frame: "*"`` searches every frame (product-level handlers: a notice can appear anywhere)."""
        if target.frame == "*":
            return list(self.session.page.frames)
        frame = self.session.frame(target.frame)
        return [frame] if frame is not None else []

    async def resolve_once(self, target: Target, *, many: bool = False) -> tuple[Resolved | None, list[Attempt]]:
        frames = self._frames(target)
        attempts: list[Attempt] = []
        if not frames:
            return None, attempts
        for rank, locator in enumerate(target.locators):
            found: list[tuple[Frame, ElementHandle]] = []
            for frame in frames:
                try:
                    found.extend((frame, element) for element in await self.candidates(frame, locator))
                except Error:
                    continue  # frame navigating or detached: not ready yet
            attempts.append(Attempt(locator.by, len(found)))
            if len(found) == 1 or (many and found):
                frame = found[0][0]
                elements = [element for f, element in found if f == frame]
                return Resolved(frame, elements[0], elements, locator.by, rank), attempts
        return None, attempts

    async def resolve(
        self, target: Target, timeout_ms: int, *, many: bool = False, on_poll: Poll | None = None
    ) -> Resolved:
        deadline = time.monotonic() + timeout_ms / 1000
        attempts: list[Attempt] = []
        while True:
            resolved, attempts = await self.resolve_once(target, many=many)
            if resolved is not None:
                return resolved
            if on_poll is not None:
                await on_poll()
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.1)
        ambiguous = [a for a in attempts if a.matches > 1]
        tried = ", ".join(f"{a.strategy}={a.matches}" for a in attempts) or "frame not found"
        where = target.description or describe_target(target)
        if ambiguous:
            return_code, what = "TARGET_AMBIGUOUS", "matched more than one control"
        else:
            return_code, what = "TARGET_NOT_FOUND", "was not found"
        raise ResolutionFailure(
            return_code, f"{where} {what} (matches per strategy: {tried})", attempts, await self.did_you_mean(target)
        )

    async def did_you_mean(self, target: Target) -> str | None:
        frames = self._frames(target)
        if not frames:
            return f"frame {target.frame!r} is not present"
        frame = frames[0]
        wanted: list[tuple[str, str | None]] = []
        for locator in target.locators:
            if isinstance(locator, RoleLocator) and locator.name:
                wanted.append((self.text(locator.name), locator.role))
            elif isinstance(locator, LabelLocator):
                wanted.append((self.text(locator.text), None))
        if not wanted:
            return None
        try:
            data = await call(frame, "index", {"tag": False, "maxText": 0, "prefix": "x", "start": 0})
        except Error:
            return None
        pool: set[str] = set()
        for e in data["elements"]:
            if e.get("name"):
                pool.add(e["name"])
            label = (e.get("label") or {}).get("text")
            if label and e["role"] != "cell":
                pool.add(label)
        best: tuple[float, str, str] | None = None
        for text, _role in wanted:
            for candidate in pool:
                score = similarity(text, candidate)
                if score >= 0.45 and (best is None or score > best[0]):
                    best = (score, text, candidate)
        return f"no control named {best[1]!r}; closest on screen: {best[2]!r}" if best else None


def similarity(a: str, b: str) -> float:
    ratio = difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
    ta, tb = set(a.lower().split()), set(b.lower().split())
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(ratio, jaccard)


def describe_target(target: Target) -> str:
    first = target.locators[0]
    where = f" in frame {target.frame!r}" if target.frame else ""
    if isinstance(first, RoleLocator):
        return f"{first.role} {first.name!r}{where}" if first.name else f"{first.role}{where}"
    if isinstance(first, LabelLocator):
        return f"control labeled {first.text!r}{where}"
    if isinstance(first, TableCellLocator):
        dest = f"column {first.column!r}" if first.column else f"{first.then.role if first.then else '?'}"
        return f"{dest} of the row {first.row} in table {first.headers}{where}"
    return f"css {first.css!r}{where}"


async def element_facts(resolved: Resolved) -> dict[str, Any]:
    facts: dict[str, Any] = await resolved.frame.evaluate("el => window.__rote.facts(el)", resolved.element)
    return facts


async def hit_test(resolved: Resolved) -> dict[str, Any]:
    result: dict[str, Any] = await resolved.frame.evaluate("el => window.__rote.hitTest(el)", resolved.element)
    return result
