"""A renamed Agent workspace is reported until its sources adopt the new path."""
from hashlib import sha256
import json, os, tempfile, unittest
from pathlib import Path
from pulse_compute_cost.receipt_sources import registered_workspace_actor
from pulse_compute_cost.source_registration import actor_candidate_registry, workspace_actor_drift

SITE = 'oracle-node-1'


class WorkspaceActorDriftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(os.path.realpath(self.tmp.name)) / 'Coevolution'
        (self.repo / 'ssot').mkdir(parents=True)
        self.team = self.repo / 'ssot/registry.yaml'
        self.old, self.new = self.repo / 'PARA/守夜-space', self.repo / 'PARA/vigil-space'
        self.old.mkdir(parents=True)
        self.declare(self.old)
        base = {'schema': 'computecost_source_registry/v1', 'ledger_root': str(self.repo / 'ledger'),
                'workspace_registry_path': str(self.team),
                'sources': [{'id': 'oracle.pi', 'kind': 'pi', 'path': str(self.repo / 'sessions')}]}
        self.registry = self.adopt(base)

    def declare(self, workspace):
        self.team.write_text(json.dumps({'repo_root': str(self.repo), 'agents': {
            'vigil': {'workspace': str(workspace.relative_to(self.repo))}}}, ensure_ascii=False))
        self.team.chmod(0o644)

    def adopt(self, registry):
        return actor_candidate_registry(registry, {'agent_id': 'pulse', 'surfaces': []}, site=SITE)

    def agent(self, cwd):
        return registered_workspace_actor(self.registry['sources'][0], str(cwd), 'pi')['executor']

    def test_adopted_registry_has_no_drift(self):
        self.assertEqual(self.registry['workspace_registry_sha256'], sha256(self.team.read_bytes()).hexdigest())
        self.assertEqual(workspace_actor_drift(self.registry), [])
        self.assertEqual(self.agent(self.old), 'vigil')

    def test_renamed_workspace_is_drift_until_adopted_again(self):
        self.old.rename(self.new)
        self.assertEqual(workspace_actor_drift(self.registry), [
            {'reason': 'workspace_root_missing', 'agent': 'vigil', 'workspace_root': str(self.old)}])
        self.declare(self.new)
        self.assertEqual(workspace_actor_drift(self.registry), [
            {'reason': 'workspace_registry_changed'},
            {'reason': 'workspace_rule_unadopted', 'source': 'oracle.pi', 'agent': 'vigil',
             'workspace_root': str(self.new)}])
        self.assertIsNone(self.agent(self.new))
        self.registry = self.adopt(self.registry)
        self.assertEqual(workspace_actor_drift(self.registry), [])
        self.assertEqual(self.agent(self.new), 'vigil')

    def test_adoption_without_a_team_registry_keeps_no_stale_digest(self):
        self.registry.pop('workspace_registry_path')
        self.assertNotIn('workspace_registry_sha256', self.adopt(self.registry))

    def test_sources_adopted_before_the_digest_was_recorded_are_reported(self):
        self.registry.pop('workspace_registry_sha256')
        self.assertEqual(workspace_actor_drift(self.registry), [{'reason': 'workspace_registry_unadopted'}])


if __name__ == '__main__':
    unittest.main()
