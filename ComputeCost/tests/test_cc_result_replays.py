"""Repeated CC final results of one Host attempt, resolved by what they assert."""
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt
from test_cc_native_dedupe import CATALOG, END, execution, host_message, host_result, native, total


def version(row, *, at, output, covers):
    """Another final result for the same attempt identity (no supersession edge)."""
    p = dict(row.payload)
    p.update(covers=covers, interval_end=at, tokens={k: output for k in p["tokens"]},
             native_tokens={k: output for k in p["native_tokens"]})
    return Receipt.make("usage", None, p, entity_ref=row.entity_ref, occurred_at=at, observed_at=at,
                        producer=row.producer)


def query(tmp_path, rows):
    ledger = UsageLedger(tmp_path / "ledger")
    ledger.ingest_receipts(rows)
    return query_collaboration(ledger, CollaborationQuery(scope="activity", from_date="2026-09-13",
        to_date="2026-09-14", timezone="UTC", group_by=("agent",)), catalog=CATALOG, catalog_observed_at=END)


def cohort():
    earlier = execution("earlier", "2026-09-13T00:40:00Z")
    previous = host_result(earlier, start="2026-09-13T00:40:00Z", end="2026-09-13T00:45:00Z", output=500)
    before = native("m0", "2026-09-13T00:42:00Z", 30)
    ex = execution("resumed", "2026-09-13T01:00:00Z")
    message = native("m1", "2026-09-13T01:01:00Z", 40)
    stream = host_message("m1", ex, "2026-09-13T01:01:01Z", 40)
    base = host_result(ex, start="2026-09-13T01:00:00Z", end="2026-09-13T01:00:01Z", output=500)
    early = version(base, at="2026-09-13T01:00:01Z", output=500, covers=[])
    own = version(base, at="2026-09-13T01:02:00Z", output=600, covers=[stream.entity_ref])
    return [earlier, previous, before, ex, message, stream], early, own


def test_result_repeating_an_earlier_counter_is_not_the_attempts_result(tmp_path):
    rows, early, own = cohort()
    out = query(tmp_path, [*rows, early, own])
    assert out["revision_conflicts"] == []
    assert sorted(out["reconciliation"]["observation_groups"][own.entity_ref]) == sorted([early.fact_ref, own.fact_ref])
    usage = out["summary"]["usage"]
    # One bill per native message. The result is a session counter: only its
    # increase over the previous counter (100 per class) not itemized by m1 is added.
    residual = {"uncached_input": 98, "cache_read": 0, "cache_write": 90, "output": 60}
    assert usage["event_count"] == 3
    assert usage["known_total_tokens"] == total(30) + total(40) + sum(residual.values())
    proof, = out["reconciliation"]["cc_unitemized_counters"]
    assert proof["residual_entity_refs"] == [own.entity_ref]
    assert usage["conflicted_event_count"] == 0
    assert "join_conflict" not in out["summary"]["cost"]["reasons"]


def test_result_before_the_attempts_first_request_is_resolved_without_a_prior_record(tmp_path):
    rows, early, own = cohort()
    rows = rows[3:]
    early = version(early, at="2026-09-13T01:00:01Z", output=450, covers=[])
    out = query(tmp_path, [*rows, early, own])
    assert out["revision_conflicts"] == []
    assert out["summary"]["usage"]["conflicted_event_count"] == 0


def test_equal_results_are_one_fact(tmp_path):
    rows, early, own = cohort()
    again = version(own, at="2026-09-13T01:02:05Z", output=600, covers=own.payload["covers"])
    out = query(tmp_path, [*rows, own, again])
    assert out["revision_conflicts"] == []


def test_disagreeing_results_remain_a_conflict_with_a_reason(tmp_path):
    rows, early, own = cohort()
    other = version(own, at="2026-09-13T01:02:05Z", output=650, covers=own.payload["covers"])
    late = version(early, at="2026-09-13T01:01:30Z", output=500, covers=[])
    for competing in (other, late):
        out = query(tmp_path / competing.fact_ref[:12], [*rows[3:], own, competing])
        assert out["revision_conflicts"] == [own.entity_ref]
        assert out["summary"]["usage"]["conflict_reasons"] == {"revision_conflict": 1}
        assert out["groups"][0]["usage"]["conflict_reasons"] == {"revision_conflict": 1}
        assert out["groups"][0]["cost"]["partial_machine_cash_usd"] is None


def test_attempt_result_repeating_the_previous_counter_adds_nothing(tmp_path):
    earlier = execution("earlier", "2026-09-13T00:40:00Z")
    previous = host_result(earlier, start="2026-09-13T00:40:00Z", end="2026-09-13T00:45:00Z", output=500)
    message = native("m1", "2026-09-13T00:42:00Z", 40)
    ex = execution("notification", "2026-09-13T01:00:00Z")
    repeat = host_result(ex, start="2026-09-13T01:00:00Z", end="2026-09-13T01:00:01Z", output=500)
    out = query(tmp_path, [earlier, previous, message, ex, repeat])
    assert repeat.entity_ref in out["reconciliation"]["suppressed_aggregate_refs"]
    assert out["summary"]["usage"]["conflicted_event_count"] == 0
    # A risen counter is its increase since the previous counter, not a conflict.
    risen = host_result(ex, start="2026-09-13T01:00:00Z", end="2026-09-13T01:00:01Z", output=510)
    out = query(tmp_path / "risen", [earlier, previous, message, ex, risen])
    assert risen.entity_ref not in out["reconciliation"]["conflict_reasons"]
    assert out["summary"]["usage"]["conflicted_event_count"] == 0
    assert out["summary"]["usage"]["known_total_tokens"] == total(40) + 4 * 10
