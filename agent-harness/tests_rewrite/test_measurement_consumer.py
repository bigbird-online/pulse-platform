"""The existing ComputeCost entry reads real replacement-core query observations."""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ComputeCost"))
from pulse_compute_cost.harness_query_report import HarnessQueryError, load_report
from harness_rewrite import Query, SourceInput
from harness_rewrite.domain import INTENTS
from harness_rewrite.model import digest, encoded
from tests_rewrite.test_lightweight import light
from tests_rewrite.test_measurements import admission, log, row
from tests_rewrite.test_workflow import BASE, Rig, original
from tests_rewrite.test_review import VERIFIER, PROOF, produce, assign, verify


def source(tmp_path, mixed=False):
    rig = light(tmp_path)
    usage = {
        "input": 4,
        "output": 6,
        "cacheRead": 3,
        "cacheWrite": 0,
        "reasoning": 2,
        "totalTokens": 13,
    }
    rows = [row(usage)]
    if mixed:
        rows += [
            row(None),
            row(
                {
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 0,
                    "cost": {"total": 0, "currency": "USD"},
                },
                model="other",
            ),
            row({"input": 5, "cost": {"total": 2, "currency": "EUR"}}, model="third"),
        ]
    path = log(tmp_path, *rows)
    data = admission(rig, path)
    original(rig.send("record_usage", data, handle="usage", request="usage"))
    owner = replace(BASE, allowed_intents=INTENTS)
    original(
        rig.approve(
            "record_attention",
            {"task_attempt": None, "category": "review", "duration_seconds": None},
            actor=owner,
            request="attention",
        )
    )
    return rig, rig.state(query=Query(rig.wi, view="measurements"))


def consume(tmp_path, rig, query):
    path = tmp_path / "query.json"
    path.write_bytes(encoded(query))
    return load_report(path, rig.wi)


def test_existing_cli_consumes_real_query_without_legacy_discovery_or_private_output(
    tmp_path,
):
    rig, query = source(tmp_path)
    query["effects"].append(
        {
            "native_session_id": "PRIVATE_DISPATCH_ID",
            "snapshot": {"path": "/private/path"},
        }
    )
    path = tmp_path / "query.json"
    path.write_bytes(encoded(query))
    compute = Path(__file__).resolve().parents[2] / "ComputeCost"
    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        "import sys;sys.path.insert(0,sys.argv.pop(1));from pulse_compute_cost.cli import main;raise SystemExit(main())",
        str(compute),
        "wi-report",
        rig.wi,
        "--harness-query-snapshot",
        str(path),
    ]
    result = subprocess.run(
        command,
        env={**os.environ, "PULSE_COMPUTECOST_SOURCES": "/unavailable/source-registry"},
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["schema"] == "computecost_harness_query_report/v1"
    assert report["source"]["sha256"] == sha256(path.read_bytes()).hexdigest()
    assert report["source"]["journal_revision"] == query["work_item"]["local_revision"]
    assert report["usage"]["totals"]["totalTokens"]["value"] == 13
    assert report["usage"]["totals"]["reasoning"]["value"] == 2
    assert report["usage"]["totals"]["cacheWrite"]["value"] == 0
    assert report["usage"]["totals"]["cacheWrite1h"]["value"] is None
    assert report["usage"]["costs_by_observed_currency"] == {}
    assert report["human_attention"]["event_count"] == 1
    assert report["human_attention"]["self_reported_seconds"] is None
    assert (
        report["whole_WI_lifetime_covered"] is False
        and report["completion_authority"] is False
    )
    for private in (
        "PRIVATE_DISPATCH_ID",
        "PRIVATE_MESSAGE_NOT_PUBLIC",
        "private-provider-native-id",
        "/private/path",
        "model-a",
        "measurement-session.",
    ):
        assert private not in result.stdout
    mixed = subprocess.run(
        [*command, "--scl-measurement-snapshot", str(path)],
        capture_output=True,
        text=True,
    )
    assert mixed.returncode == 1 and "cannot mix" in mixed.stderr and not mixed.stdout


def test_partial_counters_zero_and_separate_currencies_remain_observations(tmp_path):
    rig, query = source(tmp_path, mixed=True)
    report = consume(tmp_path, rig, query)
    assert report["usage"]["totals"]["input"] == {
        "value": 9,
        "observed_messages": 3,
        "coverage": "partial",
    }
    assert report["usage"]["costs_by_observed_currency"]["USD"]["value"] == 0
    assert report["usage"]["costs_by_observed_currency"]["EUR"]["value"] == 2
    assert report["usage"]["totals"]["reasoning"]["coverage"] == "partial"


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_wi",
        "failed_read",
        "revision",
        "duplicate_usage",
        "overlap",
        "range",
        "coverage",
        "boolean",
        "negative",
        "currency",
        "duplicate_interval",
        "raw_identity",
        "private_key",
        "disagreement",
        "attention_task_identity",
        "measurement_progress",
    ],
)
def test_malformed_or_conflicting_queries_never_become_empty_or_zero(tmp_path, fault):
    rig, query = source(tmp_path)
    data = query["result"]["items"]
    usage = data["usage"][0]
    if fault == "wrong_wi":
        query["work_item"]["id"] = "another-wi"
    elif fault == "failed_read":
        query["status"] = "rejected"
    elif fault == "revision":
        query["work_item"]["local_revision"] += 1
    elif fault in {"duplicate_usage", "overlap"}:
        new = deepcopy(usage)
        if fault == "overlap":
            new["record_id"] = "different-record"
        data["usage"].append(new)
    elif fault == "range":
        usage["start_byte"] = usage["end_byte"]
    elif fault == "coverage":
        usage["groups"][0]["tokens"]["input"]["coverage"] = "unavailable"
    elif fault in {"boolean", "negative"}:
        usage["groups"][0]["tokens"]["input"]["value"] = (
            True if fault == "boolean" else -1
        )
    elif fault == "currency":
        usage["groups"][0]["costs"] = {
            "usd": {"value": 1, "coverage": "complete", "observed_messages": 1}
        }
    elif fault == "duplicate_interval":
        data["intervals"].append(deepcopy(data["intervals"][0]))
    elif fault == "raw_identity":
        usage["session"] = "raw-provider-session"
    elif fault == "private_key":
        usage["groups"][0]["api_key"] = "PRIVATE_VALUE"
    elif fault == "attention_task_identity":
        data["attention"][0]["task_attempt"] = "/private/sensitive/path"
    elif fault == "measurement_progress":
        data["business_progress"] = "completed"
    elif fault == "disagreement":
        query["result"]["current_observation"]["measurements"] = {}
    # Real query objects may share nested values; exported JSON independently repeats them.
    if fault != "disagreement":
        query["result"]["current_observation"]["measurements"] = deepcopy(data)
    with pytest.raises(HarnessQueryError):
        consume(tmp_path, rig, query)


def test_original_captured_range_survives_later_unknown_interval_end(tmp_path):
    rig, query = source(tmp_path)
    item = query["result"]["items"]["intervals"][0]
    item.update(closed=True, end_byte=None, end_boundary="unavailable")
    query["result"]["current_observation"]["measurements"] = deepcopy(
        query["result"]["items"]
    )
    report = consume(tmp_path, rig, query)
    assert report["usage"]["totals"]["input"]["value"] == 4
    assert report["usage"]["records"][0]["interval_end_boundary"] == "unavailable"


def test_duplicate_json_and_symlink_sources_are_rejected(tmp_path):
    path = tmp_path / "ambiguous.json"
    path.write_text('{"schema":"harness.response/v1","schema":"another"}')
    with pytest.raises(HarnessQueryError, match="Duplicate JSON"):
        load_report(path, "wi")
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(HarnessQueryError, match="non-symlink"):
        load_report(link, "wi")


def test_empty_query_has_unavailable_counters_and_no_invented_events(tmp_path):
    rig = light(tmp_path)
    report = consume(tmp_path, rig, rig.state(query=Query(rig.wi, view="measurements")))
    assert report["coverage"] == "unavailable"
    assert all(v["value"] is None for v in report["usage"]["totals"].values())
    assert report["human_attention"]["event_count"] == 0
    assert report["human_attention"]["self_reported_seconds"] is None


def test_quality_reader_keeps_the_original_negative_verdict(tmp_path):
    rig = Rig(tmp_path)
    rig.contexts["owner"] = replace(BASE, allowed_intents=INTENTS)
    rig.contexts["verifier"] = VERIFIER
    candidate = produce(rig)
    original(verify(rig, candidate, assign(rig, candidate), "rejected"))
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
    original(rig.send("record_quality", data, handle="quality", request="quality"))
    query = rig.state(query=Query(rig.wi, view="measurements"))
    report = consume(tmp_path, rig, query)
    assert report["quality"][0]["score"] == 0.25
    assert report["outcomes"][0]["verification"]["verdict"] == "rejected"
    assert report["outcomes"][0]["review"] is None
    assert report["completion_authority"] is False


def test_conflicting_current_candidates_are_rejected(tmp_path):
    rig = Rig(tmp_path)
    candidate = produce(rig)
    query = rig.state(query=Query(rig.wi, view="measurements"))
    data = query["result"]["items"]
    assert data["outcomes"][0]["candidate_id"] == candidate
    other = deepcopy(data["outcomes"][0])
    other["candidate_id"] = "different-candidate"
    data["outcomes"].append(other)
    query["result"]["current_observation"]["measurements"] = deepcopy(data)
    with pytest.raises(HarnessQueryError, match="Multiple Harness candidates"):
        consume(tmp_path, rig, query)
