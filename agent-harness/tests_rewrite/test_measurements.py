"""Actual captured bytes, missing counters and immutable observation attribution."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from harness_rewrite import Command, Kernel, Query, SourceInput
from harness_rewrite.domain import INTENTS
from harness_rewrite.model import Rejected, digest, encoded
from harness_rewrite.native_host import NativeHost
from harness_rewrite import native_usage
from tests_rewrite.test_native_host import native as native
from tests_rewrite.test_lightweight import light, record
from tests_rewrite.test_workflow import BASE, DEFINITION, Rig, graph, original, error
from tests_rewrite.test_review import VERIFIER, PROOF, produce, assign, verify

HEADER = {"type": "session", "id": "private-provider-native-id"}


def row(usage=None, model="model-a"):
    return {
        "type": "message",
        "message": {
            "role": "assistant",
            "model": model,
            "provider": "provider",
            "usage": usage,
            "content": "PRIVATE_MESSAGE_NOT_PUBLIC",
        },
    }


def log(tmp_path, *messages):
    path = tmp_path / "original.jsonl"
    path.write_bytes(b"".join(encoded(x) + b"\n" for x in [HEADER, *messages]))
    return path


def admission(rig, path, start=0, end=None, attempt=None, context=BASE):
    intervals = rig.state()["result"]["current_observation"]["measurements"][
        "intervals"
    ]
    if not intervals:
        rig.contexts["open-usage"] = replace(
            BASE, allowed_intents=INTENTS, native_cursor=0
        )
        interval_id = original(
            rig.send(
                "open_usage_interval", {"task_attempt": attempt}, handle="open-usage"
            )
        )["interval_id"]
    else:
        interval_id = intervals[0]["interval_id"]
    end = path.stat().st_size if end is None else end
    artifact, packet = native_usage.capture(path, HEADER, start, end)
    data = {
        "interval_id": interval_id,
        "task_attempt": attempt,
        "start_byte": start,
        "end_byte": end,
    }
    ctx = replace(
        context,
        allowed_intents=INTENTS,
        artifacts=(artifact,),
        usage_snapshot=packet,
        source=SourceInput(
            "original-usage." + digest(packet),
            "native_usage",
            encoded(packet),
            "record_usage",
            digest({"intent": "record_usage", "work_item_id": rig.wi, "input": data}),
        ),
    )
    rig.contexts["usage"] = ctx
    return data


def values(rig):
    return rig.state(query=Query(rig.wi, view="measurements"))["result"]["items"]


def test_actual_usage_zero_missing_partial_model_changes_and_private_originals(
    tmp_path,
):
    rig = light(tmp_path)
    assert values(rig)["coverage"] == "unavailable"
    path = log(
        tmp_path,
        row(
            {
                "input": 0,
                "output": 5,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 5,
                "cost": {"total": 0.0, "currency": "USD"},
            }
        ),
        row(None),
        row({"input": 3, "output": None, "cost": {"total": 0.2}}, model="model-b"),
    )
    data = admission(rig, path)
    result = rig.send("record_usage", data, handle="usage", request="capture")
    original(result)
    observed = values(rig)
    item = observed["usage"][0]
    assert item["coverage"] == "partial"
    a, b = item["groups"]
    assert a["tokens"]["input"] == {
        "value": 0,
        "observed_messages": 1,
        "coverage": "partial",
    }
    assert b["tokens"]["output"] == {
        "value": None,
        "observed_messages": 0,
        "coverage": "unavailable",
    }
    assert a["costs"]["USD"]["value"] == 0
    assert b["costs"] == {} and b["cost_coverage"] == "unavailable"
    public = encoded(observed)
    assert (
        b"PRIVATE_MESSAGE_NOT_PUBLIC" not in public
        and b"private-provider-native-id" not in public
        and b"model-a" not in public
    )
    fixed = rig.kernel._database.read_bytes()
    assert original(
        rig.send("record_usage", data, handle="usage", request="capture")
    ) == original(result)
    assert rig.kernel._database.read_bytes() == fixed
    assert (
        error(rig.send("record_usage", data, handle="usage", request="overlap"))
        == "domain_conflict"
    )
    rig.contexts["reader"] = replace(BASE, actor="another-reader")
    assert (
        error(
            rig.state(
                query=Query(rig.wi, view="artifact", artifact_ref=item["evidence"]),
                handle="reader",
            )
        )
        == "authority_denied"
    )


@pytest.mark.parametrize(
    "usage",
    [
        {"input": True},
        {"input": -1},
        {"input": 1.5},
        {"input": 2**63},
        {"futureCounters": 1},
        {"reasoning": True},
        {"reasoning": -1},
        {"cacheWrite1h": 1.5},
        {"cost": {"total": -1}},
        {"cost": {"total": 1, "currency": "usd"}},
    ],
)
def test_malformed_or_unknown_usage_never_becomes_zero(tmp_path, usage):
    path = log(tmp_path, row(usage))
    with pytest.raises(Rejected) as exc:
        native_usage.capture(path, HEADER, 0, path.stat().st_size)
    assert exc.value.code == "source_invalid"


@pytest.mark.parametrize("detail", ["reasoning", "cacheWrite1h"])
def test_native_pi_detail_counters_do_not_change_totals_or_legacy_retry(
    tmp_path, detail
):
    rig = light(tmp_path)
    base = {"input": 2, "output": 3, "cacheRead": 4, "cacheWrite": 5, "totalTokens": 14}
    path = log(tmp_path, row(base))
    first_data = admission(rig, path)
    legacy_packet = rig.contexts["usage"].usage_snapshot
    assert legacy_packet["extraction"] == "deterministic-native-pi-jsonl/v1"
    assert set(legacy_packet["groups"][0]["tokens"]) == set(native_usage.COUNTERS)
    first = original(
        rig.send("record_usage", first_data, handle="usage", request="legacy-capture")
    )
    end = path.stat().st_size
    with path.open("ab") as stream:
        stream.write(
            encoded(row({**base, detail: 2})) + b"\n" + encoded(row(base)) + b"\n"
        )
    data = admission(rig, path, start=end)
    assert (
        rig.contexts["usage"].usage_snapshot["extraction"]
        == "deterministic-native-pi-jsonl/v2"
    )
    original(rig.send("record_usage", data, handle="usage", request="detail-capture"))
    group = values(rig)["usage"][-1]["groups"][0]
    assert group["tokens"]["totalTokens"]["value"] == 28
    assert group["tokens"]["output"]["value"] == 6
    assert group["tokens"]["cacheWrite"]["value"] == 10
    assert group["tokens"][detail] == {
        "value": 2,
        "observed_messages": 1,
        "coverage": "partial",
    }
    admission(rig, path, end=end)
    assert rig.contexts["usage"].usage_snapshot == legacy_packet
    assert (
        original(
            rig.send(
                "record_usage", first_data, handle="usage", request="legacy-capture"
            )
        )
        == first
    )


def test_original_ranges_survive_append_and_refuse_partial_line_or_changed_session(
    tmp_path,
):
    path = log(tmp_path, row({"input": 1}))
    end = path.stat().st_size
    artifact, packet = native_usage.capture(path, HEADER, 0, end)
    with path.open("ab") as out:
        out.write(encoded(row({"output": 4})))
    available = native_usage.boundaries(path)
    assert available["end_byte"] == end and available["incomplete_tail_bytes"] > 0
    assert native_usage.capture(path, HEADER, 0, end) == (artifact, packet)
    with pytest.raises(Rejected):
        native_usage.capture(path, HEADER, 0, path.stat().st_size)
    with path.open("ab") as out:
        out.write(b"\n")
    new = native_usage.capture(path, HEADER, end, path.stat().st_size)
    assert new[1]["groups"][0]["tokens"]["output"]["value"] == 4
    with pytest.raises(Rejected):
        native_usage.capture(path, HEADER, 1, end)
    with pytest.raises(Rejected):
        native_usage.capture(path, {**HEADER, "id": "different-native"}, 0, end)


def test_concurrent_overlapping_attributions_commit_only_once(tmp_path):
    rig = light(tmp_path)
    path = log(tmp_path, row({"input": 7}))
    data = admission(rig, path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda key: rig.send("record_usage", data, handle="usage", request=key),
                ["first", "second"],
            )
        )
    assert sorted(r["status"] for r in responses) == ["local_committed", "rejected"]
    assert len(values(rig)["usage"]) == 1


def test_usage_requires_original_host_capture_and_actual_task_executor(tmp_path):
    rig = Rig(tmp_path)
    rig.contexts["owner"] = replace(BASE, allowed_intents=INTENTS)
    rig.adopt(graph(rig))
    attempt = original(rig.claim("work"))["attempt_id"]
    path = log(tmp_path, row({"input": 5}))
    data = admission(rig, path, attempt=attempt)
    original(rig.send("record_usage", data, handle="usage"))
    end = path.stat().st_size
    with path.open("ab") as out:
        out.write(encoded(row({"input": 6})) + b"\n")
    data = admission(
        rig,
        path,
        start=end,
        attempt=attempt,
        context=replace(BASE, session="other-session"),
    )
    assert error(rig.send("record_usage", data, handle="usage")) == "authority_denied"
    data = admission(rig, path, start=end, attempt=attempt)
    rig.contexts["usage"] = replace(rig.contexts["usage"], usage_snapshot=None)
    assert error(rig.send("record_usage", data, handle="usage")) == "source_invalid"
    assert len(values(rig)["usage"]) == 1


def test_terminal_usage_observation_keeps_completion_and_never_regrants_work(tmp_path):
    rig = light(tmp_path)
    work = record(rig)
    original(
        rig.approve(
            "complete_lightweight", {"record_ids": [work], "acceptance_refs": ["AC-1"]}
        )
    )
    terminal = rig.state()["result"]["current_observation"]["terminal"]
    data = admission(rig, log(tmp_path, row(None)))
    original(rig.send("record_usage", data, handle="usage"))
    assert values(rig)["usage"][0]["coverage"] == "unavailable"
    assert (
        values(rig)["terminal"] == terminal
        and values(rig)["business_progress"] == "completed"
    )
    assert (
        next(c for c in rig.state()["capabilities"] if c["name"] == "local_work")[
            "state"
        ]
        == "unavailable"
    )


def test_attention_needs_actual_human_source_and_does_not_double_count(tmp_path):
    rig = light(tmp_path)
    owner = replace(BASE, allowed_intents=INTENTS)
    rig.contexts["owner"] = owner
    data = {"task_attempt": None, "category": "review", "duration_seconds": None}
    assert error(rig.send("record_attention", data)) == "source_invalid"
    first = rig.approve("record_attention", data, actor=owner, request="attention")
    original(first)
    assert original(
        rig.approve("record_attention", data, actor=owner, request="attention")
    ) == original(first)
    assert (
        error(
            rig.approve(
                "record_attention", {**data, "category": "correction"}, actor=owner
            )
        )
        == "domain_conflict"
    )
    item = values(rig)["attention"][0]
    assert item["duration_seconds"] is None and item["duration_basis"] == "unavailable"
    assert "source_ref" not in item


def test_quality_and_outcome_use_original_verifier_and_do_not_overwrite_verdict(
    tmp_path,
):
    rig = Rig(tmp_path)
    rig.contexts["owner"] = replace(BASE, allowed_intents=INTENTS)
    rig.contexts["verifier"] = VERIFIER
    candidate = produce(rig)
    assignment = assign(rig, candidate)
    original(verify(rig, candidate, assignment, "rejected"))
    data = {
        "candidate_id": candidate,
        "rubric": "report-quality",
        "version": "v1",
        "score": 0.25,
        "evidence": [PROOF.reference()],
    }
    source = SourceInput(
        "quality-result",
        "independent_result",
        b"Original independent scoring evidence",
        "record_quality",
        digest({"intent": "record_quality", "work_item_id": rig.wi, "input": data}),
    )
    rig.contexts["quality"] = replace(
        VERIFIER, allowed_intents=INTENTS, artifacts=(PROOF,), source=source
    )
    first = rig.send("record_quality", data, handle="quality", request="quality")
    original(first)
    assert original(
        rig.send("record_quality", data, handle="quality", request="quality")
    ) == original(first)
    assert values(rig)["outcomes"][0]["verification"]["verdict"] == "rejected"
    assert values(rig)["quality"][0]["score"] == 0.25
    changed = {**data, "score": 0.9}
    rig.contexts["quality"] = replace(
        rig.contexts["quality"],
        source=replace(
            source,
            approved_input_digest=digest(
                {"intent": "record_quality", "work_item_id": rig.wi, "input": changed}
            ),
        ),
    )
    assert (
        error(rig.send("record_quality", changed, handle="quality"))
        == "domain_conflict"
    )
    rig.contexts["quality"] = replace(rig.contexts["quality"], actor="producer")
    assert (
        error(rig.send("record_quality", changed, handle="quality"))
        == "authority_denied"
    )


def test_native_host_reads_its_real_session_and_no_caller_counter(native, tmp_path):
    policy, proc, path, _ = native
    with path.open("ab") as out:
        out.write(encoded(row({"input": 17})) + b"\n")
    host = NativeHost(policy, proc=proc, pid=101)
    kernel = Kernel(tmp_path / "native-facts.sqlite", host.authenticate)
    handle = host.prepare("create_lightweight", DEFINITION, None, 0, ())
    wi = original(
        kernel.execute(Command("create", "create_lightweight", DEFINITION), handle)
    )["work_item_id"]
    handle = host.prepare("start_work", {}, wi, 1, ())
    original(kernel.execute(Command("start", "start_work", {}, wi), handle))
    handle = host.prepare("open_usage_interval", {"task_attempt": None}, wi, 1, ())
    interval = original(
        kernel.execute(
            Command("interval", "open_usage_interval", {"task_attempt": None}, wi),
            handle,
        )
    )
    with path.open("ab") as out:
        out.write(encoded(row({"input": 17})) + b"\n")
    data = {
        "interval_id": interval["interval_id"],
        "task_attempt": None,
        "start_byte": interval["start_byte"],
        "end_byte": path.stat().st_size,
    }
    handle = host.prepare("record_usage", data, wi, 1, ())
    response = kernel.execute(Command("usage", "record_usage", data, wi), handle)
    original(response)
    observed = response["result"]["current_observation"]["measurements"]["usage"][0]
    assert observed["groups"][0]["tokens"]["input"]["value"] == 17
    assert observed["groups"][0]["messages"] == 1
    assert (
        observed["session"] == "measurement-session." + digest(host.base.principal())
        and "test-session" not in encoded(observed).decode()
    )
    with pytest.raises(Rejected):
        host.prepare("record_usage", {**data, "tokens": 99}, wi, 1, ())
    with pytest.raises(Rejected):
        host.prepare("record_usage", data, wi, 1, (), human_ref="0" * 64)


@pytest.mark.parametrize("usage", [None, {}, {"input": None}, {"cost": {"total": 2}}])
def test_missing_fields_are_unavailable_even_when_usage_object_exists(tmp_path, usage):
    path = log(tmp_path, row(usage))
    _, packet = native_usage.capture(path, HEADER, 0, path.stat().st_size)
    assert packet["coverage"] == "unavailable"


def test_native_interval_excludes_prehistory_and_closes_at_actual_task_result(tmp_path):
    rig = Rig(tmp_path)
    rig.contexts["owner"] = replace(BASE, allowed_intents=INTENTS)
    rig.adopt(graph(rig))
    attempt = original(rig.claim("work"))["attempt_id"]
    path = log(tmp_path, row({"input": 99}))
    start = native_usage.cursor(path)
    rig.contexts["owner"] = replace(rig.contexts["owner"], native_cursor=start)
    interval = original(
        rig.send("open_usage_interval", {"task_attempt": attempt}, request="anchor")
    )
    with path.open("ab") as out:
        out.write(encoded(row({"input": 7})) + b"\n")
    end = native_usage.cursor(path)
    rig.contexts["owner"] = replace(rig.contexts["owner"], native_cursor=end)
    assert (
        original(
            rig.send("open_usage_interval", {"task_attempt": attempt}, request="anchor")
        )
        == interval
    )
    old = admission(rig, path, start=0, attempt=attempt)
    assert error(rig.send("record_usage", old, handle="usage")) == "source_invalid"
    original(rig.result(attempt))
    assert values(rig)["intervals"][0]["end_byte"] == end
    data = admission(rig, path, start=start, attempt=attempt)
    original(rig.send("record_usage", data, handle="usage"))
    with path.open("ab") as out:
        out.write(encoded(row({"input": 11})) + b"\n")
    later = admission(rig, path, start=end, attempt=attempt)
    assert error(rig.send("record_usage", later, handle="usage")) == "source_invalid"
    assert values(rig)["usage"][0]["groups"][0]["tokens"]["input"]["value"] == 7
    assert (
        error(rig.send("open_usage_interval", {"task_attempt": attempt}))
        == "domain_conflict"
    )


def test_same_native_bytes_cannot_be_attributed_to_two_wis_concurrently(tmp_path):
    rig = light(tmp_path)
    path = log(tmp_path, row({"input": 7}))
    first_wi = rig.wi
    first = admission(rig, path)
    rig.contexts["first-usage"] = rig.contexts["usage"]
    rig.wi = original(
        rig.kernel.execute(
            Command("second-wi", "create_lightweight", DEFINITION), "create"
        )
    )["work_item_id"]
    original(rig.send("start_work", {}))
    second = admission(rig, path)
    jobs = [
        (first_wi, first, "first-usage", "capture-first"),
        (rig.wi, second, "usage", "capture-second"),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda j: rig.kernel.execute(
                    Command(j[3], "record_usage", j[1], j[0]), j[2]
                ),
                jobs,
            )
        )
    assert sorted(r["status"] for r in responses) == ["local_committed", "rejected"]
    assert (
        next(r["error"]["code"] for r in responses if r["status"] == "rejected")
        == "domain_conflict"
    )
    total = sum(
        len(
            rig.kernel.inspect(Query(wi, view="measurements"), "owner")["result"][
                "items"
            ]["usage"]
        )
        for wi in (first_wi, rig.wi)
    )
    assert total == 1


def test_unknown_native_end_is_closed_unavailable_instead_of_unbounded(tmp_path):
    rig = Rig(tmp_path)
    rig.contexts["owner"] = replace(BASE, allowed_intents=INTENTS)
    rig.adopt(graph(rig))
    attempt = original(rig.claim("work"))["attempt_id"]
    path = log(tmp_path, row({"input": 7}))
    data = admission(rig, path, attempt=attempt)
    # This trusted embedding cannot observe an ending native cursor.
    original(rig.result(attempt))
    interval = values(rig)["intervals"][0]
    assert interval["closed"] is True and interval["end_byte"] is None
    assert interval["end_boundary"] == "unavailable"
    assert error(rig.send("record_usage", data, handle="usage")) == "source_invalid"


def test_session_truncation_cannot_create_an_impossible_closed_interval(tmp_path):
    rig = Rig(tmp_path)
    rig.contexts["owner"] = replace(BASE, allowed_intents=INTENTS, native_cursor=100)
    rig.adopt(graph(rig))
    attempt = original(rig.claim("work"))["attempt_id"]
    original(rig.send("open_usage_interval", {"task_attempt": attempt}))
    rig.contexts["owner"] = replace(rig.contexts["owner"], native_cursor=50)
    original(rig.result(attempt))
    interval = values(rig)["intervals"][0]
    assert interval["closed"] is True and interval["end_byte"] is None
    assert interval["end_boundary"] == "unavailable"
    assert interval["end_boundary_reason"] == "source_moved_backwards"
