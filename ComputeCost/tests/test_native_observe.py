"""Observe-only node + judging collector reach what one full local collection reaches."""
import gzip
import json
import os
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

    def sessions(self, count):
        """``count`` CC sessions of the same shape, each in its own transcript."""
        from test_mcp_cc_session_wi import SESSION
        self.case.request('a', 0); self.case.request('b', 10)
        self.case.write()
        text = self.case.path.read_text()
        for n in range(1, count):
            other = SESSION[:-4] + f'{0xd000 + n:04x}'
            (self.case.logs / (other + '.jsonl')).write_text(
                text.replace(SESSION, other).replace('input-', f'input{n}-').replace('prompt-', f'prompt{n}-')
                .replace('"u-', f'"u{n}-').replace('msg-', f'msg{n}-'))

    def run_observe(self, at, **kwargs):
        """One observe run delivering each bundle to the judge as it is written; the CC sessions it read."""
        from unittest.mock import patch
        from pulse_compute_cost import receipt_sources
        real, read = receipt_sources.observe_registered_sources, []

        def counted(registry, **options):
            if options.get('files'):
                read.extend(options['files'].get('cc', ()))
            return real(registry, **options)
        delivered = kwargs.pop('deliver', None) or (lambda bundle: (store(self.spool, PEER, bundle.read_bytes()),
                                                                   acknowledge(self.index, bundle)))
        try:
            with patch.object(receipt_sources, 'observe_registered_sources', counted):
                result = observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox, read_at=at,
                                 deliver=delivered, **kwargs)
        finally:
            for rest in pending_bundles(self.outbox):
                discard(rest)
        return result, read

    def judged_matches_a_full_collection(self):
        collect_registered_sources(self.case.registry, self.case.ledger, retirements={}, recoveries={})
        collect_registered_sources(self.judge_registry, self.judge, retirements={}, recoveries={})
        self.assertEqual(heads(self.judge), heads(self.case.ledger))

    def last_coverage(self):
        documents = sorted(self.spool.glob('*.json.gz'))
        return [s['coverage'] for s in json.loads(gzip.decompress(documents[-1].read_bytes()))['sources']
                if s['config']['id'] == 'cc'][-1]

    def test_a_run_out_of_time_delivers_what_it_read_and_the_next_run_continues(self):
        from pulse_compute_cost.native_observe import INCOMPLETE_REASON
        self.sessions(3)
        left = []
        for n, at in enumerate(('2026-01-01T02:00:00Z', '2026-01-01T03:00:00Z', '2026-01-01T04:00:00Z')):
            # A deadline already passed: each run reads one session (at least one), delivers it, then stops.
            result, read = self.run_observe(at, deadline=0)
            self.assertEqual(len(read), 1)
            left.append(result.remaining)
            coverage = self.last_coverage()
            self.assertEqual(coverage['reason'] == INCOMPLETE_REASON, n < 2)
            self.assertEqual(coverage['status'] == 'partial', n < 2)
        self.assertEqual(left, [2, 1, 0])
        self.assertEqual(len(load_index(self.index)['groups']), 3)
        self.judged_matches_a_full_collection()
        # Nothing changed: nothing is read again.
        result, read = self.run_observe('2026-01-01T05:00:00Z', deadline=0)
        self.assertEqual((read, result.remaining), ([], 0))

    def test_a_run_ended_mid_way_keeps_every_acknowledged_session(self):
        from unittest.mock import patch
        from pulse_compute_cost import native_observe
        self.sessions(3)

        def deliver(bundle):
            # Ends like the deadline's SIGALRM once one session's record was acknowledged.
            if load_index(self.index)['groups']:
                raise KeyboardInterrupt
            store(self.spool, PEER, bundle.read_bytes()); acknowledge(self.index, bundle)
        with patch.object(native_observe, 'CHUNK_BYTES', 1), self.assertRaises(KeyboardInterrupt):
            self.run_observe('2026-01-01T02:00:00Z', deliver=deliver)
        kept = len(load_index(self.index)['groups'])
        self.assertTrue(1 <= kept < 3)
        result, read = self.run_observe('2026-01-01T03:00:00Z')
        self.assertEqual((len(read), result.remaining), (3 - kept, 0))
        self.judged_matches_a_full_collection()

    def test_a_run_killed_inside_a_session_redoes_only_that_session(self):
        from unittest.mock import patch
        from pulse_compute_cost import native_observe, receipt_sources
        # Default CHUNK_BYTES: every session's rows fit one bundle, so only the
        # confirmation before each session keeps the sessions already read.
        self.assertEqual(native_observe.CHUNK_BYTES, 32 * 1024 * 1024)
        self.sessions(3)
        real, started = receipt_sources.observe_registered_sources, []

        def slow_last(registry, **options):
            if options.get('files'):
                started.append(sorted(options['files']['cc']))
                if len(started) == 3:
                    # Unacknowledged work at this moment: none but this session.
                    self.assertEqual(pending_bundles(self.outbox), [])
                    self.assertEqual(len(load_index(self.index)['groups']), 2)
                    raise KeyboardInterrupt  # The 45-minute SIGALRM inside the largest session.
            return real(registry, **options)
        with patch.object(receipt_sources, 'observe_registered_sources', slow_last), \
                self.assertRaises(KeyboardInterrupt):
            observe(self.registry, peer=PEER, index_path=self.index, outbox=self.outbox,
                    read_at='2026-01-01T02:00:00Z', deadline=float('inf'),
                    deliver=lambda bundle: (store(self.spool, PEER, bundle.read_bytes()),
                                            acknowledge(self.index, bundle)))
        for rest in pending_bundles(self.outbox):
            discard(rest)
        result, read = self.run_observe('2026-01-01T03:00:00Z')
        self.assertEqual((sorted(read), result.remaining), (started[2], 0))
        self.judged_matches_a_full_collection()

    def test_a_backfill_spans_runs_and_reads_each_session_once(self):
        self.sessions(3)
        self.run_observe('2026-01-01T02:00:00Z')
        reads = [len(self.run_observe(at, backfill='backfill-1', deadline=0)[1])
                 for at in ('2026-01-01T03:00:00Z', '2026-01-01T04:00:00Z', '2026-01-01T05:00:00Z',
                            '2026-01-01T06:00:00Z')]
        self.assertEqual(reads, [1, 1, 1, 0])
        self.assertEqual({r.get('backfill') for r in load_index(self.index)['groups'].values()}, {'backfill-1'})
        self.judged_matches_a_full_collection()

    def test_a_backfill_waits_for_an_unreachable_source_and_reads_it_once_back(self):
        self.sessions(3)
        self.run_observe('2026-01-01T02:00:00Z')  # Every session indexed before the backfill starts.
        root = Path(next(s for s in self.registry['sources'] if s.get('kind') == 'cc')['path'])
        moved = root.with_name(root.name + '-moved')
        root.rename(moved); root.symlink_to(moved)  # Unreachable: a symlinked root is refused.
        result, read = self.run_observe('2026-01-01T03:00:00Z', backfill='backfill-1')
        # Nothing read, yet the backfill still owes the three indexed sessions.
        self.assertEqual((read, result.remaining), ([], 3))
        self.assertEqual(self.last_coverage()['status'], 'unreachable')
        root.unlink(); moved.rename(root)  # Back with the same signatures as when indexed.
        result, read = self.run_observe('2026-01-01T04:00:00Z', backfill='backfill-1')
        self.assertEqual((len(read), result.remaining), (3, 0))
        self.assertEqual({r.get('backfill') for r in load_index(self.index)['groups'].values()}, {'backfill-1'})
        self.judged_matches_a_full_collection()
        # An ordinary run with the source unreachable reports it, and owes nothing to a backfill.
        root.rename(moved); root.symlink_to(moved)
        result, read = self.run_observe('2026-01-01T05:00:00Z')
        self.assertEqual((read, result.remaining), ([], 0))

    def test_a_source_directory_that_cannot_be_listed_is_unreachable_not_empty(self):
        # rglob lists an unreadable directory as empty; every session would read as removed.
        self.sessions(3)
        self.run_observe('2026-01-01T02:00:00Z')
        indexed = load_index(self.index)['groups']
        root = Path(next(s for s in self.registry['sources'] if s.get('kind') == 'cc')['path'])
        hidden = root / 'nested'
        hidden.mkdir()
        for unlisted, at in ((root, '03'), (hidden, '04')):
            with self.subTest(unlisted=unlisted.name):
                os.chmod(unlisted, 0)
                try:
                    result, read = self.run_observe(f'2026-01-01T{at}:00:00Z', backfill='backfill-1')
                finally:
                    os.chmod(unlisted, 0o700)
                # Nothing read or removed; the backfill still owes every indexed session.
                self.assertEqual((read, result.remaining), ([], 3))
                self.assertEqual(self.last_coverage()['status'], 'unreachable')
                self.assertEqual(load_index(self.index)['groups'], indexed)
        result, read = self.run_observe('2026-01-01T05:00:00Z', backfill='backfill-1')
        self.assertEqual((len(read), result.remaining), (3, 0))
        self.judged_matches_a_full_collection()

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


class IncrementalObserveTests(unittest.TestCase):
    """ObserveTests' rounds with this node's own sources in its local spool, judged incrementally (WI754)."""

    def setUp(self):
        ObserveTests.setUp(self)
        from pulse_compute_cost.incremental_collect import open_index
        self.index_db = open_index(self.judge, cache_mib=16, path=self.case.root / 'judge-index' / 'index.sqlite')
        self.addCleanup(self.index_db.close)
        self.publisher, self.rounds = True, []

    def round(self, at):
        from pulse_compute_cost.incremental_collect import collect_incremental, judging_registry, observe_into_spool
        self.case.write()
        collect_registered_sources(self.case.registry, self.case.ledger, retirements={}, recoveries={})
        own = {**self.registry, **{k: v for k, v in self.judge_registry.items() if k != 'sources'},
               'ledger_root': str(self.judge.root)}
        local = self.case.root / 'local-spool'
        observe_into_spool(own, spool=local, state=self.index.parent, read_at=at, retirements={})
        judged = judging_registry(own, local, {})
        self.assertEqual([s['kind'] for s in judged['sources']], [KIND])
        timings = {}
        result = collect_incremental(judged, self.judge, self.index_db, publisher=self.publisher, limit=10**6,
                                     retirements={}, recoveries={}, timings=timings)
        self.assertEqual(heads(self.judge), heads(self.case.ledger))
        self.assertFalse(list(local.glob('*.json.gz')))  # Consumed with the ingest.
        self.rounds.append(timings['incremental'])
        return [], result

    test_three_rounds_match_a_full_collection = ObserveTests.test_three_rounds_match_a_full_collection
    test_holder_supplement_and_a_later_export_match_a_full_collection = \
        ObserveTests.test_holder_supplement_and_a_later_export_match_a_full_collection

    def test_holder_supplement_on_a_node_that_keeps_no_queue(self):
        self.publisher = False
        self.test_holder_supplement_and_a_later_export_match_a_full_collection()
