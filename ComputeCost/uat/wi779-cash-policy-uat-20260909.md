# WI779 machine-cash policy consolidation UAT — 2026-09-09

## Scope and safety

- Base: `origin/main@288d420a34f69046f0221cd0ab771604b7f0d4d9` (PR #467 merged).
- Target: registered macOS worktree and branch `pulse/wi_20260906_779-relay-rollover` only.
- Read-only canonical sources; no lifecycle mutation, merge, deploy, or production state change in this turn.
- Reports retain only canonical WI IDs plus one-way evidence digests. No raw user/session/thread/chat/tenant
  identity, prompt, credential, or absolute source path is committed. The OpenRouter management key stayed in
  the host environment and was never printed or committed.

## Policy under test

One WI machine cash policy: exact Token classes × top-level flat standard OpenRouter rates from a
timestamped, digest-bound `GET /api/v1/models` catalog. The calculated conserving Decimal total is the
`machine_cash_usd` used in ROI, independent of procurement route or subscription. The Harness/provider
native cash field is `provider_native_cash_usd` context only and can never override the policy. No
subscription allocation or settlement; OpenRouter generation billing may corroborate Token/cost
conservation but is not a competing WI valuation authority.

## Installed-source terminal retrospective: WI342

Command (read-only, privacy-safe projection printed; full report not persisted):

```text
HARNESS_ROOT=$HARNESS_ROOT PYTHONPATH=Bigbird/ComputeCost \
  python3 -m pulse_compute_cost wi-report wi_20260906_342 \
  --harness-root "$HARNESS_ROOT" --machine ownermac
```

| field | observed result |
|---|---|
| report schema | `computecost_wi_report/v4` |
| lifecycle | `completed` |
| usage.status / tokens | `unavailable` / `unavailable` |
| `cost.machine_cash_usd` | `unavailable` |
| `cost.cash_basis` | `openrouter_standard_token_cash` |
| `cost.cash_coverage` | `unavailable` |
| `cost.provider_native_cash_usd` | `unavailable` |
| `cost.retrospective_machine_cash_usd` | `unavailable` |
| `cost.retrospective_cash_coverage` | `token_quantity_unavailable` |
| retrospective flags | `canonical_attribution=false` `canonical_total_eligible=false` `may_include_unrelated_turns=true` `noncanonical_total=true` |
| `cost.subscription_settlement` | `excluded_from_wi_report_and_roi` |
| report digest | `dd5d4e9107e8628716a7df4ab92bfb54779fd4ff8c79c6b0fcbc934ae580f92c` |

Semantics: the canonical total is unavailable because Codex/Token attribution is incomplete on the
installed target — not because provider cash or subscription evidence is absent. The zero-subscription
false default is gone: `cash_basis` is the policy string everywhere, and cash availability lives in
`cash_coverage`.

## Synthetic WI342 end-to-end (canonical priced usage)

`Bigbird.ComputeCost.tests.test_wi342_uat` PASS: a >272K request keeps top-level standard tier, native
cost and subscription settlement are excluded, and the canonical priced cohort now populates
`machine_cash_usd=0.116` with `cash_coverage=calculated_from_digest_bound_openrouter_catalog` and
`cash_basis=openrouter_standard_token_cash` on schema `computecost_wi_report/v4`.

## Historical Pi-only retrospective estimate

The explicitly supplied WI342 created→terminal Pi quantities price to exactly `$25.7279004`:

```text
uncached input  2,338,224 × $2.0 / 1,000,000  =  $4.6764480
cache read     95,818,112 × $0.2 / 1,000,000  = $19.1636224
output            188,783 × $10  / 1,000,000  =  $1.8878300
                                                     -----------
Pi-only retrospective estimate                         $25.7279004
```

`WiPricingTests.test_historical_pi_flat_standard_subtotal_is_exact` and the new
`CashPolicyTests.test_wi342_historical_retrospective_cash_estimate_is_flagged_noncanonical` reproduce the
exact Decimal and prove the estimate object carries `may_include_unrelated_turns=true`,
`noncanonical_total=true`, `canonical_attribution=false`, `canonical_total_eligible=false`, and a hostile
>272K override remains unused. This is policy-visible retrospective cash, never canonical.

## Regression suite (new `test_cash_policy.py`)

1. `test_canonical_priced_usage_populates_machine_cash_and_coverage`
2. `test_harness_native_cash_cannot_override_policy_machine_cash`
3. `test_native_cash_alone_never_creates_machine_cash`
4. `test_above_272k_request_keeps_flat_standard_tier_and_machine_cash`
5. `test_unknown_usage_is_not_zero_and_roi_eligibility_stays_strict`
6. `test_deprecated_not_provider_cash_basis_is_rejected_not_aliased`
7. `test_subscription_and_native_cash_never_drive_wi_machine_cash`
8. `test_wi342_historical_retrospective_cash_estimate_is_flagged_noncanonical`

## Verification

```text
PYTHONPATH=Bigbird/ComputeCost python3 -m unittest discover -s Bigbird/ComputeCost/tests   # 225/225 PASS
python3 -m compileall -q Bigbird/ComputeCost/pulse_compute_cost Bigbird/ComputeCost/tests  # OK
node --test Bigbird/ComputeCost/tests/test_pi_usage_extension.mjs                          # 17/17 PASS
```

Spec-drift guards ran; this sparse worktree lacks `vultr-pulse-space/skills/spec-anchor/src/scan.py`,
so all four guards fail open ("放行未检查"). Full-checkout CI runs the scanner for real.

Classification: **PASS for one-machine-cash policy consolidation**; unknown-preserving behavior,
nothing installed, merged, or deployed in this turn.