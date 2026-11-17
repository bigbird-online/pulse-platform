# PULSE Stats global-authority carrier

> schema: `pulse_stats_global_authority_evidence/v1`
> publisher: agent-harness SCL
> consumer: PULSE Stats exporter
> status: native scheduled publisher contract
> modified: 2026-10-11

## Boundary

Stats is a read-only consumer. It does not scan, merge, repair, or publish Harness
ledgers. The SCL publisher accepts one independently produced, versioned
`pulse_stats_global_authority_materialization/v1` receipt for each of the four
publication endpoints below, registered in `pulse_scl_surface_registry/v3`:

1. `surface_v1_local_macos`
2. `surface_v1_cloud_linux_a`
3. `surface_v1_cloud_linux_b`
4. `surface_v1_chatgpt_github`

The registry also enrolls execution surfaces such as Muse. Enrollment does not
extend this v1 publication protocol. Producer, durable query and Stats consumer
must require the same four endpoint identities in this order; an additional
valid, unique registry row cannot substitute for any of them. The complete
registry remains unchanged and must still pass identity validation. This
carrier proves the canonical read model at these four publication endpoints;
it does not prove materialization, health or coverage at Muse or other execution
surfaces. Adding a publication endpoint requires changing the protocol and its
real scheduled producer and consumers together.

`github_authoritative_convergence_receipt/v1` is not a substitute: it proves Git
endpoint convergence, not that all four publication endpoints materialized the
same canonical Harness read model.

## Canonical read-model materialization

The SCL producer runs `harness stats-authority project` against the canonical
Harness root, its repository slug, and an externally proven exact Git SHA. The
command derives the `sha256(repository@full_sha)` authority revision itself;
callers cannot inject an opaque digest that the Stats consumer cannot bind back
to its actual service checkout. The
projection whitelists only typed ledger facts; malformed YAML is represented as
an opaque `invalid_record` identity instead of being silently dropped or
leaking parser/path details. It never derives authority from its checkout path
or calls a local projection global by itself.

Each surface then runs `harness stats-authority materialize` against the same
`pulse_stats_canonical_read_model/v2` artifact and the versioned surface
registry. The artifact carries a `sha256:` authority revision, source
`observed_at`/`expires_at`, and a sorted list of privacy-safe facts. Facts expose
only typed identity, lifecycle, owner, execution, epoch, phase, surface and
relationship fields; titles, acceptance text, reasons, paths, session refs,
attachments and transcripts are outside the schema.

The materializer:

- rejects a surface outside the four publication endpoints, even when enrolled
  for execution, rather than inferring publication authority from host or path;
- rejects unsupported, duplicated or unsorted facts and non-slug values;
- hashes canonical JSON facts so endpoint paths and observation time cannot
  alter the materialization digest;
- refuses a stale source and caps each receipt expiry at the earlier of source
  expiry and its bounded local TTL;
- atomically writes a mode `0600`
  `pulse_stats_global_authority_materialization/v1` receipt.

This contract separates the canonical global projection from per-surface
runtime evidence: the projection digest must be identical at all four ends,
while the receipt's endpoint identity and observation time remain local facts.

## Publication gate

`harness stats-authority publish` writes the carrier only when all of the
following are true:

- every registry row has a unique, safe surface ID, and its four publication
  endpoint rows are all present in the defined order;
- receipts cover those four IDs exactly once and in the defined order;
- every receipt has the supported schema and a `sha256:<64hex>` authority
  revision and materialization digest;
- every receipt is fresh at publisher time;
- all receipts name one authority revision and one materialization digest.

The aggregate `observed_at` is the latest endpoint observation and `expires_at`
is the earliest endpoint expiry. Publication is atomic and mode `0600`. Invalid,
missing, stale, duplicated, conflicting, or unreadable inputs fail closed and do
not overwrite the last known-good carrier; that carrier will naturally expire
at the Stats consumer.

The same publication also persists the exact
`pulse_stats_canonical_read_model/v2` whose canonical facts digest matches all
four receipts. The durable query model is not a second authority: consumers
must validate it against the carrier revision, digest and TTL on every query.
A crash or partial write that leaves the two artifacts mismatched therefore
fails closed instead of exposing a plausible but unproven WI list.

## Agent work-item query

`harness stats-authority wi get` and `harness stats-authority wi list` consume
only the durable carrier and its proven query model. They never discover or scan
a local `.harness` root. The list command supports exact status filters,
`--terminal`, `--created-since`, and `--updated-since`; terminal means the
canonical five-state set `completed`, `failed`, `cancelled`, `superseded`, and
`migrated`. Bare dates are inclusive UTC-midnight cutoffs; callers needing a
local-day boundary provide an explicit timezone-aware timestamp.

Each work-item fact includes privacy-safe `created_at`, `updated_at`, and the
derived terminal flag in addition to its identity, owner, state, executor and
successor references. The CLI returns a versioned JSON envelope with authority
revision, observation/expiry, normalized filters, count, and `coverage=complete`.
It exits nonzero when the carrier or model is missing, stale, malformed,
revision/digest-conflicted, or contains an invalid work-item record.
`coverage=complete` describes the four-end canonical read-model proof, not
coverage of every execution surface enrolled in the registry.

## Scheduled refresh and source binding

The service-owned `com.pulse.stats-authority-refresh-trigger` launchd job now invokes Oikos `runner-stats-service.py` with its versioned `oikos_stats_authority_service/v1` configuration. It schedules the existing `agent-harness/scripts/stats-authority-refresh.sh` protocol without occupying a GitHub Actions runner. The retired CollaborateWithAI Actions publisher must be disabled or removed and have no unfinished runs before the native single writer starts. Program adoption remains a reviewed GitHub change; scheduled read-side publication does not mutate or converge authority checkouts.

The registered authority repository is `bigbird-online/PULSE`. The service reads its exact installed authority commit, verifies that commit with GitHub, and obtains the publisher from that commit's `agent-harness` tree. Cache reuse verifies source and interpreter identity. The publication script derives all authority roots and four surface IDs from `config/scl-surfaces.yaml`, requires the three service checkouts to agree, and checks current-default authority-input equivalence before publication. Unrelated default-branch code changes do not alone invalidate otherwise identical ledger/publisher/registry inputs. Per-surface runtime bindings remain separate evidence.

Stats preparation and refresh use the business account's sole Coevolution Python environment at `$HOME/.local/share/coevolution/python-env`. The local service and remote profile/materialize/publish commands execute its interpreter directly; they never install packages or create `.venv` directories. The exact archived Harness source declares the required dependencies; the canonical launcher verifies the actual `sys.prefix` and installed requirement versions before importing that source under isolated Python. Missing/incompatible dependencies fail before publication and are repaired through Coevolution `ssot/toolchain`, not through per-run installation. Source tree, endpoint, single-writer, final authority and receipt checks remain required. Rollback preserves the prior program/source receipt and requires a compatible shared environment; an old private environment is retired only after its consumers and rollback obligations are discharged.

The PULSE consumer independently binds the carrier to the registered PULSE Harness service checkout and its `sha256(repository@full_sha)` authority revision. Wrong source, digest or expiry still fails closed. Carrier and read-model publication, installed exporter consumption and the actual App cache must be checked separately; refreshing only the producer does not satisfy App freshness.

A successful publisher receipt proves carrier publication and readback only. PULSE merge,
install/runtime adoption, visible Stats UAT and business Outcome remain separate
gates.

`@anchor symbol publish_stats_authority_carrier`
`@anchor symbol project_stats_canonical_read_model`
`@anchor symbol materialize_stats_authority_read_model`
`@anchor command harness stats-authority project`
`@anchor command harness stats-authority materialize`
`@anchor command harness stats-authority publish`
`@anchor command harness stats-authority wi get`
`@anchor command harness stats-authority wi list`
`@anchor test agent-harness/tests/test_stats_authority.py`
