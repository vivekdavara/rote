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
