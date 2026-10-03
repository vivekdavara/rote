# Any Python 3.11 or newer; override with `make setup PY=/path/to/python3`.
PY ?= $(firstword $(shell command -v python3.13 python3.12 python3.11 2>/dev/null) python3)
VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup app test lint typecheck demo clean

setup:
	@$(PY) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "rote needs Python 3.11 or newer: make setup PY=...")'
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e ".[dev,mcp]"
	$(BIN)/playwright install chromium
	test -f .env || cp .env.example .env

app:
	$(BIN)/rote app

test:
	$(BIN)/pytest -q

lint:
	$(BIN)/ruff check src mockapp tests spikes

typecheck:
	$(BIN)/mypy src/rote/schema src/rote/replay

demo:
	$(BIN)/rote demo

clean:
	rm -rf runs .pytest_cache .mypy_cache .ruff_cache
