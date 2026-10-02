"""Tenant overlays: one base capability per vendor product, small per-tenant patches.

Hundreds of institutions run the same vendor product, configured differently.
Re-recording a capability per tenant doesn't scale. Instead, differences are
absorbed in two layers:

1. **Labels** (``tenants/<tenant>.yaml``): a tenant-wide dictionary of relabels
   ("Member Search" -> "Find Member"), applied at resolution time. Pure
   rebranding needs no per-capability change at all.
2. **Overlays** (``overlays/<tenant>/<capability>.yaml``): structural patches
   keyed by step id. For example, insert a Branch selection that this tenant's
   configuration requires.

An overlay may change *how* (the procedure) but never *what* (the contract):
applying it must leave the contract hash unchanged, so a calling agent sees the
same capability on every tenant. Approvals bind to the base content hash plus
the overlay hash, so editing either voids the approval.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from rote.schema.capability import Capability, Step
from rote.schema.hashing import sha256_hex
from rote.schema.target import Target


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InsertStep(_Model):
    op: Literal["insert_after", "insert_before"]
    step: str
    new_step: Step


class ReplaceStep(_Model):
    op: Literal["replace_step"]
    step: str
    new_step: Step


class RemoveStep(_Model):
    op: Literal["remove_step"]
    step: str


class ReplaceTarget(_Model):
    op: Literal["replace_target"]
    step: str
    target: Target


Patch = Annotated[InsertStep | ReplaceStep | RemoveStep | ReplaceTarget, Field(discriminator="op")]


class Overlay(_Model):
    overlay_for: str
    base_major: int = Field(description="Applies to base versions with this major version.")
    tenant: str
    version: str
    reason: str
    patches: list[Patch] = Field(min_length=1)

    def content_hash(self) -> str:
        return sha256_hex(self.model_dump(mode="json", by_alias=True, exclude_none=True))


class OverlayError(ValueError):
    pass


def load_overlay(path: str | Path) -> Overlay:
    return Overlay.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def apply_overlay(base: Capability, overlay: Overlay) -> Capability:
    """The effective capability for one tenant. Raises OverlayError if the overlay doesn't fit."""
    if overlay.overlay_for != base.id:
        raise OverlayError(f"overlay is for {overlay.overlay_for!r}, not {base.id!r}")
    major = int(base.version.split(".")[0])
    if major != overlay.base_major:
        raise OverlayError(f"overlay targets major version {overlay.base_major}; the base is {base.version}")
    data: dict[str, Any] = base.model_dump(mode="json", by_alias=True, exclude_none=True)
    steps: list[dict[str, Any]] = data["steps"]
    for patch in overlay.patches:
        ids = [s["id"] for s in steps]
        if patch.step not in ids:
            raise OverlayError(f"patch {patch.op} names step {patch.step!r}, which the base no longer has")
        index = ids.index(patch.step)
        if isinstance(patch, InsertStep):
            new = patch.new_step.model_dump(mode="json", by_alias=True, exclude_none=True)
            steps.insert(index + 1 if patch.op == "insert_after" else index, new)
        elif isinstance(patch, ReplaceStep):
            steps[index] = patch.new_step.model_dump(mode="json", by_alias=True, exclude_none=True)
        elif isinstance(patch, RemoveStep):
            del steps[index]
        elif isinstance(patch, ReplaceTarget):
            steps[index]["target"] = patch.target.model_dump(mode="json", by_alias=True, exclude_none=True)
    try:
        effective = Capability.model_validate(data)
    except ValueError as exc:
        raise OverlayError(f"the patched capability is invalid: {exc}") from exc
    if effective.contract_hash() != base.contract_hash():
        raise OverlayError("overlays may change the procedure, never the contract (inputs, outputs, outcomes)")
    return effective
