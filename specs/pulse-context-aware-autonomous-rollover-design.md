---
feature_name: "pulse-context-aware-autonomous-rollover"
version: "0.7.0"
status: "proposed"
root_work_item: "wi_20260822_730"
created: "2026-08-22"
modified: "2026-09-04"
# history: git log -- specs/pulse-context-aware-autonomous-rollover-design.md
owner: "Pulse"
upstream_dependency:
  - "specs/pulse-context-aware-autonomous-rollover-requirements.md"
  - "specs/pulse-session-context-lifecycle-design.md"
external_dependency:
  - "bigbird-online/CollaborateWithAI:Bigbird/Lab/BigbirdEnvironment/agent-harness/specs/harness-autonomous-rollover-design.md"
tags: [design, autonomous-rollover, multi-surface, resume-fast-path, actions-fallback, evaluator]
---

# PULSE Context-Aware Autonomous Rollover — Design

## S0 control flow: surface class before adapter

```text
user/runtime /wi intent
        |
SCL Surface Registry + Resolver
        |
        +-- resident_harness -> resident Harness
        +-- actions_bridge   -> harness-native external entry -> Harness
```

The registry stores current surface **instances** and the workspace's exact canonical `agent_id`. Local/cloud vendor labels and human-readable identity names are not lifecycle or machine-identity types. Both resident and brokered adapters consume the registry token; an explicit override must match before Harness is called. Resident migration is dormant + checkpoint + explicit same-WI adoption.

## S0R control flow: checkpoint before archaeology

For a fresh resident Session continuing an existing WI, the platform hot path is intentionally narrow:

```text
continuation intent
      |
      v
PULSE workspace/surface descriptor
  + canonical agent_id
      |
      v
Scripts/scl-resume.py
  - read JSON-compatible config/scl-surfaces.yaml once with stdlib
  - require exactly one resident_harness surface
  - invoke exactly one `harness continue`
      |
      v
Harness ResumePacket
      |
      +-- completed / pending / decisions
      +-- next_action
      +-- must_read / stale_if
      +-- control_plane_completed
      |
      v
FIRST WI BUSINESS ACTION
```

There is deliberately **no GitHub/PR/Actions/provider-discovery node** between surface resolution and ResumePacket. The adapter measures `surface_resolution`, `harness_resume`, and total control-plane milliseconds so real channel tests can measure TTFWA.

The registry is serialized as JSON-compatible YAML. `scl-surface-route.py` and `scl-resume.py` use `json.loads` from the Python standard library, so resident startup does not inherit the CI environment's PyYAML installation. Repository-wide validators may still use PyYAML after an explicit setup step.

For a physical fresh target, Relay first persists a private `pulse_pending_work_item_entry/v1` plan containing the raw local runtime Session id and its opaque Harness ref. `scl-resume.py` passes only the opaque ref to `harness continue`, then atomically creates or reuses the privacy-safe runtime usage binding. Relay stores that binding in the pending plan before returning control-plane success. Activation kills only a conflicting old runtime, starts or reuses the exact planned target with the binding injected, persists routing, sends the ResumePacket prompt, and clears the plan only after send acceptance. Restart reloads the same plan; post-bind failure therefore retries the same Session rather than stranding Harness authority.

A canonical WorkItem failure never reaches this activation state. The adapter carries the typed Harness error unchanged, and neither Stats inventory nor the runtime prompt consumes a linked-only business object. For consumed rollover, Harness exposes the digest-bound source checkpoint as the target resume baseline, so the packet remains `execute` instead of degrading to an empty `orient` packet.

The fresh Session consumes the packet with this precedence:

```text
1. packet work frontier
2. packet-declared stale_if / must_read revalidation
3. current business authority required by next_action
4. broader repository discovery only after work has actually resumed or when explicitly required
```

A ready packet is also a work-mode decision, not merely data:

```text
resume_ready=true
      |
      +-- valid next_action/must_read/stale_if
      |       -> consumer_work_mode=execute
      |       -> packet-scoped micro-check
      |       -> FIRST WI BUSINESS ACTION
      |
      +-- malformed frontier -> fail closed
```

Generic `orient`, repository archaeology, broad rule reloads and audit fan-out are post-first-action activities unless the packet explicitly makes them the business action. The PULSE adapter exposes a `pulse_scl_first_action/v1` contract and labels its timer `control_plane_only`; channel dogfood records the later first-action timestamp so control-plane speed cannot masquerade as TTFWA.


`control_plane_completed` is a compatibility seam for older checkpoints. If an old `next_action` says “enter/reconcile the WI and then ...”, the Agent treats the entry/reconcile part as satisfied by the current `harness continue` and performs the remaining business intent.

### Audit/distribution lane

Execution authority and Git distribution are decoupled:

```text
resident Harness durable resume ------------------------> work continues
                     \
                      \--> later Git/PR audit replication / normal CI / closeout
```

Git remains the formal code delivery and final verification control plane. It is simply not a mandatory synchronous hop for every same-Agent Session replacement. A repository query may still be first-work-critical when `next_action`, `must_read`, or a staleness predicate explicitly binds repository state.

### Checkpoint production contract

The rollover side should persist a work frontier, not a startup script:

- completed = durable facts that must not be rediscovered;
- pending = remaining work/gates;
- decisions = settled choices/constraints;
- artifacts = minimal exact reads needed next;
- next_action = first post-entry business action, surface-neutral when possible.

This is what makes an autonomous rollover economically viable: it cannot save context cost if each replacement Session spends minutes re-deriving the old Session.


## Terminal required-consumer convergence

Terminal closeout is a published pre-terminal settlement followed by protected authority retirement:

```text
accepted outcome + exact human review
        |
        v
phase2_closeout/v3 settlement (WI remains nonterminal)
        |
        +-- derive inventory from config/scl-surfaces.yaml
        +-- publish settlement unchanged on current default
        +-- install every required resident consumer
        +-- exact revision readback
        +-- real Stats collect_in_flight effect probe
        +-- cleanup, then digest-revalidate all durable evidence
        |
        +-- every gate passed
                -> binding active|dormant -> terminal(settlement ref+digest)
                -> one WorkItem terminal event
        |
        +-- any omission / downgrade / unavailable / dirty / mismatch / stale effect
                -> remain nonterminal
                -> checkpoint obligations + exact next probe
```

The existing surface registry remains the sole inventory authority. Its closeout projection names the canonical consumer and read models, while resident consumers are derived from the existing surface rows/classes; it does not duplicate the resident surface list. The settlement binds registry repo/path/SHA-256, and Harness verifies the local bytes against that repository's current-default Git blob before trusting the derived set. The ChatGPT surface (`actions_bridge`) is not a resident installation.

The settlement file and all cleanup/probe/projection artifacts are repository-relative and digest-bound. Harness verifies the settlement's Git blob on the current default head and re-reads every durable artifact after cleanup. Temporary absolute paths, deleted snapshots, symlinks, and content drift fail closed.

Execution retirement is deliberately ordered before the WorkItem terminal write. The explicit `terminal` lifecycle state is not a second business outcome; it records that Session authority has been irreversibly retired for one settlement digest. A crash between writes leaves a legal replay-only state, not an active binding attached to a completed WI.

WI136 remains the stale-consumer counterexample. WI938 adds omitted inventory, branch-local completion, cleanup-invalidated evidence, and active-authority-after-completion regressions. The positive control requires exact install/readback plus `probe_terminal_work_item_projection`, which calls the production Stats `collect_in_flight` path rather than a cache or mock.

## Resident surface deployment boundary

A startup-contract deployment is distinct from a Relay runtime release. The deploy unit is the exact-main registry, route/resume adapters, relevant tests/Specs, and four workspace projections.

- A clean resident repository may receive the allowlisted payload as a local deployment commit. Unrelated worktree state is fingerprinted before and after.
- An active Relay release with overlapping live changes is never overlaid. The startup surface is installed as an immutable exact-main checkout and published through a managed pointer only after smoke passes.
- A newer control-plane SHA with an identical startup payload reuses the active deployment commit and emits a new exact-request receipt; an empty commit is not required.
- This deployment neither restarts Relay nor changes runtime data, packages, service definitions, or process ownership.

## Actions resource resolver

Portable repository CI uses the registered `portable-ci-continuity` profile,
projected to the existing `trusted-linux-control` pool (`pulse-linux-trusted`,
`pulse-control-plane`). Ordinary PR/main jobs prefer self-hosted execution and
reuse the account's SSOT-managed Python/pytest and system Node. A missing runner
leaves the job queued; it does not trigger paid hosted execution. Only trusted
same-repository PRs may reach this resident pool. Host workload hooks and a
pre-checkout lease assertion serialize physical work. The default-branch gate
uses the same guarded pool on main. The full capability workflow remains
manual-only, and hosted UAT requires an explicit manual selection.

This continuity route does not broaden authority. Jobs that require the canonical Harness ledger, Mac-local state, production credentials, deployment bindings, or an installed-runtime readback remain on their registered trusted pool and fail closed when that capability is absent. A GitHub-hosted pass is evidence only for the portable suite that actually executed; it is never substituted for a skipped trusted-runtime test or production UAT.

## Later context-aware control flow

```text
WI execution/outcome/usage/TTFWA facts
  -> matched-phase evaluator
  -> recommendation artifact (shadow)
  -> optional suggestion
  -> explicit promotion decision
  -> Harness policy-gated continuation intent
  -> existing rollover/recovery
```

No second execution ledger. Shadow/suggestion remain read-only. Autonomous canary requires exact recommendation, policy opt-in, explicit human promotion, and evidence that continuation latency itself does not erase the expected benefit.

`@anchor symbol RolloverCoordinator`
`@anchor manual scl-resume-fast-path :: Scripts/scl-resume.py + Harness ResumePacket`
`@anchor manual scl-surface-route :: config/scl-surfaces.yaml + Scripts/scl-surface-route.py`
`@anchor manual actions-resource-resolver :: config/actions-resource-pools.yaml + Scripts/actions-resource-resolver.py`
`@anchor manual context-aware-recommendation-artifact :: exact WI/epoch/checkpoint/evidence digest`
`@anchor absent CONTINUATION_PROFILE`
