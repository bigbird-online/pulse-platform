"""CC session counters of a model the transcript never itemizes are counted once."""
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration, reconcile_usage
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.receipts import reference
from test_cc_native_dedupe import MODEL, SCOPE, execution, native

SMALL = "openai/test-small"
CATALOG = {"data": [
    {"id": MODEL, "pricing": {"prompt": "1", "completion": "1", "input_cache_read": "0.1", "input_cache_write": "2"}},
    {"id": SMALL, "pricing": {"prompt": "1", "completion": "1", "input_cache_read": "0", "input_cache_write": "0"}}]}


def counter(ex, at, input_tokens, output_tokens, *, model=SMALL, start=None):
    """The Host's per-model result: CC's persisted cumulative session counter."""
    return usage_receipt("cc", ["result", ex.entity_ref, model],
        dict(input_tokens=input_tokens, output_tokens=output_tokens,
             cache_read_input_tokens=0, cache_creation_input_tokens=0),
        at=at, observed=at, model=model, execution_ref=ex.entity_ref,
        coverage_ref=reference("coverage", [ex.entity_ref, model]),
        common={"wi": None, "executor": "pulse", "responsible": None, "delegator": None},
        granularity="turn_aggregate", covers=[], interval=(start or ex.occurred_at, at), final=True,
        mapping="cc_sdk_result.v1", native_scope_ref=SCOPE)


def attempts():
    first = execution("first", "2026-09-13T00:50:00Z")
    second = execution("second", "2026-09-13T01:00:00Z")
    third = execution("third", "2026-09-13T01:10:00Z")
    return first, second, third


def test_small_model_counter_does_not_quarantine_main_model_messages():
    first, second, third = attempts()
    messages = [native("m1", "2026-09-13T01:01:00Z", 40),
                native("m2", "2026-09-13T01:02:00Z", 8, final=False),
                native("m3", "2026-09-13T01:11:00Z", 30)]
    counters = [counter(first, "2026-09-13T00:55:00Z", 100, 10),
                counter(second, "2026-09-13T01:05:00Z", 100, 10),
                counter(third, "2026-09-13T01:15:00Z", 160, 25)]
    rows, result = reconcile_usage([*messages, *counters], executions=[first, second, third])
    assert result["conflicting_refs"] == []
    assert {m.entity_ref for m in messages} <= {r.entity_ref for r in rows}
    small = [r for r in rows if r.payload["model"] == SMALL]
    # The first value has no known start; the repeat adds nothing; the rise is
    # the usage since the previous observation, spent no earlier than the
    # session's next activity (here its own turn, before message m3).
    assert [(r.payload["granularity"], r.payload["tokens"]["uncached_input"], r.payload["tokens"]["output"],
             r.payload["interval_start"], r.payload["interval_end"]) for r in small] == [
        ("session_counter", 100, 10, None, None),
        ("turn_aggregate", 60, 15, "2026-09-13T01:10:00Z", "2026-09-13T01:15:00Z")]
    assert sum(r.payload["tokens"]["output"] for r in small) == 25
    assert {r.entity_ref for r in small} == {counters[0].entity_ref, counters[2].entity_ref}
    proof, = result["cc_unitemized_counters"]
    assert proof["model"] == SMALL and proof["counter_refs"] == sorted(c.entity_ref for c in counters)


def test_first_small_model_counter_starts_after_the_sessions_first_usage():
    """An auxiliary model runs inside turns: its first counter is bounded by the
    session's earliest usage, so a dated view can place it."""
    first, second, third = attempts()
    messages = [native("m0", "2026-09-13T00:51:00Z", 5), native("m1", "2026-09-13T01:01:00Z", 40)]
    counters = [counter(first, "2026-09-13T00:55:00Z", 100, 10),
                counter(third, "2026-09-13T01:15:00Z", 160, 25)]
    rows, result = reconcile_usage([*messages, *counters], executions=[first, second, third])
    small = [r for r in rows if r.payload["model"] == SMALL]
    assert [(r.payload["granularity"], r.payload["interval_start"], r.payload["interval_end"]) for r in small] == [
        ("turn_aggregate", "2026-09-13T00:51:00Z", "2026-09-13T00:55:00Z"),
        ("turn_aggregate", "2026-09-13T01:01:00Z", "2026-09-13T01:15:00Z")]
    assert result["conflicting_refs"] == []


def test_dated_view_counts_only_the_increase_inside_the_day(tmp_path):
    first, second, third = attempts()
    rows = [first, second, third, native("m1", "2026-09-13T01:01:00Z", 40),
            counter(first, "2026-09-12T23:00:00Z", 100, 10, start="2026-09-12T22:59:00Z"),
            counter(second, "2026-09-13T01:05:00Z", 100, 10),
            counter(third, "2026-09-13T01:15:00Z", 160, 25)]
    ledger = UsageLedger(tmp_path / "ledger")
    ledger.ingest_receipts(rows)
    out = query_collaboration(ledger, CollaborationQuery(scope="activity", from_date="2026-09-13",
        to_date="2026-09-14", timezone="UTC", group_by=("model",)), catalog=CATALOG,
        catalog_observed_at="2026-09-13T02:00:00Z")
    groups = {g["key"]["model"]: g for g in out["groups"]}
    assert groups[SMALL]["usage"]["event_count"] == 1
    assert groups[SMALL]["cost"]["priced_machine_cash_usd"] == "75"
    assert groups[MODEL]["usage"]["event_count"] == 1
    assert out["summary"]["usage"]["conflicted_event_count"] == 0


def test_decreasing_small_model_counter_is_a_conflict_of_that_result_only():
    first, second, third = attempts()
    counters = [counter(first, "2026-09-13T00:55:00Z", 100, 10),
                counter(second, "2026-09-13T01:05:00Z", 90, 12)]
    message = native("m1", "2026-09-13T01:01:00Z", 40)
    rows, result = reconcile_usage([message, *counters], executions=[first, second])
    assert [r.entity_ref for r in rows] == [message.entity_ref, counters[0].entity_ref]
    assert result["conflicting_refs"] == [counters[1].entity_ref]
    assert result["conflict_reasons"][counters[1].entity_ref] == ["cc_counter_regressed"]


def test_counter_regression_splits_the_family_at_the_break(tmp_path):
    """A counter resumed lower (its writer restarted) is unknown at the break
    only; the results before and after it still reconcile as increases."""
    first, second, third = attempts()
    fourth = execution("fourth", "2026-09-13T01:20:00Z")
    counters = [counter(first, "2026-09-13T00:55:00Z", 100, 10),
                counter(second, "2026-09-13T01:05:00Z", 160, 25),
                counter(third, "2026-09-13T01:15:00Z", 150, 24),   # resumed after the restart
                counter(fourth, "2026-09-13T01:25:00Z", 200, 40)]
    message = native("m1", "2026-09-13T01:01:00Z", 40)
    rows, result = reconcile_usage([message, *counters], executions=[first, second, third, fourth])
    assert result["conflicting_refs"] == [counters[2].entity_ref]
    small = [r for r in rows if r.payload["model"] == SMALL]
    assert [(r.entity_ref, r.payload["tokens"]["uncached_input"], r.payload["tokens"]["output"]) for r in small] == [
        (counters[0].entity_ref, 100, 10), (counters[1].entity_ref, 60, 15), (counters[3].entity_ref, 50, 16)]
    ledger = UsageLedger(tmp_path / "ledger")
    ledger.ingest_receipts([first, second, third, fourth, message, *counters])
    out = query_collaboration(ledger, CollaborationQuery(scope="activity", from_date="2026-09-13",
        to_date="2026-09-14", timezone="UTC"), catalog=CATALOG, catalog_observed_at="2026-09-13T02:00:00Z")
    assert out["summary"]["usage"]["conflict_reasons"] == {"cc_counter_regressed": 1}


def test_different_counters_at_one_time_still_quarantine_the_family():
    first, second, third = attempts()
    counters = [counter(first, "2026-09-13T00:55:00Z", 100, 10),
                counter(second, "2026-09-13T01:05:00Z", 160, 25),
                counter(third, "2026-09-13T01:05:00Z", 150, 25, start="2026-09-13T01:00:00Z")]
    rows, result = reconcile_usage(counters, executions=[first, second, third])
    assert rows == []
    assert set(result["conflicting_refs"]) == {c.entity_ref for c in counters}


def test_counter_without_cache_classes_is_missing_fields_not_a_regression():
    first, second, third = attempts()
    counters = [counter(first, "2026-09-13T00:55:00Z", 1, 2), counter(second, "2026-09-13T01:05:00Z", 1, 2)]
    counters = [usage_receipt("cc", ["result", c.entity_ref], {"input_tokens": 1, "output_tokens": 2},
                              at=c.occurred_at, observed=c.occurred_at, model=SMALL,
                              execution_ref=c.payload["execution_ref"], coverage_ref=c.payload["coverage_ref"],
                              common={"wi": None, "executor": "pulse", "responsible": None, "delegator": None},
                              granularity="turn_aggregate", covers=[], interval=(c.payload["interval_start"], c.occurred_at),
                              final=True, mapping="cc_sdk_result.v1", native_scope_ref=SCOPE) for c in counters]
    assert all(c.payload["tokens"]["cache_read"] is None for c in counters)
    rows, result = reconcile_usage(counters, executions=[first, second])
    assert rows == []
    assert all(result["conflict_reasons"][c.entity_ref] == ["cc_counter_fields_missing"] for c in counters)


def test_itemized_model_keeps_the_message_reconciliation():
    first, second, third = attempts()
    message = native("small", "2026-09-13T01:01:00Z", 4)
    message = usage_receipt("cc", ["native", "small"], message.payload["native_tokens"],
        at="2026-09-13T01:01:00Z", observed="2026-09-13T01:01:00Z", model=SMALL,
        mapping="cc_message.v1", native_scope_ref=SCOPE)
    result_row = counter(second, "2026-09-13T01:05:00Z", 100, 10)
    rows, result = reconcile_usage([message, result_row], executions=[second])
    assert result["cc_unitemized_counters"] == []
    assert [r.entity_ref for r in rows] == [message.entity_ref]
    assert result_row.entity_ref in result["suppressed_aggregate_refs"]


def session_result(ex, at, uncached, cache_read, cache_write, output, covers):
    """A Host result of an itemized model: CC's cumulative session counter, with the attempt's messages."""
    return usage_receipt("cc", ["result", ex.entity_ref, MODEL],
        dict(input_tokens=uncached, output_tokens=output,
             cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write),
        at=at, observed=at, model=MODEL, execution_ref=ex.entity_ref,
        coverage_ref=reference("coverage", [ex.entity_ref, MODEL]),
        common={"wi": None, "executor": "pulse", "responsible": None, "delegator": None},
        granularity="turn_aggregate", covers=[r.entity_ref for r in covers],
        interval=(ex.occurred_at, at), final=True, mapping="cc_sdk_result.v1", native_scope_ref=SCOPE)


def test_session_counter_of_an_itemized_model_is_its_increase_not_a_turn_total():
    from test_cc_native_dedupe import host_message
    first, second, third = attempts()
    # Each streamed snapshot counts 2/100/10 input classes; output is a lower bound.
    early = host_message("a", first, "2026-09-13T00:51:00Z", 5)
    late = host_message("b", first, "2026-09-13T00:55:00.187Z", 1)   # arrives after its result
    kept = host_message("c", second, "2026-09-13T01:01:00Z", 7)
    cancelled = host_message("d", second, "2026-09-13T01:04:59Z", 8)  # never enters the counter
    one = session_result(first, "2026-09-13T00:55:00Z", 2, 100, 10, 9, [early, late])
    two = session_result(second, "2026-09-13T01:05:00Z", 6, 300, 30, 20, [kept, cancelled])
    rows, result = reconcile_usage([early, late, kept, cancelled, one, two],
                                   executions=[first, second])
    assert result["conflicting_refs"] == []
    # Neither cumulative counter is added to, or replaces, the attempt's messages.
    assert {r.entity_ref for r in rows} == {m.entity_ref for m in (early, late, kept, cancelled)}
    assert {one.entity_ref, two.entity_ref} <= set(result["suppressed_aggregate_refs"])
    proof, = result["cc_unitemized_counters"]
    assert proof["baseline_fact_ref"] == one.fact_ref and proof["residual_entity_refs"] == []
    # The cancelled snapshot exceeds the counter's increase; nothing is carried as usage.
    assert proof["reconciled_fact_ref"] == one.fact_ref


def test_session_counter_increase_beyond_its_messages_is_one_residual():
    from test_cc_native_dedupe import host_message
    first, second, third = attempts()
    early = host_message("a", first, "2026-09-13T00:51:00Z", 5)
    kept = host_message("c", second, "2026-09-13T01:01:00Z", 7)
    one = session_result(first, "2026-09-13T00:55:00Z", 2, 100, 10, 9, [early])
    two = session_result(second, "2026-09-13T01:05:00Z", 4, 200, 20, 30, [kept])
    three = session_result(third, "2026-09-13T01:15:00Z", 4, 200, 20, 30, [])
    rows, result = reconcile_usage([early, kept, one, two, three], executions=[first, second, third])
    assert result["conflicting_refs"] == []
    residual, = [r for r in rows if r.payload["mapping"] == "cc_cumulative_residual.v1"]
    assert residual.entity_ref == two.entity_ref
    assert residual.payload["tokens"] == {"uncached_input": 0, "cache_read": 0, "cache_write": 0, "output": 14}
    assert (residual.payload["interval_start"], residual.payload["interval_end"]) == (
        "2026-09-13T01:00:00Z", "2026-09-13T01:05:00Z")


def test_itemized_counter_after_a_break_differences_from_the_resumed_baseline():
    from test_cc_native_dedupe import host_message
    first, second, third = attempts()
    fourth = execution("fourth", "2026-09-13T01:20:00Z")
    early = host_message("a", first, "2026-09-13T00:51:00Z", 5)
    kept = host_message("c", second, "2026-09-13T01:01:00Z", 7)
    later = host_message("e", fourth, "2026-09-13T01:21:00Z", 5)
    one = session_result(first, "2026-09-13T00:55:00Z", 2, 100, 10, 9, [early])
    two = session_result(second, "2026-09-13T01:05:00Z", 4, 200, 20, 30, [kept])
    three = session_result(third, "2026-09-13T01:15:00Z", 3, 150, 15, 25, [])
    four = session_result(fourth, "2026-09-13T01:25:00Z", 6, 300, 30, 40, [later])
    rows, result = reconcile_usage([early, kept, later, one, two, three, four],
                                   executions=[first, second, third, fourth])
    assert result["conflicting_refs"] == [three.entity_ref]
    assert {early.entity_ref, kept.entity_ref, later.entity_ref} <= {r.entity_ref for r in rows}
    residuals = {r.entity_ref: r.payload["tokens"] for r in rows if r.payload["mapping"] == "cc_cumulative_residual.v1"}
    assert residuals == {two.entity_ref: {"uncached_input": 0, "cache_read": 0, "cache_write": 0, "output": 14},
                         four.entity_ref: {"uncached_input": 1, "cache_read": 50, "cache_write": 5, "output": 10}}
