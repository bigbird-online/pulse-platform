# WI568 ComputeCost × live SCL calibration — 2026-09-01

## Boundary

This is a read-only compatibility calibration. It does not carry forward an old lifecycle authority, alter code, backfill hidden usage, or claim completion of prospective/S5 acceptance gates. Control-plane correlation identities were filtered from retained evidence.

Calibrated facts:

- PULSE packet baseline: `8e42fefdd7f1bfe52c73566f8ccd2c66f457b3bd`.
- PULSE current default readback: `9d682410004587a4e6a0c066509403fe508e12bd`.
- Installed sealed SCL source: `62faefcbf7ae9d8f84faddc63480583aeb7eeda1`.
- Registered WI worktree head: `89c93c8e2bcfb9933cf03507974423bf867c7ef8`.
- Current execution binding: epoch 3, phase `computecost_program`, runtime `pi`, procurement `unknown`.

The current default branch has advanced since the packet, but the `Bigbird/ComputeCost` tree and the three ComputeCost Specs have identical Git tree/blob identities at the packet baseline, current default, and registered worktree. The registered worktree is clean and owner-only.

## Result

**Overall: incompatible for live prospective measurement; compatible only as an isolated code baseline.**

The default implementation remains fail-closed and privacy-safe in isolated tests, but the live SCL-produced runtime binding cannot be consumed by that revision. Therefore this execution has no durable target open baseline, cannot produce a trustworthy current-epoch seal, and cannot forward-propagate current WI → measurement epoch → outcome → cost.

### Repair update — 2026-09-02

Canonical live SCL remains at active execution epoch 3. A legacy execution projection reports epoch 7, but it is not current lifecycle authority and is excluded from this calibration; no intervening session replacement is retroactively relabeled as rollover.

A local candidate accepts both JavaScript millisecond and Python microsecond UTC `Z` precision without rewriting signed bytes, validates the complete binding and digest, and isolates legacy tests from ambient authority. Binding v2 additionally carries a privacy-safe runtime authority ref: Relay derives it from the planned native session, SCL signs it into the epoch binding, and the Pi host refuses any mismatch. Historical v1 remains read-only compatible but cannot activate a new bound Pi epoch.

Bound session replacement is now fenced at both surfaces. Pi cancels in-process new/resume/fork operations and refuses a bypassed replacement activation; Relay refuses new/reset, runtime replacement, and cross-runtime model replacement while active or pending WI authority exists. Baseline failure requests runtime shutdown, handles subsequent input, and aborts any fallback Agent start.

Harness attribution accepts exactly one host-provided Codex or Pi identity and persists only a runtime-domain lineage hash. The PULSE/Relay candidate emits rollover completion evidence v2 that separates source fencing from Token availability: Pi seal success remains exact WI/epoch evidence, while capture failure is bounded `unavailable`, never zero, and does not block lifecycle rollover. Readback proved the managed SCL authority already matched the registered branch, so no authority rotation was needed: the branch was merged with current GitHub `main` without changing registration bytes, and the matching v2 validator was persisted as a local candidate.

Persisted candidate validation is 188 ComputeCost Python tests, 7 Pi lifecycle tests, 20 SCL resume adapter tests, and 212 focused Relay tests with typecheck, plus compileall and forced Spec drift guard. The refreshed Harness candidate passes 163 lifecycle/canonical tests and 23 attribution/observation/outcome/attention tests. The frozen UAT status records PR #298 as independently reviewed and merged while retaining WI938/WI941 quality as unavailable.

These repairs are not merged or deployed and cannot retroactively create the missing epoch-3 baseline or legitimize prior session replacements. G1, the silent-baseline portion of G2, Relay-side lifecycle/measurement coupling, the SCL-owned prospective boundary, automatic Pi host invocation and producer-schema joins are fixed only at candidate scope; merge, installation and live prospective readback remain pending.

### Canonical measurement boundary and host wiring candidate — 2026-09-03

The source candidates now close G4a at implementation scope without invoking the stale Git execution projection:

- Canonical SCL atomically establishes one append-only prospective measurement epoch from its own current execution
  frontier before an executable entry response returns. The kernel is authority; mode-0600 artifacts outside Git are
  repairable ComputeCost projections.
- `record-native-usage-local` accepts only the attached current host, preserves the first open baseline, validates the
  final Pi snapshot against the same runtime-binding digest and current SCL WI/epoch/phase/runtime, and emits a
  conserving `app_server_epoch_token_seal.v1` delta.
- Relay's reviewed Pi host configuration now injects the exact canonical SCL executable, Harness authority root, kernel
  root and host-owned SCL Session ref. The Pi extension records the baseline through SCL before enabling the first
  business turn and records a naturally settled final snapshot before reporting seal success. Progress snapshots remain
  local observations and cannot overwrite the SCL baseline.
- Failed, malformed or mismatched SCL readback blocks baseline activation; raw Session paths and provider identities are
  not carried into SCL artifacts or user-facing receipts.

Candidate validation on current publication bases passes the complete Harness suite (2770 tests), complete ComputeCost
Python suite (196 tests plus 6 subtests), Pi lifecycle extension suite (8 tests), complete Relay suite (1569 tests plus
1 skipped) and Relay typecheck. A bounded
cross-repository process UAT records a real generated open snapshot and settled final snapshot through the candidate SCL,
then proves the existing ComputeCost reader accepts the normalized 16-Token seal while retaining the absent separate
cache-write dimension as `unavailable`, zero eligible outcomes and no raw native identity. These candidates
are not merged or installed, so they do not retroactively create an epoch-3 baseline, make the current live epoch
eligible, or close the prospective ChatGPT+GitHub/S5 gates.

## Compatible

1. **Scoped product baseline**
   - Current default, packet baseline, and registered worktree have identical ComputeCost and ComputeCost Spec content.
   - The frozen 14-WI cohort implementation is unchanged by later default-branch commits.

2. **Binding shape and authority dimensions**
   - The live binding is parseable JSON with the exact nine `pulse_runtime_usage_binding/v1` fields.
   - WI, Agent, phase, epoch 3, and runtime `pi` agree with the current execution frontier.
   - The binding digest conserves its exact payload.
   - Procurement is explicitly `unknown`; this is correct unknown-preserving behavior and cannot be converted to subscription or API cash.

3. **Isolated ComputeCost contracts**
   - With ambient host binding removed, all 184 Python tests pass.
   - Five Pi lifecycle extension tests pass.
   - A focused 34-test privacy/fail-closed set passes, including raw provider identity omission, counter conservation, tamper rejection, final-turn seal ordering, explicit procurement, and request-cost/actual-cash separation.

4. **Installed SCL identity**
   - The sealed runtime handshake and immutable release readback identify the requested SCL source revision.
   - Installed cost-attribution and execution modules match the current source checkout byte-for-byte; calibration conclusions use installed bytes where the current checkout has advanced.

## Stale

1. **The previous “184 tests PASS” statement is not a live-runtime result**
   - In the actual host environment, the suite is 182 pass / 2 fail because legacy explicit-attribution CLI tests inherit the ambient binding.
   - This does not invalidate their isolated behavior, but it proves the old green result cannot be reused as live compatibility evidence.

2. **Frozen UAT narrative at calibration time**
   - The prior text said PR #298 was awaiting independent re-review and still needed PR/CI/review.
   - The local repair refreshes that status to independently reviewed and merged.
   - WI938/WI941 exact-candidate review evidence remains stale and must continue to project quality as unavailable.

3. **Retired-lineage measurement artifacts**
   - One valid attribution artifact exists for execution epoch 4, while the current local-first execution epoch is 3.
   - One valid accepted task-outcome artifact also exists, but it does not link to that attribution’s measurement epoch.
   - Neither artifact belongs to the current epoch cohort; neither may be imported or counted.

4. **Task SSOT**
   - V2-005 and S5-001 remain marked not started. Existing component tests and old open-prefix UAT do not supersede those states.

## Gaps

### G1 — P0: live timestamp interoperability blocks all Pi measurement

The live producer emits a valid UTC `Z` timestamp with millisecond precision. ComputeCost reparses it and requires exact equality with Python’s canonical rendering, which expands the fraction to microseconds. The digest is valid, but `load_runtime_usage_binding()` rejects the binding as non-canonical.

Observed consequences:

- direct live binding validation fails closed;
- the live Pi probe exits non-zero;
- the snapshot directory contains no current artifact;
- the `session_start` hook catches and suppresses baseline failure, so business execution continues without the required pre-turn baseline.

This is a producer/consumer canonicalization mismatch, not malformed authority.

### G2 — P0: no current target baseline or source usage seal

Epoch 3 has no durable open baseline. Without it, a later sealed snapshot cannot prove a complete epoch boundary.

The local-first SCL effect named `runtime_source_usage_seal` only proves that the replaced source is fenced in the lifecycle kernel. It does not read or validate a ComputeCost Token artifact. That fence receipt must not be described as a usage seal.

The legacy v1 rollover completion model requires a Pi source artifact and target binding readback, while the local-first effect path does not currently supply equivalent Token evidence. The candidate v2 contract keeps target authority readback and source fencing as lifecycle requirements but records missing Token capture separately as `unavailable`; such an epoch is not ComputeCost measurement/cohort eligible.

Prior same-WI session replacements did not traverse Harness rollover. They cannot be relabeled after the fact. The candidate closes the known Pi-native and Relay command bypasses and binds each future Pi epoch to one privacy-safe native-session authority; planned replacement must use rollover and failed-source replacement must use recovery.

### G3 — P0: live SCL producer schemas and ComputeCost schemas are disconnected

Installed SCL publishes or validates:

- `work_item_cost_attribution/v1`;
- three normalized App Server observation/seal schemas;
- `work_item_task_outcome_receipt/v1`;
- `s1_human_attention_evidence/v1`.

The original implementation contains no consumer for any of those exact schemas. The local candidate adds an automatic, privacy-safe read-side join for attribution, final Token seals, linked task outcomes, and human-attention evidence under `--harness-root` or the discovered canonical `.harness`. It does not manufacture the cache-write dimension absent from the SCL normalized Token shape, so such usage remains explicitly partial.

A live read-only candidate query finds one attribution, no seal, no linked outcome, and no attention evidence; the existing outcome is excluded as unlinked. Eligible epoch count is zero and actual cash remains unavailable. Outcome references remain under their producer’s domain rather than being relabeled through ComputeCost continuation-shadow identities.

### G4 — P0: current Pi cannot open a Harness measurement epoch

The installed SCL attribution producer obtains lineage from a Codex-only host signal. The current runtime is Pi, so it cannot prospectively create a current-epoch attribution through that path. Source candidates now derive a privacy-safe lineage from canonical SCL and wire Pi baseline/final recording automatically, but the installed runtime remains unchanged. Therefore no current-epoch attribution exists in live SCL.

### G4a — P0: automatic producer invocation has no authoritative write path

The 2026-09-02 invocation audit found:

- SCL entry, continue and rollover apply do not invoke `CostAttributionManager` or the normalized observation writers.
- Those managers authorize against the legacy Git execution binding, which currently projects epoch 7 while canonical live SCL remains epoch 3. Calling them from the live SCL path would bind evidence to the wrong authority, not repair the divergence.
- Relay creates a target runtime-usage binding and can read back a privacy-safe native Pi seal, but the binding has no SCL measurement-epoch ref and the native seal is not an `app_server_epoch_token_seal.v1` fact.
- Task outcome and attention evidence remain explicit producer actions; neither may be inferred from rollover success or conversational traffic.

Therefore automatic invocation could not be completed by adding a subprocess call into the legacy managers. The source candidate instead derives epoch and phase from canonical SCL, assigns one prospective measurement epoch before the first business turn, and durably adapts native baseline/final evidence without consulting the stale legacy binding. Outcome and attention remain independently evidenced joins. Deployment and live readback are still pending.

### G5 — P1: no complete current cohort

Current safe inventory:

- current-epoch attribution: 0;
- normalized observations: 0;
- linked current-epoch task outcomes: 0;
- human-attention evidence: 0;
- current Pi baseline/seal artifacts: 0.

Thus productive/recovery/verification/adjudication costs, quality, effect, efficiency, human attention, actual cash, and ROI remain unavailable for this epoch.

### G6 — P1: privacy-safe user projection is not uniform

ComputeCost artifacts omit raw provider identities and paths. SCL readbacks can still carry private control-plane correlation fields. They are operational joins, not user-facing ComputeCost data, and must be filtered from reports and conversational output.

### G7 — acceptance gates remain open

- No ChatGPT+GitHub prospective full-coverage epoch is established by this cloud-Pi run.
- S5-001 still lacks a legal cohort, seven elapsed days, ten eligible outcomes, a real observation window, outcome gate, and measurement completeness.
- No historical hidden Token backfill or manufactured downstream workload is allowed to close these gaps.

## Remediation order

1. **Unify binding timestamp semantics.** Define canonical UTC `Z` timestamps to accept producer millisecond precision without changing signed payload bytes. Add a Python regression using a live-style millisecond timestamp and a cross-language fixture generated by the TypeScript producer.
2. **Make baseline failure a pre-turn activation failure.** Do not swallow a missing/mismatched initial Pi baseline. Relay/runtime activation must read back a valid epoch- and digest-bound open snapshot before the first business turn.
3. **Isolate legacy CLI tests from ambient authority.** Explicit-attribution tests must clear host binding variables; add a separate live-host test proving ambient authority wins and conflicts fail closed.
4. **Separate source fencing from usage sealing — local Relay and SCL candidates completed.** Lifecycle replacement requires source retirement, continuation consume/apply, target readback, and binding migration. A verified Pi Token artifact controls ComputeCost measurement/cohort eligibility instead of ordinary rollover; missing capture is bounded `unavailable`, never `not_applicable` or zero. Merge, runtime deployment and prospective UAT remain open.
5. **Choose one producer contract and adapter boundary — source candidates completed.** ComputeCost validates and joins SCL attribution, final seal, outcome and attention; canonical SCL now owns prospective attribution and native Pi seal adaptation. Cache-write and other absent fields remain unavailable; deployment and current-epoch evidence are still pending.
6. **Add Pi lineage attribution and host invocation — source candidates completed.** SCL domain-hashes the logical source, host aliases retain one epoch, and Pi records baseline/final evidence through the canonical local client. Installed runtime integration remains pending.
7. **Unify task/outcome reference derivation.** One domain-separated definition must propagate WI → measurement epoch → task → outcome → usage/cost. Migration must not relabel retired artifacts as current.
8. **Refresh only stale UAT status text — completed in the local candidate.** The 14-WI numbers and unavailable fields are preserved; PR #298 merge/review status is current without asserting WI938/WI941 quality completion.
9. **Re-run evidence in order.** Cross-language contract tests → clean 185 suite → live v2 Pi baseline → naturally settled seal → real rollover with independent lifecycle/measurement readback → zero-manual-join report → ChatGPT+GitHub prospective epoch → four-surface seal/UAT → S5 seven-day/ten-outcome gate.

## Acceptance impact

No acceptance criterion is closed by this calibration. The prior merged defect fixes and frozen 14-WI totals remain valid scoped evidence, but live prospective measurement, current outcome/cost propagation, ChatGPT+GitHub coverage, S5-001, four-surface final seal, independent Outcome UAT, smoke, cleanup, and terminal closeout remain open.
