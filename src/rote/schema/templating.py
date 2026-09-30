"""Minimal ``{{namespace.name}}`` templating for artifacts.

Deliberately not Jinja: an artifact may substitute values but must never contain
logic. The namespaces are:

* ``inputs``: per-invocation parameters from the capability contract
* ``tenant``: tenant configuration variables (``base_url``, ``default_branch``, ...)
* ``secrets``: resolved at run time from the environment and never serialized
  (product-profile login flows only; capabilities may not reference secrets)
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

TEMPLATE = re.compile(r"\{\{\s*([a-z_]+)\.([A-Za-z0-9_.\-]+)\s*\}\}")
NAMESPACES = frozenset({"inputs", "tenant", "secrets"})


class TemplateError(ValueError):
    pass


def references(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in TEMPLATE.finditer(text)]


def render(text: str, context: Mapping[str, Mapping[str, str]]) -> str:
    def substitute(match: re.Match[str]) -> str:
        namespace, name = match.group(1), match.group(2)
        if namespace not in NAMESPACES:
            raise TemplateError(f"unknown template namespace '{namespace}' in {match.group(0)}")
        values = context.get(namespace, {})
        if name not in values:
            raise TemplateError(f"no value for {match.group(0)}")
        return str(values[name])

    return TEMPLATE.sub(substitute, text)


def iter_strings(value: Any) -> Iterator[str]:
    """Yield every string inside nested dicts/lists (keys included)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from iter_strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from iter_strings(item)
