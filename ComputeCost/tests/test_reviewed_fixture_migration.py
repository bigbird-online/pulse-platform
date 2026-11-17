"""Finite contamination review: no inferred exclusion of future or real usage."""
import hashlib
import importlib.util
import json
from pathlib import Path
from copy import deepcopy
from pulse_compute_cost.receipts import digest, Receipt
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import read_host

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('reviewed_fixture', ROOT/'uat/reviewed_fixture_migration.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
MANIFEST = json.loads((ROOT/'uat/reviewed-test-contamination-20261004.json').read_bytes())


def test_review_is_finite_and_every_record_has_a_reproduced_graph():
    assert len(MANIFEST['records']) == 13
    traces = {r['sha256']: r for r in MANIFEST['reproductions']}
    assert len(traces) == 6
    targets = set()
    for item in MANIFEST['records']:
        witness = traces[item['trace_sha256']]
        assert digest(witness['trace']) == witness['sha256']
        assert witness['test_path'].startswith('Relay/tests/')
        assert item['usage_fact_ref'] in item['target_fact_refs']
        targets.update(item['target_fact_refs'])
    assert len(targets) == 46


def test_renaming_preserves_links_quantities_state_native_scope_and_order():
    rows = [{'kind':'execution','dispatch_ref':'stable','at':'clock','event_ref':'a'},
            {'kind':'usage','dispatch_ref':'run-x','event_ref':'b','parent_ref':'a',
             'usage':{'input':1},'native_scope_ref':'native','final':True}]
    encode = lambda x: ('\n'.join(json.dumps(r) for r in x)+'\n').encode()
    expected = m.trace_shape(encode(rows))
    renamed = deepcopy(rows);renamed[0].update(at='later',event_ref='A')
    renamed[1].update(event_ref='B',parent_ref='A',dispatch_ref='another-run')
    assert m.trace_shape(encode(renamed)) == expected
    for key, value in [('parent_ref','B'),('native_scope_ref','other'),('usage',{'input':2}),('final',False)]:
        bad = deepcopy(renamed);bad[1][key] = value
        assert m.trace_shape(encode(bad)) != expected
    assert m.trace_shape(encode(list(reversed(rows)))) != expected


def test_changed_source_bytes_refused_before_plan(tmp_path):
    item = MANIFEST['records'][0]
    (tmp_path/item['file']).write_text('{}\n')
    import pytest
    with pytest.raises(ValueError, match='source changed'):
        m.source_plan(MANIFEST, tmp_path, '2026-10-04T13:20:00Z')


def test_ledger_check_keeps_competing_business_claims(tmp_path):
    from test_cc_native_dedupe import native, END
    import pytest
    row = native('real', '2026-09-13T01:01:00Z', 40)
    assert m.verify_ledger([row], [row]) == {row.fact_ref:row}
    claim = Receipt.make('attribution','contrary',{'target_ref':row.entity_ref,'wi':'wi_20260913_001',
        'executor':None,'responsible':None,'delegator':None,'state':'assigned','policy':'independent',
        'effective_at':row.occurred_at},occurred_at=row.occurred_at,observed_at=END,producer='independent')
    with pytest.raises(ValueError, match='business claim'):
        m.verify_ledger([row,claim], [row])
