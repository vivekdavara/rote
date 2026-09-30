"""Replay is the production path, and it must never consult a model.

Checked two ways: the replay package's import graph is inspected in a fresh
interpreter, and an integration test runs a full replay with the ``anthropic``
module made unimportable.
"""

from __future__ import annotations

import subprocess
import sys


def test_replay_import_graph_has_no_model_client_or_discovery() -> None:
    probe = (
        "import sys, rote.replay.engine, rote.cli; "
        "bad = sorted(m for m in sys.modules if m == 'anthropic' or m.startswith('anthropic.') "
        "or m.startswith('rote.discovery')); print(','.join(bad))"
    )
    loaded = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout.strip()
    assert loaded == "", f"replay path imports: {loaded}"
