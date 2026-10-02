# Fault matrix

Each runtime condition maps to its own status and code (business outcome, recovered, rejected, or a specific hard failure). The full matrix, with more cases, runs in `tests/integration/test_fault_matrix.py`.

| Condition | Expected | Observed | Matches |
|---|---|---|---|
| Happy path | succeeded - | succeeded - | yes |
| Unknown member | business_outcome MEMBER_NOT_FOUND | business_outcome MEMBER_NOT_FOUND | yes |
| Malformed member number | rejected INVALID_INPUT | rejected INVALID_INPUT | yes |
| Known security notice | succeeded - | succeeded - (recovered: KNOWN_INTERSTITIAL) | yes |
| Session expires mid-run | succeeded - | succeeded - (recovered: SESSION_EXPIRED) | yes |
| Server error page | failed APP_ERROR | failed APP_ERROR | yes |
| Unknown modal, nobody attending | failed UNKNOWN_MODAL | failed UNKNOWN_MODAL | yes |
