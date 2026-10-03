# Live discovery (a real model run)

A discovery run driven by the live model, committed exactly as recorded and never regenerated (`rote demo`
leaves this folder alone).

- **Command:** `rote discover specs/get_savings_balance.yaml --live` (headless), against the local mock, on
  2026-10-03.
- **Model:** `claude-opus-5-5` at effort `high`. All 6 calls were served by that model, with no fallback.
- **Cost:** 5 actions plus `finish`, in 78 s. Tokens: 14,000 uncached input, 14,750 read from cache, 2,950
  written to cache, 988 output.
- **Result:** compiled into a 5-step capability. Verify-replay on the spec's second example succeeded.
  MEMBER_NOT_FOUND and ACCESS_RESTRICTED were learned from the negative examples.

| Folder or file | What it is |
|---|---|
| `discover-20261003-101804-40bd2f/` | The run: `events.jsonl` (each decision with the model's rationale), `trace.json`, `cassette.json`, `discovery.json`, masked screenshots |
| `…-verify/` | The compiled capability replayed on the second example |
| `…-negative-N/`, `…-negative-N-confirm/` | Outcome learning: the negative example's run, then the confirming replay |
| `artifact.yaml` | The capability exactly as this run compiled it |

## What it exposed

In `artifact.yaml`, two of the clicks (`open_member_search` and `open_search`) have no postcondition. The
model proposed "Member Search" as the check for the first click, but that text was already on screen as the
navigation link, so it didn't discriminate. When that happens, the compiler falls back to text that appeared
with the action. It looked only for new headings, and these screens had none.

The compiler now also falls back to new field labels and column titles, never to values. Replaying this run's
decisions with the fixed compiler (the cassette is `tests/fixtures/cassettes/get_savings_balance.live.json`)
gives every action a postcondition:

| Step | Postcondition |
|---|---|
| `open_member_search` | "Member Number" (a field label; on Summit the tenant dictionary checks "Account Holder No.") |
| `enter_member_number` | the field holds `{{inputs.member_id}}` |
| `open_search` | "Member #" (a column title), plus the member number echoed |
| `open_view` | "Share Savings", plus the member number echoed |

CI asserts this on every push (`test_live_model_decisions_still_compile_to_a_working_capability`), against a
mock with different control names. `../02-discovery-balance/` is `rote demo` recompiling the same decisions.
