# rote

An LLM figures out a workflow in a legacy back-office app once. After that, the workflow runs by rote.

`rote` is a take-home for interface.ai's computer-use brief. It drives a UI that has no API, records the successful run as a typed, versioned **capability**, replays that capability deterministically with no model in the loop, and hands the live session to a human when it gets stuck.

> Work in progress. The full setup and demo path lands with the final milestone.

## Setup

```bash
make setup
```

This needs Python 3.11. It creates `.venv`, installs the package with dev extras, installs Chromium for Playwright, and copies `.env.example` to `.env`.

```bash
make test
```
