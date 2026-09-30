"""Perform step actions on resolved elements, and read and parse extracted values."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from playwright.async_api import ElementHandle

ACTION_TIMEOUT_MS = 5000


class OptionNotFound(ValueError):
    pass


class ParseError(ValueError):
    pass


async def click(element: ElementHandle) -> None:
    await element.click(timeout=ACTION_TIMEOUT_MS)


async def fill(element: ElementHandle, value: str) -> None:
    await element.fill(value, timeout=ACTION_TIMEOUT_MS)


async def select(element: ElementHandle, option: str) -> str:
    """Select by exact visible label, then by value, then by unique label prefix ("S00 - ...")."""
    options: list[dict[str, str]] = await element.evaluate(
        "s => Array.from(s.options).map(o => ({label: o.text.trim(), value: o.value}))"
    )
    if any(o["label"] == option for o in options):
        await element.select_option(label=option, timeout=ACTION_TIMEOUT_MS)
        return "label"
    if any(o["value"] == option for o in options):
        await element.select_option(value=option, timeout=ACTION_TIMEOUT_MS)
        return "value"
    prefixed = [o for o in options if o["label"].startswith(option + " ")]
    if len(prefixed) == 1:
        await element.select_option(label=prefixed[0]["label"], timeout=ACTION_TIMEOUT_MS)
        return "label-prefix"
    raise OptionNotFound(f"no option matches {option!r}")


async def set_checked(element: ElementHandle, checked: bool) -> None:
    await element.set_checked(checked, timeout=ACTION_TIMEOUT_MS)


async def press(element: ElementHandle, key: str) -> None:
    await element.press(key, timeout=ACTION_TIMEOUT_MS)


async def read(element: ElementHandle) -> str:
    """The text a person would read from this element."""
    text: str = await element.evaluate(
        """el => {
            const tag = el.tagName.toLowerCase();
            if (tag === 'select') return el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : '';
            if (tag === 'input' || tag === 'textarea') return el.value;
            return el.innerText;
        }"""
    )
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()


def parse(text: str, kind: str) -> Any:
    raw = text.strip()
    try:
        if kind == "string":
            return raw
        if kind in ("money", "decimal"):
            negative = raw.startswith("-") or (raw.startswith("(") and raw.endswith(")"))
            digits = re.sub(r"[^0-9.]", "", raw)
            if not digits:
                raise ParseError(f"no number in {raw!r}")
            amount = Decimal(digits) * (-1 if negative else 1)
            if kind == "money":
                return {"amount": f"{amount:.2f}", "currency": "USD"}
            return str(amount)
        if kind == "integer":
            digits = re.sub(r"[^0-9-]", "", raw)
            return int(digits)
        if kind == "date":
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y"):
                try:
                    return datetime.strptime(raw, fmt).date().isoformat()
                except ValueError:
                    continue
            raise ParseError(f"unrecognized date {raw!r}")
        if kind == "boolean":
            lowered = raw.lower()
            if lowered in ("yes", "y", "true", "on", "checked", "1"):
                return True
            if lowered in ("no", "n", "false", "off", "unchecked", "0"):
                return False
            raise ParseError(f"not a yes/no value: {raw!r}")
    except (InvalidOperation, ValueError) as exc:
        raise ParseError(str(exc)) from exc
    raise ParseError(f"unknown type {kind!r}")
