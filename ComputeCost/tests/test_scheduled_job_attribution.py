"""A scheduler run's usage is named by its job, or the WI its job configures."""
from pathlib import Path
import json
import tempfile
import unittest

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import read_host, usage_receipt
from pulse_compute_cost.receipts import Receipt, ReceiptError, reference
from test_mcp_cc_session_wi import CATALOG

AT = '2026-01-01T02:00:00Z'


def codex_usage(name, minute, thread='thread-1', **payload):
    row = usage_receipt('codex', name, {'input_tokens': 100, 'output_tokens': 2, 'cached_input_tokens': 0,
                                         'cache_write_input_tokens': 0},
                        at=f'2026-01-01T01:{minute:02d}:00Z', observed=AT, model='openai/test',
                        native_scope_ref=reference('native_session', ['codex', thread]))
    if payload:
        row = Receipt.make('usage', None, {**row.payload, **payload}, occurred_at=row.occurred_at,
                           observed_at=AT, producer=row.producer, entity_ref=row.entity_ref)
    return row


def scheduled_stream(path, wi=None, thread='thread-1'):
    """One Relay Host record of the native session a scheduler run started."""
    scope = reference('native_session', ['codex', thread])
    path.write_text(json.dumps({
        'schema': 'pulse_collaboration_host_event/v1', 'kind': 'scheduled_session', 'at': AT,
        'event_ref': reference('host_event', ['scheduled_session', scope]),
        'run_ref': reference('scheduler_run', ['run-1']), 'native_scope_ref': scope,
        'started_at': '2026-01-01T00:59:00Z', 'job_id': 'hestia-triage', 'job_name': 'Oikos 30 分钟异常分流',
        'wi': wi}) + '\n')
    return read_host(path, AT)


class ScheduledJobTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.ledger = UsageLedger(self.root/'ledger')

    def scheduled(self, wi=None):
        return scheduled_stream(self.root/'host.jsonl', wi)

    def query(self):
        result = query_collaboration(self.ledger, CollaborationQuery(scope='activity', from_date='2026-01-01',
            to_date='2026-01-02', timezone='UTC', group_by=('wi',)), catalog=CATALOG)
        return {g['key']['wi']: g for g in result['groups']}

    def test_scheduled_job_without_wi_is_named_by_its_job(self):
        rows = [codex_usage('one', 1)]
        self.ledger.ingest_receipts([*rows, *self.scheduled()])
        group = self.query()['cron:hestia-triage']
        self.assertEqual(group['attribution'], {'sources': {'scheduled_job': {'event_count': 1, 'priced_machine_cash_usd': '102'}},
                                                'job': {'id': 'hestia-triage', 'name': 'Oikos 30 分钟异常分流'}})
        self.assertNotIn('attribution_unknown', group['cost']['reasons'])
        self.assertNotIn('wi_execution_inventory_unknown', group['cost']['reasons'])

    def test_scheduled_session_without_a_proven_channel_is_automation(self):
        from pulse_compute_cost.receipt_sources import actor_receipt
        before = codex_usage('before', 1, thread='thread-2')  # another session, not scheduled
        routed = codex_usage('routed', 3)
        rows = [codex_usage('plain', 2), routed, before,
                actor_receipt(routed, node='mac', executor='hestia', entrypoint='telegram', observed=AT,
                              evidence=(reference('x', 1),))]
        self.ledger.ingest_receipts([*rows, *self.scheduled()])
        result = query_collaboration(self.ledger, CollaborationQuery(scope='activity', from_date='2026-01-01',
            to_date='2026-01-02', timezone='UTC', group_by=('entrypoint',)), catalog=CATALOG)
        counts = {g['key']['entrypoint']: g['usage']['event_count'] for g in result['groups']}
        self.assertEqual(counts, {'automation': 1, 'telegram': 1, 'unassigned': 1})

    def test_scheduled_job_identity_is_validated(self):
        payload = self.scheduled()[0].payload
        for job in ({'id': '/etc', 'name': 'x'}, {'id': 'a', 'name': ' x'}, {'id': 'a', 'name': 'x\ny'}, {'id': 'a'}):
            with self.assertRaises(ReceiptError):
                Receipt.make('attribution', None, {**payload, 'job': job}, occurred_at=AT, observed_at=AT,
                             producer='relay_host.v1', evidence=(reference('x', 1),))
        with self.assertRaises(ReceiptError):  # only the scheduler policy names a job
            Receipt.make('attribution', None, {**payload, 'policy': 'other.v1'}, occurred_at=AT,
                         observed_at=AT, producer='relay_host.v1', evidence=(reference('x', 1),))



if __name__ == '__main__':
    unittest.main()
