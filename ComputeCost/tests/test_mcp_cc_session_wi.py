"""CC session WI: the transcript's own sessionId binds one Agent declaration."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from pulse_compute_cost import mcp_request_purpose as m
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import REGISTRY_SCHEMA, collect_registered_sources
from pulse_compute_cost.receipts import current_receipts, reference

SESSION = "00000000-0000-4000-8000-0000000000c1"
OTHER = "00000000-0000-4000-8000-0000000000c2"
WI = "wi_20260101_101"
OTHER_WI = "wi_20260101_102"
BEFORE = "2026-01-01T00:00:00Z"
AFTER = "2026-01-01T02:00:00Z"
CATALOG = {"data": [{"id": "openai/test", "pricing": {
    "prompt": "1", "completion": "1", "input_cache_read": "0.1", "input_cache_write": "2"}}]}


def call(wi=WI, meta=None):
    params = {"name": m.TOOL, "arguments": {"wi": wi}}
    if meta is not None:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}


def usage(session, minute, ident, *, sidechain=False):
    return {"type": "assistant", "sessionId": session, "isSidechain": sidechain,
            "uuid": f"u-{ident}", "timestamp": f"2026-01-01T01:{minute:02}:00Z",
            "message": {"id": f"msg-{ident}", "model": "openai/test", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 100, "output_tokens": 10,
                                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}


def declaration(session, minute, call_id, wi=WI, *, sidechain=False, ack=None, error=False):
    used = f"2026-01-01T01:{minute:02}:30Z"
    # Historical session-wide protocol remains covered independently of the
    # new native request-qualified CC server protocol.
    result = {"isError": False, "content": [{"type": "text", "text": json.dumps(m.cc_transport(wi))}]}
    assert not result["isError"], result
    use = {"type": "assistant", "sessionId": session, "isSidechain": sidechain,
           "uuid": f"use-{call_id}", "timestamp": used,
           "message": {"id": f"msg-use-{call_id}", "model": "openai/test",
                       "content": [{"type": "tool_use", "id": call_id, "name": m.CC_TOOL,
                                    "input": {"wi": wi}}],
                       "usage": {"input_tokens": 1, "output_tokens": 1}}}
    block = {"type": "tool_result", "tool_use_id": call_id,
             "content": ack if ack is not None else result["content"]}
    if error:
        block["is_error"] = True
    done = {"type": "user", "sessionId": session, "isSidechain": sidechain,
            "uuid": f"result-{call_id}", "timestamp": used,
            "sourceToolAssistantUUID": use["uuid"],
            "message": {"role": "user", "content": [block]}}
    return [use, done]


class ServerTests(unittest.TestCase):
    def test_cc_ack_needs_no_codex_metadata_and_carries_no_scope(self):
        for meta in (None, {"claudecode/toolUseId": "toolu_1"}):
            result = m.handle(call(meta=meta), clock=lambda: BEFORE, host="cc")
            self.assertFalse(result["isError"], result)
            self.assertEqual(result["structuredContent"],
                             {"schema": "cc_request_wi/v1", "wi": WI, "origin": "agent_declaration"})

    def test_cc_mode_rejects_codex_identity_and_extra_arguments(self):
        codex = {"x-codex-turn-metadata": {"session_id": SESSION, "thread_id": SESSION}}
        self.assertEqual(m.handle(call(meta=codex), clock=lambda: BEFORE, host="cc")
                         ["structuredContent"]["reason"], "host_request_identity_conflict")
        bad = call()
        bad["params"]["arguments"]["session"] = SESSION
        self.assertTrue(m.handle(bad, clock=lambda: BEFORE, host="cc")["isError"])

    def test_codex_default_still_requires_host_metadata(self):
        result = m.handle(call(), clock=lambda: BEFORE)
        self.assertEqual(result["structuredContent"]["reason"], "host_request_metadata_missing")

    def test_real_stdio_cc_host_flag(self):
        script = Path(__file__).resolve().parents[1] / "runtime/codex-mcp-request-purpose.py"
        out = subprocess.run([sys.executable, str(script), "--host", "cc"],
                             input=json.dumps(call()) + "\n", text=True,
                             capture_output=True, check=True, timeout=15)
        self.assertEqual(json.loads(out.stdout)["result"]["structuredContent"]["schema"],
                         "cc_request_wi/v1")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = self.root / "projects/-workspace"
        self.project.mkdir(parents=True)
        self.ledger = UsageLedger(self.root / "ledger")
        self.source = {"id": "cc", "kind": "cc", "path": str(self.root / "projects"),
                       "session_wi_mcp_since": BEFORE}
        self.registry = {"schema": REGISTRY_SCHEMA, "sources": [self.source]}
        self.main = [usage(SESSION, 0, "a"), usage(SESSION, 5, "b")]
        self.agent = [usage(SESSION, 2, "s1", sidechain=True)]
        self.other = [usage(OTHER, 1, "o")]

    def write(self):
        def dump(path, rows):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        dump(self.project / f"{SESSION}.jsonl", self.main)
        dump(self.project / SESSION / "subagents/agent-a1.jsonl", self.agent)
        dump(self.project / f"{OTHER}.jsonl", self.other)

    def collect(self):
        self.write()
        return collect_registered_sources(self.registry, self.ledger)

    def claims(self):
        active, _ = current_receipts(self.ledger.receipt_snapshot()["facts"])
        usages = {r.entity_ref: r for r in active if r.kind == "usage"}
        return {usages[r.payload["target_ref"]].payload["native_scope_ref"] + "|"
                + r.payload["target_ref"]: (r.payload["state"], r.payload["wi"])
                for r in active if r.kind == "attribution" and r.producer == m.POLICY}

    def scope_states(self):
        states = {}
        for key, value in self.claims().items():
            states.setdefault(key.split("|")[0], []).append(value)
        return states

    def test_mid_session_declaration_covers_prior_later_and_subagent_usage(self):
        self.main[1:1] = declaration(SESSION, 3, "toolu_1")
        result = self.collect()
        scope = reference("native_session", ["cc", SESSION])
        self.assertEqual(self.scope_states(), {scope: [("assigned", WI)] * 4})
        self.assertEqual(result["sources"][0]["reason"], "execution_inventory_not_observed")
        self.assertEqual(collect_registered_sources(self.registry, self.ledger)["inserted"], 0)
        q = query_collaboration(self.ledger, CollaborationQuery(
            from_date="2026-01-01", to_date="2026-01-02", scope="activity", timezone="UTC",
            group_by=("wi",)), catalog=CATALOG)
        groups = {g["key"]["wi"]: g["usage"]["event_count"] for g in q["groups"]}
        self.assertEqual(groups, {WI: 4, "unassigned": 1})

    def test_source_not_enabled_or_later_cutoff_admits_nothing(self):
        self.main[1:1] = declaration(SESSION, 3, "toolu_1")
        del self.source["session_wi_mcp_since"]
        self.collect()
        self.assertEqual(self.claims(), {})
        self.source["session_wi_mcp_since"] = AFTER
        self.collect()
        self.assertEqual(self.claims(), {})

    def test_repeat_is_idempotent_and_contrary_declaration_is_ambiguous(self):
        self.main[1:1] = declaration(SESSION, 3, "toolu_1") + declaration(SESSION, 4, "toolu_2")
        self.collect()
        self.assertEqual({v for v in self.claims().values()}, {("assigned", WI)})
        self.main[1:1] = declaration(SESSION, 4, "toolu_3", OTHER_WI)
        self.collect()
        self.assertEqual({v for v in self.claims().values()}, {("ambiguous", None)})

    def test_result_mismatch_is_diagnostic_not_assignment(self):
        wrong = [{"type": "text", "text": json.dumps(m.cc_transport(OTHER_WI))}]
        self.main = [usage(SESSION, 0, "a"), *declaration(SESSION, 3, "toolu_1", ack=wrong)]
        result = self.collect()
        self.assertEqual(result["sources"][0]["reason"], "native_mcp_result_mismatch")
        self.assertEqual(self.claims(), {})

    def test_refused_call_is_counted_not_a_source_failure(self):
        # The server rejected wrong arguments: nothing was declared, and the
        # source's usage is complete. Count it without degrading the source.
        self.main = [usage(SESSION, 0, "a"), *declaration(SESSION, 3, "toolu_1", error=True)]
        result = self.collect()
        self.assertEqual(result["sources"][0]["diagnostics"], {"native_mcp_call_refused": 1})
        self.assertEqual(result["sources"][0]["reason"], "execution_inventory_not_observed")
        self.assertEqual(self.claims(), {})

    def test_subagent_call_cannot_claim_parent_session(self):
        self.agent += declaration(SESSION, 3, "toolu_s", sidechain=True)
        result = self.collect()
        # Counted, but a subagent needs no declaration: the source is not degraded.
        self.assertEqual(result["sources"][0]["diagnostics"], {"native_mcp_sidechain_unbound": 1})
        self.assertEqual(result["sources"][0]["reason"], "execution_inventory_not_observed")
        self.assertEqual(self.claims(), {})

    def test_concurrent_session_copy_and_unfinished_call_are_not_declarations(self):
        self.other += declaration(SESSION, 3, "toolu_copy")
        self.main += declaration(SESSION, 6, "toolu_open")[:1]
        result = self.collect()
        self.assertEqual(self.claims(), {})
        self.assertEqual(result["sources"][0]["reason"], "execution_inventory_not_observed")
        self.main[1:1] = declaration(SESSION, 3, "toolu_1")
        self.collect()
        other = reference("native_session", ["cc", OTHER])
        self.assertNotIn(other, self.scope_states())


if __name__ == "__main__":
    unittest.main()
