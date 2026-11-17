# Waiting and activity observations

Status: source implementation and bounded original Linux Pi adoption for
AH-062 and AH-065 / A-29 and A-31; not complete provider or scheduled-scan acceptance.
Exact installation, eight native query results and preservation limits are recorded in
[the native evidence](evidence/rewrite-runtime-presence-native-20261009.json).

`inspect --wi WI --view watch` reads the same authenticated journal transaction
as the normal query. It lists the current execution holder, running attempts,
open collaboration responses, pending current-candidate verification/review,
unfinished external effects, and pending transfer. No additional database,
process registry or lifecycle state is created. Historical queries select only
facts at or before the requested revision and retain current capability checks.

The watch response is compact: `current_observation` retains journal source,
generation and business progress; `result.items` contains the bounded watch
page. Envelope effects are safe summaries only for effects on that page,
explicitly labelled `watch_page`; no snapshot or native dispatch capability is
included. Current capability checks and the actual next action are unchanged.

Each item has a stable logical identity, original recorded status, source
revision and last related recorded event when available. Native session identity,
message content, private paths and credentials are excluded. Where an actual
executor exists, expose only an opaque scoped executor reference and the last
authenticated journal entry from that exact principal. A recorded interaction
is historical evidence, never proof the process is currently alive. Presence
is `unknown` unless the invocation contains the trusted exact-principal runtime
observation described below; no missing process or Relay entry is interpreted as death.

The optional positive integer `--age-threshold-seconds` is a caller-selected
advisory query threshold, not an execution policy or deadline. With a valid UTC
observation clock, elapsed time at least that threshold is `stale`; otherwise it
is `recent`. Missing thresholds, missing/unparseable dates or backward clocks
produce `unknown` with a reason, never a fake zero age. Journal order determines
the latest fact, not the largest wall-clock timestamp. Business overdue is separate from this advisory age. A missing original deadline
produces unknown, not an inferred SLA. Providers beyond the admitted current Linux Pi process and scheduled multi-WI
scanning remain separate implementation work.

Results are stable-sorted and paginated with `after`/`limit`, disclose total count
and next offset, and separate business progress from observation age. Archive
scan only identifies terminal items with recorded resources retired, all effects
settled and no required collaboration outstanding. It grants no deletion,
resource retirement or execution authority; retained originals remain required.
Unknown effects or absent process observations never become successful tasks.

Reads must leave journal bytes, request count, grants, generation and original
effects unchanged. Positive and negative cases cover pending/accepted/rejected
collaboration, unfinished tasks, waiting verifier/reviewer, unknown effects,
completed resources, exact principals, privacy, bounded pagination, historical
reads, revoked scope, clock rollback and unsupported query options. Source tests
and an installed original-native query are recorded separately. This slice does
not implement immediate/aggregate notifications, cleanup or all diagnostics.


## Original deadlines and historical CLI reads

The compatible deadline slice accepts optional `deadline` on an original confirmed
graph node, collaboration offer, or rollover request. It must be an explicit
RFC3339 date-time with seconds, optional at most six fractional digits and an
explicit timezone. Null, naive time, impossible dates and relative durations are
rejected before a new fact. Preserve the exact literal and original source; old
events without the field replay unchanged and have no inferred deadline.

Graph deadlines change only through the existing confirmed plan revision;
candidate verification/review/delivery deadlines come from that exact frozen
candidate's graph gate. Running task deadlines come from their adopted graph.
Collaboration reassignment preserves the original deadline. A changed deadline
requires withdrawing and making a new original offer; no in-place history edit.
Transfer deadlines belong to the original transfer, never to a later timestamp.

Watch reports the deadline with its source kind and exact graph/node, contract
or transfer identity. At or after the deadline it reports overdue and observed
lateness; before it, not_due. Missing/malformed observation clocks, a clock older
than the related recorded event or absent original deadline remain unknown.
Age threshold does not affect overdue. It does not expire grants, invalidate
attempts, auto-reject an offer, complete work or mutate a transfer. A recipient
can still accept a valid overdue offer or transfer through normal authority.
Successful/withdrawn obligations are no longer waiting items.

`inspect --revision N` exposes existing historical query semantics through the
actual CLI, including state and watch. It requires a positive integer and is
rejected for actions other than inspect. Historical data and deadline come from
that exact revision; returned capabilities and next action remain current.
No historical view is materialized over the current disposable state view.
Test exact retries, invalid deadlines, offsets, boundary times, clock rollback,
accepted overdue delegation/transfer, frozen verification/review deadlines and
historical reads that cannot revive previous authority. Source completion and
native adoption are recorded separately.


## Bounded collection scan

`inspect --view scan` reads current observations for the caller's readable WIs
in its authenticated namespace, inside one read transaction. The same optional
status/owner/text filters and after/limit pagination as list apply. It rejects
a specific WI or historical revision, and never attempts implicit native binding
selection. Unsupported action/scan combinations fail instead of silently listing.

Each row retains the normal WI summary plus watch counts and archive scan state.
Counts are computed from all current waiting observations, independent of the
single-WI watch page size: counts by kind, overdue, advisory record age and unique
executors with observed-present or unknown presence. Single-WI watch publishes the same
summary. The collection response uses one observation clock and discloses exact
matching count and next offset, scoped only to readable WIs. No historical
candidates, messages, native identities, paths, dispatch payloads or execution
capabilities are exposed. Corrupt accessible original facts reject the scan;
they cannot be silently omitted to produce a clean result. Inaccessible WIs are
filtered before replay and never contribute to public counts.

This is an on-demand journal scan; it does not install a timer, run platform or
filesystem probes, resolve violations or send notifications. Pagination is
stable for one fact snapshot; a later page is a fresh transaction and does not
claim a cross-call global snapshot. Consumers retain each row's source revision
and observation time and use single-WI watch for bounded details. Unknown inputs
stay unknown. Repeated scans must preserve database bytes and all grants/effects.


Historical replay integrity: the returned revision and chain digest refer to
the selected row itself, while validation still checks the complete current
chain. The internal historical digest must not be the selected row's parent.
This does not alter native query permissions, recorded events or public pages.

## Trusted runtime observations for the current native caller

The trusted Host boundary supplies an invocation-scoped runtime observation. Business JSON cannot supply a PID, presence flag, session identity,
proof, process path or probe command. The first native provider observes only the
exact Pi process already admitted from this invocation's ancestry and original
session. Other executors remain unknown; this is not general process discovery.

At admission retain the process's kernel identity (PID, start ticks, boot identity)
in Host memory. At authentication read that exact process again. A matching stamp
and a non-zombie process can establish observed presence at that observation time.
A missing/inaccessible process, changed stamp, unavailable admission stamp,
malformed metadata or zombie state produces unknown with a reason, never absent,
dead, expired, completed or revoked. Do not scan other PIDs, read credentials,
query message contents again, send signals or write an observation registry.

The Host supplies a typed observation bound to the exact principal digest, with
an explicit timezone-aware observation time, fixed source kind and opaque proof
digest for a positive observation. Watch and collection scan match the complete
principal identity; matching actor alone is insufficient. Invalid or unsupported
observations are unknown. Public output includes only the opaque executor ref,
observed presence/unknown, observation time, fixed reason/source and proof digest;
never expose PID, boot ID, process start ticks, session name or private paths.
Historical watch retains the historical executor but labels runtime evidence as
current observation, not evidence that the executor was alive at that revision.

Capture once per authenticated query, reuse the same snapshot for each matching
executor and collection row, and count unique observed-present and unknown
executors consistently in watch/scan summaries. Journal interaction age and
business overdue stay separate. Runtime observation never changes capabilities,
next action, grants, requests, journal bytes, effects or archive eligibility.
A missing provider retains the existing unknown behavior. Audit mode can observe
but cannot gain execution authority. A delivery worker does not inherit the Pi
principal identity merely because it runs in that process.

Acceptance covers a live admitted native process, missing/inaccessible metadata,
PID reuse, boot change, zombie process, malformed stamp and unknown provider;
exact-principal isolation, public privacy, historical-time labeling, collection
summary consistency and no mutations. Native adoption must use the actual original
Pi watch/scan consumer after formal installation. Source fixtures are not native
liveness proof. Scheduled scans, other-runtime providers and complete AH-062/AH-065
remain separate work.
