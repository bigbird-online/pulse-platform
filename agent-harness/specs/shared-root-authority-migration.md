# Shared-root authority migration

Status: normative

## Problem

A shared human work root can contain a complete, attributable dirty manifest
while its authority branch is being retired. Ordinary settlement integrates the
desired bytes back into that branch. That is unsafe when old generated ledgers
conflict with newer canonical records: overwriting authority loses canonical
history, while resetting the root loses the only exact endpoint snapshot.

## Contract

An owner-only `workspace_sync_authorization/v1` may select
`settlement_mode: preserve_then_adopt_authority`. The mode is valid only for a
complete safe inventory and has these invariants:

1. The Broker snapshots every selected dirty path and publishes an immutable
   `refs/heads/runtime/<repo>/human/<digest>` preservation ref before touching
   the endpoint root.
2. The protected authority branch is not mutated by the settlement. Its SHA
   must still equal the audited `authority_remote_sha` at publication and
   reconciliation.
3. The endpoint `HEAD` must be an ancestor of the audited authority. Committed
   local history outside authority blocks adoption; it is never reset.
4. After the preservation ref is read back byte-for-byte, the Broker restores
   the captured preimage and advances the endpoint by `merge --ff-only` to the
   audited authority SHA.
5. Completion requires a clean endpoint at that SHA, an exact preservation
   proof for every captured path, and a durable receipt that names the mode,
   preservation ref/SHA, authority SHA, inventory, and transaction.
6. The ordinary `integrate` mode remains the default. Direct `apply-safe` never
   enables authority adoption; the explicit owner-only authorization file is
   required.

`harness sync-workspace apply-authorized` executes only the workspace half of
this contract. Endpoint convergence and branch migration remain separate,
read-back operations.

## CollaborateWithAI migration slice

The final platform topology is `collaboratewithai-human-work-roots`, with the
ownermac, Vultr, and TCloud shared roots registered as `human_work_root`
endpoints on `refs/heads/main`.

Migration order is fixed:

1. settle ordinary ownermac dirt on `feature/doc-para`;
2. preserve-then-adopt the conflicting Vultr and TCloud dirty manifests;
3. merge the resulting `feature/doc-para` lineage into GitHub `main`;
4. enroll the same topology on `feature/doc-para`, then use
   `migrate-authority-branch` with the versioned `main` manifest;
5. read back all three roots as clean, on `main`, at one GitHub SHA.

No step may reset, stash, clean, rebase, force-push, or edit a shared root
directly.

## Timeout and idempotent recovery

Endpoint Git operations run against large shared roots and can cross a bounded
transport or runner timeout after the remote side has already completed the
requested transition. A timeout therefore means `outcome_unknown`, not
`mutation_absent`.

Before retrying branch migration or convergence, the controller must read back:

1. the endpoint's current branch, exact `HEAD`, cleanliness and remote identity;
2. the topology registry's authority branch and digest;
3. any preservation, settlement or migration receipt already emitted; and
4. GitHub's current authority SHA.

If an endpoint is already clean on the requested branch and its `HEAD` is the
audited authority or a permitted fast-forward descendant, an idempotent retry
must accept that endpoint instead of treating the earlier timeout as a second
mutation. Registry publication occurs only after every endpoint readback
converges. A later GitHub `main` advance does not invalidate the historical
migration receipt: the receipt names the cutover SHA, while current convergence
must independently adopt and report the latest audited `main` SHA.

Completion is the conjunction of preservation proof, clean endpoint readback,
one registry digest and current authority adoption. Process exit, timeout,
installed runtime metadata or one endpoint's success is insufficient.
