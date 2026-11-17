from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pulse_compute_cost.cli import main as cli_main
from pulse_compute_cost.pi_usage import (
    PiUsageBinding,
    PiUsageError,
    collect_pi_session_usage,
)
from pulse_compute_cost.retrospective_usage import discover_relay_pi_retrospective


WI = "wi_20260905_383"
OTHER_WI = "wi_20260906_779"
RAW_USER = "fixture-private-tenant"
RAW_AGENT = "fixture-private-agent"
RAW_SESSION = "11111111-2222-4333-8444-555555555555"
RAW_CHAT = "998877665544"
RAW_TOKEN = "fixture-private-bot-credential"


def write_jsonl(path: Path, rows: list[dict[str, object]], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    path.chmod(mode)


def pi_assistant(
    entry_id: str,
    timestamp: str,
    *,
    input_tokens: int,
    cache_read: int,
    output: int,
    reasoning: int | None = None,
) -> dict[str, object]:
    return {
        "type": "message",
        "id": entry_id,
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "provider": "fixture-provider",
            "model": "fixture-model",
            "content": [{"type": "text", "text": "synthetic-only"}],
            "usage": {
                "input": input_tokens,
                "output": output,
                "cacheRead": cache_read,
                "cacheWrite": 0,
                "reasoning": output // 2 if reasoning is None else reasoning,
                "totalTokens": input_tokens + cache_read + output,
            },
        },
    }


def pi_compaction(entry_id: str, timestamp: str) -> dict[str, object]:
    return {
        "type": "compaction",
        "id": entry_id,
        "timestamp": timestamp,
        "usage": {
            "input": 9,
            "output": 1,
            "cacheRead": 0,
            "cacheWrite": 0,
            "reasoning": 0,
            "totalTokens": 10,
        },
    }


def metering(
    timestamp: str,
    *,
    tenant: str = f"{RAW_USER}:{RAW_AGENT}",
    session: str = RAW_SESSION,
    input_tokens: int,
    cache_read: int,
    output: int,
) -> dict[str, object]:
    return {
        "ts": timestamp,
        "tenant": tenant,
        "session_id": session,
        "model": "fixture-model",
        "input_tokens": input_tokens,
        "output_tokens": output,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cache_read,
    }


class RetrospectiveRelayPiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.harness = self.root / ".harness"
        (self.harness / "work-items").mkdir(parents=True)
        (self.harness / "events").mkdir()
        (self.harness / "work-items" / f"{WI}.yaml").write_text(
            f"work_item_id: {WI}\n"
            "status: cancelled\n"
            "owner: pulse\n"
            "created_at: '2026-09-05T00:00:00Z'\n"
            "updated_at: '2026-09-05T10:00:00Z'\n"
            "artifact_refs:\n"
            f"- .harness/evidence/outcomes/{WI}.md\n"
            f"- .harness/evidence/outcomes/{WI}/verification-fixture.md\n",
            encoding="utf-8",
        )
        # Two distinct active intervals and a cancelled terminal transition.
        (self.harness / "events" / f"{WI}.events.yaml").write_text(
            f"""---
event_id: ev_created
event_type: work_item.created
operator: pulse
payload:
  title: synthetic
  fixture: true
timestamp: '2026-09-05T00:00:00Z'
work_item_id: {WI}
---
event_id: ev_active_1
event_type: work_item.in_progress
operator: pulse
payload:
  from_status: ready
  to_status: in_progress
timestamp: '2026-09-05T01:00:00Z'
work_item_id: {WI}
---
event_id: ev_verify
event_type: work_item.awaiting_verification
operator: pulse
payload:
  from_status: in_progress
  to_status: awaiting_verification
timestamp: '2026-09-05T02:00:00Z'
work_item_id: {WI}
---
event_id: ev_active_2
event_type: work_item.in_progress
operator: pulse
payload:
  from_status: awaiting_verification
  to_status: in_progress
timestamp: '2026-09-05T05:00:00Z'
work_item_id: {WI}
---
event_id: ev_cancelled
event_type: work_item.cancelled
operator: pulse
payload:
  from_status: in_progress
  to_status: cancelled
timestamp: '2026-09-05T10:00:00Z'
work_item_id: {WI}
""",
            encoding="utf-8",
        )
        # Inventory entry makes Codex short aliases deterministic without adding
        # any model-text attribution evidence.
        (self.harness / "work-items" / f"{OTHER_WI}.yaml").write_text(
            f"work_item_id: {OTHER_WI}\nstatus: completed\n"
            "owner: pulse\ncreated_at: '2026-09-06T00:00:00Z'\n"
            "updated_at: '2026-09-06T01:00:00Z'\n",
            encoding="utf-8",
        )

        self.relay = self.root / "relay"
        self.config = self.relay / "relay.json"
        self.state = self.relay / "state.json"
        self.manifest = self.relay / "pi-runtime-manifest.json"
        self.metering_dir = self.relay / "metering"
        self.session_root = self.relay / "pi-sessions"
        self.codex_home = self.root / "codex"
        self.codex_home.mkdir()
        self.relay.mkdir()
        self._write_registration()
        self.session_dir = self._registered_session_dir()
        self._write_sources()

    def _config_value(self, *, second_mapping: bool = False) -> dict[str, object]:
        users: list[dict[str, object]] = [{
            "userId": RAW_USER,
            "chatId": RAW_CHAT,
            "defaultAgent": RAW_AGENT,
            "agents": [{
                "id": RAW_AGENT,
                "harnessAgentId": "pulse",
                "name": "Synthetic",
                "emoji": "S",
                "botToken": RAW_TOKEN,
                "workspace": str(self.root / "workspace"),
                "sessionId": RAW_SESSION,
                "runtime": "pi",
                "conversationRuntime": {
                    "runtime": "pi",
                    "model": {
                        "provider": "fixture-provider",
                        "model": "fixture-model",
                        "reasoning": "high",
                    },
                    "isolationProfile": "fixture-profile",
                },
            }],
        }]
        if second_mapping:
            users.append({
                "userId": "second-private-tenant",
                "chatId": "second-private-chat",
                "defaultAgent": "second-private-agent",
                "agents": [{
                    "id": "second-private-agent",
                    "harnessAgentId": "pulse",
                    "name": "Second",
                    "emoji": "S",
                    "botToken": "second-private-token",
                    "workspace": str(self.root / "second-workspace"),
                    "sessionId": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                    "runtime": "pi",
                    "conversationRuntime": {
                        "runtime": "pi",
                        "model": {
                            "provider": "fixture-provider",
                            "model": "fixture-model",
                            "reasoning": "high",
                        },
                        "isolationProfile": "fixture-profile",
                    },
                }],
            })
        return {"users": users}

    def _state_value(self, *, second_mapping: bool = False) -> dict[str, object]:
        pool_key = f"{RAW_USER}:{RAW_AGENT}"
        sessions: dict[str, object] = {
            pool_key: {
                "poolKey": pool_key,
                "agentId": RAW_AGENT,
                "runtime": "pi",
                "sessionId": RAW_SESSION,
                "turnCount": 8,
                "maxTurns": 1000,
                "lastActivity": 1,
                "status": "active",
                # Reused session is now bound to another WI; this must not turn
                # the historical window estimate into per-turn attribution.
                "activeWorkItemRuntimeBinding": {
                    "schema": "pulse_active_work_item_runtime_binding/v1",
                    "workItemId": OTHER_WI,
                },
            }
        }
        if second_mapping:
            second_key = "second-private-tenant:second-private-agent"
            sessions[second_key] = {
                "poolKey": second_key,
                "agentId": "second-private-agent",
                "runtime": "pi",
                "sessionId": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                "turnCount": 1,
                "maxTurns": 1000,
                "lastActivity": 1,
                "status": "active",
            }
        return {
            "version": 1,
            "updatedAt": "2026-09-06T00:00:00Z",
            "sessions": sessions,
        }

    def _write_registration(self, *, second_mapping: bool = False) -> None:
        self.config.write_text(json.dumps(self._config_value(second_mapping=second_mapping)), encoding="utf-8")
        self.state.write_text(json.dumps(self._state_value(second_mapping=second_mapping)), encoding="utf-8")
        self.manifest.write_text(json.dumps({
            "schemaVersion": "relay_pi_runtime_manifest/v1",
            "profiles": {"fixture-profile": {"sessionRoot": str(self.session_root)}},
        }), encoding="utf-8")
        for path in (self.config, self.state, self.manifest):
            path.chmod(0o600)

    def _registered_session_dir(self) -> Path:
        import hashlib
        import re

        pool_key = f"{RAW_USER}:{RAW_AGENT}"
        prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", pool_key)[:48]
        return self.session_root / f"{prefix}-{hashlib.sha256(pool_key.encode()).hexdigest()[:12]}"

    def _write_sources(self) -> None:
        header = {"type": "session", "id": RAW_SESSION, "timestamp": "2026-09-04T20:00:00Z"}
        write_jsonl(self.session_dir / f"first_{RAW_SESSION}.jsonl", [
            header,
            pi_assistant("pre", "2026-09-04T23:59:00Z", input_tokens=900, cache_read=0, output=90),
            pi_assistant("active-one", "2026-09-05T01:30:00Z", input_tokens=10, cache_read=100, output=2),
            pi_assistant("verification-gap", "2026-09-05T03:00:00Z", input_tokens=20, cache_read=200, output=3),
            pi_compaction("compaction", "2026-09-05T05:15:00Z"),
        ], mode=0o644)
        write_jsonl(self.session_dir / f"second_{RAW_SESSION}.jsonl", [
            header,
            pi_assistant("active-two", "2026-09-05T05:30:00Z", input_tokens=30, cache_read=300, output=4),
            pi_assistant("post", "2026-09-05T10:01:00Z", input_tokens=800, cache_read=0, output=80),
        ], mode=0o644)
        write_jsonl(self.metering_dir / "2026-09-05.jsonl", [
            metering("2026-09-04T23:59:00Z", input_tokens=900, cache_read=0, output=90),
            metering("2026-09-05T01:30:00Z", input_tokens=11, cache_read=100, output=2),
            metering("2026-09-05T03:00:00Z", input_tokens=20, cache_read=200, output=3),
            metering("2026-09-05T05:30:00Z", input_tokens=30, cache_read=300, output=5),
            metering("2026-09-05T10:01:00Z", input_tokens=800, cache_read=0, output=80),
            metering(
                "2026-09-05T05:30:00Z",
                tenant="unrelated-private-tenant",
                session="unrelated-private-session",
                input_tokens=999,
                cache_read=0,
                output=999,
            ),
        ])

    def discover(self) -> dict[str, object]:
        return discover_relay_pi_retrospective(
            work_item_id=WI,
            harness_root=self.harness,
            relay_config_path=self.config,
            relay_state_path=self.state,
            relay_pi_manifest_path=self.manifest,
            relay_metering_dir=self.metering_dir,
        )

    def test_reused_session_is_windowed_but_never_canonical(self) -> None:
        candidate = self.discover()

        self.assertEqual(candidate["status"], "available")
        self.assertEqual(candidate["attribution"]["mapping"], "unique")
        self.assertFalse(candidate["attribution"]["model_text_searched"])
        self.assertEqual(candidate["lifecycle"]["status"], "cancelled")
        self.assertEqual(candidate["lifecycle"]["active_interval_count"], 2)
        self.assertEqual(
            candidate["lifecycle"]["evidence_availability"]["status"],
            "registered_refs_only",
        )
        broad = candidate["windows"]["created_to_terminal"]
        active = candidate["windows"]["lifecycle_active"]
        self.assertEqual(broad["pi_jsonl"]["event_count"], 3)
        self.assertEqual(active["pi_jsonl"]["event_count"], 2)
        self.assertEqual(broad["pi_jsonl"]["tokens"]["total_tokens"], 669)
        self.assertEqual(active["pi_jsonl"]["tokens"]["total_tokens"], 446)
        self.assertEqual(broad["relay_metering"]["event_count"], 3)
        self.assertEqual(active["relay_metering"]["event_count"], 2)
        self.assertEqual(
            broad["relay_metering"]["tokens"]["reasoning"]["status"],
            "unavailable",
        )
        self.assertEqual(broad["cross_check"]["status"], "corroborated_with_divergence")
        self.assertEqual(
            broad["pi_jsonl"]["excluded_non_outer_turn_usage"]["event_count"], 1
        )
        self.assertTrue(all(ref.startswith("source_v1_") for ref in candidate["source_refs"]))
        self.assertTrue(all(len(ref) == 74 for ref in candidate["source_refs"]))

    def test_post_terminal_invalid_usage_does_not_poison_historical_window(self) -> None:
        path = self.session_dir / f"second_{RAW_SESSION}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    pi_assistant(
                        "later-provider-defect",
                        "2026-09-05T11:00:00Z",
                        input_tokens=10,
                        cache_read=0,
                        output=1,
                        reasoning=2,
                    ),
                    separators=(",", ":"),
                )
                + "\n"
            )

        candidate = self.discover()

        self.assertEqual(candidate["status"], "available")
        self.assertEqual(
            candidate["windows"]["created_to_terminal"]["pi_jsonl"]["tokens"][
                "total_tokens"
            ],
            669,
        )
        with self.assertRaisesRegex(PiUsageError, "reasoning cannot exceed output"):
            collect_pi_session_usage(
                path,
                PiUsageBinding(
                    work_item_id="retrospective_unattributed",
                    phase="retrospective",
                    epoch=1,
                    procurement_source="unknown",
                ),
            )

    def test_in_window_invalid_usage_still_fails_closed(self) -> None:
        path = self.session_dir / f"second_{RAW_SESSION}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    pi_assistant(
                        "in-window-provider-defect",
                        "2026-09-05T09:00:00Z",
                        input_tokens=10,
                        cache_read=0,
                        output=1,
                        reasoning=2,
                    ),
                    separators=(",", ":"),
                )
                + "\n"
            )

        candidate = self.discover()

        self.assertEqual(candidate["status"], "partial")
        self.assertEqual(
            candidate["windows"]["created_to_terminal"]["pi_jsonl"]["status"],
            "source_unreadable",
        )
        self.assertIn("relay_pi_source_unreadable", candidate["diagnostics"])

    def test_report_surfaces_candidate_without_tokens_cost_or_roi(self) -> None:
        output = io.StringIO()
        with patch.dict(os.environ, {
            "RELAY_CONFIG_PATH": str(self.config),
            "RELAY_PI_RUNTIME_MANIFEST_PATH": str(self.manifest),
            "METERING_DIR": str(self.metering_dir),
        }), redirect_stdout(output):
            result = cli_main([
                "wi-report", WI,
                "--harness-root", str(self.harness),
                "--codex-home", str(self.codex_home),
                "--machine", "fixture-machine",
                "--no-auto-price",
            ])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["lifecycle"]["status"], "cancelled")
        self.assertEqual(report["coverage"]["retrospective_usage"], "available")
        self.assertEqual(report["coverage"]["pi_usage"], "retrospective_unsealed")
        self.assertEqual(report["usage"]["pi_native"]["status"], "retrospective_unsealed_candidate")
        self.assertEqual(report["usage"]["tokens"], "unavailable")
        self.assertEqual(report["usage"]["conservation"], "unavailable")
        self.assertEqual(report["cost"]["token_resource"], "unavailable")
        self.assertEqual(report["cost"]["openrouter_cash_valuation"], "not_applicable")
        attention = report["cost"]["human_attention"]
        self.assertEqual(attention["status"], "partial")
        self.assertEqual(attention["cycle_elapsed_seconds"], 36_000)
        self.assertEqual(attention["focused_seconds"], "unavailable")
        self.assertEqual(attention["exact_human_feedback_count"], "unavailable")
        self.assertEqual(attention["category_counts"]["review_approval"], 0)
        self.assertEqual(
            report["outcome"]["measurement_readiness"]["final_token_seal"],
            "unavailable",
        )
        native = report["usage"]["pi_native"]
        self.assertFalse(native["canonical_usage_eligible"])
        self.assertFalse(native["token_seal_eligible"])
        self.assertFalse(native["cost_eligible"])
        self.assertFalse(native["roi_eligible"])
        self.assertIn("no_canonical_usage_producer", report["warnings"])
        self.assertNotIn("no_joinable_usage_producer", report["warnings"])

        rendered = json.dumps(report)
        for private in (
            RAW_USER, RAW_AGENT, RAW_SESSION, RAW_CHAT, RAW_TOKEN,
            str(self.root), "synthetic-only", "unrelated-private-session",
        ):
            self.assertNotIn(private, rendered)

    def test_retrospective_pi_estimate_uses_flat_standard_rates_but_not_canonical_total(self) -> None:
        models = self.root / "openrouter-models.json"
        models.write_text(json.dumps({"data": [{
            "id": "fixture-model",
            "canonical_slug": "fixture/model-standard-snapshot",
            "pricing": {
                "prompt": "0.000002",
                "input_cache_read": "0.0000002",
                "input_cache_write": "0.0000025",
                "completion": "0.00001",
                "overrides": [{
                    "min_prompt_tokens": 1,
                    "prompt": "9",
                    "input_cache_read": "9",
                    "input_cache_write": "9",
                    "completion": "9",
                }],
            },
        }]}), encoding="utf-8")
        output = io.StringIO()
        with patch.dict(os.environ, {
            "RELAY_CONFIG_PATH": str(self.config),
            "RELAY_PI_RUNTIME_MANIFEST_PATH": str(self.manifest),
            "METERING_DIR": str(self.metering_dir),
        }), redirect_stdout(output):
            result = cli_main([
                "wi-report", WI,
                "--harness-root", str(self.harness),
                "--codex-home", str(self.codex_home),
                "--machine", "fixture-machine",
                "--openrouter-models-snapshot", str(models),
            ])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        estimate = report["cost"]["retrospective_cash_estimate"]
        self.assertEqual(estimate["status"], "calculated")
        self.assertEqual(estimate["valuation"]["total_usd"], "0.00033")
        self.assertEqual(estimate["valuation"]["partitions"][0]["tier"], "standard")
        self.assertFalse(estimate["canonical_attribution"])
        self.assertFalse(estimate["canonical_total_eligible"])
        self.assertTrue(estimate["may_include_unrelated_turns"])
        self.assertTrue(estimate["noncanonical_total"])
        # Retrospective Pi estimate is visible machine cash under the policy,
        # but never the canonical WI total.
        self.assertEqual(report["cost"]["retrospective_machine_cash_usd"], "0.00033")
        self.assertEqual(report["cost"]["retrospective_cash_coverage"], "calculated")
        self.assertEqual(report["cost"]["machine_cash_usd"], "unavailable")
        self.assertEqual(report["usage"]["tokens"], "unavailable")
        self.assertEqual(report["cost"]["openrouter_cash_valuation"], "not_applicable")
        self.assertEqual(
            report["cost"]["subscription_settlement"],
            "excluded_from_wi_report_and_roi",
        )

    def test_0644_sources_are_reported_not_mutated(self) -> None:
        before = [path.stat().st_mode & 0o777 for path in self.session_dir.glob("*.jsonl")]
        candidate = self.discover()
        after = [path.stat().st_mode & 0o777 for path in self.session_dir.glob("*.jsonl")]

        self.assertEqual(before, [0o644, 0o644])
        self.assertEqual(after, before)
        self.assertEqual(candidate["source_permissions"]["status"], "privacy_defect")
        pi_permissions = candidate["source_permissions"]["by_source"]["pi_jsonl"]
        self.assertEqual(pi_permissions["observed_modes"], ["0644"])
        self.assertEqual(pi_permissions["unsafe_file_count"], 2)
        self.assertIn(
            "historical_source_permissions_privacy_defect",
            candidate["diagnostics"],
        )

    def test_ambiguous_agent_tenant_session_mapping_fails_closed(self) -> None:
        self._write_registration(second_mapping=True)

        candidate = self.discover()

        self.assertEqual(candidate["status"], "ambiguous_mapping")
        self.assertEqual(candidate["attribution"]["mapping"], "ambiguous")
        for window in candidate["windows"].values():
            self.assertEqual(window["pi_jsonl"]["event_count"], "unavailable")
            self.assertEqual(window["relay_metering"]["tokens"]["total_tokens"], "unavailable")
        self.assertIn("relay_pi_mapping_ambiguous", candidate["diagnostics"])
        attention = candidate["human_attention"]
        self.assertEqual(attention["cycle_elapsed_seconds"], 36_000)
        self.assertEqual(attention["category_counts"]["review_approval"], 0)
        self.assertEqual(attention["exact_human_feedback_count"], "unavailable")
        self.assertEqual(attention["relay_turn_binding"], "ambiguous")

    def test_config_state_session_mismatch_is_ambiguous_not_missing(self) -> None:
        state = self._state_value()
        pool_key = f"{RAW_USER}:{RAW_AGENT}"
        state["sessions"][pool_key]["sessionId"] = "different-private-session"
        self.state.write_text(json.dumps(state), encoding="utf-8")
        self.state.chmod(0o600)

        candidate = self.discover()

        self.assertEqual(candidate["status"], "ambiguous_mapping")
        self.assertEqual(candidate["attribution"]["mapping"], "ambiguous")
        self.assertIn("relay_pi_mapping_ambiguous", candidate["diagnostics"])

    def test_missing_metering_counter_is_unknown_not_zero(self) -> None:
        path = self.metering_dir / "2026-09-05.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        del rows[1]["output_tokens"]
        write_jsonl(path, rows)

        candidate = self.discover()
        metering_source = candidate["windows"]["created_to_terminal"]["relay_metering"]

        self.assertEqual(candidate["status"], "partial")
        self.assertEqual(metering_source["status"], "partial")
        self.assertEqual(metering_source["tokens"]["output"], "unavailable")
        self.assertEqual(metering_source["tokens"]["total_tokens"], "unavailable")
        self.assertIn(
            "relay_metering_resource_fields_unavailable", candidate["diagnostics"]
        )

    def test_missing_registered_usage_sources_preserve_unknown(self) -> None:
        for path in self.session_dir.glob("*.jsonl"):
            path.unlink()
        for path in self.metering_dir.glob("*.jsonl"):
            path.unlink()

        candidate = self.discover()

        self.assertEqual(candidate["status"], "source_missing")
        for window in candidate["windows"].values():
            self.assertEqual(window["pi_jsonl"]["event_count"], "unavailable")
            self.assertEqual(window["pi_jsonl"]["tokens"]["input"], "unavailable")
            self.assertEqual(window["relay_metering"]["tokens"]["total_tokens"], "unavailable")
            self.assertEqual(window["cross_check"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
