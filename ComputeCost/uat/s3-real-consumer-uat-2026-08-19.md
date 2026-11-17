# ComputeCost S3 real-consumer UAT — 2026-08-19

## Result

**PASS.** The acceptance gate required one real weekly-review consumption and one real Duke recommendation consumption with no manual recomputation. The earlier Codex/Claude/GLM attempts were blocked before artifact consumption; that blocker was resolved later on 2026-08-19 by persisting the real W33 artifacts under Duke `ops/` long enough for the consumer path to run, followed by direct GPT-5.6 Sol consumption of the two emitted projections.

This PASS is a **consumer-path PASS**, not authorization to change production routes. The Duke decision from the consumed recommendation is **HOLD / NO CHANGE** because price and outcome evidence remain incomplete.

## Real consumer facts

- Duke weekly-review method: `/Users/owner/Coevolution/duke-space/skills/s3-weekly-review/SKILL.md`.
- Canonical W33 report: `duke-space/memo/2026-08/s3-weekly-review-2026-W33.md`.
- Legacy inputs remain `duke-space/scripts/token-ledger.sh` and `duke-space/scripts/team-dashboard.sh`.
- The canonical W33 resource section leaves convergence cost unmeasured, so this is a real operational consumer path rather than a synthetic fixture.

## W33 operational artifact placement and durable audit oracle

The original `/private/tmp` artifacts had already been cleaned up when UAT resumed. They were **not reconstructed from the old numeric summary**. Instead, the W33 query was regenerated from ownermac's real CC/Codex runtime evidence through the S3 adapters and a fresh isolated shadow ledger, then persisted in runner-local Duke `ops/` for the live consumer pass:

- `/Users/owner/Coevolution/duke-space/ops/compute-cost-s3-w33/w33-cost-query.v1.json`
- `/Users/owner/Coevolution/duke-space/ops/compute-cost-s3-w33/w33-recommendation-artifact.v1.json`
- `/Users/owner/Coevolution/duke-space/ops/compute-cost-s3-w33/w33-weekly-consumption.v1.json`
- `/Users/owner/Coevolution/duke-space/ops/compute-cost-s3-w33/w33-duke-consumption.v1.json`
- `/Users/owner/Coevolution/duke-space/ops/compute-cost-s3-w33/w33-uat-provenance.json`

Those `ops/` files are **not Git-tracked and are mutable operational evidence**, not the long-lived acceptance oracle. A later rebuild may legitimately change recommendation validity timestamps and therefore its file SHA. The durable acceptance record is this committed document plus GitHub Actions run `32216817328`; hashes below refer to the accepted run, not necessarily to the latest runner-local copy.

The regenerated query reproduced the earlier real W33 result exactly on the acceptance facts:

- Beijing half-open window `2026-08-10 .. 2026-08-17` (`2026-W33`)
- `event_count=158`
- `total_tokens=706813638`
- `conservation.status=pass`
- `priced_event_count=0`
- `outcome_linked_event_count=0`
- `driver_delta.status=pending_s4`
- query SHA-256 at the accepted run: `c1c13101151e2510eb7f69046156e27bdcb66d9cf136309a4ac5ec84a6278a60`

No production ledger was created or mutated.

## Runtime-manager producer provenance

The earlier same-day read-only production probe remains the durable producer evidence for the recommendation:

- active preset: `phase23_canary`
- `hestia_weekly_review` effective profile: `media`
- persisted sessions: `15`, managed `15`, matching `10`, mismatched `5`
- plan reported `changed_files=18`
- terminal guard: `PLAN ONLY: no files written`
- active-route digest: `routecfg1_aaf8dcff8892668eb91b62ac48006946dc98c7e12245f1961389be26211cb227`
- plan digest: `plan1_593eaf5b8d8b98434633b41a86eaecba6c3eb4bbb86721ae1e20aeff20c7610a`

During the rerun, a fresh live-manager reprobe was attempted. SSH resolved to user `pulse`, and `/home/pulse/Coevolution/Bigbird/PULSE` still existed, but the candidate runtime-manager files used by the earlier UAT were no longer present in the non-backup live tree. A deploy backup was found and **explicitly refused as current production evidence**.

Therefore the accepted recommendation artifact was rebuilt with the official S3 builder from: (a) the freshly regenerated real W33 query, (b) the persisted same-day real producer facts above, and (c) the current PR preset contract. `w33-uat-provenance.json` records this distinction. Actions run `32216817328` directly logged the accepted artifact SHA-256 as `541e8f56b4975e89304d860cb809c638ab8c39f090e6edf33681e090d65fd401`. A later runner-local rebuild changed validity timestamps and file SHA; that later file is not used as the acceptance oracle.

This provenance limitation does not weaken the consumer gate: the gate is whether the real weekly-review and Duke consumers can consume valid artifacts without recomputation. It **does** mean this UAT must not be cited as a fresh live-manager health check.

## Real weekly-review consumption

GitHub Actions run `32216817328` projected the persisted query through `s3-consume.py weekly`; the UAT LLM consumer (GPT-5.6 Sol) then directly read the emitted `weekly_cost_consumption.v1` JSON.

Consumed facts included:

- `manual_recalculation_required=false`
- `conservation_status=pass`
- W33 `event_count=158`, `total_tokens=706813638`
- W32 baseline `event_count=39`, `total_tokens=158189655`
- producer-provided delta `event_count=+119`, `total_tokens=+548623983`
- quality status `partial`
- price evidence unavailable; outcome coverage incomplete
- `driver_delta.status=pending_s4`

The LLM did not recompute any token, cost, share, or delta. It correctly interpreted this as visibility into usage volume, **not** evidence that any route should be changed.

## Real Duke recommendation consumption

The same run projected the persisted recommendation through `s3-consume.py duke`; the UAT LLM consumer then directly read the emitted `duke_recommendation_consumption.v1` JSON.

Consumed decision facts:

- `manual_recalculation_required=false`
- `status=insufficient_evidence`
- current/target preset: `phase23_canary`
- `expected_changed_routes=[]`
- `production_apply_authorized=false`
- reasons: `outcome_coverage_incomplete`, `price_coverage_incomplete`, `price_stale`
- conservation/frontier/preset/transition constraints passed; price/outcome constraints did not

**Duke decision: HOLD / NO CHANGE.** No route/profile/preset change is approved from this evidence. That is the expected fail-closed consumer behavior. The machine projection still carries `duke_decision=pending` by design because it is the artifact presented *to* Duke; HOLD / NO CHANGE is the downstream consumer decision and must not be back-filled into the recommend-only artifact as apply authority.

The Actions run itself proves projection and safety invariants; it does not by itself substitute for the LLM consumption. The subsequent GPT-5.6 Sol read supplies that real-consumer step. The run ended with `UAT_INVARIANTS_PASS zero_recalculation=true recommend_only=true production_apply=false`.

## Earlier blocked attempts retained as history

Before the durable `ops/` path existed, all real LLM attempts stopped before artifact consumption:

1. Codex on ownermac Duke workspace: account usage limit.
2. Codex on Vultr Duke workspace: same account usage limit.
3. Claude Code on Vultr: `401 OAuth access token has been revoked`.
4. Harness `claude_glm` fallback: `429 [1113] insufficient balance/no resource`.

Those failures remain valid historical evidence; they are superseded only with respect to the consumer acceptance gate. No credential value was printed or persisted.

The old temporary `ComputeCost S3 Runtime UAT` run `32208149369` also remains classified as a workflow configuration failure (`PYTHONPATH` omitted) and never counted as consumer evidence.

## Acceptance decision

- Weekly-review real consumer: **PASS** — GPT-5.6 Sol directly consumed `weekly_cost_consumption.v1` with no manual recomputation and produced a valid weekly-review interpretation.
- Duke recommendation real consumer: **PASS** — GPT-5.6 Sol directly consumed `duke_recommendation_consumption.v1` and made the expected **HOLD / NO CHANGE** decision.
- S3 real-consumer merge gate: **PASS**.
- UAT was not waived.
- Production apply authority remains **false**; S3 remains shadow/recommend-only.

## S4 readiness facts

S4 remains separate from this acceptance result:

- implement the fixed `D → R → E → P + residual` driver bridge with exact delta conservation;
- keep outcome as an independent quality constraint;
- extend the existing runtime priority manager, not ComputeCost, with a structured read-only plan carrying route diff, active-route digest, plan digest, and precondition;
- treat observed runtime drift as a precondition/concurrency case, not permission to apply;
- do not infer cost optimization from token volume while price/outcome coverage is incomplete.
