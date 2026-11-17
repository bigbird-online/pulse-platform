# WI568 epoch 6 final-seal UAT — 2026-09-06

## Result

The naturally completed cloud-Pi epoch 6 is durably sealed and accepted by the current default-branch ComputeCost consumer. This closes the checkpointed cloud-linux-b final-usage frontier:

- final usage evidence: **PASS**;
- baseline/final Token conservation: **PASS**;
- privacy and private-artifact mode: **PASS**;
- canonical attribution/seal join: **PASS**;
- linked outcome and human attention: **UNAVAILABLE**;
- ChatGPT+GitHub prospective full coverage: **PENDING**;
- S5-001 seven-day/ten-eligible-outcome gate: **PENDING**.

Machine-readable evidence is in `Bigbird/ComputeCost/uat/wi568-epoch6-final-seal-uat-20260906.json`.

## Frozen scope and readback

- Surface: `surface_v1_cloud_linux_b`.
- Runtime/phase: `pi` / `computecost_program`.
- Execution epoch: `6`.
- Validation default revision: `307bb878ae1d7531984b946b0fc371a6bfc87c52`.
- Current installed PULSE revision: `187ced051c997768247ab530b12f2f8e2eed16ec`.
- The installed and validation-default bytes of `pi-usage-binding.mjs`, `scl_measurement.py`, and `wi_report.py` match exactly.
- The capture revision remains `unavailable`: the native artifact does not independently retain producer source revision, so this UAT does not infer one from later runtime state.

## Final usage and Token conservation

The private baseline, open, and sealed artifacts are regular non-symlink files with mode `0600`. Their content digests validate. The baseline contains zero events and zero Token; the open and final artifacts have identical totals for 32 naturally recorded post-baseline events.

| Counter | Final-minus-baseline delta |
|---|---:|
| input, including cached input | 2,201,949 |
| cached input | 1,987,072 |
| output | 14,207 |
| reasoning output, subset of output | 5,882 |
| total | 2,216,156 |
| normalized cache write | unavailable |

The native counters conserve, the canonical seal conserves, and the canonical delta matches the native final exactly:

- `total = input + output` in the normalized seal;
- cached input does not exceed input;
- reasoning output does not exceed output;
- cache write remains `unavailable`, not an imputed zero.

## Canonical join and privacy

The current SCL measurement projection validates and contains exactly one attribution/seal join for epoch 6. The join remains `partial`, not complete: no linked accepted outcome or human-attention producer exists for this epoch, so it is not measurement-eligible.

Producer and consumer schemas pass. Recursive scans of the retained private snapshots and canonical projection found no forbidden raw identity/content key or absolute path. No historical hidden usage, outcome, attention, quality, effect, efficiency, or cash value was backfilled.

## Acceptance impact and next gate

This UAT completes the cloud-linux-b final-usage, Token-conservation, and privacy action carried by checkpoint revision 15. It does not close the full WI.

The next permitted action is to begin the **serial** prospective UAT on macOS. The same WI must not run concurrently on another resident surface. Tencent Cloud follows only after the macOS window is settled and validated. ChatGPT+GitHub full coverage and S5-001 retain their original evidence and elapsed-time boundaries.
