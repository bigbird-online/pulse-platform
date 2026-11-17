# Harness Platform Epoch Execution Quarantine

Version: `1.2.9`
Status: normative
Root WI: `wi_20260820_136`
Current epoch: `pulse-2026-08-18`
Cutoff: `2026-08-18T00:00:00+08:00`

## 1. Purpose

A platform or Agent-contract migration must be able to stop old work from acquiring new execution authority without rewriting historical facts. Platform Epoch Quarantine is an execution gate, not a lifecycle disposition: a quarantined WI remains readable and auditable until a later review completes, fails, supersedes, cancels, or migrates it through the WorkItem state machine.

### 1.1 Legacy disposition receipt

`harness disposition WI --to cancelled|superseded|migrated` is the deliberate
terminal path for a quarantined WI. It is not a shortcut to `failed` and it
does not claim that an Outcome UAT was accepted. Every disposition records a
stable reason code, human-readable reason, source and target epoch, and emits
the matching `work_item.<status>` event. `superseded` and `migrated` also
require a distinct, open successor WI that passes the current epoch execution
gate; `cancelled` carries no successor.

The command semantically closes associated handoffs: accepted handoffs are
archived as honored receipts, while pending handoffs are withdrawn with the WI
disposition reason (never fabricated as receiver timeouts). The WI event plus
the final handoff states are the cutover receipt that a dispatcher, scan, lint,
continuation, and execution surface must treat as terminal.

## 2. Policy

The optional policy is `<HARNESS_ROOT>/platform-epoch.yaml`, or the exact path in `HARNESS_PLATFORM_EPOCH_POLICY`.

```yaml
schema_version: '1.0'
current_epoch: pulse-2026-08-18
legacy_cutoff: '2026-08-18T00:00:00+08:00'
legacy_execution: quarantine
allow_work_items: []
required_result_refs: [CR-01, CR-02, CR-03]
registered_operating_area_refs: [OA-AKI-01, OA-HESTIA-01]
registered_operating_area_work_classes:
  - platform_development
  - platform_maintenance
  - platform_migration
  - platform_repair
  - platform_governance
  - platform_release
  - platform_validation
  - operating_evidence
default_portfolio_ref: PORTFOLIO-DEFAULT
work_item_result_overrides:
  wi_20260820_136: CR-02
work_item_operating_area_overrides:
  wi_20260830_583: OA-AKI-01
```

Requirements:

1. `legacy_cutoff` is timezone-aware; a WI with naive `created_at` is denied while the policy is active.
2. A WI created before the cutoff is denied unless its exact ID is allowlisted.
3. A WI created at or after the cutoff is current and may proceed through all other guards.
4. If no policy exists and `HARNESS_PLATFORM_EPOCH_REQUIRED` is false, legacy installations retain prior behavior.
5. If the policy exists but is malformed, execution fails closed.
6. If `HARNESS_PLATFORM_EPOCH_REQUIRED=1`, a missing policy fails closed even on a clean scan or WI-less night patrol.
7. When only `required_result_refs` is non-empty, every executable WI must map to exactly one current operating result through a token in `title`/`source` or an explicit `work_item_result_overrides` entry. Existing `work_item_result_unmapped`, `work_item_result_ambiguous`, and `*_result_mapped` reason codes remain stable for this legacy CR-only policy shape.
8. When `registered_operating_area_refs` is non-empty, every executable WI must map to exactly one typed admission reference: a current result, a registered operating area, or the policy-only default portfolio when that exact WI has a durable `portfolio_default` admission matching `default_portfolio_ref`. A new OA path maps only through an exact, complete `operating_area_ref=<ref>` field plus exactly one admitted `operating_area_work_class=<class>` field in `source`; missing, duplicate, malformed, or unknown machine-field values deny as `work_item_admission_invalid_contract`. A bare title/source mention is not authority. The default portfolio is never inferred from prose. Zero matches deny as `work_item_admission_unmapped`; multiple same-layer or cross-layer matches deny as `work_item_admission_ambiguous`.
9. `registered_operating_area_refs` and `registered_operating_area_work_classes` are a deployed snapshot of the canonical operating-area registry, not a second portfolio and not another CR. Publication must wait for the upstream registry revision containing the exact refs and work classes.
10. Result and operating-area overrides are separate namespaces. An OA must never be inserted into `required_result_refs` or `work_item_result_overrides`; conflicting typed overrides fail closed rather than choosing one.
11. No mapping bypasses the legacy cutoff. A pre-cutoff WI still requires `allow_work_items` or a later migration/disposition action.
12. `work_item_operating_area_overrides` is a bounded compatibility seam for WIs created before source-carried OA refs existed. Each entry ends when that exact WI becomes terminal or is superseded; successors and newly created WIs carry one `operating_area_ref=<ref>` plus one `operating_area_work_class=<class>` in `source`. The next Platform Epoch removes stale entries rather than inheriting them.
13. An existing, open, unclaimed `main_only` draft/ready WI that lacks a mapping must not create a second bootstrap WI merely to edit its own admission condition. An interactive human-authorized session uses the canonical `harness admit-existing` mutation to publish one immutable `<HARNESS_ROOT>/admissions/<wi_id>.yaml` record (`work_item_admission/v1`). The record identifies exactly one registered `current_result` or one registered `operating_area` plus work class, names the human decider and rationale, and leaves the WorkItem `source`, status, owner, criteria, and executor unchanged. Exact replay keeps the same decision and emits only replay audit; redirect, conflict with legacy mapping, cron use, missing human confirmation, owner mismatch, and malformed/unknown refs fail closed. `activate-owner` is a separate subsequent mutation and may run only after canonical publication/readback makes the admission visible.

```bash
harness admit-existing wi_YYYYMMDD_NNN \
  --agent <owner> \
  --admission-ref-type current_result \
  --admission-ref CR-02 \
  --decided-by <human-id> \
  --reason '<human rationale>' \
  --human-confirmed
```

An operating-area decision uses `--admission-ref-type operating_area` and must also supply exactly one registered `--operating-area-work-class`. This command records admission only; it never activates the WI.

14. `default_portfolio_ref` is an optional, bounded operating-portfolio fallback for **direct lightweight startup only**. When a freshly created, current-epoch, unclaimed `main_only` WI has no explicit CR/OA mapping, the startup path may publish one immutable `work_item_admission/v1` with `admission_ref_type=portfolio_default`, `admission_ref=<default_portfolio_ref>`, no operating-area work class, and `decided_by=policy:<current_epoch>`. This is a policy decision, not a fabricated human confirmation, not a fourth CR, and not an OA. Callers cannot select or redirect a `portfolio_default` ref; `admit-existing` accepts it only through the parameterless `--policy-default` path that reads the active policy. Existing confirmed drafts starting as standard WI never consume this default. Explicit CR/OA mappings, legacy cutoff, owner checks, cron denial, durable replay/readback, and ambiguity rejection remain authoritative.

15. An accepted `outcome_verification` handoff whose parent has valid OA admission must carry its own explicit OA and work class; the parent OA is not inherited. The interactive sending owner may use the existing `harness admit-existing <handoff-id>` command with `--expected-work-item`, the explicit registered OA/work class, and the same human-decision arguments. The parent and sender must match, the parent must remain open, and the requested OA must equal its valid parent OA. This narrow backfill appends only the explicit machine fields to `input_summary` and records `handoff.admission_recorded` in the parent event stream. It preserves parent state/admission, ACK, recipients, purpose, responsibility, criteria, execution scope/rights, and existing verdicts. Exact replay preserves assignment bytes; conflicting, duplicate or incomplete mappings, non-verification or non-accepted assignments, cron invocation, terminal parents and a mismatched parent hint fail closed. Assignment persistence and audit append share the store's exception-rollback transaction. Canonical publication resolves the actual handoff parent before authorizing the supplied hint.

```bash
harness admit-existing ho_YYYYMMDD_NNN \
  --expected-work-item wi_YYYYMMDD_NNN --agent <sending-owner> \
  --admission-ref-type operating_area --admission-ref <registered-oa> \
  --operating-area-work-class platform_validation \
  --decided-by <human-id> --reason '<human rationale>' --human-confirmed --json
```

`@anchor symbol PlatformEpochPolicy`
`@anchor symbol evaluate_work_item_execution`
`@anchor symbol WorkItemAdmission`
`@anchor symbol record_default_portfolio_admission`
`@anchor symbol record_existing_work_item_admission`

## 3. Execution-bearing operations

| Operation | Legacy quarantined WI | Current/allowlisted WI |
|---|---|---|
| scan/read/show/list/lint | allowed | allowed |
| dispatcher autonomous batch/spawn | denied with `skipped_platform_epoch` | evaluated by typed CR/OA/default-portfolio mapping and existing autonomy gates |
| `transition … --to in_progress` | denied before mutation | allowed |
| create or reassign a handoff | denied before mutation | allowed |
| accept a handoff / acquire executor claim | denied before mutation | allowed |
| continuation create, claim, or consume | denied before mutation | allowed |
| reject/withdraw/fail/supersede/release/terminal review | allowed | allowed |

The gate must run before durable mutation, event emission, dedup mutation, LLM spawn, or child-session creation. A denied dispatcher hit keeps its original autonomy classification and records:

```json
{
  "outcome": "skipped_platform_epoch",
  "route_receipt": "legacy_epoch_quarantined"
}
```

`@anchor symbol require_work_item_execution_allowed`
`@anchor test agent-harness/tests/test_platform_epoch.py`
`@anchor test agent-harness/tests/test_dispatcher.py :: test_legacy_work_item_is_skipped_before_autonomous_spawn`

## 4. Relay propagation

A Relay `harness_dispatch` job may set:

```json
{"platformEpochPolicyRequired": true}
```

Relay then injects `HARNESS_PLATFORM_EPOCH_REQUIRED=1` into both `harness scan` and `harness dispatch`. The dispatcher-spawned runtime inherits the same environment, so a child cannot bypass the CLI gates even if it invokes Harness directly.

The PULSE cutover manifest requires this flag for every configured `harness_dispatch` job. Missing policy or invalid policy is an execution error, not a clean scan.

## 5. Compatibility, release, and rollback

- Removing the policy and the required flag restores pre-epoch behavior without rewriting WI or HO records.
- `allow_work_items` is an explicit audited escape hatch; it is not inferred from owner, status, title, or Workspace.
- Terminal disposition remains a separate phase. Q0 quarantine must not mass-write `failed`, fabricate receiver timeouts, or close old handoffs.
- Policy enforcement is idempotent: repeated denied operations must leave ledger bytes unchanged.
- The stable Harness entrypoint on ownermac is `/Users/owner/Coevolution/Bigbird/PULSE/data/agent-harness/tools/agent-harness`; Vultr and TCloud retain `/Users/owner/Coevolution/Bigbird/Lab/BigbirdEnvironment/agent-harness/.venv` as their compatibility entrypoint. `/home/pulse/.venvs/agent-harness` is not a valid runtime path and must not appear as the configured production Harness entrypoint.
- The paired adopter supports an explicit cloud `--migrate-stable-entrypoint-to /home/pulse/.local/share/coevolution/tools/agent-harness` transition. It verifies the current recorded full apply, exact policy, sealed code inventory and canonical SCL snapshot under the installer lock; it changes only the stable code route. A destination preimage, foreign receipt/scope or redirected ancestor is rejected. The durable original intent supports interruption recovery and exact replay. Apply/rollback/reclaim stop while the intent has no completion. Completed route evidence maps historical stable path fields to the new code route without editing receipt bytes, WI identity, policy, ledger/kernel or data. An unchanged legacy code link remains an explicit reclaim protection until that link is retired; changed or broken legacy preimages stop reclaim. Existing legacy consumers are preserved until separately migrated and verified; route completion does not claim those consumers adopted or authorize their retirement. Normal paired code upgrades and rollback continue to validate the original journal bindings and operate the adopted route.
- Exact wheel builds reuse the selected existing Python with offline/no-build-isolation. The Vultr and TCloud production build steps select `/Users/owner/.local/share/coevolution/python-env/bin/python`, verify its prefix, and never create a build environment or install packages. Missing or incompatible declared build dependencies fail before the backend runs; the artifact records the interpreter and dependency versions. Runtime adoption uses the code-only release contract below.
- New runtime releases contain exact wheel code under `packages/`, launchers, and `runtime.json`; they do not contain `pyvenv.cfg`, copied dependencies, or an interpreter. Every launcher runs the OS account's `$HOME/.local/share/coevolution/python-env/bin/python` in isolated mode, checks its real prefix and the wheel's Python/dependency requirements, and imports Harness only from that release. Missing or incompatible dependencies fail before Harness import; installation never invokes pip, ensurepip, or an environment builder. The existing `.venv` stable pathname remains a compatibility route to code, not a second Python environment.
- Linux and macOS deployment readback must bind the resident reconciler as a direct Relay child to the selected code release and the OS account's canonical Python. A shared release must have a regular `runtime.json` declaring that exact prefix and entrypoint; the process must execute `python -I -B <release>/packages/harness/runtime_python.py cli <release-entry> harness-scl-reconciler`. Foreign interpreters, bootstrap code, entrypoints, missing isolation flags, and redirected or missing shared manifests fail closed. Receipt fields identify the shared interpreter separately from the code release. Legacy private-Python process matching remains available only for retained legacy rollback releases.
- Runtime reclamation discovers account-owned Git guards, global CLI and uv-tool launchers for both the legacy user-state layout and the macOS PULSE data layout. Moving runtime state must not move or hide these consumers; unavailable references stop reclamation before deletion or launcher repair. Reference discovery does not grant authority to alter another WorkItem or its guards.
- Shared-runtime inventory v2 binds every release-owned file, manifest, and launcher; it rejects symlinks and private environment directories. Receipt-bound inventory is verified before executing a retained release. Legacy inventory v1 stays readable for migration and authorized rollback. Shared Python and dependency maintenance belongs to `Coevolution/ssot/toolchain`; release rollback checks compatibility with the current shared environment before any pointer change, and does not restore a private dependency copy.
- Broker subprocesses and Git hooks preserve the selected release's code while reporting the real shared interpreter in `sys.executable` and `sys.prefix`. Guard generation inside a loaded shared bundle selects that bundle's module-capable `bin/python` before any PATH or adjacent-launcher fallback; a generic account Python cannot import code-only Harness. Source and retained legacy invocations keep their existing selection rules. Every process loading bundled Harness retains an open manifest descriptor so existing procfs/lsof reclamation protects its code, even after Python closes imported source files. Wheel contents cannot escape the release root. Capacity admission budgets wheel expansion and overhead rather than cloning a dependency environment.

  @anchor test agent-harness/tests/test_git_branch_guard.py :: test_loaded_bundle_guard_python_wins_over_unrelated_launcher
  @anchor test Scripts/tests/test_ownermac_activation_wrapper.py :: test_existing_wrapper_uses_registered_isolated_python
  @anchor test Scripts/tests/test_ownermac_activation_wrapper.py :: test_missing_shared_python_never_falls_back_or_starts_writer
  @anchor test Scripts/tests/test_ownermac_activation_wrapper.py :: test_invalid_registered_environment_stops_before_build_and_activation

- Ordinary Linux CI, including Stats platform integration, must use the registered self-hosted pool and its existing canonical Python/Node, reject fork execution and verify the host workload lease. The control-plane validator rejects setup-python/setup-node, per-job pip installation, missing selection/admission checks and automatic hosted routing.
- Harness CI uses the registered self-hosted runner account's existing canonical environment and verifies its Python/Node toolchain before running. It does not provision another environment or install packages during the job. Tests import the candidate source; synthetic wheel UAT uses a sealed code bundle on that same interpreter. Bounded legacy-venv fixtures exercise migration/rollback only and must be removed with their test scratch.
- The production runtime workflow has one explicit site selector and a closed allowlist of registered hosts. Vultr and TCloud must build the same exact Git revision and use the same installer contract; a caller cannot supply an arbitrary SSH target or runtime path.
- Production deployment on ownermac, TCloud and Vultr uses full recorded paired Harness/policy/SCL adoption for every exact SHA, even when platform-policy bytes are unchanged. SCL-only adoption is a bootstrap primitive: the installer refuses it before preparation whenever recorded full-apply history exists.
- An already-written single completed SCL-only successor may be absorbed by a new full revision only when its preserved stable runtime, policy digest, exact prior canonical snapshot, scope, receipt/transaction bindings and inventories extend the current full apply. The new full receipt and transaction digest-bind this segment as `canonical_scl_successor`; later replay, upgrades and rollback validate it without rewriting old records. Multiple successors, SCL rollback branches, unresolved or foreign intents remain fail-closed. Full rollback restores the captured SCL successor together with its prior Harness and policy; exact replay of the older full revision cannot erase the successor.
- If a rollback restores a full apply whose own predecessor has already been formally reclaimed, replay and the next upgrade may validate that predecessor through its exact inventory-bound retirement receipt. A missing release without that evidence still fails; an actual rollback continues to require the physical restore target.
- The workflow scheduler must select the registered ownermac control-plane runner by its stable platform labels before the in-job identity gate runs. A broad self-hosted label that makes correct execution depend on which runner wins the queue is not an authorized production entrypoint.
- A missing or dangling stable virtualenv is a recoverable cold-kernel state, not a reason to strand the host. In that state the workflow may use only the host system Python (version 3.11 or newer) as a bounded bootstrap interpreter for the exact checked-out installer. The installer must seed the new release from a retained sealed runtime that passes the normal runtime verification; it must fail closed when no such dependency-complete seed exists. The bootstrap interpreter does not become the stable runtime. After apply, every Harness/SCL probe must execute through the newly switched stable virtualenv and read back the requested source revision.
- An unsupported site, an inadequate bootstrap Python, an artifact mismatch, or a failed post-switch readback fails closed. It must not be bypassed with an editable install, an ad-hoc release copy, or a process-global source checkout.
- Runtime adoption readback verifies the stable Harness import/CLI and the canonical active SCL wrapper revision. It must not require the authorizing WI ledger to be replicated onto every runtime host, and it must not mistake the outer package console script (which intentionally has no canonical revision projection) for the active SCL consumer.
- Apply and rollback share a non-blocking process lock. Before the stable virtualenv or live policy changes, the installer persists the desired policy snapshot, exact prior policy, prior virtualenv location, and a transaction journal in mode-0700 state storage.
- Deployment failure compensation may undo only a full apply performed by that invocation. A verified replay retains the original immutable receipt for readback but returns no rollback ownership; a later capacity or Relay failure must preserve that preexisting runtime. Paired adoption persists newly returned rollback ownership before its own post-apply readback, and cloud rollback consumes that invocation-owned reference rather than the historical readback receipt.
- SCL-only recorded adoption preserves the stable Harness runtime and live platform policy. The first later full recorded apply may promote only a fully verified history of completed recorded SCL applies and SCL rollbacks with exactly one active canonical frontier; the full receipt must digest-bind those prior receipts/transactions so replay and rollback continue to reject missing, foreign, unresolved, or modified history. An SCL receipt is part of that canonical history only when its linked transaction explicitly records `record_canonical_state=true`; older pre-recording receipts remain audit history and cannot become canonical merely because they contain activation evidence. Likewise, legacy rollback transactions without recorded canonical state remain audit history and must not block promotion; recorded or unresolved rollback intents remain fail-closed boundaries.
- If source convergence has already advanced only the live `platform-epoch.yaml`, paired adoption may recover that precondition only when the current bytes equal the exact requested source policy and the active SCL receipt retains a physical content-addressed snapshot matching its recorded prior policy digest. During the existing tcloud policy-route migration, a recorded SCL receipt may still name exactly `<state_dir>/live-platform-epoch.yaml`; that legacy route is admissible only while the physical file exists, is not a symlink, and its bytes still match the receipt policy digest at promotion time. Recovery and the full apply share the installer operation lock; any other legacy route, third policy state, missing snapshot, ambiguous canonical frontier, or existing recorded full lineage fails closed.
- A failed post-switch verification compensates to the exact prior policy and virtualenv. If restoring the prior virtualenv itself fails, the new fail-closed policy is retained rather than pairing a new runtime with an unverified policy.
- Rollback preflights every retained artifact and checksum before the first live mutation; it depends on the retained policy snapshot, not the build checkout.

### WI184 implementation boundary: complete rollback acceptance remains open

The default legacy installer snapshots/restores the stable venv and policy only. Its
`rolled_back` result does **not** establish canonical SCL restoration: the active
wrapper contains an absolute target in the newly installed outer release.
Neither prior canonical `active`/`previous` state nor lineage restoration is
captured in the installer receipt. The existing manager's `rollback()` swaps a
mutable previous pointer; it is not a receipt-bound exact restore and must not
be blindly composed as one. Post-activation failure compensation has the same
unresolved canonical-pointer boundary. Requirement 9 below remains unmet.

Candidate revision mismatch is now checked before activation as well as after
readback. This prevents a known-wrong candidate from moving either pointer; it
does not fix concurrent pointer transactions, a later handshake failure, or
installer rollback. The installer lock covers its state directory; it is not the canonical-pointer
lock. Runtime routing restoration is not data rollback or evidence of forward
compatibility, and must not rewrite lineage, trust identities, keys or accepted
ledger facts.

The candidate manager serializes `activate`, `rollback`, `snapshot` and
`restore` **under one non-blocking lock** rooted at its explicit runtime root;
this is serialization, not an atomic multi-file transaction. `snapshot()` records
the exact active/previous targets, verified release digests and handshake
revisions, bound to that root. `restore(snapshot, expected_current=...)` validates
both records and targets, compares the complete current pointer pair, then
restores and reads back both pointers. Failure attempts compensation and verifies
its readback; it does not report restoration solely because writes returned. When
the prior active pointer is `None`, compensation distinguishes a healthy
previous-only pair from a pair where both pointers were absent; it must not claim
that either absence is a healthy active restoration. Raw traversal/borrowed aliases
are rejected before path normalization. Restoring an absent active runtime is
deliberately unsupported.

These are trusted-local pointer records, **not authorization or installer
receipts**. CAS checks current state, not intervening history/ABA. Pair replacement
is not a crash-atomic multi-file transaction and readers need not take this lock.
Old binaries and direct filesystem writes are outside the shared-writer contract.
The opt-in path now uses `activate_recorded()` to capture verified prior/current
states and its final handshake under that same lock. Its failure compensation
restores and checks the full prior pair. This is activation-bound local evidence,
not a signed authority receipt or a crash-atomic transaction. Default legacy
activation/rollback compensation is unchanged.

Installer `apply` and `apply-scl-runtime` require one `--intent`. The public Python
entry points likewise require `intent`; callers cannot combine recording, lineage
activation and baseline booleans. The closed intent set is:

| Intent | Consumer result | Allowed command |
| --- | --- | --- |
| `upgrade` | Upgrade the selected runtime with recorded canonical state and existing history/replay/rollback rules | Both |
| `establish-baseline` | Establish the explicitly requested first recorded baseline under the existing baseline guard | `apply` |
| `initialize-lineage` | Perform the existing legacy lineage cutover; recorded history still blocks it | `apply` |
| `legacy-apply` | Explicitly use the existing unrecorded path; recorded history still blocks it | Both |

Ordinary production adoption uses `upgrade`. The installer never infers baseline
creation or lineage cutover from missing history or a failed upgrade. Missing,
unknown, conflicting legacy flags, or SCL-only baseline/lineage intents are rejected
before release preparation or live mutation. The removed CLI flags and Python
boolean arguments have no fallback translator. Deploy callers and the installer
come from the same exact source artifact. Historical receipts/journals retain their
existing Boolean fields and remain readable; this interface change does not rewrite
audit evidence, choose an authorization identity, or change transaction semantics.

For `upgrade` and `establish-baseline`, canonical activation is recorded.
The prepared runtime must support the capability before live routing/policy changes; an old
wheel is never silently retried without recording. The returned record is checked
and persisted through the existing receipt/journal. Once canonical dispatch has
started, opt-in failures halt with recovery-required/unknown outcome rather than
independently restoring stable/policy; finalization failures attempt the same
marker and may themselves fail to persist it.

Recorded application now captures an initial canonical snapshot before local
routing/policy writes and passes it as an expected-current condition to the
lock-held activation. The actual activation prior must match; separate snapshots
are not represented as one spanning parent/child lock.

The candidate admits a **narrow coordinated rollback**: a completed recorded full
apply with a matching journal, sealed prior stable symlink, prior policy file,
usable prior canonical active, no lineage activation and no competing recorded
operations. It verifies receipt/transaction/path bindings and both old/current
release contents before execution, then preflights the canonical restore before
local writes. Local restoration precedes final canonical restore-CAS, its logical
commit point—not a crash-atomic publication. A failed CAS permits local compensation
only with verified unchanged canonical-current state and freshly checked owned
local values. Desired/third-party/unreadable outcomes require recovery. Completed
retries check the actual restored triple; unknown transactions remain blocked.

Release inventories bind owned file hashes and link/directory structure to prior
receipt evidence, not read-only mode bits or a newly computed self-hash. Retained
sources/caches are checked before execution/cloning; malformed, unreadable or
conflicting captures never downgrade to legacy trust. Recorded handshakes use a
closed allowlist of inventoried outer releases and exact wrapper templates, so an
unverified third outer is rejected before execution. External import startup
hooks are restricted. Host interpreter/stdlib and explicitly supplied unmanaged
legacy bootstrap seeds remain trusted inputs. These checks detect static drift;
they do not prevent a malicious filesystem owner replacing trees between check
and execution, nor certify arbitrary application dependency closure.

Legacy receipts cannot bypass recorded boundaries; unsupported initial, directory,
absent-policy, noop/SCL-only or competing-record histories still refuse rollback.
Reclaim retains its conservative **all-releases hold** for recorded receipts/intents
and unknown evidence. Precise lifecycle retirement/bounded retention, broader
history eligibility, crash recovery and data compatibility remain unclosed before
production enablement. The seeded two-release native test passed in the complete
GitHub-hosted suite at `ae0d28e9`: A→B→A and completed retry, with distinct outer
releases. That run still had 19 failures: the older large UAT exposed legacy
reclaim deleting a captured historical SCL-only outer before recorded admission.
The follow-up candidate protects every recognized historical inventory binding
even in legacy reclaim. Unknown reference discovery blocks mutation; release
aliases/traversal are rejected. This preserves current all-history consumers,
not bounded historical retirement. Local pure/synthetic follow-up tests are not
native validation of that fix.

The next candidate connects the existing exact builder directly to that native
case: committed PULSE HEAD/subtree, archive bytes, manifest bytes/schema/paths and
one wheel are checked, and B installs the builder's unchanged manifest/wheel.
A remains a synthetic predecessor using the same wheel, not an old-code version.
The build occurs inside the full-suite case; no tests are narrowed or bypassed.
`GITHUB_ACTIONS` is merely an accidental-execution interlock; the actual hosted
workflow supplies OS-account provenance. This connection has only static/pure
validation at introduction. The pure-audit successor `b0e3912b` subsequently
completed the full hosted suite with 18 old contract/data failures and both native
installer cases passing; exact-builder apply/rollback therefore has hosted native
evidence, not production or real-old-code compatibility evidence.

A further candidate adds narrow full-apply replay before reclaim/preparation:
new recorded receipts and matching completed journals retain the manifest byte
hash and source map parsed from that same buffer. Exact request, confined history,
sealed prior/current inventories, policy buffers and the applied canonical pair
must match. Replay returns the original receipt without activation or new
receipt/journal; any recorded rollback history, foreign/noop history, unsupported
prior state or old uncaptured receipt remains ineligible. Ordinary legacy rollback
history without recorded markers is not turned into a replay gate. Native
exact-builder UAT now requests the identical B again and checks unchanged evidence
before rollback; `b1d8c448` subsequently validated that extension in the complete
GitHub-hosted suite (17 old contract/data failures, both native cases passed).
Version/data compatibility and resident acceptance remain open.

Known Linux process listing/access/UID-parse failures now mark discovery
incomplete while preserving observed release roots. Existing reclaim/retirement
consumers refuse incomplete scans. Vanished PID/closed-FD races are tolerated.
Synthetic procfs tests do not establish universal idleness: other users, lazy
imports, old invocation entry points and historical retry obligations remain
unclosed. No captured operation expires, and hold-all is not removed by this fix.

`@anchor symbol apply_release`
`@anchor symbol rollback_release`
`@anchor symbol _operation_lock`
`@anchor test agent-harness/tests/test_runtime_release.py :: test_runtime_release_replaces_editable_and_exactly_rolls_back`
`@anchor test agent-harness/tests/test_runtime_release.py :: test_apply_failure_compensates_exact_prior_state`
`@anchor test agent-harness/tests/test_runtime_release.py :: test_concurrent_apply_fails_before_release_mutation`
`@anchor test agent-harness/tests/test_runtime_release.py :: test_runtime_release_workflow_uses_verified_retained_artifact`

## 6. UAT contract

A real CLI/dispatcher UAT must prove all of the following:

1. a pre-cutoff WI cannot transition to `in_progress`, create/accept/reassign a handoff, or create/claim/consume a continuation;
2. the same legacy WI remains readable and can be rejected, withdrawn, released, failed, or dispositioned with a durable cutover receipt;
3. current WIs mapped to exactly one CR or one registered OA succeed through the same entry points, while unmapped and same-layer/cross-layer ambiguous current WIs fail closed;
   an existing unmapped main-only draft can receive one human-confirmed durable admission and then activate without another WI, while its original source and draft state remain unchanged until activation;
4. an OA mapping and OA override do not bypass the legacy cutoff, and a bare OA mention does not grant admission;
5. a missing required policy and a malformed policy produce non-zero error without LLM spawn;
6. an allowlisted legacy WI succeeds;
7. runtime apply proves the import and CLI come from the exact release virtualenv, duplicate apply is a no-op, and a concurrent apply is rejected before release mutation;
8. rollback succeeds after the source policy file is removed, refuses missing/corrupt backups before mutation, and restores the exact prior virtualenv and policy;
9. PULSE rollback removes the Relay required flag and cron shrink, while Harness rollback restores the prior policy/runtime, together reproducing pre-cutover behavior exactly;
10. the production workflow applies one exact revision independently to Vultr and TCloud, rejects an unknown site before SSH, and proves a dangling stable virtualenv can be rebuilt from the exact artifact without weakening the post-switch stable-runtime probe.


## Runtime capacity sampling

Runtime release admission keeps its 80% maximum projected utilization and adds
allocated candidate growth before copying. Measurement follows the target volume:
Darwin uses the fixed system `/bin/df -kP` used/available byte columns, matching the
site capacity guard on shared APFS storage; other platforms retain `statvfs` used
and non-superuser-available accounting. It must not choose whichever probe reports
less usage. Missing, malformed, ambiguous or timed-out measurement fails closed;
actual over-limit volumes remain denied. No validation override or threshold
increase is permitted to force a production install.

## Runtime references for independently selected Host profiles

`install-runtime-release.py runtime-reference` records explicit operator selection
or retirement under the existing runtime-release lock. It is retention metadata,
not SCL assignment, profile activation, write permission, or independent Outcome.
A profile directory or mtime never establishes a reference. The operator supplies
`--installation`, its exact `--expected-installation-sha256`, `--state selected|retired`,
`--expected-reference-sha256` (`absent` for first selection), `--state-dir`, and the
existing matching `--root-work-item-id`/`--authorize` deployment authorization.

Selection validates the local Host, principal, exact regular paths, installed
profile/runtime digests and installation closure. The immutable reference event
binds the operation WI, actual Host user UID, Host, profile path/digest, installation digest and exact
release. Events are the source of truth, with sequence and prior-digest linkage;
there is no separately mutable index. The response schema is
`agent_harness_runtime_reference/v1`, with `status=local_committed|read_only`,
`reference`, `reference_sha256`, `receipt_path`, and `grants_write_authority=false`.
An exact lost-response retry returns the same receipt. Conflicting expected digest,
owner, Host, installation, or path is rejected without changing the reference.

Every supported installer reclaim (including capacity admission during apply)
protects all selected references with reason `selected_runtime_reference` and
reports their receipts. Protection survives unlimited upgrades and does not depend
on rollback position, a running verifier, or the continued availability of mutable
method source. Invalid history or a missing selected release fails closed.
Multiple profiles referring to one release remain protected until all references
are explicitly retired. Retirement requires the current reference digest, the same actual Host user UID
and installation, and a complete process scan proving that release idle. The WI
is operation provenance, not a persistent execution lease: a later currently
authorized maintenance WI can retire a reference under the same Host owner,
without reusing a completed WI or expired Session authority. It
retires only the retention reference: it does not revoke Host invocation capability,
remove profile/history/failed evidence, or itself reclaim the runtime. The operator
must stop selecting the retired profile; re-selection must be registered before
further use. Actual reclamation remains the existing supported `reclaim` operation,
which independently retains stable, rollback, launcher, guard and live references.
Selection/retirement and reclaim serialize on the same lock; a reclaimed runtime
cannot subsequently acquire a successful selection receipt.

These guarantees apply to the updated installer. Historical installer copies and
arbitrary shell deletion are outside this execution boundary. A retention receipt
must not be advertised as first-write interception or profile execution authority.
