"""Notice facts and channel effects through the same kernel; no real transport."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

import pytest

from harness_rewrite import Command, Query, SourceInput
from harness_rewrite.delivery import WORKER_INTENTS
from harness_rewrite.effects import Effects
from harness_rewrite.model import NotificationRoute, digest
from harness_rewrite.notifications import INTENTS
from tests_rewrite.test_kernel import Host, Kernel, OWNER, start, code

ROUTE = NotificationRoute("original-route", "telegram", "a" * 64)
SOURCE = SourceInput("notice-original", "native_agent", b"original notice input")


class Notices:
    def __init__(self, tmp_path, route=ROUTE):
        self.host = Host()
        self.kernel = Kernel(
            tmp_path / "facts.sqlite",
            self.host.authenticate,
            clock=lambda: "2026-10-07T16:30:00Z",
        )
        self.wi = start(self.kernel)
        self.route = route
        self.context = replace(
            OWNER,
            allowed_intents=OWNER.allowed_intents | INTENTS,
            source=SOURCE,
            notification_routes=(route,),
        )
        self.host.contexts["owner"] = self.context
        self.counter = 0

    def send(self, intent, data, request=None, handle="owner"):
        self.counter += 1
        return self.kernel.execute(
            Command(request or f"n{self.counter}", intent, data, self.wi), handle
        )

    def record(self, text="Synthetic notice", request=None, **changes):
        return self.send(
            "record_notice",
            {
                "route_id": self.route.route_id,
                "resources": ["repo/src"],
                "text": text,
                "purpose": "operational",
                "attention": None,
                **changes,
            },
            request=request,
        )

    def worker(self, artifacts):
        self.host.contexts["worker"] = replace(
            self.context,
            actor="delivery-worker",
            session="notice-worker",
            source=None,
            allowed_intents=WORKER_INTENTS,
            effect_kinds=frozenset({"notification_send"}),
            artifacts=artifacts,
        )
        return "worker"

    def state(self):
        return self.kernel.inspect(Query(self.wi), "owner")

    def query(self, **kw):
        return self.kernel.inspect(Query(self.wi, view="notices", **kw), "owner")[
            "result"
        ]["items"]

    def flush(self, window="2026-10-07", request=None):
        return self.send(
            "flush_notices",
            {"route_id": self.route.route_id, "window": window},
            request,
        )

    def resolve(self, key):
        return self.send(
            "resolve_notice", {"notice_id": key, "reason": "Original issue resolved"}
        )

    def drain(self, adapter, fault=None):
        return Effects(self.kernel, adapter, self.worker, fault=fault).drain(self.wi)


def original(response):
    assert response["status"] == "local_committed", response
    return response["result"]["original_request_result"]


class Channel:
    """A receipt-capable test channel with an irreversible send counter."""

    def __init__(self):
        self.sent = []
        self.receipts = {}

    def apply(self, effect, content):
        snapshot = json.loads(content)
        assert snapshot == effect["snapshot"]
        self.sent.append(snapshot["text"])
        receipt = {
            "channel": snapshot["route"]["channel"],
            "recipient_digest": snapshot["route"]["recipient_digest"],
            "route_digest": snapshot["route_digest"],
            "text_sha256": snapshot["text_sha256"],
            "external_message_id": f"test-message-{len(self.sent)}",
        }
        self.receipts[effect["effect_id"]] = receipt
        return "success", receipt

    def readback(self, effect):
        receipt = self.receipts.get(effect["effect_id"])
        return (
            ("success", receipt)
            if receipt
            else ("unknown", {"reason": "receipt_unavailable"})
        )


def test_immediate_delivery_resolution_and_exact_retry_are_separate(tmp_path):
    n = Notices(tmp_path)
    declared = n.record(request="original")
    key = original(declared)["notice_id"]
    assert n.query()["items"][0]["source_revision"] == 2
    assert len(n.state()["effects"]) == 1
    assert original(n.record(request="original")) == original(declared)
    assert (
        code(n.record(text="Different bytes", request="original")) == "request_conflict"
    )
    assert (
        code(
            n.send("begin_effect", {"effect_id": n.state()["effects"][0]["effect_id"]})
        )
        == "authority_denied"
    )
    channel = Channel()
    assert all(r["status"] == "local_committed" for r in n.drain(channel))
    assert channel.sent == ["Synthetic notice"]
    assert n.query(status="open")["items"][0]["delivery_status"] == "observed_success"
    assert n.drain(channel) == []
    original(n.resolve(key))
    assert n.query(status="open")["items"] == []
    assert len(n.query(status="resolved")["items"]) == 1
    assert n.state()["work_item"]["business_progress"] == "active"
    assert channel.sent == ["Synthetic notice"]


@pytest.mark.parametrize("stage", ["after_attempt", "after_send", "before_observation"])
def test_crash_preserves_one_attempt_and_missing_receipt_is_unknown(tmp_path, stage):
    n = Notices(tmp_path)
    key = original(n.record())["notice_id"]
    channel = Channel()

    class Crash(BaseException):
        pass

    def fault(point):
        if point == stage:
            raise Crash()

    with pytest.raises(Crash):
        n.drain(channel, fault)
    original(n.resolve(key))
    n.drain(channel)
    effect = n.state()["effects"][0]
    assert len(effect["attempts"]) == 1
    assert effect["status"] == (
        "unknown" if stage == "after_attempt" else "observed_success"
    )
    n.drain(channel)
    assert len(channel.sent) == (0 if stage == "after_attempt" else 1)


def test_lost_receipt_after_real_send_does_not_resend_or_infer_absence(tmp_path):
    n = Notices(tmp_path)
    original(n.record())
    channel = Channel()

    class Crash(BaseException):
        pass

    def fault(point):
        if point == "after_send":
            raise Crash()

    with pytest.raises(Crash):
        n.drain(channel, fault)
    channel.receipts.clear()
    n.drain(channel)
    n.drain(channel)
    assert n.query(status="unresolved_send")["items"][0]["delivery_status"] == "unknown"
    assert len(channel.sent) == 1
    assert len(n.state()["effects"][0]["attempts"]) == 1


def test_concurrent_daily_flush_captures_disjoint_bounded_original_windows(tmp_path):
    n = Notices(
        tmp_path,
        replace(
            ROUTE, mode="daily", timezone="Asia/Taipei", max_items=2, max_bytes=128
        ),
    )
    keys = [
        original(n.record(text="合成" * 12 + str(i)))["notice_id"] for i in range(3)
    ]
    assert n.state()["effects"] == []
    assert len(n.query(status="backlog")["items"]) == 3
    assert n.query()["items"][0]["window"] == "2026-10-08"
    assert original(n.flush("2026-10-07"))["batch_ids"] == []
    n.kernel._clock = lambda: "2026-10-10T00:00:00Z"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda key: n.flush("2026-10-08", request=key), ["f1", "f2"])
        )
    assert all(r["status"] == "local_committed" for r in results)
    effects = n.state()["effects"]
    # Producer observation hides private dispatch snapshots.
    assert all("snapshot" not in e for e in effects)
    assert len(effects) == 3
    channel = Channel()
    n.drain(channel)
    assert len(channel.sent) == 3 and all(len(t.encode()) <= 128 for t in channel.sent)
    assert {i["notice_id"] for i in n.query()["items"]} == set(keys)
    assert original(n.flush("2026-10-08"))["batch_ids"] == []
    original(n.record(text="Late new notice"))
    assert len(n.query(status="backlog")["items"]) == 1
    assert original(n.flush("2026-10-08"))["batch_ids"] == []
    assert len(channel.sent) == 3


def test_resolution_before_send_suppresses_whole_immutable_batch_then_rebatches(
    tmp_path,
):
    n = Notices(tmp_path, replace(ROUTE, mode="daily"))
    first = original(n.record(text="Resolved member"))["notice_id"]
    original(n.record(text="Still open"))
    original(n.flush())
    original(n.resolve(first))
    channel = Channel()
    n.drain(channel)
    assert channel.sent == []
    assert n.state()["effects"][0]["status"] == "cancelled"
    assert len(n.query(status="backlog")["items"]) == 1
    original(n.flush())
    n.drain(channel)
    assert channel.sent == ["Still open"]
    assert len(n.state()["effects"]) == 2


def test_authority_change_suppresses_unstarted_attention_without_resolving(tmp_path):
    n = Notices(tmp_path)
    original(n.record())
    stop = {"reason": "Human stop"}
    n.host.contexts["owner"] = replace(
        n.context,
        source=SourceInput(
            "human-stop",
            "native_human",
            b"Stop the original work",
            "stop",
            digest({"intent": "stop", "work_item_id": n.wi, "input": stop}),
        ),
    )
    original(n.send("stop", stop))
    channel = Channel()
    n.drain(channel)
    assert channel.sent == []
    assert n.state()["effects"][0]["status"] == "cancelled"
    item = n.query(status="open")["items"][0]
    assert item["resolution"] is None and item["current_attention"] is False


def test_route_change_cannot_retarget_original_send_or_retry(tmp_path):
    n = Notices(tmp_path)
    original(n.record(request="original"))
    n.context = replace(
        n.context, notification_routes=(replace(ROUTE, recipient_digest="b" * 64),)
    )
    n.host.contexts["owner"] = n.context
    assert code(n.record(request="original")) == "request_conflict"
    channel = Channel()
    result = n.drain(channel)
    assert code(result[0]) == "authority_changed"
    assert channel.sent == [] and n.state()["effects"][0]["attempts"] == []


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"route_id": "not-installed"}, "authority_denied"),
        ({"resources": ["outside"]}, "authority_denied"),
        ({"text": ""}, "invalid_input"),
        ({"text": "x" * 4097}, "invalid_input"),
        ({"recipient": "unapproved"}, "invalid_input"),
        ({"purpose": "high"}, "invalid_input"),
        (
            {"purpose": "candidate_review", "attention": {"candidate_id": "invented"}},
            "domain_conflict",
        ),
        ({"attention": {"reason": "need help"}}, "authority_denied"),
    ],
)
def test_invalid_or_unauthorized_notice_has_no_effect(tmp_path, changes, expected):
    n = Notices(tmp_path)
    before = n.state()["work_item"]["local_revision"]
    assert code(n.record(**changes)) == expected
    assert n.state()["work_item"]["local_revision"] == before
    assert n.state()["effects"] == []


def test_explicit_attention_needs_exact_original_human_decision(tmp_path):
    n = Notices(tmp_path, replace(ROUTE, audience="owner_attention"))
    data = {
        "route_id": ROUTE.route_id,
        "resources": ["repo/src"],
        "text": "Synthetic decision",
        "purpose": "explicit_decision",
        "attention": {"reason": "Choose synthetic case"},
    }
    assert code(n.send("record_notice", data)) == "source_invalid"
    n.host.contexts["owner"] = replace(
        n.context,
        source=SourceInput(
            "human-source",
            "native_human",
            b"Request this exact decision",
            "record_notice",
            digest({"intent": "record_notice", "work_item_id": n.wi, "input": data}),
        ),
    )
    original(n.send("record_notice", data))
    assert len(n.query(status="open")["items"]) == 1


def test_http_success_without_exact_actual_channel_receipt_cannot_settle(tmp_path):
    n = Notices(tmp_path)
    original(n.record())

    class BadChannel(Channel):
        def apply(self, effect, content):
            self.sent.append("sent but missing receipt")
            return "success", {"ok": True}

    channel = BadChannel()
    assert code(n.drain(channel)[0]) == "source_invalid"
    assert n.state()["effects"][0]["status"] == "attempt_started"
    n.drain(channel)
    assert n.state()["effects"][0]["status"] == "unknown"
    assert len(channel.sent) == 1


def test_historical_paginated_queries_are_readonly_and_do_not_resolve(tmp_path):
    n = Notices(tmp_path)
    key = original(n.record(text="one"))["notice_id"]
    revision = n.state()["work_item"]["local_revision"]
    original(n.record(text="two"))
    original(n.resolve(key))
    before = n.kernel._database.read_bytes()
    page = n.query(status="open", limit=1)
    assert len(page["items"]) == 1 and page["items"][0]["text"] == "two"
    old = n.query(revision=revision, status="open")
    assert len(old["items"]) == 1 and old["items"][0]["text"] == "one"
    assert n.kernel._database.read_bytes() == before
    n.host.contexts["unreadable"] = replace(n.context, readable_wis=frozenset())
    assert (
        code(n.kernel.inspect(Query(n.wi, view="notices"), "unreadable"))
        == "authority_denied"
    )


def test_resolution_between_durable_attempt_and_network_call_prevents_send(tmp_path):
    n = Notices(tmp_path)
    key = original(n.record())["notice_id"]
    channel = Channel()

    def fault(point):
        if point == "after_attempt":
            original(n.resolve(key))

    results = n.drain(channel, fault)
    assert code(results[0]) == "authority_changed"
    assert channel.sent == []
    assert n.state()["effects"][0]["status"] == "attempt_started"
    n.drain(channel)
    assert n.query(status="unresolved_send")["items"][0]["delivery_status"] == "unknown"


def test_no_candidate_cannot_manufacture_review_attention(tmp_path):
    n = Notices(tmp_path, replace(ROUTE, audience="owner_attention"))
    assert (
        code(n.record(purpose="candidate_review", attention={"candidate_id": None}))
        == "domain_conflict"
    )
    assert n.state()["effects"] == []


def test_real_pending_candidate_review_is_suppressed_after_approval(tmp_path):
    from tests_rewrite.test_review import produce, assign, verify, approve, VERIFIER
    from tests_rewrite.test_workflow import Rig, BASE

    rig = Rig(tmp_path)
    rig.contexts["verifier"] = VERIFIER
    candidate = produce(rig)
    original(verify(rig, candidate, assign(rig, candidate)))
    route = replace(ROUTE, audience="owner_attention")
    context = replace(
        BASE,
        allowed_intents=BASE.allowed_intents | INTENTS,
        notification_routes=(route,),
        source=SOURCE,
    )
    rig.contexts["owner"] = context
    original(
        rig.send(
            "record_notice",
            {
                "route_id": route.route_id,
                "resources": ["repo"],
                "text": "Review captured candidate",
                "purpose": "candidate_review",
                "attention": {"candidate_id": candidate},
            },
        )
    )
    original(approve(rig, candidate))

    def worker(artifacts):
        rig.contexts["worker"] = replace(
            context,
            actor="delivery-worker",
            session="worker",
            allowed_intents=WORKER_INTENTS,
            effect_kinds=frozenset({"notification_send"}),
            artifacts=artifacts,
        )
        return "worker"

    channel = Channel()
    Effects(rig.kernel, channel, worker).drain(rig.wi)
    assert channel.sent == []
    assert rig.state()["effects"][-1]["status"] == "cancelled"


def test_proven_absence_recovers_original_effect_with_new_attempt_only(tmp_path):
    n = Notices(tmp_path)
    original(n.record(request="declared"))

    class AbsentChannel(Channel):
        def readback(self, effect):
            if not self.sent:
                return "absence", {"reason": "test_channel_proved_original_send_absent"}
            return super().readback(effect)

    channel = AbsentChannel()

    class Crash(BaseException):
        pass

    def fault(point):
        if point == "after_attempt":
            raise Crash()

    with pytest.raises(Crash):
        n.drain(channel, fault)
    n.drain(channel)
    effect = n.state()["effects"][0]
    assert effect["status"] == "observed_absence"
    n.drain(channel)
    again = n.state()["effects"][0]
    assert again["effect_id"] == effect["effect_id"] and len(again["attempts"]) == 2
    assert channel.sent == ["Synthetic notice"]
    original(n.record(request="declared"))
    n.drain(channel)
    assert channel.sent == ["Synthetic notice"]


def test_unrelated_route_changes_do_not_conflict_with_original_notice(tmp_path):
    n = Notices(tmp_path)
    first = original(n.record(request="original"))
    n.context = replace(
        n.context, notification_routes=(ROUTE, replace(ROUTE, route_id="another"))
    )
    n.host.contexts["owner"] = n.context
    assert original(n.record(request="original")) == first
    assert len(n.state()["effects"]) == 1
