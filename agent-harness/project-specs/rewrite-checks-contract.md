# Recorded checks and compliance observations

AH-066 / A-31 require explicit passed, violated and unknown observations, tied
to original rules and evidence, without another lifecycle writer. This slice
assesses a fixed evidence artifact through the existing authenticated query.
It does not run caller-supplied commands, certify human decisions, install a
general rule language, or claim current filesystem/process observations.
Native pre-action probes and action enforcement remain separate unfinished work.

## Boundary and provenance

Use inspect with view checks and an exact artifact reference already captured
in this WI's original journal. CLI uses --artifact-ref FILE containing that
returned reference; the same option enables the existing artifact view.
Reject the option on other views/actions instead of ignoring it. Historical
queries can only read an artifact captured by that revision. Existing WI and
private-artifact access controls apply before parsing any evidence.

Kernel reads and verifies the original bytes in the same transaction as the
WI history. Reports retain source revision, artifact digest/reference and JSON
pointers into the original packet. They never echo text, regexes, commands,
paths, native identities or rule source text. Raw source remains available only
through the existing authorized artifact query. Current authority is retained
even for historical observations. Querying cannot write facts, effects, views
or grants. Report effects is empty and labelled checks_read_only.

The packet has schema harness.check-evidence/v1, action, rules and facts.
Action is a business identifier. Each rule has id, kind, actions, source_ref and
check. Rule IDs must be unique. source_ref identifies the original rule; report
exposes its pointer and digest rather than its text. Rules apply only to listed
actions. An unmatched rule is unknown with applicable=false, never a pass.
Missing or malformed rule fields, unknown kinds or unsupported check settings
produce unknown with a precise reason. No free-form pass condition is executed.

Facts are explicit recorded claims, not independently authenticated observations.
Every used fact needs a nonempty source_ref and evidence=observed. Inferred facts
remain unknown. All reports label evidence_trust=recorded_artifact_claims and
human_authority=false. An observed label in an artifact never grants authority.

## Four fixed checks

- file_exists: check paths and nonempty (boolean, default true); facts.files
  contains path, state present/absent/unknown, bytes and provenance. Duplicate,
  missing, inaccessible or inferred observations are unknown. Explicit absence
  or an observed empty required file violates; known present nonempty passes.
  No live file I/O is inferred from these recorded observations.
- command: check command_id; facts.commands contains id, state completed/
  timeout/unavailable, exit_code and provenance. A recorded completed exit 0
  passes and a nonzero integer violates. Timeout, missing exit or incomplete
  observation is unknown. The query never executes the command under review.
- pattern_match: check field and patterns; facts.fields contains name, values
  (strings) and provenance. Any case-insensitive regex match passes; a complete
  observed field with no match violates. Invalid regex or missing/inferred field
  is unknown. Regex work runs only in a bounded isolated stdlib worker with no
  shell or network and inputs on stdin. Timeout/failure is unknown, never pass.
- temporal_check: check objects; facts.events contains object, version, kind
  confirmed/written, sequence (positive integer) and provenance. Each observed
  write needs an earlier observed confirmation for that exact object/version.
  A confirmation of another artifact or a later confirmation cannot satisfy it.
  Unique explicit sequences are required; timestamps are not guessed. When no
  qualifying confirmation exists, incomplete coverage or relevant inferred
  facts are unknown; complete observed coverage proves a violation.
  No observed writes is unknown. facts.events_complete is an explicit boolean.

For a multi-input rule, a proven violation dominates unknown; otherwise all
inputs must pass. Empty input sets never pass by vacuity. The packet is limited
to 2 MiB, 100 rules and 1000 facts per family; strict JSON rejects duplicate
members and nonfinite values. Pattern length is at most 256, each text at most
65536, at most 32 patterns/values; one worker per report has a two-second budget.
Oversized or malformed envelopes reject with structured input errors. A bad
individual check remains reportable as unknown.

## Results and validation

Output harness.check-report/v1 includes rule count, counts by status, paginated
items, next_offset, the recorded action and immutable evidence provenance.
All counts precede pagination. Each item contains rule id/kind, applicability,
status/reason, rule pointer and evidence pointers. No automatic WI creation,
violation resolution, retry, execution approval or state transition occurs.

Test passing/violated/unknown cases for each kind, wrong-artifact and wrong-order
confirmations, incomplete/inferred evidence, malformed/unknown rules, invalid/
pathological regex, duplicate JSON/IDs, bounds, private/cross-WI/historical access,
original corruption, CLI option compatibility, pagination and no byte mutations.
Source tests, real native adoption, pre-action enforcement, old consumer routing
and A-31 completion must be reported separately.
