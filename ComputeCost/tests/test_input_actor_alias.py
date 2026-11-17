"""An input-route identity follows a Relay alias appended for its time."""
from pulse_compute_cost.receipt_sources import _input_actor_alias_corrections, actor_receipt, usage_receipt
from pulse_compute_cost.receipts import current_receipts

SHA = "0" * 64
HISTORY = [{"alias": "main", "principal": "main", "effective_from": "2026-09-22T08:00:00Z", "config_sha256": SHA},
           {"alias": "main", "principal": "duke", "effective_from": "2026-10-21T14:57:09Z", "config_sha256": "1" * 64}]


def usage(ident, at):
    return usage_receipt("pi", ["session", ident], {"input": 1, "output": 1}, at=at, observed=at,
                         model="openai/test", mapping="pi_native.v1", native_scope_ref="ccf1_" + "a" * 64)


def test_old_alias_identity_is_superseded_only_inside_the_mapping():
    later, earlier, other = usage("later", "2026-10-25T05:23:43Z"), usage("earlier", "2026-10-20T01:00:00Z"), usage("other", "2026-10-25T05:24:00Z")
    claims = [actor_receipt(row, node="ownermac", executor="main", entrypoint="telegram", observed=row.occurred_at,
                            evidence=("ccf1_" + "b" * 64,)) for row in (later, earlier)]
    claims.append(actor_receipt(other, node="ownermac", executor="pulse", entrypoint="telegram",
                                observed=other.occurred_at, evidence=("ccf1_" + "b" * 64,)))
    active, _ = current_receipts([later, earlier, other, *claims])
    targets = {later.entity_ref, earlier.entity_ref, other.entity_ref}
    revised = _input_actor_alias_corrections(active, targets, HISTORY, "2026-10-25T10:00:00Z")
    assert [(r.payload["target_ref"], r.payload["executor"], r.supersedes) for r in revised] == [
        (later.entity_ref, "duke", claims[0].fact_ref)]
    assert revised[0].payload["context"] == claims[0].payload["context"]
    heads, conflicts = current_receipts([*claims, *revised])
    assert not conflicts and {r.payload["executor"] for r in heads} == {"duke", "main", "pulse"}
    # A corrected head is not revised again.
    assert _input_actor_alias_corrections(heads, targets, HISTORY, "2026-10-25T11:00:00Z") == []
