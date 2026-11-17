"""Registered native Outcome files must reach the real query and publisher."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timezone, timedelta
import unittest
import yaml
from pulse_compute_cost.canonical_outcomes import parse_recorded_outcome, display_text, FEATURE, CHECKS
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import ReceiptError, Receipt
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.stats_dashboard import build_dashboard

WI = "wi_20260917_123"
AT = "2026-09-17T08:00:00Z"
NOW = datetime(2026,9,17,9,tzinfo=timezone.utc)


def document():
    return dict(schema="verify-work-outcome/v2", claim_id=WI, claim="恢复本周统计的真实产出展示",
        acceptance_fingerprint="sha256:"+"a"*64, change_set=["commit:"+"b"*40],
        change_type="runtime", risk="medium", status="accepted", producer="pulse", verifier="duke",
        verification_level="independent", verified_at=AT, acceptance_source=["spec:outcome"],
        target=dict(environment="isolated",revision="b"*40,consumer="Stats"),
        checks={name:dict(result="pass",note="verified",evidence=["evidence:fixture"]) for name in CHECKS},
        unresolved_high_risk=False,residual_risks=["历史月份未验收"])


def raw(data=None):
    return ("---\n"+yaml.safe_dump(data or document(),allow_unicode=True)+
        "---\n## 声明边界\n已批准范围。\n## 验收矩阵\n见结构化检查。\n## 结论\n产出、成本与来源可展开查看。\n").encode()


class CanonicalOutcomeTests(unittest.TestCase):
    def test_recorded_outcome_keeps_native_identity_time_and_prose(self):
        row=parse_recorded_outcome(raw(),WI,NOW.isoformat())
        self.assertEqual(row.payload["status"],"accepted")
        self.assertEqual(row.occurred_at,AT)
        self.assertEqual(row.payload["outcome_context"]["title"],document()["claim"])
        self.assertIn("可展开",row.payload["outcome_context"]["summary"])
        self.assertEqual(row.producer,"canonical_outcome.v1")
        self.assertEqual(Receipt.from_dict(row.to_dict()),row)

    def test_wrong_identity_future_and_self_approval_are_rejected(self):
        for changes in (dict(claim_id="wi_20260917_999"),dict(verified_at="2026-09-18T00:00:00Z"),
                        dict(verifier="pulse"),dict(unresolved_high_risk=True),dict(unresolved_high_risk="false"),dict(checks={})):
            with self.subTest(changes=changes),self.assertRaises(ReceiptError):
                parse_recorded_outcome(raw({**document(),**changes}),WI,NOW.isoformat())

    def test_canonical_directory_cannot_escape_through_symlinked_evidence_parent(self):
        from pulse_compute_cost.canonical_outcomes import read_canonical_outcomes
        with TemporaryDirectory() as tmp:
            root=Path(tmp)/"registered";root.mkdir()
            other=Path(tmp)/"other"; (other/"outcomes").mkdir(parents=True)
            (other/"outcomes"/(WI+".md")).write_bytes(raw())
            (root/"evidence").symlink_to(other,target_is_directory=True)
            with self.assertRaises(ReceiptError):
                read_canonical_outcomes(root,NOW.isoformat(),[],None)

    def test_incomplete_and_rejected_verdicts_are_not_promoted(self):
        for status in ("blocked","partial","rejected"):
            row=parse_recorded_outcome(raw({**document(),"status":status}),WI,NOW.isoformat())
            self.assertEqual(row.payload["status"],status)
            self.assertIsNone(row.payload["accepted_at"])

    def test_prose_is_bounded_and_private_material_is_not_exported(self):
        self.assertIsNone(display_text("api_key=not-for-publication",240))
        self.assertEqual(display_text("成果写入 /Users/fixture/private.md。",240),"成果写入 [私有路径]")
        self.assertEqual(len(display_text("a"*300,240)),240)

    def test_native_source_through_collector_query_and_publisher(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"Stats/exporter"))
        from pulse_stats.cost_dashboard import _validate
        with TemporaryDirectory() as tmp:
            root=Path(tmp); harness=root/"harness"; folder=harness/"evidence/outcomes";folder.mkdir(parents=True)
            source=folder/(WI+".md");source.write_bytes(raw())
            nested=folder/WI; nested.mkdir();(nested/"copied.md").write_bytes(raw())
            ledger=UsageLedger(root/"ledger",clock=lambda:NOW)
            registry=dict(schema="computecost_source_registry/v1",ledger_root=str(ledger.root),
                sources=[dict(id="canonical",kind="harness_root",path=str(harness))])
            collect_registered_sources(registry,ledger)
            self.assertFalse(any(r.kind=="delivery" for r in ledger.receipt_snapshot()["facts"]))
            registry["required_receipt_features"]=[FEATURE]
            collect_registered_sources(registry,ledger);before=ledger.receipt_snapshot()
            q=CollaborationQuery(scope="activity",from_date="2026-09-17",to_date="2026-09-18")
            report=query_collaboration(ledger,q)
            self.assertEqual(report["summary"]["outcomes"]["count"],1)
            self.assertEqual(report["summary"]["usage"]["event_count"],0)
            doc=build_dashboard(ledger,subscriptions={"schema_version":"pulse_stats_procurement_facts/v1","subscriptions":[]},
                source_revision="c"*40,now=NOW+timedelta(seconds=1),identity_dimensions=True,activity_table=True)
            _validate(doc)
            view=next(v for v in doc["views"] if v["period"]=="today" and v["dimension"]=="activity")
            self.assertEqual(view["outcomes"]["count"],1)
            self.assertEqual(view["outcomes"]["items"][0]["title"],document()["claim"])
            self.assertIsNone(view["outcome_only_groups"][0]["cost"]["partial_machine_cash_usd"])
            collect_registered_sources(registry,ledger)
            self.assertEqual(query_collaboration(ledger,q)["summary"]["outcomes"]["count"],1)
            self.assertEqual(ledger.receipt_snapshot(watermark=before["watermark"])["sha256"],before["sha256"])
            source.unlink();source.symlink_to(nested/"copied.md")
            failed=collect_registered_sources(registry,ledger)
            self.assertEqual(failed["sources"][0]["source_detail"]["files_invalid"],1)


    def test_canonical_outcome_replaces_only_unknown_review_placeholder(self):
        from pulse_compute_cost.receipts import current_receipts, reference
        with TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/"harness/evidence/outcomes";folder.mkdir(parents=True)
            (folder/(WI+".md")).write_bytes(raw())
            ledger=UsageLedger(root/"ledger",clock=lambda:NOW)
            native=parse_recorded_outcome(raw(),WI,NOW.isoformat())
            placeholder=Receipt.make("delivery",WI,{k:v for k,v in
                {**native.payload,"status":"unknown","accepted_at":None,
                 "candidate_ref":reference("candidate","old-unrelated-review")}.items() if k!="outcome_context"},
                occurred_at="2026-09-12T01:00:00Z",observed_at=NOW.isoformat(),producer="harness_review.v1")
            ledger.ingest_receipts([placeholder]);old=ledger.receipt_snapshot()
            registry=dict(schema="computecost_source_registry/v1",ledger_root=str(ledger.root),
                required_receipt_features=[FEATURE],sources=[dict(id="canonical",kind="harness_root",path=str(root/"harness"))])
            collect_registered_sources(registry,ledger)
            active,conflicts=current_receipts(ledger.receipt_snapshot()["facts"])
            self.assertFalse(conflicts)
            delivered=next(r for r in active if r.kind=="delivery")
            self.assertEqual(delivered.payload["candidate_ref"],native.payload["candidate_ref"])
            self.assertEqual(delivered.supersedes,placeholder.fact_ref)
            self.assertEqual(ledger.receipt_snapshot(watermark=old["watermark"])["sha256"],old["sha256"])
            collect_registered_sources(registry,ledger)
            self.assertFalse(current_receipts(ledger.receipt_snapshot()["facts"])[1])
            # Conflicting known outcomes must not become last-write-wins.
            changed={**document(),"status":"rejected"}
            (folder/(WI+".md")).write_bytes(raw(changed))
            collect_registered_sources(registry,ledger)
            self.assertIn(native.entity_ref,current_receipts(ledger.receipt_snapshot()["facts"])[1])

    def test_format_only_source_change_is_evidence_not_conflicting_outcome(self):
        from pulse_compute_cost.receipts import current_receipts
        with TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/"harness/evidence/outcomes";folder.mkdir(parents=True)
            source=folder/(WI+".md");source.write_bytes(raw())
            ledger=UsageLedger(root/"ledger",clock=lambda:NOW)
            registry=dict(schema="computecost_source_registry/v1",ledger_root=str(ledger.root),
                required_receipt_features=[FEATURE],sources=[dict(id="canonical",kind="harness_root",path=str(root/"harness"))])
            collect_registered_sources(registry,ledger);old=ledger.receipt_snapshot()
            source.write_bytes(raw().replace(b"---\n",b"---\n# formatting-only source comment\n",1))
            collect_registered_sources(registry,ledger)
            active,conflicts=current_receipts(ledger.receipt_snapshot()["facts"])
            self.assertFalse(conflicts)
            self.assertEqual(sum(r.kind=="delivery" for r in active),1)
            self.assertEqual(ledger.receipt_snapshot(watermark=old["watermark"])["sha256"],old["sha256"])

    def test_old_outcome_conflict_does_not_poison_another_period(self):
        from pulse_compute_cost.outcome_projection import outcomes_for_period
        one=parse_recorded_outcome(raw({**document(),"verified_at":"2026-09-12T00:00:00Z"}),WI,NOW.isoformat())
        two=Receipt.make("delivery",None,{**one.payload,"status":"rejected","accepted_at":None},entity_ref=one.entity_ref,
            occurred_at=one.occurred_at,observed_at=NOW.isoformat(),producer=one.producer)
        window=(datetime(2026,9,14,tzinfo=timezone.utc),datetime(2026,9,18,tzinfo=timezone.utc))
        result=outcomes_for_period([],[],window=window,coverage=[],wis={WI},conflicts=[one,two])
        self.assertEqual(result["conflict_refs"],[])
        self.assertEqual(result["status"],"unavailable")


if __name__=="__main__":unittest.main()
