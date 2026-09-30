"""Resolve tenant secrets from the environment (or a local, gitignored .env) at run time.

Secret values are never written into artifacts, logs or results. Product-profile
login steps reference them as ``{{secrets.<name>}}``.
"""

from __future__ import annotations

import os
from pathlib import Path

from rote.schema.config import TenantConfig


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_secrets(tenant: TenantConfig, workspace: Path) -> tuple[dict[str, str], list[str]]:
    """Return (values, names of missing secrets)."""
    dotenv = load_dotenv(workspace / ".env")
    values: dict[str, str] = {}
    missing: list[str] = []
    for name, env_var in tenant.secrets.items():
        value = os.environ.get(env_var) or dotenv.get(env_var)
        if value:
            values[name] = value
        else:
            missing.append(f"{name} (set {env_var})")
    return values, missing
