"""A known Agent's tenant comes from the team registry; session proof keeps precedence."""
from datetime import datetime, timezone
from pathlib import Path
import json, tempfile, unittest

from pulse_compute_cost.agent_tenants import load_agent_tenants
from pulse_compute_cost.collaboration_query import CollaborationQuery, CollaborationQuerySession
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import actor_receipt, usage_receipt
from pulse_compute_cost.receipts import ReceiptError, reference
from pulse_compute_cost.tenant_context import FEATURE, SCHEMA, collect_tenant_context

AT = '2026-09-15T01:00:00Z'; END = '2026-09-15T01:01:00Z'
CATALOG = {'data': [{'id': 'openai/test', 'pricing': {'prompt': '1', 'completion': '1', 'input_cache_read': '0.1', 'input_cache_write': '2'}}]}
DIMS = ('tenant', 'agent', 'wi', 'entrypoint', 'node', 'runtime', 'model')
REGISTRY = """repo_root: {root}
owners:
  team-a: {{}}
  team-b: {{}}
agents:
  shared:
    workspace: shared-space/
    tenant: team-a
  other:
    workspace: other-space/
    tenant: team-b
"""


class AgentTenantTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / 'ssot').mkdir()
        self.team = self.root / 'ssot/registry.yaml'
        self.team.write_text(REGISTRY.format(root=self.root)); self.team.chmod(0o644)
        self.sources = {'schema': 'computecost_source_registry/v1', 'sources': [],
                        'workspace_registry_path': str(self.team)}
        self.ledger = UsageLedger(self.root / 'ledger', clock=lambda: datetime(2026, 9, 15, 4, tzinfo=timezone.utc))

    def usage(self, identity):
        return usage_receipt('codex', identity, {'input_tokens': 120, 'cached_input_tokens': 20, 'cache_write_input_tokens': 0, 'output_tokens': 10},
            at=END, observed=END, model='openai/test', mapping='codex_cli.v1', granularity='request', interval=(AT, END),
            native_scope_ref=reference('native_session', identity))

    def actor(self, target, executor='shared'):
        return actor_receipt(target, node='ownermac', executor=executor, entrypoint='codex_app', observed=END,
                             evidence=(reference('workspace_registry', executor),))

    def report(self, agent_tenants):
        snapshot = self.ledger.receipt_snapshot()
        query = CollaborationQuery(from_date='2026-09-15', to_date='2026-09-16', scope='activity', group_by=DIMS,
                                   watermark=snapshot['watermark'])
        return CollaborationQuerySession(snapshot, agent_tenants=agent_tenants).query(
            query, catalog=CATALOG, catalog_observed_at=END)

    def by_agent(self, report):
        return {g['key']['agent']: (g['key']['tenant'], g.get('tenant'), g['cost']['priced_machine_cash_usd'])
                for g in report['groups']}

    def test_known_agent_gets_its_registered_tenant_and_amounts_are_unchanged(self):
        known, unknown = self.usage('known'), self.usage('unknown')
        self.ledger.ingest_receipts([known, self.actor(known), unknown])
        before, after = self.report(None), self.report(load_agent_tenants(self.sources))
        self.assertEqual(after['summary']['cost'], before['summary']['cost'])
        groups = self.by_agent(after)
        tenant, identity, amount = groups['shared']
        self.assertEqual(tenant, 'team-a')
        self.assertEqual((identity['name'], identity['sources'], identity['registry_conflicts']), ('team-a', ['registry'], []))
        self.assertEqual(groups['unassigned'][0], 'unassigned')  # No Agent, no tenant.
        self.assertEqual({a: v[2] for a, v in groups.items()}, {a: v[2] for a, v in self.by_agent(before).items()})

    def test_ambiguous_agent_gets_no_registered_tenant(self):
        row = self.usage('contested')
        self.ledger.ingest_receipts([row, self.actor(row), self.actor(row, 'other')])
        groups = self.by_agent(self.report(load_agent_tenants(self.sources)))
        self.assertEqual(groups['ambiguous'][0], 'unassigned')

    def membership(self, row, tenant):
        path = self.root / 'membership.json'
        path.write_text(json.dumps({'schema': SCHEMA, 'bindings': [{
            'tenant_id': tenant, 'tenant_name': tenant, 'agent': 'shared', 'node': 'ownermac', 'runtime': 'codex',
            'native_scope_ref': row.payload['native_scope_ref'], 'effective_from': AT, 'effective_to': None}]}))
        path.chmod(0o600)
        registry = {**self.sources, 'required_receipt_features': [FEATURE], 'tenant_registry_path': str(path)}
        return collect_tenant_context([row, self.actor(row)], registry, END)

    def test_session_tenant_takes_precedence_and_a_differing_registry_is_a_conflict(self):
        for session, conflicts in (('team-a', []), ('team-b', ['team-a'])):
            with self.subTest(session=session):
                self.setUp()
                row = self.usage('declared')
                self.ledger.ingest_receipts([row, self.actor(row), *self.membership(row, session)])
                tenant, identity, _ = self.by_agent(self.report(load_agent_tenants(self.sources)))['shared']
                self.assertEqual(tenant, session)
                self.assertEqual((identity['sources'], identity['registry_conflicts']), (['session'], conflicts))

    def test_registry_tenant_must_be_a_declared_owner(self):
        self.team.write_text(REGISTRY.format(root=self.root).replace('tenant: team-b', 'tenant: stranger'))
        with self.assertRaises(ReceiptError):
            load_agent_tenants(self.sources)
        self.team.write_text(REGISTRY.format(root=self.root)); self.team.chmod(0o666)
        with self.assertRaises(ReceiptError):
            load_agent_tenants(self.sources)
        self.assertEqual(load_agent_tenants({'sources': []}), {})

    def test_reference_follows_only_agent_tenant_bindings(self):
        first = load_agent_tenants(self.sources)
        self.team.write_text(REGISTRY.format(root=self.root) + "unrelated: changed\n")
        self.assertEqual(load_agent_tenants(self.sources), first)
        self.team.write_text(REGISTRY.format(root=self.root).replace('tenant: team-b', 'tenant: team-a'))
        second = load_agent_tenants(self.sources)
        self.assertEqual(second['shared'], first['shared'])
        self.assertNotEqual(second['other']['binding_ref'], first['other']['binding_ref'])
