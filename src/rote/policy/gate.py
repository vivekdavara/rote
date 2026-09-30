"""The policy gate: what the agent may do for one tenant, checked in code.

The gate sits between "a step wants to act" and "the actuator acts", and it runs
the same way for discovery (model proposals) and replay (artifact steps). A
prompt tells the model the rules; this is what enforces them.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from rote.schema.capability import Capability, Effect
from rote.schema.config import Policy
from rote.schema.templating import render

_RANK = {"read_only": 0, "reversible": 1, "irreversible": 2}


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    rule: str
    reason: str
    runtime_effect: Effect = "read_only"


class PolicyGate:
    def __init__(self, policy: Policy, tenant_vars: dict[str, str]) -> None:
        self.policy = policy
        self.origins = {render(o, {"tenant": tenant_vars}).rstrip("/") for o in policy.allowed_origins}

    # -- static checks, before a run touches the UI --------------------------------

    def static_violations(self, capability: Capability) -> list[str]:
        problems = []
        if capability.product.name != self.policy.product:
            problems.append(f"capability is for product {capability.product.name!r}, policy for {self.policy.product!r}")
        for step in capability.steps:
            if step.action not in self.policy.allowed_actions:
                problems.append(f"step {step.id}: action {step.action!r} is not allowed")
            if step.effect == "irreversible" and self.policy.on_irreversible.replay == "block":
                problems.append(f"step {step.id}: irreversible steps are blocked for this tenant")
        return problems

    # -- network and routes -----------------------------------------------------------

    def origin_allowed(self, url: str) -> bool:
        if url.startswith(("data:", "about:", "blob:")):
            return True
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}" in self.origins

    def route_allowed(self, url: str) -> bool:
        path = urlsplit(url).path or "/"
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.policy.allowed_routes)

    # -- per action -------------------------------------------------------------------

    def classify(self, facts: dict[str, Any], frame_url: str) -> Effect:
        """How risky is this control? Decided from what it is, not from what the artifact claims."""
        path = urlsplit(frame_url).path or "/"
        effect: Effect = "read_only"
        name = facts.get("name") or facts.get("text") or ""
        for rule in self.policy.risk_rules:
            if rule.role is not None and facts.get("role") != rule.role:
                continue
            if rule.name_pattern is not None and not re.search(rule.name_pattern, name):
                continue
            if rule.route_pattern is not None and not fnmatch.fnmatchcase(path, rule.route_pattern):
                continue
            if _RANK[rule.effect] > _RANK[effect]:
                effect = rule.effect
        return effect

    def check_action(
        self, action: str, facts: dict[str, Any], frame_url: str, declared_effect: Effect
    ) -> GateDecision:
        if action not in self.policy.allowed_actions:
            return GateDecision(False, "allowed_actions", f"action {action!r} is not allowed for this tenant")
        if not self.origin_allowed(frame_url):
            return GateDecision(False, "allowed_origins", "the frame is on an origin outside the allowlist")
        if not self.route_allowed(frame_url):
            return GateDecision(False, "allowed_routes", f"route {urlsplit(frame_url).path!r} is not allowlisted")
        runtime = self.classify(facts, frame_url) if action in ("click", "press") else "read_only"
        if _RANK[runtime] > _RANK[declared_effect]:
            return GateDecision(
                False,
                "risk_rules",
                f"this control classifies as {runtime} but the step declares {declared_effect}",
                runtime,
            )
        return GateDecision(True, "ok", "allowed", runtime)
