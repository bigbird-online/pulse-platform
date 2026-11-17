# WI779 ComputeCost material-defect UAT — 2026-09-09

## Scope and safety

- Base: `origin/main@9516975af4f428434239b789f6e94017f1401549`.
- Target: registered macOS worktree and branch only.
- Read-only canonical sources; no lifecycle mutation, historical-source edit, merge, deploy, or post-terminal backfill.
- Reports retain only canonical WI IDs plus one-way evidence digests. No raw user/session/thread/chat/tenant identity, prompt, credential, or absolute source path is committed.

## Synthetic WI342-shaped end-to-end UAT

Command:

```text
PYTHONPATH=Bigbird/ComputeCost python3 -m unittest \
  Bigbird.ComputeCost.tests.test_wi342_uat -v
```

Result: **PASS**.

The fixture proves, through the public `wi-report` CLI:

- two exact accepted Harness verifier assignments bind two Codex children whose prompts contain no WI text;
- one archived child rollout contributes Token once, while a second indexed child with no rollout leaves Codex and Token coverage `partial` rather than `producer_missing`;
- active/archive copies, controller/child lineage, and request events are deduplicated;
- a mixed-WI lineage is rejected by the adjacent negative fixture;
- the 24-hour attention exposure, 10 exact prospective feedback events, six category counts, 10 G0 HAU, density `10.000000/day`, and focused-time unavailability are visible without conflation;
- a request above 272K uses the same top-level standard unit rates, never the catalog override;
- native cost and subscription settlement are excluded;
- the complete serialized report contains none of the fixture's raw identities, prompt, turn, workspace path, or assignment IDs.

## Installed-source terminal retrospective: WI342

The current worktree implementation read the installed canonical Harness source and local producer inventory. Only a privacy-safe field projection was printed; the full report was not persisted.

| field | observed result |
|---|---|
| report schema | `computecost_wi_report/v3` |
| lifecycle | `completed` |
| created→terminal exposure | `177022` seconds = 49h 10m 22s |
| human review approval | `1` canonical `work_item.review_approved` event |
| focused seconds | `unavailable` |
| exact human feedback count | `unavailable` |
| G0 HAU | `unavailable` |
| accepted verifier assignments | `5` |
| distinct assigned verifier roles | `1` |
| outcome-bound assignments | `4` |
| physical assigned sessions | `unavailable` |
| Codex attempt | `partial` |
| Codex Token coverage | `unavailable` |
| canonical Token total | `unavailable` |
| retrospective Pi source | `source_missing` |
| retrospective price | `token_quantity_unavailable` |
| subscription settlement | `excluded_from_wi_report_and_roi` |
| report digest | `330f94e384af293d8e94a6a992a7c5322a7ca1f335b95deed38188248ea7a6e7` |
| privacy scan | absolute path=false; raw identity key=false |

Classification: **PASS for unknown-preserving retrospective behavior; PARTIAL evidence coverage**. The report proves lifecycle exposure, the human approval, and assignment counts. It does not claim unavailable feedback, HAU, Codex Token, Pi Token, cost, or ROI.

## Historical Pi-only recalculation

Using the explicitly supplied created→terminal historical quantities and the flat standard OpenRouter rates:

```text
uncached input  2,338,224 × $2.0 / 1,000,000  =  $4.6764480
cache read     95,818,112 × $0.2 / 1,000,000  = $19.1636224
output            188,783 × $10  / 1,000,000  =  $1.8878300
                                                     -----------
Pi-only retrospective estimate                         $25.7279004
```

`WiPricingTests.test_historical_pi_flat_standard_subtotal_is_exact` reproduces the Decimal result and includes a hostile >272K override that must remain unused. This is a **retrospective Pi-only estimate**, not canonical WI total: the installed Pi request source, exact binding, and seals are absent.

## WI383 regression

The existing cancelled-WI fixture remains PASS: retrospective evidence is visible when present while canonical Token/cost/ROI remain unavailable. The installed-source read also remains safely reportable as `cancelled`, with elapsed exposure retained and missing producer facts typed unavailable.

## Upstream evidence gaps (fail closed)

1. Current canonical Harness Handoff/lifecycle/outcome records prove assignment, verifier role, acceptance, and outcome binding, but do not carry an opaque physical Codex Session/rollout producer ref. With no installed Codex session index or archived rollout, physical assigned-session count and Token are therefore unavailable.
2. Current Relay `req.inbound` / `turn.end` evidence has no exact per-event WI binding or common opaque human-event identity. A terminal session registration plus a time window is insufficient, especially for a reused/shared session. PULSE therefore retains approval and elapsed exposure but does not count Relay outer turns as feedback.
3. The historical Pi JSONL/request partitions are absent on the installed target. No Token is reconstructed from verifier prose, prior report prose, or the user-supplied subtotal.

A future producer can close (1) with an opaque assignment→runtime-session reference and close (2) with an exact WI-bound human-event schema. This change does not invent either join.
