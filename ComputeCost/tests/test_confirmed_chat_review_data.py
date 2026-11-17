"""The human clarification is finite data, not a classifier for future chats."""
import json
from pathlib import Path
import unittest
from pulse_compute_cost.receipts import reference

class ConfirmedChatDataTests(unittest.TestCase):
    def test_exact_finite_reviews_and_retained_provenance(self):
        root = Path(__file__).resolve().parents[1]
        evidence = json.loads((root / 'uat/evidence/wi300-confirmed-chats-20260927.json').read_text())
        document = json.loads((root / 'reviewed-activity.json').read_text())
        groups = evidence['groups']
        self.assertEqual([len(g['usage_fact_refs']) for g in groups], [38, 1])
        targets = {r for g in groups for r in g['usage_fact_refs']}
        self.assertEqual(len(targets), 39)
        proof = reference('human_clarification', evidence['confirmation'])
        self.assertEqual(proof, evidence['confirmation_ref'])
        self.assertTrue(evidence['confirmation']['not_a_host_issued_receipt'])
        reviews = [r for r in document['items'] if r['target_fact_ref'] in targets]
        self.assertEqual(len(reviews), 39)
        self.assertEqual({r['target_fact_ref'] for r in reviews}, targets)
        for row in reviews:
            self.assertEqual(row['work_kind'], 'none')
            self.assertEqual((row['agent'], row['node'], row['entrypoint']),
                             ('hestia', 'ownermac', 'codex_app'))
            self.assertEqual(set(row), {'target_fact_ref', 'work_kind', 'agent', 'node', 'entrypoint', 'evidence_refs'})
            self.assertEqual(set(row['evidence_refs']), {proof, row['target_fact_ref']})
        self.assertEqual(evidence['source_ledger_watermark'], 761)
        self.assertFalse(evidence['production_effect_verified'])

if __name__ == '__main__':
    unittest.main()
