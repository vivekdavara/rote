"""Configuration around capabilities: product profiles, tenants, policies, approvals.

* A **product profile** belongs to a vendor product (CoreOne) and is shared by every
  tenant running it: the login flow, interrupts every capability inherits, and the
  version fingerprint.
* A **tenant** is one institution's deployment: base URL, product version, label
  dictionary, variables, and the environment variables its secrets come from.
* A **policy** is what the agent may do for one tenant.
* An **approval** binds a reviewer's sign-off to an exact content hash.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, TypeVar
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from rote.schema.capability import Step
from rote.schema.condition import Condition

ActionName = Literal["click", "fill", "select", "check", "press", "extract"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ product profile


class InterruptSpec(_Model):
    """A product-level condition any step can run into."""

    code: str
    kind: Literal["recoverable", "failure"]
    when: Condition
    handle: Literal["steps", "relogin_restart", "restart"] | None = None
    steps: list[Step] = Field(default_factory=list)
    max_attempts: int = 2
    description: str | None = None


class DialogRule(_Model):
    message_contains: str
    respond: Literal["accept", "dismiss"]


class Fingerprint(_Model):
    frame: str | None = None
    version_regex: str


class RedactionSpec(_Model):
    mask_labels: list[str] = Field(
        default_factory=list, description="In screenshots, mask the value cell next to these labels."
    )
    mask_columns: list[str] = Field(default_factory=list, description="In screenshots, mask these table columns.")


class ProductProfile(_Model):
    product: str
    entry_path: str = "/login"
    login: list[Step]
    login_success: Condition
    fingerprint: Fingerprint | None = None
    loading: Condition | None = Field(default=None, description="True while the app shows its loading indicator.")
    interrupts: list[InterruptSpec] = Field(default_factory=list)
    dialogs: list[DialogRule] = Field(default_factory=list)
    redaction: RedactionSpec = Field(default_factory=RedactionSpec)


# -------------------------------------------------------------------------- tenant


class TenantConfig(_Model):
    id: str
    product: str
    display_name: str
    base_url: str
    product_version: str
    labels: dict[str, str] = Field(
        default_factory=dict, description="Tenant-wide relabels: base-product text -> this tenant's text."
    )
    vars: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict, description="Secret name -> environment variable.")

    @property
    def origin(self) -> str:
        parts = urlsplit(self.base_url)
        return f"{parts.scheme}://{parts.netloc}"

    def template_vars(self) -> dict[str, str]:
        return {"base_url": self.base_url.rstrip("/"), "origin": self.origin, "id": self.id, **self.vars}

    def relabel(self, text: str) -> str:
        return self.labels.get(text, text)


# -------------------------------------------------------------------------- policy


class RiskRule(_Model):
    role: str | None = None
    name_pattern: str | None = Field(default=None, description="Regex over the control's accessible name.")
    route_pattern: str | None = Field(default=None, description="Glob over the frame's URL path.")
    effect: Literal["reversible", "irreversible"] = "irreversible"


class IrreversibleHandling(_Model):
    discovery: Literal["require_operator_approval", "block"] = "require_operator_approval"
    replay: Literal["require_commit_token", "block"] = "require_commit_token"


class Limits(_Model):
    max_steps: int = 30
    max_seconds: int = 600
    step_timeout_ms: int = 10_000
    slow_load_cap_ms: int = 30_000
    max_recoveries_per_step: int = 3
    max_restarts: int = 1
    escalation_timeout_s: int = 900


class Policy(_Model):
    product: str
    tenant: str
    allowed_origins: list[str] = Field(description="Origins the browser may contact; templates allowed.")
    allowed_routes: list[str] = Field(description="Globs over URL paths the agent may act on.")
    allowed_actions: list[ActionName]
    risk_rules: list[RiskRule] = Field(default_factory=list)
    on_irreversible: IrreversibleHandling = Field(default_factory=IrreversibleHandling)
    limits: Limits = Field(default_factory=Limits)


# ------------------------------------------------------------------------ approval


class Approval(_Model):
    capability: str
    version: str
    content_hash: str
    overlay_hash: str | None = None
    tenant: str | None = Field(default=None, description="Omitted: valid for tenants without an overlay.")
    reviewer: str
    approved_at: datetime
    note: str | None = None


# ---------------------------------------------------------------------------- I/O

M = TypeVar("M", bound=BaseModel)


def load_yaml_model(model: type[M], path: str | Path) -> M:
    return model.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
