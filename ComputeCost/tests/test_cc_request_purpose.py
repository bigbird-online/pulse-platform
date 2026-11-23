"""Real CC input/tool/result prefixes exercise the collector and priced query."""
from copy import deepcopy
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import patch
from pathlib import Path
import json
import tempfile
import unittest

from pulse_compute_cost import mcp_request_purpose as m
from pulse_compute_cost.cc_request_purpose import POLICY, request_bindings
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import REGISTRY_SCHEMA, collect_registered_sources, read_cc
from pulse_compute_cost.receipts import Receipt, current_receipts, reference
from test_mcp_cc_session_wi import SESSION, WI, OTHER_WI, CATALOG, BEFORE, usage, declaration, call

OBSERVED = '2026-01-02T00:00:00Z'


class RequestPurposeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.logs = self.root/'projects'; self.logs.mkdir()
        self.path = self.logs/(SESSION+'.jsonl'); self.ledger = UsageLedger(self.root/'ledger')
        self.source = {'id':'cc', 'kind':'cc', 'path':str(self.logs), 'session_wi_mcp_since':BEFORE}
        self.registry = {'schema':REGISTRY_SCHEMA, 'sources':[self.source]}
        self.main = []; self.children = {}

    def request(self, label, minute, wi=None, *, legacy=False):
        parent = self.main[-1]['uuid'] if self.main else None
        user = {'type':'user', 'sessionId':SESSION, 'isSidechain':False, 'uuid':'input-'+label,
                'parentUuid':parent, 'promptId':'prompt-'+label,
                'timestamp':f'2026-01-01T01:{minute:02d}:00Z', 'message':{'content':'private original '+label}}
        first = usage(SESSION, minute+1, label+'-before'); first['parentUuid'] = user['uuid']
        self.main.extend([user, first]); done = first
        if wi:
            use, done = declaration(SESSION, minute+3, 'tool-'+label, wi)
            use['parentUuid'] = first['uuid']; done['parentUuid'] = use['uuid']; done['promptId'] = user['promptId']
            if not legacy:
                done['message']['content'][0]['content'] = m.handle(call(wi), clock=lambda:use['timestamp'], host='cc')['content']
            self.main.extend([use, done])
        last = usage(SESSION, minute+5, label+'-after'); last['parentUuid'] = done['uuid']; self.main.append(last)
        return user, last

    def write(self):
        for path, rows in [(self.path,self.main), *self.children.items()]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))

    def collect(self):
        self.write(); return collect_registered_sources(self.registry,self.ledger)

    def query(self, **kwargs):
        return query_collaboration(self.ledger, CollaborationQuery(scope='activity', from_date='2026-01-01',
            to_date='2026-01-02', timezone='UTC', group_by=('wi',), **kwargs), catalog=CATALOG)

    def states(self):
        active, conflicts = current_receipts(self.ledger.receipt_snapshot()['facts'])
        self.assertFalse(conflicts)
        return {r.payload['target_ref']:(r.payload['state'],r.payload['wi']) for r in active
                if r.kind=='attribution' and r.producer==POLICY}

    def target(self, ident):
        return reference('usage',[SESSION,'msg-'+ident])

    def test_reused_session_has_each_request_and_no_ambient_wi(self):
        self.request('a',0,WI); self.request('b',10,OTHER_WI); self.request('c',20)
        self.collect(); states=self.states()
        self.assertEqual(states[self.target('a-before')],('assigned',WI))
        self.assertEqual(states[self.target('a-after')],('assigned',WI))
        self.assertEqual(states[self.target('b-before')],('assigned',OTHER_WI))
        self.assertEqual(states[self.target('b-after')],('assigned',OTHER_WI))
        self.assertEqual(states[self.target('c-before')],('unassigned',None))
        self.assertEqual(states[self.target('c-after')],('unassigned',None))
        self.assertEqual({g['key']['wi'] for g in self.query()['groups']},{WI,OTHER_WI,'unassigned'})
        self.assertEqual(self.collect()['inserted'],0)

    def test_a_request_declared_none_is_non_wi_work_not_unassigned(self):
        self.request('a',0,'none'); self.request('b',10,WI)
        self.collect(); states=self.states()
        self.assertEqual(states[self.target('a-before')],('assigned',None))
        self.assertEqual(states[self.target('b-after')],('assigned',WI))
        usage=self.query()['summary']['usage']
        self.assertEqual((usage['no_wi_event_count'],usage['unassigned_event_count']),(3,0))
        self.assertEqual(self.collect()['inserted'],0)

    def test_cc_interrupt_marker_is_not_a_second_request(self):
        for marker in ('[Request interrupted by user]', '[Request interrupted by user for tool use]'):
            with self.subTest(marker=marker):
                self.main = []; self.ledger = UsageLedger(self.root/('ledger-'+str(len(marker))))
                user, last = self.request('a', 0, WI)
                # CC writes the marker under the interrupted request's promptId.
                self.main.append({'type':'user', 'sessionId':SESSION, 'isSidechain':False, 'uuid':'interrupt',
                    'parentUuid':last['uuid'], 'promptId':user['promptId'], 'timestamp':'2026-01-01T01:07:00Z',
                    'message':{'role':'user', 'content':[{'type':'text', 'text':marker}]}})
                result = self.collect()
                self.assertNotEqual(result['sources'][0]['reason'], 'cc_request_declaration_boundary_unproved')
                self.assertEqual(self.states()[self.target('a-after')], ('assigned', WI))

    def test_empty_separator_line_does_not_void_declarations(self):
        self.request('a',0,WI); self.request('c',20)
        self.write()
        rows = self.path.read_text().splitlines(keepends=True)
        self.path.write_text(''.join(rows[:2]) + '\n' + ''.join(rows[2:]))
        collect_registered_sources(self.registry,self.ledger)
        states = self.states()
        self.assertEqual(states[self.target('a-after')],('assigned',WI))
        self.assertEqual(states[self.target('c-after')],('unassigned',None))

    def test_verified_legacy_scope_correction_keeps_raw_amounts_and_old_queries(self):
        self.request('a',0,WI,legacy=True); self.request('b',10,OTHER_WI,legacy=True)
        # Seed the historical registered collector, including its original
        # source provenance. Direct read_cc ingestion lacks that provenance
        # and would legitimately append evidence revisions on first collect.
        with patch('pulse_compute_cost.cc_request_purpose.request_bindings', return_value={}), \
             patch('pulse_compute_cost.receipt_sources.datetime') as clock:
            clock.now.return_value = datetime.fromisoformat(OBSERVED.replace('Z','+00:00'))
            self.collect()
        old=self.ledger.receipt_snapshot(); before=self.query(watermark=old['watermark'])
        as_of=datetime.now(timezone.utc).isoformat(); before_as_of=self.query(as_of=as_of)
        self.assertEqual({g['key']['wi'] for g in before['groups']},{'ambiguous'})
        self.collect(); after=self.query()
        self.assertEqual({g['key']['wi'] for g in after['groups']},{WI,OTHER_WI})
        self.assertEqual(before['summary']['usage']['tokens'],after['summary']['usage']['tokens'])
        priced=lambda q:sum(Decimal(g['cost']['partial_machine_cash_usd'] or '0')for g in q['groups'])
        self.assertEqual(priced(before),priced(after))
        self.assertEqual(self.query(watermark=old['watermark']),before)
        self.assertEqual([r.to_dict()for r in self.ledger.receipt_snapshot()['facts']if r.kind=='usage'],
                         [r.to_dict()for r in old['facts']if r.kind=='usage'])
        active,_=current_receipts(self.ledger.receipt_snapshot()['facts'])
        self.assertFalse([r for r in active if r.producer==m.POLICY])
        qualified=[r for r in active if r.producer==POLICY]
        original_claims={r.fact_ref for r in old['facts']if r.kind=='attribution'}
        self.assertTrue(qualified)
        self.assertEqual({r.supersedes for r in qualified},original_claims)
        self.assertTrue(original_claims <= {r.fact_ref for r in self.ledger.receipt_snapshot()['facts']})
        self.assertEqual(self.query(as_of=as_of),before_as_of)
        self.assertEqual(self.collect()['inserted'],0)

    def test_child_keeps_unique_original_prompt_after_another_request(self):
        user,_=self.request('a',0,WI);self.request('b',10,OTHER_WI)
        child={**user,'uuid':'child-input','parentUuid':None,'isSidechain':True,'agentId':'child',
               'timestamp':'2026-01-01T01:02:00Z','message':{'content':'private delegated task'}}
        answer=usage(SESSION,25,'child',sidechain=True);answer.update(agentId='child',parentUuid='child-input')
        path=self.logs/SESSION/'subagents/agent-child.jsonl';self.children[path]=[child,answer]
        self.collect();self.assertEqual(self.states()[self.target('child')],('assigned',WI))
        child['promptId']='independent-child-input'
        self.collect();self.assertEqual(self.states()[self.target('child')],('unassigned',None))

    def test_child_attachment_written_after_its_child_keeps_the_request(self):
        user,_=self.request('a',0,WI)
        child={**user,'uuid':'child-input','parentUuid':None,'isSidechain':True,'agentId':'child',
               'timestamp':'2026-01-01T01:02:00Z','message':{'content':'private delegated task'}}
        context={'type':'attachment','uuid':'child-context','parentUuid':'child-input','isSidechain':True,
                 'agentId':'child','sessionId':SESSION,'timestamp':'2026-01-01T01:02:01Z','attachment':{'type':'skill_listing'}}
        late={**context,'uuid':'child-session-context','parentUuid':'child-context','timestamp':'2026-01-01T01:02:02Z'}
        answer=usage(SESSION,25,'child',sidechain=True);answer.update(agentId='child',parentUuid='child-session-context')
        # CC appends the context attachment after the record that names it as parent.
        path=self.logs/SESSION/'subagents/agent-child.jsonl';self.children[path]=[child,late,answer,context]
        self.collect();self.assertEqual(self.states()[self.target('child')],('assigned',WI))

    def test_message_to_a_resumed_child_moves_its_later_work_to_the_sending_request(self):
        user,_=self.request('a',0,WI)
        child={**user,'uuid':'child-input','parentUuid':None,'isSidechain':True,'agentId':'child',
               'timestamp':'2026-01-01T01:02:00Z','message':{'content':'private delegated task'}}
        before=usage(SESSION,3,'child-before',sidechain=True);before.update(agentId='child',parentUuid='child-input')
        self.request('b',10,OTHER_WI)
        # SendMessage from request b reaches the child as a meta input under b's promptId.
        message={'type':'user','uuid':'child-message','parentUuid':before['uuid'],'isSidechain':True,
                 'isMeta':True,'origin':{'kind':'coordinator'},'agentId':'child','sessionId':SESSION,
                 'promptId':'prompt-b','timestamp':'2026-01-01T01:16:00Z',
                 'message':{'content':'The coordinator sent a message while you were working: continue'}}
        after=usage(SESSION,17,'child-after',sidechain=True);after.update(agentId='child',parentUuid='child-message')
        path=self.logs/SESSION/'subagents/agent-child.jsonl';self.children[path]=[child,before,message,after]
        self.collect();states=self.states()
        self.assertEqual(states[self.target('child-before')],('assigned',WI))
        self.assertEqual(states[self.target('child-after')],('assigned',OTHER_WI))

    def test_deferred_tools_record_written_after_its_reply_keeps_the_request(self):
        _,last=self.request('a',0,WI)
        # CC writes the deferred-tools record a reply was generated after as the
        # next record, stamped a few milliseconds after the reply.
        record={'type':'attachment','uuid':'deferred','parentUuid':last['uuid'],'isSidechain':False,
                'sessionId':SESSION,'timestamp':'2026-01-01T01:07:00.005Z',
                'attachment':{'type':'deferred_tools_record','entries':[]}}
        reply=usage(SESSION,7,'reply');reply['parentUuid']='deferred'
        later=usage(SESSION,8,'later');later['parentUuid']=reply['uuid']
        self.main.extend([reply,record,later])
        self.collect();states=self.states()
        self.assertEqual(states[self.target('reply')],('assigned',WI))
        self.assertEqual(states[self.target('later')],('assigned',WI))
        record['attachment']['type']='skill_listing'
        self.collect();self.assertEqual(self.states()[self.target('later')],('unassigned',None))

    def test_forked_subagent_keeps_its_parent_agents_request(self):
        # A fork's transcript starts with its parent agent's records, copied
        # under the fork's agentId; its own messages continue that chain.
        user,_=self.request('a',0,WI);self.request('b',10,OTHER_WI)
        child={**user,'uuid':'child-input','parentUuid':None,'isSidechain':True,'agentId':'child',
               'timestamp':'2026-01-01T01:02:00Z','message':{'content':'private delegated task'}}
        answer=usage(SESSION,3,'child',sidechain=True);answer.update(agentId='child',parentUuid='child-input')
        fork=usage(SESSION,25,'fork',sidechain=True);fork.update(agentId='fork',parentUuid=answer['uuid'])
        agents=self.logs/SESSION/'subagents'
        self.children[agents/'agent-child.jsonl']=[child,answer]
        self.children[agents/'agent-fork.jsonl']=[{**child,'agentId':'fork'},{**answer,'agentId':'fork'},fork]
        self.collect();self.assertEqual(self.states()[self.target('fork')],('assigned',WI))
        self.children[agents/'agent-fork.jsonl']=[fork]  # Without the copied prefix there is no proof.
        self.collect();self.assertEqual(self.states()[self.target('fork')],('unassigned',None))

    def test_compaction_summary_continues_the_request_it_names(self):
        for name, prompt, minute, wi in (('declared', 'prompt-a', 6, WI), ('unknown', 'prompt-x', 6, None),
                                          ('later_prompt', 'prompt-b', 6, None)):
            with self.subTest(name):
                self.main = []; self.ledger = UsageLedger(self.root/('ledger-'+name))
                user, last = self.request('a', 0, WI); self.request('b', 10)
                # CC auto-compacts mid-request: a parentless boundary, then a
                # summary carrying the in-flight request's promptId.
                boundary = {'type':'system', 'subtype':'compact_boundary', 'sessionId':SESSION, 'isSidechain':False,
                            'uuid':'boundary', 'parentUuid':None, 'logicalParentUuid':last['uuid'],
                            'timestamp':f'2026-01-01T01:{minute:02d}:10Z'}
                summary = {'type':'user', 'sessionId':SESSION, 'isSidechain':False, 'uuid':'summary',
                           'parentUuid':'boundary', 'promptId':prompt, 'isCompactSummary':True,
                           'timestamp':f'2026-01-01T01:{minute:02d}:20Z', 'message':{'content':'private summary'}}
                after = usage(SESSION, minute+1, 'compacted'); after['parentUuid'] = 'summary'
                self.main[5:5] = [boundary, summary, after]
                self.collect()
                self.assertEqual(self.states()[self.target('compacted')], ('assigned', wi) if wi else ('unassigned', None))
                self.assertEqual(self.states()[self.target('b-after')], ('unassigned', None))

    def test_refused_declaration_call_leaves_other_declarations_valid(self):
        self.request('a',0,WI)
        use,done=declaration(SESSION,7,'refused-tool',WI,error=True,ack='{"reason":"wi_only_arguments_required"}')
        use['message']['content'][0]['input']={'work_item_id':WI}
        use['parentUuid']=self.main[-1]['uuid'];done['parentUuid']=use['uuid']
        self.main.extend([use,done]);self.collect()
        self.assertEqual(self.states()[self.target('a-after')],('assigned',WI))

    def test_a_later_declaration_in_one_request_switches_its_wi_from_then_on(self):
        # An Agent that creates a WI mid-request declares it; earlier usage
        # keeps the request's first WI.
        user,last=self.request('a',0,WI)
        use,done=declaration(SESSION,7,'second-tool',OTHER_WI)
        use['parentUuid']=last['uuid'];done.update(parentUuid=use['uuid'],promptId=user['promptId'])
        done['message']['content'][0]['content']=m.handle(call(OTHER_WI),clock=lambda:use['timestamp'],host='cc')['content']
        after=usage(SESSION,9,'a-switched');after['parentUuid']=done['uuid']
        self.main.extend([use,done,after]);self.collect()
        states=self.states()
        self.assertEqual(states[self.target('a-before')],('assigned',WI))
        self.assertEqual(states[self.target('a-after')],('assigned',WI))
        self.assertEqual(states[self.target('a-switched')],('assigned',OTHER_WI))

    def test_independent_contrary_claim_and_old_as_of_stay_visible(self):
        self.request('a',0,WI);self.collect();old=self.ledger.receipt_snapshot();before=self.query(watermark=old['watermark'])
        at=self.main[1]['timestamp'];target=self.target('a-before')
        independent=Receipt.make('attribution','independent',{'target_ref':target,'wi':OTHER_WI,'executor':None,
            'responsible':None,'delegator':None,'state':'assigned','policy':'independent_review.v1','effective_at':at},
            occurred_at=at,observed_at=OBSERVED,producer='independent_review.v1')
        self.ledger.ingest_receipts([independent]);self.collect()
        self.assertIn('ambiguous',{g['key']['wi']for g in self.query()['groups']})
        self.assertIn(independent.fact_ref,{r.fact_ref for r in self.ledger.receipt_snapshot()['facts']})
        self.assertEqual(self.query(watermark=old['watermark']),before)

    def test_request_ack_cannot_fall_back_to_session_for_unproved_boundaries(self):
        for fault in ('missing_parent','wrong_prompt','foreign_tool_source','future_input','duplicate_input'):
            with self.subTest(fault=fault):
                self.main=[];self.request('a',0,WI)
                user,first,use,done,last=self.main
                if fault=='missing_parent':use['parentUuid']='absent'
                elif fault=='wrong_prompt':done['promptId']='foreign'
                elif fault=='foreign_tool_source':done['sourceToolAssistantUUID']='foreign'
                elif fault=='future_input':user['timestamp']='2026-01-01T01:09:00Z'
                else:self.main.append({**user,'message':{'content':'conflicting immutable input'}})
                self.write();raw=read_cc(self.path,OBSERVED);diagnostics=[]
                ds=m.cc_native_completions([self.path],raw,diagnostics,since=BEFORE)
                scoped=request_bindings([self.path],raw,ds)
                self.assertTrue(scoped or diagnostics)
                self.assertTrue(all(not b['declarations'] for b in scoped.values()))

    def test_partial_legacy_history_preserves_unproved_heads_and_new_input_is_unknown(self):
        self.request('a',0,WI,legacy=True);self.request('b',10,WI,legacy=True)
        self.main[8].pop('promptId')
        with patch('pulse_compute_cost.cc_request_purpose.request_bindings', return_value={}):
            self.collect()
        frozen=self.ledger.receipt_snapshot()
        old_claims=[r for r in frozen['facts'] if r.kind=='attribution' and r.producer==m.POLICY]
        self.request('c',20);result=self.collect();active,_=current_receipts(self.ledger.receipt_snapshot()['facts'])
        self.assertTrue({r.fact_ref for r in old_claims} <= {r.fact_ref for r in active})
        self.assertEqual(self.states()[self.target('c-before')],('unassigned',None))
        self.assertEqual(self.states()[self.target('c-after')],('unassigned',None))
        self.assertIn('cc_request_declaration_boundary_unproved',str(result))
        self.assertEqual(self.collect()['inserted'],0)

    def test_partial_mixed_protocol_and_missing_native_boundary_cannot_inherit_wi(self):
        for legacy in (True,False):
            with self.subTest(legacy=legacy):
                self.main=[];self.request('a',0,WI,legacy=True);self.request('b',10,OTHER_WI,legacy=legacy)
                self.main[8].pop('promptId');self.request('c',20)
                self.main[-2]['parentUuid']='absent'
                self.collect();states=self.states()
                self.assertEqual(states[self.target('a-before')],('assigned',WI))
                for name in ('b-before','b-after','c-before','c-after'):
                    self.assertEqual(states[self.target(name)],('unassigned',None))

    def test_server_cc_rule_and_ack_are_request_specific(self):
        reply=m.handle(call(WI),clock=lambda:OBSERVED,host='cc')
        self.assertEqual(reply['structuredContent']['schema'],'cc_request_wi/v1')
        initialized=m.handle({'method':'initialize','params':{}},clock=lambda:OBSERVED,host='cc')
        self.assertIn(m.CC_RULE,initialized['instructions'])
        # The rule appears once: the tool description only points to it.
        [tool]=m.handle({'method':'tools/list','params':{}},clock=lambda:OBSERVED,host='cc')['tools']
        self.assertNotIn('子代理',tool['description'])
        self.assertIn('并行推进多个子 WI',m.CC_RULE)
        self.assertNotIn('人类消息触发',m.CC_RULE)
        self.assertEqual(m.cc_transport(WI)['schema'],'cc_session_wi/v1')


if __name__=='__main__':
    unittest.main()
