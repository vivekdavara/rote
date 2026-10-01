"""In-memory state for the mock: tenants, members, sessions, pending reviews, faults."""

from __future__ import annotations

import copy
import random
import secrets
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).parent

LOGICAL_FIELDS = (
    "login_user",
    "login_pass",
    "search_member",
    "search_last",
    "search_branch",
    "search_go",
    "sub_type",
    "sub_nick",
    "sub_deposit",
    "sub_funding",
    "sub_disclosure",
)

FAULT_NAMES = (
    "interstitial",
    "unknown_modal",
    "js_dialog",
    "session_expire",
    "latency",
    "app_error",
    "app_unavailable",
    "confirm_timeout",
)


def field_names(seed: int, tenant: str) -> dict[str, str]:
    """ASP.NET-style control names that change with the seed and the tenant.

    Legacy builds regenerate names like these, so anything keyed on them breaks.
    Tests run several seeds to prove rote's locators never depend on them.
    """
    rng = random.Random(f"{seed}:{tenant}")
    return {name: f"ctl00$mc$f{rng.randrange(16**6):06x}" for name in LOGICAL_FIELDS}


@dataclass
class Fault:
    name: str
    page: str | None
    remaining: int
    ms: int = 0
    message: str | None = None


@dataclass
class Session:
    sid: str
    tenant: str
    user: str
    expired: bool = False
    last_results: list[str] = field(default_factory=list)
    reviews: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class TenantState:
    config: dict[str, Any]
    names: dict[str, str]
    members: dict[str, dict[str, Any]]
    faults: list[Fault] = field(default_factory=list)
    commits: int = 0
    processed_reviews: set[str] = field(default_factory=set)
    next_confirmation: int = 4512

    @property
    def reverse_names(self) -> dict[str, str]:
        return {v: k for k, v in self.names.items()}


class AppState:
    def __init__(self, seed: int, faults_enabled: bool) -> None:
        self.seed = seed
        self.faults_enabled = faults_enabled
        self._tenant_configs = {
            p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted((HERE / "tenants").glob("*.yaml"))
        }
        self._members_seed = yaml.safe_load((HERE / "seed" / "members.yaml").read_text(encoding="utf-8"))
        self.operators = {
            op["user"]: op for op in yaml.safe_load((HERE / "seed" / "operators.yaml").read_text(encoding="utf-8"))
        }
        self.sessions: dict[str, Session] = {}
        self.tenants: dict[str, TenantState] = {}
        self.reset()

    def reset(self) -> None:
        self.sessions.clear()
        self.tenants = {
            tid: TenantState(
                config=cfg,
                names=field_names(self.seed, tid),
                members={m["member_id"]: copy.deepcopy(m) for m in self._members_seed},
            )
            for tid, cfg in self._tenant_configs.items()
        }

    # -- sessions --------------------------------------------------------------

    def new_session(self, tenant: str, user: str) -> Session:
        session = Session(sid=secrets.token_hex(12), tenant=tenant, user=user)
        self.sessions[session.sid] = session
        return session

    # -- faults ------------------------------------------------------------------

    def add_fault(self, tenant: str, name: str, page: str | None, times: int, ms: int,
                  message: str | None = None) -> None:
        if name not in FAULT_NAMES:
            raise ValueError(f"unknown fault {name!r}; expected one of {FAULT_NAMES}")
        self.tenants[tenant].faults.append(Fault(name=name, page=page, remaining=times, ms=ms, message=message))

    def take_fault(self, tenant: str, name: str, page: str) -> Fault | None:
        for fault in self.tenants[tenant].faults:
            if fault.name == name and fault.remaining > 0 and fault.page in (None, page):
                fault.remaining -= 1
                return fault
        return None


def money(value: str | Decimal) -> str:
    amount = Decimal(value)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"
