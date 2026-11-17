# Outcome verification publication policy

A Host may publish the independent result for one submitted candidate without receiving the business owner's general namespace write permission.

`verification_publications` is an explicit list in site policy v1 or v2. Each entry contains exactly `work_item_id`, `owner`, `verifier` and `candidate_sha256`, with valid WI/namespace identities and a 64-character lowercase SHA-256. Unknown grant fields and malformed values fail closed.

The grant applies only to `verify-result`, its read-only `verify-admission`, and publication-source revalidation. Ordinary owner namespace writes, WI creation, handoff, review and terminal actions retain their existing checks. The grant does not change writable namespaces, excluded agents, tenant namespaces or cross-site targets.

Current canonical candidate identity, producer/verifier independence, the matching accepted assignment, registered source carrier, evidence hashes and Broker admission remain required. The same grant and candidate are checked before dispatch, during source revalidation and under the final state-write lock. Read-only admission may describe a pending assignment; it grants no ACK, workspace or publication authority.

The Broker projection is an output ledger, not a registered verification source. After binding the exact live transaction, actor, assignment and projected input bytes, the result entry revalidates the transaction’s original repository through the same registered-source admission as the outer publisher. A temporary projection must not select an older service ledger or replace the source carrier. Changed source evidence, policy, candidate or input bytes still reject publication.

A terminal-gated `work_item.outcome_accepted` event retains the existing SCL freeze when its WI, acceptance fingerprint, independent verifier, accepted verdict, review requirement and unchanged `awaiting_verification` status match the current candidate. It is an audit of independent acceptance, not a new lifecycle transition. Review and closeout still validate the receipt themselves; this classification neither accepts evidence nor grants terminal authority. Actual state transitions, changed criteria, inconsistent event identity and future event clocks remain rejected. The registered and integrated service routes consume this same interpretation.

Policies with no publication grants retain their previous effective digest. Registry-derived v2 policies retain the explicit grant list while preserving all namespace and cross-site limits.

Regression coverage must reject wrong WI, owner, verifier and candidate, malformed/extra grant fields, general namespace writes and changed publication policy. Both pending and accepted admission remain read-only; all four Outcome verdicts retain publication revalidation.
