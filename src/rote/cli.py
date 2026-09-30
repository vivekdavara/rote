"""Command-line entry point. Subcommands are added as each milestone lands."""

import json
from pathlib import Path

import typer

from rote import __version__
from rote.schema.capability import Capability

SCHEMA_PATH = Path(__file__).parent / "schema" / "capability.schema.json"

app = typer.Typer(
    help="rote: record a legacy-app workflow once with an LLM, then replay it deterministically.",
    no_args_is_help=True,
)


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
