"""Command-line entry point.

    rote app                         run the CoreOne mock (the proxy target)
    rote run <capability> ...        replay a capability (no model in the loop)
    rote review <capability>         show the contract, steps, hash and approval state
    rote approve <capability> ...    record a reviewer's approval of the exact content hash
    rote show <run-id>               print a run's evidence timeline
    rote schema [--check]            export the capability JSON Schema
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from rote import __version__
from rote.schema.capability import Capability

SCHEMA_PATH = Path(__file__).parent / "schema" / "capability.schema.json"

app = typer.Typer(
    help="rote: record a legacy-app workflow once with an LLM, then replay it deterministically.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
console = Console()


@app.callback()
def main() -> None:
    """rote command line."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command("app")
def serve_mock_app(
    port: int = typer.Option(8400, help="Port for the mock app. Tenants are harbor.localhost and summit.localhost."),
    seed: int | None = typer.Option(None, help="Seed for the randomized control names (default: random)."),
    faults: bool = typer.Option(True, "--faults/--no-faults", help="Enable the /__faults injection API."),
) -> None:
    """Run the CoreOne mock legacy app (the proxy target)."""
    import os

    import uvicorn

    from mockapp.app import create_app

    if faults:
        os.environ["COREONE_FAULTS"] = "1"
    typer.echo(f"CoreOne mock: http://harbor.localhost:{port}/login  and  http://summit.localhost:{port}/login")
    uvicorn.run(create_app(seed=seed, faults_enabled=faults), host="127.0.0.1", port=port, log_level="warning")


# --------------------------------------------------------------------------- replay


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise typer.BadParameter(f"--input expects name=value, got {pair!r}")
        name, _, value = pair.partition("=")
        inputs[name.strip()] = value
    return inputs


def _inject_faults(base_url: str, faults: list[str]) -> None:
    """--fault name[@page][:times] against a locally running mock (demo convenience)."""
    from urllib.parse import urlsplit

    import httpx

    parts = urlsplit(base_url)
    for spec in faults:
        name, _, rest = spec.partition("@")
        page, _, times = rest.partition(":")
        ms = 0
        if name.startswith("latency=") or name.startswith("confirm_timeout="):
            name, _, ms_text = name.partition("=")
            ms = int(ms_text)
        body = {"fault": name, "page": page or None, "times": int(times or 1), "ms": ms}
        response = httpx.post(f"http://127.0.0.1:{parts.port}/__faults", json=body, headers={"host": parts.netloc})
        response.raise_for_status()
        console.print(f"[dim]injected fault {body}[/dim]")


def _print_result(result: Any, workspace_root: Path) -> None:
    style = {"succeeded": "green", "business_outcome": "cyan", "preview": "cyan", "rejected": "yellow",
             "failed": "red", "needs_intervention": "magenta"}.get(result.status, "white")
    console.print(f"[bold {style}]{result.status}[/]  {result.capability}@{result.version}  tenant={result.tenant}"
                  f"  {result.duration_ms} ms")
    if result.outputs:
        console.print("outputs:", escape(json.dumps(result.outputs)))
    if result.preview:
        console.print("preview:", escape(json.dumps(result.preview.values)))
        console.print(f"commit token: {result.preview.commit_token}  (expires {result.preview.expires_at:%H:%M:%S})")
    if result.outcome:
        console.print(f"business outcome: [bold]{result.outcome.code}[/]  {result.outcome.message or ''}")
    if result.error:
        e = result.error
        console.print(f"error: [bold]{e.code}[/] ({e.category}{', retryable' if e.retryable else ''})"
                      f"  step={e.step_id}  {escape(e.message)}")
        if e.expected:
            console.print(f"  expected: {escape(e.expected)}")
        if e.observed:
            console.print(f"  observed: {escape(json.dumps(e.observed))}")
        if e.hint:
            console.print(f"  hint: {escape(e.hint)}")
    for r in result.recoveries:
        console.print(f"recovered: {r.code} at {r.step_id}")
    for d in result.drift:
        console.print(f"drift: {d.step_id}: {d.note}")
    for w in result.warnings:
        console.print(f"warning: {w}")
    if result.commit_state != "none":
        console.print(f"commit state: {result.commit_state}")
    console.print(f"[dim]evidence: {workspace_root / 'runs' / result.run_id}[/dim]")


@app.command()
def run(
    capability_id: str = typer.Argument(..., help="e.g. coreone.member.get_savings_balance"),
    tenant: str = typer.Option("harbor", "--tenant", "-t"),
    input_: list[str] = typer.Option([], "--input", "-i", help="name=value (repeatable)"),
    preview: bool = typer.Option(False, help="Stop before the first irreversible step and return a commit token."),
    commit_token: str | None = typer.Option(None, help="Commit with a token from a preview."),
    idempotency_key: str | None = typer.Option(None, help="Required to commit an irreversible capability."),
    attended: bool = typer.Option(False, help="An operator is available: escalate instead of failing."),
    headed: bool = typer.Option(False, help="Show the browser window."),
    base_url: str | None = typer.Option(None, help="Override the tenant's base URL."),
    fault: list[str] = typer.Option([], help="Inject a mock fault first: name[@page][:times] (demo only)."),
    as_json: bool = typer.Option(False, "--json", help="Print the full result as JSON."),
) -> None:
    """Replay a capability deterministically (no model in the loop)."""
    from playwright.async_api import async_playwright

    from rote.registry.store import Workspace
    from rote.replay.engine import ReplayOptions, replay

    workspace = Workspace(base_url_overrides={tenant: base_url} if base_url else {})
    if fault:
        _inject_faults(workspace.tenant(tenant).base_url, fault)
    mode = "commit" if commit_token else ("preview" if preview else "run")
    options = ReplayOptions(mode=mode, attended=attended, headless=not headed, commit_token=commit_token,  # type: ignore[arg-type]
                            idempotency_key=idempotency_key)

    async def go() -> Any:
        async with async_playwright() as pw:
            return await replay(workspace, capability_id, tenant, _parse_inputs(input_), options, playwright=pw)

    result = asyncio.run(go())
    if as_json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _print_result(result, workspace.root)
    raise typer.Exit({"succeeded": 0, "business_outcome": 0, "preview": 0, "rejected": 2}.get(result.status, 1))


# ----------------------------------------------------------------- review, approve


@app.command()
def review(capability_id: str, tenant: str = typer.Option("harbor", "--tenant", "-t")) -> None:
    """Show what a capability does, needs and returns, its hash, and whether it is approved."""
    from rote.registry.approvals import find_approval, load_approvals
    from rote.registry.store import Workspace

    workspace = Workspace()
    capability = workspace.capability(capability_id)
    console.print(f"[bold]{capability.id}[/] v{capability.version}  ({capability.side_effects} side effects)")
    console.print(capability.summary)
    console.print(f"content hash: {capability.content_hash()}")
    console.print(f"contract hash: {capability.contract_hash()}")
    source = capability.provenance.source if capability.provenance else "unknown"
    console.print(f"provenance: {source}")

    contract = Table(title="Contract", show_lines=False)
    contract.add_column("kind")
    contract.add_column("name")
    contract.add_column("detail")
    for name, spec in capability.inputs.items():
        contract.add_row("input", name, f"{spec.type} {spec.pattern or ''} {'sensitive:' + spec.sensitive if spec.sensitive else ''}")
    for name, out in capability.outputs.items():
        contract.add_row("output", name, f"{out.type} ({out.cardinality}) {'sensitive:' + out.sensitive if out.sensitive else ''}")
    for code, outcome in capability.outcomes.items():
        contract.add_row("outcome", code, f"after {outcome.after_step or 'any step'}: {outcome.description or ''}")
    console.print(contract)

    steps = Table(title="Steps")
    for column in ("#", "id", "action", "effect", "locators (ranked)", "postcondition"):
        steps.add_column(column)
    from rote.surface.web.conditions import describe

    for i, step in enumerate(capability.steps, 1):
        target = getattr(step, "target", None)
        locators = " > ".join(loc.by for loc in target.locators) if target else "-"
        steps.add_row(str(i), step.id, step.action, step.effect, locators, describe(step.expect) if step.expect else "-")
    console.print(steps)

    approval = find_approval(load_approvals(workspace.root, capability.id), capability, tenant=tenant, overlay_hash=None)
    if approval:
        console.print(f"[green]approved[/] for {tenant} by {approval.reviewer} at {approval.approved_at:%Y-%m-%d %H:%M}Z")
    else:
        console.print(f"[yellow]draft[/]: no approval matches this content hash for {tenant}")


@app.command()
def approve(
    capability_id: str,
    reviewer: str = typer.Option(..., help="Who reviewed it."),
    tenant: str | None = typer.Option(None, help="Scope the approval to one tenant (default: all without overlays)."),
    note: str | None = typer.Option(None),
) -> None:
    """Approve the capability's exact content hash for unattended replay."""
    from rote.registry.approvals import record_approval
    from rote.registry.store import Workspace

    workspace = Workspace()
    capability = workspace.capability(capability_id)
    approval = record_approval(workspace.root, capability, reviewer=reviewer, tenant=tenant, note=note)
    console.print(f"approved {capability.id} v{capability.version} {approval.content_hash} by {reviewer}")


# --------------------------------------------------------------------------- evidence


@app.command()
def show(run_id: str) -> None:
    """Print a run's timeline from its (redacted) evidence."""
    from rote.registry.store import Workspace

    run_dir = Workspace().runs / run_id
    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        data = {k: v for k, v in event["data"].items() if v not in (None, "", [], {})}
        console.print(f"[dim]{event['ts'][11:23]}[/dim] [bold]{event['type']:<20}[/] {escape(json.dumps(data))}")
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    console.print(f"result: {result['status']}  error={(result.get('error') or {}).get('code')}"
                  f"  outcome={(result.get('outcome') or {}).get('code')}")


# ---------------------------------------------------------------------------- schema


def capability_json_schema() -> str:
    return json.dumps(Capability.model_json_schema(by_alias=True), indent=2, sort_keys=True) + "\n"


@app.command()
def schema(check: bool = typer.Option(False, "--check", help="Fail if the committed schema is stale.")) -> None:
    """Write the capability JSON Schema to src/rote/schema/capability.schema.json."""
    text = capability_json_schema()
    if check:
        if not SCHEMA_PATH.exists() or SCHEMA_PATH.read_text(encoding="utf-8") != text:
            typer.echo("capability.schema.json is stale; run `rote schema`", err=True)
            raise typer.Exit(1)
        typer.echo("capability.schema.json is up to date")
        return
    SCHEMA_PATH.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {SCHEMA_PATH}")
