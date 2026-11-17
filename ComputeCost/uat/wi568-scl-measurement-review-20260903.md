---
experiment_date: "2026-09-03"
target: "CollaborateWithAI#281 + PULSE#347"
focus_question: "Can concurrency, restart, alias, stale binding, or cross-WI input produce the wrong baseline or seal after current-main integration?"
primary_runtime: "gpt-5.6-sol"
reviewer_model: "gpt-5.4 requested; unavailable"
why_different: "A different model generation was selected to challenge the primary implementation framework."
review_classification: internal_second_pass
channel_used: deferred
source_verified: false
findings_total: 4
findings_effective: 3
findings_weak: 1
findings_deferred: 1
noise_filtered: 0
---

# WI568 canonical SCL measurement review

## Review boundary

The external Codex channel failed authentication before producing content, and no different-vendor API channel was
available. This record therefore does not claim independent or adversarial review. It records a current-runtime second
pass over the clean publication candidates and the corrections made while rebasing onto current main.

## Findings

1. **Valid, blocking, fixed — current-main Host Session derivation could be overwritten.**
   - Location: `Bigbird/Relay/src/pi-runtime-config.ts`, `buildPiCommand`.
   - Trigger: the pre-rebase candidate copied ambient `SCL_SESSION_REF` after current main had already derived the
     target Pi Host ref from the native runtime identity.
   - Consequence: when ambient and target refs differed, the canonical SCL writer rejected a valid baseline, or a
     caller addressed the wrong attachment.
   - Correction: retain current main's `relayHostSessionRef(config.sessionId)` assignment and inject only the reviewed
     SCL executable and state roots. The complete current-base Relay suite now passes.
   - Falsifier: spawn a WI-bound Pi while ambient `SCL_SESSION_REF` differs and assert the child receives the ref
     derived from its own native target identity.

2. **Valid, blocking, fixed — a Host alias could split one execution epoch.**
   - Location: `SCLMeasurementManager.ensure_epoch`.
   - Trigger: one logical SCL source is rebound to a replacement Host alias without advancing the execution epoch.
   - Consequence: keying measurement identity by the current Host alias rejects a legal continuation or creates two
     cost lineages for one execution epoch.
   - Correction: key the epoch and lineage by canonical `logical_source_ref`, while still requiring the invoking Host
     to be the current attachment. The full Harness suite covers alias continuation.
   - Falsifier: attach a second Host alias to the same logical source and prove the measurement ref and attribution
     digest remain unchanged.

3. **Valid, fixed — progress snapshots could overwrite or conflict with the prospective baseline.**
   - Location: `capturePiUsageSnapshot` and `installPiUsageLifecycle`.
   - Trigger: `agent_settled` reused the same open artifact role as `session_start`.
   - Consequence: a post-turn cumulative snapshot could replace the pre-turn file or conflict with the immutable SCL
     baseline during recovery.
   - Correction: persist separate `baseline`, `progress`, and `final` roles; only baseline and final cross the SCL
     write boundary. Baseline retry reuses identical durable bytes.
   - Falsifier: run baseline, one settled progress capture, restart, and final capture; assert SCL baseline digest is
     unchanged and the final delta uses the original baseline.

4. **Weak valid, deferred — same-account authority is not cryptographic producer attestation.**
   - Location: `record-native-usage-local` authority check.
   - Trigger: a process with the current attached Host ref and access to the canonical client submits a structurally
     valid fabricated snapshot.
   - Consequence: SCL can prove current WI/epoch/phase/runtime ownership and byte immutability, but not that native
     counters originated from an independently attested provider process.
   - Disposition: non-blocking for the declared single-owner active-executor trust boundary; blocking for any future
     claim of adversarial tamper resistance. Reports must describe facts as host-adapter validated, not cryptographically
     provider-attested. A future cross-OS-account producer or one-time capability handoff should close this boundary
     before broadening the threat model.
   - Falsifier: run the producer under a separate authority principal and prove an active Agent subprocess cannot
     obtain or replay its capability.

## Verdict

The clean candidates are acceptable for source merge under the current single-owner trust boundary after findings 1–3.
They are not independently reviewed, not deployed, and do not make the current epoch or any S5 cohort eligible. Finding
4 remains an explicit threat-model limitation rather than evidence availability.
