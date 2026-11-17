"""Resuming an observation read after a verified prefix never weakens validation."""
import pytest

from harness_rewrite.model import Rejected, digest
from harness_rewrite.observations import ObservationStore, PREFIX, OBJECTS
from tests_rewrite.test_production_ledger import Rig


def fact(n, size=0):
    body = {"schema": "computecost_collaboration_fact/v1", "kind": "usage",
            "entity_ref": "ccf1_" + digest(["entity", n]), "occurred_at": None,
            "producer": "test.v1", "supersedes": None, "payload": {"n": n, "pad": "x" * size}}
    body["fact_ref"] = "ccf1_" + digest(["fact", body])
    return {**body, "observed_at": "2026-10-01T00:00:00Z", "evidence": []}


def publish(store, count, *, big=()):
    batches = store.read()
    previous = batches[-1]["sha256"] if batches else None
    added = []
    for i in range(count):
        sequence = len(batches) + i + 1
        batch = {"schema": "computecost_ledger_transaction/v1", "sequence": sequence,
                 "recorded_at": f"2026-10-01T00:{sequence:02d}:00Z", "previous": previous,
                 "facts": [fact(sequence, 600_000 if i in big else 0)]}
        batch["sha256"] = digest(batch)
        previous = batch["sha256"]
        added.append(batch)
    store.append_many(added)


def verified_prefix(store):
    """What a caller persists after a complete read."""
    full = store.read()
    tree = store.ledger.tree(store.revision)
    chunks = sorted(p for p in store.ledger.dependencies if p.startswith(OBJECTS))
    return full, {"sequence": store.sequence, "watermark": store.watermark,
                  "objects": store.prefix_identity(tree, store.sequence, chunks), "chunks": chunks}


def test_resume_reads_only_later_batches_with_the_same_dependencies(tmp_path):
    rig = Rig(tmp_path)
    store = ObservationStore(rig.backend(rig.a))
    publish(store, 3, big={1})
    _, prefix = verified_prefix(store)
    assert prefix["chunks"]  # The packed batch's chunk objects are part of the identity.
    publish(store, 2, big={0})
    full = ObservationStore(rig.backend(rig.a))
    complete = full.read()
    resumed = ObservationStore(rig.backend(rig.a))
    tail = list(resumed.iter_batches(after=prefix))
    assert resumed.resumed == 3 and tail == complete[3:]
    assert (resumed.sequence, resumed.watermark) == (full.sequence, full.watermark)
    # Publication conflicts still watch every object a complete read consumed.
    assert resumed.ledger.dependencies == full.ledger.dependencies
    publish(resumed, 1)
    assert ObservationStore(rig.backend(rig.b)).read()[:5] == complete


def test_resume_at_head_reads_nothing_but_checks_the_head(tmp_path):
    rig = Rig(tmp_path)
    store = ObservationStore(rig.backend(rig.a))
    publish(store, 2)
    _, prefix = verified_prefix(store)
    again = ObservationStore(rig.backend(rig.a))
    assert list(again.iter_batches(after=prefix)) == [] and again.resumed == 2
    with pytest.raises(Rejected, match="head differs"):
        list(ObservationStore(rig.backend(rig.a)).iter_batches(after={**prefix, "watermark": "0" * 64}))


def test_wrong_prefix_digest_cannot_chain_to_later_batches(tmp_path):
    rig = Rig(tmp_path)
    store = ObservationStore(rig.backend(rig.a))
    publish(store, 2)
    _, prefix = verified_prefix(store)
    publish(store, 1)
    with pytest.raises(Rejected, match="order or digest"):
        list(ObservationStore(rig.backend(rig.a)).iter_batches(after={**prefix, "watermark": "0" * 64}))


@pytest.mark.parametrize("change", ["objects", "sequence", "chunks", "foreign_chunk", "shape"])
def test_unrecognised_prefix_reads_everything(tmp_path, change):
    rig = Rig(tmp_path)
    store = ObservationStore(rig.backend(rig.a))
    publish(store, 3, big={0})
    complete, prefix = verified_prefix(store)
    after = {"objects": {**prefix, "objects": "0" * 64},
             "sequence": {**prefix, "sequence": 7},
             "chunks": {**prefix, "chunks": []},
             "foreign_chunk": {**prefix, "chunks": [".harness/work-items/x.yaml"]},
             "shape": {**prefix, "extra": 1}}[change]
    reader = ObservationStore(rig.backend(rig.a))
    assert list(reader.iter_batches(after=after)) == complete
    assert reader.resumed == 0


def test_prefix_identity_names_wrappers_and_chunks(tmp_path):
    rig = Rig(tmp_path)
    store = ObservationStore(rig.backend(rig.a))
    publish(store, 2, big={1})
    _, prefix = verified_prefix(store)
    tree = store.ledger.tree(store.revision)
    changed = dict(tree)
    changed[prefix["chunks"][0]] = "0" * 40
    assert store.prefix_identity(changed, 2, prefix["chunks"]) != prefix["objects"]
    changed = dict(tree)
    changed[f"{PREFIX}{1:020d}.json"] = "0" * 40
    assert store.prefix_identity(changed, 2, prefix["chunks"]) != prefix["objects"]
    assert store.prefix_identity(tree, 1, prefix["chunks"]) != prefix["objects"]
