"""Approvals bind a reviewer's sign-off to an exact artifact identity.

An approval names the capability's content hash and, when a tenant overlay
applies, the overlay's hash. Any edit to either produces new hashes, so an old
approval simply stops matching. Nothing is ever flipped from "approved" back to
"draft"; staleness falls out of the hashing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from rote.schema.capability import Capability
from rote.schema.config import Approval


def approvals_path(root: Path, capability_id: str) -> Path:
    return root / "approvals" / f"{capability_id}.yaml"


def load_approvals(root: Path, capability_id: str) -> list[Approval]:
    path = approvals_path(root, capability_id)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [Approval.model_validate(item) for item in data]


def find_approval(
    approvals: list[Approval],
    capability: Capability,
    *,
    tenant: str,
    overlay_hash: str | None,
) -> Approval | None:
    content_hash = capability.content_hash()
    for approval in approvals:
        if approval.content_hash != content_hash or approval.overlay_hash != overlay_hash:
            continue
        if approval.tenant is None and overlay_hash is None:
            return approval
        if approval.tenant == tenant:
            return approval
    return None


def record_approval(
    root: Path,
    capability: Capability,
    *,
    reviewer: str,
    tenant: str | None = None,
    overlay_hash: str | None = None,
    note: str | None = None,
) -> Approval:
    approval = Approval(
        capability=capability.id,
        version=capability.version,
        content_hash=capability.content_hash(),
        overlay_hash=overlay_hash,
        tenant=tenant,
        reviewer=reviewer,
        approved_at=datetime.now(UTC).replace(microsecond=0),
        note=note,
    )
    existing = load_approvals(root, capability.id)
    existing.append(approval)
    path = approvals_path(root, capability.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump([a.model_dump(mode="json", exclude_none=True) for a in existing], sort_keys=False),
        encoding="utf-8",
    )
    return approval
