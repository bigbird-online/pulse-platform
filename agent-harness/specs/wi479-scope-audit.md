---
name: "WI479 Scope Audit"
status: "decided"
work_item: "wi_20260822_479"
date: "2026-08-23"
owner: "Pulse"
---

# WI479 problem, target, non-goals, and delivery choice

## Problem

The original user-authorized problem is real and narrower than the current pull
requests. Agent Sessions and host automation can write a shared checkout while the
existing `git-sync` flow treats a local branch as the source of truth. A dirty,
ahead, or diverged endpoint then has no safe automatic reconciliation path. The
result is either blocked delivery or pressure to use stash/reset/clean/rebase and
silently discard another actor's work.

WI479 must therefore separate two authorities:

1. Harness grants write authority only for the exact registered WI worktree.
2. GitHub grants distribution authority only for one pinned closeout SHA.

The 2026-08-23 retention-timer incident is a concrete counterexample: a healthy
timer moved 446 reports into the shared root, left them untracked, and correctly
caused resident entry to fail with `shared_root_dirty`. PR 63 preserved those files,
and the later WI136/WI730/WI938 execution ledgers showed the same architectural
defect. Retention remains a separate producer to isolate; execution-ledger writers
must be mechanically routed away from the shared root rather than periodically swept.

## Target behavior

- Bare `/wi <id>` means enter or continue that WI on the current registered surface.
  It validates the WI owner/executor and every registered writable repository before
  granting authority. It does not checkpoint or create a fresh Session implicitly.
- `/wi checkpoint <id>` persists the current frontier without changing Session
  authority. `/wi rollover <id>` (or the continuation-specific rollover command) is
  the explicit fresh-Session transition and must revalidate the registered worktree.
- Normal commands run through a no-shell launcher from the registered WI worktree.
  Git-resident execution, checkpoint and surface ledgers are routed into that worktree;
  direct execution writes to the shared primary checkout fail closed. The launcher pins
  the child `HARNESS_ROOT` to the admitted store for the rest of the lifecycle.
  Same-account deployment is honestly reported as `protocol_only`; a hard boundary
  requires a separately authorized account/ACL rollout and executable probes.
- Explicit closeout pins the GitHub branch SHA, preflights every required endpoint,
  and applies only `merge --ff-only <sha>`. Any dirty, ahead, diverged, unreachable,
  or identity-mismatched endpoint stops before coverage is claimed.
- CollaborateWithAI, PULSE, and Oikos remain separate Git boundaries. Missing Oikos
  tcloud enrollment stays pending rather than being represented as four-end proof.
- Receipts and site registrations remain outside Git roots; final completion requires
  independent outcome evidence, exact human review, current-head CI/smoke, and cleanup.

## Non-goals

- Redesigning GitHub-hosted versus self-hosted Actions scheduling, fallback, billing,
  workflow permissions, or deployment serialization.
- Reopening WI730 resume-latency, broker routing, or autonomous-rollover scope.
- Automatically fixing dirty/diverged endpoints, committing arbitrary shared-root
  files, creating the missing Oikos checkout, or changing credentials.
- Claiming an OS-enforced boundary under the current shared account, or performing an
  account/ACL migration without separate user authorization.
- Making checkpoint, ordinary Session exit, or every `/wi` entry trigger convergence.

## Options considered

1. **Merge PR 59 and PULSE PR 131 as-is — reject.** The core worktree/converger
   implementation matches WI479, but later commits mix in broad Actions resource and
   permission policy. Both branches are now stale relative to newer S0R/main changes.
2. **Discard both candidates — reject.** The core implementation already covers the
   required fail-closed states and has focused tests; throwing it away adds risk without
   correcting the scope problem.
3. **Port the core onto current bases and supersede the broad companion — selected.**
   Start from current `feature/doc-para`, carry only worktree/execution/converger code,
   specs, and focused tests, then resolve conflicts against the landed resume fast path.

## Existing-change disposition

- **Keep and revise:** `worktree.py`, `worktree_cli.py`, `external_state.py`,
  `converger.py`, `converger_cli.py`, the execution binding/entry integration, focused
  tests, and the three WI479 Specs. Revalidate the retention-timer counterexample and
  the current one-call resume path on the new base.
- **Split out of WI479:** all PR 59 workflow routing changes,
  `run-under-active-authority.py`, and their workflow/runtime-release tests.
- **Supersede:** PULSE PR 131. Current PULSE `main` already contains later WI730 surface,
  resume, and broker-routing work; a new PULSE change is justified only for a narrow,
  reproducible WI479 integration gap.
- **Do not manufacture coverage:** ownermac's current dirty/diverged `feature/doc-para`
  state and missing Oikos tcloud checkout remain explicit preflight failures until their
  owners or separately authorized operations resolve them.

The next implementation action is to construct the narrow CollaborateWithAI candidate
from the current base without rebase or force-push, run focused and full verification,
and update PR 59 to the exact new candidate before any human approval or convergence.
