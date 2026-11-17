"""Executable Hook/declaration contract, exact child propagation and safe enrollment."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from native_purpose_fixture import capture,declare
from pulse_compute_cost.receipt_sources import collect_registered_sources
import test_root_request_collector as child_fixture
import test_scl_session_attribution as f

ROOT=Path(__file__).resolve().parents[2]
WRAPPER=ROOT/'ComputeCost/runtime/codex-request-purpose.py'
spec=importlib.util.spec_from_file_location('request_install',ROOT/'Stats/request_purpose.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)

class ExecutableFrontendTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name).resolve()
        self.env={k:v for k,v in os.environ.items() if not k.startswith(('CODEX_','HARNESS_','SCL_','PULSE_'))}
        self.env.update(HOME=str(self.home),PYTHONDONTWRITEBYTECODE='1')
    def invoke(self,data):
        return subprocess.run([sys.executable,str(WRAPPER),'hook'],input=json.dumps(data),env=self.env,
            capture_output=True,text=True,timeout=15)
    def test_actual_hook_command_to_native_session_declaration(self):
        payload={'hook_event_name':'UserPromptSubmit','session_id':f.THREAD,'turn_id':'new-executable-turn','prompt':'ordinary new request'}
        response=self.invoke(payload)
        self.assertEqual(response.returncode,0,response.stderr)
        context=json.loads(response.stdout)['hookSpecificOutput']['additionalContext']
        command=shlex.split(context.split('before business work: ',1)[1].split(' --purpose <',1)[0])
        declared=subprocess.run(command+['--purpose','none'],env={**self.env,'CODEX_THREAD_ID':f.THREAD},capture_output=True,text=True,timeout=15)
        self.assertEqual(declared.returncode,0,declared.stdout+declared.stderr)
        self.assertEqual(json.loads(declared.stdout)['origin'],'agent_declaration')
        self.assertEqual(len(list((self.home/'.local/state/pulse/compute-cost/native-requests').rglob('*.purpose.json'))),0)
    def test_malformed_native_envelope_never_breaks_the_business_prompt(self):
        for payload in ([],None,1,'invalid',{'hook_event_name':'UserPromptSubmit'}):
            result=self.invoke(payload)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('systemMessage',json.loads(result.stdout))
            self.assertNotIn('decision',json.loads(result.stdout))

class PurposeChildTests(child_fixture.RootCollectorTests):
    def setUp(self):
        super().setUp()
        self.spool=(self.root/'request-inputs').resolve();self.spool.mkdir(mode=0o700)
        self.source['request_purpose_directory']=str(self.spool)
    def root_declaration(self,purpose):
        original=capture({'hook_event_name':'UserPromptSubmit','session_id':f.THREAD,
             'turn_id':child_fixture.ROOT_TURN,'prompt':'one fresh root request'},self.spool)
        declare(self.spool,original['target_ref'],original['request_ref'],purpose,session=f.THREAD)
    def test_declared_root_wi_propagates_to_exact_child_despite_two_open_bindings(self):
        self.bind(f.OTHER_WI,f.THREAD,f.BEFORE)
        self.root_declaration(f.WI)
        result=self.collect()
        self.assertEqual({g['key']['wi'] for g in result['groups'] if g['usage']['event_count']},{f.WI})
        self.assertEqual(result['summary']['usage']['known_total_tokens'],260)
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)['inserted'],0)
    def test_explicit_ordinary_root_propagates_without_inventing_a_wi(self):
        for table in ('source_attachments','source_attachment_history','checkpoints','work_items'):
            self.db.execute('DELETE FROM '+table)
        self.db.commit()
        self.root_declaration('none')
        result=self.collect()
        self.assertEqual({g['key']['wi'] for g in result['groups'] if g['usage']['event_count']},{'none'})
        self.assertEqual(result['summary']['usage']['known_total_tokens'],260)
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)['inserted'],0)

    def _current_child_purpose(self):
        from pulse_compute_cost.receipts import current_receipts
        from pulse_compute_cost.codex_request_purpose import POLICY
        active, conflicts = current_receipts(self.ledger.receipt_snapshot()['facts'])
        rows = [r for r in active if r.kind == 'attribution' and r.producer == POLICY
                and r.payload['target_ref'] == self.child.entity_ref]
        self.assertEqual(len(rows), 1)
        self.assertNotIn(rows[0].entity_ref, conflicts)
        return rows[0]

    def _new_parent_input(self):
        return capture({'hook_event_name': 'UserPromptSubmit', 'session_id': f.THREAD,
            'turn_id': child_fixture.ROOT_TURN, 'prompt': 'fresh steering input requiring its own purpose'}, self.spool)

    def _quantity_state(self):
        from pulse_compute_cost.receipts import current_receipts
        active, _ = current_receipts(self.ledger.receipt_snapshot()['facts'])
        return {r.entity_ref: r.payload for r in active if r.kind == 'usage'}

    def test_new_parent_input_withdraws_already_published_child_purpose(self):
        self.root_declaration(f.WI)
        before = self.collect()
        old_mark = self.ledger.receipt_snapshot()['watermark']
        old_view = self.query(watermark=old_mark)
        old_child = self._current_child_purpose()
        quantities = self._quantity_state()
        self._new_parent_input()
        after = self.collect()
        child = self._current_child_purpose()
        self.assertEqual(child.payload['state'], 'unassigned')
        self.assertIsNone(child.payload['wi'])
        self.assertEqual(child.supersedes, old_child.fact_ref)
        self.assertEqual(self._quantity_state(), quantities)
        self.assertEqual(after['summary']['usage']['event_count'], before['summary']['usage']['event_count'])
        self.assertEqual(after['summary']['cost']['partial_machine_cash_usd'], before['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(self.query(watermark=old_mark), old_view)
        self.assertEqual(collect_registered_sources(self.registry, self.ledger)['inserted'], 0)

    def test_late_matching_declaration_restores_child_on_the_same_version_chain(self):
        self.bind(f.OTHER_WI, f.THREAD, f.BEFORE)
        self.root_declaration(f.WI)
        self.collect()
        original = self._new_parent_input()
        self.collect()
        withdrawn = self._current_child_purpose()
        self.assertEqual(withdrawn.payload['state'], 'unassigned')
        declare(self.spool, original['target_ref'], original['request_ref'], f.WI, session=f.THREAD)
        self.collect()
        restored = self._current_child_purpose()
        self.assertEqual(restored.entity_ref, withdrawn.entity_ref)
        self.assertEqual(restored.supersedes, withdrawn.fact_ref)
        self.assertEqual(restored.payload['state'], 'assigned')
        self.assertEqual(restored.payload['wi'], f.WI)
        self.assertEqual(collect_registered_sources(self.registry, self.ledger)['inserted'], 0)

    def test_conflicting_parent_purposes_do_not_leave_a_clean_child_wi(self):
        self.root_declaration(f.WI)
        self.collect()
        original = self._new_parent_input()
        declare(self.spool, original['target_ref'], original['request_ref'], f.OTHER_WI, session=f.THREAD)
        self.collect()
        self.assertEqual(self._current_child_purpose().payload['state'], 'unassigned')
        self.assertIsNone(self._current_child_purpose().payload['wi'])

    def test_ordinary_parent_withdrawal_also_withdraws_child_none(self):
        for table in ('source_attachments', 'source_attachment_history', 'checkpoints', 'work_items'):
            self.db.execute('DELETE FROM ' + table)
        self.db.commit()
        self.root_declaration('none')
        self.collect()
        self.assertEqual(self._current_child_purpose().payload['work_kind'], 'none')
        self._new_parent_input()
        self.collect()
        current = self._current_child_purpose()
        self.assertEqual(current.payload['state'], 'unassigned')
        self.assertNotIn('work_kind', current.payload)

    def test_later_independent_parent_conflict_invalidates_only_derived_evidence(self):
        from pulse_compute_cost.receipts import Receipt, reference, current_receipts
        self.root_declaration(f.WI)
        self.collect()
        independent = Receipt.make('attribution', ['independent-parent-purpose', self.parent.entity_ref], {
            'target_ref': self.parent.entity_ref, 'wi': f.OTHER_WI, 'executor': None,
            'responsible': None, 'delegator': None, 'state': 'assigned',
            'policy': 'independent-fixture.v1', 'effective_at': f.START},
            occurred_at=f.START, observed_at=f.END, producer='independent-fixture.v1',
            evidence=(reference('fixture', 'separate original purpose evidence'),))
        self.ledger.ingest_receipts([independent])
        self.collect()
        self.assertEqual(self._current_child_purpose().payload['state'], 'unassigned')
        active, _ = current_receipts(self.ledger.receipt_snapshot()['facts'])
        retained = next(r for r in active if r.entity_ref == independent.entity_ref)
        self.assertEqual(retained, independent)

    def test_withdrawal_preserves_independent_child_assignment(self):
        from pulse_compute_cost.receipts import Receipt, reference, current_receipts
        self.root_declaration(f.WI)
        self.collect()
        independent = Receipt.make('attribution', ['independent-child-purpose', self.child.entity_ref], {
            'target_ref': self.child.entity_ref, 'wi': f.OTHER_WI, 'executor': None,
            'responsible': None, 'delegator': None, 'state': 'assigned',
            'policy': 'independent-fixture.v1', 'effective_at': self.child.payload['interval_start']},
            occurred_at=self.child.payload['interval_start'], observed_at=f.END,
            producer='independent-fixture.v1', evidence=(reference('fixture', 'direct child purpose'),))
        self.ledger.ingest_receipts([independent])
        self._new_parent_input()
        self.collect()
        self.assertEqual(self._current_child_purpose().payload['state'], 'unassigned')
        active, _ = current_receipts(self.ledger.receipt_snapshot()['facts'])
        self.assertEqual(next(r for r in active if r.entity_ref == independent.entity_ref), independent)

class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name).resolve();self.revision='a'*40
        self.runtime=self.home/'.local/share/pulse/stats/releases'/self.revision
        (self.runtime/'ComputeCost/runtime').mkdir(parents=True)
        (self.runtime/'ComputeCost/pulse_compute_cost').mkdir(parents=True)
        (self.runtime/'.stats-source-revision').write_text(self.revision+'\n')
        for name in ('runtime/codex-request-purpose.py','pulse_compute_cost/native_request_purpose.py'):
            (self.runtime/'ComputeCost'/name).write_text('fixture installed reader')
        (self.runtime.parent.parent/'current').symlink_to(self.runtime)
        self.registry={'schema':'computecost_source_registry/v1','ledger_root':'unchanged',
             'sources':[{'id':'native','kind':'codex','node':'ownermac','path':'unchanged'},
                        {'id':'other','kind':'pi','path':'unchanged'}]}
        self.hooks={'hooks':{'UserPromptSubmit':[{'hooks':[{'type':'command','command':'original-lifecycle-hook'}]}],
                            'Stop':[{'hooks':[{'type':'command','command':'original-stop-hook'}]}]}}
        self.rpath=self.home/'.local/state/pulse/compute-cost/sources.json';self.hpath=self.home/'.codex/hooks.json'
        for path,value in ((self.rpath,self.registry),(self.hpath,self.hooks)):
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value));path.chmod(0o600)
        self.old=(self.rpath.read_bytes(),self.hpath.read_bytes())
    def apply(self,**kwargs):
        return installer.install(self.home,self.runtime,Path(sys.executable),self.revision,
            expected_registry_sha256=installer.sha(self.old[0]),expected_hooks_sha256=installer.sha(self.old[1]),**kwargs)
    def test_preserves_unrelated_hooks_sources_and_does_not_change_native_trust(self):
        result=self.apply()
        self.assertEqual(result['status'],'configured_pending_native_trust_and_invocation')
        registry=json.loads(self.rpath.read_bytes());hooks=json.loads(self.hpath.read_bytes())
        self.assertEqual(hooks['hooks']['Stop'],self.hooks['hooks']['Stop'])
        self.assertEqual(hooks['hooks']['UserPromptSubmit'][0],self.hooks['hooks']['UserPromptSubmit'][0])
        self.assertEqual(registry['sources'][1],self.registry['sources'][1])
        self.assertEqual(registry['ledger_root'],'unchanged')
        self.old=(self.rpath.read_bytes(),self.hpath.read_bytes());self.apply()
        self.assertEqual((self.rpath.read_bytes(),self.hpath.read_bytes()),self.old)
        self.assertEqual(len(json.loads(self.hpath.read_bytes())['hooks']['UserPromptSubmit']),2)
        self.assertFalse((self.home/'.codex/config.toml').exists())
    def test_stale_review_or_uninstalled_reader_rejects_without_changes(self):
        self.hpath.write_text('{}')
        with self.assertRaises(ValueError):self.apply()
        self.assertEqual(self.rpath.read_bytes(),self.old[0])
        self.hpath.write_bytes(self.old[1]);(self.runtime/'.stats-source-revision').write_text('b'*40)
        with self.assertRaises(ValueError):self.apply()
        self.assertEqual((self.rpath.read_bytes(),self.hpath.read_bytes()),self.old)
    def test_second_write_failure_rolls_back_only_our_bytes(self):
        original=installer._atomic
        def fail(path,raw,mode=0o600):
            if path==self.hpath:raise OSError('injected hook write failure')
            return original(path,raw,mode)
        with patch.object(installer,'_atomic',side_effect=fail):
            with self.assertRaises(OSError):self.apply()
        self.assertEqual((self.rpath.read_bytes(),self.hpath.read_bytes()),self.old)
    def test_ambiguous_sources_are_not_enrolled_by_default(self):
        self.registry['sources'].append({'id':'duplicate','kind':'codex','node':'ownermac'})
        with self.assertRaises(ValueError):
            installer.plan(self.registry,self.hooks,home=self.home,runtime=self.runtime,python=Path(sys.executable),site='ownermac')

if __name__=='__main__':unittest.main()
