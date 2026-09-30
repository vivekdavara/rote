PY ?= python3.11
VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup app test lint typecheck demo clean

setup:
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
