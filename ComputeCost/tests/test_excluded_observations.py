"""A reviewed non-observation must not quarantine actual measured consumption."""
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, reference
from test_cc_native_dedupe import execution, native, host_result, END, CATALOG, total


def exclude(target):
    at = target.payload.get('interval_start') or target.occurred_at
    return Receipt.make('attribution', ['reviewed-non-observation', target.entity_ref], {
        'target_ref': target.entity_ref, 'wi': None, 'executor': None,
        'responsible': None, 'delegator': None, 'state': 'excluded',
        'policy': 'reviewed_test_contamination.v1', 'effective_at': at},
        occurred_at=at, observed_at=END, producer='reviewed_test_contamination.v1',
        evidence=(target.fact_ref, reference('source_fixture', 'original-test-evidence')))


def query(ledger, watermark=None):
    return query_collaboration(ledger, CollaborationQuery(scope='activity',
        from_date='2026-09-13', to_date='2026-09-14', timezone='UTC',
        watermark=watermark), catalog=CATALOG, catalog_observed_at=END)


def test_excluded_summary_does_not_quarantine_real_partial_native(tmp_path):
    ex = execution(); observed = native('actual', '2026-09-13T01:01:00Z', 40, final=False)
    mock = host_result(ex)
    ledger = UsageLedger(tmp_path/'ledger'); ledger.ingest_receipts([ex, observed, mock])
    old = ledger.receipt_snapshot(); before = query(ledger, old['watermark'])
    # The covers-less result is not added to the native message it summarizes.
    assert before['summary']['usage']['conflicted_event_count'] == 0
    assert before['summary']['usage']['event_count'] == 1
    change = exclude(mock); ledger.ingest_receipts([change])
    result = query(ledger)
    assert result['summary']['usage']['event_count'] == 1
    assert result['summary']['usage']['known_total_tokens'] == total(40)
    assert result['summary']['usage']['conflicted_event_count'] == 0
    assert 'provisional_usage' in result['summary']['cost']['reasons']
    assert result['reconciliation']['excluded_observation_refs'] == [mock.entity_ref]
    assert query(ledger, old['watermark'])['report_sha256'] == before['report_sha256']
    retained = {r.fact_ref: r.to_dict() for r in ledger.receipt_snapshot()['facts']}
    assert retained[mock.fact_ref] == mock.to_dict()
    assert ledger.ingest_receipts([change])['inserted'] == 0


def test_exclusion_of_one_source_does_not_hide_another_conflict(tmp_path):
    ex = execution(); first = host_result(ex)
    second = host_result(ex, output=999998)
    other_ex = execution('other')
    mock = host_result(other_ex)
    ledger = UsageLedger(tmp_path/'ledger')
    ledger.ingest_receipts([ex, other_ex, first, second, mock, exclude(mock)])
    result = query(ledger)
    assert first.entity_ref in result['revision_conflicts']
    assert result['reconciliation']['excluded_observation_refs'] == [mock.entity_ref]
    assert result['summary']['cost']['machine_cash_usd'] is None


def test_disputed_exclusion_cannot_remove_real_observation(tmp_path):
    actual = native('actual', '2026-09-13T01:01:00Z', 40)
    excluded = exclude(actual)
    assigned = Receipt.make('attribution', 'independent-assignment', {
        'target_ref': actual.entity_ref, 'wi': 'wi_20260913_001', 'executor': 'pulse',
        'responsible': None, 'delegator': None, 'state': 'assigned',
        'policy': 'independent.v1', 'effective_at': actual.occurred_at},
        occurred_at=actual.occurred_at, observed_at=END, producer='independent',
        evidence=(actual.fact_ref,))
    ledger = UsageLedger(tmp_path/'ledger'); ledger.ingest_receipts([actual, excluded, assigned])
    result = query(ledger)
    assert result['summary']['usage']['event_count'] == 1
    assert result['summary']['usage']['known_total_tokens'] == total(40)
    assert actual.entity_ref not in result['reconciliation']['excluded_observation_refs']
    assert actual.entity_ref in result['attribution_conflicts']


def test_unknown_actor_or_zero_usage_is_not_an_exclusion(tmp_path):
    unknown = native('unknown', '2026-09-13T01:01:00Z', 0)
    ledger = UsageLedger(tmp_path/'ledger'); ledger.ingest_receipts([unknown])
    result = query(ledger)
    assert result['summary']['usage']['event_count'] == 1
    assert result['reconciliation']['excluded_observation_refs'] == []


def test_excluded_mock_execution_does_not_seed_invalid_wi_group(tmp_path):
    ex = execution(); mock = host_result(ex)
    ledger = UsageLedger(tmp_path/'ledger')
    ledger.ingest_receipts([ex, mock, exclude(ex), exclude(mock)])
    result = query_collaboration(ledger, CollaborationQuery(scope='activity',
        from_date='2026-09-13', to_date='2026-09-14', timezone='UTC',
        group_by=('node', 'entrypoint', 'runtime', 'agent', 'wi')),
        catalog=CATALOG, catalog_observed_at=END)
    assert result['groups'] == []
    assert result['summary']['usage']['event_count'] == 0
    assert result['summary']['usage']['expected_execution_count'] == 0
