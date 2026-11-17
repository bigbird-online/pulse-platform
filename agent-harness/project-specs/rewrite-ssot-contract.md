# SSOT diagnostics contract

AH-068 / A-31 retain a static configuration registry, separate from WI authority.
The current slice replaces drift decisions and migrates the existing check CLI
and scheduled scan consumer. Agent registration and metadata display remain the
existing static configuration interfaces; their full rewrite and native adoption
are not claimed by this slice.

## Boundary

The new stdlib module `harness_rewrite.ssot` owns dependency validation, scope
selection, filesystem observations and their verdict. It never imports legacy
controllers, opens a WI database, repairs a projection, writes a registry, runs
commands or grants authority. The old `harness.ssot_engine` is only a typed adapter.
YAML/Pydantic decoding stays at the existing configuration edge; the new module
receives a typed diagnostic snapshot and a policy-byte decoder. This decoder
does not own file I/O or verdicts.

Accept diagnostic layouts 1.0, 2.0 and the current observed registry version
2.3.0. Version 2.3.0 retains the same entry/projection/dependency fields and adds
static metadata including repository bindings. This slice does not verify owner,
repository or channel bindings, external surface registries, runtime liveness,
policy action semantics or governance prose. Reports list these unchecked
dimensions explicitly; a pass applies only to the implemented diagnostics.
Reject unknown schema, unknown mode, simultaneous
agent/entry selectors, missing selectors, dangling dependencies, cycles, invalid
entry/projection kinds and invalid relative paths as configuration errors before
filesystem inspection. Validate the complete graph even for a narrow selection.
Agent selection includes its transitive upstream closure. Entry selection includes
that entry and its upstream closure. All selection includes every entry and agent.
Each selected entry appears once in deterministic order. Quick applies to all
three selectors and skips C1/C2 explicitly; it never silently runs a full check.

## Checks and evidence

S1 checks document/file or package/directory presence; a document must be nonempty.
S2 checks agent workspace directory; S3 checks nonempty identity file.
S5 checks projection presence/type. S6 checks the declared policy's agent_id:
missing policy or mismatching identity is a finding; malformed/unreadable policy
is unknown. A retired agent with an existing matching policy is a minor finding.
No policy declaration requires no S6 check. Governance prose headings are not
mechanical pass criteria; S4 remains retired.

C1 checks that a symlink projection resolves to the declared source and that
source exists. Wrong-kind, dangling and wrong-source links are findings.
C2 compares exact source/target file bytes for reference/embed projections.
This retained contract is byte equality, not Markdown reference resolution or
semantic excerpt equivalence. A package with a reference/embed projection has
no supported content comparator and is unknown, not a pass.

Only declared relative paths beneath the selected root are observed. Absolute,
parent-traversing and backslash paths are invalid configuration. A resolved
symlink outside that root is reported as a boundary finding without reading its
content. Reads open the resolved in-root path through directory descriptors with
no-follow flags, reject non-regular content, and use nonblocking opens so a FIFO
cannot stall the check. Missing paths are findings; inaccessible/racing/unsupported
observations are unknown. A single content read is capped at 2 MiB; oversized
content is unknown. No recursive directory walk or process/network probe occurs.
The registry is bounded to 10000 entities and 50000 dependency/projection edges.

Reports retain scope, mode, actual supplied registry path when loaded from a file,
registry byte SHA256 when available, normalized diagnostic snapshot SHA256,
observation time, selected entities, skipped check IDs, and individual observations.
Each observation records check ID, layer, target, status passed/violated/unknown,
fixed reason, severity and bounded evidence (file digest/size where applicable).
Reports do not echo file bodies, YAML parser exceptions, owner bindings or policy
values. The observation concerns the current filesystem, not a historical or
transactionally consistent repository snapshot.

Every violated/unknown observation is also a compatible finding for the existing
scan consumer. Unknown observations have major severity so scans cannot silently
drop them. A critical violation yields fail; another violation yields
pass_with_findings; otherwise any unknown yields unknown; otherwise pass.
Unknown counts remain visible even when a violation dominates the verdict.
The CLI exits 0 only for pass, 1 for findings/unknown, 2 for invalid input.
An empty registry or a selected agent with no entry dependencies still reports
its exact scope and counts; absence of applicable C checks is not fabricated
content validation.

## Validation and completion boundaries

New tests exercise all/agent/entry and quick/full selection, transitive closure,
cycles/dangling references, bad modes/schema/kinds/paths, missing/empty/wrong-type
files, exact/mismatching/oversized content, in-root/wrong/outside/broken symlinks,
unreadable and changing files, malformed/mismatching/retired policies, unknown
aggregation, provenance, no raw private values and byte-preserving repeated reads.
Integration tests use the existing CLI and scan function with isolated registries,
prove both consume the replacement and do not repair files or create WI facts.
Legacy private helper tests are not a release gate for the replacement.

Source validation, installed release, actual native consumers, static registration
migration and complete AH-068/A-31 acceptance remain separately reported.
