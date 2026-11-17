"""Local CC protocol notices are not provider calls; retain uncertain/paid evidence."""
import json
from pathlib import Path
import pytest
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration, reconcile_usage
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, TOKEN_FIELDS, reference
from pulse_compute_cost.receipt_sources import read_cc, usage_receipt
from test_cc_native_dedupe import execution, native, host_result, SCOPE, START, END, MODEL
from test_collaboration_receipts import CATALOG

ZERO = dict(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)

def notice(model="<synthetic>", counts=None, key="notice"):
    return usage_receipt("cc", key, ZERO if counts is None else counts, at=END, observed=END,
        model=model, final=True, mapping="cc_message.v1", native_scope_ref=SCOPE)

def replace_fact(row, **changes):
    return Receipt.make(row.kind, None, {**row.payload, **changes}, entity_ref=row.entity_ref,
        occurred_at=row.occurred_at, observed_at=row.observed_at, producer=row.producer, evidence=row.evidence)

def test_local_cc_notice_is_retained_but_not_counted_or_priced(tmp_path):
    row={'type':'assistant','uuid':'error-frame','sessionId':'session','isApiErrorMessage':True,
         'error':'rate_limit','timestamp':END,'message':{'id':'local-error','model':'<synthetic>',
         'stop_reason':'stop_sequence','usage':ZERO}}
    path=tmp_path/'session.jsonl';path.write_text(json.dumps(row)+'\n')
    local=read_cc(path,END)[0];real=native('real','2026-09-13T01:01:00Z',40)
    ledger=UsageLedger(tmp_path/'ledger');ledger.ingest_receipts([local,real]);before=ledger.receipt_snapshot()
    out=query_collaboration(ledger,CollaborationQuery(scope='activity',from_date='2026-09-13',
        to_date='2026-09-14',timezone='UTC',watermark=before['watermark']),catalog=CATALOG,catalog_observed_at=END)
    assert out['summary']['usage']['event_count']==1
    assert out['summary']['usage']['known_total_tokens']==sum(real.payload['tokens'].values())
    assert out['reconciliation']['non_provider_observation_refs']==[local.entity_ref]
    assert ledger.receipt_snapshot()['sha256']==before['sha256']
    assert local.to_dict()==next(r.to_dict() for r in ledger.receipt_snapshot()['facts'] if r.entity_ref==local.entity_ref)

@pytest.mark.parametrize('kind',['real_zero','unknown_model','nonzero','missing','foreign_producer'])
def test_zero_unknown_or_contradictory_models_are_not_automatically_excluded(kind):
    row=notice()
    if kind=='real_zero':row=notice(MODEL)
    elif kind=='unknown_model':row=notice(None)
    elif kind=='nonzero':row=notice(counts={**ZERO,'output_tokens':1})
    elif kind=='missing':row=notice(counts={k:v for k,v in ZERO.items() if k!='cache_read_input_tokens'})
    elif kind=='foreign_producer':row=Receipt.make('usage',None,row.payload,entity_ref=row.entity_ref,
        occurred_at=END,observed_at=END,producer='foreign_observation.v1')
    counted,report=reconcile_usage([row])
    assert [r.entity_ref for r in counted]==[row.entity_ref]
    assert report.get('non_provider_observation_refs',[])==[]

def test_explicit_zero_summary_of_only_local_notices_is_not_a_second_call():
    local=notice();ex=execution();summary=replace_fact(host_result(ex),model='<synthetic>',
        tokens={k:0 for k in TOKEN_FIELDS},native_tokens=ZERO,covers=[local.entity_ref])
    counted,report=reconcile_usage([local,summary],executions=[ex])
    assert counted==[]
    assert set(report['non_provider_observation_refs'])=={local.entity_ref,summary.entity_ref}

def test_local_summary_cannot_hide_contradictory_real_detail():
    local=notice();real=native('real','2026-09-13T01:01:00Z',40);ex=execution()
    summary=replace_fact(host_result(ex),model='<synthetic>',tokens={k:0 for k in TOKEN_FIELDS},
        native_tokens=ZERO,covers=[local.entity_ref,real.entity_ref])
    _,report=reconcile_usage([local,real,summary],executions=[ex])
    assert summary.entity_ref not in report.get('non_provider_observation_refs',[])
    assert real.entity_ref not in report.get('non_provider_observation_refs',[])

def failed_zero_summary():
    ex=replace_fact(execution(),state='failed')
    result=replace_fact(host_result(ex),model=None,tokens={k:0 for k in TOKEN_FIELDS},native_tokens=ZERO,
        coverage_ref=reference('coverage',[ex.entity_ref,None]))
    return ex,result

def test_failed_host_with_explicit_zero_and_no_provider_message_has_no_usage():
    ex,result=failed_zero_summary();counted,report=reconcile_usage([result],executions=[ex])
    assert counted==[]
    assert report['non_provider_observation_refs']==[result.entity_ref]

@pytest.mark.parametrize('case',['completed','missing_host','measured_native'])
def test_failed_zero_result_needs_exact_host_and_no_competing_native(case):
    ex,result=failed_zero_summary();rows=[result];executions=[ex]
    if case=='completed':executions=[replace_fact(ex,state='completed')]
    elif case=='missing_host':executions=[]
    else:rows.append(native('real','2026-09-13T01:01:00Z',40))
    _,report=reconcile_usage(rows,executions=executions)
    assert result.entity_ref not in report.get('non_provider_observation_refs',[])


def test_contrary_reported_total_is_not_a_local_zero_notice():
    from pulse_compute_cost.receipts import ReceiptError
    with pytest.raises(ReceiptError, match='reported total does not conserve'):
        replace_fact(notice(),reported_total_tokens=1)

def test_scope_mismatch_cannot_hide_a_local_summary():
    local=notice();ex=execution()
    summary=replace_fact(host_result(ex),model='<synthetic>',tokens={k:0 for k in TOKEN_FIELDS},
        native_tokens=ZERO,covers=[local.entity_ref],native_scope_ref=reference('scope','foreign'))
    _,result=reconcile_usage([local,summary],executions=[ex])
    assert summary.entity_ref not in result['non_provider_observation_refs']

def test_nonfinal_local_notice_remains_provisional():
    row=replace_fact(notice(),final=False)
    counted,result=reconcile_usage([row])
    assert [r.entity_ref for r in counted]==[row.entity_ref]
    assert result['non_provider_observation_refs']==[]


def test_cancelled_zero_summary_does_not_count_later_greeting_as_its_usage():
    ex,result=failed_zero_summary();ex=replace_fact(ex,state='cancelled')
    later=native('later-greeting','2026-09-13T01:06:00Z',40)
    counted,report=reconcile_usage([result,later],executions=[ex])
    assert [r.entity_ref for r in counted]==[later.entity_ref]
    assert report['non_provider_observation_refs']==[result.entity_ref]

def test_cancelled_zero_summary_cannot_hide_inflight_native_request():
    ex,result=failed_zero_summary();ex=replace_fact(ex,state='cancelled')
    inside=native('inside','2026-09-13T01:03:00Z',40)
    _,report=reconcile_usage([result,inside],executions=[ex])
    assert result.entity_ref not in report['non_provider_observation_refs']
