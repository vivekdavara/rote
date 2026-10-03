# rote

An LLM figures out a workflow in a legacy back-office app once. After that, the workflow runs by rote.

`rote` is my take-home for interface.ai's computer-use brief. It drives an application that has no API,
the way an operator would. The first time, a model completes the goal. That run is compiled into a typed,
versioned, reviewable **capability**. In production, the capability is **replayed deterministically with no
model in the loop**, and its result keeps business outcomes, recoveries and hard failures apart. When a run
can't safely continue, it hands the **same live session** to a human and takes it back afterwards.

The design and its trade-offs are in **[REPORT.md](REPORT.md)**. What the system does under every condition is in
**[evidence/](evidence/README.md)**.

## What's here

| Brief requirement | Where |
|---|---|
| 3.1 Goal-driven agent loop | `rote discover`: `src/rote/discovery/agent.py` (observe, decide, gate, record, act) |
| 3.2 Structured artifact | `src/rote/schema/capability.py`, JSON Schema in `src/rote/schema/capability.schema.json` |
| 3.3 Deterministic replay | `rote run`: `src/rote/replay/engine.py`, taxonomy in `src/rote/schema/codes.py` |
| 3.4 Safety and policy | `src/rote/policy/gate.py`, `src/rote/redaction/`, `policies/*.yaml`, commit protocol in `src/rote/replay/commit.py` |
| 3.5 Evidence | `runs/<id>/events.jsonl`, masked screenshots, failure snapshots; curated in `evidence/` |
| 3.6 Human handoff | `src/rote/control/` (lease, console, input relay), resync in the engine |
| 3.7 Heterogeneity and scale | `src/rote/surface/web/` (the web adapter), `tenants/`, `overlays/`; other surfaces are designed in REPORT.md |
| Stretch: cross-tenant reuse | `overlays/summit/`, `tenants/summit.yaml`, `tests/integration/test_tenants.py` |
| Stretch: agent-facing catalog | `rote mcp serve`: `src/rote/mcp/server.py` |

The proxy target is **CoreOne**, a mock credit-union servicing app in `mockapp/`, built to be hostile to
automation: nested framesets, table layouts, labels in the next cell instead of `<label>`, control names that
change on every start, `__doPostBack` links, and a fault-injection API. All data is synthetic.

## Setup

Python 3.11 and Chromium (installed by Playwright).

```bash
make setup     # venv, package + dev extras, Chromium, and .env from .env.example
make test      # 132 tests: unit + integration against the mock, no API key needed
make demo      # regenerates evidence/ end to end, no API key needed (2–3 minutes; OCR-checks screenshots on macOS)
```

`.env` holds the mock's training login (`COREONE_USERNAME`, `COREONE_PASSWORD`). Only live discovery needs
`ANTHROPIC_API_KEY`: put it in `.env` or export it (an exported variable wins). Everything else (replay, handoff,
the demo, CI) runs without a key, and discovery itself can be replayed offline from a recorded cassette.

## Demo path

Start the mock (tenants `harbor.localhost:8400` and `summit.localhost:8400`) in one terminal:

```bash
.venv/bin/rote app
```

In another terminal, activate the venv (`source .venv/bin/activate`) and check the setup:

```bash
rote doctor            # add --probe to make a one-token call with your API key
```

**1. Discover the capability.** Live, with your key:

```bash
rote discover specs/get_savings_balance.yaml --live --headed
```

Or offline, replaying the decisions the live model made in `evidence/01-discovery-live/`:

```bash
rote discover specs/get_savings_balance.yaml --cassette tests/fixtures/cassettes/get_savings_balance.live.json
```

Discovery compiles the run into `capabilities/coreone/member.get_savings_balance.yaml`, replays it on the spec's
second example, and learns the business outcomes (MEMBER_NOT_FOUND, ACCESS_RESTRICTED) from negative examples.

**2. Review and approve it.** Unattended replay requires an approval bound to the exact content hash.

```bash
rote review coreone.member.get_savings_balance
rote approve coreone.member.get_savings_balance --reviewer "Your Name"
```

**3. Replay it.** No model is involved.

```bash
rote run coreone.member.get_savings_balance -i member_id=100234          # succeeded, typed money output
rote run coreone.member.get_savings_balance -i member_id=999999          # business_outcome MEMBER_NOT_FOUND
rote run coreone.member.get_savings_balance -i member_id=12ab            # rejected INVALID_INPUT, UI untouched
rote run coreone.member.get_savings_balance -i member_id=100234 --fault interstitial@search    # recovered
rote run coreone.member.get_savings_balance -i member_id=100234 --fault app_error@results      # APP_ERROR
rote show <run-id>                                                       # the evidence timeline
```

**4. Hand the live session to a human.** With `--attended`, the run opens an operator console instead of
failing. Open the printed URL, take control, click **Supervisor Override** on the live view, and hand back.

```bash
rote run coreone.member.get_savings_balance -i member_id=100234 --attended --fault unknown_modal@search
```

**5. An irreversible capability.** It previews first, then commits only with the preview's token and an
idempotency key.

```bash
rote discover specs/open_sub_account.yaml --cassette tests/fixtures/cassettes/open_sub_account.scripted.json
rote approve coreone.member.open_sub_account --reviewer "Your Name"
rote run coreone.member.open_sub_account -i member_id=100517 -i "share_type=Money Market" -i nickname=Rainy \
    -i deposit=40.00 -i funding_suffix=S10 --preview          # prints the review values and a commit token
rote run coreone.member.open_sub_account -i member_id=100517 -i "share_type=Money Market" -i nickname=Rainy \
    -i deposit=40.00 -i funding_suffix=S10 --commit-token <token> --idempotency-key req-1
```

Running the commit again with the same key returns the recorded result (`idempotent replay`) and clicks nothing.
Changing any input after the preview gets `rejected COMMIT_TOKEN_INVALID`.

**6. Another institution on the same product.** Summit relabels screens and requires a Branch:
`tenants/summit.yaml` holds the labels and `overlays/summit/` holds the one structural patch.

```bash
rote approve coreone.member.get_savings_balance --tenant summit --reviewer "Your Name"
rote run coreone.member.get_savings_balance --tenant summit -i member_id=100234
```

**7. Serve capabilities to an agent over MCP.**

```bash
rote catalog                       # the tools an agent would see (approved capabilities only)
rote mcp serve --tenant harbor     # stdio MCP server; point Claude Code or any MCP client at it
```

The server's workspace is its working directory (or `$ROTE_HOME`), and it needs the mock running. For Claude Code,
add this to `.mcp.json` in the repo:

```json
{"mcpServers": {"rote": {"command": ".venv/bin/rote", "args": ["mcp", "serve", "--tenant", "harbor"]}}}
```

## Layout

```
src/rote/schema/       the artifact, conditions, locators, results, config, overlays (Pydantic v2)
src/rote/surface/web/  perception and action on the web: frame-aware indexer, resolver, conditions, masking
src/rote/discovery/    planners (live model and cassette), recorder, compiler, the discovery loop
src/rote/replay/       the deterministic engine, commit tokens, idempotency ledger
src/rote/control/      control lease, operator console with input relay
src/rote/policy/       the policy gate and network allowlist
src/rote/redaction/    pseudonymization, detectors, artifact lint
src/rote/mcp/          the capability catalog over MCP
mockapp/               CoreOne, the hostile legacy mock (FastAPI + Jinja2)
apps/ tenants/ policies/ overlays/ specs/    product profile, tenants, policies, overlays, goal specs
spikes/live_loop.py    the bare loop used to settle the observation format
tests/                 unit, integration, fault matrix, negative controls, handoff, MCP, tenants
evidence/              generated by `rote demo`, plus the live discovery run
```
