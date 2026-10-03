# rote: design report

## Architecture

```
goal spec ─► discovery (observe → Claude → policy gate → recorder → act) ─► compile ─► verify ─► review ─► approve
caller/MCP ─► registry (base + tenant labels + overlay) ─► replay engine, no LLM ─► RunResult
```

**Discovery** has a model complete a goal once and compiles the run into a typed capability; **replay** runs it
with no model, for a person or an agent over MCP. The target, CoreOne, is a mock credit-union app built to resist
automation, with fault injection that makes
failures reproducible. The model points at element refs from a frame-aware accessibility index plus a
screenshot, not at pixels. The
recorder therefore knows which element was used, and the policy gate sees `button "Confirm"` before the click.
Planner calls are stateless: a cached prefix, a history my code writes, and the current screen. Recorded
decisions (a cassette) replay discovery offline. The live run in `evidence/01-discovery-live/` took 6 calls and
78 s. The model never logs in or navigates; the product profile signs in with secrets resolved at run time.

## Artifact schema

A capability is one YAML file. Its **contract** is what a caller needs: id, semver, `side_effects`, typed inputs
(patterns, sensitivity), typed outputs (cardinality), `preview_outputs` and business outcomes. Its **procedure**
is the steps, each with an intent, an action, a declared effect, ranked locators and a postcondition:

```yaml
- id: open_view
  locators: [{by: table_cell, headers: ['Member #'], row: {'Member #': '{{inputs.member_id}}'}, then: {role: link, name: View}},
             {by: role, role: link, name: View}, {by: css, css: '… > td:nth-of-type(5) > a', brittle: true}]
  expect: {all: [{text_visible: Share Savings}, {text_visible: '{{inputs.member_id}}'}]}
```

- **Locators are semantic first:** role, label (inferred from the adjacent cell when there's no `<label>`), table
  cell, then brittle CSS. A fallback match is reported as drift. Discovery keeps a candidate only if it resolves
  uniquely to the element acted on.
- **Postconditions must discriminate** (false before the action, true after) and echo the inputs, so a page for
  the wrong member fails. If the model's proposed check doesn't discriminate, the compiler uses the first new
  heading, field label or column title, never a value. The live run exposed this gap.
- **Business outcomes are contract,** scoped to the step where they appear and learned from negative examples.
- **Approval is separate from identity.** Approvals bind the content hash (plus a tenant's overlay hash), so any
  edit voids them. Re-discovery bumps the major version for a contract change and the minor for a procedure
  change; an identical result keeps its version and approval.

## Determinism & error handling

Each step checks interrupts, resolves its target, hit-tests it (is the control covered?), passes the policy gate,
acts, and waits for its postcondition. Nothing sleeps, every in-page call is bounded, and a test runs replay with
`anthropic` blocked from import.

| Status | Examples | Meaning |
|---|---|---|
| `rejected` | INVALID_INPUT, NOT_APPROVED, COMMIT_TOKEN_INVALID | refused; UI never touched |
| `business_outcome` | MEMBER_NOT_FOUND, ACCESS_RESTRICTED | an answer the caller handles |
| `succeeded` + `recoveries` | KNOWN_INTERSTITIAL, SESSION_EXPIRED, SLOW_LOAD | handled inside the run |
| handoff, or `failed` | UNKNOWN_MODAL, UNKNOWN_DIALOG, INDETERMINATE_COMMIT | a human if one is attending |
| `failed` | TARGET_NOT_FOUND, UNEXPECTED_STATE, LOAD_TIMEOUT, APP_ERROR, PERMISSION_DENIED | step, expected vs observed, evidence |

A timeout while the app is still loading is LOAD_TIMEOUT (retryable); one after it settled elsewhere is
UNEXPECTED_STATE. ACCESS_RESTRICTED is a fact about the member; PERMISSION_DENIED is our misconfigured service
account. A run may restart from the top only before anything irreversible.

Irreversible capabilities use **preview, then commit**, because an LLM caller would always pass `confirm: true`.
Preview returns the review screen's values and an HMAC token bound to the artifact, tenant, inputs and those
values. Commit needs the token and an idempotency key. It re-reads the review screen (PREVIEW_MISMATCH if it
changed) before clicking Confirm. A repeated key returns the recorded result, and an unconfirmed click is
INDETERMINATE_COMMIT, never retried. The evidence covers 16 scenarios and 20 of 20 identical replays.

## Heterogeneity & multi-tenant

Roles, labels, tables and visible text also exist in Windows UI Automation and macOS AX, so artifacts carry over.
A surface must observe, resolve, act, evaluate conditions, hit-test and mask screenshots; that interface is still
implicit in `Runtime`. On the desktop, ControlType maps to role and Grid patterns to `table_cell`. On a
3270/5250 terminal, a locator is "the unprotected field after label X", the most deterministic surface.
Citrix/VDI pixels have no tree: discovery uses Claude's computer-use tools and replay uses OCR anchors,
with stricter checks and more escalation.

For many tenants on one product, a base capability covers a version range. A tenant label dictionary ("Member
Search" → "Find Member") applies to locators and postconditions alike. Overlays keyed by step id insert, replace
or remove steps, but may not change the contract hash. Summit (relabeled, needs a Branch) runs the
Harbor-discovered capability through its labels plus one inserted step; without them, replay reports drift and
stops at the first real difference. At fleet scale, canary replays run per tenant on each vendor release, and
drift is triaged by blast radius: every v4.3 tenant breaks → fix the base; one breaks → write an overlay.

## Escalation & handoff

Discovery escalates on `request_human`, no progress, three failed actions in a row, an unknown dialog, or an
irreversible action, which needs an operator's approval; an exhausted step or time budget stops it. Attended
replay escalates on UNKNOWN_MODAL, UNKNOWN_DIALOG, INDETERMINATE_COMMIT, UNEXPECTED_STATE and TARGET_NOT_FOUND;
unattended, those fail. Control is a lease: AGENT_ACTIVE →
AWAITING_OPERATOR → HUMAN_ACTIVE → RESYNCING → AGENT_ACTIVE. The console shows which capability is stuck (id,
version, tenant, summary), the step and its intent, why it stopped, and a live view of the same session. That
view is unmasked because the operator acts on it, and it is never saved. Humans act only through the console's
input relay, so the lease is enforced, and each action is logged with what it hit (`button "Supervisor
Override"`). On hand-back, a postcondition that already holds means the operator did the step. Otherwise a
reversible step is retried, and an irreversible one is skipped only on the operator's attestation. The evidence
has it twice: a scripted operator on the console's API, and a person in the console UI, screen-recorded.

## Safety

Policy is per tenant and enforced in code: a network allowlist on every request, route and action checks when an
artifact loads, a runtime gate that classifies risk from the element itself (Confirm is irreversible whatever the
artifact claims), and approvals bound to the hash. The model never sees credentials. Inputs appear in artifacts
only as templates, and in logs as keyed-HMAC pseudonyms. Screen values the profile marks sensitive (Name, SSN,
Balance, Available…) are registered as soon as a screen is observed, so logs pseudonymize them and screenshots mask
them wherever they appear. `rote demo` then searches for every seeded member value, in text and, with OCR, in every
screenshot. Widening that check found two leaks, now fixed; the live run was redacted after the fact (see its
README). A labelled adversarial script that obeys an injected instruction is stopped by the allowlist. Limits:
regex misses free-text PII, discovery shows the model the screen (so synthetic data only), and the console token
isn't real authentication.

## Cuts

Cut: live discovery of the irreversible capability (it needs an attending operator; its cassette is a labelled
fixture), a second surface adapter (a stub would prove little), and scaling, co-browsing and operator SSO. Next:
attended live discovery of `open_sub_account`, a UIA adapter behind an explicit surface interface, per-tenant
canaries, a session broker, and a shared idempotency ledger.
