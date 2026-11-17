"""Explicit I1 registration into existing ledger, synthetic source trees only."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.source_registration import candidate_registry
from pulse_compute_cost.receipts import ReceiptError

class NativeSourceRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('pi','active','archive','cc'): (self.root/name).mkdir()
        self.registry={'schema':'computecost_source_registry/v1','ledger_root':str(self.root/'existing-ledger'),
                       'sources':[{'id':'host','kind':'relay_host','path':str(self.root/'spool')}],
                       'private_configuration':'retained'}

    def test_preserves_existing_configuration_and_groups_active_archive(self):
        original=deepcopy(self.registry)
        native={'pi':[str(self.root/'pi')],'codex':[str(self.root/'active'),str(self.root/'archive')]}
        candidate=candidate_registry(self.registry,native)
        self.assertEqual(self.registry,original)
        self.assertEqual(candidate['ledger_root'],original['ledger_root'])
        self.assertEqual(candidate['sources'][0],original['sources'][0])
        self.assertEqual(len(candidate['sources']),3)
        self.assertEqual(candidate_registry(candidate,native),candidate)
        self.assertFalse(any(s['kind']=='cc' for s in candidate['sources']))

    def test_never_silently_adds_an_overlapping_second_codex_source(self):
        first=candidate_registry(self.registry,{'codex':[str(self.root/'active')]})
        with self.assertRaises(ReceiptError):
            candidate_registry(first,{'codex':[str(self.root/'active'),str(self.root/'archive')]})

    def test_normalized_existing_codex_scope_is_reconciled_or_overlapping_scope_rejected(self):
        nested = self.root / 'active' / 'nested'; nested.mkdir()
        original = deepcopy(self.registry)
        original['sources'].append({'id':'original-codex','kind':'codex','paths':[str(self.root/'active')]})
        alias = str(self.root / 'active' / '..' / 'active')
        self.assertEqual(candidate_registry(original, {'codex':[alias]}), original)
        with self.assertRaises(ReceiptError):
            candidate_registry(original, {'codex':[str(nested)]})
        child = deepcopy(self.registry)
        child['sources'].append({'id':'child-codex','kind':'codex','paths':[str(nested)]})
        with self.assertRaises(ReceiptError):
            candidate_registry(child, {'codex':[str(self.root/'active')]})

    def test_no_source_discovery_and_unsafe_roots_rejected(self):
        self.assertEqual(candidate_registry(self.registry,{}),self.registry)
        (self.root/'linked').symlink_to(self.root/'pi')
        for path in ('relative',str(self.root/'linked')):
            with self.assertRaises(ReceiptError): candidate_registry(self.registry,{'pi':[path]})
        with self.assertRaises(OSError): candidate_registry(self.registry,{'pi':[str(self.root/'missing')]})
        with self.assertRaises(ReceiptError): candidate_registry(self.registry,{'guessed':[]})

class TeamWorkspaceEnrollmentTests(unittest.TestCase):
    def setUp(self):
        import yaml
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();(self.root/'ssot').mkdir()
        self.path=self.root/'ssot/registry.yaml'
        self.document={'repo_root':str(self.root),'agents':{'hestia':{'workspace':'Oikos/hestia-space'}}}
        self.path.write_text(yaml.safe_dump(self.document));self.path.chmod(0o644)
        self.registry={'schema':'computecost_source_registry/v1','ledger_root':str(self.root/'ledger'),
            'workspace_registry_path':str(self.path),'sources':[{'id':'native','kind':'codex'}]}
        self.surfaces={'agent_id':'pulse','surfaces':[]}

    def candidate(self, registry=None):
        from pulse_compute_cost.source_registration import actor_candidate_registry
        return actor_candidate_registry(registry or self.registry,self.surfaces,site='ownermac')

    def test_non_pulse_workspace_and_subagent_startup_use_same_declared_owner(self):
        from pulse_compute_cost.receipt_sources import registered_workspace_actor
        original=deepcopy(self.registry);candidate=self.candidate();source=candidate['sources'][0]
        actor=registered_workspace_actor(source,str(self.root/'Oikos/hestia-space'),'codex')
        self.assertEqual(actor['executor'],'hestia');self.assertIsNotNone(actor['workspace_ref'])
        self.assertEqual(self.registry,original)
        self.assertEqual(self.candidate(candidate),candidate)
        self.assertIsNone(registered_workspace_actor(source,str(self.root/'unregistered/hestia-space'),'codex')['executor'])

    def test_no_enrolled_registry_does_not_guess_from_bot_or_folder(self):
        registry=deepcopy(self.registry);registry.pop('workspace_registry_path')
        self.assertEqual(self.candidate(registry)['sources'][0]['workspace_actors'],[])

    def test_existing_valid_history_is_preserved_and_ownership_conflict_rejected(self):
        candidate=self.candidate();rule=candidate['sources'][0]['workspace_actors'][0]
        history={**rule,'workspace_root':str(self.root/'old-hestia-space')}
        candidate['sources'][0]['workspace_actors'].append(history)
        self.assertIn(history,self.candidate(candidate)['sources'][0]['workspace_actors'])
        rule['agent']='not-hestia'
        with self.assertRaises(ReceiptError):self.candidate(candidate)

    def test_unsafe_or_ambiguous_team_registry_is_rejected(self):
        import yaml
        for workspace in ('../escape','/absolute'):
            self.document['agents']['hestia']['workspace']=workspace
            self.path.write_text(yaml.safe_dump(self.document))
            with self.assertRaises(ReceiptError):self.candidate()
        self.document['agents']={'a':{'workspace':'same'},'b':{'workspace':'same'}}
        self.path.write_text(yaml.safe_dump(self.document))
        with self.assertRaises(ReceiptError):self.candidate()
        self.path.chmod(0o666)
        with self.assertRaises(ReceiptError):self.candidate()

if __name__=='__main__': unittest.main()
