"""One reviewed CC family origin, independent child quantities and one WI."""
import pytest
from pulse_compute_cost.activity_classification import reviewed_activity_receipts
from pulse_compute_cost.receipt_sources import actor_receipt, usage_receipt
from pulse_compute_cost.receipts import Receipt, ReceiptError, reference
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.mcp_request_purpose import session_attributions
from test_collaboration_receipts import CATALOG

AT='2026-09-13T01:00:00Z'; END='2026-09-13T01:10:00Z'; WI='wi_20260913_001'
SCOPE=reference('native_session',['cc','root'])
def usage(name, *, scope=SCOPE, runtime='cc', at=END, output=3):
    return usage_receipt(runtime,['root',name],dict(input_tokens=2,output_tokens=output,
        cache_read_input_tokens=10,cache_creation_input_tokens=1), at=at,observed=at,
        model='openai/test',mapping='cc_message.v1' if runtime=='cc' else 'pi_native.v1',native_scope_ref=scope)
def item(root):
    return dict(target_fact_ref=root.fact_ref,scope='cc_session_ingress',native_scope_ref=SCOPE,
        effective_at=AT,agent=None,node='ownermac',entrypoint='telegram',
        evidence_refs=[reference('human_confirmation','TG for original parent and its children')])
def document(record):return dict(schema='computecost_reviewed_activity/v1',items=[record])
def query(ledger,mark=None):
    return query_collaboration(ledger,CollaborationQuery(scope='activity',from_date='2026-09-13',
        to_date='2026-09-14',timezone='UTC',group_by=('wi','entrypoint'),watermark=mark),catalog=CATALOG,catalog_observed_at=END)

def test_one_confirmation_covers_parent_children_and_later_child_without_new_wi(tmp_path):
    parent=usage('parent');children=[usage('child-one',output=5),usage('child-two',output=7)]
    recorded=[parent,*children];binding={SCOPE:[dict(wi=WI,native_evidence_ref=reference('mcp','parent-declaration'))]}
    purposes=session_attributions([],recorded,binding,END)
    ledger=UsageLedger(tmp_path/'ledger');ledger.ingest_receipts(recorded+purposes);old=ledger.receipt_snapshot();before=query(ledger,old['watermark'])
    confirmed=reviewed_activity_receipts(recorded,document(item(parent)),END)
    assert len(confirmed)==3
    for r in confirmed:
        assert r.payload['wi'] is r.payload['executor'] is None
        assert r.producer=='reviewed_activity.v1' and 'input_ref' not in r.payload['context']
    ledger.ingest_receipts(confirmed);after=query(ledger)
    groups=[g for g in after['groups'] if g['usage']['event_count']]
    assert len(groups)==1 and groups[0]['key']=={'wi':WI,'entrypoint':'telegram'}
    assert after['summary']['usage']['known_total_tokens']==before['summary']['usage']['known_total_tokens']==54
    assert after['summary']['cost']==before['summary']['cost']
    assert query(ledger,old['watermark'])==before
    assert ledger.ingest_receipts(reviewed_activity_receipts(recorded,document(item(parent)),END))['inserted']==0
    later=usage('later-child',at='2026-09-13T01:20:00Z')
    assert len(reviewed_activity_receipts(recorded+[later],document(item(parent)),END))==4
    assert {r.to_dict()['fact_ref'] for r in ledger.receipt_snapshot()['facts'] if r.kind=='usage'}=={r.fact_ref for r in recorded}

def test_scope_not_wi_or_runtime_name_controls_inheritance():
    parent=usage('parent');foreign=usage('other-session',scope=reference('native_session',['cc','other']))
    other_runtime=usage('pi',runtime='pi');before=usage('earlier',at='2026-09-12T01:00:00Z')
    rows=reviewed_activity_receipts([parent,foreign,other_runtime,before],document(item(parent)),END)
    assert [r.payload['target_ref'] for r in rows]==[parent.entity_ref]
    assert reviewed_activity_receipts([foreign],document(item(parent)),END)==[]

@pytest.mark.parametrize('change',[{'agent':'pulse'},{'entrypoint':None},{'node':None},
    {'native_scope_ref':reference('native_session',['cc','different'])},{'effective_at':'2026-09-14T01:00:00Z'},
    {'evidence_refs':[]}])
def test_invalid_confirmation_never_becomes_task_or_host_authority(change):
    parent=usage('parent');record={**item(parent),**change}
    with pytest.raises(ReceiptError):reviewed_activity_receipts([parent],document(record),END)

def test_quantity_conflicts_survive_and_cross_scope_identity_does_not_get_route(tmp_path):
    parent=usage('parent');child=usage('child',output=5);different=usage('child',output=6)
    rows=reviewed_activity_receipts([parent,child,different],document(item(parent)),END)
    ledger=UsageLedger(tmp_path/'ledger');ledger.ingest_receipts([parent,child,different]);before=query(ledger)
    ledger.ingest_receipts(rows);assert query(ledger)['revision_conflicts']==before['revision_conflicts']
    assert query(ledger)['summary']['usage']['conflicted_event_count']==before['summary']['usage']['conflicted_event_count']
    crossed=Receipt.make('usage',None,{**child.payload,'native_scope_ref':reference('native_session',['cc','foreign'])},
        entity_ref=child.entity_ref,occurred_at=child.occurred_at,observed_at=END,producer=child.producer)
    assert all(r.payload['target_ref']!=child.entity_ref for r in reviewed_activity_receipts([parent,child,crossed],document(item(parent)),END))

def test_independent_contrary_route_is_visible_not_overwritten(tmp_path):
    parent=usage('parent');child=usage('child')
    direct=actor_receipt(child,node='ownermac',entrypoint='feishu',observed=END,evidence=(reference('host','other-input'),))
    rows=reviewed_activity_receipts([parent,child],document(item(parent)),END)
    ledger=UsageLedger(tmp_path/'ledger');ledger.ingest_receipts([parent,child,direct,*rows]);result=query(ledger)
    assert any(g['key']['entrypoint']=='ambiguous' and g['usage']['event_count']==1 for g in result['groups'])
    assert ledger.receipt_snapshot()['facts']


def test_normal_collector_reuses_family_confirmation_for_new_child(tmp_path,monkeypatch):
    import json
    import pulse_compute_cost.activity_classification as activity
    from pulse_compute_cost.receipt_sources import collect_registered_sources,read_cc,REGISTRY_SCHEMA
    root=tmp_path/'projects';root.mkdir();main=root/'root.jsonl'
    def raw(name,child=False):
        return dict(type='assistant',uuid=name,sessionId='root',isSidechain=child,
            agentId='worker' if child else None,timestamp=END,
            message=dict(id=name,model='openai/test',stop_reason='end_turn',usage=dict(
                input_tokens=2,output_tokens=3,cache_read_input_tokens=10,cache_creation_input_tokens=1)))
    main.write_text(json.dumps(raw('parent'))+'\n');anchor=read_cc(main,END)[0]
    child=root/'root'/'subagents'/'agent-worker.jsonl';child.parent.mkdir(parents=True)
    child.write_text(json.dumps(raw('child',True))+'\n')
    confirmed=tmp_path/'reviewed.json';confirmed.write_text(json.dumps(document(item(anchor))))
    monkeypatch.setattr(activity,'CONFIRMATIONS',confirmed)
    registry=dict(schema=REGISTRY_SCHEMA,required_receipt_features=['non_wi_activity.v1','actor_context.v1'],
        sources=[dict(id='cc',kind='cc',node='ownermac',path=str(root))])
    ledger=UsageLedger(tmp_path/'ledger');collect_registered_sources(registry,ledger)
    assert sum(g['usage']['event_count'] for g in query(ledger)['groups'] if g['key']['entrypoint']=='telegram')==2
    assert collect_registered_sources(registry,ledger)['inserted']==0
    with child.open('a') as f:f.write(json.dumps(raw('later-child',True))+'\n')
    collect_registered_sources(registry,ledger)
    assert sum(g['usage']['event_count'] for g in query(ledger)['groups'] if g['key']['entrypoint']=='telegram')==3
    assert collect_registered_sources(registry,ledger)['inserted']==0


def test_aggregate_with_message_mapping_does_not_inherit():
    parent=usage('parent');child=usage('aggregate')
    aggregate=Receipt.make('usage',None,{**child.payload,'granularity':'turn_aggregate'},
        entity_ref=child.entity_ref,occurred_at=child.occurred_at,observed_at=END,producer=child.producer)
    assert all(x.payload['target_ref']!=aggregate.entity_ref for x in
        reviewed_activity_receipts([parent,aggregate],document(item(parent)),END))
    with pytest.raises(ReceiptError):reviewed_activity_receipts([aggregate],document(item(aggregate)),END)
