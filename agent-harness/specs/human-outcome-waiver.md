# Exact-candidate human waiver of independent Outcome UAT

Tracking: PULSE WI282, issue #961. This source change does not authorize a waiver
for any real WorkItem. Independent, accepted canonical Outcome UAT remains the
default in review and completion. Missing verification and ordinary human
confirmation never select a waiver.

An explicit approved `review record` may accept the candidate and waive only
independent UAT in the same action:

```text
harness review record <synthetic-wi> --decision approved --reviewer "Named Human"
  --repo-head <repo>=<full-revision> --evidence-ref <review-evidence>
  --human-confirmed --waive-independent-outcome
  --expected-candidate sha256:<exact-current-candidate>
  --decision-source-ref <repository-relative-human-instruction>
  --waiver-reason "Substantive reason for waiving independent UAT"
  --accepted-risk "Specific risk consciously accepted by the named human"
```

This is the **existing protocol-only human action boundary**, extended with
explicit waiver fields. `--human-confirmed` attests to a direct human action;
the command does not cryptographically authenticate a person. Generic/service
reviewer identities and autonomous cron review writes are rejected. The reason
and each accepted risk must be nonblank and bounded to 2000 characters.
No minimum prose length is imposed: short meaningful human decisions are valid;
semantic sufficiency belongs to the human decision, not character counting.

`work_item_review/v2` adds one typed `outcome_waiver` to the existing review.
The outer review retains reviewer, timestamp, evidence references, acceptance
fingerprint, content-bound artifact fingerprint, candidate fingerprint and repo
revisions. The waiver pins the exact expected candidate, frozen claim/producer/
change type/acceptance, submitted artifact references, submission/withdrawal
event digest, human instruction reference and content digest, reason, risks,
and the original independent receipt status/digest and evidence bundle digests.
Absent receipts are recorded as `missing` with a null digest. Existing valid
`blocked`, `partial`, `rejected`, and `accepted` receipts retain their bytes and
status. No receipt or Outcome acceptance event is created or rewritten.

Existing `work_item_review/v1` and `phase2_settlement_bundle/v1` records parse
and serialize with their original fields; no null waiver fields are added.
Waiver bundles use `phase2_settlement_bundle/v2` and bind the actual Outcome
authority separately from the nullable original receipt digest. Review
replacement events retain the prior review as well as the new explicit decision.
The command envelope captures human instruction bytes for the existing Broker
projection; it keeps their repository path and does not interpret reason/risk
strings as file arguments. New transported instruction files must reside under
`.harness/evidence`; other source references must already exist unchanged in
the canonical projection. The Broker rejects a source overwrite or symlink.

`validate_terminal_outcome` is the shared, narrow decision interpreter used by
review, final freeze authority, settlement publication, CLI completion/replay,
and delivered terminal readback. Audit data distinguishes `independent_outcome`
from `human_waiver`, retaining `independent_outcome_status`. Waiver completion
passes a bound decision to the state machine with `outcome_receipt_validated`
false. A waiver never supplies independent acceptance. An actually accepted
canonical receipt remains visible and permits a later explicit return to default
independent review without reinitialization. Original independent evidence and
historical approvals remain audit material.

All other gates remain: exact human approval, SCL final freeze, declared consumer
convergence, source integration/ancestry, exact-head CI, settlement/publication,
execution retirement, handoff preconditions and resource cleanup. Changing the
reason, accepted risks, source bytes, receipt/bundle, claim, acceptance, artifact
bytes/set or repo revisions requires a new explicit approval and invalidates
old freeze/bundle use. An identical request returns its existing review and does
not emit another event.

Revocation uses existing `review record --decision changes_requested` without
waiver flags. An existing approved waiver can be revoked even when independent
UAT is absent or nonaccepted. This replaces the review, returns the candidate to
execution through the existing transition, and clears its frozen claim. The
submission/withdrawal digest prevents an old waiver from reviving after an
identical resubmission. A final-frozen SCL candidate still requires the existing
explicit withdrawal before another final review can be frozen; no reopen or
status-reset path is added. Terminal review writes remain prohibited.

Completion retains the original submitted artifact set separately from the
exact terminal additions and their content digests recorded by its unique
completion event. Replay hashes every original artifact again, allows only those
recorded additions, validates each addition's bytes, and checks
the exact review, instruction, independent evidence and terminal receipt digests.
Arbitrary added artifacts are not ignored. Existing independent completion
replay semantics remain unchanged.

Supported boundary: current `awaiting_verification` candidates in the existing
standard human review/terminal pipeline. Existing receipts must be contract-valid
and their check evidence must use a captured canonical bundle. Malformed receipts,
external mutable evidence references, unsafe/symlink sources, cron waivers,
noncandidates, and new terminal waivers are rejected. The historical v1 terminal
reconciliation repair protocol remains independent-accepted-only; no waived
history repair or new waiver/revocation manager is introduced. Lightweight
completion is unchanged. This implementation supplies no deployment, installed
consumer adoption, independent business verdict or production readiness proof.

A structurally valid receipt for the same WI may describe a prior submitted
version. Waiver observation validates that retained receipt against its own
declared claim/producer/change-type/acceptance and captures its unchanged bundle.
It must not require the old result to describe a newly approved candidate. The
waiver still pins the current candidate independently, and default independent
acceptance still requires a matching current receipt. A retained historical
accepted status is historical evidence, not current independent acceptance.
Wrong-WI, malformed or tampered historical material remains rejected.
