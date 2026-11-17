# WI730 S0R dogfood — resident continuation latency

Date: 2026-08-22
Root WI: `wi_20260822_730`
Observed continuation: tcloud Feishu bot continuing `wi_20260820_568`

The real resident continuation eventually restored the same WI correctly, but took about seven minutes before returning to the business frontier. The Session spent its critical path rediscovering workspace, Git/PR/Actions, old runtime binding and surface routing even though the prior `/rollover` checkpoint already contained completed/pending work, decisions, risks, gates and next action.

Verdict:

- SCL authority correctness: PASS
- provider-neutral resident adoption: PASS
- no backfill: PASS
- continuation momentum / TTFWA: FAIL
- observed TTFWA: approximately 7 minutes
- target after S0R: resident P95 < 30 seconds

This document records the regression; it is not proof that S0R passes. A new real Feishu/resident continuation must be run after the fast path lands.


## ChatGPT Web brokered continuation regression

Canonical entry Issue `#133` was opened at `2026-08-22T23:57:34Z` and closed with `CHATGPT_WI_ENTRY_OK` at `2026-08-22T23:59:22Z`.

- authority correctness: PASS
- sanitized receipt correctness: PASS
- no checkpoint backfill: PASS (`checkpoint_revision=0`, matching the missing prior `/rollover`)
- observed end-to-end TTFWA: approximately 108 seconds
- brokered target: < 60 seconds
- continuation momentum / TTFWA: FAIL

The workflow still permits a second hosted-Linux scheduling attempt for publish after the same request already proved the hosted pool unavailable before preflight began. The request-scoped circuit breaker under test removes that serial retry only when trusted preflight fallback succeeded; executed failures remain non-reroutable. A new canonical Issue on merged default-branch code is required before P3-S0R-005 can pass.


## Post-merge brokered samples

The request-scoped publisher circuit breaker merged in PR `#134`. Two default-branch canonical entries then exercised the exact path:

| Issue | Timing context | Durable result | TTFWA | Target |
|---|---|---:|---:|---:|
| `#135` | opened immediately after merge/default-branch validation began | PASS | 291s | FAIL |
| `#136` | sequential idle retest after `#135` completed | PASS | 48s | PASS |

Together with pre-fix Issue `#133` (108s), observed samples are `48s, 108s, 291s`; nearest-rank observed P95 is 291s. Correctness is 3/3, but P3-S0R-005 remains blocked because the performance distribution fails. The durable Issue timestamps prove the SLO result but do not attribute the 291-second sample to a specific Actions stage. The next acceptance run must publish its workflow run id so job/log timing can identify whether hosted preflight, trusted Linux fallback, canonical macOS mutation, or publisher scheduling consumed the queue time before any pool-priority or capacity change is proposed.


## Exact stage attribution — Issue #138

Issue `#138` was intentionally opened immediately after PR `#137` merged so default-branch verification and broker entry competed under a realistic post-merge load. It closed successfully from `2026-08-23T00:24:53Z` to `2026-08-23T00:29:48Z`: 295 seconds, FAIL. The durable acknowledgement exposed workflow run `32607789825`.

| Boundary / job | Evidence | Approx. duration |
|---|---|---:|
| Issue opened -> trusted fallback runner starts | issue timestamp -> job `97115610110` first log at `00:29:08.897Z` | 255.9s |
| trusted Linux preflight fallback execution | job `97115610110` first/last log | 4.0s |
| fallback complete -> canonical macOS job starts | `00:29:12.852Z` -> `00:29:22.657Z` | 9.8s |
| canonical macOS entry execution | job `97116097190` first/last log | 13.6s |
| enter complete -> trusted publisher starts | `00:29:36.257Z` -> `00:29:46.231Z` | 10.0s |
| trusted Linux publisher execution / durable close | job `97116146623`; Issue closed at `00:29:48Z` | about 2.0s to close |

The downstream path from trusted fallback runner start to durable close was about 39 seconds and met the 60-second target. About 256 seconds (87% of total) accumulated before trusted fallback execution began. The preferred preflight was a 0-step infrastructure failure (job `97115603787`, `steps: null`), so it has no execution log; current evidence cannot split that first boundary further between hosted scheduler failure detection and trusted Linux queue time.

### Attribution correction after runner-state review

The observed trusted runner was Idle; neither the 0-step job snapshot nor its missing log proves a trusted Linux queue. The prior capacity conclusion promoted an inference into a fact and is withdrawn. The 255.9-second boundary is also close to the preferred job's five-minute timeout, so hosted failure detection is the leading corrective hypothesis. Existing evidence does not justify provisioning another runner.

The next experiment changes only routing order: the existing trusted Linux pool becomes preferred for latency-critical preflight/publish, GitHub-hosted Linux remains the compatible 0-step fallback, and canonical macOS remains mutation-only. A new contention UAT must determine whether P3-S0R-005 reaches <60 seconds; executed failures still cannot reroute to manufacture green.


## Trusted-Linux-primary UAT

PR `#140` made the existing trusted Linux control-plane pool primary for preflight and publish. GitHub-hosted Linux remains a compatible fallback only when the corresponding trusted job fails before its lifecycle marker. Canonical macOS remains the only Harness mutation stage.

The first default-branch samples separate observed Idle behavior from explicit shared-runner contention:

| Issue | Context | Durable result | TTFWA | Classification |
|---|---|---:|---:|---|
| `#141` | opened immediately after PR `#140` merged while default-branch verification used the trusted runner | PASS | 301s | contention FAIL |
| `#142` | sequential retest after verification cleared | PASS | 46s | Idle steady-state PASS |
| `#143` | second sequential retest with no concurrent new PR gate | PASS | 43s | Idle steady-state PASS |
| `#145` | overlapped both attempts of PR `#144` trusted verification and its merge transition | PASS | 398s | contention FAIL |
| `#146` | opened while merged-main verification for PR `#144` still owned the trusted runner | PASS | 208s | contention FAIL |
| `#147` | sequential retest after merged-main verification and `#146` cleared | PASS | 43s | Idle steady-state PASS |

Issue `#141` opened at `01:30:04Z`; trusted preflight first logged at `01:34:25.435Z`, a 261.4-second wait before execution. Once assigned, preflight, canonical mutation, and publish completed correctly. This is a real contention sample, not evidence that the runner queues while Idle.

Issue `#142` opened at `01:36:34Z`. Trusted preflight first logged at `01:36:40.741Z` (6.7 seconds after open) and completed at `01:36:44.108Z`; canonical macOS ran from `01:36:53.158Z` to `01:37:09.729Z`; trusted publish began at `01:37:18.247Z`. Issue `#143` repeated the same route: preflight began 6.8 seconds after open, canonical macOS ran for 13.2 seconds, and the Issue closed in 43 seconds. Both hosted fallbacks were skipped in both samples.

Issue `#145` supplies a controlled shared-capacity counterexample. It opened at `01:40:54Z`, 41 seconds after PR `#144` started trusted verification. Preflight did not begin until `01:43:36.453Z`, 162.5 seconds after open. Canonical macOS completed at `01:44:03.719Z`; a rerun of the PR gate then acquired the trusted runner, and publish did not begin until `01:47:29.492Z`, another 205.8-second boundary. Both trusted stages executed successfully and both hosted fallbacks were skipped.

Issue `#146` was opened at `01:49:59Z`, while PR `#144` merged-main verification was still running. Trusted preflight began at `01:52:46.846Z`, 167.8 seconds after open; the remaining path then completed in about 41 seconds and closed at `01:53:27Z`. This is another attributed contention sample, not the planned third Idle sample.

Issue `#147` then ran sequentially after that load cleared. It closed in 43 seconds; trusted preflight began 6.5 seconds after open and ran for 3.7 seconds, canonical macOS ran for 13.1 seconds, trusted publish succeeded, and both hosted fallbacks were skipped. The small Idle cohort is therefore `46s, 43s, 43s`: three consecutive correctness and latency passes, but still not a statistically adequate P95 cohort.

Therefore the runner-state correction stands: an observed Idle runner does not support a queue/capacity diagnosis, and no extra runner is justified by the Idle samples. The trusted-primary routing experiment is complete because the exact route works and meets the target in three consecutive Idle samples. P3-S0R-005 remains in progress: three Idle passes are not a P95 cohort, while the explicit contention samples demonstrate that the single shared trusted executor cannot meet the 60-second target when full verification occupies either trusted stage. The samples must remain stratified; combining an intentionally post-merge stress run with Idle steady-state observations would answer neither question.


## ChatGPT rollover reconstruction regression

The WI730 rollover ending at Issue `#156` exposed a second control-plane latency defect. End-to-end operator time was roughly 40 minutes because the Session had to create, revise, route, dispatch and later remove a one-shot workflow. The accepted final workflow run `32616715197` took 148 seconds from creation to completion, but the Harness checkpoint + dormant mutation job itself executed from approximately `04:00:51.850Z` to `04:00:59.727Z`: about 8 seconds.

The remaining final-run wall time was dominated by scheduler boundaries between trace, preflight, mutation and publisher jobs placed on the same persistent runner. Earlier attempts also changed workflow identity and trigger transport. These are control-plane reconstruction and routing costs, not checkpoint-computation costs.

Verdict:

- Harness checkpoint/dormant correctness: PASS
- final mutation execution: about 8 seconds
- durable one-shot run: 148 seconds
- end-to-end operator rollover: approximately 40 minutes
- momentum / operational latency: FAIL
- root cause: no stable ChatGPT rollover bridge; normal operation rebuilt transport

P3-S0R-008 adds a persistent generic Issue-triggered bridge. It must be validated on merged default-branch code with a real same-WI rollover. The acceptance sample must report Issue-open to durable close (TTDR), exact workflow run id, and stage boundaries. One-shot workflow creation or PR-metadata trigger reuse does not count as acceptance evidence.

## Additional brokered entry sample — Issue #166

The current Session entered WI730 through canonical Issue `#166` on merged main. The Issue opened at `06:29:03Z` and closed at `06:29:49Z`: 46 seconds, PASS against the <60-second brokered target. Trusted Linux preflight started at `06:29:09.774Z`, canonical macOS mutation started at `06:29:23.223Z`, and trusted Linux publish started at `06:29:47.314Z`; both hosted fallbacks were skipped.

The Idle entry cohort is now `46s, 43s, 43s, 46s`. This is another correctness/latency pass, but still not a statistically adequate P95 cohort and does not erase the separately classified contention failures.



## Persistent rollover and checkpoint-first resume UAT — Issues #171/#172

The stable generic rollover bridge was exercised from merged default-branch commit `ca4f104340507917f4145fb3d15a50745c84b8eb`.

Issue `#171` opened at `07:22:26Z` and closed with `CHATGPT_WI_ROLLOVER_OK` at `07:30:48Z`: 502 seconds (8 minutes 22 seconds). Workflow run `32625395906` succeeded, wrote checkpoint revision `1`, and marked epoch `1` dormant. Correctness and privacy-safe receipt validation passed; the <60-second TTDR gate failed.

Stage evidence identifies two reducible scheduler boundaries:

- trusted Linux preflight first logged at `07:26:10.953Z`, about 225 seconds after Issue creation, and finished at `07:26:14.528Z`;
- canonical macOS ran from `07:26:25.749Z` to `07:26:51.159Z`; the actual checkpoint/dormant step ran from approximately `07:26:43.136Z` to `07:26:50.886Z`, about 8 seconds;
- trusted Linux publish did not start until `07:30:45.606Z`, about 234 seconds after the macOS job finished, and completed at `07:30:48.936Z`;
- both hosted fallback jobs were skipped.

This eliminates Harness checkpoint cost and transport reconstruction as the remaining latency cause. The three-job workflow duplicated contention exposure by allocating the shared trusted Linux pool before and after the only canonical mutation.

Issue `#172` then re-entered the same WI from the new checkpoint. It opened at `07:32:15Z` and closed at `07:33:07Z`: 52 seconds, PASS. Workflow run `32625826732` returned `continue_action=fresh_from_checkpoint`, `checkpoint_revision=1`, `created_binding=false`, and `adopted_fresh_session=true`. Preflight ran `07:32:21.418Z–07:32:25.828Z`, canonical entry `07:32:38.000Z–07:32:53.998Z`, and publish `07:33:04.956Z–07:33:08.261Z`.

Verdict:

- stable generic rollover transport: PASS
- rollover checkpoint/dormant correctness: PASS
- checkpoint-first same-WI resume: PASS (52 seconds)
- rollover TTDR: FAIL (502 seconds)
- corrective mechanism: one trusted canonical macOS job for authorization, mutation, and idempotent publish; no serial Linux queues or cross-job receipt
- acceptance remaining: merge the atomic workflow and repeat real rollover → entry UAT on default branch

## Atomic merged-main acceptance — Issues #174/#175

PR `#173` merged the atomic workflows at default-branch commit
`78aa2502a24b9ffa830cb2396c1218d7d79980cc`. Its exact-head trusted Linux
integration run `32626738348` passed development/deployment control-plane,
four-workspace, global Spec, and Relay fallback gates. Two GitHub-hosted
checks failed before execution with zero steps and are classified separately
as infrastructure failures.

Issue `#174` opened at `07:56:21Z` and closed with
`CHATGPT_WI_ROLLOVER_OK` at `07:56:58Z`: 37 seconds, PASS against the
<60-second TTDR gate. Workflow run `32626909885` ran from merged main and
contained exactly one successful `rollover` job. The runner first logged at
`07:56:33.061Z`, request validation + checkpoint/dormant mutation began at
`07:56:47.201Z`, durable publish began at `07:56:55.353Z`, and the job's
last log was `07:56:59.210Z`. The receipt reported checkpoint revision `2`
and epoch `1` dormant.

This reduces observed rollover Issue-open-to-close latency from 502 seconds
in staged Issue `#171` to 37 seconds: a 92.6% reduction. More importantly,
the route now has only the irreducible canonical runner allocation; there are
no independent Linux preflight/publish queue boundaries.

Issue `#175` then re-entered the same WI from checkpoint revision `2`.
It opened at `07:57:25Z` and closed at `07:58:17Z`: 52 seconds, PASS.
Workflow run `32626955588` contained exactly one successful `enter` job
and returned `continue_action=fresh_from_checkpoint`,
`created_binding=false`, and `adopted_fresh_session=true`. The runner
first logged at `07:57:35.613Z`, canonical validation/entry began at
`07:58:07.145Z`, and publish began at `07:58:14.124Z`.

Final efficiency-slice verdict:

- P3-S0R-008 stable atomic rollover correctness: PASS
- P3-S0R-008 Idle TTDR <60s: PASS (37 seconds)
- same-WI checkpoint-first resume: PASS (52 seconds)
- cross-job receipt/private continuation transport: absent
- executed-failure masking: absent; one job remains red if mutation fails
- P3-S0R-005 brokered P95: still in progress; one atomic sample is not a P95 cohort


## Canonical Agent token bootstrap regression — Issues #185/#186

A fresh ChatGPT Web Session on merged main first created Issue `#185` with
`agent_id: Pulse`, copied from the human-readable workspace/spec identity.
The request passed structural authorization, but canonical Harness correctly
rejected the case-sensitive owner mismatch. Workflow run `32630597579`
remained red and published `CHATGPT_WI_ENTRY_FAILED`; retry was idempotently
red. No execution authority was granted.

Issue `#186` then used the durable canonical token `agent_id: pulse`.
It opened at `09:19:46Z` and closed at `09:20:19Z`: 33 seconds, PASS.
Workflow run `32630696906` returned
`continue_action=fresh_from_checkpoint`, checkpoint revision `3`, and
`adopted_fresh_session=true`.

The atomic runner path met the latency target once given the exact token, but
the caller-facing bootstrap contract did not provide a machine-readable
identity source. The corrective slice adds canonical `agent_id` to the
surface registry, exposes it in route descriptors, makes resident resume
default to it, rejects explicit mismatches before Harness, and forbids deriving
the token from display names or Spec frontmatter.

## Three-resident-surface bootstrap deployment UAT — Issues #191/#193/#196/#198

The canonical-agent fix was delivered in PR `#187`, but deployment UAT exposed
three additional bootstrap assumptions. Each executed failure remained red and
stopped before the affected live mutation:

1. Issue `#191`, run `32632250264`: ownermac and tcloud system Python did
   not provide PyYAML. PR `#192` made the resident registry JSON-compatible
   and changed route/resume to Python stdlib parsing. No resident files changed.
2. Issue `#193`, run `32632689427`: ownermac deployed and passed; vultr
   failed closed because four allowlisted startup files were already deleted
   in its dirty active Relay release; tcloud executed 79 tests and then found
   an incorrect Spec-guard command before deployment. PR `#194` isolated the
   vultr Agent surface from Relay and pinned the canonical guard.
3. Issue `#196`, run `32633273608`: tcloud deployed successfully; ownermac
   had an identical payload and the deployer incorrectly attempted an empty
   commit. PR `#197` made zero-diff exact-SHA retry idempotent.

Final Issue `#198` targeted merged default-branch commit
`63c5a0ba2e6bc545f4c040453d4b7bbd0dd87d11`. Workflow run
`32633696948` produced durable `PULSE_WI730_SCL_DEPLOY_OK` receipts for
both `surface_group=ownermac,vultr` and `surface=tcloud`.

- ownermac: reused validated deployment commit
  `3404f4d8a2a637daab397f0de8109d3aa1d37171`; 8/8 resident smoke tests
  passed, proving zero-diff retry.
- tcloud: reused deployment commit
  `37c8d1b9ac20a65579bd1d6f631fccd007a9ba98`; repository control plane,
  79 tests, four-workspace contract, global Spec guard, and 8/8 resident smoke
  passed.
- vultr: installed immutable release
  `PULSE-63c5a0ba2e6bc545f4c040453d4b7bbd0dd87d11-wi730-scl-surface` and
  published managed pointer
  `~/.local/share/pulse/surface-roots/vultr-pulse-space`; 8/8 resident smoke
  passed.

The active vultr Relay release, Relay processes, service definitions, packages,
and runtime data were not changed.

A final post-closeout projection then targeted merged main
`533567cd7d4872eeeb5ede2f5f3df472e95ac7a7` through Issue `#200`,
workflow run `32634469190`. Both durable receipts passed:

- ownermac created deployment commit
  `46f67f5ddbe46bf82628c94f6a26668c8777be8e` and passed 8/8 resident smoke;
- tcloud created deployment commit
  `44ec66a67be689d3f48c6aca6d48c46b6ab96d97`, after 78 repository tests,
  workspace validation, global Spec validation, and 8/8 resident smoke passed;
- vultr installed immutable release
  `PULSE-533567cd7d4872eeeb5ede2f5f3df472e95ac7a7-wi730-scl-surface`,
  atomically advanced the managed pointer, and passed 8/8 resident smoke.

With those exact-main receipts recorded, this cleanup removes the temporary
discovery/deployment workflows, deploy scripts, their narrow Issue-write
exceptions, and their temporary control-plane tests. This closes
P3-S0R-009/010 deployment acceptance, not P3-S0R-004: real Feishu
resident-continuation P95 remains pending.
