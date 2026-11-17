"""Joint views reuse only frozen evidence preparation, not filters or prices."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch
import unittest
from test_collaboration_receipts import complete, AT, CATALOG, WI
from pulse_compute_cost.receipts import digest
from pulse_compute_cost.collaboration_query import CollaborationQuery, CollaborationQuerySession, query_collaboration, _prepare_collaboration

class FreshLedger:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def receipt_snapshot(self, **kwargs):
        return self.snapshot

class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.facts=complete()
        self.snap={"facts":self.facts,"watermark":1,"recorded_at":AT,
                   "sha256":digest([r.to_dict() for r in self.facts])}
        self.q=CollaborationQuery(scope="activity",from_date="2026-09-01",to_date="2026-09-15",watermark=1)

    def read(self,ledger,q=None,catalog=None):
        read = ledger.query if isinstance(ledger, CollaborationQuerySession) else lambda q, **kw: query_collaboration(ledger, q, **kw)
        return read(q or self.q,catalog=catalog or CATALOG,catalog_observed_at=AT)

    def test_21_views_identical_with_one_preparation(self):
        dims=[("agent",),("wi",),("entrypoint",),("node",),("runtime",),("model",),
              ("agent","wi","entrypoint","node","runtime","model")]
        queries=[replace(self.q,from_date=start,group_by=d) for start in
                 ("2026-09-14","2026-09-07","2026-09-01") for d in dims]
        baseline=[self.read(FreshLedger(self.snap),q) for q in queries]
        shared=CollaborationQuerySession(self.snap)
        with patch("pulse_compute_cost.collaboration_query._prepare_collaboration",wraps=_prepare_collaboration) as prepare:
            actual=[self.read(shared,q) for q in queries]
            self.assertEqual(prepare.call_count,1)
        self.assertEqual(actual,baseline)

    def test_returned_report_mutation_and_filters_do_not_leak(self):
        ledger=CollaborationQuerySession(self.snap)
        answer=self.read(ledger)
        answer["reconciliation"].clear()
        answer["summary"]["cost"]["reasons"].append("not_evidence")
        for q in (self.q,replace(self.q,work_items=(WI,),group_by=("model",)),
                  replace(self.q,agent="not-observed")):
            self.assertEqual(self.read(ledger,q),self.read(FreshLedger(self.snap),q))

    def test_prices_are_never_cached_with_evidence(self):
        ledger=CollaborationQuerySession(self.snap);self.read(ledger)
        other=deepcopy(CATALOG);other["data"][0]["pricing"]["prompt"]="2"
        self.assertEqual(self.read(ledger,catalog=other),self.read(FreshLedger(self.snap),catalog=other))

    def test_payload_eviction_does_not_change_report_or_cross_query_state(self):
        from pulse_compute_cost.collaboration_query import _PayloadReads
        queries=[self.q,replace(self.q,group_by=('agent','wi','model')),
                 replace(self.q,work_items=(WI,)),replace(self.q,agent='not-observed')]
        expected=[self.read(FreshLedger(self.snap),q) for q in queries]
        with patch.object(_PayloadReads,'MAX_ROWS',1),patch.object(_PayloadReads,'MAX_ENCODED_BYTES',512):
            ledger=CollaborationQuerySession(self.snap)
            self.assertEqual([self.read(ledger,q) for q in queries],expected)

    def test_watermark_boundary_and_membership_are_frozen(self):
        ledger=CollaborationQuerySession(self.snap);before=self.read(ledger)
        self.facts.clear()
        self.assertEqual(self.read(ledger),before)
        with self.assertRaises(ValueError):self.read(ledger,replace(self.q,watermark=2))

    def test_exported_prices_and_seed_are_owned_and_bounded(self):
        session=CollaborationQuerySession(self.snap)
        self.read(session)
        seed=session.export_prices()
        self.assertTrue(seed)
        other=CollaborationQuerySession(self.snap, prices=seed)
        seed.clear()
        exported=other.export_prices()
        next(iter(exported.values()))[0]="999999"
        self.assertEqual(self.read(other),self.read(FreshLedger(self.snap)))
        self.assertEqual(len(other.export_prices(limit=1)),1)
        self.assertEqual(other.export_prices(limit=0),{})

    def test_live_ledger_never_gets_query_cache_attributes(self):
        ledger=FreshLedger(self.snap)
        self.read(ledger)
        self.assertEqual(set(vars(ledger)),{"snapshot"})
