"""Bounded native observations and full Pi collection must share one quantity."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources, read_pi
from pulse_compute_cost.receipts import Receipt, current_receipts, reference
from pulse_compute_cost.stage_record import record_stage
from test_stage_record import AT, OBSERVED, HEADER, ASSISTANT, capture, command, encoded

MODEL = {"type": "model_change", "id": "model", "parentId": None,
         "timestamp": AT, "modelId": "test-model", "provider": "synthetic"}
SUMMARY = {"type": "compaction", "id": "compact", "parentId": "model", "timestamp": AT,
           "usage": {"input": 10, "output": 5, "cacheRead": 20,
                     "cacheWrite": 0, "totalTokens": 35, "reasoning": 2}}


@pytest.fixture
def ledger(tmp_path):
    return UsageLedger(tmp_path / "ledger", clock=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc))


def save(ledger, rows):
    source = capture(rows, start=100)
    return record_stage(ledger, source, command(source, changes=[]))


def report(ledger, **kwargs):
    return query_collaboration(ledger, CollaborationQuery(**kwargs))


def collect(ledger, path, rows):
    path.write_bytes(encoded([HEADER, *rows]))
    return collect_registered_sources({"schema": "computecost_source_registry/v1", "sources": [
        {"id": "synthetic-pi", "kind": "pi", "path": str(path)}]}, ledger)


def assert_one_quantity(ledger):
    active, conflicts = current_receipts(ledger.receipt_snapshot()["facts"])
    assert not conflicts
    usage = [r for r in active if r.kind == "usage"]
    assert len(usage) == 1
    assert usage[0].payload["model"] == "test-model"
    assert report(ledger)["summary"]["usage"]["known_total_tokens"] == 35


@pytest.mark.parametrize("first", ["bounded", "collector"])
def test_bounded_and_collector_order_does_not_make_compaction_usage_conflict(ledger, tmp_path, first):
    if first == "bounded":
        save(ledger, [SUMMARY])
        collect(ledger, tmp_path / "pi.jsonl", [MODEL, SUMMARY])
    else:
        collect(ledger, tmp_path / "pi.jsonl", [MODEL, SUMMARY])
        save(ledger, [SUMMARY])
    assert_one_quantity(ledger)


@pytest.mark.parametrize("first", ["bounded", "full"])
def test_two_bounded_observations_refine_only_model_evidence(ledger, first):
    unknown, known = [SUMMARY], [MODEL, SUMMARY]
    before = save(ledger, unknown if first == "bounded" else known)
    historical = report(ledger, watermark=before["watermark"])
    save(ledger, known if first == "bounded" else unknown)
    assert_one_quantity(ledger)
    assert report(ledger, watermark=before["watermark"]) == historical
    watermark = ledger.receipt_snapshot()["watermark"]
    # Reopen, repeat both observations; neither revives the old unknown head.
    reopened = UsageLedger(ledger.root)
    save(reopened, unknown)
    save(reopened, known)
    assert reopened.receipt_snapshot()["watermark"] == watermark
    assert_one_quantity(reopened)


@pytest.mark.parametrize("change", ["model", "provider", "quantity", "time", "evidence"])
def test_known_contradictions_are_not_model_refinements(ledger, change):
    save(ledger, [MODEL, SUMMARY])
    source = capture([MODEL, SUMMARY], start=100)
    old = source["usage"][0]
    payload, at, evidence = old.payload, old.occurred_at, old.evidence
    if change == "model":
        payload["model"] = "different"
    elif change == "provider":
        payload["provider"] = "different"
    elif change == "quantity":
        payload["tokens"]["output"] += 1
    elif change == "time":
        at = OBSERVED
    else:
        payload["model"] = None
        evidence = (reference("native_record", "different-original"),)
    source["usage"] = [Receipt.make("usage", None, payload, entity_ref=old.entity_ref,
        occurred_at=at, observed_at=OBSERVED, producer=old.producer, evidence=evidence)]
    record_stage(ledger, source, command(source, changes=[]))
    _, conflicts = current_receipts(ledger.receipt_snapshot()["facts"])
    assert old.entity_ref in conflicts


def test_a_model_refinement_cannot_choose_among_conflicting_heads(ledger):
    save(ledger, [SUMMARY])
    other = deepcopy(SUMMARY)
    other["usage"]["output"] = 6
    other["usage"]["totalTokens"] = 36
    save(ledger, [other])
    save(ledger, [MODEL, SUMMARY])
    _, conflicts = current_receipts(ledger.receipt_snapshot()["facts"])
    assert len(conflicts) == 1
    assert len(next(iter(conflicts.values()))) >= 2


def test_a_new_compaction_is_new_cost_not_a_metadata_refinement(ledger):
    save(ledger, [MODEL, SUMMARY])
    save(ledger, [MODEL, {**SUMMARY, "id": "next-compaction"}])
    assert report(ledger)["summary"]["usage"]["known_total_tokens"] == 70


@pytest.mark.parametrize("order", ["native_first", "pi_first"])
def test_scl_capture_and_pi_parser_share_calls_not_additive_aggregate(ledger, tmp_path, order):
    # Exercise the same pure native capture used by SCL, not another controller
    # or a forged production measurement. Only synthetic bytes enter this test.
    from harness_rewrite.native_usage import capture as native_capture
    from pulse_compute_cost.native_stage_host import NativeStageHost
    path = tmp_path / "native.jsonl"
    path.write_bytes(encoded([HEADER, ASSISTANT, {**SUMMARY, "parentId": "a1"}]))
    host = SimpleNamespace(session_file=path, native_header=HEADER,
                           base=SimpleNamespace(principal=lambda: {"actor": "synthetic"}))
    boundary = {"start_byte": 0, "end_byte": path.stat().st_size}
    _, packet = native_capture(path, HEADER, 0, boundary["end_byte"])
    source = NativeStageHost(host, native_capture).capture(boundary)
    # SCL's aggregate covers assistant messages; compaction is a separate
    # invocation in Pi. Never add this aggregate to those request quantities.
    assert packet["groups"][0]["tokens"]["totalTokens"]["value"] == 35
    rows = read_pi(path, source["observed_at"])
    if order == "native_first":
        record_stage(ledger, source, command(source, changes=[]))
        ledger.ingest_receipts(rows)
    else:
        ledger.ingest_receipts(rows)
        record_stage(ledger, source, command(source, changes=[]))
    result = report(ledger)["summary"]["usage"]
    assert result["known_total_tokens"] == 70
    assert result["event_count"] == 2
    # Another capture observes these same bytes; it is not another model call.
    saved = record_stage(ledger, source, command(source, changes=[]))
    assert saved["inserted"] == 0
    assert report(ledger)["summary"]["usage"]["known_total_tokens"] == 70


def test_source_still_does_not_guess_model_from_outside_its_boundary():
    source = capture([SUMMARY], start=100)
    assert source["usage"][0].payload["model"] is None
    assert source["usage"][0].entity_ref == read_pi(Path("/synthetic/pi.jsonl"), OBSERVED,
        data=encoded([HEADER, MODEL, SUMMARY]))[0].entity_ref
