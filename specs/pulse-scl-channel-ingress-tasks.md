---
feature_name: "pulse-scl-channel-ingress"
version: "0.7.1"
status: "implementation_partial"
root_work_item: "wi_20260827_282"
created: "2026-08-29"
modified: "2026-09-04"
owner: "Pulse"
dependencies:
  - "specs/pulse-scl-channel-ingress-requirements.md"
  - "specs/pulse-scl-channel-ingress-design.md"
tags: [tasks, scl, relay, feishu, telegram, deployment, uat]
---

# PULSE SCL Channel Ingress — Tasks

| id | task | state | exit gate |
|---|---|---|---|
| C0 | Record the real three-node/two-channel ingress inventory and failure | complete | Active release, service, configured Harness client and shared handler read back without mutation |
| C1 | Commit and push the Specs-only boundary | complete (`ef038bcca9b6`) | Requirements/design/tasks are the only changed files and remote readback matches |
| C2 | Add canonical plan/entry `scl_response/v1` client and channel-neutral result | complete (`992dee498a6`) | Read-only planning cannot claim replacement; exactly one mutating entry consumes real runtime readback; parser rejects leakage/malformed envelopes |
| C3 | Route shared `/wi` through sealed client; remove active legacy resume path | complete (`992dee498a6`) | Feishu and Telegram tests prove identical lifecycle call/result semantics |
| C4 | Add durable host dispatch and restart recovery | complete (`992dee498a6`) | Same valid client result is dispatched at most once; restart never repeats lifecycle entry |
| C5 | Preserve typed safe diagnostics end to end | complete (`992dee498a6`) | Real downstream codes reach both channels and allowlisted logs; secrets/private refs do not |
| C6 | Build compatible immutable Relay and SCL artifacts | complete | Exact source, wheel, manifest and protocol digests pass source and installed black-box tests |
| C7 | Stage and activate ownermac, cloud-linux-a and cloud-linux-b | complete | Each node independently reads back active artifact pair and retained rollback release |
| C8 | Run active-route visible UAT | failed (UAT1/UAT2) | Feishu and Telegram positive/negative results exist for every actual resident route; user judges acceptance |
| C9 | Move dormant-runtime activation behind sealed read-only planning | complete (`253c3aa4aac`) | `source_session_active` and other no-authority plans return unchanged even when a stale persisted Pi session has no usage binding; execution plans still require exact live readback |
| C10 | Make mechanical channel retry durable and truthful | complete (`253c3aa4aac`) | Authenticated continue intent survives restart, same-intent attempts are single-flight/backed off, final result is retained until original-route delivery, and `retry=automatic` appears only after scheduled-state readback |
| C11 | Unify all Relay lifecycle verbs behind sealed SCL | pending | `/wi`, checkpoint, rollover, reopen and review use one local frontier and no active channel path calls legacy controller mutation |
| C12 | Hydrate an existing canonical WI on local-kernel miss | pending | Cold-kernel real-shape fixture imports once with exact parity; absence and parity conflict remain distinct and no second writer starts |
| C13 | Attach logical resident source to Host Session truthfully | pending | Same-Session legacy fixture records a durable attachment without epoch/candidate/authority change; mismatch alone never emits `source_session_active` |
| C14 | Add human-first Feishu/TG lifecycle presentation | pending | Default text states mutation fact, frontier safety and one next action without raw protocol fields or unsupported positive claims |
| C15 | Regress UAT2 shapes, seal and redeploy | pending | WI568/WI282-shaped synthetic fixtures pass source, installed and three-node readback before user-directed UAT resumes |
| C16 | Specify and route `/wi reinit` as human root recovery | pending | Both channels bypass corrupt historical permission, obtain a complete Agent/human draft and call only sealed reinit apply |
| C17 | Present retired-lineage WIs as `reinit_required` | pending | No old effect or legacy writer runs; default response states no mutation, workspace safety and one reinit action |
| C18 | Make fresh-session `/wi continue` prove readiness | pending | One durable intent restores checkpoint and returns success only after attachment, fenced authority and worktree/Git-guard readback |
| C19 | Run automated channel UAT before human UAT | pending | Copied WI568/WI282/WI479 shapes pass Feishu/TG reinit, rollover and continue using exact sealed installed artifacts |
| C20 | Update lifecycle skill after product gates | pending | Skill reflects verified reopen/reinit/rollover/continue semantics and passes independent forward-testing |
| C21 | Complete resident effect routing and canonical runtime adoption | source complete; installed adoption pending | Relay passes no process-global authority root; formal install repairs/reads back canonical SCL; an existing pending effect self-settles and projects external publish available on all resident nodes |
| C22 | Publish exact PULSE input for private cross-repository adoption | implementation complete; Actions readback pending | Trusted PULSE job materializes the three required blobs under its repository token, then publishes a digest-bound minimal snapshot from an exact SHA; Harness adoption consumes it without undeclared SSH or repository-scoped token leakage; portable Linux CI uses the registered Standard GitHub-hosted continuity pool, while adoption publication and explicit trusted UAT retain their capability-bound pools |
| C23 | Fence production Harness authority to managed service repositories | implementation complete; Actions and three-node readback pending | The writer replaces all three authority variables per site, forbids Codex/WI worktrees, and activation receipts read back the exact ownermac/Vultr/TCloud repository plus `.harness` roots |

Source evidence for C21: the resident Relay launch no longer passes a process-global Harness root,
the focused reconciler contract and TypeScript typecheck pass, and the full Relay suite passes 89
test files / 1538 tests (2 files / 2 tests skipped). Formal ownermac/three-node runtime adoption and
the existing-effect readback remain open and are not implied by these source results.

The production release writer now treats the Relay launch binding and Pi runtime manifest as one
transaction on ownermac and both Linux sites: it validates every target resource before activation,
backs up both carriers, performs maintenance-gated process readback, restores both on failure and
only then reclaims releases outside the active/rollback dependency set. The exact writer/deploy
contract passes all 151 `Scripts/tests` cases on Python 3.13; this source gate still does not imply
that a node has adopted the merged release.

## Current inventory evidence

- Feishu and Telegram both call `handleWorkItemCommand`.
- The shared production client still invokes `Scripts/scl-resume.py -> harness continue`.
- ownermac runs an immutable PULSE release with the legacy script while its sealed SCL runtime is
  installed separately and not consumed by Relay.
- cloud-linux-a and cloud-linux-b run independent immutable PULSE releases; neither configured
  Harness environment currently exposes `harness-scl`.
- A real cloud-linux-b Feishu `/wi` hid downstream `identity_mismatch` as `resume_failed` and made no
  binding or durable entry-plan mutation.

## Source implementation evidence

- Harness source: `c68d44a1da00a8f8614242a65696f19f81358b81`; PULSE source:
  `992dee498a642791cfe99279be3ab8885cd77c0b`.
- The shared handler now defaults to the sealed SCL client. The legacy adapter is reachable only
  behind explicit compatibility flag `PULSE_SCL_LEGACY_RESUME_ENABLED=1` and is not the default
  channel path.
- Relay performs read-only local planning, starts and reads back the exact target runtime when a
  target-neutral replacement is required, then makes one mutating entry call. Dispatch state and
  the idempotency operation survive restart.
- Typecheck passed. The SCL/session/Feishu/Telegram slice returned `181 passed`; the full suite
  returned `1512 passed, 1 skipped` on its clean rerun. Harness returned
  `2615 passed, 1 skipped, 63 warnings, 4 subtests passed`; targeted Ruff and format checks passed.
- These source results did not by themselves establish C6-C8. Installed evidence below establishes
  C6-C7; the user's active-route visible decisions remain C8.
- Real WI568 UAT then exposed that Relay tried to revive an unrelated dormant Pi conversation before
  calling the sealed read-only planner. Source `253c3aa4aac` moves activation behind an affirmative
  execution plan and adds a strict-flush authenticated intent queue for both channels. A retryable
  result is rendered `automatic` only after queue readback; restart recovery preserves the exact
  channel-scoped pool key and delivers a final safe result to the original route. Typecheck passed,
  the focused regression set returned `140 passed`, and the clean full suite returned
  `1521 passed, 1 skipped`.

## Three-node installed evidence

- Relay source/archive is `8af9f17626a13f8f23596341f8cc6c4434f3dbf7` with archive SHA-256
  `22e1df15fb3142725cd16102b480f969b93ab5a6a91be13ccdd83c20ff1cbc1c`.
  All three platform builds read back `dist/cli.js` SHA-256
  `7cffe9ffc2d2aa5b3dfbbf8751ed4dddef2de88efd4bf6412566b127bf0307c0` and sealed-client module
  SHA-256 `c6e6a3d406c0f5ffe0001a4f2d8041cf3e512b3b157b7084b811d97a79fbc0d6`.
- Harness source is `d6e4942a251f87406c7bc626e6a2b83a167d4c52`; wheel SHA-256 is
  `06427e992ada407abd073b7f404064bbedfe031a17e011b99ab71ae7989faccb`; artifact-manifest SHA-256
  is `a5b23cbc66a78b1cec0c5e5be64aef7a5961912e8d952da287461d88b29b15c0`. Each installed
  `harness-scl handshake` returned `scl_response/v1`, `read_only`, and
  `runtime_handshake_succeeded` with the same SCL module digest.
- ownermac, cloud-linux-a and cloud-linux-b run the exact Relay release with explicit sealed-client
  path and legacy mode disabled. Each prior Relay release and Harness release remains retained.
- The first ownermac restart exposed a pre-existing Pi manifest path to absent release
  `PULSE-e64733aa...`. The failed candidate load left WI/SCL state untouched; launchd restored the
  prior plist, and Relay recovered after the manifest was backed up and repointed to the exact new
  release. A second safe activation succeeded. This is recorded as installation recovery evidence,
  not hidden as a clean first attempt.
- Post-activation runtime truth is ownermac `2 Feishu / 12 Telegram`, cloud-linux-a
  `10 Feishu / 1 Telegram`, and cloud-linux-b `7 Feishu / 0 Telegram`. The earlier six-cell matrix
  was wrong: enabling a second Telegram poller only to satisfy it would create a production defect.
  C8 therefore follows actual registered routes and remains pending human UAT.

## WI568 corrective deployment evidence

- The corrective immutable Relay release is
  `aa82d8ff615088d4fd17e65047c78772ed98d906`. ownermac, cloud-linux-a and cloud-linux-b all run
  that exact release. Their built `dist/scl-intent-retry.js` SHA-256 is
  `8c43b73ce272472c9b9e7cd0ab8e153ea9417f0fb142dbee04e724af0945bb74`; their
  `dist/work-item-session.js` SHA-256 is
  `13c3a22af6cf1d70c232fca5b6161a6e828cfdc04ecee7f900cc852b3aa72b86`. Platform-native PTY
  import and production dependency readback passed independently on all three nodes.
- Before every restart, the resident maintenance endpoint reported zero busy conversations, zero
  running scheduler jobs and `safeToRestart=true`. After activation, all three services again read
  back active with the exact release path, the same bot inventory, a `read_only` sealed SCL
  handshake and the same safety result. The prior `8af9f176...` release remains installed as LKG.
- The failed cloud-linux-b Feishu intent predated durable retry support. Its authenticated Feishu
  event identity and active Gateway binding were recovered without copying a raw message body into
  state. The first enqueue probe correctly refused the legacy inline-user lookup and made no queue
  mutation. The binding-aware retry then moved from one durable `recognized` record to zero pending
  records; code removes it only after a final SCL result and successful original-route Feishu
  delivery. Relay stayed active and emitted no retry/recovery error.
- ownermac launchd initially returned transient `EIO` for both candidate and automatic LKG
  bootstrap, leaving Relay genuinely down. A later bootstrap of the same lint-valid candidate plist
  succeeded; exact process path, HTTP maintenance endpoint, `12 Telegram / 2 Feishu`, module digest
  and SCL handshake were then read back. This is recovery evidence, not a clean first activation.
- These installed and delivery receipts close C9/C10 deployment proof. They do not close the
  user's C8 UAT verdict or manufacture visible tests for routes the user has not exercised.

## UAT2 failure evidence — 2026-08-30

- The automatically recovered Vultr Pulse Feishu `/wi wi_20260820_568` reached sealed SCL and
  returned a final read-only envelope. Its text exposed raw status fields and said
  `source_session_active`, although the user confirmed that work had remained in the same channel
  Session.
- Read-only production inspection showed the sealed kernel at epoch 8/checkpoint 30 while the
  legacy Harness status used by Relay rollover read epoch 7/checkpoint 19. Relay's current Pulse
  channel Session refs did not match the imported logical source and had no durable WI runtime
  attachment. Installed `_entry_action` treated only opaque-ref inequality as sufficient evidence
  for `source_session_active`; no distinct live source readback existed.
- Source inspection showed `handleWorkItemContinue` uses sealed local-first planning, while
  `handleWorkItemCheckpoint` and `handleWorkItemRollover` still call the legacy controller. A real
  rollover would therefore read and write through a different frontier from the preceding `/wi`.
- A subsequent Codex `/wi rollover wi_20260827_282` returned a valid `scl_response/v1` with
  `status=rejected`, `code=work_item_not_found`, conversation available and no effects. Read-only
  legacy status still contained that WI as active at epoch 1/checkpoint 5. No checkpoint,
  reservation, binding or epoch mutation occurred.
- UAT2 is therefore a product failure, not a request to retry, reopen or repair ledger bytes. C11-C15
  are engineering prerequisites; only the user decides when and how visible UAT resumes.

`@anchor manual pulse-scl-channel-tasks :: no task becomes complete without its named source, installed and visible readback gate`
