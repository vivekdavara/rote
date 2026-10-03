"""`rote demo`: regenerate /evidence/ end to end, with no API key.

It runs against a fresh CoreOne mock and a throwaway workspace:

1. discovery of both capabilities, driven by cassettes. The live cassette is used
   when one has been recorded; otherwise the hand-written script, labelled as such.
2. a scripted reviewer approves the compiled artifacts (labelled as scripted)
3. every scenario the brief asks for, each copied into its own evidence folder
4. fault-matrix.md, stability.md, an MCP transcript, and a PII canary scan
5. evidence/README.md, an index with expected vs. observed for every scenario

A live discovery run (`rote discover --live`) lives in evidence/01-discovery-live/.
This command never touches that folder: live evidence is committed as recorded.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import Browser, async_playwright

from rote.control.plane import ControlPlane
from rote.devserver import MockServer, free_port, start_mock
from rote.discovery.agent import DiscoveryAgent, DiscoveryOptions
from rote.discovery.cassette import load_cassette
from rote.discovery.planner import CassettePlanner
from rote.registry.approvals import record_approval
from rote.registry.store import Workspace
from rote.replay.engine import ReplayEngine, ReplayOptions, replay
from rote.schema.result import RunResult
from rote.schema.spec import load_spec
from rote.secrets import load_dotenv

REPO = Path(__file__).resolve().parents[2]
BALANCE = "coreone.member.get_savings_balance"
SUB = "coreone.member.open_sub_account"
SUB_INPUTS = {"member_id": "100517", "share_type": "Money Market", "nickname": "Rainy Day", "deposit": "40.00",
              "funding_suffix": "S10"}
REVIEWER = "demo reviewer (scripted by `rote demo`)"
# Values that must never appear in persisted evidence (synthetic, but treated as real).
CANARIES = ["100234", "100517", "100733", "100900", "100666", "Avery Quill", "Jordan Pike", "Casey Lin",
            "900-12-0234", "900-45-0517", "1234.56", "1,234.56", "318.02", "12 Sample Lane", "(555) 010-2234"]


@dataclass
class Scenario:
    folder: str
    title: str
    expected: str
    observed: str = ""
    passed: bool = False
    notes: list[str] = field(default_factory=list)


class Demo:
    def __init__(self, evidence: Path) -> None:
        self.evidence = evidence
        self.root = Path(tempfile.mkdtemp(prefix="rote-demo-"))
        for name in ("apps", "tenants", "policies", "specs", "overlays"):
            shutil.copytree(REPO / name, self.root / name)
        self.mock: MockServer = start_mock(seed=42)
        self.workspace = Workspace(self.root, base_url_overrides={
            "harbor": self.mock.base_url("harbor"), "summit": self.mock.base_url("summit")})
        self.scenarios: list[Scenario] = []
        self.browser: Browser

    # ------------------------------------------------------------------ helpers

    def keep(self, folder: str, *run_ids: str) -> None:
        """Copy run folders (already redacted) into evidence/<folder>/."""
        target = self.evidence / folder
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for run_id in run_ids:
            for source in sorted(self.workspace.runs.glob(f"{run_id}*")):
                shutil.copytree(source, target / source.name)

    def record(self, scenario: Scenario, ok: bool, observed: str) -> None:
        scenario.passed, scenario.observed = ok, observed
        self.scenarios.append(scenario)
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {scenario.folder:<34} {observed}", flush=True)

    async def run(self, capability_id: str, inputs: dict[str, str], tenant: str = "harbor", **options: Any) -> RunResult:
        options.setdefault("step_timeout_ms", 4000)
        return await replay(self.workspace, capability_id, tenant, inputs, ReplayOptions(**options),
                            browser=self.browser)

    @staticmethod
    def code(result: RunResult) -> str:
        if result.error:
            return result.error.code
        if result.outcome:
            return result.outcome.code
        return "-"

    # ---------------------------------------------------------------- scenarios

    async def discover(self, spec_name: str, cassette: Path, folder: str, title: str) -> None:
        spec = load_spec(self.root / "specs" / spec_name)
        source = load_cassette(cassette)
        agent = DiscoveryAgent(self.workspace, spec, CassettePlanner(source, spec.example(0)), DiscoveryOptions(),
                               browser=self.browser)
        result = await agent.run()
        scenario = Scenario(folder, title, "compiled, verified on the second example, outcomes learned")
        origin = (f"the live model run ({source.model}, recorded {source.recorded_at:%Y-%m-%d}) kept in "
                  "`01-discovery-live/`" if source.model else source.source)
        scenario.notes.append(f"decisions replayed from: {origin}")
        learned = [f"{o.code}={o.status}" for o in result.outcomes]
        verified = result.verification.status if result.verification else "-"
        self.keep(folder, result.run_id)
        if result.capability_path:
            shutil.copy(result.capability_path, self.evidence / folder / "artifact.yaml")
        self.record(scenario, result.status == "compiled" and verified in ("succeeded", "preview"),
                    f"{result.status}; verify-replay {verified}; outcomes {', '.join(learned) or 'none'}")

    async def replay_case(self, folder: str, title: str, expected_status: str, expected_code: str,
                          inputs: dict[str, str] | None = None, fault: dict[str, Any] | None = None,
                          capability_id: str = BALANCE, tenant: str = "harbor", **options: Any) -> RunResult:
        if fault:
            self.mock.fault(**fault)
        result = await self.run(capability_id, inputs or {"member_id": "100234"}, tenant, **options)
        recoveries = ",".join(r.code for r in result.recoveries)
        observed = f"{result.status} {self.code(result)}" + (f" (recovered: {recoveries})" if recoveries else "")
        self.keep(folder, result.run_id)
        self.record(Scenario(folder, title, f"{expected_status} {expected_code}"),
                    result.status == expected_status and self.code(result) == expected_code, observed)
        return result

    async def handoff(self) -> None:
        self.mock.fault("unknown_modal", page="search")
        plane = ControlPlane(port=free_port(), timeout_s=60, announce=lambda message: None)
        capability = self.workspace.capability(BALANCE)
        engine = ReplayEngine(self.workspace, capability, self.workspace.tenant("harbor"),
                              self.workspace.profile("coreone"), self.workspace.policy("coreone", "harbor"),
                              {"member_id": "100234"}, ReplayOptions(attended=True, step_timeout_ms=4000),
                              browser=self.browser, escalator=plane)
        await plane.start()

        async def operator() -> None:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{plane.port}",
                                         headers={"x-rote-token": plane.token}, timeout=10) as client:
                for _ in range(900):
                    if (await client.get("/api/state")).json()["intervention"]:
                        break
                    await asyncio.sleep(0.1)
                await client.post("/api/take", json={"operator": "scripted operator (rote demo)"})
                frame = engine.web.page.frame(name="main")
                assert frame is not None
                box = await frame.get_by_role("button", name="Supervisor Override").bounding_box()
                assert box is not None
                await client.post("/api/input", json={"kind": "click", "x": box["x"] + box["width"] / 2,
                                                      "y": box["y"] + box["height"] / 2})
                await client.post("/api/handback", json={"note": "Supervisor override applied (scripted)."})

        driver = asyncio.create_task(operator())
        try:
            result = await engine.run()
            await asyncio.wait_for(driver, timeout=15)
        finally:
            driver.cancel()
            await plane.stop()
        self.keep("11-handoff-scripted-operator", result.run_id)
        ok = result.status == "succeeded" and len(result.interventions) == 1
        self.record(Scenario("11-handoff-scripted-operator", "Unknown modal -> operator takes over the live "
                             "session through the console relay -> hands back -> run resumes",
                             "succeeded after one handed-back intervention"), ok,
                    f"{result.status}; interventions: {[i.resolution for i in result.interventions]}")

    async def commit_flow(self) -> None:
        preview = await self.run(SUB, SUB_INPUTS, mode="preview")
        token = preview.preview.commit_token if preview.preview else ""
        first = await self.run(SUB, SUB_INPUTS, mode="commit", commit_token=token, idempotency_key="demo-commit-1")
        again = await self.run(SUB, SUB_INPUTS, mode="commit", commit_token=token, idempotency_key="demo-commit-1")
        tampered = await self.run(SUB, {**SUB_INPUTS, "deposit": "400.00"}, mode="commit", commit_token=token,
                                  idempotency_key="demo-commit-2")
        commits = self.mock.state()["harbor"]["commits"]
        self.keep("12-preview-commit-idempotent", preview.run_id, first.run_id, tampered.run_id)
        (self.evidence / "12-preview-commit-idempotent" / "repeat-with-same-key.json").write_text(
            json.dumps({"status": again.status, "idempotent_replay": again.idempotent_replay,
                        "commit_state": again.commit_state, "note": "returned from the idempotency ledger"},
                       indent=2) + "\n")
        ok = (preview.status == "preview" and first.commit_state == "committed" and again.idempotent_replay
              and self.code(tampered) == "COMMIT_TOKEN_INVALID")
        self.record(Scenario("12-preview-commit-idempotent", "Irreversible: preview -> commit with token -> "
                             "repeat key returns the recorded result -> changed inputs refused",
                             "preview; committed; idempotent replay; COMMIT_TOKEN_INVALID"), ok,
                    f"{preview.status}; {first.commit_state}; replay={again.idempotent_replay}; "
                    f"{self.code(tampered)}; CoreOne commits counted: {commits}")

    async def indeterminate(self) -> None:
        policy = self.root / "policies" / "coreone.harbor.yaml"
        original = policy.read_text()
        policy.write_text(original.replace("slow_load_cap_ms: 30000", "slow_load_cap_ms: 4000"))
        try:
            preview = await self.run(SUB, {**SUB_INPUTS, "nickname": "Stall Test"}, mode="preview")
            token = preview.preview.commit_token if preview.preview else ""
            self.mock.fault("confirm_timeout", ms=20000)
            stalled = await self.run(SUB, {**SUB_INPUTS, "nickname": "Stall Test"}, mode="commit",
                                     commit_token=token, idempotency_key="demo-stall", step_timeout_ms=2500)
        finally:
            policy.write_text(original)
        self.keep("13-indeterminate-commit", stalled.run_id)
        ok = self.code(stalled) == "INDETERMINATE_COMMIT" and stalled.commit_state == "unknown"
        self.record(Scenario("13-indeterminate-commit", "Confirm clicked, confirmation never arrives",
                             "failed INDETERMINATE_COMMIT, commit_state unknown, not retryable"), ok,
                    f"{stalled.status} {self.code(stalled)}; commit_state {stalled.commit_state}; "
                    f"retryable {stalled.error.retryable if stalled.error else '-'}")

    async def cross_tenant(self) -> None:
        summit = await self.run(BALANCE, {"member_id": "100234"}, tenant="summit")
        tenant_file = self.root / "tenants" / "summit.yaml"
        overlay_dir = self.root / "overlays"
        original = tenant_file.read_text()
        tenant_file.write_text(re.sub(r"labels:\n(  .*\n)+", "labels: {}\n", original))
        hidden = self.root / "overlays.off"
        overlay_dir.rename(hidden)
        try:
            drifted = await self.run(BALANCE, {"member_id": "100234"}, tenant="summit", step_timeout_ms=1500)
        finally:
            tenant_file.write_text(original)
            hidden.rename(overlay_dir)
        self.keep("14-cross-tenant", summit.run_id, drifted.run_id)
        ok = summit.status == "succeeded" and summit.overlay_hash is not None and drifted.status == "failed"
        drift = [f"{d.step_id}:{d.primary_strategy}->{d.matched_strategy}" for d in drifted.drift]
        self.record(Scenario("14-cross-tenant", "Same capability on Summit (relabeled, requires Branch): "
                             "labels + overlay succeed; without them, drift is reported and the run stops "
                             "at the real difference", "succeeded with overlay; failed without, with drift"), ok,
                    f"with overlay: {summit.status}; without: {drifted.status} {self.code(drifted)} "
                    f"at {drifted.error.step_id if drifted.error else '-'}, drift {drift}")

    async def adversarial(self) -> None:
        spec = load_spec(self.root / "specs" / "get_savings_balance.yaml")
        member = spec.inputs["member_id"].model_copy(update={"examples": ["100666", "100517"]})
        spec = spec.model_copy(update={"inputs": {"member_id": member}, "negative_examples": []})
        cassette = load_cassette(REPO / "demo" / "adversarial_injection.cassette.json")
        agent = DiscoveryAgent(self.workspace, spec, CassettePlanner(cassette, spec.example(0)),
                               DiscoveryOptions(save=False, verify=False, learn_outcomes=False),
                               browser=self.browser)
        result = await agent.run()
        events = (self.workspace.runs / result.run_id / "events.jsonl").read_text()
        blocked = '"network_blocked"' in events and "evil.localhost" in events
        self.keep("15-adversarial-injection", result.run_id)
        self.record(Scenario("15-adversarial-injection", "Labelled adversarial script obeys the prompt "
                             "injection in a member's notes and clicks the planted link",
                             "the request to evil.localhost is blocked by the network allowlist; no artifact"),
                    blocked and result.status == "failed",
                    f"network_blocked logged: {blocked}; discovery {result.status} ({result.reason})")

    async def mcp_transcript(self) -> None:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        env = {**os.environ, "ROTE_HOME": str(self.root), "ROTE_BASE_URL_HARBOR": self.mock.base_url("harbor")}
        params = StdioServerParameters(command=str(Path(sys.executable).with_name("rote")),
                                       args=["mcp", "serve", "--tenant", "harbor"], env=env, cwd=str(self.root))
        lines = ["# MCP transcript (an agent's view of the catalog)", "",
                 "`rote mcp serve --tenant harbor`, driven by the official MCP Python client.", ""]
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            lines += ["## tools/list", ""]
            for tool in tools:
                hints = tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}
                lines += [f"### `{tool.name}`", "", tool.description or "", "",
                          f"annotations: `{json.dumps(hints)}`", "",
                          "```json", json.dumps(tool.input_schema, indent=2), "```", ""]
            calls = [("coreone__member__get_savings_balance", {"member_id": "100234"}),
                     ("coreone__member__get_savings_balance", {"member_id": "999999"}),
                     ("coreone__member__open_sub_account", {**SUB_INPUTS, "nickname": "MCP Demo", "mode": "preview"})]
            outcomes = []
            for name, arguments in calls:
                reply = await session.call_tool(name, arguments)
                structured = reply.structured_content or {}
                outcomes.append((structured.get("status"), reply.is_error))
                shown = {k: structured.get(k) for k in ("status", "outputs", "outcome", "preview", "commit_state")
                         if structured.get(k) not in (None, {}, [])}
                if "preview" in shown:
                    shown["preview"] = {**shown["preview"], "commit_token": "<signed token, elided>"}
                lines += [f"## tools/call `{name}`", "", "arguments (inputs redacted below):", "",
                          f"`is_error={reply.is_error}`", "", "```json", json.dumps(shown, indent=2), "```", ""]
        (self.evidence / "16-mcp-catalog").mkdir(parents=True, exist_ok=True)
        transcript = "\n".join(lines)
        for canary in CANARIES:  # the caller legitimately gets outputs; the committed transcript must not
            transcript = transcript.replace(canary, "[redacted]")
        (self.evidence / "16-mcp-catalog" / "transcript.md").write_text(transcript)
        ok = outcomes == [("succeeded", False), ("business_outcome", False), ("preview", False)]
        self.record(Scenario("16-mcp-catalog", "An MCP client lists the approved tools and calls them",
                             "succeeded; business_outcome (not an error); preview"), ok, str(outcomes))

    async def stability(self, runs: int = 20) -> None:
        durations, statuses = [], []
        for _ in range(runs):
            result = await self.run(BALANCE, {"member_id": "100234"}, screenshots=False)
            statuses.append(result.status)
            durations.append(result.duration_ms or 0)
        passed = statuses.count("succeeded")
        p50 = statistics.median(durations)
        p95 = sorted(durations)[max(0, int(len(durations) * 0.95) - 1)]
        (self.evidence / "stability.md").write_text(
            f"# Stability\n\n{passed}/{runs} replays of `{BALANCE}` succeeded against a fresh mock "
            f"(no faults injected).\n\nDuration p50 {p50:.0f} ms, p95 {p95} ms, max {max(durations)} ms.\n")
        self.record(Scenario("stability.md", f"{runs} identical replays", f"{runs}/{runs} succeeded"),
                    passed == runs, f"{passed}/{runs}; p50 {p50:.0f} ms, p95 {p95} ms")

    # ---------------------------------------------------------------- reports

    def canary_scan(self) -> list[str]:
        hits = []
        for path in self.evidence.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".jsonl", ".txt", ".md", ".yaml"):
                body = path.read_text(encoding="utf-8", errors="ignore")
                hits += [f"{path.relative_to(self.evidence)}: {c}" for c in CANARIES if c in body]
        return hits

    def write_index(self, hits: list[str], live: bool) -> None:
        rows = "\n".join(f"| `{s.folder}` | {s.title} | {s.expected} | {s.observed} | {'yes' if s.passed else 'NO'} |"
                         for s in self.scenarios)
        notes = "\n".join(f"- `{s.folder}`: {n}" for s in self.scenarios for n in s.notes)
        live_note = ("`01-discovery-live/` holds a real model run (`rote discover --live`), committed as recorded."
                     if live else "`01-discovery-live/` has no live model run yet (see its README). "
                     "Run `rote discover specs/get_savings_balance.yaml --live` and copy the run folder there.")
        (self.evidence / "README.md").write_text(f"""# Evidence

Generated by `rote demo` on {datetime.now(UTC):%Y-%m-%d %H:%M} UTC against a fresh CoreOne mock, with no API key.
Every run folder holds `events.jsonl` (what happened and why), `result.json` (the caller's result, redacted),
masked screenshots, and on failure `failure/` (a redacted snapshot of the screen).

{live_note}

| Folder | Scenario | Expected | Observed | Matches |
|---|---|---|---|---|
{rows}

Notes:
{notes}
- Approvals in this demo were made by a scripted reviewer and the handoff in `11-` by a scripted operator
  driving the real console API. A handoff by a person through the console UI is recorded separately when
  available.

**PII canary:** scanned {sum(1 for p in self.evidence.rglob('*') if p.is_file())} files for
{len(CANARIES)} synthetic member values (member numbers, names, SSNs, balances, addresses, phones):
{'no hits.' if not hits else f'{len(hits)} HITS: ' + '; '.join(hits[:10])}

See also [fault-matrix.md](fault-matrix.md) and [stability.md](stability.md).
""")
        matrix = [s for s in self.scenarios if s.folder[:2].isdigit() and 4 <= int(s.folder[:2]) <= 10]
        (self.evidence / "fault-matrix.md").write_text(
            "# Fault matrix\n\nEach runtime condition maps to its own status and code (business outcome, "
            "recovered, rejected, or a specific hard failure). The full matrix, with more cases, runs in "
            "`tests/integration/test_fault_matrix.py`.\n\n| Condition | Expected | Observed | Matches |\n|---|---|---|---|\n"
            + "\n".join(f"| {s.title} | {s.expected} | {s.observed} | {'yes' if s.passed else 'NO'} |" for s in matrix)
            + "\n")

    # ------------------------------------------------------------------- main

    async def main(self) -> int:
        live = any(p.is_dir() for p in (self.evidence / "01-discovery-live").glob("discover-*"))
        for folder in self.evidence.glob("*"):
            if folder.name != "01-discovery-live":
                shutil.rmtree(folder) if folder.is_dir() else folder.unlink()
        (self.evidence / "01-discovery-live").mkdir(parents=True, exist_ok=True)
        live_cassette = REPO / "tests" / "fixtures" / "cassettes" / "get_savings_balance.live.json"
        balance_cassette = live_cassette if live_cassette.exists() else \
            REPO / "tests" / "fixtures" / "cassettes" / "get_savings_balance.scripted.json"
        cases: list[Callable[[], Awaitable[Any]]] = []
        async with async_playwright() as pw:
            self.browser = await pw.chromium.launch(headless=True)
            print("discovery (cassette-driven)")
            await self.discover("get_savings_balance.yaml", balance_cassette, "02-discovery-balance",
                                "Discovery of the balance capability from recorded decisions")
            await self.discover("open_sub_account.yaml", REPO / "tests" / "fixtures" / "cassettes" /
                                "open_sub_account.scripted.json", "03-discovery-irreversible",
                                "Discovery of the irreversible sub-account capability (recorded approval)")
            for capability_id in (BALANCE, SUB):
                record_approval(self.root, self.workspace.capability(capability_id), reviewer=REVIEWER)
            overlay = self.workspace.overlay(BALANCE, "summit")
            if overlay is not None:
                record_approval(self.root, self.workspace.capability(BALANCE), reviewer=REVIEWER, tenant="summit",
                                overlay_hash=overlay.content_hash())
            self.mock.reset()  # discovery ran against the sandbox (and committed once); replays start clean
            print("replay scenarios")
            cases = [
                lambda: self.replay_case("04-replay-success", "Happy path", "succeeded", "-"),
                lambda: self.replay_case("05-member-not-found", "Unknown member", "business_outcome",
                                         "MEMBER_NOT_FOUND", {"member_id": "999999"}),
                lambda: self.replay_case("06-invalid-input", "Malformed member number", "rejected", "INVALID_INPUT",
                                         {"member_id": "12ab"}),
                lambda: self.replay_case("07-interstitial-recovered", "Known security notice", "succeeded", "-",
                                         fault={"fault": "interstitial", "page": "search"}),
                lambda: self.replay_case("08-session-expired-recovered", "Session expires mid-run", "succeeded", "-",
                                         fault={"fault": "session_expire", "page": "results"}),
                lambda: self.replay_case("09-app-error", "Server error page", "failed", "APP_ERROR",
                                         fault={"fault": "app_error", "page": "results"}),
                lambda: self.replay_case("10-unknown-modal-unattended", "Unknown modal, nobody attending", "failed",
                                         "UNKNOWN_MODAL", fault={"fault": "unknown_modal", "page": "search"}),
                self.handoff, self.commit_flow, self.indeterminate, self.cross_tenant, self.adversarial,
            ]
            for case in cases:
                self.mock.clear_faults()
                await case()
            print("catalog and stability")
            await self.mcp_transcript()
            await self.stability()
            await self.browser.close()
        self.mock.stop()
        hits = self.canary_scan()
        self.write_index(hits, live)
        shutil.rmtree(self.root, ignore_errors=True)
        failed = [s.folder for s in self.scenarios if not s.passed]
        print(f"\n{len(self.scenarios) - len(failed)}/{len(self.scenarios)} scenarios matched; PII canary hits: "
              f"{len(hits)}; evidence in {self.evidence}")
        return 0 if not failed and not hits else 1


def run_demo(evidence: Path) -> int:
    for key, value in load_dotenv(REPO / ".env").items():
        os.environ.setdefault(key, value)
    os.environ.setdefault("COREONE_USERNAME", "svc_rote")
    os.environ.setdefault("COREONE_PASSWORD", "training-only-2026")
    return asyncio.run(Demo(evidence).main())
