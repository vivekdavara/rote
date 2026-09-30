"""Command-line entry point. Subcommands are added as each milestone lands."""

import typer

from rote import __version__

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
