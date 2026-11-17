"""Source identity and WI denominator regressions (synthetic, no provider calls)."""
from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, canonical, reference
from pulse_compute_cost.receipt_sources import collect_registered_sources, usage_receipt
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from test_collaboration_receipts import AT, WI, OTHER, CATALOG, complete, coverage, fact
from test_scl_measurement import attribution, write_json, WI as SCL_WI
import test_scl_measurement as scl_fixture


class SourceJoinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = UsageLedger(self.root / 'ledger')

    def tearDown(self):
        self.tmp.cleanup()

    def observations(self, wi=WI):
        native = usage_receipt('cc', ['native-session','message-1'],
            dict(input_tokens=20, output_tokens=10, cache_read_input_tokens=80, cache_creation_input_tokens=0),
            at=AT, observed=AT, model='openai/test', mapping='cc_message.v1')
        host = Receipt.make('usage', None, {**native.payload,
            'wi':wi, 'executor':'pulse', 'execution_ref':reference('execution','e'),
            'coverage_ref':reference('coverage','e'),
            'interval_start':'2026-09-13T00:59:59Z', 'interval_end':'2026-09-13T01:00:02Z'},
            entity_ref=native.entity_ref, occurred_at='2026-09-13T01:00:02Z',
            observed_at='2026-09-13T01:00:02Z', producer='cc_message.v1')
        return native, host

    def query(self, rows=(), **kwargs):
        self.ledger.ingest_receipts(rows)
        return query_collaboration(self.ledger, CollaborationQuery(**kwargs), catalog=CATALOG, catalog_observed_at=AT)

    def test_host_native_same_request_reconciles_not_conflicts(self):
        native, host = self.observations()
        report = self.query([host,native], work_items=(WI,))
        self.assertEqual(report['summary']['usage']['known_total_tokens'],110)
        self.assertEqual(report['summary']['usage']['event_count'],1)
        self.assertEqual(report['revision_conflicts'],[])
        self.assertEqual(report['reconciliation']['observation_groups'][native.entity_ref],sorted([native.fact_ref,host.fact_ref]))
        self.assertEqual(len(self.ledger.receipt_snapshot()['facts']),2)

    def test_sdk_host_refines_native_cc_format_for_same_request_only(self):
        native, host = self.observations()
        sdk = Receipt.make('usage', None, {**host.payload, 'runtime':'cc_sdk'},
            entity_ref=host.entity_ref, occurred_at=host.occurred_at,
            observed_at=host.observed_at, producer=host.producer)
        out = self.query([native, sdk], work_items=(WI,), runtime='cc_sdk')
        self.assertEqual(out['summary']['usage']['known_total_tokens'], 110)
        self.assertEqual(out['summary']['usage']['event_count'], 1)
        self.assertEqual(out['revision_conflicts'], [])
        # Two actual, disagreeing execution claims are not a format alias.
        self.ledger.ingest_receipts([host])
        conflict = self.query(work_items=(WI,))
        self.assertIn(native.entity_ref, conflict['revision_conflicts'])
        self.assertIsNone(conflict['summary']['cost']['machine_cash_usd'])

    def test_join_does_not_change_old_watermark_or_ingest_order(self):
        native, host = self.observations()
        self.ledger.ingest_receipts([native])
        old = self.ledger.receipt_snapshot()['watermark']
        self.ledger.ingest_receipts([host])
        current = self.query(work_items=(WI,))
        self.assertEqual(current['summary']['usage']['known_total_tokens'],110)
        prior = self.query(watermark=old, group_by=('wi',))
        self.assertEqual(prior['groups'][0]['key']['wi'],'unassigned')
        other = UsageLedger(self.root/'other')
        other.ingest_receipts([host,native])
        reverse = query_collaboration(other,CollaborationQuery(work_items=(WI,)))
        self.assertEqual(reverse['summary']['usage'],current['summary']['usage'])

    def test_real_final_quantity_conflict_stays_conflict(self):
        native,host=self.observations()
        changed=Receipt.make('usage',None,{**native.payload,'tokens':{**native.payload['tokens'],'output':11}},
            entity_ref=native.entity_ref,occurred_at=AT,observed_at=AT,producer='cc_message.v1')
        out=self.query([host,changed],work_items=(WI,))
        self.assertIn(native.entity_ref,out['revision_conflicts'])
        self.assertIn('join_conflict',out['summary']['cost']['reasons'])
        self.assertIsNone(out['summary']['cost']['machine_cash_usd'])

    def test_conflicting_wi_keeps_known_quantity_ambiguous(self):
        native,host=self.observations()
        other=Receipt.make('usage',None,{**host.payload,'wi':OTHER}, entity_ref=host.entity_ref,
            occurred_at=host.occurred_at,observed_at=host.observed_at,producer=host.producer)
        out=self.query([native,host,other],group_by=('wi',))
        self.assertEqual(out['summary']['usage']['known_total_tokens'],110)
        self.assertEqual(out['groups'][0]['key']['wi'],'ambiguous')
        self.assertIn(native.entity_ref,out['attribution_conflicts'])
        for wi in (WI,OTHER):
            self.assertIn('attribution_unknown',self.query(work_items=(wi,))['summary']['cost']['reasons'])

    def test_identity_join_never_guesses_timestamp_outside_host_interval(self):
        native,host=self.observations()
        native=Receipt.make('usage',None,native.payload,entity_ref=native.entity_ref,
            occurred_at='2026-09-14T01:00:00Z',observed_at='2026-09-14T01:00:00Z',producer=native.producer)
        out=self.query([native,host],scope='activity',from_date='2026-09-13',to_date='2026-09-14',timezone='UTC')
        self.assertEqual(out['time_unallocated']['tokens']['output']['known'],10)
        self.assertEqual(out['summary']['usage']['event_count'],0)

    def test_streamed_native_records_refine_a_host_interval_across_midnight(self):
        native,host=self.observations()
        host=Receipt.make('usage',None,{**host.payload,'interval_start':'2026-09-13T23:00:00Z',
            'interval_end':'2026-09-14T00:30:00Z'},entity_ref=host.entity_ref,occurred_at='2026-09-14T00:30:00Z',
            observed_at='2026-09-14T00:30:00Z',producer=host.producer)
        # CC writes one streamed reply as several records milliseconds apart.
        blocks=[Receipt.make('usage',None,native.payload,entity_ref=native.entity_ref,occurred_at=at,
            observed_at=at,producer=native.producer) for at in ('2026-09-14T00:10:00Z','2026-09-14T00:31:00Z')]
        out=self.query([*blocks,host],scope='activity',from_date='2026-09-14',to_date='2026-09-15',timezone='UTC')
        self.assertEqual(out['summary']['usage']['event_count'],1)
        self.assertEqual(out['time_unallocated']['fact_refs'],[])
        # Later blocks of the reply may follow the Host's end; its start may not precede the Host's start.
        early=Receipt.make('usage',None,native.payload,entity_ref=native.entity_ref,occurred_at='2026-09-13T22:00:00Z',
            observed_at='2026-09-13T22:00:00Z',producer=native.producer)
        self.ledger=UsageLedger(self.root/'other')
        out=self.query([early,blocks[0],host],scope='activity',from_date='2026-09-14',to_date='2026-09-15',timezone='UTC')
        self.assertEqual(out['summary']['usage']['event_count'],0)

    def test_complete_execution_source_cannot_hide_another_partial_source(self):
        partial = fact('coverage','offline-host',dict(domain='execution',wi=WI,
            source_ref=reference('source','offline-host'),status='partial',expected=2,observed=1,watermark='1',reason='missing'))
        out=self.query(complete()+[partial],work_items=(WI,))
        self.assertEqual(out['summary']['usage']['wi_inventory_coverage'],'unknown')
        self.assertIn('wi_execution_inventory_unknown',out['summary']['cost']['reasons'])

    def test_global_unavailable_source_is_not_filtered_out_of_wi_query(self):
        gap=fact('coverage','global-source',dict(domain='usage',wi=None,source_ref=reference('source','global-source'),
            status='unreachable',expected=None,observed=None,watermark=None,reason='offline'))
        out=self.query(complete()+[gap],work_items=(WI,))['summary']
        self.assertIsNone(out['cost']['machine_cash_usd'])
        self.assertIn('source_coverage_unknown',out['cost']['reasons'])

    def test_unreachable_harness_is_not_only_a_quality_gap(self):
        from test_collaboration_receipts import PERIOD
        self.query(complete())
        registry = {'schema':'computecost_source_registry/v1','sources':[
            {'id':'missing-harness','kind':'harness_root','path':str(self.root/'absent')}]}
        collect_registered_sources(registry, self.ledger)
        out = self.query(work_items=(WI,), **PERIOD)['summary']
        self.assertIsNone(out['cost']['machine_cash_usd'])
        self.assertIsNone(out['roi']['ratio'])
        self.assertIn('wi_execution_inventory_unknown', out['cost']['reasons'])
        self.assertIn('value_inventory_unknown', out['roi']['reasons'])
        rows = self.ledger.receipt_snapshot()['facts']
        domains = {r.payload['domain'] for r in rows if r.kind == 'coverage'
                   and r.payload['source_ref'] == reference('source','missing-harness')}
        self.assertEqual(domains, {'execution','human','quality','value'})
        self.assertEqual(collect_registered_sources(registry, self.ledger)['inserted'], 0)

    def test_value_source_gap_blocks_roi_but_not_machine_cost(self):
        gap=fact('coverage','value-source',dict(domain='value',wi=WI,source_ref=reference('source','value-source'),
            status='partial',expected=2,observed=1,watermark='1',reason='missing'))
        from test_collaboration_receipts import PERIOD
        out=self.query(complete()+[gap],work_items=(WI,),**PERIOD)['summary']
        self.assertEqual(out['cost']['machine_cash_usd'],'100')
        self.assertIsNone(out['roi']['ratio'])
        self.assertIn('value_inventory_unknown',out['roi']['reasons'])

    def test_explicit_later_attribution_can_resolve_metadata_not_quantity(self):
        native,host=self.observations()
        other=Receipt.make('usage',None,{**host.payload,'wi':OTHER},entity_ref=host.entity_ref,
            occurred_at=host.occurred_at,observed_at=host.observed_at,producer=host.producer)
        self.query([native,host,other])
        old=self.ledger.receipt_snapshot()['watermark']
        resolution=fact('attribution','resolution',dict(target_ref=native.entity_ref,wi=WI,executor='pulse',responsible=None,
            delegator=None,state='assigned',policy='explicit_review.v1',effective_at='2026-09-13T00:00:00Z'))
        out=self.query([resolution],work_items=(WI,))
        self.assertEqual(out['summary']['usage']['known_total_tokens'],110)
        self.assertEqual(out['attribution_conflicts'],[])
        self.assertTrue(self.query(watermark=old)['attribution_conflicts'])

    def registry(self):
        return {'schema':'computecost_source_registry/v1','sources':[
            {'id':'harness','kind':'harness_root','path':str(self.root/'harness')}]}

    def test_canonical_attribution_without_usage_appears_as_expected_epoch(self):
        a=attribution()
        path=self.root/'harness/evidence/compute-cost/attributions'/SCL_WI/(sha256(a['operation_ref'].encode()).hexdigest()+'.json')
        write_json(path,a)
        collect_registered_sources(self.registry(),self.ledger)
        out=self.query(work_items=(SCL_WI,))['summary']
        self.assertEqual(out['usage']['expected_measurement_epoch_count'],1)
        self.assertEqual(len(out['usage']['missing_measurement_epoch_refs']),1)
        self.assertIn('expected_measurement_missing',out['cost']['reasons'])
        self.assertEqual(out['usage']['event_count'],0)
        self.assertEqual(collect_registered_sources(self.registry(),self.ledger)['inserted'],0)

    def test_late_seal_updates_expectation_not_old_watermark_or_token_sum(self):
        root=self.root/'harness';root.mkdir()
        scl_fixture.SclMeasurementTests().populate(root)
        pi=root/'evidence/compute-cost/observations'/SCL_WI/'scl_pi_usage_measurement.v1'
        file=next(pi.glob('*.json'));payload=file.read_bytes();file.unlink()
        collect_registered_sources(self.registry(),self.ledger)
        old=self.ledger.receipt_snapshot()['watermark']
        file.write_bytes(payload);file.chmod(0o600)
        collect_registered_sources(self.registry(),self.ledger)
        before=self.query(work_items=(SCL_WI,),watermark=old)['summary']['usage']
        after=self.query(work_items=(SCL_WI,))['summary']['usage']
        self.assertEqual(len(before['missing_measurement_epoch_refs']),1)
        self.assertEqual(after['missing_measurement_epoch_refs'],[])
        # A seal proves a source prefix, not that the collector already joined its quantity.
        self.assertEqual(len(after['unjoined_measurement_epoch_refs']),1)
        self.assertEqual(after['event_count'],0)
        self.assertEqual(after['wi_inventory_coverage'],'unknown')
        self.assertEqual(collect_registered_sources(self.registry(),self.ledger)['inserted'],0)
        self.assertEqual(self.query(work_items=(SCL_WI,))['revision_conflicts'],[])
        file.unlink()
        collect_registered_sources(self.registry(),self.ledger)
        missing=self.query(work_items=(SCL_WI,))['summary']['usage']
        self.assertEqual(len(missing['missing_measurement_epoch_refs']),1)
        self.assertEqual(missing['event_count'],0)



if __name__=='__main__': unittest.main()
