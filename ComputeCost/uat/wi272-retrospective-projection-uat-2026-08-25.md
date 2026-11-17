# WI272 retrospective ComputeCost projection UAT

- Status: **PASS**
- Candidate base: PULSE `7072547fe91c8fe0d31e97d1f798648b56e0a097`
- Work item: `wi_20260820_272` (terminal state was not mutated)
- Report: `Bigbird/ComputeCost/uat/wi272-retrospective-projection-20260825.json`
- Report content SHA-256: `c596b8462d339c998ce8039a3867899095452d97a3daf947ca39ee41bfa81c6a`
- Report file SHA-256: `ab38204d51724a7ad32c432bf979c8b9c16107226b8d56d0a727b2b4cc2f3ce7`

## Inputs

- Harness measurement projection: `.harness/evidence/measurements/wi_20260820_272/s1-result-outcome-reconciled-2026-08-22.json`
- Measurement file SHA-256: `2b17b10318908d8afc16f59d5090dd1e9797fee79bd42d430ecfb25c0cb75a54`
- App Server retrospective ledger: empty host-owned evidence boundary, SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- No historical hidden usage, raw Session ID, subscription allocation, inferred procurement source, review snapshot, or business-value receipt was supplied.

## Assertions

1. The report schema is `computecost_wi_report/v2` and is bound to `wi_20260820_272`.
2. Historical usage is `status=unavailable`; both `tokens` and `conservation` are `unavailable`, not numeric zero.
3. Available canonical facts remain visible: delivery `accepted`, final review `approved`, three session segments, nine planned rollovers, seven runtime recoveries, and a result cycle of 159342 seconds.
4. Missing first-pass review, rework, correction, re-explanation, token cost, human attention, cash, quota, artifacts, downstream effect, and business value remain `unknown` or `unavailable`.
5. Coverage remains partial with explicit authority-gap warnings; no missing producer is inferred from provider, runtime, host, path, or model.
6. The full ComputeCost suite passes `161/161` after adding the unavailable-not-zero regression.

## Result

V2-006 is satisfied as a retrospective, unknown-preserving projection. This evidence does not alter WI272 authority or claim historical usage completeness.
