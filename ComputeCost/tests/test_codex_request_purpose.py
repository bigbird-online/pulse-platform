"""A delegated cost claim needs exact root-turn and bounded authoritative purpose."""
import unittest
from pathlib import Path
import json,tempfile
from pulse_compute_cost.codex_request_purpose import native_root_requests,root_request_attributions
from pulse_compute_cost.receipts import Receipt,reference
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.collaboration_query import CollaborationQuery,query_collaboration
AT='2026-09-26T01:00:00Z'
END='2026-09-26T01:10:00Z'
WI='wi_20260924_219'
class RootRequestTests(unittest.TestCase):
    def setUp(self):
        self.proof=reference('fixture','native evidence')
        self.parent=self.usage('parent','parent-scope',AT,END)
        self.child=self.usage('child','child-scope','2026-09-26T01:02:00Z','2026-09-26T01:05:00Z')
        self.task=Receipt.make('attribution','task',{'target_ref':self.parent.entity_ref,'wi':WI,'executor':'parent-agent',
            'responsible':None,'delegator':None,'state':'assigned','policy':'scl_host_session_binding.v1','effective_at':AT},
            occurred_at=AT,observed_at=END,producer='scl_host_session_binding.v1',evidence=(self.proof,))
        self.links={self.child.entity_ref:{'parent':self.parent.entity_ref,'parent_scope':self.parent.payload['native_scope_ref'],
            'child_scope':self.child.payload['native_scope_ref'],'evidence':(self.proof,)}}
    def usage(self,name,scope,start,end):
        row=usage_receipt('codex',name,{'input_tokens':100,'cached_input_tokens':0,'cache_write_input_tokens':0,'output_tokens':2},at=end,observed=END,model='fixture')
        return Receipt.make('usage',name,{**row.payload,'native_scope_ref':reference('native_session',scope),'interval_start':start,'interval_end':end},occurred_at=end,observed_at=END,producer='codex_rollout.v1')
    def test_exact_root_turn_attributes_without_agent_quantity_or_rights(self):
        rows=root_request_attributions([self.parent,self.child,self.task],self.links,END)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0].payload['wi'],WI)
        self.assertIsNone(rows[0].payload['executor']);self.assertNotIn('execution_ref',rows[0].payload)
        with tempfile.TemporaryDirectory() as d:
            ledger=UsageLedger(d);ledger.ingest_receipts([self.parent,self.child,self.task]);mark=ledger.receipt_snapshot()['watermark']
            q=CollaborationQuery(group_by=('wi',),watermark=mark);before=query_collaboration(ledger,q)
            ledger.ingest_receipts(rows);after=query_collaboration(ledger,CollaborationQuery(group_by=('wi',)))
            self.assertEqual(after['summary']['usage']['wi_unassigned_event_count'],0)
            self.assertEqual(after['summary']['usage']['tokens'],before['summary']['usage']['tokens'])
            self.assertEqual(query_collaboration(ledger,q),before)
            self.assertEqual(ledger.ingest_receipts(rows)['inserted'],0)
    def test_parent_scope_or_current_owner_is_not_a_request_proof(self):
        self.assertEqual(root_request_attributions([self.parent,self.child],self.links,END),[])
        other=self.usage('other-root','parent-scope',AT,END)
        self.assertEqual(root_request_attributions([other,self.child,self.task],self.links,END),[])
        self.links[self.child.entity_ref]['parent_scope']=self.proof
        self.assertEqual(root_request_attributions([self.parent,self.child,self.task],self.links,END),[])
    def test_child_outside_root_interval_stays_unassigned(self):
        child=self.usage('child','child-scope',AT,'2026-09-26T01:11:00Z')
        self.assertEqual(root_request_attributions([self.parent,child,self.task],self.links,END),[])
    def test_contrary_root_assignments_are_not_selected(self):
        other=Receipt.make('attribution','other',{**self.task.payload,'wi':'wi_20260924_220'},occurred_at=AT,observed_at=END,producer=self.task.producer,evidence=(self.proof,))
        self.assertEqual(root_request_attributions([self.parent,self.child,self.task,other],self.links,END),[])
    def test_independent_contrary_parent_review_is_not_ignored(self):
        other=Receipt.make('attribution','independent',{**self.task.payload,'wi':'wi_20260924_220','policy':'reviewed_activity.v1'},
            occurred_at=AT,observed_at=END,producer='reviewed_activity.v1',evidence=(self.proof,))
        self.assertEqual(root_request_attributions([self.parent,self.child,self.task,other],self.links,END),[])
    def test_native_parent_without_root_turn_does_not_create_link(self):
        with tempfile.TemporaryDirectory() as d:
            child='00000000-0000-4000-8000-000000000001';parent='00000000-0000-4000-8000-000000000002'
            turn='00000000-0000-4000-8000-000000000003';root='00000000-0000-4000-8000-000000000004'
            path=Path(d)/('rollout-2026-09-26T01-00-00-'+child+'.jsonl')
            meta={'type':'session_meta','timestamp':AT,'payload':{'id':child,'source':{'subagent':{'thread_spawn':{'parent_thread_id':parent}}},'parent_thread_id':parent}}
            row={'type':'event_msg','timestamp':AT,'payload':{'type':'task_started','turn_id':turn}}
            path.write_text(json.dumps(meta)+'\n'+json.dumps(row)+'\n')
            self.assertEqual(native_root_requests([path]),{})
            row['payload']['root_turn_id']=root;path.write_text(json.dumps(meta)+'\n'+json.dumps(row)+'\n')
            parent_path=Path(d)/('rollout-2026-09-26T01-00-00-'+parent+'.jsonl')
            parent_path.write_text(json.dumps({'type':'session_meta','timestamp':AT,'payload':{'id':parent}})+'\n')
            self.assertEqual(len(native_root_requests([parent_path,path])),1)
            row['payload']['root_turn_id']='00000000-0000-4000-8000-000000000005'
            with path.open('a') as f:f.write(json.dumps(row)+'\n')
            self.assertEqual(native_root_requests([parent_path,path]),{})



class NativeRootOwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.root, self.middle, self.leaf = [self.uuid(i) for i in (101, 102, 103)]
        self.root_turn, self.middle_turn, self.leaf_turn = [self.uuid(i) for i in (111, 112, 113)]
        self.paths = [self.write(self.root, None, self.root_turn, AT, END),
                      self.write(self.middle, self.root, self.middle_turn, '2026-09-26T01:01:00Z', '2026-09-26T01:09:00Z'),
                      self.write(self.leaf, self.middle, self.leaf_turn, '2026-09-26T01:02:00Z', '2026-09-26T01:05:00Z', self.root_turn)]
    def uuid(self, value):
        return '00000000-0000-4000-8000-'+str(value).zfill(12)
    def target(self, thread, turn):
        from pulse_compute_cost.adapters import _stable_event_id
        return reference('usage', _stable_event_id('codex', 'registered-source', thread+':'+turn))
    def write(self, thread, parent, turn, start, end, root_turn=None):
        meta = {'id': thread, 'session_id': self.root, 'cwd': '/fixture', 'originator': 'fixture', 'source': 'cli'}
        if parent: meta['parent_thread_id'] = parent
        task = {'type': 'task_started', 'turn_id': turn}
        if root_turn: task['root_turn_id'] = root_turn
        counter = {'input_tokens': 120, 'cached_input_tokens': 20, 'cache_write_input_tokens': 0, 'output_tokens': 10}
        rows = [{'type': 'session_meta', 'timestamp': start, 'payload': meta},
                {'type': 'event_msg', 'timestamp': start, 'payload': task},
                {'type': 'turn_context', 'timestamp': start, 'payload': {'model': 'fixture'}},
                {'type': 'event_msg', 'timestamp': end, 'payload': {'type': 'token_count', 'info': {'total_token_usage': counter, 'last_token_usage': counter}}},
                {'type': 'event_msg', 'timestamp': end, 'payload': {'type': 'task_complete', 'turn_id': turn}}]
        path = self.directory / ('rollout-2026-09-26T01-00-00-'+thread+'.jsonl')
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        return path
    def test_nested_child_uses_root_thread_not_direct_parent(self):
        link = native_root_requests(self.paths)[self.target(self.leaf, self.leaf_turn)]
        self.assertEqual(link['parent'], self.target(self.root, self.root_turn))
        self.assertEqual(link['parent_scope'], reference('native_session', ['codex', self.root]))
        self.assertNotEqual(link['parent'], self.target(self.middle, self.root_turn))
    def test_missing_intermediate_cannot_prove_root_owner(self):
        self.assertNotIn(self.target(self.leaf, self.leaf_turn), native_root_requests([self.paths[0], self.paths[2]]))
    def test_cycle_cannot_prove_root_owner(self):
        rows = [json.loads(r) for r in self.paths[0].read_text().splitlines()]
        rows[0]['payload']['parent_thread_id'] = self.leaf
        self.paths[0].write_text(''.join(json.dumps(r)+'\n' for r in rows))
        self.assertEqual(native_root_requests(self.paths), {})
    def test_explicit_root_session_conflict_is_not_overwritten(self):
        rows = [json.loads(r) for r in self.paths[2].read_text().splitlines()]
        rows[0]['payload']['session_id'] = self.uuid(999)
        self.paths[2].write_text(''.join(json.dumps(r)+'\n' for r in rows))
        self.assertNotIn(self.target(self.leaf, self.leaf_turn), native_root_requests(self.paths))
    def test_copied_parent_context_is_not_another_child_request(self):
        middle = [json.loads(r) for r in self.paths[1].read_text().splitlines()]
        middle[1]['payload']['root_turn_id'] = self.root_turn
        self.paths[1].write_text(''.join(json.dumps(r)+'\n' for r in middle))
        leaf = [json.loads(r) for r in self.paths[2].read_text().splitlines()]
        leaf[1:1] = middle
        self.paths[2].write_text(''.join(json.dumps(r)+'\n' for r in leaf))
        links = native_root_requests(self.paths)
        self.assertIn(self.target(self.leaf, self.leaf_turn), links)
        self.assertNotIn(self.target(self.leaf, self.middle_turn), links)
        self.assertEqual(links[self.target(self.leaf, self.leaf_turn)]['parent'], self.target(self.root, self.root_turn))
    def test_native_usage_to_root_purpose_to_query_conserves_history(self):
        from pulse_compute_cost.receipt_sources import read_codex
        usages = read_codex(self.directory, END)
        self.assertEqual(len(usages), 3)
        root_ref = self.target(self.root, self.root_turn)
        task = Receipt.make('attribution', 'explicit-root', {'target_ref': root_ref, 'wi': WI,
            'executor': None, 'responsible': None, 'delegator': None, 'state': 'assigned',
            'policy': 'codex_request_purpose.v1', 'effective_at': AT},
            occurred_at=AT, observed_at=END, producer='codex_request_purpose.v1', evidence=(reference('fixture','root-purpose'),))
        catalog = {'data':[{'id':'fixture','pricing':{'prompt':'1','completion':'1','input_cache_read':'0.1','input_cache_write':'2'}}]}
        ledger = UsageLedger(self.directory/'ledger'); ledger.ingest_receipts([*usages, task])
        mark = ledger.receipt_snapshot()['watermark']
        old = query_collaboration(ledger, CollaborationQuery(group_by=('wi',), watermark=mark), catalog=catalog)
        links = native_root_requests(self.paths)
        claims = root_request_attributions([*usages, task], links, END)
        leaf_claims = [r for r in claims if r.payload['target_ref'] == self.target(self.leaf, self.leaf_turn)]
        self.assertEqual(len(leaf_claims), 1)
        self.assertEqual(leaf_claims[0].payload['wi'], WI)
        self.assertIsNone(leaf_claims[0].payload['executor'])
        self.assertNotIn('execution_ref', leaf_claims[0].payload)
        ledger.ingest_receipts(claims)
        after = query_collaboration(ledger, CollaborationQuery(group_by=('wi',)), catalog=catalog)
        self.assertEqual(after['summary']['usage']['known_total_tokens'], 390)
        self.assertEqual(after['summary']['usage']['tokens'], old['summary']['usage']['tokens'])
        self.assertEqual(after['summary']['usage']['event_count'], old['summary']['usage']['event_count'])
        self.assertEqual(after['summary']['cost']['partial_machine_cash_usd'], '336')
        self.assertEqual(after['summary']['cost']['partial_machine_cash_usd'], old['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(query_collaboration(ledger, CollaborationQuery(group_by=('wi',), watermark=mark), catalog=catalog), old)
        again = root_request_attributions(ledger.receipt_snapshot()['facts'], links, END)
        self.assertEqual(ledger.ingest_receipts(again)['inserted'], 0)

if __name__=='__main__':unittest.main()
