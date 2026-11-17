from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

from pulse_stats.producer import build_snapshot
from pulse_stats.global_authority import PUBLICATION_ENDPOINTS


NOW = datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)
HARNESS_SHA = "1" * 40
HARNESS_REPOSITORY = "bigbird-online/PULSE"


def _authority_revision(source_sha: str = HARNESS_SHA) -> str:
    payload = f"{HARNESS_REPOSITORY}@{source_sha}".encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _capability() -> dict[str, object]:
    return {
        "id": "s2.ledger_invariants",
        "implementation_status": "active_validated",
        "health": "ok",
        "aggregation_role": "runtime",
        "scope": "global Harness authority",
        "reason": "fixture capability",
        "freshness": {
            "state": "fresh",
            "observed_at": NOW.isoformat(),
            "max_age_seconds": 900,
        },
        "evidence": ["fixture:harness"],
    }


def _metric() -> dict[str, object]:
    return {
        "id": "harness.wi.in_flight",
        "capability_id": "s2.ledger_invariants",
        "status": "available",
        "value": [{"work_item_id": "wi_20260824_615"}],
        "unit": "list",
        "window": "current",
        "sample_count": 1,
        "quality": "validated",
        "scope": "global non-terminal work items",
        "observed_at": NOW.isoformat(),
        "reason": None,
    }


def _complete_authority_evidence() -> dict[str, object]:
    revision = _authority_revision()
    return {
        "schema_version": "pulse_stats_global_authority_evidence/v1",
        "authority_revision": revision,
        "required_endpoints": list(PUBLICATION_ENDPOINTS),
        "materializations": [
            {
                "endpoint_id": endpoint,
                "authority_revision": revision,
                "digest": "sha256:" + "b" * 64,
            } for endpoint in PUBLICATION_ENDPOINTS
        ],
        "observed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
    }


def _snapshot(evidence: dict[str, object] | None) -> dict[str, object]:
    return build_snapshot(
        generated_at=NOW,
        expires_at=NOW + timedelta(minutes=45),
        capabilities=[_capability()],
        required_capability_ids=["s2.ledger_invariants"],
        incidents=[],
        metrics=[_metric()],
        provenance={
            "producer": {"name": "pulse-stats-exporter", "version": "test"},
            "source_revisions": {"pulse": "03207a2", "harness": HARNESS_SHA},
            "sources": [],
        },
        headline="fixture",
        global_authority_evidence=evidence,
    )


class GlobalAuthorityContractTests(unittest.TestCase):
    def test_direct_snapshot_cannot_choose_or_replace_publication_endpoints(self) -> None:
        for mutation in ("one", "unknown", "muse", "reverse", "duplicate", "extra", "malformed"):
            with self.subTest(mutation=mutation):
                evidence = _complete_authority_evidence()
                if mutation == "one":
                    evidence["required_endpoints"] = evidence["required_endpoints"][:1]
                    evidence["materializations"] = evidence["materializations"][:1]
                elif mutation in {"unknown", "muse"}:
                    endpoint = "surface_v1_" + mutation + "_linux"
                    evidence["required_endpoints"][-1] = endpoint
                    evidence["materializations"][-1]["endpoint_id"] = endpoint
                elif mutation == "reverse":
                    evidence["materializations"].reverse()
                elif mutation == "duplicate":
                    evidence["materializations"][-1] = dict(evidence["materializations"][0])
                elif mutation == "extra":
                    evidence["materializations"].append(dict(evidence["materializations"][0]))
                else:
                    evidence["materializations"].append(None)
                snapshot = _snapshot(evidence)
                self.assertNotEqual(snapshot["global_authority"]["status"], "validated")
                self.assertNotEqual(snapshot["metrics"][0]["quality"], "validated")

    def test_complete_evidence_is_the_only_path_to_validated_global_data(self) -> None:
        snapshot = _snapshot(_complete_authority_evidence())

        self.assertEqual(snapshot["global_authority"]["status"], "validated")
        self.assertEqual(snapshot["global_authority"]["scope"], "global")
        metric = snapshot["metrics"][0]
        self.assertEqual(metric["quality"], "validated")
        self.assertEqual(metric["scope"], "global non-terminal work items")
        self.assertEqual(snapshot["overall"]["coverage"], "full")

    def test_each_missing_authority_proof_fails_closed(self) -> None:
        cases = {
            "authority_revision": lambda evidence: evidence.pop("authority_revision"),
            "required_endpoint_coverage": lambda evidence: evidence.pop("required_endpoints"),
            "materialization": lambda evidence: evidence.pop("materializations"),
            "freshness": lambda evidence: evidence.pop("expires_at"),
        }
        for missing, mutate in cases.items():
            with self.subTest(missing=missing):
                evidence = deepcopy(_complete_authority_evidence())
                mutate(evidence)

                snapshot = _snapshot(evidence)

                self.assertIn(snapshot["global_authority"]["status"], {"partial", "unknown"})
                self.assertNotEqual(snapshot["global_authority"]["scope"], "global")
                metric = snapshot["metrics"][0]
                self.assertNotEqual(metric["quality"], "validated")
                self.assertNotIn("global", metric["scope"].lower())
                self.assertIn(missing, snapshot["global_authority"]["degraded_reasons"])
                self.assertIn(snapshot["overall"]["coverage"], {"partial", "unknown"})

    def test_absent_authority_evidence_is_unknown_not_an_empty_global_ledger(self) -> None:
        snapshot = _snapshot(None)

        self.assertEqual(snapshot["global_authority"]["status"], "unknown")
        self.assertEqual(snapshot["global_authority"]["scope"], "unknown")
        self.assertEqual(snapshot["metrics"][0]["quality"], "insufficient")
        self.assertEqual(snapshot["overall"]["coverage"], "unknown")

    def test_different_endpoint_digests_cannot_claim_one_global_quantity(self) -> None:
        evidence = _complete_authority_evidence()
        evidence["materializations"][1]["digest"] = "sha256:" + "c" * 64

        snapshot = _snapshot(evidence)

        self.assertEqual(snapshot["global_authority"]["status"], "partial")
        self.assertEqual(snapshot["global_authority"]["materialization_digest"], None)
        self.assertIn("materialization", snapshot["global_authority"]["degraded_reasons"])
        self.assertNotEqual(snapshot["metrics"][0]["quality"], "validated")

    def test_expired_authority_evidence_cannot_claim_current_global_truth(self) -> None:
        evidence = _complete_authority_evidence()
        evidence["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()

        snapshot = _snapshot(evidence)

        self.assertEqual(snapshot["global_authority"]["freshness"], "stale")
        self.assertEqual(snapshot["global_authority"]["status"], "partial")
        self.assertIn("freshness", snapshot["global_authority"]["degraded_reasons"])

    def test_fresh_carrier_for_another_harness_revision_fails_closed(self) -> None:
        evidence = _complete_authority_evidence()
        other_revision = _authority_revision("2" * 40)
        evidence["authority_revision"] = other_revision
        for materialization in evidence["materializations"]:
            materialization["authority_revision"] = other_revision

        snapshot = _snapshot(evidence)

        self.assertEqual(snapshot["global_authority"]["freshness"], "fresh")
        self.assertEqual(snapshot["global_authority"]["status"], "partial")
        self.assertEqual(snapshot["global_authority"]["scope"], "partial")
        self.assertIn(
            "source_revision", snapshot["global_authority"]["degraded_reasons"]
        )
        self.assertEqual(snapshot["metrics"][0]["quality"], "insufficient")

    def test_missing_exact_harness_revision_cannot_validate_global_data(self) -> None:
        snapshot = build_snapshot(
            generated_at=NOW,
            expires_at=NOW + timedelta(minutes=45),
            capabilities=[_capability()],
            required_capability_ids=["s2.ledger_invariants"],
            incidents=[],
            metrics=[_metric()],
            provenance={
                "producer": {"name": "pulse-stats-exporter", "version": "test"},
                "source_revisions": {"pulse": "03207a2"},
                "sources": [],
            },
            headline="fixture",
            global_authority_evidence=_complete_authority_evidence(),
        )

        self.assertEqual(snapshot["global_authority"]["status"], "partial")
        self.assertIn(
            "source_revision", snapshot["global_authority"]["degraded_reasons"]
        )


if __name__ == "__main__":
    unittest.main()
