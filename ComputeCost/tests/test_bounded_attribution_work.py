"""Collector-only work bounds; policy and declaration semantics do not change."""
from unittest.mock import patch

from pulse_compute_cost import mcp_request_purpose as m
from pulse_compute_cost import codex_request_purpose as c
from pulse_compute_cost.receipts import Receipt, reference, current_receipts
from test_cc_session_revisions import fixture, legacy_claim, OBSERVED
from test_codex_request_purpose import RootRequestTests, END


def test_session_claim_reads_only_its_own_predecessor_graph(tmp_path):
    usages, bindings = fixture(tmp_path)
    own = legacy_claim(usages[-1], bindings)
    foreign = [Receipt.make('usage', ['foreign', i], usages[-1].payload,
        occurred_at=usages[-1].occurred_at, observed_at=OBSERVED, producer=usages[-1].producer) for i in range(100)]
    original = [*usages, own, *foreign]
    with patch.object(m, 'current_receipts', wraps=current_receipts) as spy:
        result = m.session_attributions(original, usages, bindings, OBSERVED)
    assert len(result) == 1
    assert result[0].supersedes == own.fact_ref
    selected = list(spy.call_args.args[0])
    assert selected == [own]


def test_old_native_boundaries_and_contrary_producer_are_preserved(tmp_path):
    usages, bindings = fixture(tmp_path)
    own = legacy_claim(usages[-1], bindings)
    early = Receipt.make('usage', None, usages[0].payload, entity_ref=usages[0].entity_ref,
        occurred_at='2026-01-01T00:59:00Z', observed_at=OBSERVED, producer=usages[0].producer)
    result = m.session_attributions([early, *usages, own], usages, bindings, OBSERVED)
    assert result[0].payload['effective_at'] == early.occurred_at
    alien = Receipt.make('attribution', None, own.payload, entity_ref=own.entity_ref,
        occurred_at=own.occurred_at, observed_at=OBSERVED, producer='foreign_claim.v1', evidence=own.evidence)
    assert m.session_attributions([*usages, own, alien], usages, bindings, OBSERVED) == []


def test_session_no_local_targets_does_not_scan_global_history():
    with patch.object(m, 'current_receipts', side_effect=AssertionError('unneeded history projection')):
        assert m.session_attributions([], [], {}, OBSERVED) == []


def test_parent_candidate_and_revalidation_share_one_current_projection():
    case = RootRequestTests(); case.setUp()
    facts = [case.parent, case.child, case.task]
    with patch.object(c, 'current_receipts', wraps=current_receipts) as spy:
        result = c.root_request_attributions(facts, case.links, END)
    assert len(result) == 1
    assert spy.call_count == 1
    assert result[0].payload['wi'] == case.task.payload['wi']
