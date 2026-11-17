# Measurement, attention and quality contract

Status: source observation capability implemented for AH-069/070/071 and A-32. Native adoption and
existing-consumer replacement must be evidenced separately. This is a query and
observation capability of the same WI journal, never another completion authority.

1. Native usage is captured by the authenticated Host from an explicit byte range
   of its own original session file. A business input cannot name a provider log,
   replace a Session identity, submit token/cost counters, or claim an unavailable
   field is zero. The Host captures fixed original bytes privately, validates whole
   JSONL boundaries and known usage fields, and binds extraction to this command.
2. An execution interval is opened at the Host-observed current complete byte
   boundary of the actual native file. Caller-supplied historical boundaries cannot
   open an interval. Task completion/abandon and owner-side completion, stop,
   rollover or sealing close that caller's intervals at the Host's observed upper
   byte bound. Native business preparation uses the fixed file extent observed on
   the descriptor already opened for mandatory header authentication; it does not
   call the usage scanner or wait for collection. This upper bound can include an
   incomplete last row, but capture still accepts only complete records inside it.
   Only explicit interval opening requires the current complete-row cursor.
   Missing ending observations, or a bound before the interval start or any saved
   usage end, close attribution as unavailable (`not_observed` or
   `source_moved_backwards`). They do not reject the authorized business transition.
   Existing originals and counters remain intact; an unknown end does not authorize
   new captures. Native identity, human-source, grant and journal-integrity failures
   remain hard refusals. Captures outside the observed interval are rejected.
   Each usage record binds WI, native principal, generation, optional actual task
   attempt, byte interval and observed runtime/model configuration. Task attribution
   requires that actual caller executed the named attempt. Source identity and
   extraction range remain stable after session append. Conflicting or overlapping
   attribution for the same native principal across WIs in the same fact-store
   namespace is atomically rejected; retry the original request for
   an already-recorded interval. No aggregate cost is invented across currencies.
3. Missing native usage is unavailable, partial fields remain null/partial, and zero
   is reported only for an explicit actual observed zero. Separate per-model groups
   preserve configuration changes. A malformed observed counter or unknown usage
   schema fails capture without a committed observation. Truncated final records are
   not consumed; a later call can capture their completed original bytes.
   Pi 0.87.1 detail counters `reasoning` and `cacheWrite1h` are exact optional
   native observations. They are never added again to output, cacheWrite or
   totalTokens. Missing detail counters are not inferred as zero. A range with
   these fields uses extraction v2; ranges without them retain the original v1
   packet bytes and retry identity. Malformed native schema is a source error,
   not an allegation that a valid business command supplied unknown fields.
4. A usage snapshot records extracted facts and the digest of its privately held
   original range. The raw range is never published to the production Git ledger,
   whether plain, base64 encoded or compressed. See the
   [production storage contract](rewrite-production-storage.md). Public queries expose aggregate counters, coverage,
   opaque scoped session, exact interval and evidence digest, never native provider
   identifiers, full messages, credentials or raw paths. Historical records remain
   readable after completion and migration; observing usage does not grant work.
5. Human attention records require a real source bound to the exact intent. They
   distinguish original event count, explicitly self-reported duration and category
   from model-inferred labels. Missing duration stays unavailable. Duplicate source
   events cannot be counted twice under another request or task. These observations
   neither edit a review nor substitute for its original human decision.
6. A quality observation binds an immutable candidate, its actual assigned verifier,
   rubric/version, explicit score/coverage and captured evidence. It cannot change
   the candidate's independent verdict or review. Conflicting observations for the
   same candidate/verifier/rubric/version are rejected, not overwritten. Task/WI
   Outcome views derive from the already-admitted original verification/review and
   terminal facts; no duplicate completion receipt is generated from a cache.
7. Usage/attention admission is limited to the current native holder, an active
   scoped executor attributing its own task, or the terminal responsible native
   owner. Quality uses the exact verifier assignment. Sealed sources remain sealed.
   Existing authorization, namespace and private artifact rules continue to apply.
8. Queries and exports declare journal revision, covered observations and missing
   portions. Consumers read the same canonical query; subsequent transport and
   actual consumer adoption require explicit effects/readback. A JSON file or a
   successful source test is not a production consumer adoption receipt.

Acceptance uses real bounded session files with explicit positive, zero, missing,
malformed and truncated records; append/retry, concurrent overlapping attribution,
wrong actor/task/session, duplicate human event, wrong verifier, conflicting quality,
old candidates, completed WI, migration and projection loss. Native Muse capture
and actual consumer compatibility are separate required results.

## Private saved-source projection

`inspect --wi WI --view artifact --artifact-ref reference.json` keeps its existing
private-artifact authorization and original byte response. For a private
`native/usage/range` reference it additionally returns `native_usage_sources`,
one entry per exact recorded use of that reference. No event or grant is created.
Each `harness.saved-usage-source/v1` entry is either:

- `verified`: `record_id,interval_id,session,actor,source_sha256` match the public
  usage record, `native_header` is the saved original Host session header, and
  `interval_source_sha256` identifies the interval-opening source bytes;
- `unavailable`: only schema, record_id, status and
  reason=`original_interval_header_unavailable`, without a native header.

Verification uses the exact interval-opening journal entry, its `native_agent`
original, content digest, opening command/WI/task and original native principal.
The header identity must reproduce that principal's session binding. A missing or
unsupported source cannot be inferred from the current owner, another interval
or another session. The raw session ID remains private and is not added to the
measurements view. Original session files/processes are not reread, and the query
does not mutate the journal. Both the saved range and its interval-opening source
use the backend's original reader: production v2 resolves private custody, not
only the transaction's SQLite cache. Missing/corrupt custody returns the backend's
evidence error without revoking saved decisions or execution authority. All
existing namespace and original integrity checks apply.

The ComputeCost `import-scl-usage` candidate consumes this proof only through the
installed read entry, after admitting the current parent's registered ledger.
It imports the exact saved range through its existing Pi parser and usage
reconciliation, never an extra aggregate charge. A reader without this extension
can only support the original current principal, not cross-session recovery.
This source contract does not prove installation or production consumer adoption.

## Original outcome record times

The existing authenticated measurements query additionally returns
`result.outcome_record_times`, with `candidate_id,kind,record_id,recorded_at,revision`.
Kinds are verification, review, waiver and quality. Times and revisions come from
exact journal entries, not query/ingestion time; no original human or verifier
text is exposed. The existing closed public measurement payload is unchanged.
This is a read projection, not a new decision, grant or lifecycle operation.

ComputeCost's candidate-only `import-scl-assessments` joins these times to the
existing candidate content and measurement records through the installed read
boundary. It validates exact candidate content digests, decision/assignment and
artifact references. Missing times, duplicate identities, inconsistent references
or future observations fail without a partial save. An offline JSON file is not
an authenticated source. The original ledger's observation identity makes repeat
imports idempotent; imported records never confer execution or approval rights.

Exact single-artifact or full-artifact-set references can connect a stage version
to these observations. Intermediate, terminal and unknown candidate scopes remain
distinct; approval of an old version does not approve a new version or confer
separate acceptance on each component. Verification, human review, quality scores
and explicit waiver/revocation remain separate observations with original times,
criteria, evidence and order. No complete/current acceptance total is inferred.
This source contract does not prove consumer installation, Stats acceptance
projection, lightweight completion coverage or natural UAT.

## Existing offline ComputeCost reader

The existing `pulse-compute-cost wi-report WI` entry accepts an explicit
`--harness-query-snapshot FILE` from the installed native
`inspect --wi WI --view measurements` result. This selects one replacement-core
source, before legacy source discovery. Conflicting explicit legacy measurement,
collaboration or pricing sources are rejected. No legacy epoch, seal, attention
unit or lifecycle receipt is synthesized for the opaque new WI identity.
The versioned result is `computecost_harness_query_report/v1`, not the legacy
report schema. This is a narrow read-side adapter; ComputeCost does not import
Harness code, open its database, read provider transcripts, or write WI state.

Admission validates response schema, exact requested WI, successful read
capability and consistent journal revision/generation, measurement source and
coverage, original interval membership, bounded non-overlapping native ranges,
unique observation IDs, and numeric/counter coverage. Unknown/malformed shapes,
duplicate JSON members, mismatched observations and private native identity
fields in the consumed measurement view are errors, not empty reports. The snapshot is an explicitly supplied
observation: file digest and source revision are reported, while current source
liveness and global freshness remain unknown without a new query. A JSON file
does not become independent proof of authoritative origin.

The report preserves per-configuration token groups and distinguishes observed
partial sums from complete coverage. Reasoning/cache detail is not added again
to reported totals. A missing detail remains unavailable. Costs remain separate
by their observed currency; no currency, price, exchange rate or billing amount
is inferred. Coverage is only the original recorded intervals, never the whole
WI lifetime. Human event count and explicit self-reported duration remain
separate; missing durations are unavailable. Quality and candidate outcomes are
observations of original records, not new verdicts, approvals or completion.
Report output contains no messages, native provider/session identity, paths or
credentials. Unrelated query envelope sections are not copied into the report;
the original private query can contain caller-scoped dispatch details. A previously
recorded bounded range remains readable if its interval later closes with an
unavailable final boundary; that uncertainty is retained, not converted into
permission for a new capture. Consumer tests must use the new independent core's real query
and malformed/conflicting cases; the original bounded Muse query must also be
read through the actual ComputeCost CLI. That limited adoption does not prove
production installation, automatic publication or other Stats consumers.
