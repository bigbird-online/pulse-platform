# WI568 epoch 5 final-seal UAT — 2026-09-04

## Result

The naturally completed cloud-Pi epoch 5 is now durably sealed and accepted by the default-branch ComputeCost consumer.
This closes the epoch-5 baseline/final Token and privacy checks, but it does **not** close the full WI:

- final seal: **PASS**;
- baseline/final Token conservation: **PASS**;
- privacy and private-artifact mode: **PASS**;
- WI attribution/report: **PARTIAL**;
- ChatGPT+GitHub prospective full coverage: **PENDING**;
- S5-001 seven-day/ten-eligible-outcome gate: **PENDING**.

The machine-readable evidence summary is
`Bigbird/ComputeCost/uat/wi568-epoch5-final-seal-uat-20260904.json`.

## Scope and implementation readback

- Surface: `surface_v1_cloud_linux_b`.
- Runtime/phase: `pi` / `computecost_program`.
- Tested default revision: `99504cf39959ba4cd7f9acae6d8d46e4f7bf9d5a`.
- The installed `pi-usage-binding.mjs`, `scl_measurement.py`, and `wi_report.py` bytes match that default revision.
- This UAT reads the canonical SCL measurement lineage and the private native artifacts. It does not reuse the stale Git execution projection, backfill an earlier epoch, or manufacture downstream work.

## Seal, conservation, and privacy

The mode-`0600`, regular, non-symlink baseline contained zero events and zero Token. The final artifact contained 189 post-baseline events. Canonical SCL accepted the exact attribution/seal join and produced this delta:

| Counter | Delta |
|---|---:|
| input, including cached input | 29,022,779 |
| cached input | 28,500,352 |
| output | 85,480 |
| reasoning output, subset of output | 32,648 |
| total | 29,108,259 |
| cache write | unavailable |

Conservation passed independently:

- `total = input + output`;
- cached input does not exceed input;
- reasoning output does not exceed output;
- native final cumulative totals equal the canonical final-minus-baseline delta;
- the missing normalized cache-write dimension remains `unavailable`, not an imputed zero.

Producer and consumer schema validation passed. A recursive scan found no forbidden raw identity/content key or absolute path in the retained native and joined measurement projections. The retained UAT summary contains only privacy-safe facts and content digests.

## ComputeCost attribution and report

`computecost_wi_report/v2` accepted the exact sealed Pi artifact and canonical SCL join:

- Pi usage coverage: `exact_sealed`;
- SCL measurement coverage: `partial`;
- canonical eligible epoch count: `0`;
- missing producers: linked task outcome and human attention;
- productive/recovery/verification/adjudication classification: `unavailable` for each class;
- quality, downstream effect, efficiency, human attention, quota pressure, and actual marginal cash: `unavailable`.

A live public OpenRouter catalog valued the sealed request partitions at **$8.1824414 OpenRouter-equivalent reference cost**. This is a cross-runtime reference valuation, not OpenRouter cash, OpenAI subscription cash, or a single-WI subscription allocation. Procurement remained `unknown`, so actual cash correctly remained unavailable and no subscription was double-counted.

## Acceptance impact and next gate

This UAT proves the durable source boundary that was previously missing: a zero baseline, real post-baseline work, a naturally settled final seal, exact Token conservation, and privacy-safe ComputeCost consumption. It does not create outcome or human-attention evidence after the fact.

The remaining gates stay open without weakening their boundaries:

1. complete at least one prospective ChatGPT+GitHub full-coverage epoch using visible reference/GitHub telemetry while hidden provider Token and per-WI Pro cash remain unavailable;
2. accumulate a legal S5-001 cohort over at least seven elapsed days with at least ten eligible outcomes and complete outcome/measurement coverage;
3. only then assess quality, effect, efficiency, activity-class cost, and any measured human-attention cash where both trusted time and an explicit user rate exist.
