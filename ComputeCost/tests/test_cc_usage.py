from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from pulse_compute_cost.cc_usage import (
    CcUsageBinding,
    CcUsageError,
    build_cc_session_snapshot,
    capture_cc_session_snapshot,
    cc_authority_ref,
    validate_cc_session_snapshot,
)
from pulse_compute_cost.cli import main
from pulse_compute_cost.native_attribution import _host_authorities, bind_cc_boundaries
from pulse_compute_cost.receipt_sources import read_cc, read_host
from pulse_compute_cost.pi_usage import PiUsageError
from pulse_compute_cost.receipts import ReceiptError, reference
from pulse_compute_cost.runtime_binding import build_runtime_usage_binding
from pulse_compute_cost.wi_discovery import _cc_binding_intervals


WI = "wi_20261001_297"
SESSION = "11111111-2222-4333-8444-555555555555"
SURFACE = "surface_v1_ownermac_relay"


def _binding(**overrides):
    values = dict(
        work_item_id=WI, agent_id="pulse", phase="implementation", epoch=1, runtime="cc",
        procurement_source="claude_subscription", authority_ref=cc_authority_ref(SESSION),
        execution_surface=SURFACE, runtime_binding_sha256="a" * 64,
    )
    values.update(overrides)
    return CcUsageBinding(**values)


def _assistant(message_id: str, uuid: str, at: str, *, output: int, model: str = "claude-opus-4-1") -> str:
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": SESSION, "timestamp": at, "isSidechain": False,
        "message": {"id": message_id, "model": model, "role": "assistant", "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 100,
                              "cache_creation_input_tokens": 5, "output_tokens": output}},
    }) + "\n"


def _sidechain(message_id: str, uuid: str, at: str, *, output: int) -> str:
    row = json.loads(_assistant(message_id, uuid, at, output=output))
    row.update(isSidechain=True, agentId="a1b2c3")
    return json.dumps(row) + "\n"


def _user(uuid: str, at: str) -> str:
    return json.dumps({"type": "user", "uuid": uuid, "sessionId": SESSION, "timestamp": at,
                       "isSidechain": False, "message": {"role": "user", "content": "hi"}}) + "\n"


class CcBoundedSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "projects" / "-ws"
        self.project.mkdir(parents=True)
        self.transcript = self.project / f"{SESSION}.jsonl"

    def test_baseline_is_exact_zero_and_final_counts_only_post_baseline_requests(self) -> None:
        self.transcript.write_text(_user("u0", "2026-10-01T00:00:00Z")
                                   + _assistant("msg_old", "a0", "2026-10-01T00:00:01Z", output=7))
        binding = _binding()
        baseline = build_cc_session_snapshot(self.transcript, SESSION, binding,
            lifecycle_state="open", measurement_role="baseline", generated_at="2026-10-01T00:00:02Z")
        self.assertEqual(baseline["summary"]["event_count"], 0)
        self.assertEqual(baseline["attribution_boundary"]["excluded_pre_baseline_usage_events"], 1)
        self.assertEqual(baseline["coverage"], "exact_zero_baseline")
        with self.transcript.open("a") as stream:
            stream.write(_user("u1", "2026-10-01T00:00:03Z"))
            # CC 每个 content block 一行；同一 message.id 只算一次，取最后计数。
            stream.write(_assistant("msg_new", "a1", "2026-10-01T00:00:04Z", output=3))
            stream.write(_assistant("msg_new", "a2", "2026-10-01T00:00:04Z", output=20))
            stream.write('{"type":"assistant","partial')  # 未完成的尾行不进入前缀
        final = build_cc_session_snapshot(self.transcript, SESSION, binding,
            lifecycle_state="sealed", measurement_role="final", baseline_snapshot=baseline,
            generated_at="2026-10-01T00:00:05Z")
        validate_cc_session_snapshot(final)
        summary = final["summary"]
        self.assertEqual(summary["schema"], "cc_native_usage_summary/v1")
        self.assertEqual(summary["event_count"], 1)
        self.assertEqual(summary["totals"], {"input": 10, "cache_read": 100, "cache_write": 5,
                                             "output": 20, "reasoning": 0, "total_tokens": 135})
        self.assertEqual(summary["deduplicated_entry_count"], 1)
        self.assertEqual(summary["request_partitions"][0]["provider"], "anthropic")
        self.assertEqual(final["runtime"], "cc")
        self.assertNotIn(SESSION, json.dumps(final))

    def test_subagent_transcripts_are_bounded_per_file(self) -> None:
        subagents = self.project / SESSION / "subagents"
        subagents.mkdir(parents=True)
        early = subagents / "agent-aaaa.jsonl"
        self.transcript.write_text(_assistant("msg_main_old", "m0", "2026-10-01T00:00:01Z", output=1))
        early.write_text(_sidechain("msg_sub_old", "s0", "2026-10-01T00:00:01Z", output=2))
        (subagents / "agent-aaaa.meta.json").write_text("{}")  # 元数据不是 transcript
        binding = _binding()
        baseline = build_cc_session_snapshot(self.transcript, SESSION, binding,
            lifecycle_state="open", measurement_role="baseline", generated_at="2026-10-01T00:00:02Z")
        self.assertEqual(baseline["schema"], "cc_session_usage_snapshot/v2")
        self.assertEqual(baseline["attribution_boundary"]["excluded_pre_baseline_usage_events"], 2)
        self.assertEqual(len(baseline["attribution_boundary"]["baseline"]["sources"]), 2)
        with early.open("a") as stream:
            stream.write(_sidechain("msg_sub_new", "s1", "2026-10-01T00:00:03Z", output=3))
            stream.write(_sidechain("msg_sub_new", "s2", "2026-10-01T00:00:03Z", output=30))
        # baseline 之后才出现的子代理文件从 0 起算。
        (subagents / "agent-bbbb.jsonl").write_text(_sidechain("msg_late", "s3", "2026-10-01T00:00:04Z", output=4))
        with self.transcript.open("a") as stream:
            stream.write(_assistant("msg_main_new", "m1", "2026-10-01T00:00:05Z", output=5))
        final = build_cc_session_snapshot(self.transcript, SESSION, binding, lifecycle_state="sealed",
            measurement_role="final", baseline_snapshot=baseline, generated_at="2026-10-01T00:00:06Z")
        summary = final["summary"]
        self.assertEqual(summary["event_count"], 3)
        self.assertEqual(summary["totals"]["output"], 30 + 4 + 5)
        self.assertEqual(summary["deduplicated_entry_count"], 1)
        self.assertEqual(sorted(p["activity"] for p in summary["request_partitions"]),
                         ["assistant_turn", "sidechain", "sidechain"])
        final_sources = final["attribution_boundary"]["final"]["sources"]
        self.assertEqual(len(final_sources), 3)
        self.assertEqual(final["attribution_boundary"]["final"]["source_bytes"],
                         sum(item["source_bytes"] for item in final_sources))
        self.assertNotIn("agent-", json.dumps(final))
        # 改写任一 baseline 前缀（含子代理）都被拒绝。
        early.write_text(_sidechain("msg_sub_old", "s0", "2026-10-01T00:00:01Z", output=9))
        with self.assertRaisesRegex(CcUsageError, "changed before the baseline"):
            build_cc_session_snapshot(self.transcript, SESSION, binding, lifecycle_state="sealed",
                measurement_role="final", baseline_snapshot=baseline)
        early.unlink()
        with self.assertRaisesRegex(CcUsageError, "disappeared"):
            build_cc_session_snapshot(self.transcript, SESSION, binding, lifecycle_state="sealed",
                measurement_role="final", baseline_snapshot=baseline)

    def test_manifest_must_conserve_with_its_aggregate(self) -> None:
        baseline = build_cc_session_snapshot(self.transcript, SESSION, _binding(),
            lifecycle_state="open", measurement_role="baseline")
        tampered = json.loads(json.dumps(baseline))
        for edge in ("baseline", "final"):
            tampered["attribution_boundary"][edge]["sources"][0]["source_bytes"] = 7
        tampered["snapshot_sha256"] = sha256(json.dumps({k: v for k, v in tampered.items() if k != "snapshot_sha256"},
            ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        with self.assertRaisesRegex(PiUsageError, "source manifest"):
            validate_cc_session_snapshot(tampered)

    def test_missing_transcript_is_an_empty_baseline(self) -> None:
        baseline = build_cc_session_snapshot(self.transcript, SESSION, _binding(),
            lifecycle_state="open", measurement_role="baseline")
        self.assertEqual(baseline["attribution_boundary"]["baseline"]["source_bytes"], 0)

    def test_foreign_session_and_rewritten_prefix_are_rejected(self) -> None:
        with self.assertRaises(CcUsageError):
            build_cc_session_snapshot(self.transcript, "other-session", _binding(),
                lifecycle_state="open", measurement_role="baseline")
        self.transcript.write_text(_assistant("m1", "a1", "2026-10-01T00:00:01Z", output=1))
        baseline = build_cc_session_snapshot(self.transcript, SESSION, _binding(),
            lifecycle_state="open", measurement_role="baseline")
        self.transcript.write_text(_assistant("m1", "a1", "2026-10-01T00:00:01Z", output=2))
        with self.assertRaises(CcUsageError):
            build_cc_session_snapshot(self.transcript, SESSION, _binding(), lifecycle_state="sealed",
                measurement_role="final", baseline_snapshot=baseline)

    def test_capture_replays_the_immutable_winner(self) -> None:
        binding = _binding()
        target = self.project / "compute-cost" / f"{WI}-{binding.runtime_binding_sha256}-baseline.json"
        first = capture_cc_session_snapshot(self.transcript, SESSION, binding, output_path=target,
            lifecycle_state="open", measurement_role="baseline")
        second = capture_cc_session_snapshot(self.transcript, SESSION, binding, output_path=target,
            lifecycle_state="open", measurement_role="baseline")
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["snapshot"], second["snapshot"])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_cli_probe_requires_cc_binding_and_writes_bounded_capture(self) -> None:
        runtime_binding = build_runtime_usage_binding(
            work_item_id=WI, agent_id="pulse", phase="implementation", epoch=1, runtime="cc",
            procurement_source="claude_subscription", authority_ref=cc_authority_ref(SESSION),
            execution_surface=SURFACE)
        output = self.project / "compute-cost" / f"{WI}-{runtime_binding.binding_sha256}-baseline.json"
        env = {"CC_SESSION_FILE": str(self.transcript), "CC_SESSION_ID": SESSION,
               "PULSE_CC_USAGE_CAPTURE_PATH": str(output),
               "PULSE_RUNTIME_USAGE_BINDING": json.dumps(runtime_binding.to_dict())}
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(stdout):
            self.assertEqual(main(["cc-session-probe", "--measurement-role", "baseline"]), 0)
        captured = json.loads(stdout.getvalue())
        self.assertEqual(captured["schema"], "cc_atomic_capture/v1")
        self.assertEqual(captured["snapshot"]["runtime_binding_sha256"], runtime_binding.binding_sha256)
        pi_binding = build_runtime_usage_binding(
            work_item_id=WI, agent_id="pulse", phase="implementation", epoch=1, runtime="pi",
            procurement_source="chatgpt_subscription", authority_ref=cc_authority_ref(SESSION),
            execution_surface=SURFACE)
        env["PULSE_RUNTIME_USAGE_BINDING"] = json.dumps(pi_binding.to_dict())
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["cc-session-probe", "--measurement-role", "baseline"]), 1)


class CcNativeAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "projects"
        self.project = self.root / "-ws"
        self.project.mkdir(parents=True)
        self.transcript = self.project / f"{SESSION}.jsonl"
        self.transcript.write_text(_assistant("msg_old", "a0", "2026-10-01T00:00:01Z", output=7))
        self.binding = _binding(runtime_binding_sha256="b" * 64)
        folder = self.project / "compute-cost"
        self.baseline = capture_cc_session_snapshot(self.transcript, SESSION, self.binding,
            output_path=folder / f"{WI}-{'b' * 64}-baseline.json", lifecycle_state="open",
            measurement_role="baseline")["snapshot"]
        with self.transcript.open("a") as stream:
            stream.write(_assistant("msg_new", "a1", "2099-10-01T00:00:04Z", output=20))
        capture_cc_session_snapshot(self.transcript, SESSION, self.binding,
            output_path=folder / f"{WI}-{'b' * 64}-sealed.json", lifecycle_state="sealed",
            measurement_role="final", baseline_snapshot=self.baseline)

    def _host_facts(self, wi: str = WI):
        host = Path(self.tmp.name) / "host.jsonl"
        common = {"schema": "pulse_collaboration_host_event/v1", "wi": wi, "executor": "pulse",
                  "responsible": None, "delegator": None, "attempt_ref": "ccf1_" + "1" * 64,
                  "writer_ref": "ccf1_" + "2" * 64, "lost_events": 0}
        rows = [
            {**common, "kind": "execution", "at": "2026-10-01T00:00:03Z", "started_at": "2026-10-01T00:00:03Z",
             "event_ref": "ccf1_" + "3" * 64, "dispatch_ref": "ccf1_" + "4" * 64, "runtime": "cc",
             "assignment_ref": reference("assignment", "b" * 64), "parent_ref": None, "state": "accepted",
             "expected_usage": True, "sequence": 1},
            {**common, "kind": "usage", "at": "2026-10-01T00:00:04Z", "started_at": "2026-10-01T00:00:03Z",
             "event_ref": "ccf1_" + "5" * 64, "runtime": "cc",
             "usage_ref": reference("usage", [SESSION, "msg_new"]), "model": "claude-opus-4-1", "provider": None,
             "usage": {"input_tokens": 10, "cache_read_input_tokens": 100,
                       "cache_creation_input_tokens": 5, "output_tokens": 20},
             "mapping": "cc_message.v1", "granularity": "request",
             "native_scope_ref": reference("native_session", ["cc", SESSION]),
             "coverage_ref": "ccf1_" + "6" * 64, "covers": [], "final": True, "sequence": 2},
        ]
        host.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return read_host(host, "2026-10-01T00:00:06Z")

    def test_host_bound_snapshot_attributes_only_bounded_requests(self) -> None:
        data = self.transcript.read_bytes()
        rows = read_cc(self.transcript, "2026-10-01T00:00:06Z", data=data)
        authorities = _host_authorities(self._host_facts(), "cc")
        attributions = bind_cc_boundaries(self.transcript, data, rows, "2026-10-01T00:00:06Z",
                                          host_authorities=authorities)
        self.assertEqual(len(attributions), 1)
        payload = attributions[0].payload
        self.assertEqual((payload["wi"], payload["executor"]), (WI, "pulse"))
        self.assertEqual(payload["target_ref"], reference("usage", [SESSION, "msg_new"]))
        self.assertEqual(payload["policy"], "scl_verified_native_boundary.v1")

    def test_subagent_requests_are_attributed_within_their_own_file_bounds(self) -> None:
        folder = self.project / "compute-cost"
        (folder / f"{WI}-{'b' * 64}-sealed.json").unlink()
        subagent = self.project / SESSION / "subagents" / "agent-cccc.jsonl"
        subagent.parent.mkdir(parents=True)
        subagent.write_text(_sidechain("msg_sub", "s1", "2099-10-01T00:00:05Z", output=6))
        capture_cc_session_snapshot(self.transcript, SESSION, self.binding,
            output_path=folder / f"{WI}-{'b' * 64}-sealed.json", lifecycle_state="sealed",
            measurement_role="final", baseline_snapshot=self.baseline)
        data = self.transcript.read_bytes()
        rows = read_cc(self.transcript, "2026-10-01T00:00:06Z", data=data)
        rows = read_cc(subagent, "2026-10-01T00:00:06Z", into=rows)
        attributions = bind_cc_boundaries(self.transcript, data, rows, "2026-10-01T00:00:06Z",
                                          host_authorities=_host_authorities(self._host_facts(), "cc"))
        self.assertEqual({a.payload["target_ref"] for a in attributions},
                         {reference("usage", [SESSION, "msg_new"]), reference("usage", [SESSION, "msg_sub"])})
        subagent.write_text(_sidechain("msg_sub", "s1", "2099-10-01T00:00:05Z", output=7))
        with self.assertRaisesRegex(ReceiptError, "prefix no longer matches"):
            bind_cc_boundaries(self.transcript, self.transcript.read_bytes(), rows, "2026-10-01T00:00:06Z",
                               host_authorities=_host_authorities(self._host_facts(), "cc"))

    def test_without_matching_host_authority_nothing_is_attributed(self) -> None:
        data = self.transcript.read_bytes()
        rows = read_cc(self.transcript, "2026-10-01T00:00:06Z", data=data)
        authorities = _host_authorities(self._host_facts(wi="wi_20261001_001"), "cc")
        self.assertEqual(bind_cc_boundaries(self.transcript, data, rows, "2026-10-01T00:00:06Z",
                                            host_authorities=authorities), [])

    def _scl_epoch(self, **overrides):
        return {"runtime": "cc", "agent_id": "pulse", "execution_epoch": 1, "phase": "implementation",
                "cc_usage": "unavailable", **overrides}

    def test_wi_discovery_reads_bound_session_intervals_only_with_scl_authority(self) -> None:
        window_end = datetime(2100, 1, 1, tzinfo=timezone.utc)
        intervals = _cc_binding_intervals(self.root, WI, scl_epochs=[self._scl_epoch()], window_end=window_end)
        self.assertEqual(len(intervals), 1)
        [(start, end)] = next(iter(intervals.values()))
        self.assertLess(end, window_end)  # sealed final bounds the interval
        self.assertLessEqual(start, end)
        # 没有 SCL 在 Host Session 授权下接受的同一 epoch：不凭本地文件归属。
        self.assertEqual(_cc_binding_intervals(self.root, WI, scl_epochs=[], window_end=window_end), {})
        self.assertEqual(_cc_binding_intervals(self.root, WI, scl_epochs=[self._scl_epoch(agent_id="other")],
                                               window_end=window_end), {})
        self.assertEqual(_cc_binding_intervals(self.root, WI, scl_epochs=[self._scl_epoch(cc_usage={
            "runtime_binding_sha256": "c" * 64, "baseline_snapshot_sha256": self.baseline["snapshot_sha256"]})],
            window_end=window_end), {})
        self.assertEqual(_cc_binding_intervals(self.root, "wi_20261001_001", scl_epochs=[self._scl_epoch()],
                                               window_end=window_end), {})

    def test_open_interval_is_capped_by_next_binding_or_window_end(self) -> None:
        folder = self.project / "compute-cost"
        (folder / f"{WI}-{'b' * 64}-sealed.json").unlink()
        window_end = datetime(2100, 1, 1, tzinfo=timezone.utc)
        [(start, end)] = next(iter(_cc_binding_intervals(
            self.root, WI, scl_epochs=[self._scl_epoch()], window_end=window_end).values()))
        self.assertEqual(end, window_end)
        other = _binding(work_item_id="wi_20261001_001", runtime_binding_sha256="d" * 64)
        later = capture_cc_session_snapshot(self.transcript, SESSION, other,
            output_path=folder / f"wi_20261001_001-{'d' * 64}-baseline.json", lifecycle_state="open",
            measurement_role="baseline")["snapshot"]
        [(_, capped)] = next(iter(_cc_binding_intervals(
            self.root, WI, scl_epochs=[self._scl_epoch()], window_end=window_end).values()))
        self.assertEqual(capped, datetime.fromisoformat(later["generated_at"].replace("Z", "+00:00")))
        self.assertLess(capped, window_end)

if __name__ == "__main__":
    unittest.main()
