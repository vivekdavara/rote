# Handoff done by a person

A handoff through the operator console UI, done by a person (Vivek, the author) rather than a script, and
committed as recorded. `rote demo` leaves this folder alone.

- **Command:** `rote run coreone.member.get_savings_balance -i member_id=<the spec's first example> --attended --fault unknown_modal@search`,
  on 2026-10-03 against the local mock. The capability is [artifact.yaml](artifact.yaml): version 0.2.0,
  recompiled from the live run's decisions. Its content hash matches the run's `result.json`, and apart from
  version and provenance it is identical to `../02-discovery-balance/artifact.yaml`. Version 0.2.0 had no
  approval yet; an attended run may trial an unapproved read-only capability (current code records that as a
  warning; irreversible capabilities always need an approval).
- **What happened:** an unrecognized Fraud Alert covered the member-number field. The run stopped and opened an
  intervention naming the capability, the step (`enter_member_number`) and the reason. The person then took
  control in the console, clicked **Supervisor Override** on the live view, and handed back. The engine resynced
  (the step's target was reachable again, so it retried the step), and the run succeeded.

| Time (UTC) | Event |
|---|---|
| 16:21:14 | `escalation_requested` UNKNOWN_MODAL at `enter_member_number`; lease AGENT_ACTIVE → AWAITING_OPERATOR |
| 16:25:25 | lease → HUMAN_ACTIVE, actor "Vivek" |
| 16:25:37 | `human_action` click on `button "Supervisor Override"` in frame `main`, relayed by the console |
| 16:26:19 | lease → RESYNCING (note: "Applied Supervisor Override"), then `resync` and lease → AGENT_ACTIVE |
| 16:26:19 | `run_finished` succeeded |

**[recording.mp4](recording.mp4)** is a 70-second screen recording of it: the console naming the stuck
capability, step and reason, the person taking control and clicking through the live view, and the hand-back. The
console's live view is unmasked by design, but the run finished within a second of the hand-back, so no member data
appears. OCR of a frame every 2 seconds finds no seeded member value.

The run folder holds `events.jsonl` (the full timeline), `result.json` (redacted), the intervention record with
its masked screenshot (`interventions/`), and the masked step screenshots. The scripted version of the same
handoff is in `../11-handoff-scripted-operator/`.
