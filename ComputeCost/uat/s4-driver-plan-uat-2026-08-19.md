# ComputeCost S4 driver/plan UAT — 2026-08-19

## Scope and safety boundary

This UAT validates S4-001 only: the fixed-order cost-driver bridge, recommendation-to-manager-plan binding,
and the existing runtime manager's new read-only structured plan. It does **not** execute `apply`, deploy,
restart services, create a production usage ledger, or start the S5 seven-day active-canary window.

Implementation under UAT: PR #10 head `a5b464c1363a9695e6429255bd6d3f6f014388cf`.

## Production runtime read-only plan

The UAT was initiated from ownermac and executed the PR-head `pulse-runtime-priority.py` over the current Vultr
production runtime through SSH. The branch manager and branch cost-preset catalog were copied only to
`/tmp/pulse-computecost-s4-uat-a5b464c`; all managed inputs remained the live production files under
`/Users/owner/Coevolution/Bigbird/PULSE`, `/Users/owner/Coevolution/.harness/policies`, and
`/home/pulse/.codex/config.toml`.

Observed live state:

- production repository HEAD: `2b565bbcac06c71ac55ec1d24474b7eae10b57fd`
- active preset: `phase23_canary`
- raw `data/runtime-cost-route.json` SHA-256 before plan: `aaf8dcff8892668eb91b62ac48006946dc98c7e12245f1961389be26211cb227`
- raw route SHA-256 after plan: `aaf8dcff8892668eb91b62ac48006946dc98c7e12245f1961389be26211cb227`
- structured-plan schema: `runtime_priority_plan.v1`
- active route digest: `routecfg1_633b446fa5164e231e14845e4c8131f03f2edd845ddd0cc02213516bb17bab30`
- target route digest: `routecfg1_633b446fa5164e231e14845e4c8131f03f2edd845ddd0cc02213516bb17bab30`
- plan digest: `plan1_35a60dd3b0edabd93eb8eb0d12c4a39b6f7cfcafc840ed4fa632a04d71b0ed1c`
- `expected_changed_routes=0`
- `global_compute_profile_changed=false`

The UAT snapshot-hashed runtime priority, Relay, cron, active cost route, compute-profile catalog, Codex config,
and all Harness policies before and after the plan. The snapshots were byte-identical. The configured backup
sentinel was never created, so the command produced no backup/write side effect. The temporary remote UAT
directory was removed after verification.

Result: **PASS — `managed_files_unchanged=true backup_created=false apply_invoked=false`.**

## High-risk bridge and recommendation fixtures

The same PR-head implementation then ran the exact S4 high-risk/fail-closed tests on ownermac:

- `CostQueryTests.test_s4_driver_bridge_explains_retry_model_and_outcome_changes`
- `CostQueryTests.test_s4_driver_bridge_fails_closed_without_pro_settlement_evidence`
- `RecommendationArtifactTests.test_promote_without_manager_plan_fails_closed`
- `RecommendationArtifactTests.test_manager_plan_digest_or_route_diff_mismatch_fails_closed`

All four passed. The high-risk fixture changes retry, model, and outcome together while the four cost segments plus
residual exactly recompose observed delta; outcome remains a quality constraint. Missing weekly Pro settlement and
missing/tampered manager-plan evidence fail closed.

Result: **PASS — `retry_model_outcome_fixture=true recommendation_plan_binding=true recommend_only=true production_apply=false`.**

## CI and temporary-resource note

PR-head `a5b464c1363a9695e6429255bd6d3f6f014388cf` passed formal ComputeCost CI run `32254519724`.
A temporary self-hosted workflow run `32254519773` never started because the runner remained queued; it was
cancelled and is **not** acceptance evidence. The one-shot workflow file is removed in the closeout commit.

The merge path was also reviewed for accidental deployment. `deploy.yml` previously matched the ComputeCost
spec-drift guard through the broad `Scripts/**` path; the PR now excludes
`Scripts/hooks/compute-cost-spec-drift-guard.sh`, so this recommend-only S4 merge does not authorize or trigger the
production deploy/restart workflow.
