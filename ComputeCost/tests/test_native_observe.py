"""Observe-only node + judging collector reach what one full local collection reaches."""
import gzip
import json
from pathlib import Path
import unittest

from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.native_observations import KIND, store
from pulse_compute_cost.native_observe import acknowledge, discard, load_index, observe, pending_bundles
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.receipts import ReceiptError, current_receipts, reference

PEER = 'ownermac'


def heads(ledger):
    active, conflicts = current_receipts(ledger.receipt_snapshot()['facts'])
    assert not conflicts
    return {r.entity_ref: r.payload for r in active if r.kind != 'native_input'}


class ObserveTests(unittest.TestCase):
    def setUp(self):
        from test_cc_request_purpose import RequestPurposeTests
        self.case = RequestPurposeTests('test_reused_session_has_each_request_and_no_ambient_wi')
        self.case.setUp(); self.addCleanup(self.case.doCleanups)
        root = self.case.root
        self.registry = {**self.case.registry, 'ledger_root': str(root / 'observer')}
        self.index, self.outbox, self.spool = root / 'observer' / 'index.json', root / 'observer' / 'outbox', root / 'spool'
        self.judge = UsageLedger(root / 'judge')
        self.judge_registry = {'schema': 'computecost_source_registry/v1', 'sources': [
            {'id': 'ownermac-observations', 'kind': KIND, 'peer': PEER, 'path': str(self.spool),
             'source_ids': [self.case.source['id']]}]}

    def round(self, at):
        """One full local collection, and the same files observed, pushed and judged."""
        self.case.write()
        collect_registered_sources(self.case.registry, self.case.ledger, retirements={}, recoveries={})
        bundles = observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox, read_at=at)
        for bundle in bundles:
            store(self.spool, PEER, bundle.read_bytes())
            acknowledge(self.index, bundle)
        result = collect_registered_sources(self.judge_registry, self.judge, retirements={}, recoveries={})
        self.assertEqual(heads(self.judge), heads(self.case.ledger))
        return bundles, result

    def test_three_rounds_match_a_full_collection(self):
        from test_cc_request_purpose import WI, OTHER_WI
        self.case.request('a', 0, WI); self.case.request('b', 10)
        self.round('2026-01-01T02:00:00Z')
        self.assertTrue(load_index(self.index)['groups'])
        # The session gains a request: only that session is read and sent again.
        self.case.request('c', 20, OTHER_WI)
        self.round('2026-01-01T03:00:00Z')
        # Nothing changed: nothing new reaches the stream.
        _, result = self.round('2026-01-01T04:00:00Z')
        self.assertEqual(result['inserted'], 0)

    def test_holder_supplement_and_a_later_export_match_a_full_collection(self):
        from test_holder_supplement import CcRequestSupplementTests, OTHER_WI
        holder = CcRequestSupplementTests('test_declaration_first_then_single_holder_then_ambiguous')
        holder.setUp(); self.addCleanup(holder.doCleanups)
        self.case = holder
        self.registry = {**holder.registry, 'ledger_root': str(holder.root / 'observer')}
        self.judge_registry.update(core_session_writes=holder.registry['core_session_writes'],
                                   required_receipt_features=holder.registry['required_receipt_features'])
        holder.request('b', 10); holder.export()
        self.round('2026-01-01T02:00:00Z')
        # The core exports the write only after the session stopped changing.
        holder.held(OTHER_WI, 12); holder.export()
        self.round('2026-01-01T03:00:00Z')
        self.assertIn(holder.target('b-before'), {p['target_ref'] for p in heads(self.judge).values()
                                                  if p.get('policy') == 'holder_supplement.v1'})

    def test_bundles_stay_within_their_chunk_and_a_session_advances_only_when_all_arrive(self):
        from unittest.mock import patch
        from pulse_compute_cost import native_observe
        from test_cc_request_purpose import WI, OTHER_WI
        self.case.request('a', 0, WI); self.case.request('b', 10, OTHER_WI); self.case.request('c', 20)
        self.case.write()
        with patch.object(native_observe, 'CHUNK_BYTES', 6000):
            bundles = observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox,
                              read_at='2026-01-01T02:00:00Z')
            self.assertGreater(len(bundles), 2)
            for bundle in bundles:
                self.assertLessEqual(len(gzip.decompress(bundle.read_bytes())), 6000 + 2048)
            # Only the first chunk arrives: the session is read and sent again next run.
            acknowledge(self.index, bundles[0])
            for rest in bundles[1:]:
                discard(rest)
            self.assertEqual(load_index(self.index)['groups'], {})
            again = observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox,
                            read_at='2026-01-01T03:00:00Z')
            for bundle in again:
                store(self.spool, PEER, bundle.read_bytes()); acknowledge(self.index, bundle)
        self.assertTrue(load_index(self.index)['groups'])
        collect_registered_sources(self.case.registry, self.case.ledger, retirements={}, recoveries={})
        collect_registered_sources(self.judge_registry, self.judge, retirements={}, recoveries={})
        self.assertEqual(heads(self.judge), heads(self.case.ledger))

    def test_a_snapshot_value_returning_to_an_older_one_is_sent_again(self):
        from pulse_compute_cost.native_observations import decode
        from pulse_compute_cost.native_observe import _Outbox
        from pulse_compute_cost.request_inputs import SESSION_DECLARATION, recorded, version_inputs
        from test_request_inputs import SnapshotRuleTests
        rule = SnapshotRuleTests('test_concurrent_heads_prove_nothing')
        entry = {'config': {'id': 'relay', 'kind': 'relay_host'}, 'inventories': [], 'appended': []}
        coverage = {'status': 'unknown', 'reason': None, 'expected': None, 'observed': None, 'watermark': None,
                    'raw_count': 0, 'detail': {}, 'diagnostics': {}}
        for wi, at in (('wi_20261113_754', '2026-11-15T00:00:00Z'), ('none', '2026-11-15T01:00:00Z'),
                       ('wi_20261113_754', '2026-11-15T02:00:00Z')):
            box = _Outbox(self.outbox, PEER, load_index(self.index), at)
            box.add(entry, coverage, [], rule.declarations(wi, at))
            box.flush()
            bundle, = box.written
            inputs = decode(bundle.read_bytes(), PEER)['inputs']
            acknowledge(self.index, bundle)
            self.assertEqual(len(inputs), 1)
            versioned = version_inputs(inputs, self.judge.receipt_snapshot()['facts'])
            self.judge.ingest_receipts(versioned)
        found = recorded(self.judge.receipt_snapshot()['facts'], SESSION_DECLARATION)
        self.assertEqual(found[SnapshotRuleTests.KEY][0]['wi'], 'wi_20261113_754')

    def test_unchanged_sessions_are_not_read_again(self):
        from test_cc_request_purpose import WI
        self.case.request('a', 0, WI)
        self.case.write()
        first = observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox, read_at='2026-01-01T02:00:00Z')
        captured = [json.loads(gzip.decompress(b.read_bytes())) for b in first]
        self.assertTrue(any(s['rows'] for d in captured for s in d['sources']))
        for bundle in first:
            acknowledge(self.index, bundle)
        again = observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox, read_at='2026-01-01T03:00:00Z')
        documents = [json.loads(gzip.decompress(b.read_bytes())) for b in again]
        self.assertEqual([r for d in documents for s in d['sources'] for r in s['rows']], [])
        # Coverage still describes every file of the source.
        coverage = [s['coverage'] for d in documents for s in d['sources']]
        self.assertEqual(coverage[-1]['watermark'], captured[-1]['sources'][-1]['coverage']['watermark'])

    def test_an_unacknowledged_bundle_blocks_and_a_refused_one_is_read_again(self):
        from test_cc_request_purpose import WI
        self.case.request('a', 0, WI)
        self.case.write()
        bundles = observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox, read_at='2026-01-01T02:00:00Z')
        self.assertEqual(pending_bundles(self.outbox), bundles)
        with self.assertRaises(ReceiptError):
            observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox, read_at='2026-01-01T03:00:00Z')
        for bundle in bundles:
            discard(bundle)
        again = observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox, read_at='2026-01-01T03:00:00Z')
        self.assertTrue(any(s['rows'] for b in again for s in json.loads(gzip.decompress(b.read_bytes()))['sources']))


if __name__ == '__main__':
    unittest.main()


from test_cc_input_context import case, child, run  # noqa: E402,F401  (pytest fixture and helpers)


def test_relay_host_and_cc_input_actors_match_a_full_collection(case, tmp_path):
    """Relay Host rows, Host-authenticated CC input actors and principal history across nodes."""
    relay_case(case, tmp_path, metadata_late=False)


def test_input_metadata_restored_later_rereads_the_unchanged_session(case, tmp_path):
    """The session's bytes do not change; its Host input metadata appears only in round two."""
    relay_case(case, tmp_path, metadata_late=True)


def relay_case(case, tmp_path, *, metadata_late):
    from hashlib import sha256
    from pulse_compute_cost.receipts import reference
    from test_cc_input_context import AT, COUNTS, DISPATCH, END
    child(case); run(case)
    hostroot = tmp_path / 'host'; hostroot.mkdir(); case['host']['path'] = str(hostroot)
    ex = case['host_facts'][0].entity_ref
    human = reference('human', case['metadata'].stem)
    common = dict(schema='pulse_collaboration_host_event/v1', wi=None, executor='pulse', responsible=None, delegator=None,
                  runtime='cc', attempt_ref=ex, assignment_ref=None)
    rows = [{**common, 'kind': 'execution', 'state': 'accepted', 'expected_usage': True, 'at': DISPATCH,
             'started_at': DISPATCH, 'human_ref': human, 'human_input_at': AT,
             'event_ref': reference('host_event', [ex, 'authenticated_input', human])},
            {**common, 'kind': 'execution', 'state': 'completed', 'at': END, 'started_at': DISPATCH,
             'event_ref': reference('host_event', [ex, 'completed'])},
            {**common, 'kind': 'usage', 'at': END, 'started_at': DISPATCH, 'usage': COUNTS, 'model': 'test',
             'mapping': 'cc_result.v1', 'granularity': 'turn_aggregate', 'final': True,
             'native_scope_ref': reference('native_session', ['cc', 'session']),
             'covers': [reference('usage', ['session', 'message'])], 'event_ref': reference('host_event', [ex, 'usage'])}]
    (hostroot / 'later.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    registry = {'schema': 'computecost_source_registry/v1', 'sources': [case['host'], case['source']]}
    full = UsageLedger(tmp_path / 'full')
    collect_registered_sources(registry, full, retirements={}, recoveries={})
    observer = {**registry, 'ledger_root': str(tmp_path / 'observer')}
    index, outbox, spool = tmp_path / 'observer' / 'index.json', tmp_path / 'observer' / 'outbox', tmp_path / 'spool'
    judge = UsageLedger(tmp_path / 'judge')
    judge_registry = {'schema': 'computecost_source_registry/v1', 'sources': [
        {'id': 'ownermac-observations', 'kind': KIND, 'peer': PEER, 'path': str(spool), 'source_ids': ['host', 'native']}]}
    metadata = case['metadata']
    hidden = metadata.with_name('hidden')
    rounds = (True, False) if metadata_late else (False,)
    for missing in rounds:
        if missing:
            metadata.rename(hidden)
        for bundle in observe(observer, peer=PEER, index_path=index, outbox=outbox, read_at=END):
            store(spool, PEER, bundle.read_bytes()); acknowledge(index, bundle)
        if missing:
            hidden.rename(metadata)
        collect_registered_sources(judge_registry, judge, retirements={}, recoveries={})
    assert heads(judge) == heads(full)
    route = reference('attribution', ['cc_native_input_route', reference('usage', ['session', 'child-message']),
                                      reference('input_metadata', sha256(case['metadata'].read_bytes()).hexdigest())])
    assert heads(judge)[route]['context']['entrypoint'] == 'telegram'


def versions(ledger):
    """Every current version per entity; a Host and a transcript observation of one request may both stand."""
    active, conflicts = current_receipts(ledger.receipt_snapshot()['facts'])
    found = {r.entity_ref: [json.dumps(r.payload, sort_keys=True)] for r in active if r.kind != 'native_input'}
    found.update({ref: sorted(json.dumps(r.payload, sort_keys=True) for r in rows) for ref, rows in conflicts.items()})
    return found


class LateEvidenceTests(unittest.TestCase):
    """Evidence besides the transcript bytes that arrives or recovers later."""

    def setUp(self):
        from test_cc_usage import CcNativeAttributionTests
        self.fixture = CcNativeAttributionTests('test_host_bound_snapshot_attributes_only_bounded_requests')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        base = Path(self.fixture.tmp.name)
        hostdir = base / 'host'; hostdir.mkdir()
        self.fixture._host_facts()
        (base / 'host.jsonl').rename(hostdir / 'host.jsonl')
        self.registry = {'schema': 'computecost_source_registry/v1', 'ledger_root': str(base / 'observer'),
                         'sources': [{'id': 'host', 'kind': 'relay_host', 'path': str(hostdir)},
                                     {'id': 'cc', 'kind': 'cc', 'path': str(self.fixture.root)}]}
        self.index, self.outbox, self.spool = base / 'observer' / 'index.json', base / 'observer' / 'outbox', base / 'spool'
        self.judge = UsageLedger(base / 'judge')
        self.base = base
        self.judge_registry = {'schema': 'computecost_source_registry/v1', 'sources': [
            {'id': 'ownermac-observations', 'kind': KIND, 'peer': PEER, 'path': str(self.spool),
             'source_ids': ['host', 'cc']}]}

    def step(self, at):
        """An observe/push/judge round agrees with a fresh full collection of the same files.

        Fresh: a full collection that already holds the transcript's own
        observation beside the Host one no longer finds the Host authority
        for a snapshot that arrives later; reading from scratch does.
        """
        for bundle in observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox, read_at=at):
            store(self.spool, PEER, bundle.read_bytes()); acknowledge(self.index, bundle)
        collect_registered_sources(self.judge_registry, self.judge, retirements={}, recoveries={})
        full = UsageLedger(self.base / f'full-{at}')
        collect_registered_sources(self.registry, full, retirements={}, recoveries={})
        self.assertEqual(versions(self.judge), versions(full))

    def boundaries(self):
        return [v for vs in versions(self.judge).values() for v in vs if 'scl_verified_native_boundary.v1' in v]

    def test_a_snapshot_written_after_the_transcript_rereads_the_session(self):
        sealed = next((self.fixture.project / 'compute-cost').glob('*-sealed.json'))
        hidden = sealed.with_name('hidden')
        sealed.rename(hidden)
        self.step('2026-10-01T00:00:10Z')
        self.assertEqual(self.boundaries(), [])
        hidden.rename(sealed)  # The transcript and the Host stream do not change.
        self.step('2026-10-01T00:00:20Z')
        self.assertEqual(len(self.boundaries()), 1)

    def test_an_unreadable_source_is_reported_and_read_once_it_recovers(self):
        root = self.fixture.root
        moved = root.with_name('moved')
        root.rename(moved); root.symlink_to(moved)  # A full collection refuses a symlinked root.
        self.step('2026-10-01T00:00:10Z')
        cc = reference('source', 'cc')
        self.assertIn('"status": "unreachable"', ''.join(v for vs in versions(self.judge).values() for v in vs
                                                          if cc in v and '"domain"' in v))
        root.unlink(); moved.rename(root)  # Same inode, size and mtime as before.
        self.step('2026-10-01T00:00:20Z')
        self.assertEqual(len(self.boundaries()), 1)

    def test_a_failed_session_reading_is_kept_and_retried(self):
        from unittest.mock import patch
        from pulse_compute_cost import receipt_sources

        def broken(*args, **kwargs):
            raise OSError('transient')
        broken.__name__ = 'read_cc'
        with patch.object(receipt_sources, 'read_cc', broken):
            for bundle in observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox,
                                  read_at='2026-10-01T00:00:10Z'):
                store(self.spool, PEER, bundle.read_bytes()); acknowledge(self.index, bundle)
        collect_registered_sources(self.judge_registry, self.judge, retirements={}, recoveries={})
        records = list(load_index(self.index)['groups'].values())
        self.assertTrue(records)
        self.assertTrue(all(r['files'] is None and r['reason'] == 'source_unreadable_or_invalid' for r in records))
        self.step('2026-10-01T00:00:20Z')
        self.assertEqual(len(self.boundaries()), 1)

    def test_restoring_only_a_snapshots_permissions_rereads_the_session(self):
        import os
        sealed = next((self.fixture.project / 'compute-cost').glob('*-sealed.json'))
        os.chmod(sealed, 0o644)  # The snapshot reader refuses a readable-by-others snapshot.
        self.step('2026-10-01T00:00:10Z')
        self.assertEqual(self.boundaries(), [])
        os.chmod(sealed, 0o600)  # Same bytes, safe again.
        self.step('2026-10-01T00:00:20Z')
        self.assertEqual(len(self.boundaries()), 1)

    def test_an_unreadable_transcript_fails_its_session_only_and_is_retried(self):
        import os
        transcript = self.fixture.transcript
        os.chmod(transcript, 0)
        try:
            bundles = observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox,
                              read_at='2026-10-01T00:00:10Z')
            for bundle in bundles:
                store(self.spool, PEER, bundle.read_bytes()); acknowledge(self.index, bundle)
        finally:
            os.chmod(transcript, 0o600)
        records = list(load_index(self.index)['groups'].values())
        self.assertTrue(records and all(r['files'] is None and r['status'] == 'unreachable' for r in records))
        self.assertTrue(load_index(self.index)['sent'].get('host'))  # The other source went on.
        collect_registered_sources(self.judge_registry, self.judge, retirements={}, recoveries={})
        self.step('2026-10-01T00:00:20Z')
        self.assertEqual(len(self.boundaries()), 1)
