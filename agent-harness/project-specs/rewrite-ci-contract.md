# WI018: bounded CI for the replacement core

Status: implemented and verified on exact trusted Linux candidate 984a0ba7c (2026-10-09).

## Goal and boundary
Daily new-core development and Muse candidate verification must finish in minutes.
One source-bound entry runs all replacement-core contracts, strict typing and lint.
Feature risk: high; slice risk: high (release evidence). Production deployment
authority, existing state, runner admission and canonical interpreters stay intact.

## Current evidence
Trusted run 37864821554 spent 4520.324s in the combined Harness suite:
old compatibility 4429.852s, replacement core 77.584s. The automatic selector
did not recognize harness_rewrite/tests_rewrite. Run 37883995475 was cancelled,
not accepted as a successful deployment gate.

## Verification ownership
- harness-ci.yml runs the complete replacement core on PR/main. No AST classifier,
  smoke/runtime/full tiers, implicit old-suite fallback, wheel rebuild or old CLI UAT.
- Scripts/ci/harness-check.py is the sole core execution boundary, also used by
  the exact-commit Muse candidate workflow. It owns isolation, failure propagation,
  elapsed budget and machine-readable phase evidence.
- The old kernel is retired; no workflow runs its tests or installed wheel.
- The manual Muse candidate gate retains exact source, control-plane, Relay and
  ComputeCost checks, then the same complete new-core gate. It no longer rebuilds
  the old wheel, repeats old preflight tests, or runs old full Harness by default.
- Delete tests that prescribe retired YAML step sequences. Replace them with
  execution-boundary tests for failure, time budget, clean environment, source
  identity and scratch ownership. Retain fork/lease/permission negative tests.

The retired Muse-only canary branch is removed: every successful candidate run
now includes the integration gate; host health alone cannot produce that success.

## Acceptance
1. Native suite, lint and typing run exactly once through the common entry.
   The suite runs as interleaved parallel shards (one per CPU, at most four),
   each in its own scratch; together they are every collected test, each run once,
   and their JUnit merges into one harness-results.xml.
   No inherited pytest filters/plugins/coverage or installed legacy Harness imports.
2. Child failure/timeout or source mismatch makes the gate fail. No retries,
   success masks, unknown-diff fallback or reusable success from another SHA.
3. Private per-invocation HOME/state/tmp; cleanup only that invocation's scratch.
   Existing files and original host sessions remain unchanged.
4. Execution budgets: core 300 seconds, automatic job 10 minutes,
   candidate integration job 15 minutes (queue time reported separately).
   A breach is a failure to investigate, not grounds to extend time silently.
5. Real trusted Linux candidate run must succeed and publish exact SHA, phase
   durations and JUnit. Compare observed time with the baseline above.
6. Legacy validation is explicitly labelled and independently routed; changing
   old runtime/installer/Relay SCL consumer sources still triggers it.

## Non-goals and risk
This does not remove old deployed runtime code or prove Telegram UAT. Legacy
regressions remain until actual old consumers migrate; their elapsed time must
not be disguised as new-core cost. Branch validation does not publish main or
activate Muse. The existing exact-run receipt remains the Muse deployment input.

## Local evidence

Complete replacement core: 632 tests passed in 103.86s; lint and strict mypy
(36 modules) passed; full common entry elapsed 107.387s, below 180s.
CI/control-plane targeted tests: 111 passed plus 81 subtests. All Scripts
contracts: 676 passed in 80.816s using physical macOS TMPDIR=/private/tmp;
the default /var alias produced seven path-identity fixture failures before
that environment correction. Workflow topology and global spec drift passed.
These are local working-tree results, not a successful exact GitHub run.

## Exact Linux evidence

Run [37890147054](https://github.com/bigbird-online/PULSE/actions/runs/37890147054)
passed on bigbird-pulse at source 984a0ba7c3533fddb1bbf6c1f22bc567fcdd9b75.
Job wall time: 492s (8m12s); dispatch to completion: 499s. Core gate: 64.026s,
including all 632 tests (zero skips/failures), lint and strict mypy. Control-plane
checks took 53s, ComputeCost 33s and Relay 305s. Full phase evidence and digests:
[evidence/rewrite-ci-cleanup-20261009.json](evidence/rewrite-ci-cleanup-20261009.json).
The old 75-minute combined Harness scope is retired from this native gate;
the old production compatibility suite remains separately available and is
not claimed faster. Documentation/evidence updates do not replace this exact
validated deployment candidate or trigger another manual full validation.
