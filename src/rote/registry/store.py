"""The on-disk workspace: capabilities, approvals, tenants, product profiles, policies, runs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from rote.schema.capability import Capability, load_capability
from rote.schema.config import Policy, ProductProfile, TenantConfig, load_yaml_model


def default_root() -> Path:
    return Path(os.environ.get("ROTE_HOME") or Path.cwd())


@dataclass
class Workspace:
    root: Path = field(default_factory=default_root)
    base_url_overrides: dict[str, str] = field(default_factory=dict)

    def capability_path(self, capability_id: str) -> Path:
        product, rest = capability_id.split(".", 1)
        return self.root / "capabilities" / product / f"{rest}.yaml"

    def capability(self, capability_id: str) -> Capability:
        return load_capability(self.capability_path(capability_id))

    def tenant(self, tenant_id: str) -> TenantConfig:
        config = load_yaml_model(TenantConfig, self.root / "tenants" / f"{tenant_id}.yaml")
        override = self.base_url_overrides.get(tenant_id) or os.environ.get(f"ROTE_BASE_URL_{tenant_id.upper()}")
        return config.model_copy(update={"base_url": override}) if override else config

    def profile(self, product: str) -> ProductProfile:
        return load_yaml_model(ProductProfile, self.root / "apps" / product / "profile.yaml")

    def policy(self, product: str, tenant_id: str) -> Policy:
        return load_yaml_model(Policy, self.root / "policies" / f"{product}.{tenant_id}.yaml")

    @property
    def runs(self) -> Path:
        return self.root / "runs"
