"""A replayed ancestor header must not erase a proven local child turn."""
import json
import unittest
from pulse_compute_cost.codex_request_purpose import native_root_requests
from test_codex_request_purpose import NativeRootOwnerTests


class ReplayedContextOwnerTests(unittest.TestCase):
    setUp = NativeRootOwnerTests.setUp
    uuid = NativeRootOwnerTests.uuid
    target = NativeRootOwnerTests.target
    write = NativeRootOwnerTests.write

    def rows(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    def save(self, path, rows):
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))

    def owner(self, thread, turn, timestamp='2026-09-26T01:04:00Z'):
        return {'type': 'event_msg', 'timestamp': timestamp, 'payload': {
            'type': 'item_completed', 'thread_id': thread, 'turn_id': turn,
            'item': {'type': 'AgentMessage', 'id': self.uuid(151)}}}

    def replay(self, *, owner=True):
        middle = self.rows(self.paths[1])
        middle[1]['payload']['root_turn_id'] = self.root_turn
        middle.insert(2, self.owner(self.middle, self.middle_turn, '2026-09-26T01:01:10Z'))
        self.save(self.paths[1], middle)
        # A context replay has its own transport ordinals. Native owner/turn
        # identities remain explicit; byte-prefix equality cannot prove it.
        copied = [{**row, 'ordinal': index+900} for index, row in enumerate(middle[:4])]
        leaf = self.rows(self.paths[2])
        leaf[1:1] = copied
        if owner:
            leaf.insert(-1, self.owner(self.leaf, self.leaf_turn))
        self.save(self.paths[2], leaf)

    def test_recorded_local_owner_survives_ancestor_context(self):
        self.replay()
        links = native_root_requests(self.paths)
        local = self.target(self.leaf, self.leaf_turn)
        self.assertEqual(links[local]['parent'], self.target(self.root, self.root_turn))
        self.assertNotIn(self.target(self.leaf, self.middle_turn), links)
        self.assertGreaterEqual(len(links[local]['evidence']), 5)

    def test_ancestor_header_without_local_owner_is_not_waived(self):
        self.replay(owner=False)
        self.assertNotIn(self.target(self.leaf, self.leaf_turn), native_root_requests(self.paths))

    def test_an_unrelated_header_is_still_a_real_identity_conflict(self):
        self.replay()
        rows = self.rows(self.paths[2])
        rows.insert(1, {'type': 'session_meta', 'payload': {'id': self.uuid(999)}})
        self.save(self.paths[2], rows)
        self.assertNotIn(self.target(self.leaf, self.leaf_turn), native_root_requests(self.paths))

    def test_a_local_header_with_a_different_root_is_not_context(self):
        self.replay()
        rows = self.rows(self.paths[2])
        rows[0]['payload']['session_id'] = self.uuid(999)
        self.save(self.paths[2], rows)
        self.assertNotIn(self.target(self.leaf, self.leaf_turn), native_root_requests(self.paths))

    def test_same_turn_with_another_explicit_owner_stays_unassigned(self):
        self.replay()
        rows = self.rows(self.paths[2])
        rows.append(self.owner(self.root, self.leaf_turn))
        self.save(self.paths[2], rows)
        self.assertNotIn(self.target(self.leaf, self.leaf_turn), native_root_requests(self.paths))

    def test_owner_witness_before_the_request_is_not_accepted(self):
        self.replay(owner=False)
        rows = self.rows(self.paths[2])
        rows.append(self.owner(self.leaf, self.leaf_turn, '2026-09-26T00:59:00Z'))
        self.save(self.paths[2], rows)
        self.assertNotIn(self.target(self.leaf, self.leaf_turn), native_root_requests(self.paths))

    def test_conflicting_root_turns_are_not_resolved_by_local_ownership(self):
        self.replay()
        rows = self.rows(self.paths[2])
        original = next(row for row in rows if row.get('payload', {}).get('turn_id') == self.leaf_turn
                        and row.get('payload', {}).get('type') == 'task_started')
        changed = {**original, 'payload': {**original['payload'], 'root_turn_id': self.uuid(999)}}
        rows.append(changed)
        self.save(self.paths[2], rows)
        self.assertNotIn(self.target(self.leaf, self.leaf_turn), native_root_requests(self.paths))

    def test_source_to_usage_to_root_purpose_preserves_quantity_and_history(self):
        self.replay()
        NativeRootOwnerTests.test_native_usage_to_root_purpose_to_query_conserves_history(self)

    def test_each_followup_request_needs_its_own_native_owner(self):
        self.replay()
        rows = self.rows(self.paths[2])
        followup = self.uuid(114)
        rows.append({'type': 'event_msg', 'timestamp': '2026-09-26T01:06:00Z', 'payload': {
            'type': 'task_started', 'turn_id': followup, 'root_turn_id': self.root_turn}})
        self.save(self.paths[2], rows)
        links = native_root_requests(self.paths)
        self.assertIn(self.target(self.leaf, self.leaf_turn), links)
        self.assertNotIn(self.target(self.leaf, followup), links)
        rows.append(self.owner(self.leaf, followup, '2026-09-26T01:07:00Z'))
        self.save(self.paths[2], rows)
        self.assertEqual(native_root_requests(self.paths)[self.target(self.leaf, followup)]['parent'],
                         self.target(self.root, self.root_turn))


if __name__ == '__main__':
    unittest.main()
