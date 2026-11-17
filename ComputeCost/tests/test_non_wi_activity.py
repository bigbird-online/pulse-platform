"""Confirmed non-WI work is not missing attribution, nor a made-up WI."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from pulse_compute_cost.receipts import Receipt, ReceiptError, reference, non_wi_attribution
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.receipt_sources import usage_receipt, actor_receipt
from test_collaboration_receipts import CATALOG, AT

class NonWiActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=UsageLedger(Path(self.tmp.name)/"ledger")
        self.row=usage_receipt("codex", "activity-fixture",
            {"input_tokens":100,"output_tokens":2,"cached_input_tokens":0,"cache_write_input_tokens":0},
            at=AT,observed=AT,model=next(iter(CATALOG["data"]))["id"] if isinstance(CATALOG.get("data"),list) else "fixture")
        self.proof=reference("human_confirmation","reviewed exact usage; cron without WI")

    def query(self, watermark=None, groups=("agent","wi","entrypoint","node","runtime","model"), **kw):
        return query_collaboration(self.ledger,CollaborationQuery(group_by=groups,watermark=watermark,**kw),
                                   catalog=CATALOG,catalog_observed_at=AT)

    def classify(self,row=None,kind="cron",**kw):
        return non_wi_attribution(row or self.row,work_kind=kind,observed_at=AT,evidence=(self.proof,),**kw)

    def test_null_wi_remains_unknown_until_reviewed(self):
        self.ledger.ingest_receipts([self.row]);before=self.query(self.ledger.receipt_snapshot()["watermark"])
        self.assertEqual(before["summary"]["usage"]["wi_unassigned_event_count"],1)
        self.assertEqual(before["groups"][0]["key"]["wi"],"unassigned")
        mark=before["watermark"]
        self.ledger.ingest_receipts([self.classify()]);after=self.query()
        self.assertEqual(after["groups"][0]["key"]["wi"],"cron")
        self.assertEqual(after["summary"]["usage"]["wi_unassigned_event_count"],0)
        self.assertEqual(after["summary"]["usage"]["cron_event_count"],1)
        self.assertNotIn("attribution_unknown",after["summary"]["cost"]["reasons"])
        self.assertNotIn("wi_execution_inventory_unknown",after["summary"]["cost"]["reasons"])
        self.assertEqual(after["summary"]["usage"]["wi_inventory_coverage"],"not_applicable")
        self.assertEqual(self.query(mark),before)
        self.assertEqual(after["summary"]["usage"]["known_total_tokens"],before["summary"]["usage"]["known_total_tokens"])
        self.assertIsNotNone(before["summary"]["cost"]["partial_machine_cash_usd"])
        self.assertEqual(after["summary"]["cost"]["partial_machine_cash_usd"],before["summary"]["cost"]["partial_machine_cash_usd"])
        self.assertEqual(self.ledger.ingest_receipts([self.classify()])["inserted"],0)

    def test_confirmed_none_is_separate_from_unknown_and_cron(self):
        def named(name):
            return Receipt.make("usage",name,self.row.payload,occurred_at=AT,observed_at=AT,producer=self.row.producer)
        cron,plain,unknown=named("cron"),named("plain"),named("unknown")
        self.ledger.ingest_receipts([cron,plain,unknown,self.classify(cron),self.classify(plain,"none")])
        result=self.query(groups=("wi",))
        self.assertEqual({g["key"]["wi"]:g["usage"]["event_count"] for g in result["groups"]},
                         {"cron":1,"none":1,"unassigned":1})
        self.assertEqual(result["summary"]["usage"]["wi_unassigned_event_count"],1)
        self.assertEqual(result["summary"]["usage"]["non_wi_event_count"],2)
        self.assertEqual(result["summary"]["usage"]["unassigned_event_count"],1)
        self.assertTrue(all(not g["work_items"] for g in result["groups"]))

    def test_activity_label_does_not_remove_independent_actor_evidence(self):
        actor=actor_receipt(self.row,node="ownermac",executor="duke",entrypoint="automation",observed=AT,evidence=(self.proof,))
        self.ledger.ingest_receipts([self.row,self.classify(),actor])
        row=self.query()["groups"][0]
        self.assertEqual(row["key"]["agent"],"duke")
        self.assertEqual(row["key"]["entrypoint"],"automation")
        self.assertEqual(row["key"]["wi"],"cron")

    def test_labels_cannot_be_used_as_canonical_wi_ids(self):
        for label in ("cron","none"):
            with self.assertRaises(ReceiptError):CollaborationQuery(work_items=(label,))
        self.ledger.ingest_receipts([self.row,self.classify()])
        r=self.query(work_items=("wi_20260901_001",))
        self.assertEqual(r["summary"]["usage"]["event_count"],0)

    def test_evidence_required_and_known_wi_is_never_reclassified(self):
        with self.assertRaises(ReceiptError):non_wi_attribution(self.row,work_kind="cron",observed_at=AT,evidence=())
        linked=Receipt.make("usage","linked",{**self.row.payload,"wi":"wi_20260901_001"},occurred_at=AT,observed_at=AT,producer=self.row.producer)
        with self.assertRaises(ReceiptError):self.classify(linked)
        with self.assertRaises(ReceiptError):self.classify(kind="unassigned")

    def test_conflicting_classifications_are_not_marked_complete(self):
        self.ledger.ingest_receipts([self.row,self.classify(),self.classify(kind="none")])
        result=self.query()
        self.assertEqual(result["summary"]["usage"]["wi_unassigned_event_count"],1)
        self.assertNotEqual(result["groups"][0]["key"]["wi"],"cron")
        self.assertIn("attribution_unknown",result["summary"]["cost"]["reasons"])

    def test_real_wi_and_non_wi_claims_conflict_instead_of_hiding_work(self):
        assigned=Receipt.make("attribution","task",{
            "target_ref":self.row.entity_ref,"wi":"wi_20260901_001","executor":None,
            "responsible":None,"delegator":None,"state":"assigned","policy":"explicit-task", "effective_at":AT},
            occurred_at=AT,observed_at=AT,producer="fixture")
        self.ledger.ingest_receipts([self.row,assigned,self.classify()])
        r=self.query()
        self.assertEqual(r["summary"]["usage"]["wi_unassigned_event_count"],1)
        self.assertEqual(r["summary"]["usage"]["event_count"],1)
        self.assertEqual(r["summary"]["usage"]["cron_event_count"],0)

    def test_reviewed_source_does_not_guess_other_records_or_require_sealed_cost(self):
        from pulse_compute_cost.activity_classification import reviewed_activity_receipts
        document={"schema":"computecost_reviewed_activity/v1","items":[{
            "target_fact_ref":self.row.fact_ref,"work_kind":"cron","agent":"duke","node":"ownermac",
            "entrypoint":"automation","evidence_refs":[self.proof]}]}
        self.assertEqual(reviewed_activity_receipts([],document,AT),[])
        receipts=reviewed_activity_receipts([self.row],document,AT)
        self.ledger.ingest_receipts([self.row,*receipts])
        row=self.query()["groups"][0]
        self.assertEqual(row["key"]["wi"],"cron")
        from pulse_compute_cost.stats_dashboard import _view
        view=_view(self.query(),"week","activity")
        self.assertEqual(view["groups"][0]["key"]["wi"],"cron")
        self.assertEqual(view["summary"]["usage"]["cron_event_count"],1)

    def test_later_quantity_observation_does_not_turn_classification_into_a_gate(self):
        from pulse_compute_cost.activity_classification import reviewed_activity_receipts
        document={"schema":"computecost_reviewed_activity/v1","items":[{
            "target_fact_ref":self.row.fact_ref,"work_kind":"cron","agent":"duke","node":"ownermac",
            "entrypoint":"automation","evidence_refs":[self.proof]}]}
        changed={**self.row.payload,"final":not self.row.payload["final"]}
        current=Receipt.make("usage",None,changed,entity_ref=self.row.entity_ref,supersedes=self.row.fact_ref,
                             occurred_at=AT,observed_at=AT,producer=self.row.producer)
        receipts=reviewed_activity_receipts([self.row,current],document,AT)
        self.ledger.ingest_receipts([self.row,current,*receipts])
        self.assertEqual(self.query()["groups"][0]["key"]["wi"],"cron")

    def test_explicit_revision_changes_classification_without_changing_usage(self):
        first=self.classify();self.ledger.ingest_receipts([self.row,first])
        prior=self.query(self.ledger.receipt_snapshot()["watermark"]);self.ledger.ingest_receipts([self.classify(kind="none",supersedes=first.fact_ref)])
        self.assertEqual(self.query()["groups"][0]["key"]["wi"],"none")
        self.assertEqual(self.query(prior["watermark"]),prior)

if __name__=="__main__":unittest.main()


class ReviewedPurposeTests(unittest.TestCase):
    setUp = NonWiActivityTests.setUp
    query = NonWiActivityTests.query
    def test_exact_reviewed_wi_is_separate_from_actor_and_not_a_future_rule(self):
        from pulse_compute_cost.activity_classification import reviewed_activity_receipts
        document={"schema":"computecost_reviewed_activity/v1","items":[{
            "target_fact_ref":self.row.fact_ref,"work_item_id":"wi_20260901_101","agent":"reviewer",
            "node":"test-node","entrypoint":"automation","evidence_refs":[self.proof]}]}
        self.ledger.ingest_receipts([self.row]);before=self.ledger.receipt_snapshot()
        rows=reviewed_activity_receipts([self.row],document,AT)
        self.ledger.ingest_receipts(rows)
        result=self.query();self.assertEqual(result['groups'][0]['key']['wi'],'wi_20260901_101')
        self.assertEqual(result['groups'][0]['key']['agent'],'reviewer')
        self.assertEqual(self.ledger.receipt_snapshot(watermark=before['watermark'])['sha256'],before['sha256'])
        other=Receipt.make('usage','new-call',self.row.payload,occurred_at=AT,observed_at=AT,producer=self.row.producer)
        self.assertEqual(reviewed_activity_receipts([other],document,AT),[])
        self.assertEqual(self.ledger.ingest_receipts(rows)['inserted'],0)

    def test_actor_only_review_does_not_invent_or_override_work_item(self):
        from pulse_compute_cost.activity_classification import reviewed_activity_receipts
        document={"schema":"computecost_reviewed_activity/v1","items":[{
            "target_fact_ref":self.row.fact_ref,"scope":"actor","agent":"reviewer","node":"test-node",
            "entrypoint":"automation","evidence_refs":[self.proof]}]}
        rows=reviewed_activity_receipts([self.row],document,AT)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0].payload['scope'],'actor')
        self.ledger.ingest_receipts([self.row,*rows])
        self.assertEqual(self.query()['groups'][0]['key']['wi'],'unassigned')
        document['items'][0]['evidence_refs']=[]
        with self.assertRaises(ReceiptError):reviewed_activity_receipts([self.row],document,AT)
