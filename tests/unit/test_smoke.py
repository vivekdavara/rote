from typer.testing import CliRunner

from rote import __version__
from rote.cli import app


def test_cli_prints_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
