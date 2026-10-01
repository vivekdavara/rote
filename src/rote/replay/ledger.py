"""The idempotency ledger: one row per (capability, tenant, idempotency key).

"The agent retried and opened two sub-accounts" is the first thing a bank asks
about. A commit claims its key before clicking the irreversible control. A
repeated key returns the recorded result instead of running again, and a key
still in progress is refused. An unknown commit outcome is recorded as such and
never retried.

SQLite in ``.rote/`` (gitignored). In production this is the system-of-record
store: encrypted at rest and shared by every worker.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rote.schema.result import RunResult


class Ledger:
    def __init__(self, workspace_root: Path) -> None:
        self.path = workspace_root / ".rote" / "ledger.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS ledger (capability TEXT, tenant TEXT, key TEXT, state TEXT, "
                "result TEXT, created_at TEXT, updated_at TEXT, PRIMARY KEY (capability, tenant, key))"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, isolation_level="IMMEDIATE")

    def lookup(self, capability: str, tenant: str, key: str) -> tuple[str, RunResult | None] | None:
        with self._connect() as db:
            row = db.execute("SELECT state, result FROM ledger WHERE capability=? AND tenant=? AND key=?",
                             (capability, tenant, key)).fetchone()
        if row is None:
            return None
        state, result = row
        return state, RunResult.model_validate_json(result) if result else None

    def claim(self, capability: str, tenant: str, key: str) -> bool:
        """Claim the key for a commit about to happen. False if it is already taken."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            try:
                db.execute("INSERT INTO ledger VALUES (?, ?, ?, 'in_progress', NULL, ?, ?)",
                           (capability, tenant, key, now, now))
            except sqlite3.IntegrityError:
                return False
        return True

    def record(self, capability: str, tenant: str, key: str, result: RunResult) -> None:
        state = {"committed": "committed", "unknown": "unknown"}.get(result.commit_state, result.status)
        with self._connect() as db:
            db.execute("UPDATE ledger SET state=?, result=?, updated_at=? WHERE capability=? AND tenant=? AND key=?",
                       (state, result.model_dump_json(), datetime.now(UTC).isoformat(), capability, tenant, key))

    def release(self, capability: str, tenant: str, key: str) -> None:
        """Nothing irreversible happened (the run stopped before the commit): free the key."""
        with self._connect() as db:
            db.execute("DELETE FROM ledger WHERE capability=? AND tenant=? AND key=? AND state='in_progress'",
                       (capability, tenant, key))

    def rows(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(zip(("capability", "tenant", "key", "state"), r, strict=True))
                    for r in db.execute("SELECT capability, tenant, key, state FROM ledger")]
