"""Membership must follow exact usage versions and proven actor/timing evidence."""
from copy import deepcopy
from pathlib import Path
import unittest

import test_tenant_context as fixture
from pulse_compute_cost.receipts import Receipt, ReceiptError, reference
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import actor_receipt, _revision
from pulse_compute_cost.tenant_context import collect_tenant_context


class TenantVersionGuardTests(unittest.TestCase):
    setUp = fixture.TenantContextTests.setUp
    binding = fixture.TenantContextTests.binding
    registry = fixture.TenantContextTests.registry
    query = fixture.TenantContextTests.query
    make_usage = fixture.TenantContextTests.make_usage
    actor = fixture.TenantContextTests.actor

    def named(self):
        registry = self.registry([self.binding(effective_to='2026-09-15T01:02:00Z')])
        memberships = collect_tenant_context(self.facts, registry, fixture.END)
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0].payload['context']['tenant']['usage_fact_ref'], self.usage.fact_ref)
        self.ledger.ingest_receipts(self.facts + memberships)
        mark = self.ledger.receipt_snapshot()['watermark']
        before = self.query(watermark=mark)
        self.assertEqual(before['groups'][0]['key']['tenant'], 'team-a')
        return registry, memberships, mark, before

    def revision(self, **changes):
        payload = self.usage.payload
        payload.update(changes)
        return Receipt.make('usage', None, payload, entity_ref=self.usage.entity_ref,
            occurred_at=payload['interval_end'], observed_at='2026-09-15T01:04:00Z',
            producer=self.usage.producer, evidence=self.usage.evidence, supersedes=self.usage.fact_ref)

    def test_cumulative_final_extension_does_not_reuse_old_name_or_change_old_asof(self):
        payload = self.usage.payload
        payload.update(final=False, granularity='turn_aggregate', mapping='codex_rollout_cumulative.v1')
        payload['reported_total_tokens'] = sum(payload['tokens'].values())
        self.usage = Receipt.make('usage', None, payload, entity_ref=self.usage.entity_ref,
            occurred_at=fixture.END, observed_at=fixture.END, producer='codex_rollout.v1')
        self.facts = [self.usage, self.actor(self.usage)]
        registry, _, mark, before = self.named()
        payload = self.usage.payload
        payload.update(final=True, interval_end='2026-09-15T01:03:00Z')
        payload['tokens']['output'] += 10
        payload['native_tokens']['output_tokens'] += 10
        payload['reported_total_tokens'] += 10
        native = Receipt.make('usage', None, payload, entity_ref=self.usage.entity_ref,
            occurred_at=payload['interval_end'], observed_at=payload['interval_end'], producer=self.usage.producer)
        revisions = [self.usage]
        _revision(revisions, native)
        newer = revisions[-1]
        self.assertEqual(newer.supersedes, self.usage.fact_ref)
        self.ledger.ingest_receipts([newer])
        self.assertEqual(collect_tenant_context(self.ledger.receipt_snapshot()['facts'], registry, fixture.END), [])
        self.assertEqual(self.query()['groups'][0]['key']['tenant'], 'unassigned')
        self.assertEqual(self.query()['summary']['usage']['known_total_tokens'], 140)
        self.assertEqual(self.query(watermark=mark)['report_sha256'], before['report_sha256'])

    def test_native_scope_or_runtime_revision_requires_its_own_new_binding(self):
        for index, changes in enumerate(({'native_scope_ref':reference('native_session','other')}, {'runtime':'pi'})):
            with self.subTest(changes=changes):
                self.ledger = UsageLedger(self.root / ('changed-' + str(index)))
                registry, _, mark, before = self.named()
                self.ledger.ingest_receipts([self.revision(**changes)])
                self.assertEqual(collect_tenant_context(self.ledger.receipt_snapshot()['facts'], registry, fixture.END), [])
                self.assertEqual(self.query()['groups'][0]['key']['tenant'], 'unassigned')
                self.assertEqual(self.query(watermark=mark)['report_sha256'], before['report_sha256'])

    def test_in_window_new_version_can_be_reconfirmed_append_only_and_idempotently(self):
        registry, previous, mark, before = self.named()
        newer = self.revision(interval_end='2026-09-15T01:01:30Z')
        self.ledger.ingest_receipts([newer])
        self.assertEqual(self.query()['groups'][0]['key']['tenant'], 'unassigned')
        current = collect_tenant_context(self.ledger.receipt_snapshot()['facts'], registry, fixture.END)
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0].payload['context']['tenant']['usage_fact_ref'], newer.fact_ref)
        self.assertNotEqual(current[0].fact_ref, previous[0].fact_ref)
        self.ledger.ingest_receipts(current)
        self.assertEqual(self.query()['groups'][0]['key']['tenant'], 'team-a')
        self.assertEqual(self.query()['summary']['cost'], before['summary']['cost'])
        self.assertEqual(self.ledger.ingest_receipts(current)['inserted'], 0)
        self.assertEqual(self.query(watermark=mark)['report_sha256'], before['report_sha256'])

    def test_executor_or_node_conflict_retains_proof_but_cannot_name_tenant(self):
        for index, change in enumerate(({'executor':'other','node':'ownermac'}, {'executor':'shared','node':'vultr'})):
            with self.subTest(change=change):
                self.ledger = UsageLedger(self.root / ('conflict-' + str(index)))
                registry, memberships, mark, before = self.named()
                opposite = actor_receipt(self.usage, **change, observed=fixture.END,
                    evidence=(reference('actor','opposite-' + str(index)),))
                self.ledger.ingest_receipts([opposite])
                after = self.query()
                tenant = after['groups'][0]['tenant']
                self.assertEqual(after['groups'][0]['key']['tenant'], 'ambiguous')
                self.assertIsNone(tenant['name'])
                self.assertEqual(tenant['binding_refs'], [memberships[0].payload['context']['tenant']['binding_ref']])
                self.assertEqual(collect_tenant_context(self.ledger.receipt_snapshot()['facts'], registry, fixture.END), [])
                self.assertEqual(after['summary']['usage']['known_total_tokens'], before['summary']['usage']['known_total_tokens'])
                expected_cost = deepcopy(before['summary']['cost'])
                expected_cost['reasons'] = sorted(set(expected_cost['reasons']) | {'actor_attribution_conflict'})
                self.assertEqual(after['summary']['cost'], expected_cost)
                self.assertEqual(self.query(watermark=mark)['report_sha256'], before['report_sha256'])

    def test_entrypoint_disagreement_does_not_revoke_proven_tenant_actor(self):
        _, _, _, _ = self.named()
        opposite = actor_receipt(self.usage, node='ownermac', executor='shared', entrypoint='telegram',
            observed=fixture.END, evidence=(reference('actor','opposite-entry'),))
        self.ledger.ingest_receipts([opposite])
        after = self.query()
        self.assertEqual(after['groups'][0]['key']['entrypoint'], 'ambiguous')
        self.assertEqual(after['groups'][0]['key']['tenant'], 'team-a')

    def metered(self, points):
        payload = self.usage.payload
        payload.update(granularity='turn_aggregate', mapping='codex_rollout_cumulative.v1',
            metering={'basis':'codex_token_count_observed.v1','baseline_ref':reference('baseline','fixture'), 'points':points})
        return Receipt.make('usage', None, payload, entity_ref=self.usage.entity_ref,
            occurred_at=fixture.END, observed_at=fixture.END, producer='codex_rollout.v1')

    def test_metering_first_is_inclusive_last_is_exclusive_for_every_point(self):
        registry = self.registry([self.binding(effective_to=fixture.END)])
        for at, expected in ((fixture.AT, 1), (fixture.END, 0)):
            with self.subTest(at=at):
                usage = self.metered([{'at':at,'tokens':self.usage.payload['tokens'],
                    'evidence_ref':reference('metering',at)}])
                rows = collect_tenant_context([usage,self.actor(usage)],registry,fixture.END)
                self.assertEqual(len(rows), expected)
        half = {key:value // 2 for key,value in self.usage.payload['tokens'].items()}
        usage = self.metered([{'at':at,'tokens':half,'evidence_ref':reference('metering',at)}
            for at in (fixture.AT,fixture.END)])
        self.assertEqual(collect_tenant_context([usage,self.actor(usage)],registry,fixture.END), [])

    def test_coarse_exclusive_endpoint_is_unknown_without_token_notification_proof(self):
        registry = self.registry([self.binding(effective_to=fixture.END)])
        self.assertEqual(collect_tenant_context(self.facts,registry,fixture.END), [])
        widened = self.registry([self.binding(effective_to='2026-09-15T01:01:01Z')])
        self.assertEqual(len(collect_tenant_context(self.facts,widened,fixture.END)), 1)

    def test_receipt_requires_closed_opaque_usage_version_proof(self):
        registry = self.registry([self.binding()])
        membership = collect_tenant_context(self.facts,registry,fixture.END)[0]
        for value in (None, 'raw-native-session'):
            with self.subTest(value=value):
                raw = deepcopy(membership.to_dict())
                tenant = raw['payload']['context']['tenant']
                if value is None:
                    tenant.pop('usage_fact_ref')
                else:
                    tenant['usage_fact_ref'] = value
                with self.assertRaises(ReceiptError):
                    Receipt.from_dict(raw)


if __name__ == '__main__':
    unittest.main()
