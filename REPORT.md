# rote: design report

rote has two paths over one runtime. **Discovery** lets a model complete a goal once against the live UI and
compiles what happened into a capability. **Replay** runs that capability with no model, for a person on the CLI
or an agent over MCP.

## Architecture

```
goal spec ─► discovery: observe → planner (Claude) → policy gate → recorder → act      (repeat until finish)
          ─► compiler ─► verify-replay on a second input ─► outcomes learned from negative examples
          ─► draft capability (YAML) ─► review ─► approval bound to the content hash
caller or MCP tool ─► registry (base capability + tenant labels + overlay) ─► replay engine, no LLM ─► RunResult
shared runtime: fresh browser context · network allowlist · native-dialog handling · login from the product
profile · version fingerprint · redacted evidence · control lease and operator console
```

The target is CoreOne, a mock credit-union app built to be hostile to automation (framesets, labels in neighbouring
cells, control names re-randomized on every start), with fault injection that makes every failure reproducible.

Three decisions shape the rest:

- **The model acts on element refs, not coordinates.** An observation is a frame-aware, accessibility-first index
  (role, name, inferred label, table context) plus a screenshot. Because the model says "click ref 14", the
  recorder knows which element was used, and the policy gate sees `button "Confirm"` before anything is clicked.
- **Planner calls are stateless.** Each call is a cached prefix (tools, system prompt, goal), a history my code
  writes, and the current observation. The trace is the source of truth, context stays bounded, and recorded
  decisions (a cassette) replay discovery offline. The model is `claude-opus-5-5` with adaptive thinking, strict
  tools and one action per turn. The live run in `evidence/01-discovery-live/` took 6 calls and 78 s.
- **The model never logs in or navigates.** The product profile signs in with secrets resolved at run time, and
  there is no navigate tool.

One process per run holds the CLI, the library and the operator console (FastAPI on the same event loop). SQLite
holds only the idempotency ledger. The control lease is what would move into a session broker at scale.

## Artifact schema

A capability is one YAML file in two layers. The **contract** is what a caller, reviewer or MCP client needs:

- id and semver
- `side_effects` (none, reversible or irreversible)
- typed inputs, with patterns and sensitivity classes
- typed outputs, with cardinality
- `preview_outputs` for irreversible flows
- business outcomes and requirements

The **procedure** is how replay does it: steps with an intent, an action, a declared effect, ranked locators and a
postcondition.

```yaml
outcomes:
  MEMBER_NOT_FOUND:
    after_step: open_search                  # checked only where it can happen
    when: {frame: main, text_visible: No members found matching the search criteria.}
steps:
- id: open_view
  action: click
  effect: read_only
  target:
    frame: main
    locators:                                # ranked; matching a fallback is reported as drift
    - {by: table_cell, headers: ['Member #'], row: {'Member #': '{{inputs.member_id}}'},
       then: {role: link, name: View}}
    - {by: role, role: link, name: View}
    - {by: css, css: 'body > font > table:nth-of-type(2) > … > a', brittle: true}
  expect: {all: [{frame: main, text_visible: Member Detail},
                 {frame: main, text_visible: '{{inputs.member_id}}'}]}
```

- **Semantic locators first.** The ranking is role and name, then label (explicit, otherwise inferred from the
  adjacent cell), then table cell (headers plus a templated row predicate), then CSS, marked brittle. In a row
  identified by an input, as above, the table locator goes first so replay can't pick another member's row.
  Discovery keeps a candidate only if it resolves uniquely to the same element. Replay requires exactly one
  visible, enabled match.
- **Postconditions must discriminate.** The compiler keeps a condition only if it was false before the action and
  true after. It adds input echoes so a page for the wrong member can't pass. Beyond those echoes, conditions
  hold no data. The live run showed why the fallback matters: the model proposed "Member Search", already
  visible as the nav link, and two clicks compiled unchecked. The fallback now tries new headings, field labels,
  then column titles, and CI asserts that every live step is checked.
- **Business outcomes are contract.** "Member not found" is an answer, scoped to the step where it can appear.
  Discovery learns outcomes from the spec's negative examples. It takes the new message where the run diverged,
  rejects it if it appears on any happy-path screen, and confirms it with a replay.
- **Validation at load.** An artifact is refused if:
  - an output isn't extracted exactly once
  - `side_effects` disagrees with the steps
  - a preview output comes after an irreversible step
  - a template references `{{secrets.*}}` or an undeclared input
- **Identity and approval stay separate.** The content hash covers canonical JSON minus provenance. Approvals are
  separate files bound to that hash (plus the overlay hash for a tenant), so approving changes nothing and any
  edit voids the approval. A contract hash constrains overlays. Contract changes bump the major version;
  procedure changes bump the minor.
- **The product profile holds shared behaviour:** the login, the version fingerprint, the loading signal, and
  interrupts such as Security Notice → Acknowledge.

The exported JSON Schema (`src/rote/schema/capability.schema.json`) is snapshot-tested.

## Determinism & error handling

Replay is plain code. One test runs it with `anthropic` blocked from import, and an import-graph test asserts
that `rote.replay` never imports `rote.discovery`. Each step runs in this order:

1. check interrupts
2. resolve the target
3. hit-test it (is something covering the control?)
4. pass the policy gate
5. act
6. wait for the postcondition while polling detectors

Nothing sleeps. Waits are conditions with timeouts, and every in-page call is bounded at 2.5 s.

A run returns `succeeded` (with `recoveries` and `drift`), `business_outcome`, `failed` (with code, step,
expected versus observed, evidence and `retryable`), `rejected`, `preview`, or the interim `needs_intervention`.
Irreversible runs add `commit_state`.

| Category | Examples | Caller sees |
|---|---|---|
| Rejected | INVALID_INPUT, NOT_APPROVED, COMMIT_TOKEN_INVALID, OVERLAY_INVALID | `rejected`; UI never touched |
| Business outcome | MEMBER_NOT_FOUND, ACCESS_RESTRICTED, VALIDATION_REJECTED | `business_outcome` + code |
| Recoverable | KNOWN_INTERSTITIAL, SESSION_EXPIRED, SLOW_LOAD, TRANSIENT_APP_ERROR | `succeeded` + `recoveries` |
| Needs a human | UNKNOWN_MODAL, UNKNOWN_DIALOG, INDETERMINATE_COMMIT | handoff if attended, otherwise `failed` |
| Hard failure | TARGET_NOT_FOUND, UNEXPECTED_STATE, LOAD_TIMEOUT, APP_ERROR, PERMISSION_DENIED, PREVIEW_MISMATCH, AMBIGUOUS_EXTRACTION | `failed` + evidence |

Three distinctions matter:

- **Loading versus wrong page.** A timeout while the app is still loading is LOAD_TIMEOUT, which is retryable. A
  timeout after the page settled somewhere unexpected is UNEXPECTED_STATE.
- **Data versus configuration.** ACCESS_RESTRICTED is a fact about the member. PERMISSION_DENIED means our
  service account is misconfigured, which is an administrator's problem.
- **Bounded recovery.** At most three recoveries per step and one restart. A run restarts from the top only while
  nothing irreversible has happened.

Irreversible capabilities use **preview, then commit**, because an LLM caller would always pass `confirm: true`.

1. Preview runs to the review screen and returns its values with an HMAC token. The token binds the content and
   overlay hashes, the tenant, keyed digests of the inputs and review values, and an expiry.
2. Commit needs the token and an idempotency key. It claims the key in the ledger and re-reads the review screen;
   any change fails with PREVIEW_MISMATCH. Then it clicks Confirm and checks the confirmation.
3. A repeated key returns the recorded result.
4. If Confirm was clicked but no confirmation arrives, the run reports INDETERMINATE_COMMIT with `commit_state:
   unknown`, and it is never retried.

Evidence: 16 scenarios, a fault matrix, negative controls (mutated artifacts must fail with the right code), and
20 of 20 identical replays (p50 825 ms, p95 1414 ms). Control names change with the seed; no artifact depends on
them.

## Heterogeneity & multi-tenant

**Other surfaces.** Roles, labels, tables and visible text also exist in Windows UI Automation, macOS AX and the
Java Access Bridge, so the artifact carries over. A surface must observe, resolve a target to exactly one element,
act, evaluate a condition, hit-test, and take a masked screenshot. These aren't yet a formal interface: the engine
reaches Playwright through `Runtime` and `surface/web`, and extracting one is the first step toward a second
adapter.

- **Desktop (UIA/AX):** ControlType maps to role, Name and LabeledBy to label, and Grid and Table patterns to
  `table_cell`. The Invoke and Value patterns carry the actions. Native dialogs become ordinary windows.
- **Terminals (3270/5250), through an emulator API:** the screen is a field buffer. A locator is "the unprotected
  field after label X" or a row and column. A condition is text at a position. Actions are typing plus AID keys
  such as PF3. This is the most deterministic surface.
- **Citrix/VDI pixels:** there is no tree. Discovery uses Claude's computer-use tools; replay uses OCR text anchors
  and template matching. Determinism is weaker, so postconditions get stricter and escalation more frequent.

**Many tenants on one product.** A base capability covers a product version range. Tenant differences live
outside it:

1. A tenant label dictionary ("Member Search" → "Find Member"), applied to locators and postconditions alike.
2. Structural overlays keyed by step id: insert, replace or remove a step, or replace a target. Overlays that
   would change the contract hash are refused. Approvals bind the base hash plus the overlay hash.

Summit (v4.3.1, relabeled, requires a Branch) runs the capability discovered on Harbor, using its labels plus one
inserted step. Without them, replay reaches the relabeled link through its brittle CSS locator and reports drift.
It then stops at the real difference: "Member Number" isn't on screen (UNEXPECTED_STATE at `open_member_search`).

At fleet scale, each run flags a product version outside the artifact's range as drift, and canary replays run
per tenant on every vendor release. Drift is triaged by how many tenants it hits: if every v4.3 tenant breaks, fix
the base; if one breaks, write an overlay. A repair mode would let the model propose that overlay for human
review.

## Escalation & handoff

**Triggers.** Discovery escalates on `request_human`, an exhausted budget, no progress (an unchanged or
oscillating screen), three failed actions in a row, and any irreversible action, which needs an operator's
approval. Attended replay escalates on UNKNOWN_MODAL, UNKNOWN_DIALOG and INDETERMINATE_COMMIT; unattended, those
are hard failures.

**The lease.** Control moves AGENT_ACTIVE → AWAITING_OPERATOR → HUMAN_ACTIVE → RESYNCING → AGENT_ACTIVE, with
ABORTED and ESCALATION_TIMEOUT as exits. Each transition bumps an epoch and is logged with actor and reason.
Automation acts only in AGENT_ACTIVE, and step timers pause while a human holds control.

**The console**, a token URL served by the run, shows the intervention, a masked live view of the same session,
and any pending native dialog. The operator can take control, hand back (with a note, attestations and an
optional resume step), approve, reject or abort.

**The relay is the only way a human can act.** In a headed browser nothing can stop physical input while
automation drives, so the lease would be fiction. Through the relay it is real, and every human action is logged
with hit-test facts such as `button "Supervisor Override"`.

**Resync on hand-back:**

1. A terminal screen (a business outcome or an error page) wins.
2. A resume step the operator names is honoured if the step before it held.
3. If the current step's postcondition already holds, the operator did it, so advance.
4. Otherwise retry the step, but only if it is reversible.

An irreversible step is skipped only if the operator attests to it, and it is then recorded as
`performed_by_human`. A third escalation at the same step ends the run. In discovery, relayed actions become steps
marked `provenance: human`.

**Evidence and limits.** The handoff in `evidence/11-` is a scripted operator driving the console's own HTTP API,
and it is labelled as such. On Linux, Chromium reports native dialogs late while blocking in-page reads. Bounded
calls, a short wait and escalation as UNKNOWN_DIALOG keep it correct, but that CI test takes about 40 s.

## Safety

**Policy** is per tenant and enforced in code, in layers:

1. A network route aborts requests to any origin outside the allowlist.
2. When an artifact loads, its routes and action types are checked (no upload, download or script).
3. At run time a gate classifies risk from the element itself. A button named Confirm is irreversible whatever
   the artifact claims, and a mismatch is blocked.
4. Invocation needs an approval bound to the hash. Irreversible capabilities also need a commit token and an
   idempotency key.

The model never handles credentials, and capabilities can't reference `{{secrets.*}}`.

**Data handling:**

- Sensitive inputs appear in artifacts only as `{{inputs.*}}`. Logs get keyed-HMAC pseudonyms, because a plain
  hash of a six-digit member number is easy to brute-force.
- Saved results redact sensitive outputs. Detectors cover SSNs, Luhn-valid cards, account numbers, email, phone,
  DOB, and bearer and JWT tokens. Password fields are never logged.
- Screenshots are masked before they touch disk. Cassettes store decisions and element facts, never screen text.
  The compiler refuses data-like literals.

A PII canary scans everything the demo writes for 15 synthetic member values. It caught a real leak in
development, a balance in a discovery summary. It now reports no hits across 290 files.

**Prompt injection** is contained by code, not by trusting the model. The system prompt tells the model screen
content is data, not instructions, but nothing depends on that. One seeded member's notes hold an injected
instruction and link. A clearly labelled adversarial script obeys it, the allowlist blocks the request to
`evil.localhost`, and no artifact is produced.

**Limits:**

- Regex misses free-text PII.
- The risk lexicon can misclassify an unusual button.
- Discovery shows the model the screen, so it belongs in sandboxes with synthetic data (replay never involves a
  model).
- The console token isn't real authentication.
- The allowlist is per route, not per record.

## Cuts

- **Live discovery of the irreversible capability.** Its Confirm needs an operator's approval, so it hasn't been
  recorded live. Its cassette is a labelled, hand-written fixture.
- **A second surface adapter.** The mock already forces the hard parts (frames, inferred labels, randomized
  names). A shallow desktop stub would prove less than a careful design.
- **Scaling infrastructure** (a session broker, a worker pool, a shared ledger). The brief says not to build it.
- **Co-browsing, operator SSO, queues and SLAs.** A 1 Hz view plus the relay is enough to prove the lease.
- **Overlay repair mode, pagination, file transfer:** not needed to show the core contracts.

Next steps, in order:

1. Record an attended live discovery of `open_sub_account`, so CI replays real decisions for both specs.
2. Extract the surface interface and build a UI Automation adapter against a WinForms test app.
3. Add per-tenant canaries, drift triage and overlay repair proposals.
4. Build a session broker: leases outside the run process, co-browsing, operator SSO, queues.
5. Move to a shared ledger (Postgres) so idempotency holds across workers.
6. Build the terminal adapter, and the pixel adapter last.
