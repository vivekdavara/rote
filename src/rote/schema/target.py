"""How a recorded step finds its control on screen.

A target carries several locator strategies, ranked most robust first. Replay tries
them in order and requires exactly one match. If a lower-ranked strategy wins, the
result records drift instead of failing. Only ``css`` depends on markup structure,
and it is marked brittle.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleLocator(_Model):
    """Accessible role and name, as the browser's accessibility engine computes them.

    Accessibility trees exist on desktop too (UIA, macOS AX), so this is the
    strategy that carries over to non-web surfaces.
    """

    by: Literal["role"] = "role"
    role: str
    name: str | None = None


class LabelLocator(_Model):
    """A form control identified by its visible label.

    Resolution order: an explicit ``<label>`` or aria label; then the text in the
    adjacent table cell on the same row (the usual legacy layout); then the nearest
    text to the left of or above the control.
    """

    by: Literal["label"] = "label"
    text: str
    role: str | None = None


class InnerElement(_Model):
    role: str
    name: str | None = None


class TableCellLocator(_Model):
    """A cell, or an element inside a row, found by header text and row values.

    ``headers`` pick the table (the innermost table whose header row contains all of
    them). ``row`` picks exactly one row by cell values, and those values may be
    templates such as ``{{inputs.member_id}}``. The locator then points at either
    the cell in ``column`` or the element described by ``then`` within that row.
    """

    by: Literal["table_cell"] = "table_cell"
    headers: list[str] = Field(min_length=1)
    row: dict[str, str] = Field(min_length=1)
    column: str | None = None
    then: InnerElement | None = None

    @model_validator(mode="after")
    def _one_destination(self) -> TableCellLocator:
        if (self.column is None) == (self.then is None):
            raise ValueError("table_cell locator needs exactly one of 'column' or 'then'")
        return self


class CssLocator(_Model):
    """Structural CSS path. Last resort: legacy markup and per-build ids break it."""

    by: Literal["css"] = "css"
    css: str
    brittle: bool = True


Locator = Annotated[
    RoleLocator | LabelLocator | TableCellLocator | CssLocator,
    Field(discriminator="by"),
]


class Target(_Model):
    """A control within one frame, with ranked locator strategies."""

    frame: str | None = Field(default=None, description="Frame name; omitted for the top-level document.")
    locators: list[Locator] = Field(min_length=1)
    description: str | None = None
