"""The recorder: what discovery keeps about each action.

Refs are throwaway, so the recorder never stores one. For the element behind a
ref it stores the facts: frame, Playwright's own role and name, the inferred
label, the table context. It then derives ranked locator candidates and
validates every one on the live page *before* the action runs, since a click
can make the element disappear. A candidate survives only if it matches
exactly one element, and that element is the one acted on.

It also snapshots the screen before and after each action. The compiler uses
those snapshots to choose postconditions that discriminate (false before, true
after) and to drop actions that changed nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import ElementHandle, Error, Frame

from rote.discovery.cassette import templated
from rote.schema.capability import Effect
from rote.schema.target import CssLocator, InnerElement, LabelLocator, Locator, RoleLocator, TableCellLocator
from rote.surface.web.observe import Observation
from rote.surface.web.resolver import Resolver

FORM_ROLES = frozenset({"textbox", "password", "combobox", "listbox", "checkbox", "radio"})
_ARIA_LINE = re.compile(r'^- (\w+)(?: "((?:[^"\\]|\\.)*)")?')


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()


@dataclass
class ScreenState:
    fingerprint: str
    texts: dict[str, str]
    headings: dict[str, list[str]]
    messages: dict[str, list[str]]
    values: dict[str, Any]
    # UI vocabulary, never data: the labels of form fields and the column titles of data tables.
    labels: dict[str, list[str]] = field(default_factory=dict)
    columns: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def of(cls, observation: Observation) -> ScreenState:
        texts, headings, messages, values = {}, {}, {}, {}
        labels: dict[str, list[str]] = {}
        columns: dict[str, list[str]] = {}
        for f in observation.frames:
            key = f.name or "top"
            texts[key] = norm(f.text)
            headings[key] = list(f.headings)
            messages[key] = list(f.messages)
            labels[key], columns[key] = [], []
            for e in f.elements:
                label = (e.get("label") or {}).get("text")
                if e["role"] in FORM_ROLES:
                    ident = f"{key}:{e['role']}:{label or e.get('name')}"
                    values[ident] = e.get("checked") if "checked" in e else e.get("value")
                    if label and label not in labels[key]:
                        labels[key].append(label)
                for header in (e.get("table") or {}).get("headers") or []:
                    if header and header not in columns[key]:
                        columns[key].append(header)
        return cls(observation.fingerprint, texts, headings, messages, values, labels, columns)

    def shows(self, text: str) -> bool:
        wanted = norm(text)
        return any(wanted in body for body in self.texts.values())

    def frame_showing(self, text: str) -> str | None:
        wanted = norm(text)
        return next((frame for frame, body in self.texts.items() if wanted in body), None)

    def same_as(self, other: ScreenState) -> bool:
        return self.fingerprint == other.fingerprint and self.values == other.values and self.texts == other.texts


@dataclass
class TraceStep:
    index: int
    tool: str
    args: dict[str, Any]
    rationale: str | None
    frame: str | None = None
    facts: dict[str, Any] | None = None
    candidates: list[Locator] = field(default_factory=list)
    effect: Effect = "read_only"
    before: ScreenState | None = None
    after: ScreenState | None = None
    result: str = "pending"
    expect: str | None = None
    provenance: str = "llm"
    approval: str | None = None
    extracted: str | None = None


async def aria_identity(frame: Frame, ref: str) -> tuple[str, str | None] | None:
    """Role and accessible name as Playwright computes them: the same engine replay uses."""
    try:
        snapshot = await frame.locator(f'[data-rote-ref="{ref}"]').aria_snapshot(timeout=2000)
    except Error:
        return None
    match = _ARIA_LINE.match(snapshot.splitlines()[0].strip()) if snapshot.strip() else None
    if match is None:
        return None
    name = match.group(2)
    return match.group(1), (name.replace('\\"', '"') if name is not None else None)


async def _same(frame: Frame, a: ElementHandle, b: ElementHandle) -> bool:
    result: bool = await frame.evaluate("([x, y]) => x === y", [a, b])
    return result


async def _unique_and_same(resolver: Resolver, frame: Frame, locator: Locator, element: ElementHandle) -> bool:
    try:
        found = await resolver.candidates(frame, locator)
    except Error:
        return False
    return len(found) == 1 and await _same(frame, found[0], element)


async def build_candidates(
    resolver: Resolver,
    frame: Frame,
    element: ElementHandle,
    ref: str,
    facts: dict[str, Any],
    inputs: dict[str, str],
) -> list[Locator]:
    """Ranked, validated locators for ``element``. Input values become templates."""
    role = facts["role"]
    table = facts.get("table") or {}
    row_values: list[str] = table.get("row") or []
    input_values = {v for v in inputs.values() if v}
    in_input_row = any(v in input_values for v in row_values)

    proposals: list[Locator] = []

    # Cells are named after their contents ("$1,234.56"), so a role locator would bake
    # data into the artifact. Cells are found by table or label strategies instead.
    aria = await aria_identity(frame, ref) if role != "cell" else None
    role_locator: Locator | None = None
    if aria is not None and aria[1]:
        role_locator = RoleLocator(role=aria[0], name=templated(aria[1], inputs))

    # A label for a form control, or for a value cell in a label/value layout. A data-table
    # cell's "label" would just be its neighbor's value.
    label = (facts.get("label") or {}).get("text")
    label_locator: Locator | None = None
    if label and (role in FORM_ROLES or (role == "cell" and not table.get("column"))):
        label_locator = LabelLocator(text=templated(label, inputs) or label, role=role if role in FORM_ROLES else None)

    table_locators: list[Locator] = []
    headers: list[str] = table.get("headers") or []
    if headers and row_values:
        column = table.get("column")
        key_order = [i for i, h in enumerate(headers) if h and i < len(row_values) and row_values[i]
                     and h != column]
        for width in (1, 2, 3):
            keys = key_order[:width]
            if len(keys) < width:
                break
            row = {headers[i]: templated(row_values[i], inputs) or row_values[i] for i in keys}
            wanted_headers = [headers[i] for i in keys]
            if role == "cell" and column:
                table_locators.append(TableCellLocator(headers=[*wanted_headers, column], row=row, column=column))
            elif role != "cell" and facts.get("name"):
                table_locators.append(TableCellLocator(headers=wanted_headers, row=row,
                                                       then=InnerElement(role=role, name=facts["name"])))

    css_locator = CssLocator(css=facts["css"]) if facts.get("css") else None

    if in_input_row:
        # The element sits in a data row identified by an input (the result row for
        # this member): bind to the input first, or replay would click the wrong row.
        proposals.extend(table_locators)
        proposals.extend(c for c in (role_locator, label_locator) if c is not None)
    else:
        proposals.extend(c for c in (role_locator, label_locator) if c is not None)
        proposals.extend(table_locators)
    if css_locator is not None:
        proposals.append(css_locator)

    validated: list[Locator] = []
    seen_strategies: set[str] = set()
    for proposal in proposals:
        if proposal.by in seen_strategies:
            continue  # one locator per strategy: the narrowest table predicate that is unique wins
        if await _unique_and_same(resolver, frame, proposal, element):
            validated.append(proposal)
            seen_strategies.add(proposal.by)
    return validated
