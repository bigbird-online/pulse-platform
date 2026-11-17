# Operator-selected verifier computation

AH-063 / AH-064 / A-30 require computation and tool selection to come from
installed policy. The current native dispatcher embeds provider/model/thinking
constants. This next slice moves the selection into the same validated policy
boundary and records the exact launch configuration. It does not change producer
conversation settings, credentials, verification criteria, assignment identity,
human authority or the current execution mode.

## Configuration and admission

An optional verification_compute object is closed and complete: provider, model,
thinking and tools. No business command, candidate, artifact or suggestion may
supply or override it. Absence preserves the current openai-codex / gpt-6.1-sol /
high configuration and the existing read,bash,edit,write tool set. The dispatcher
must pass an explicit tool allowlist even for this default, so a runtime's later
default additions cannot silently widen tools.

Provider and model are bounded identifiers, not shell text, glob patterns or
thinking suffixes. thinking uses an explicitly supported Pi level. tools is a
bounded, duplicate-free list of supported tool names. Unknown fields, wrong
types and unsupported values reject during policy review and installation before
preparation. Empty or unusable tool selections must not be silently expanded to
make a verifier work. Without the command-execution tool needed for native
identity/ACK/result submission, dispatch reports a concrete configuration failure
before launching a model; authenticated read-only policy inspection remains
available. Bad privileged configuration never reconfigures ordinary Relay chat.

The existing Pi CLI supports fuzzy model matching. Passing model strings alone
therefore does not prove exact selection. Before any model invocation, the
adapter must resolve through the installed runtime's model catalog and refuse a
provider/model mismatch or silent fallback. It must preserve the original
selection and failure evidence without logging credential values or editing the
catalog. A runtime that cannot prove the requested selection remains unavailable;
it does not fall back to an unrelated provider. Native session metadata must
subsequently show the actual provider/model/thinking and enabled tools. A launch
attempt or process exit alone is not independent verification.

## Paths and authority

Existing profile artifact_roots govern Harness artifact capture; repository,
publication, consumer and delivery bindings govern those physical adapters.
Their canonical-path and redirection checks remain authoritative. Named Pi tools
are distinct from filesystem confinement: permitting bash can reach any path the
OS user can reach. This slice must not claim an OS sandbox or full arbitrary-shell
path enforcement. Broader A-30 path confinement remains open until a real
enforcement boundary and escape tests exist.

Native producer/verifier roles and tenant/session provenance remain derived from
the original process and installed profile. Tool/model selection cannot grant
publication, human review or lifecycle write capabilities. Audit policy still
constructs no dispatch adapter, and switching a compute setting cannot opt into
automatic dispatch. The same program/policy writer identity and formal installer
protect activation. Older installers reject the unsupported verification_compute field. Retired
program/policy identities remain fenced from writes; this is not a claim that
every historical read-only client validates the new policy shape.

## Acceptance and rollout

First prove strict validation, read-only diff/inspection, rejection without
partial activation, exact runner configuration/tool selection, invalid catalog resolution
without model invocation, and immutable launch evidence under retries. Exercise
real artifact-root rejection separately from a tool allowlist. Process stubs are
source tests, never native adoption.

Then prepare a candidate and review a concrete isolated policy diff with the
original management endpoint. Activate only at a stable native frontier. The
original producer must dispatch a new independent assignment; its actual native
session must consume the selected configuration, ACK, inspect the fixed original
artifacts, and submit its own result. Preserve all prior assignments, generations,
human decisions and resources. Full A-30 and full WI018 UAT remain open.

Status: implemented with 474 source tests passing in 52.45 seconds, 30 modules
checked by strict mypy, and ruff/diff passing. Real Node fixtures exercise the fixed
runner; an installed Pi 0.87.1 SDK factory probe constructed the exact selected
model/thinking/tools with prompt replaced by a local marker (no model call).
These are source checks. Candidate20 is now installed only in the original isolated
destination. Controller readback verified the exact bundle and compute policy,
253 originals / 129 journal entries / one head, custody, floor3 and 39 guards.
The same installer retry preserved DB bytes; the old candidate19 installer rejected
the new compute field without changing activation. Source remains candidate19
and sealed79. See [installation evidence](evidence/rewrite-compute-selection-install-20261008.json).
Real independent SDK ACK/verdict, consumer adoption and broader A-30 path
enforcement remain pending.

## Exact native integration

Use the admitted Pi installation's SDK for the independent verifier. One bounded
runner loads one ModelRuntime, restores the selected provider local catalog with
network refresh disabled, resolves provider and model by exact identifiers,
and passes that exact object, the explicit thinking level and tool allowlist to
createAgentSession. It checks the constructed session's actual model, thinking
and active tools before calling prompt. Any fallback, clamping or additional
tools refuses the launch; it does not silently adjust policy. Disable extension,
skill, prompt-template and context-file discovery, as in the current CLI launch.
The producer keeps its original Relay/Pi process and conversation.

The runner is a fixed resource of the verified Harness bundle. It is copied
without replacement into the original dispatch directory, with its content hash
and the selected configuration in the immutable launch record. The existing
child-owned spawn proof is persisted before exec, and the SDK creates a real Pi
session with the assigned session ID in that same dispatch. A distinct runner
process title must not borrow ordinary CLI admission: NativeHost verifies the
admitted Node executable, original PID/start/boot stamp, exact runner bytes,
launch configuration and session header before admitting only the existing
verifier profile. No command input supplies a principal or ACK.

NativeHost reads the runner's title, environment and executable from the host
kernel. Linux uses procfs comm, environ and exe. macOS has no procfs: the title
and environment come from the process's KERN_PROCARGS2 arguments, bounded by
kern.argmax, and the executable from libproc proc_pidpath. The runner's title
blanks the rest of its argv there, so only the named dispatch, attempt, tenant
and Relay context keys are taken from the remaining strings, and a repeated key
is refused. Only processes of the invoking user are read. Both platforms then
apply the same checks: admitted Node executable, spawn.json stamp and effect,
no symlinked dispatch proof, and the launch configuration and session evidence.

Native session records retain the exact model/thinking selection and a custom
configuration entry for enabled tools and runner digest. Dispatch readback
distinguishes launch intention, pre-model selection failure, admitted session,
ACK and final verdict. A missing or interrupted result remains on the original
effect and attempt. Successful process startup never becomes an invented ACK
or accepted verdict.

Compatibility tests preserve historical CLI launch records and native sessions.
New dispatches use one SDK path; no fuzzy CLI fallback is retained. The adapter
uses only the installed SDK and never patches its source or copies credentials.
The Python domain kernel remains independent of the SDK. New package resource
admission is limited to this exact runner filename and its manifest digest.

## Existing transport and model-failure semantics

The first real candidate20 SDK run selected the exact configured computation but
its first assistant message ended with stopReason=error and fetch failed, with no
tool call, ACK or verdict. Its prompt promise resolved, so prompt_completed was
not sufficient evidence of successful model execution. Preserve this original
attempt and session; do not rerun it through an administrator or manufacture a
verification result.

The fixed SDK adapter must initialize the admitted Pi installation's own HTTP
dispatcher, including its existing operator proxy and idle-timeout settings,
before any provider request. The original Pi CLI performs this bootstrap; merely
importing the SDK does not. Read only the admitted agent directory's bounded
settings file and select the two networking fields. Do not rewrite settings,
authentication or runtime packages; do not discover project settings or print
proxy URLs, credential values or unrelated configuration. Existing environment
proxy precedence and certificate verification remain unchanged. Bind the exact
transport module to the same admitted Pi installation in the immutable launch
configuration and record its hash, initialization and settings provenance without
secret values. Missing or invalid transport initialization fails before a model
call; no unrelated implementation or fuzzy CLI fallback is allowed.

Observe assistant message completion as well as the prompt promise. A resolved
prompt containing an assistant error or abort must retain a failed/interrupted
runner result and nonzero exit. A completed prompt still cannot invent ACK,
verification, human approval or delivery. The same dispatch attempt is retained;
no implicit model retry, new assignment or request replay is authorized by this
repair. Any later business revision uses the original WI and its actual human
decision.

Acceptance adds a local proxy integration probe with the installed Pi transport
(no external model or credentials), exact environment/settings precedence,
read-only settings preservation, missing/invalid transport rejection before
prompt, and a source fixture where prompt resolves with an error event. After
source checks and a stable-frontier upgrade, a new genuine independent assignment
must prove actual model/tool execution, ACK and per-criterion result in Muse.
The failed original candidate remains historical evidence, never a passed UAT.

Repair status: 482 source tests passed in 49.38 seconds, strict mypy checked 30
modules, and ruff/node syntax/diff checks passed. The actual installed Pi HTTP
module completed a local proxy request with the new runner, while candidate20
made no proxy request and failed. A separate run proved an existing environment
proxy wins over a different operator setting. All settings bytes were unchanged;
these probes loaded no credentials and called no model. See
[source evidence](evidence/rewrite-compute-transport-source-20261008.json).

The original Muse journey is gen7/rev140, awaiting_verification. Independent
controller readback verified 19 original commands, unchanged preexisting
history/definition, three retained resources and unchanged source79 and published
consumer b64. The one new SDK session returned fetch failed without any tool call,
ACK, verification or review. Its incorrect prompt_completed marker remains
historical evidence; it is not a pass. See
[failed native attempt](evidence/rewrite-compute-selection-failure-20261008.json).
Candidate21 is now installed only in the original isolated destination with the same
policy. Independent readback confirmed 274 originals / 140 journal entries / one
head, unchanged binding/custody, floor3, 39 guards, all 30 resource-evidence files
and all 12 failed-dispatch files. The actual installation subprocess and its
exact retry both returned exit0; the retry preserved DB bytes. Original operator
settings/HTTP-module hashes and source79 remained unchanged. See
[installation evidence](evidence/rewrite-compute-transport-install-20261008.json).
Original Pi read-only adoption passed: three actual commands returned OS exit0,
with the same principal, exact installed bundle/policy, canonical140 projection,
and unchanged DB/Views/source bytes. The controller read all three main6/v8
proposal originals and approved their exact hashes in the original Telegram
channel at22:17. The authorized path is revise_work on the original active
candidate, main5 to6, reuse of the third resource, a new v8 child commit and a
new independent assignment, stopping before candidate human review. The actual
model recovery/ACK/verdict remain pending. See
[native adoption and HOW](evidence/rewrite-compute-transport-native-20261008.json).
