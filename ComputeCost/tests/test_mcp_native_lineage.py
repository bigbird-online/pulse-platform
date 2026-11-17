"""Native MCP owner isolation reuses the already verified child-history fixtures."""

from datetime import datetime
import unittest
import test_codex_request_purpose as native_fixture
import test_codex_replayed_owner as replay_fixture
from pulse_compute_cost import mcp_request_purpose as m
from pulse_compute_cost.receipt_sources import read_codex

END = "2026-09-26T01:10:00Z"
WI = "wi_20260825_300"


class NativeMcpLineageTests(unittest.TestCase):
    setUp = native_fixture.NativeRootOwnerTests.setUp
    uuid = native_fixture.NativeRootOwnerTests.uuid
    write = native_fixture.NativeRootOwnerTests.write
    target = native_fixture.NativeRootOwnerTests.target
    rows = replay_fixture.ReplayedContextOwnerTests.rows
    save = replay_fixture.ReplayedContextOwnerTests.save
    owner = replay_fixture.ReplayedContextOwnerTests.owner
    replay = replay_fixture.ReplayedContextOwnerTests.replay

    def add_statement(self, index=2, wi=WI):
        thread = (self.root, self.middle, self.leaf)[index]
        turn = (self.root_turn, self.middle_turn, self.leaf_turn)[index]
        started = f"2026-09-26T01:0{index}:00Z"
        captured = f"2026-09-26T01:0{index}:01Z"
        meta = {
            "sessionId": self.root,
            "threadId": thread,
            "callId": f"call-{index}",
            "itemId": f"item-{index}",
            "x-codex-turn-metadata": {
                "session_id": self.root,
                "thread_id": thread,
                "turn_id": turn,
                "turn_started_at_unix_ms": int(
                    datetime.fromisoformat(started).timestamp() * 1000
                ),
            },
        }
        args = {"wi": wi}
        stamp = int(datetime.fromisoformat("2026-09-26T01:04:00Z").timestamp() * 1000)
        result = m.handle(
            {
                "method": "tools/call",
                "params": {"name": m.TOOL, "arguments": args, "_meta": meta},
            },
            clock=lambda: "2026-09-26T01:04:00Z",
        )
        self.assertFalse(result["isError"])
        witness = {
            "type": "event_msg",
            "timestamp": captured,
            "payload": {
                "type": "item_completed",
                "thread_id": thread,
                "turn_id": turn,
                "item": {"type": "UserMessage", "id": f"input-{index}", "content": []},
            },
        }
        call = {
            "type": "event_msg",
            "timestamp": "2026-09-26T01:04:00Z",
            "payload": {
                "type": "item_completed",
                "thread_id": thread,
                "turn_id": turn,
                "item": {
                    "type": "McpToolCall",
                    "id": f"item-{index}",
                    "server": "pulse-request-purpose",
                    "tool": m.TOOL,
                    "arguments": args,
                    "status": "completed",
                    "result": result,
                },
                "started_at_ms": stamp,
                "completed_at_ms": stamp,
            },
        }
        rows = self.rows(self.paths[index])
        rows[-2:-2] = [witness, call]
        self.save(self.paths[index], rows)
        return {"target_ref": self.target(thread, turn)}

    def decode(self, originals, usages=None):
        usages = read_codex(self.directory, END) if usages is None else usages
        diagnostics = []
        bindings = m.native_completions(
            self.paths, usages, diagnostics, since="2026-01-01T00:00:00Z"
        )
        decisions = [
            {**d, "target_ref": u.entity_ref, "purpose": d["wi"]}
            for u in usages
            if u.kind == "usage"
            for d in bindings.get(u.payload.get("native_scope_ref"), [])
        ]
        return decisions, diagnostics

    def test_nested_child_survives_replayed_ancestor_startup(self):
        self.replay()
        original = self.add_statement()
        decisions, diagnostics = self.decode([original])
        self.assertEqual(len(decisions), 1, diagnostics)
        self.assertEqual(
            decisions[0]["target_ref"], self.target(self.leaf, self.leaf_turn)
        )
        self.assertEqual(decisions[0]["purpose"], WI)

    def test_child_survives_byte_identical_parent_prefix(self):
        child = self.rows(self.paths[2])
        child[1:1] = self.rows(self.paths[1])
        self.save(self.paths[2], child)
        original = self.add_statement()
        decisions, diagnostics = self.decode([original])
        self.assertEqual(len(decisions), 1, diagnostics)

    def test_parent_and_child_do_not_exchange_purposes(self):
        self.replay()
        parent = self.add_statement(0, "wi_20260924_219")
        child = self.add_statement(2, WI)
        decisions, diagnostics = self.decode([parent, child])
        self.assertEqual(
            {d["target_ref"]: d["purpose"] for d in decisions},
            {parent["target_ref"]: "wi_20260924_219", child["target_ref"]: WI},
            diagnostics,
        )

    def test_foreign_header_rejects_only_its_native_scope(self):
        parent = self.add_statement(0)
        child = self.add_statement()
        usages = read_codex(self.directory, END)
        rows = self.rows(self.paths[2])
        rows.insert(1, {"type": "session_meta", "payload": {"id": self.uuid(999)}})
        self.save(self.paths[2], rows)
        decisions, diagnostics = self.decode([parent, child], usages)
        self.assertEqual([d["target_ref"] for d in decisions], [parent["target_ref"]])
        self.assertIn("native_mcp_thread_identity_conflict", diagnostics)

    def test_contrary_owner_for_local_turn_is_not_discarded(self):
        original = self.add_statement()
        usages = read_codex(self.directory, END)
        rows = self.rows(self.paths[2])
        rows.insert(-1, self.owner(self.root, self.leaf_turn))
        self.save(self.paths[2], rows)
        decisions, diagnostics = self.decode([original], usages)
        self.assertEqual(decisions, [])
        self.assertIn("native_mcp_owner_conflict", diagnostics)

    def test_missing_ancestor_cannot_excuse_a_foreign_startup(self):
        self.replay()
        original = self.add_statement()
        usages = read_codex(self.directory, END)
        self.paths = [self.paths[0], self.paths[2]]
        decisions, diagnostics = self.decode([original], usages)
        self.assertEqual(decisions, [])
        self.assertIn("native_mcp_thread_identity_conflict", diagnostics)

    def test_explicit_local_root_conflict_is_not_a_replay(self):
        original = self.add_statement()
        usages = read_codex(self.directory, END)
        rows = self.rows(self.paths[2])
        rows[0]["payload"]["session_id"] = self.uuid(999)
        self.save(self.paths[2], rows)
        decisions, diagnostics = self.decode([original], usages)
        self.assertEqual(decisions, [])
        self.assertIn("native_mcp_thread_identity_conflict", diagnostics)

    def test_archived_compatible_copy_does_not_duplicate_a_child_declaration(self):
        self.replay()
        original = self.add_statement()
        archive = self.directory / "archive"
        archive.mkdir()
        copied = archive / self.paths[2].name
        copied.write_bytes(self.paths[2].read_bytes())
        self.paths.append(copied)
        decisions, diagnostics = self.decode([original])
        self.assertEqual(len(decisions), 1, diagnostics)

    def test_late_contrary_owner_after_task_complete_is_still_conflicting(self):
        original = self.add_statement()
        usages = read_codex(self.directory, END)
        rows = self.rows(self.paths[2])
        rows.append(self.owner(self.root, self.leaf_turn))
        self.save(self.paths[2], rows)
        decisions, diagnostics = self.decode([original], usages)
        self.assertEqual(decisions, [])
        self.assertIn("native_mcp_owner_conflict", diagnostics)

    def test_cyclic_ancestry_cannot_justify_ignoring_a_copied_header(self):
        child = self.rows(self.paths[2])
        child[1:1] = self.rows(self.paths[1])
        self.save(self.paths[2], child)
        original = self.add_statement()
        usages = read_codex(self.directory, END)
        root = self.rows(self.paths[0])
        root[0]["payload"]["parent_thread_id"] = self.leaf
        self.save(self.paths[0], root)
        decisions, diagnostics = self.decode([original], usages)
        self.assertEqual(decisions, [])
        self.assertIn("native_mcp_thread_identity_conflict", diagnostics)


if __name__ == "__main__":
    unittest.main()
