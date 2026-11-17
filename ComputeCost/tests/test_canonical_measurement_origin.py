"""Canonical origin recovery preserves the accepted native measurement interval."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pulse_compute_cost.cli import main as cli_main
from pulse_compute_cost.pi_usage import PiUsageBinding, build_pi_session_snapshot
from pulse_compute_cost.runtime_binding import (
    CANONICAL_MEASUREMENT_BASELINE_ENV,
    RUNTIME_USAGE_BINDING_ENV,
    RuntimeUsageBinding,
    RuntimeUsageBindingError,
    build_runtime_usage_binding,
    load_measurement_baseline_reference,
)


class CanonicalMeasurementOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "private-session.jsonl"
        self.baseline_path = self.root / "baseline.json"
        self.original = self.binding("2026-09-21T01:00:00Z")
        self.active = self.binding("2026-09-21T02:00:00Z")
        self.before = [
            {"type": "session", "version": 3, "id": "private-session",
             "timestamp": "2026-09-21T00:00:00Z"},
            self.message("prior-usage", 1000, "2026-09-21T00:01:00Z"),
        ]
        self.write_source(self.before)
        self.baseline = build_pi_session_snapshot(
            self.source,
            PiUsageBinding(
                work_item_id=self.original.work_item_id,
                phase=self.original.phase,
                epoch=self.original.epoch,
                procurement_source=self.original.procurement_source,
                runtime_binding_sha256=self.original.binding_sha256,
                agent_id=self.original.agent_id,
                execution_surface=self.original.execution_surface,
                authority_ref=self.original.authority_ref,
            ),
            generated_at="2026-09-21T01:00:01Z",
            measurement_role="baseline",
        )
        self.baseline_path.write_text(json.dumps(self.baseline), encoding="utf-8")
        self.baseline_path.chmod(0o600)
        self.reference = {
            "schema": "pulse_canonical_measurement_baseline_reference/v1",
            "status": "open",
            **{key: getattr(self.active, key) for key in (
                "work_item_id", "agent_id", "epoch", "phase", "runtime",
                "procurement_source", "authority_ref", "execution_surface",
            )},
            "measurement_epoch_ref": "measurement_epoch_v1_" + "7" * 64,
            "baseline_binding_sha256": self.original.binding_sha256,
            "baseline_sha256": self.baseline["snapshot_sha256"],
            "active_runtime_binding_sha256": self.active.binding_sha256,
        }
        self.write_source([
            *self.before,
            self.message("isolated-usage", 10, "2026-09-21T01:30:00Z"),
            self.message("canonical-usage", 20, "2026-09-21T02:30:00Z"),
        ])

    @staticmethod
    def binding(issued_at: str, *, epoch: int = 5) -> RuntimeUsageBinding:
        return build_runtime_usage_binding(
            work_item_id="wi_20260823_184", agent_id="pulse", epoch=epoch,
            phase="migration_plan_pr_review", runtime="pi",
            procurement_source="chatgpt_subscription",
            authority_ref="session_v1_" + "a" * 64,
            execution_surface="surface_v1_cloud_linux_b", issued_at=issued_at,
        )

    @staticmethod
    def message(identity: str, tokens: int, timestamp: str) -> dict:
        return {
            "type": "message", "id": identity, "timestamp": timestamp,
            "message": {
                "role": "assistant", "provider": "openai-codex", "model": "gpt-fixture",
                "usage": {"input": tokens, "output": 2, "cacheRead": 0,
                          "cacheWrite": 0, "totalTokens": tokens + 2},
            },
        }

    def write_source(self, entries: list[dict]) -> None:
        self.source.write_text("".join(json.dumps(row) + "\n" for row in entries), encoding="utf-8")

    def environment(self) -> dict[str, str]:
        return {
            "HOME": str(self.root), "TMPDIR": str(self.root),
            "PI_SESSION_FILE": str(self.source),
            RUNTIME_USAGE_BINDING_ENV: json.dumps(self.active.to_dict()),
            CANONICAL_MEASUREMENT_BASELINE_ENV: json.dumps(self.reference),
            "PULSE_PI_USAGE_BASELINE_SNAPSHOT": str(self.baseline_path),
        }

    def probe(self, role: str, *, env: dict[str, str] | None = None) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        arguments = ["pi-session-probe", "--measurement-role", role,
                     "--lifecycle-state", "sealed" if role == "final" else "open"]
        with patch.dict(os.environ, self.environment() if env is None else env, clear=True), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_main(arguments)
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else {}, stderr.getvalue()

    def test_reference_preserves_current_v3_and_original_baseline_identity(self) -> None:
        before = self.active.to_dict()
        self.assertNotEqual(self.active.binding_sha256, self.original.binding_sha256)
        result = load_measurement_baseline_reference(self.active, self.environment())
        self.assertEqual(result, self.reference)
        self.assertEqual(self.active.to_dict(), before)
        self.assertEqual(RuntimeUsageBinding.from_mapping(before), self.active)
        self.assertIsNone(load_measurement_baseline_reference(self.active, {}))

    def test_reference_rejects_each_tuple_mismatch_and_closed_epoch(self) -> None:
        mismatches = {
            "work_item_id": "wi_20260823_185", "agent_id": "another",
            "epoch": 6, "phase": "other_phase", "runtime": "codex",
            "procurement_source": "openrouter_api",
            "authority_ref": "session_v1_" + "b" * 64,
            "execution_surface": "surface_v1_cloud_linux_a",
            "active_runtime_binding_sha256": self.original.binding_sha256,
            "measurement_epoch_ref": "measurement_epoch_v1_bad",
            "baseline_binding_sha256": "bad", "baseline_sha256": "bad",
            "status": "sealed", "schema": "unsupported/v1",
        }
        for key, value in mismatches.items():
            with self.subTest(field=key), self.assertRaises(RuntimeUsageBindingError):
                load_measurement_baseline_reference(self.active, {
                    CANONICAL_MEASUREMENT_BASELINE_ENV: json.dumps({**self.reference, key: value}),
                })

    def test_reference_rejects_unknown_missing_and_invalid_fields(self) -> None:
        missing = dict(self.reference)
        missing.pop("baseline_sha256")
        for value in ({**self.reference, "issued_at": self.original.issued_at}, missing, [], None):
            with self.subTest(value=value), self.assertRaises(RuntimeUsageBindingError):
                load_measurement_baseline_reference(self.active, {
                    CANONICAL_MEASUREMENT_BASELINE_ENV: json.dumps(value),
                })
        with self.assertRaises(RuntimeUsageBindingError):
            load_measurement_baseline_reference(self.active, {CANONICAL_MEASUREMENT_BASELINE_ENV: "{"})

    def test_reference_rejects_boolean_epoch_even_at_epoch_one(self) -> None:
        active = self.binding("2026-09-21T02:00:00Z", epoch=1)
        reference = {**self.reference, "epoch": True,
                     "active_runtime_binding_sha256": active.binding_sha256}
        with self.assertRaises(RuntimeUsageBindingError):
            load_measurement_baseline_reference(active, {
                CANONICAL_MEASUREMENT_BASELINE_ENV: json.dumps(reference),
            })

    def test_progress_and_final_keep_original_cumulative_boundary_without_rewriting_baseline(self) -> None:
        original_bytes = self.baseline_path.read_bytes()
        original_source = self.source.read_bytes()
        active = self.active.to_dict()
        for role in ("progress", "final"):
            with self.subTest(role=role):
                code, result, error = self.probe(role)
                self.assertEqual(code, 0, error)
                self.assertEqual(result["runtime_binding_sha256"], self.original.binding_sha256)
                boundary = result["attribution_boundary"]
                self.assertEqual(boundary["baseline_snapshot_sha256"], self.baseline["snapshot_sha256"])
                self.assertEqual(boundary["baseline"], self.baseline["attribution_boundary"]["baseline"])
                self.assertEqual(boundary["excluded_pre_baseline_usage_events"], 1)
                self.assertEqual(result["summary"]["event_count"], 2)
                self.assertEqual(result["summary"]["totals"]["total_tokens"], 34)
                self.assertEqual(result["lifecycle_state"], "sealed" if role == "final" else "open")
                self.assertEqual(result["phase"], "migration_plan_pr_review")
        self.assertEqual(self.baseline_path.read_bytes(), original_bytes)
        self.assertEqual(self.source.read_bytes(), original_source)
        self.assertEqual(self.active.to_dict(), active)

    def test_canonical_origin_cannot_create_another_baseline(self) -> None:
        previous = self.baseline_path.read_bytes()
        code, _, error = self.probe("baseline")
        self.assertEqual(code, 1)
        self.assertIn("cannot be regenerated", error)
        self.assertEqual(self.baseline_path.read_bytes(), previous)

    def test_progress_refuses_missing_or_different_private_baseline(self) -> None:
        env = self.environment()
        env.pop("PULSE_PI_USAGE_BASELINE_SNAPSHOT")
        code, _, error = self.probe("progress", env=env)
        self.assertEqual(code, 1)
        self.assertIn("baseline is unavailable", error)
        env = self.environment()
        env[CANONICAL_MEASUREMENT_BASELINE_ENV] = json.dumps({**self.reference, "baseline_sha256": "f" * 64})
        code, _, error = self.probe("progress", env=env)
        self.assertEqual(code, 1)
        self.assertIn("origin baseline does not match", error)

    def test_final_rejects_modified_native_prefix(self) -> None:
        self.source.write_text(self.source.read_text().replace("prior-usage", "other-usage"))
        code, _, error = self.probe("final")
        self.assertEqual(code, 1)
        self.assertIn("changed before the immutable baseline", error)

    def test_final_rejects_tampered_baseline_even_with_original_digest_field(self) -> None:
        value = json.loads(self.baseline_path.read_text())
        value["phase"] = "other_phase"
        self.baseline_path.write_text(json.dumps(value))
        code, _, error = self.probe("final")
        self.assertEqual(code, 1)
        self.assertIn("digest", error)

    def test_active_binding_cannot_be_forged_to_the_old_digest(self) -> None:
        env = self.environment()
        env[RUNTIME_USAGE_BINDING_ENV] = json.dumps({
            **self.active.to_dict(), "binding_sha256": self.original.binding_sha256,
        })
        code, _, error = self.probe("final", env=env)
        self.assertEqual(code, 1)
        self.assertIn("digest does not match", error)

    def test_final_capture_replay_keeps_the_original_origin_artifact(self) -> None:
        target = self.root / f"{self.active.work_item_id}-{self.original.binding_sha256}-sealed.json"
        env = {**self.environment(), "PULSE_PI_USAGE_CAPTURE_PATH": str(target)}
        code, first, error = self.probe("final", env=env)
        self.assertEqual(code, 0, error)
        self.assertFalse(first["idempotent_replay"])
        artifact = target.read_bytes()
        code, replay, error = self.probe("final", env=env)
        self.assertEqual(code, 0, error)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["snapshot"], first["snapshot"])
        self.assertEqual(replay["snapshot"]["runtime_binding_sha256"], self.original.binding_sha256)
        self.assertEqual(target.read_bytes(), artifact)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
