"""The compiler: a discovery trace in, a typed capability out.

Decisions it makes, each recorded in ``notes`` so a reviewer can see why:

* Keep actions that worked; drop failed attempts, waits, actions that changed
  nothing, and detours whose end state equals an earlier state.
* Each kept action becomes a step with the recorder's validated, ranked locators.
* Inputs become ``{{inputs.x}}`` templates everywhere: typed values, row keys,
  checkpoint text.
* A step's postcondition must *discriminate*: false before the action, true
  after. The model's proposed ``expect`` is used if it passes that test.
  Otherwise the first UI text the action brought up is used: a heading, else a
  field label, else a column title. Checkpoints never contain data, except
  echoes of inputs, which are added on purpose so a wrong-member page can never
  pass.
* The success checkpoint is the final screen's discriminating conditions plus
  every output; it is checked to be false on the starting screen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rote.discovery.recorder import ScreenState, TraceStep
from rote.schema.capability import (
    Capability,
    CheckStep,
    ClickStep,
    ExtractStep,
    FillStep,
    OutcomeSpec,
    PressStep,
    ProductRef,
    ProvenanceInfo,
    SelectStep,
    Step,
)
from rote.schema.condition import AllOf, OutputPresent, TextVisible, ValueEquals
from rote.schema.spec import GoalSpec
from rote.schema.target import Target
from rote.schema.templating import render

ACTION_TOOLS = frozenset({"click", "type_text", "select_option", "set_checkbox", "press_key", "extract"})
_DATA = re.compile(r"\d|\$")
_VERBS = {"click": "open", "type_text": "enter", "select_option": "choose", "set_checkbox": "check",
          "press_key": "press", "extract": "read"}


class CompileError(ValueError):
    pass


@dataclass
class Compiled:
    capability: Capability
    notes: list[str] = field(default_factory=list)


def slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "_".join(words[:4]) or "step"


def template_text(text: str, inputs: dict[str, str]) -> str:
    for name, value in sorted(inputs.items(), key=lambda kv: -len(kv[1])):
        if value:
            text = text.replace(value, f"{{{{inputs.{name}}}}}")
    return text


def _data_free(text: str, inputs: dict[str, str]) -> bool:
    """No data except input echoes: digits and currency are allowed only inside templates."""
    return not _DATA.search(re.sub(r"\{\{inputs\.[a-z0-9_]+\}\}", "", template_text(text, inputs)))


def _discriminates(text: str, before: ScreenState, after: ScreenState) -> bool:
    return not before.shows(text) and after.shows(text)


def _fresh_ui_text(before: ScreenState, after: ScreenState, inputs: dict[str, str]) -> tuple[str, str] | None:
    """(frame, text) for the first UI text the action brought up: a heading, else a field label, else a column title.

    Only the screen's own vocabulary qualifies, never its values, so a postcondition can't bake in a member's data.
    """
    for kind in (after.headings, after.labels, after.columns):
        for frame, texts in kind.items():
            for text in texts:
                if _discriminates(text, before, after) and _data_free(text, inputs):
                    return frame, text
    return None


def _postcondition(step: TraceStep, inputs: dict[str, str], notes: list[str]) -> Any:
    assert step.before is not None and step.after is not None
    before, after = step.before, step.after
    if step.tool == "type_text":
        return ValueEquals(value_equals=template_text(str(step.args.get("text", "")), inputs))
    if step.tool not in ("click", "press_key"):
        return None

    conditions: list[Any] = []
    expect = step.expect
    if expect and _data_free(expect, inputs) and _discriminates(expect, before, after):
        conditions.append(TextVisible(text_visible=template_text(expect, inputs), frame=after.frame_showing(expect)))
    else:
        fresh = _fresh_ui_text(before, after, inputs)
        if expect:
            instead = f"used {fresh[1]!r}, which appeared with the action" if fresh else "nothing else appeared"
            notes.append(f"step {step.index}: the model's expect {expect!r} did not discriminate; {instead}")
        if fresh:
            conditions.append(TextVisible(text_visible=fresh[1], frame=fresh[0]))
    # Input echoes: if the destination shows the input (the member number on Member
    # Detail), require it. Alone an echo may not discriminate, but next to a
    # discriminating condition it guarantees the right record, not just the right page.
    if conditions:
        for name, value in inputs.items():
            if value and after.shows(value):
                conditions.append(TextVisible(text_visible=f"{{{{inputs.{name}}}}}", frame=after.frame_showing(value)))
    if not conditions:
        notes.append(f"step {step.index}: no discriminating change found; the step has no postcondition")
        return None
    return conditions[0] if len(conditions) == 1 else AllOf(all=conditions)


def _without_detours(steps: list[TraceStep], notes: list[str]) -> list[TraceStep]:
    """Drop navigation that came back to where it started (opened the wrong menu, went back).

    Only clicks and key presses close a detour; reads never move the screen. A segment that
    extracted an output or did anything irreversible is never dropped.
    """
    kept: list[TraceStep] = []
    for step in steps:
        assert step.before is not None and step.after is not None
        if step.tool in ("click", "press_key") and step.effect == "read_only":
            loop_start = next((i for i, k in enumerate(kept) if k.before is not None
                               and k.before.same_as(step.after)), None)
            segment = kept[loop_start:] if loop_start is not None else []
            if loop_start is not None and not any(k.tool == "extract" or k.effect != "read_only" for k in segment):
                dropped = [k.index for k in segment] + [step.index]
                notes.append(f"dropped detour steps {dropped}: they ended where they started")
                del kept[loop_start:]
                continue
        kept.append(step)
    return kept


def _step_from_trace(step: TraceStep, step_id: str, inputs: dict[str, str], notes: list[str]) -> Step:
    target = Target(frame=step.frame, locators=step.candidates, description=_describe(step))
    common: dict[str, Any] = {"id": step_id, "intent": step.rationale, "effect": step.effect,
                              "expect": _postcondition(step, inputs, notes),
                              "provenance": "human" if step.provenance == "human" else "llm"}
    args = step.args
    if step.tool == "click":
        return ClickStep(target=target, **common)
    if step.tool == "type_text":
        return FillStep(target=target, value=template_text(str(args["text"]), inputs), **common)
    if step.tool == "select_option":
        return SelectStep(target=target, option=_option(step, inputs), **common)
    if step.tool == "set_checkbox":
        return CheckStep(target=target, checked=bool(args["checked"]), **common)
    if step.tool == "press_key":
        return PressStep(key=str(args["key"]), target=target if step.candidates else None, **common)
    raise CompileError(f"cannot compile tool {step.tool!r}")


def _option(step: TraceStep, inputs: dict[str, str]) -> str:
    """The option to select at replay: an input placeholder when the chosen option's value or label is an input.

    Templating inside a label ("S00 - Primary Savings" -> "{{inputs.funding_suffix}} - Primary Savings") would
    break as soon as the input picks a different account, so the whole option is bound or none of it is.
    """
    chosen = render(str(step.args["option"]), {"inputs": inputs})
    options = (step.facts or {}).get("options") or []
    option = next((o for o in options if chosen in (o["label"], o["value"]) or o["label"].startswith(chosen + " ")),
                  None)
    for name, value in inputs.items():
        if value and option is not None and value in (option["value"], option["label"]):
            return f"{{{{inputs.{name}}}}}"
        if value and value == chosen:
            return f"{{{{inputs.{name}}}}}"
    return option["label"] if option is not None else chosen


def _describe(step: TraceStep) -> str | None:
    facts = step.facts or {}
    label = (facts.get("label") or {}).get("text")
    if facts.get("role") == "cell":
        column = (facts.get("table") or {}).get("column")
        return f"value in column {column!r}" if column else f"value labeled {label!r}"
    name = facts.get("name")
    return f"{facts.get('role')} {name!r}" if name else (f"{facts.get('role')} labeled {label!r}" if label else None)


def compile_trace(
    spec: GoalSpec,
    trace: list[TraceStep],
    *,
    start: ScreenState,
    run_id: str,
    model: str | None,
    served_by: list[str],
    tenant: str,
) -> Compiled:
    notes: list[str] = []
    inputs = spec.example(0)
    usable = [t for t in trace if t.tool in ACTION_TOOLS and t.result in ("ok", "extracted")
              and t.before is not None and t.after is not None]
    dropped = [t.index for t in trace if t.tool in ACTION_TOOLS and t not in usable]
    if dropped:
        notes.append(f"dropped failed or blocked actions {dropped}")

    no_ops = [t for t in usable if t.tool in ("click", "press_key") and t.effect == "read_only"
              and t.before.same_as(t.after)]  # type: ignore[union-attr]
    if no_ops:
        notes.append(f"dropped actions that changed nothing {[t.index for t in no_ops]}")
    usable = [t for t in usable if t not in no_ops]
    usable = _without_detours(usable, notes)

    extracts = {t.args.get("output_name"): t for t in usable if t.tool == "extract"}
    missing = [name for name in spec.outputs if name not in extracts]
    if missing:
        raise CompileError(f"the run never extracted outputs {missing}")

    steps: list[Step] = []
    used_ids: set[str] = set()
    for t in usable:
        if not t.candidates:
            raise CompileError(f"step {t.index} ({t.tool}) has no locator that held up on the live page")
        facts = t.facts or {}
        base = f"{_VERBS[t.tool]}_" + slug(str(t.args.get("output_name")) if t.tool == "extract" else
                                         facts.get("name") or (facts.get("label") or {}).get("text") or t.tool)
        step_id, n = base, 2
        while step_id in used_ids:
            step_id, n = f"{base}_{n}", n + 1
        used_ids.add(step_id)
        if t.tool == "extract":
            name = str(t.args["output_name"])
            if extracts.get(name) is not t:
                continue  # extracted twice: keep the last read
            steps.append(ExtractStep(
                id=step_id, intent=t.rationale, output=name, parse=spec.outputs[name].type,
                target=Target(frame=t.frame, locators=t.candidates, description=_describe(t)),
                provenance="human" if t.provenance == "human" else "llm",
            ))
        else:
            steps.append(_step_from_trace(t, step_id, inputs, notes))

    # Success: the final screen's discriminating conditions plus every output, each
    # checked to be false on the screen the run started from.
    final_screen = next((s for s in reversed(steps) if isinstance(s, ClickStep | PressStep) and s.expect), None)
    candidates: list[Any] = []
    if final_screen is not None and final_screen.expect is not None:
        expect = final_screen.expect
        candidates.extend(expect.all if isinstance(expect, AllOf) else [expect])
    success_parts: list[Any] = []
    for part in candidates:
        literal = isinstance(part, TextVisible) and "{{" not in part.text_visible
        if literal and start.shows(part.text_visible):
            notes.append(f"success condition {part.text_visible!r} already holds at the start; dropped")
            continue
        success_parts.append(part)
    success_parts.extend(OutputPresent(output_present=name) for name in spec.outputs)

    effects = {s.effect for s in steps}
    side_effects = "irreversible" if "irreversible" in effects else ("reversible" if "reversible" in effects else "none")
    capability = Capability(
        id=spec.capability_id,
        version="0.1.0",
        summary=spec.summary,
        product=ProductRef(name=spec.product, versions=spec.product_versions),
        side_effects=side_effects,  # type: ignore[arg-type]
        inputs={name: s.contract() for name, s in spec.inputs.items()},
        outputs=dict(spec.outputs),
        preview_outputs=_preview_outputs(steps),
        outcomes={},
        requires=[f"session:{spec.product}"],
        steps=steps,
        success=success_parts[0] if len(success_parts) == 1 else AllOf(all=success_parts),
        provenance=ProvenanceInfo(
            source="discovered", run_id=run_id, model=model, served_by=sorted(set(served_by)) or None,
            recorded_on=tenant, recorded_at=datetime.now(UTC).replace(microsecond=0),
        ),
    )
    return Compiled(capability, notes)


def _preview_outputs(steps: list[Step]) -> list[str]:
    first = next((i for i, step in enumerate(steps) if step.effect == "irreversible"), None)
    if first is None:
        return []
    return [step.output for step in steps[:first] if isinstance(step, ExtractStep)]


def with_outcome(capability: Capability, code: str, description: str | None, after_step: str,
                 detector: Any) -> Capability:
    """Add a business outcome and revalidate the whole artifact."""
    data = capability.model_dump(mode="json", by_alias=True, exclude_none=True)
    outcome = OutcomeSpec(description=description, after_step=after_step, when=detector)
    data["outcomes"][code] = outcome.model_dump(mode="json", by_alias=True, exclude_none=True)
    return Capability.model_validate(data)
