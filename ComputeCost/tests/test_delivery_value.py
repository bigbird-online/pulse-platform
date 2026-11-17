from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.app_server_usage import AppServerUsageLedger
from pulse_compute_cost.cli import main as cli_main
from pulse_compute_cost.delivery_value import (
    DeliveryValueError,
    build_business_value_receipt,
    review_candidate_fingerprint,
    validate_business_value_receipt,
    validate_harness_review_snapshot,
)
from pulse_compute_cost.wi_report import WiReportError, build_wi_report


WI = "wi_20260822_730"
ARTIFACT_SET = "sha256:" + "a" * 64
OUTCOME = "outcome_v1_" + "b" * 64


def review_snapshot(*, decision: str = "approved") -> dict[str, object]:
    heads = [
        {
            "repo": "bigbird-online/CollaborateWithAI",
            "revision": "c" * 40,
        },
        {"repo": "bigbird-online/PULSE", "revision": "d" * 40},
    ]
    acceptance = "sha256:" + "e" * 64
    candidate = review_candidate_fingerprint(
        work_item_id=WI,
        acceptance_fingerprint=acceptance,
        artifact_set_sha256=ARTIFACT_SET,
        repo_heads=heads,
    )
    return {
        "schema_version": "work_item_review/v1",
        "work_item_id": WI,
        "reviewer": "human",
        "decision": decision,
        "acceptance_fingerprint": acceptance,
        "artifact_set_sha256": ARTIFACT_SET,
        "repo_heads": heads,
        "candidate_fingerprint": candidate,
        "evidence_refs": [
            ".harness/evidence/independent-verification/wi730/review.log",
            ".harness/evidence/outcomes/wi_20260822_730.md",
        ],
        "reviewed_at": "2026-08-25T03:00:00Z",
    }


def business_value_receipt(*, qualitative: bool = False) -> dict[str, object]:
    measurement: dict[str, object]
    if qualitative:
        measurement = {
            "kind": "qualitative",
            "metric_name": "consumer_adoption",
            "unit": "qualitative",
            "baseline": None,
            "current": None,
            "delta": None,
            "value_direction": "qualitative",
            "statement": "The registered consumer adopted the delivered read-only projection.",
        }
    else:
        measurement = {
            "kind": "time_saved",
            "metric_name": "operator_seconds_per_report",
            "unit": "seconds",
            "baseline": "900",
            "current": "240",
            "delta": "-660",
            "value_direction": "lower_is_value",
            "statement": "The consumer reproduced the report with less operator time.",
        }
    return build_business_value_receipt(
        work_item_id=WI,
        outcome_ref=OUTCOME,
        artifact_set_sha256=ARTIFACT_SET,
        producer="weekly_review",
        consumer="weekly_review",
        verifier="independent_uat",
        measurement=measurement,
        evidence=[
            {
                "ref": ".harness/evidence/compute-cost/wi730-business-value.json",
                "sha256": "sha256:" + "f" * 64,
            }
        ],
        observed_at="2026-08-25T03:05:00Z",
    )


class DeliveryValueTests(unittest.TestCase):
    def test_exact_review_recomputes_candidate_identity(self) -> None:
        value = validate_harness_review_snapshot(
            review_snapshot(), expected_work_item_id=WI
        )
        self.assertEqual(value["artifact_set_sha256"], ARTIFACT_SET)
        self.assertRegex(value["snapshot_sha256"], r"^[0-9a-f]{64}$")

    def test_native_logical_repo_names_and_iso_time_preserve_candidate(self) -> None:
        value = review_snapshot()
        value["repo_heads"] = [
            {"repo": name, "revision": str(index) * 40}
            for index, name in enumerate(("CollaborateWithAI", "Oikos", "PULSE"), 1)
        ]
        value["candidate_fingerprint"] = review_candidate_fingerprint(
            work_item_id=WI,
            acceptance_fingerprint=value["acceptance_fingerprint"],
            artifact_set_sha256=value["artifact_set_sha256"],
            repo_heads=value["repo_heads"],
        )
        for timestamp in ("2026-08-25T03:00:00+00:00", "2026-08-25T11:00:00+08:00"):
            with self.subTest(timestamp=timestamp):
                value["reviewed_at"] = timestamp
                result = validate_harness_review_snapshot(value, expected_work_item_id=WI)
                self.assertEqual(result["repo_heads"], value["repo_heads"])
                self.assertEqual(result["candidate_fingerprint"], value["candidate_fingerprint"])
                self.assertEqual(result["reviewed_at"], "2026-08-25T03:00:00Z")
        # Do not rewrite logical names into guessed org/repo identities.
        value["repo_heads"][0]["repo"] = "bigbird-online/CollaborateWithAI"
        value["repo_heads"].sort(key=lambda head: head["repo"])
        with self.assertRaisesRegex(DeliveryValueError, "fingerprint"):
            validate_harness_review_snapshot(value, expected_work_item_id=WI)

    def test_native_harness_review_import_has_no_synthetic_acceptance(self) -> None:
        from pulse_compute_cost.receipt_sources import read_harness_root
        value = review_snapshot()
        value["repo_heads"] = [{"repo": "PULSE", "revision": "d" * 40}]
        value["reviewed_at"] = "2026-08-25T03:00:00+00:00"
        value["candidate_fingerprint"] = review_candidate_fingerprint(
            work_item_id=WI,
            acceptance_fingerprint=value["acceptance_fingerprint"],
            artifact_set_sha256=value["artifact_set_sha256"],
            repo_heads=value["repo_heads"],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reviews").mkdir()
            source = root / "reviews" / f"{WI}.json"
            raw = json.dumps(value)
            source.write_text(raw)
            rows = []
            files = read_harness_root(root, "2026-08-25T04:00:00Z", rows)
            self.assertIn(source, files)
            self.assertEqual(source.read_text(), raw)
        deliveries = [row for row in rows if row.kind == "delivery"]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].payload["status"], "unknown")
        self.assertIsNone(deliveries[0].payload["accepted_at"])

    def test_review_repository_paths_and_naive_time_still_fail_closed(self) -> None:
        for repo in ("/PULSE", "~/PULSE", "../PULSE", "org/../PULSE"):
            with self.subTest(repo=repo):
                value = review_snapshot()
                value["repo_heads"][0]["repo"] = repo
                with self.assertRaisesRegex(DeliveryValueError, "repository identity"):
                    validate_harness_review_snapshot(value, expected_work_item_id=WI)
        value = review_snapshot()
        value["reviewed_at"] = "2026-08-25T03:00:00"
        with self.assertRaisesRegex(DeliveryValueError, "timezone-aware"):
            validate_harness_review_snapshot(value, expected_work_item_id=WI)
        value = review_snapshot()
        value["repo_heads"] = [value["repo_heads"][0]] * 2
        with self.assertRaisesRegex(DeliveryValueError, "unique"):
            validate_harness_review_snapshot(value, expected_work_item_id=WI)

    def test_review_tamper_and_work_item_mismatch_fail_closed(self) -> None:
        value = review_snapshot()
        value["repo_heads"][1]["revision"] = "1" * 40  # type: ignore[index]
        with self.assertRaisesRegex(DeliveryValueError, "fingerprint"):
            validate_harness_review_snapshot(value, expected_work_item_id=WI)
        with self.assertRaisesRegex(DeliveryValueError, "another work item"):
            validate_harness_review_snapshot(
                review_snapshot(), expected_work_item_id="wi_20260820_272"
            )

    def test_business_value_is_consumer_verified_and_digest_bound(self) -> None:
        value = business_value_receipt()
        validated = validate_business_value_receipt(
            value,
            expected_work_item_id=WI,
            expected_artifact_set_sha256=ARTIFACT_SET,
        )
        self.assertEqual(validated["measurement"]["delta"], "-660")
        self.assertEqual(validated["producer_role"], "downstream_consumer")

        tampered = deepcopy(value)
        tampered["measurement"]["current"] = "241"  # type: ignore[index]
        with self.assertRaisesRegex(DeliveryValueError, "delta|digest"):
            validate_business_value_receipt(
                tampered,
                expected_work_item_id=WI,
                expected_artifact_set_sha256=ARTIFACT_SET,
            )

    def test_qualitative_value_preserves_non_numeric_boundary(self) -> None:
        value = business_value_receipt(qualitative=True)
        self.assertIsNone(value["measurement"]["delta"])
        self.assertEqual(value["measurement"]["unit"], "qualitative")

    def test_value_requires_independent_verifier_and_reviewed_artifact_set(self) -> None:
        value = business_value_receipt()
        value["verifier"] = "weekly_review"
        with self.assertRaisesRegex(DeliveryValueError, "independent verifier"):
            validate_business_value_receipt(
                value,
                expected_work_item_id=WI,
                expected_artifact_set_sha256=ARTIFACT_SET,
            )
        with self.assertRaisesRegex(DeliveryValueError, "reviewed artifacts"):
            validate_business_value_receipt(
                business_value_receipt(),
                expected_work_item_id=WI,
                expected_artifact_set_sha256="sha256:" + "0" * 64,
            )

    def test_wi_report_joins_exact_artifacts_and_verified_business_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = build_wi_report(
                AppServerUsageLedger(Path(directory) / "usage.jsonl"),
                WI,
                review_snapshot=review_snapshot(),
                business_value_receipt=business_value_receipt(),
            )
        self.assertEqual(report["outcome"]["artifacts"]["status"], "exact_approved_set")
        self.assertEqual(
            report["outcome"]["artifacts"]["artifact_set_sha256"], ARTIFACT_SET
        )
        self.assertEqual(
            report["outcome"]["business_value"]["status"], "consumer_verified"
        )
        self.assertEqual(report["coverage"]["artifacts"], "exact_approved_set")
        self.assertEqual(report["coverage"]["business_value"], "consumer_verified")

    def test_cli_consumes_non_stale_harness_review_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "usage.jsonl"
            ledger.write_text("", encoding="utf-8")
            review_path = root / "review.json"
            review_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "candidate": {"candidate_fingerprint": "unused-by-consumer"},
                        "review": review_snapshot(),
                        "review_state": "approved",
                        "error": None,
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                rc = cli_main(
                    [
                        WI,
                        "--app-server-ledger",
                        str(ledger),
                        "--review-snapshot",
                        str(review_path),
                    ]
                )
        self.assertEqual(rc, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["outcome"]["artifacts"]["status"], "exact_approved_set")

    def test_business_value_without_approved_review_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = AppServerUsageLedger(Path(directory) / "usage.jsonl")
            with self.assertRaisesRegex(WiReportError, "approved artifact review"):
                build_wi_report(
                    ledger,
                    WI,
                    business_value_receipt=business_value_receipt(),
                )
            with self.assertRaisesRegex(WiReportError, "approved artifact review"):
                build_wi_report(
                    ledger,
                    WI,
                    review_snapshot=review_snapshot(decision="changes_requested"),
                    business_value_receipt=business_value_receipt(),
                )


if __name__ == "__main__":
    unittest.main()
