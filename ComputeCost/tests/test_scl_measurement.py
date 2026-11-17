from __future__ import annotations

from contextlib import redirect_stdout
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.cc_usage import CcUsageBinding, build_cc_session_snapshot, cc_authority_ref
from pulse_compute_cost.cli import main
from pulse_compute_cost.receipt_sources import _scl_expectations
from pulse_compute_cost.pi_usage import PiUsageBinding, build_pi_session_snapshot
from pulse_compute_cost.scl_measurement import (
    SclMeasurementError,
    load_scl_measurement_snapshot,
    validate_scl_measurement_snapshot,
)
from pulse_compute_cost.wi_discovery import auto_discover_wi_usage
from pulse_compute_cost.wi_report import build_wi_report


WI = "wi_20260820_568"
EPOCH_REF = "measurement_epoch_v1_" + "a" * 64
LINEAGE_REF = "lineage_v1_" + "b" * 64


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()


def domain_ref(prefix: str, domain: str, *parts: str) -> str:
    digest = sha256(domain.encode())
    for part in parts:
        digest.update(b"\0")
        digest.update(part.encode())
    return prefix + digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def attribution() -> dict[str, object]:
    return {
        "schema": "work_item_cost_attribution/v1",
        "schema_version": 1,
        "work_item_id": WI,
        "work_item_ref": domain_ref("work_item_v1_", "pulse-work-item-v1", WI),
        "measurement_epoch_ref": EPOCH_REF,
        "measurement_epoch_label": "prospective",
        "execution_epoch": 8,
        "execution_phase": "computecost_program",
        "execution_runtime": "pi",
        "agent_id": "pulse",
        "lineage_root_ref": LINEAGE_REF,
        "operation_ref": "operation_v1_test_scl_join",
        "started_at": "2026-09-02T08:00:00Z",
        "ended_at": None,
        "boundary_policy": "prospective_exact_lineage_no_backfill",
    }


def tokens(input_tokens: int, cached: int, output: int, reasoning: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input_tokens + output,
    }


def seal() -> dict[str, object]:
    return {
        "schema": "app_server_epoch_token_seal.v1",
        "schema_version": 1,
        "work_item_ref": domain_ref("work_item_v1_", "pulse-work-item-v1", WI),
        "measurement_epoch_ref": EPOCH_REF,
        "phase": "computecost_program",
        "execution_epoch": 8,
        "execution_surface": "surface_v1_cloud_linux_b",
        "runtime": "pi",
        "procurement_source": "unknown",
        "usage_producer": "pi_native",
        "provider": "unknown",
        "model": "unknown",
        "reasoning_effort": "unknown",
        "thread_ref": "provider_thread_v1_" + "c" * 64,
        "lineage_root_ref": LINEAGE_REF,
        "seal_id": "epoch_token_seal_v1_" + "d" * 64,
        "baseline_observation_id": "token_observation_v1_" + "e" * 64,
        "final_observation_id": "token_observation_v1_" + "f" * 64,
        "final_completion_id": "turn_completion_v1_" + "1" * 64,
        "final_turn_ref": "provider_turn_v1_" + "2" * 64,
        "sealed_after_sequence": 4,
        "baseline_total": tokens(10, 2, 3, 1),
        "final_total": tokens(30, 7, 11, 4),
        "delta": tokens(20, 5, 8, 3),
        "final_turn_completed": True,
        "accounting": "source_epoch_final",
        "coverage": "exact_except_cache_write_unavailable",
    }


def outcome() -> dict[str, object]:
    task_ref = "task_v1_" + "3" * 64
    value: dict[str, object] = {
        "schema_version": "work_item_task_outcome_receipt/v1",
        "work_item_id": WI,
        "producer": "pulse",
        "measurement_epoch_ref": EPOCH_REF,
        "task_ref": task_ref,
        "outcome_ref": domain_ref(
            "outcome_v1_", "agent-harness.task-outcome.outcome.v1", task_ref
        ),
        "status": "accepted",
        "verifier": "verifier_v1_opaque",
        "evidence": [
            {"ref": "evidence/outcomes/result.md", "sha256": "sha256:" + "4" * 64}
        ],
        "human_correction": False,
        "recorded_at": "2026-09-02T08:30:00Z",
    }
    value["receipt_sha256"] = "sha256:" + sha256(canonical(value)).hexdigest()
    return value


def attention() -> dict[str, object]:
    event: dict[str, object] = {
        "event_ref": "human_attention_v1_" + "5" * 64,
        "category": "human_touch",
        "effort_band": "bounded",
        "occurred_at": "2026-09-02T08:10:00Z",
        "source_ref": "evidence/attention/source.md",
        "source_sha256": "sha256:" + "6" * 64,
        "timing_basis": "unavailable",
        "measured_seconds": None,
    }
    event["event_sha256"] = "sha256:" + sha256(canonical(event)).hexdigest()
    return {
        "schema_version": "s1_human_attention_evidence/v1",
        "work_item_id": WI,
        "events": [event],
    }


class SclMeasurementTests(unittest.TestCase):
    def populate(self, root: Path) -> None:
        attr = attribution()
        operation_digest = sha256(str(attr["operation_ref"]).encode()).hexdigest()
        write_json(
            root / "evidence/compute-cost/attributions" / WI / f"{operation_digest}.json",
            attr,
        )
        usage = seal()
        write_json(
            root
            / "evidence/compute-cost/observations"
            / WI
            / "app_server_epoch_token_seal.v1"
            / f"{usage['seal_id']}.json",
            usage,
        )
        session_path = root.parent / "synthetic-pi.jsonl"
        header = {
            "type": "session", "version": 3, "id": "synthetic-private-session",
            "timestamp": "2026-09-02T08:00:00Z", "cwd": "/private/synthetic",
        }
        session_path.write_text(json.dumps(header) + "\n", encoding="utf-8")
        pi_binding = PiUsageBinding(
            work_item_id=WI,
            phase="computecost_program",
            epoch=8,
            procurement_source="unknown",
            runtime_binding_sha256="7" * 64,
            agent_id="pulse",
            execution_surface="surface_v1_cloud_linux_b",
            authority_ref="session_v1_" + "8" * 64,
        )
        baseline = build_pi_session_snapshot(
            session_path,
            pi_binding,
            generated_at="2026-09-02T08:00:01Z",
            measurement_role="baseline",
        )
        assistant = {
            "type": "message", "id": "bounded-assistant", "parentId": None,
            "timestamp": "2026-09-02T08:01:00Z",
            "message": {
                "role": "assistant", "provider": "openai", "model": "gpt-fixture",
                "content": [{"type": "text", "text": "private"}],
                "usage": {
                    "input": 13, "cacheRead": 5, "cacheWrite": 2, "output": 8,
                    "reasoning": 3, "totalTokens": 28,
                },
            },
        }
        session_path.write_text(
            json.dumps(header) + "\n" + json.dumps(assistant) + "\n",
            encoding="utf-8",
        )
        final = build_pi_session_snapshot(
            session_path,
            pi_binding,
            generated_at="2026-09-02T08:02:00Z",
            lifecycle_state="sealed",
            measurement_role="final",
            baseline_snapshot=baseline,
        )
        pi_measurement: dict[str, object] = {
            "schema": "scl_pi_usage_measurement/v1",
            "schema_version": 1,
            "work_item_ref": attr["work_item_ref"],
            "measurement_epoch_ref": EPOCH_REF,
            "execution_epoch": 8,
            "phase": "computecost_program",
            "runtime": "pi",
            "agent_id": "pulse",
            "execution_surface": "surface_v1_cloud_linux_b",
            "runtime_binding_sha256": pi_binding.runtime_binding_sha256,
            "baseline_snapshot": baseline,
            "final_snapshot": final,
            "accounting": "baseline_exclusive_final_inclusive",
            "coverage": "exact_bounded_sealed",
        }
        pi_measurement["artifact_sha256"] = sha256(canonical(pi_measurement)).hexdigest()
        pi_path = (
            root / "evidence/compute-cost/observations" / WI
            / "scl_pi_usage_measurement.v1" / f"{EPOCH_REF}.json"
        )
        write_json(pi_path, pi_measurement)
        pi_path.chmod(0o600)
        result = outcome()
        write_json(
            root / "evidence/task-outcomes" / WI / f"{result['outcome_ref']}.json",
            result,
        )
        write_json(root / "evidence/measurements" / WI / "human-attention.json", attention())

    def test_zero_manual_join_preserves_unknown_cache_write_and_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".harness"
            root.mkdir()
            self.populate(root)

            snapshot = load_scl_measurement_snapshot(root, WI)
            self.assertEqual(snapshot["status"], "complete")
            self.assertEqual(snapshot["eligible_epoch_count"], 1)
            self.assertEqual(
                snapshot["epochs"][0]["usage"]["tokens"]["cache_write_input_tokens"],
                "unavailable",
            )
            self.assertEqual(snapshot["human_attention"]["measured_seconds"], "unavailable")
            self.assertEqual(snapshot["epochs"][0]["pi_usage"]["status"], "exact_bounded_sealed")

            report = build_wi_report(None, WI, scl_measurement_snapshot=snapshot)
            self.assertEqual(report["usage"]["status"], "partial")
            self.assertEqual(report["cost"]["token_resource"], "partial")
            self.assertEqual(report["coverage"]["scl_measurement"], "complete")
            self.assertEqual(report["cost"]["machine_cash_usd"], "unavailable")
            self.assertEqual(report["cost"]["cash_basis"], "openrouter_standard_token_cash")
            self.assertEqual(report["cost"]["human_attention"]["raw_turn_count"], 1)

    def test_trusted_prospective_timer_is_focused_time_not_elapsed_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".harness"
            root.mkdir()
            self.populate(root)
            evidence = attention()
            event = evidence["events"][0]  # type: ignore[index]
            event["timing_basis"] = "trusted_telemetry"  # type: ignore[index]
            event["measured_seconds"] = 75  # type: ignore[index]
            event["event_sha256"] = "sha256:" + sha256(canonical({
                key: item for key, item in event.items() if key != "event_sha256"  # type: ignore[union-attr]
            })).hexdigest()
            write_json(
                root / "evidence/measurements" / WI / "human-attention.json",
                evidence,
            )

            snapshot = load_scl_measurement_snapshot(root, WI)
            report = build_wi_report(None, WI, scl_measurement_snapshot=snapshot)
            vector = report["cost"]["human_attention"]

            self.assertEqual(vector["focused_seconds"], 75)
            self.assertEqual(vector["focused_seconds_coverage"], "observed")
            self.assertEqual(vector["cycle_elapsed_seconds"], "unavailable")
            self.assertEqual(vector["exact_human_feedback_count"], 1)
            self.assertEqual(vector["human_attention_units"], "1")

    def test_expected_pi_without_a_bounded_final_is_producer_missing_not_join_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".harness"
            root.mkdir()
            work_items = root / "work-items"
            work_items.mkdir()
            (work_items / f"{WI}.yaml").write_text(
                f"work_item_id: {WI}\nstatus: completed\n"
                "created_at: '2026-09-02T07:00:00Z'\n"
                "updated_at: '2026-09-02T09:00:00Z'\n",
                encoding="utf-8",
            )
            self.populate(root)
            for path in (
                root / "evidence/compute-cost/observations" / WI
                / "scl_pi_usage_measurement.v1"
            ).glob("*.json"):
                path.unlink()
            cohort = auto_discover_wi_usage(
                WI,
                machine="synthetic-macos",
                harness_root=root,
                codex_home=root.parent / "unmanaged-pi-has-no-codex-evidence",
            ).to_dict()
            pi_attempt = next(
                item for item in cohort["producer_attempts"]
                if item["producer"] == "pi_managed_scl"
            )
            self.assertEqual(pi_attempt["status"], "producer_missing")
            self.assertEqual(cohort["status"], "producer_missing")
            self.assertNotEqual(cohort["status"], "join_conflict")
            self.assertIn("scl_pi_baseline_final", pi_attempt["adapters"])

    def test_bounded_pi_scl_artifact_requires_private_mode_and_registered_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".harness"
            root.mkdir()
            self.populate(root)
            pi_path = next((
                root / "evidence/compute-cost/observations" / WI
                / "scl_pi_usage_measurement.v1"
            ).glob("*.json"))
            pi_path.chmod(0o644)
            with self.assertRaisesRegex(SclMeasurementError, "mode-0600"):
                load_scl_measurement_snapshot(root, WI)

            pi_path.chmod(0o600)
            value = json.loads(pi_path.read_text(encoding="utf-8"))
            value["execution_surface"] = "surface_v1_unregistered"
            value["artifact_sha256"] = sha256(canonical({
                key: item for key, item in value.items() if key != "artifact_sha256"
            })).hexdigest()
            write_json(pi_path, value)
            pi_path.chmod(0o600)
            with self.assertRaisesRegex(SclMeasurementError, "not registered"):
                load_scl_measurement_snapshot(root, WI)

    def test_pi_pair_cannot_change_wi_or_procurement_behind_the_canonical_seal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".harness"
            root.mkdir()
            self.populate(root)
            pi_path = next((
                root / "evidence/compute-cost/observations" / WI
                / "scl_pi_usage_measurement.v1"
            ).glob("*.json"))
            value = json.loads(pi_path.read_text(encoding="utf-8"))
            baseline = value["baseline_snapshot"]
            final = value["final_snapshot"]
            for candidate in (baseline, final):
                candidate["work_item_id"] = "wi_20260901_999"
                candidate["procurement_source"] = "openrouter_api"
            baseline["snapshot_sha256"] = sha256(canonical({
                key: item for key, item in baseline.items()
                if key != "snapshot_sha256"
            })).hexdigest()
            final["attribution_boundary"]["baseline_snapshot_sha256"] = baseline[
                "snapshot_sha256"
            ]
            final["snapshot_sha256"] = sha256(canonical({
                key: item for key, item in final.items()
                if key != "snapshot_sha256"
            })).hexdigest()
            value["artifact_sha256"] = sha256(canonical({
                key: item for key, item in value.items()
                if key != "artifact_sha256"
            })).hexdigest()
            write_json(pi_path, value)
            pi_path.chmod(0o600)
            with self.assertRaisesRegex(SclMeasurementError, "authority does not match"):
                load_scl_measurement_snapshot(root, WI)

    def test_user_supplied_projection_cannot_reprice_tampered_pi_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".harness"
            root.mkdir()
            self.populate(root)
            snapshot = load_scl_measurement_snapshot(root, WI)
            pi_usage = snapshot["epochs"][0]["pi_usage"]
            pi_usage["summary"]["request_partitions"][0]["input"] += 1
            snapshot["snapshot_sha256"] = sha256(canonical({
                key: item for key, item in snapshot.items()
                if key != "snapshot_sha256"
            })).hexdigest()
            with self.assertRaisesRegex(SclMeasurementError, "input/cache"):
                validate_scl_measurement_snapshot(
                    snapshot,
                    expected_work_item_id=WI,
                )

    def test_tampered_outcome_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".harness"
            root.mkdir()
            self.populate(root)
            outcome_path = next((root / "evidence/task-outcomes" / WI).glob("*.json"))
            value = json.loads(outcome_path.read_text(encoding="utf-8"))
            value["status"] = "rejected"
            write_json(outcome_path, value)
            with self.assertRaisesRegex(SclMeasurementError, "digest"):
                load_scl_measurement_snapshot(root, WI)

    def test_cli_auto_loads_harness_evidence_without_per_artifact_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".harness"
            root.mkdir()
            work_items = root / "work-items"
            work_items.mkdir()
            (work_items / f"{WI}.yaml").write_text(
                f"work_item_id: {WI}\nstatus: completed\n"
                "created_at: '2026-09-02T07:00:00Z'\n"
                "updated_at: '2026-09-02T09:00:00Z'\n",
                encoding="utf-8",
            )
            self.populate(root)
            models = root.parent / "openrouter-models.json"
            write_json(models, {"data": [{
                "id": "openai/gpt-fixture",
                "canonical_slug": "openai/gpt-fixture-20260902",
                "pricing": {
                    "prompt": "0.01", "input_cache_read": "0.001",
                    "input_cache_write": "0.002", "completion": "0.02",
                    "overrides": [{
                        "min_prompt_tokens": 20,
                        "prompt": "0.1", "input_cache_read": "0.01",
                        "input_cache_write": "0.02", "completion": "0.2",
                    }],
                },
            }]})
            cohort = auto_discover_wi_usage(
                WI,
                machine="synthetic-macos",
                harness_root=root,
                observed_at="2026-09-02T09:01:00Z",
            ).to_dict(include_events=True)
            self.assertEqual(cohort["events"][0]["lineage"]["root_agent"], "unknown")
            self.assertEqual(cohort["events"][0]["lineage"]["session_role"], "unknown")

            output = StringIO()
            with redirect_stdout(output):
                code = main([
                    "wi-report", WI, "--harness-root", str(root),
                    "--openrouter-models-snapshot", str(models),
                ])
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["coverage"]["scl_measurement"], "complete")
            self.assertEqual(report["coverage"]["producer_discovery"], "observed")
            self.assertEqual(report["coverage"]["pi_usage"], "exact_bounded_sealed")
            self.assertEqual(report["usage"]["tokens"]["input_tokens"], 20)
            self.assertEqual(report["usage"]["tokens"]["cache_write_input_tokens"], 2)
            self.assertEqual(report["usage"]["harness_scl"]["eligible_epoch_count"], 1)
            price = report["cost"]["openrouter_cash_valuation"]
            self.assertEqual(price["status"], "calculated")
            self.assertEqual(price["total_usd"], "0.299")
            self.assertEqual(price["partitions"][0]["tier"], "standard")
            self.assertEqual(
                price["pricing_policy"],
                "openrouter_standard_rates_no_overrides_no_subscription_settlement",
            )
            self.assertEqual(report["cost"]["machine_cash_usd"], "0.299")
            self.assertEqual(
                report["cost"]["cash_coverage"],
                "calculated_from_digest_bound_openrouter_catalog",
            )
            self.assertEqual(
                report["cost"]["cash_basis"],
                "openrouter_standard_token_cash",
            )
            serialized = json.dumps(report)
            self.assertNotIn("synthetic-private-session", serialized)
            self.assertNotIn("/private/synthetic", serialized)


if __name__ == "__main__":
    unittest.main()


class SclCcMeasurementTests(unittest.TestCase):
    """Relay-observed CC usage sealed by Harness joins exactly like Pi."""

    SESSION = "11111111-2222-4333-8444-555555555555"
    BINDING = "9" * 64

    def populate(self, root: Path) -> Path:
        attr = attribution()
        attr["execution_runtime"] = "cc"
        operation_digest = sha256(str(attr["operation_ref"]).encode()).hexdigest()
        write_json(root / "evidence/compute-cost/attributions" / WI / f"{operation_digest}.json", attr)
        usage = seal()
        usage.update({
            "runtime": "cc", "usage_producer": "cc_native", "procurement_source": "claude_subscription",
            "baseline_total": tokens(0, 0, 0, 0), "final_total": tokens(20, 5, 8, 0), "delta": tokens(20, 5, 8, 0),
        })
        write_json(
            root / "evidence/compute-cost/observations" / WI / "app_server_epoch_token_seal.v1"
            / f"{usage['seal_id']}.json",
            usage,
        )
        transcript = root.parent / f"{self.SESSION}.jsonl"
        transcript.write_text("", encoding="utf-8")
        binding = CcUsageBinding(
            work_item_id=WI, agent_id="pulse", phase="computecost_program", epoch=8, runtime="cc",
            procurement_source="claude_subscription", authority_ref=cc_authority_ref(self.SESSION),
            execution_surface="surface_v1_cloud_linux_b", runtime_binding_sha256=self.BINDING,
        )
        baseline = build_cc_session_snapshot(transcript, self.SESSION, binding, lifecycle_state="open",
            measurement_role="baseline", generated_at="2026-09-02T08:00:01Z")
        transcript.write_text(json.dumps({
            "type": "assistant", "uuid": "a1", "sessionId": self.SESSION, "timestamp": "2026-09-02T08:01:00Z",
            "message": {"id": "msg_1", "model": "claude-opus-4-1", "role": "assistant",
                        "usage": {"input_tokens": 13, "cache_read_input_tokens": 5,
                                  "cache_creation_input_tokens": 2, "output_tokens": 8}},
        }) + "\n", encoding="utf-8")
        final = build_cc_session_snapshot(transcript, self.SESSION, binding, lifecycle_state="sealed",
            measurement_role="final", baseline_snapshot=baseline, generated_at="2026-09-02T08:02:00Z")
        measurement: dict[str, object] = {
            "schema": "scl_cc_usage_measurement/v1", "schema_version": 1,
            "work_item_ref": attr["work_item_ref"], "measurement_epoch_ref": EPOCH_REF,
            "execution_epoch": 8, "phase": "computecost_program", "runtime": "cc", "agent_id": "pulse",
            "execution_surface": "surface_v1_cloud_linux_b", "runtime_binding_sha256": self.BINDING,
            "baseline_snapshot": baseline, "final_snapshot": final,
            "accounting": "baseline_exclusive_final_inclusive", "coverage": "exact_bounded_sealed",
        }
        measurement["artifact_sha256"] = sha256(canonical(measurement)).hexdigest()
        path = root / "evidence/compute-cost/observations" / WI / "scl_cc_usage_measurement.v1" / f"{EPOCH_REF}.json"
        write_json(path, measurement)
        path.chmod(0o600)
        result = outcome()
        write_json(root / "evidence/task-outcomes" / WI / f"{result['outcome_ref']}.json", result)
        write_json(root / "evidence/measurements" / WI / "human-attention.json", attention())
        return path

    def rewrite(self, path: Path, mutate) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))
        mutate(value)
        value["artifact_sha256"] = sha256(canonical({
            key: item for key, item in value.items() if key != "artifact_sha256"
        })).hexdigest()
        write_json(path, value)
        path.chmod(0o600)

    def test_sealed_cc_measurement_joins_and_makes_the_epoch_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".harness"
            root.mkdir()
            self.populate(root)
            snapshot = load_scl_measurement_snapshot(root, WI)
            epoch = snapshot["epochs"][0]
            self.assertEqual(epoch["pi_usage"], "unavailable")
            self.assertEqual(epoch["cc_usage"]["status"], "exact_bounded_sealed")
            self.assertEqual(epoch["cc_usage"]["summary"]["schema"], "cc_native_usage_summary/v1")
            self.assertTrue(epoch["measurement_eligible"])
            self.assertEqual(snapshot["status"], "complete")
            self.assertEqual(snapshot["missing_producers"], [])
            validate_scl_measurement_snapshot(snapshot, expected_work_item_id=WI)
            report = build_wi_report(None, WI, scl_measurement_snapshot=snapshot)
            self.assertEqual(report["coverage"]["scl_measurement"], "complete")
            self.assertEqual(report["coverage"]["pi_usage"], "unavailable")
            expectations: list = []
            _scl_expectations(root, "2026-09-02T09:00:00Z", expectations)
            payload = next(r.payload for r in expectations if r.kind == "execution_expectation")
            self.assertEqual((payload["runtime"], payload["status"]), ("cc", "sealed"))
            self.assertIsNotNone(payload["assignment_ref"])

    def test_cc_epoch_without_measurement_is_missing_not_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".harness"
            root.mkdir()
            self.populate(root).unlink()
            snapshot = load_scl_measurement_snapshot(root, WI)
            self.assertFalse(snapshot["epochs"][0]["measurement_eligible"])
            self.assertEqual(snapshot["missing_producers"], ["cc_bounded_usage"])
            self.assertEqual(snapshot["status"], "partial")
            validate_scl_measurement_snapshot(snapshot, expected_work_item_id=WI)

    def test_cc_measurement_rejects_wrong_producer_runtime_or_tampering(self) -> None:
        cases = (
            ("no exact attribution", lambda v: v.update(runtime="pi")),
            ("digest does not match", lambda v: v["final_snapshot"].update(runtime="cc_sdk")),
            ("schema is unsupported", lambda v: v.update(schema="scl_pi_usage_measurement/v1")),
        )
        for message, mutate in cases:
            with self.subTest(message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / ".harness"
                root.mkdir()
                path = self.populate(root)
                self.rewrite(path, mutate)
                with self.assertRaisesRegex(SclMeasurementError, message):
                    load_scl_measurement_snapshot(root, WI)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".harness"
            root.mkdir()
            self.populate(root)
            seal_path = next((root / "evidence/compute-cost/observations" / WI
                              / "app_server_epoch_token_seal.v1").glob("*.json"))
            value = json.loads(seal_path.read_text(encoding="utf-8"))
            value["usage_producer"] = "pi_native"
            write_json(seal_path, value)
            with self.assertRaisesRegex(SclMeasurementError, "no exact attribution"):
                load_scl_measurement_snapshot(root, WI)

    def test_projection_cannot_move_cc_usage_into_the_pi_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".harness"
            root.mkdir()
            self.populate(root)
            snapshot = load_scl_measurement_snapshot(root, WI)
            epoch = snapshot["epochs"][0]
            epoch["pi_usage"], epoch["cc_usage"] = epoch["cc_usage"], "unavailable"
            snapshot["snapshot_sha256"] = sha256(canonical({
                key: item for key, item in snapshot.items() if key != "snapshot_sha256"
            })).hexdigest()
            with self.assertRaisesRegex(SclMeasurementError, "Pi usage is invalid"):
                validate_scl_measurement_snapshot(snapshot, expected_work_item_id=WI)
