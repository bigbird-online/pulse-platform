"""Public CLI regression for conflicting observed producers; synthetic data only."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import unittest

from pulse_compute_cost.cli import main
import test_wi_report as fixtures
from test_wi342_uat import write_jsonl

WI = 'wi_20260820_568'


class CloseoutCliTests(unittest.TestCase):
    def test_public_cli_never_prices_conflicting_rollout_as_ledger_total(self):
        fixture = fixtures.WiReportTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.seal_epoch(
            fixture.observer(work_item=WI, model='gpt-fixture'), thread='private-sealed',
            baseline=(0, 0, 0, 0, 0), delta=(300000, 280000, 2000, 1000, 302000), minute=1,
        )
        root = Path(fixture.temp.name)
        harness = root / '.harness'
        (harness / 'work-items').mkdir(parents=True)
        (harness / 'work-items' / f'{WI}.yaml').write_text(
            f"work_item_id: {WI}\nstatus: completed\nowner: pulse\n"
            "created_at: '2026-08-20T00:00:00Z'\nupdated_at: '2026-08-26T00:00:00Z'\n")
        codex = root / 'codex'
        session = 'private-rollout'
        write_jsonl(codex / 'session_index.jsonl', [{'id': session, 'thread_name': WI}])
        write_jsonl(codex / 'sessions' / f'rollout-{session}.jsonl', [
            {'type': 'session_meta', 'payload': {'id': session}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
              'content': [{'type': 'input_text', 'text': WI}]}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'private-turn'}},
            {'type': 'turn_context', 'payload': {'turn_id': 'private-turn',
              'model': 'gpt-fixture', 'effort': 'high'}},
            {'type': 'event_msg', 'timestamp': '2026-08-25T00:01:01Z', 'payload': {
                'type': 'token_count', 'info': {'total_token_usage': {
                    'input_tokens': 100000, 'cached_input_tokens': 90000,
                    'cache_write_input_tokens': 0, 'output_tokens': 500,
                    'reasoning_output_tokens': 0}}}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'private-turn'}},
        ])
        catalog = root / 'catalog.json'
        catalog.write_text(json.dumps({'providers': {'openai-chatgpt': {'protocol': 'openai'}},
                                      'models': [{'id': 'gpt-fixture', 'provider': 'openai-chatgpt'}]}))
        prices = root / 'prices.json'
        prices.write_text(json.dumps({'data': [{'id': 'openai/gpt-fixture',
            'canonical_slug': 'openai/gpt-fixture-standard', 'pricing': {
                'prompt': '0.000002', 'input_cache_read': '0.0000002',
                'input_cache_write': '0.0000025', 'completion': '0.00001'}}]}))
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(['wi-report', WI, '--harness-root', str(harness),
                           '--codex-home', str(codex), '--model-catalog', str(catalog),
                           '--openrouter-models-snapshot', str(prices),
                           '--app-server-ledger', str(fixture.path), '--machine', 'fixture'])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['usage']['tokens']['total_tokens'], 302000)
        self.assertEqual(report['usage']['usage_event_cohort']['tokens']['total_resource_tokens'], 100500)
        self.assertEqual(report['usage']['status'], 'partial')
        self.assertEqual(report['usage']['conservation'], 'partial')
        self.assertIn('producer_join_conflict', report['warnings'])
        self.assertEqual(report['cost']['machine_cash_usd'], 'unavailable')
        self.assertEqual(report['cost']['partial_machine_cash_usd'], 'unavailable')
        self.assertEqual(report['cost']['cash_coverage'], 'join_conflict')
        self.assertEqual(report['cost']['openrouter_cash_valuation']['total_usd'], '0.043')
        # The source-local calculation is retained only as diagnostic evidence.
        for private in [session, 'private-turn', 'private-sealed', str(root)]:
            self.assertNotIn(private, output.getvalue())
