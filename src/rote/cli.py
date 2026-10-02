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
    if result.outputs and not result.preview:
        console.print("outputs:", escape(json.dumps(result.outputs)))
    if result.preview:
        console.print("review values:", escape(json.dumps(result.preview.values)))
        console.print(f"expires {result.preview.expires_at:%H:%M:%S}. To commit exactly these values, pass "
                      "--commit-token and an --idempotency-key:")
        console.print(result.preview.commit_token, soft_wrap=True, markup=False)  # never wrapped: it gets copied
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
    console_port: int = typer.Option(8765, help="Operator console port (with --attended)."),
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
        from rote.control.plane import ControlPlane

        plane = ControlPlane(port=console_port) if attended else None
        async with async_playwright() as pw:
            if plane is not None:
                await plane.start()
                console.print(f"[dim]operator console (opens when needed): {plane.url}[/dim]")
            try:
                return await replay(workspace, capability_id, tenant, _parse_inputs(input_), options, playwright=pw,
                                    escalator=plane)
            finally:
                if plane is not None:
                    await plane.stop()

    result = asyncio.run(go())
    if as_json:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _print_result(result, workspace.root)
    raise typer.Exit({"succeeded": 0, "business_outcome": 0, "preview": 0, "rejected": 2}.get(result.status, 1))


# ------------------------------------------------------------------------- discovery


@app.command()
def discover(
    spec_path: Path = typer.Argument(..., help="Goal spec, e.g. specs/get_savings_balance.yaml"),
    live: bool = typer.Option(False, "--live", help="Drive the live model (needs ANTHROPIC_API_KEY)."),
    cassette: Path | None = typer.Option(None, help="Replay recorded decisions instead of calling a model."),
    attended: bool = typer.Option(False, help="An operator is available for approvals and handoffs."),
    headed: bool = typer.Option(False, help="Show the browser window."),
    effort: str = typer.Option("high", help="Model effort: low | medium | high | xhigh | max."),
    base_url: str | None = typer.Option(None, help="Override the tenant's base URL."),
    max_steps: int | None = typer.Option(None, help="Step budget (default: the tenant policy's)."),
    console_port: int = typer.Option(8765, help="Operator console port (with --attended)."),
) -> None:
    """Let a model complete the goal once, then compile, verify and save the capability."""
    from playwright.async_api import async_playwright

    from rote.discovery.agent import DiscoveryAgent, DiscoveryOptions
    from rote.discovery.cassette import load_cassette
    from rote.discovery.planner import AnthropicPlanner, CassettePlanner, Planner
    from rote.registry.store import Workspace
    from rote.schema.spec import load_spec

    if live == (cassette is not None):
        raise typer.BadParameter("choose exactly one of --live or --cassette PATH")
    spec = load_spec(spec_path)
    workspace = Workspace(base_url_overrides={spec.tenant: base_url} if base_url else {})
    planner: Planner = (CassettePlanner(load_cassette(cassette), spec.example(0)) if cassette
                        else AnthropicPlanner(effort=effort))
    options = DiscoveryOptions(attended=attended, headless=not headed, max_steps=max_steps)

    async def go() -> Any:
        from rote.control.plane import ControlPlane

        plane = ControlPlane(port=console_port) if attended else None
        async with async_playwright() as pw:
            if plane is not None:
                await plane.start()
                console.print(f"[dim]operator console (opens when needed): {plane.url}[/dim]")
            try:
                return await DiscoveryAgent(workspace, spec, planner, options, playwright=pw, escalator=plane).run()
            finally:
                if plane is not None:
                    await plane.stop()

    result = asyncio.run(go())
    style = "green" if result.status == "compiled" else "red"
    console.print(f"[bold {style}]{result.status}[/]  {spec.capability_id}  steps={result.steps_taken}  "
                  f"planner={planner.name}")
    if result.reason:
        console.print(f"reason: {escape(result.reason)}")
    if result.usage.get("calls"):
        u = result.usage
        console.print(f"model calls={u['calls']} input={u['input']} cache_read={u['cache_read']} output={u['output']}")
    if result.capability_path:
        console.print(f"artifact: {result.capability_path}")
    if result.verification:
        v = result.verification
        console.print(f"verify-replay (second example): {v.status} "
                      f"{escape(json.dumps(v.outputs)) if v.outputs else (v.error.code if v.error else '')}")
    for outcome in result.outcomes:
        detail = f"after {outcome.after_step}: {outcome.detector!r}" if outcome.status == "learned" else outcome.detail
        console.print(f"outcome {outcome.code}: {outcome.status} ({escape(detail)})")
    for note in result.notes:
        console.print(f"[dim]note: {escape(note)}[/dim]")
    console.print(f"[dim]evidence: {workspace.runs / result.run_id}  cassette: {result.cassette_path}[/dim]")
    if result.status == "compiled":
        console.print(f"next: rote review {spec.capability_id}   then   rote approve {spec.capability_id} --reviewer NAME")
    raise typer.Exit(0 if result.status == "compiled" else 1)


@app.command()
def doctor(probe: bool = typer.Option(False, help="Make a one-token API call to check model credentials.")) -> None:
    """Check the local setup: browser, workspace, mock app, secrets, model credentials."""
    import os
    import sys

    import httpx

    from rote.registry.store import Workspace
    from rote.secrets import resolve_secrets

    workspace = Workspace()
    checks: list[tuple[str, bool, str]] = []
    checks.append(("python", sys.version_info >= (3, 11), sys.version.split()[0]))
    for folder in ("apps", "tenants", "policies", "specs"):
        checks.append((f"workspace/{folder}", (workspace.root / folder).is_dir(), str(workspace.root / folder)))
    for tenant_file in sorted((workspace.root / "tenants").glob("*.yaml")):
        tenant = workspace.tenant(tenant_file.stem)
        try:
            status = httpx.get(f"{tenant.base_url}/login", timeout=3).status_code
            checks.append((f"tenant {tenant.id}", status == 200, f"{tenant.base_url} -> HTTP {status}"))
        except httpx.HTTPError as exc:
            checks.append((f"tenant {tenant.id}", False, f"{tenant.base_url} unreachable ({type(exc).__name__}); "
                           "start it with `rote app`"))
        _, missing = resolve_secrets(tenant, workspace.root)
        checks.append((f"secrets {tenant.id}", not missing, "ok" if not missing else ", ".join(missing)))
    key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    token = bool(os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    base = os.environ.get("ANTHROPIC_BASE_URL")
    checks.append(("model credentials", key or token,
                   "ANTHROPIC_API_KEY set" if key else ("ANTHROPIC_AUTH_TOKEN set" if token else
                                                        "none (only live discovery needs them)")))
    checks.append(("model endpoint", True, base or "default (api.anthropic.com)"))
    if probe:
        try:
            import anthropic

            from rote.discovery.prompts import MODEL

            reply = anthropic.Anthropic().messages.create(model=MODEL, max_tokens=1,
                                                          messages=[{"role": "user", "content": "ping"}])
            checks.append(("model probe", True, f"served by {reply.model}"))
        except Exception as exc:  # noqa: BLE001 - doctor reports whatever went wrong
            checks.append(("model probe", False, f"{type(exc).__name__}: {str(exc)[:160]}"))
    for name, ok, detail in checks:
        console.print(f"{'[green]ok[/]  ' if ok else '[red]FAIL[/]'} {name:<22} {escape(detail)}")


# ------------------------------------------------------------------------------ demo


@app.command()
def demo(evidence: Path = typer.Option(Path("evidence"), help="Where to write the evidence.")) -> None:
    """Regenerate /evidence/ end to end with no API key (never touches 01-discovery-live/)."""
    from rote.demo import run_demo

    raise typer.Exit(run_demo(evidence.resolve()))


# ------------------------------------------------------------------------------- MCP

mcp_app = typer.Typer(help="Serve approved capabilities to agents over MCP.", no_args_is_help=True)
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve(tenant: str = typer.Option("harbor", "--tenant", "-t", help="One server per tenant.")) -> None:
    """Serve this tenant's approved capabilities as MCP tools over stdio."""
    from rote.mcp.server import serve
    from rote.registry.store import Workspace

    asyncio.run(serve(Workspace(), tenant))


@app.command()
def catalog(tenant: str = typer.Option("harbor", "--tenant", "-t")) -> None:
    """List the tools an agent would see for this tenant (approved capabilities only)."""
    from rote.mcp.server import Catalog
    from rote.registry.store import Workspace

    for tool in Catalog(Workspace(), tenant).tools():
        flags = "destructive" if tool.annotations and tool.annotations.destructive_hint else "read-only"
        console.print(f"[bold]{tool.name}[/] ({flags})  inputs: {', '.join(tool.input_schema['properties'])}")
        console.print(f"  {escape(tool.description or '')}")


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

    overlay = workspace.overlay(capability_id, tenant)
    if overlay is not None:
        console.print(f"{tenant} overlay v{overlay.version} {overlay.content_hash()}: {overlay.reason} "
                      f"({len(overlay.patches)} patch(es))")
    approval = find_approval(load_approvals(workspace.root, capability.id), capability, tenant=tenant,
                             overlay_hash=overlay.content_hash() if overlay else None)
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
    overlay = workspace.overlay(capability_id, tenant) if tenant else None
    if overlay is not None:
        from rote.schema.overlay import apply_overlay

        apply_overlay(capability, overlay)  # refuse to approve an overlay that doesn't fit
    approval = record_approval(workspace.root, capability, reviewer=reviewer, tenant=tenant, note=note,
                               overlay_hash=overlay.content_hash() if overlay else None)
    scope = f" with {tenant}'s overlay {approval.overlay_hash}" if overlay else (f" for {tenant}" if tenant else "")
    console.print(f"approved {capability.id} v{capability.version} {approval.content_hash}{scope} by {reviewer}")


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
