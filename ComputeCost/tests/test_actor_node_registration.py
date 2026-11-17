"""A new inspected Host uses the receipt contract, not a second node roster."""
from copy import deepcopy
import unittest

from pulse_compute_cost.receipts import ReceiptError
from pulse_compute_cost.receipt_sources import actor_receipt, usage_receipt
from pulse_compute_cost.source_registration import actor_candidate_registry

AT = '2026-09-26T11:00:00Z'


class ActorNodeRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.registry = {'schema': 'computecost_source_registry/v1', 'ledger_root': '/unchanged',
                         'sources': [{'id': 'native', 'kind': 'pi', 'path': '/existing/native'},
                                     {'id': 'peer', 'kind': 'receipts', 'node': 'vultr'}]}
        self.surfaces = {'agent_id': 'pulse', 'surfaces': [{
            'surface_id': 'surface_v1_muse_linux', 'workspace': 'pulse',
            'deployment': {'site': 'muse', 'pulse_home': '/fixture', 'kernel_root': '/fixture/kernel'}}]}
        self.usage = usage_receipt('pi', 'fixture-call',
            {'input': 10, 'cacheRead': 0, 'cacheWrite': 0, 'output': 2, 'totalTokens': 12},
            at=AT, observed=AT, model='fixture', provider='fixture')

    def test_new_host_enrolls_without_borrowing_a_production_node_or_wi(self):
        before = deepcopy(self.registry)
        result = actor_candidate_registry(self.registry, self.surfaces, site='muse')
        source = result['sources'][0]
        self.assertEqual(source['node'], 'muse')
        self.assertEqual(source['scl_surface_id'], 'surface_v1_muse_linux')
        self.assertEqual(source['scl_kernel_root'], '/fixture/kernel')
        self.assertEqual(source['path'], '/existing/native')
        self.assertEqual(result['ledger_root'], '/unchanged')
        self.assertEqual(result['sources'][1], before['sources'][1])
        self.assertEqual(self.registry, before)
        actor = actor_receipt(self.usage, node=source['node'], observed=AT)
        self.assertEqual(actor.payload['context']['node'], 'muse')
        self.assertIsNone(actor.payload['wi'])
        self.assertIsNone(actor.payload['executor'])

    def test_registration_and_actor_receipt_reject_the_same_malformed_labels(self):
        for node in ['', 'Muse', '../muse', 'muse\n', 'a' * 65, 42, ['muse']]:
            with self.subTest(node=node):
                with self.assertRaises(ReceiptError):
                    actor_candidate_registry(self.registry, self.surfaces, site=node)
                with self.assertRaises(ReceiptError):
                    actor_receipt(self.usage, node=node, observed=AT)

    def test_unknown_receipt_node_stays_unknown_but_enrollment_requires_a_site(self):
        self.assertIsNone(actor_receipt(self.usage, node=None, observed=AT).payload['context']['node'])
        with self.assertRaises(ReceiptError):
            actor_candidate_registry(self.registry, self.surfaces, site=None)
