from dataclasses import replace

import pytest

from harness_rewrite import Query
from harness_rewrite.model import Command
from harness_rewrite.delivery import INTENTS, WORKER_INTENTS
from harness_rewrite.effects import Effects, FileDelivery
from tests_rewrite.test_review import (
    ARTIFACT,
    VERIFIER,
    produce,
    assign,
    verify,
    approve,
)
from tests_rewrite.test_workflow import Rig, BASE, original, error, node


@pytest.fixture
def ready(tmp_path):
    rig = Rig(tmp_path)
    rig.contexts["owner"] = replace(
        BASE, allowed_intents=BASE.allowed_intents | INTENTS
    )
    rig.contexts["verifier"] = VERIFIER
    candidate = produce(rig, extra=[node("ship", kind="delivery", role="delivery")])
    original(verify(rig, candidate, assign(rig, candidate)))
    original(approve(rig, candidate))
    delivery = original(
        rig.send(
            "declare_delivery",
            {
                "candidate_id": candidate,
                "deliveries": [
                    {
                        "artifact": ARTIFACT.reference(),
                        "target": "result.txt",
                        "required": True,
                        "node_id": "ship",
                    }
                ],
            },
        )
    )["effect_ids"][0]
    destination = tmp_path / "destination"
    destination.mkdir()

    def worker(artifacts):
        rig.contexts["worker"] = replace(
            BASE,
            actor="delivery-worker",
            session="worker-session",
            allowed_intents=WORKER_INTENTS,
            effect_kinds=frozenset({"deliver_artifact"}),
            artifacts=artifacts,
        )
        return "worker"

    return rig, candidate, delivery, destination, worker


def test_required_delivery_is_real_fixed_bytes_and_retries_keep_original_identity(
    ready,
):
    rig, candidate, effect_id, destination, worker = ready
    assert error(rig.send("complete", {"candidate_id": candidate})) == "domain_conflict"
    assert (
        error(rig.send("begin_effect", {"effect_id": effect_id})) == "authority_denied"
    )
    results = Effects(rig.kernel, FileDelivery(destination), worker).drain(rig.wi)
    assert all(r["status"] == "local_committed" for r in results)
    assert (destination / "result.txt").read_bytes() == ARTIFACT.content
    assert len(rig.state()["effects"][0]["attempts"]) == 1
    assert Effects(rig.kernel, FileDelivery(destination), worker).drain(rig.wi) == []
    original(rig.send("complete", {"candidate_id": candidate}))
    assert rig.state()["work_item"]["business_progress"] == "completed"


@pytest.mark.parametrize("stage", ["after_attempt", "after_send", "before_observation"])
def test_interrupted_effect_reads_back_original_attempt_without_duplicate_send(
    ready, stage
):
    rig, _, _, destination, worker = ready

    class Interrupted(BaseException):
        pass

    def fail(at):
        if at == stage:
            raise Interrupted()

    with pytest.raises(Interrupted):
        Effects(rig.kernel, FileDelivery(destination), worker, fault=fail).drain(rig.wi)
    assert rig.state()["effects"][0]["status"] == "attempt_started"
    Effects(rig.kernel, FileDelivery(destination), worker).drain(rig.wi)
    effect = rig.state()["effects"][0]
    assert len(effect["attempts"]) == 1
    if stage == "after_attempt":
        assert effect["status"] == "observed_absence"
        Effects(rig.kernel, FileDelivery(destination), worker).drain(rig.wi)
        assert len(rig.state()["effects"][0]["attempts"]) == 2
    else:
        assert effect["status"] == "observed_success"
    assert (destination / "result.txt").read_bytes() == ARTIFACT.content


def test_conflicting_target_and_redirection_preserve_unowned_content(ready):
    rig, candidate, _, destination, worker = ready
    (destination / "result.txt").write_text("unowned content")
    Effects(rig.kernel, FileDelivery(destination), worker).drain(rig.wi)
    assert rig.state()["effects"][0]["status"] == "observed_failure"
    assert (destination / "result.txt").read_text() == "unowned content"
    assert error(rig.send("complete", {"candidate_id": candidate})) == "domain_conflict"
    Effects(rig.kernel, FileDelivery(destination), worker).drain(rig.wi)
    assert len(rig.state()["effects"][0]["attempts"]) == 1


def test_fixed_artifact_read_does_not_expose_source_or_other_wi(ready):
    rig, _, _, _, _ = ready
    response = rig.state(
        query=Query(rig.wi, view="artifact", artifact_ref=ARTIFACT.reference())
    )
    assert response["result"]["artifact"]["reference"] == ARTIFACT.reference()
    changed = {**ARTIFACT.reference(), "version": "other"}
    assert (
        error(rig.state(query=Query(rig.wi, view="artifact", artifact_ref=changed)))
        == "authority_denied"
    )


@pytest.mark.parametrize("stage", ["after_attempt", "after_send"])
def test_policy_switch_fences_original_executor_without_retargeting_pending_effect(
    ready, stage
):
    from harness_rewrite import Kernel
    from harness_rewrite.model import writer_identity

    rig, _, _, destination, worker = ready
    old_admission = writer_identity("a" * 64, {"configuration": "old"})
    new_admission = writer_identity("a" * 64, {"configuration": "new"})
    rig.kernel._writer_id = old_admission
    with rig.kernel._connection(write=True, activation=True) as conn:
        conn.execute(
            "CREATE TABLE runtime_binding (singleton INTEGER PRIMARY KEY, program TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO runtime_binding VALUES(1,?)", (old_admission,))
        conn.commit()

    class ObservedDelivery(FileDelivery):
        def __init__(self, root):
            super().__init__(root)
            self.sends = 0
            self.reads = 0

        def apply(self, effect, content):
            self.sends += 1
            return super().apply(effect, content)

        def readback(self, effect):
            self.reads += 1
            return super().readback(effect)

    adapter = ObservedDelivery(destination)

    def switch(at):
        if at == stage:
            with rig.kernel._connection(write=True, activation=True) as conn:
                conn.execute("UPDATE runtime_binding SET program=?", (new_admission,))
                conn.commit()

    Effects(rig.kernel, adapter, worker, fault=switch).drain(rig.wi)
    assert adapter.sends == (1 if stage == "after_send" else 0)
    before = (adapter.sends, adapter.reads)
    Effects(rig.kernel, adapter, worker).drain(rig.wi)
    assert (adapter.sends, adapter.reads) == before
    successor = Kernel(
        rig.kernel._database, rig.contexts.__getitem__, writer_id=new_admission
    )
    Effects(successor, adapter, worker).drain(rig.wi)
    if stage == "after_attempt":
        assert adapter.sends == 0
        Effects(successor, adapter, worker).drain(rig.wi)
    assert adapter.sends == 1
    assert (destination / "result.txt").read_bytes() == ARTIFACT.content


def test_failed_deliveries_have_durable_budget_and_owner_repair_entry(ready):
    from datetime import datetime, timezone, timedelta
    rig, _, effect_id, _, worker = ready
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    rig.kernel._clock = lambda: now.isoformat()

    class Missing:
        calls = 0
        def apply(self, effect, content):
            self.calls += 1
            return 'absence', {'reason': 'destination_unavailable'}
        def readback(self, effect):
            return 'absence', {'reason': 'destination_unavailable'}

    adapter = Missing()
    runner = Effects(rig.kernel, adapter, worker)
    for _ in range(8):
        runner.drain(rig.wi)
        now += timedelta(minutes=6)
    assert adapter.calls == 8
    assert runner.drain(rig.wi) == [], 'an exhausted budget is not resubmitted every pass'
    failed = rig.kernel.execute(
        Command('send.' + effect_id + '.8', 'begin_effect', {'effect_id': effect_id}, rig.wi),
        worker(()),
    )
    assert error(failed) == 'effect_retry_exhausted'
    assert adapter.calls == 8
    state = rig.state()['effects'][0]
    assert state['retry']['attempts_remaining'] == 0
    retried = rig.send('retry_effect', {'effect_id': effect_id, 'reason': 'destination repaired'})
    assert retried.get('error') is None, retried.get('error')
    original(retried)
    runner.drain(rig.wi)
    assert adapter.calls == 9
    assert len(rig.state()['effects'][0]['attempts']) == 9


def test_repeated_absence_backs_off_without_new_external_send(ready):
    from datetime import datetime, timezone
    rig, _, _, _, worker = ready
    rig.kernel._clock = lambda: datetime(2026, 10, 13, tzinfo=timezone.utc).isoformat()

    class Missing:
        calls = 0
        def apply(self, effect, content):
            self.calls += 1
            return 'absence', {'reason': 'destination_unavailable'}
        def readback(self, effect):
            return 'absence', {'reason': 'destination_unavailable'}

    adapter = Missing()
    runner = Effects(rig.kernel, adapter, worker)
    runner.drain(rig.wi)
    runner.drain(rig.wi)
    result = runner.drain(rig.wi)
    assert error(result[0]) == 'effect_retry_backoff'
    assert result[0]['error']['retryable'] is True
    assert adapter.calls == 2


def test_human_cancels_an_optional_requested_file_delivery(ready):
    rig, candidate, _, _, _ = ready
    optional = original(
        rig.send(
            "declare_delivery",
            {
                "candidate_id": candidate,
                "deliveries": [
                    {
                        "artifact": ARTIFACT.reference(),
                        "target": "optional.txt",
                        "required": False,
                        "node_id": None,
                    }
                ],
            },
        )
    )["effect_ids"][0]
    original(
        rig.approve(
            "cancel_effect",
            {"effect_id": optional, "reason": "Optional copy not needed"},
            actor=rig.contexts["owner"],
        )
    )
    effect = next(e for e in rig.state()["effects"] if e["effect_id"] == optional)
    assert effect["status"] == "cancelled"


def test_retry_refusal_names_the_path_for_each_unsettled_status():
    from harness_rewrite.delivery import retry_refusal

    def reason(status, kind):
        return retry_refusal({'effect_id': 'op_x.0', 'status': status, 'effect_kind': kind})

    verifier = reason('observed_failure', 'verification_dispatch')
    assert 'observed_failure' in verifier and 'resume' in verifier and 'verifier session' in verifier
    merge = reason('observed_failure', 'merge_pull_request')
    assert 'new head' in merge and 'new request_id' in merge and 'immediately' not in merge
    unknown = reason('unknown', 'notification_send')
    assert 'Read it back first' in unknown and 'observed_absence' in unknown
    assert 'settled' in reason('observed_success', 'workspace_allocate')
