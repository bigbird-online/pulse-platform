"""Behavioral regression of explicit native unknown -> reviewed purpose."""
import copy
from dataclasses import replace
import unittest
import test_non_wi_activity as fixtures
from pulse_compute_cost.receipts import Receipt,ReceiptError,reference,canonical
from pulse_compute_cost.activity_classification import reviewed_activity_receipts
from pulse_compute_cost.cc_request_purpose import request_attributions

class StrictNativeSupersessionTests(unittest.TestCase):
    setUp=fixtures.NonWiActivityTests.setUp
    query=fixtures.NonWiActivityTests.query
    def prepare(self):
        scope=reference('native_session',['cc','finite-session'])
        self.row=Receipt.make('usage',None,{**self.row.payload,'runtime':'cc','mapping':'cc_message.v1',
            'granularity':'request','native_scope_ref':scope,'interval_start':fixtures.AT,'interval_end':fixtures.AT},
            entity_ref=self.row.entity_ref,occurred_at=fixtures.AT,observed_at=fixtures.AT,producer='cc_message.v1')
        self.request=reference('cc_native_request_unclassified',[['finite-session','exact-request']])
        self.record=reference('native_record','original-own-input')
        self.prior=Receipt.make('attribution',None,{'target_ref':self.row.entity_ref,'wi':None,
            'executor':None,'responsible':None,'delegator':None,'state':'unassigned',
            'policy':'cc_native_request_purpose.v1','effective_at':fixtures.AT},
            entity_ref=reference('attribution',['session_wi_declaration.v1',self.row.entity_ref]),
            occurred_at=fixtures.AT,observed_at=fixtures.AT,producer='cc_native_request_purpose.v1',
            evidence=(self.record,reference('cc_native_request_record',[self.request,self.record]),reference('native_session_usage',[scope,self.row.entity_ref]),
                reference('cc_native_request_usage',[scope,self.row.entity_ref,self.request])))
        self.item={'target_fact_ref':self.row.fact_ref,'work_item_id':'wi_20261005_904',
            'agent':None,'node':None,'entrypoint':None,'source_attribution_fact_ref':self.prior.fact_ref,
            'native_request_ref':self.request,'native_request_record_ref':self.record,
            'usage_interval':[fixtures.AT,fixtures.AT],'review_basis':'recorded_continuation',
            'evidence_refs':[self.row.fact_ref,self.record,self.proof]}
        self.ledger.ingest_receipts([self.row,self.prior])
    def corrections(self,facts=None,item=None):
        return reviewed_activity_receipts(facts or self.ledger.receipt_snapshot()['facts'],
            {'schema':'computecost_reviewed_activity/v1','items':[item or self.item]},fixtures.AT)
    def test_real_graph_append_retains_old_query_raw_usage_and_repeat(self):
        self.prepare();before=self.ledger.receipt_snapshot();old=self.query(before['watermark'])
        changes=self.corrections();self.assertEqual(len(changes),1)
        self.assertEqual(changes[0].supersedes,self.prior.fact_ref)
        self.assertEqual(changes[0].entity_ref,self.prior.entity_ref)
        self.ledger.ingest_receipts(changes);new=self.query()
        self.assertEqual(new['groups'][0]['key']['wi'],'wi_20261005_904')
        self.assertEqual(self.query(before['watermark']),old)
        self.assertEqual(new['summary']['usage']['tokens'],old['summary']['usage']['tokens'])
        self.assertEqual(new['summary']['cost']['partial_machine_cash_usd'],old['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(self.ledger.ingest_receipts(self.corrections())['inserted'],0)
        self.assertEqual([r.to_dict()for r in self.ledger.receipt_snapshot()['facts']if r.kind=='usage'],[self.row.to_dict()])
    def test_fresh_collector_cannot_retract_reviewed_purpose(self):
        self.prepare();self.ledger.ingest_receipts(self.corrections())
        binding={self.row.entity_ref:{'declarations':[],'request_ref':self.request,'ancestry_refs':(self.record,),
                                     'qualified_declaration_refs':()}}
        self.assertEqual(request_attributions(self.ledger.receipt_snapshot()['facts'],[self.row],binding,{},fixtures.AT),[])
    def test_wrong_request_record_scope_or_unproven_claim_is_rejected(self):
        self.prepare()
        mutations=[{'native_request_ref':reference('request','different')},
                   {'native_request_record_ref':reference('native_record','different')},
                   {'usage_interval':['2026-01-01T00:00:00Z',fixtures.AT]},
                   {'review_basis':'latest_session_WI'}, {'evidence_refs':[self.record,self.proof]}]
        for delta in mutations:
            with self.subTest(delta=delta),self.assertRaises(ReceiptError):self.corrections(item=dict(self.item,**delta))
        for delta in ({'native_scope_ref':reference('scope','different')},{'runtime':'codex'},
                      {'mapping':'cc_sdk_result.v1'},{'granularity':'turn_aggregate'}):
            usage=replace(self.row,_payload_json=canonical(dict(self.row.payload,**delta)))
            item=dict(self.item,target_fact_ref=usage.fact_ref,evidence_refs=[usage.fact_ref,self.record,self.proof])
            with self.subTest(delta=delta),self.assertRaises(ReceiptError):self.corrections([usage,self.prior],item)
    def test_other_producer_canonical_identity_or_known_task_is_protected(self):
        self.prepare()
        priors=[replace(self.prior,producer='scl_host_session_binding.v1'),
                replace(self.prior,entity_ref=reference('attribution','independent')),
                replace(self.prior,_payload_json=canonical(dict(self.prior.payload,wi='wi_20261006_340',state='assigned'))),
                replace(self.prior,_payload_json=canonical(dict(self.prior.payload,scope='actor')))]
        for prior in priors:
            item=dict(self.item,source_attribution_fact_ref=prior.fact_ref)
            with self.subTest(prior=prior.fact_ref),self.assertRaises(ReceiptError):self.corrections([self.row,prior],item)
    def test_later_independent_choice_and_concurrent_heads_are_preserved(self):
        self.prepare()
        other=Receipt.make('attribution',None,dict(self.prior.payload,wi='wi_20261006_340',state='assigned'),
            entity_ref=self.prior.entity_ref,supersedes=self.prior.fact_ref,occurred_at=fixtures.AT,
            observed_at=fixtures.AT,producer='independent_review.v1',evidence=(self.proof,))
        self.assertEqual(self.corrections([self.row,self.prior,other]),[])
        own=self.corrections()[0]
        self.assertEqual(self.corrections([self.row,self.prior,other,own]),[])
    def test_other_usage_in_same_session_is_not_inherited(self):
        self.prepare();other=Receipt.make('usage','another-request',self.row.payload,
            occurred_at=fixtures.AT,observed_at=fixtures.AT,producer=self.row.producer)
        changes=self.corrections([self.row,self.prior,other]);self.ledger.ingest_receipts([other,*changes])
        self.assertEqual({g['key']['wi']:g['usage']['event_count']for g in self.query()['groups']},
                         {'wi_20261005_904':1,'unassigned':1})
    def test_actor_injection_and_missing_closed_proof_fields_fail(self):
        self.prepare()
        for key in ('agent','node','entrypoint'):
            with self.subTest(key=key),self.assertRaises(ReceiptError):self.corrections(item=dict(self.item,**{key:'pulse'}))
        for key in ('native_request_ref','native_request_record_ref','source_attribution_fact_ref'):
            item=copy.deepcopy(self.item);item.pop(key)
            with self.subTest(key=key),self.assertRaises(ReceiptError):self.corrections(item=item)
    def test_equivalent_review_can_add_evidence_without_changing_purpose(self):
        self.prepare();self.ledger.ingest_receipts(self.corrections())
        item=dict(self.item,evidence_refs=self.item['evidence_refs']+[reference('review','independent-proof')])
        changes=self.corrections(item=item);self.assertEqual(len(changes),1)
        self.assertEqual(changes[0].payload,self.corrections()[0].payload)
        self.ledger.ingest_receipts(changes)
        self.assertEqual(self.ledger.ingest_receipts(self.corrections(item=item))['inserted'],0)

    def test_another_ancestry_record_is_not_the_proven_request_record(self):
        self.prepare()
        other=reference('native_record','earlier-assistant-in-same-ancestry')
        prior=replace(self.prior,evidence=self.prior.evidence+(other,))
        item=dict(self.item,source_attribution_fact_ref=prior.fact_ref,
                  native_request_record_ref=other,evidence_refs=self.item['evidence_refs']+[other])
        with self.assertRaises(ReceiptError):self.corrections([self.row,prior],item)


class ExactNativeRequestPairSourceTests(unittest.TestCase):
    from test_cc_request_purpose import RequestPurposeTests as F
    setUp=F.setUp
    request=F.request
    write=F.write
    target=F.target
    def test_original_parser_pairs_fresh_input_not_the_previous_declared_request(self):
        from pulse_compute_cost import mcp_request_purpose as m
        from pulse_compute_cost.cc_request_purpose import request_bindings
        from pulse_compute_cost.receipt_sources import read_cc
        from pulse_compute_cost.receipts import digest
        from test_cc_request_purpose import WI,OBSERVED,BEFORE
        self.request('declared',0,WI)
        original,_=self.request('fresh',10)
        self.write();raw=read_cc(self.path,OBSERVED)
        declarations=m.cc_native_completions([self.path],raw,[],since=BEFORE)
        bindings=request_bindings([self.path],raw,declarations)
        record=reference('native_record',digest(original))
        binding=bindings[self.target('fresh-before')]
        self.assertEqual(binding['request_record_ref'],record)
        self.assertFalse(binding['declarations'])
        source=request_attributions([],raw,bindings,declarations,OBSERVED)
        fresh=next(r for r in source if r.payload['target_ref']==self.target('fresh-before'))
        self.assertIn(reference('cc_native_request_record',[binding['request_ref'],record]),fresh.evidence)
        self.assertEqual(fresh.payload['state'],'unassigned')


class HumanReviewedSessionTests(unittest.TestCase):
    """A request without a provable boundary, reviewed by the human for its session."""
    setUp = StrictNativeSupersessionTests.setUp
    query = StrictNativeSupersessionTests.query
    prepare = StrictNativeSupersessionTests.prepare
    corrections = StrictNativeSupersessionTests.corrections
    def session_item(self, **delta):
        item = {k: v for k, v in self.item.items() if k not in ('native_request_ref', 'native_request_record_ref')}
        item.update(native_scope_ref=self.row.payload['native_scope_ref'], review_basis='human_reviewed_session',
                    evidence_refs=[self.row.fact_ref, self.proof])
        item.update(delta)
        return item

    def test_session_review_supersedes_only_the_witnessed_unknown(self):
        self.prepare()
        changes = self.corrections(item=self.session_item())
        self.assertEqual([(c.supersedes, c.payload['wi']) for c in changes], [(self.prior.fact_ref, 'wi_20261005_904')])
        self.ledger.ingest_receipts(changes)
        self.assertEqual(self.query()['groups'][0]['key']['wi'], 'wi_20261005_904')

    def test_session_review_rejects_other_scope_basis_or_unwitnessed_usage(self):
        self.prepare()
        for delta in ({'native_scope_ref': reference('scope', 'different')}, {'review_basis': 'native_user_request'},
                      {'evidence_refs': [self.proof]}, {'agent': 'pulse'}):
            with self.subTest(delta=delta), self.assertRaises(ReceiptError):
                self.corrections(item=self.session_item(**delta))
        unwitnessed = replace(self.prior, evidence=(self.record,))
        item = self.session_item(source_attribution_fact_ref=unwitnessed.fact_ref)
        with self.assertRaises(ReceiptError):
            self.corrections([self.row, unwitnessed], item)

class RestatedSourceTests(StrictNativeSupersessionTests):
    def restated(self, payload):
        return Receipt.make('attribution',None,payload,entity_ref=self.prior.entity_ref,supersedes=self.prior.fact_ref,
            occurred_at=fixtures.AT,observed_at=fixtures.AT,producer=self.prior.producer,evidence=self.prior.evidence)
    def test_review_follows_the_source_restating_the_same_instant(self):
        # The source wrote the same boundary in another text form; the review
        # still addresses that assertion and supersedes its current version.
        self.prepare()
        at=fixtures.AT.replace('Z','.000000Z') if '.' not in fixtures.AT else fixtures.AT.replace('Z','000Z')
        again=self.restated(dict(self.prior.payload,effective_at=at))
        self.ledger.ingest_receipts([again])
        changes=self.corrections();self.assertEqual([c.supersedes for c in changes],[again.fact_ref])
        self.ledger.ingest_receipts(changes)
        self.assertEqual(self.query()['groups'][0]['key']['wi'],'wi_20261005_904')
    def test_a_changed_source_assertion_is_not_overridden(self):
        self.prepare()
        changed=self.restated(dict(self.prior.payload,effective_at='2026-01-01T00:00:00Z'))
        self.assertEqual(self.corrections([self.row,self.prior,changed]),[])
    def test_equal_reviews_written_in_different_text_agree(self):
        # Reviews of two observations of one request name the same instant.
        self.prepare()
        first=self.corrections()[0]
        at=fixtures.AT.replace('Z','.000000Z') if '.' not in fixtures.AT else fixtures.AT.replace('Z','000Z')
        second=Receipt.make('attribution',None,dict(first.payload,effective_at=at),entity_ref=first.entity_ref,
            supersedes=self.prior.fact_ref,occurred_at=at,observed_at=fixtures.AT,producer=first.producer,evidence=first.evidence)
        self.ledger.ingest_receipts([first,second])
        self.assertEqual([g['key']['wi'] for g in self.query()['groups']],['wi_20261005_904'])
        self.assertNotIn('attribution_unknown',self.query()['groups'][0]['cost']['reasons'])
