"""Evaluate checkpoint conditions against the live page."""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error, Frame
from pydantic import BaseModel

from rote.schema.condition import (
    AllOf,
    AnyOf,
    ElementAbsent,
    ElementPresent,
    LocationMatches,
    Not,
    OutputPresent,
    TextVisible,
    ValueEquals,
)
from rote.schema.target import Target
from rote.surface.web.resolver import Resolver, describe_target
from rote.surface.web.session import call


class ConditionEvaluator:
    def __init__(self, resolver: Resolver, outputs: Mapping[str, Any]) -> None:
        self.resolver = resolver
        self.outputs = outputs

    def _frames(self, name: str | None) -> list[Frame]:
        session = self.resolver.session
        if name is None:
            return list(session.page.frames)
        frame = session.frame(name)
        return [frame] if frame is not None else []

    async def holds(self, condition: BaseModel, step_target: Target | None = None) -> bool:
        if isinstance(condition, TextVisible):
            text = self.resolver.text(condition.text_visible)
            for frame in self._frames(condition.frame):
                try:
                    if await call(frame, "textVisible", text):
                        return True
                except Error:
                    continue
            return False
        if isinstance(condition, ElementPresent):
            resolved, _ = await self.resolver.resolve_once(condition.element_present)
            return resolved is not None
        if isinstance(condition, ElementAbsent):
            resolved, _ = await self.resolver.resolve_once(condition.element_absent)
            return resolved is None
        if isinstance(condition, LocationMatches):
            frames = self._frames(condition.frame) if condition.frame else [self.resolver.session.page.main_frame]
            return any(fnmatch.fnmatchcase(urlsplit(f.url).path, condition.location_matches) for f in frames)
        if isinstance(condition, ValueEquals):
            target = condition.target or step_target
            if target is None:
                return False
            resolved, _ = await self.resolver.resolve_once(target)
            if resolved is None:
                return False
            try:
                value = await resolved.element.input_value()
            except Error:
                return False
            return value == self.resolver.text(condition.value_equals)
        if isinstance(condition, OutputPresent):
            return self.outputs.get(condition.output_present) not in (None, "", [])
        if isinstance(condition, AllOf):
            for part in condition.all:
                if not await self.holds(part, step_target):
                    return False
            return True
        if isinstance(condition, AnyOf):
            for part in condition.any:
                if await self.holds(part, step_target):
                    return True
            return False
        if isinstance(condition, Not):
            return not await self.holds(condition.not_, step_target)
        raise TypeError(f"unknown condition {condition!r}")


def describe(condition: BaseModel) -> str:
    """Human-readable form for errors and logs (templates left unrendered)."""
    if isinstance(condition, TextVisible):
        where = f" in frame {condition.frame!r}" if condition.frame else ""
        return f"text {condition.text_visible!r} visible{where}"
    if isinstance(condition, ElementPresent):
        return f"{describe_target(condition.element_present)} present"
    if isinstance(condition, ElementAbsent):
        return f"{describe_target(condition.element_absent)} absent"
    if isinstance(condition, LocationMatches):
        return f"location matches {condition.location_matches!r}"
    if isinstance(condition, ValueEquals):
        return f"field value equals {condition.value_equals!r}"
    if isinstance(condition, OutputPresent):
        return f"output {condition.output_present!r} extracted"
    if isinstance(condition, AllOf):
        return " and ".join(describe(c) for c in condition.all)
    if isinstance(condition, AnyOf):
        return " or ".join(describe(c) for c in condition.any)
    if isinstance(condition, Not):
        return f"not ({describe(condition.not_)})"
    return repr(condition)
