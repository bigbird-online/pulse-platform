"""WI300 mixed-source consumer acceptance. Explicit trusted-runner readback only.

Reuse ComputeCost's collector/query and the existing Stats replay assertions.
Freeze existing central receipts, two received peer bundles and approved Mac
native roots in a private temporary directory. Never update production inputs,
ledgers, schedules, applications or provider accounts. Outage injection below
renames only the copied transport. Native text never appears in test output.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import errno
import fcntl
from hashlib import sha256
import importlib.util
import json
import os
import pwd
from pathlib import Path
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'ComputeCost'), str(ROOT/'Stats/exporter'), str(Path(__file__).parent)]
from pulse_compute_cost.ledger import LedgerError, UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.receipts import Receipt, canonical, current_receipts, digest, digest_sequence, reference
from pulse_compute_cost.source_registration import candidate_registry
from pulse_compute_cost.stats_dashboard import build_dashboard, DEFAULT_FACTS
from prepare_cost_consumer_replay import assert_query_parity, record_case


def private_bytes(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(data)


def readonly_receipt_snapshot(root, *, watermark=None):
    """Read an existing ledger without the writer API creating its lock file."""
    root = Path(root)
    lock = root / '.usage-ledger.lock'
    if root.is_symlink() or not root.is_dir():
        raise ValueError('safe receipt snapshot requires an existing ledger directory')
    ledger = UsageLedger(root)
    reader = getattr(ledger, '_receipt_batches', None)
    if not callable(reader):
        raise ValueError('safe receipt snapshot API is unavailable')
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ELOOP}:
            raise ValueError('safe receipt snapshot requires an existing non-symlink lock') from exc
        raise ValueError('safe receipt snapshot lock is unavailable') from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('safe receipt snapshot requires a regular lock')
        fcntl.flock(fd, fcntl.LOCK_SH)
        # _receipt_batches is the ledger's existing validating read primitive.
        batches = reader()
    except LedgerError as exc:
        raise ValueError('safe receipt snapshot is unavailable') from exc
    finally:
        os.close(fd)
    if watermark is not None and (isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0):
        raise ValueError('invalid receipt watermark')
    indexes = [i for i,batch in enumerate(batches) if watermark is None or batch['sequence'] <= watermark]
    selected = [batches[i] for i in indexes]
    facts = [fact for i in indexes for fact in ledger._receipt_cache_rows[i]]
    last = selected[-1] if selected else None
    return {'facts': facts, 'watermark': last['sequence'] if last else 0,
            'recorded_at': last['recorded_at'] if last else None,
            'sha256': digest_sequence(fact.to_dict() for fact in facts)}


def actual_account_home():
    """Locate the invoking OS account, never an overridable HOME value."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not home.is_absolute():
        raise ValueError('actual OS account home is unavailable')
    return home


def active_coverage(facts, source_ref):
    """Select the current collector fact; historical failures are not current."""
    active, _ = current_receipts(facts)
    rows = [fact for fact in active if fact.kind == 'coverage'
            and fact.producer == 'registered_collector.v1'
            and fact.payload['source_ref'] == source_ref]
    if len(rows) != 1:
        raise AssertionError('expected exactly one current collector coverage fact')
    return rows[0]


def consumer_coverage_transition(baseline, failed):
    """Describe only a visible loss; an already-unknown consumer cannot degrade."""
    before = ['source_coverage_unknown' in view['summary']['cost']['reasons']
              for view in baseline['views']]
    after = ['source_coverage_unknown' in view['summary']['cost']['reasons']
             for view in failed['views']]
    assert len(before) == len(after) == 9
    assert all(not was_unknown or is_unknown for was_unknown, is_unknown in zip(before, after)), \
        'collector fault falsely promoted an already-unknown consumer view'
    if all(before):
        assert all(after), 'already-unknown consumer was falsely promoted by fault injection'
        return {'failure': 'not_observable_already_unknown', 'baseline_unknown_views': len(before),
                'failed_unknown_views': len(after)}
    assert all(after), 'collector failure did not make source coverage unknown'
    return {'failure': 'degraded', 'baseline_unknown_views': sum(before),
            'failed_unknown_views': len(after)}


def run(output, revision):
    if not output.is_absolute() or output.is_symlink() or output.stat().st_mode & 0o077:
        raise ValueError('existing private consumer replay directory required')
    index = json.loads((output/'index.json').read_text())
    if any(r['name'].startswith('real-mixed-') for r in index):
        raise ValueError('mixed replay already exists')
    # Read the established live-input locations for this OS account.  Only the
    # receipt snapshot needs a special reader: UsageLedger.receipt_snapshot()
    # opens its lock a+ and could create it in the production ledger.
    home = actual_account_home()
    state = home/'.local/state/pulse/stats'
    registry = json.loads((home/'.local/state/pulse/compute-cost/sources.json').read_text())
    snapshot = readonly_receipt_snapshot(registry['ledger_root'])
    catalog = json.loads((state/'openrouter-catalog.json').read_text())
    facts = json.loads(DEFAULT_FACTS.read_text())
    # Reuse the upstream reviewed bounded source-freezing helper unchanged.
    spec = importlib.util.spec_from_file_location('wi568_private_source_copy',
        ROOT/'ComputeCost/uat/stats_chain_replay.py')
    native_copy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native_copy)
    with tempfile.TemporaryDirectory(prefix='mixed-chain-', dir=output) as tmp:
        work = Path(tmp)
        ledger = UsageLedger(work/'ledger')
        if snapshot['facts']:
            ledger.ingest_receipts(snapshot['facts'])
        before = ledger.receipt_snapshot()
        sources, transports, inputs = [], [], []
        # The peers the installed publisher actually reads.
        peers = [p['id'] for p in json.loads((state/'refresh.json').read_text())['peers']]
        for peer in peers:
            source = state/'imports'/f'{peer}.jsonl'
            if source.is_symlink() or not source.is_file() or source.stat().st_mode & 0o077:
                raise ValueError('private saved peer input required')
            target = work/f'{peer}.jsonl'
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            receipt_count = 0; whole = sha256()
            try:
                with source.open('rb') as src, os.fdopen(fd, 'wb') as out:
                    for line in src:
                        if not line.strip():
                            continue
                        if len(line) > 4*1024*1024 or not line.endswith(b'\n'):
                            raise ValueError('invalid peer receipt line')
                        row = Receipt.from_dict(json.loads(line))
                        if canonical(row.to_dict()) + b'\n' != line:
                            raise ValueError('noncanonical peer receipt input')
                        out.write(line); whole.update(line); receipt_count += 1
            except Exception:
                target.unlink(missing_ok=True)
                raise
            transports.append(target)
            sources.append({'id':'stats-transport-'+peer, 'kind':'receipts', 'path':str(target)})
            inputs.append({'kind':'saved_peer_receipts', 'peer':peer, 'facts':receipt_count,
                           'sha256':whole.hexdigest(), 'bytes':target.stat().st_size})
        native = {}
        month = datetime.now(timezone.utc).strftime('%Y/%m')
        approved = [('pi', home/'.pi/agent/sessions', 'mac-pi'),
                    ('codex', home/'.codex/sessions'/month, 'mac-codex-active'),
                    ('codex', home/'.codex/archived_sessions', 'mac-codex-archive')]
        budget = 2*1024*1024*1024
        for kind, original, name in approved:
            target = work/name
            report = native_copy.frozen_native(original, target, min(budget, 1024*1024*1024), kind)
            budget -= report['bytes']
            if report['skipped_files'] or not report['copied_files']:
                raise ValueError('approved mixed replay source incomplete: '+name)
            native.setdefault(kind, []).append(str(target))
            inputs.append({'kind':'frozen_native', 'source':name, **report})
        candidate = candidate_registry({'schema':'computecost_source_registry/v1',
            'ledger_root':str(ledger.root), 'sources':sources}, native)
        first = collect_registered_sources(candidate, ledger)
        frozen = ledger.receipt_snapshot()
        repeat_collections = [collect_registered_sources(candidate, ledger) for _ in range(2)]
        repeats = [collection['inserted'] for collection in repeat_collections]
        assert repeats == [0, 0], 'identical frozen inputs changed ledger on replay'
        assert ledger.receipt_snapshot(watermark=before['watermark'])['sha256'] == before['sha256']
        def consume(name):
            document = build_dashboard(ledger, subscriptions=facts, source_revision=revision, catalog_snapshot=catalog)
            assert document['status'] == 'available' and len(document['views']) == 9, document.get('reason')
            assert_query_parity(ledger, document, catalog)
            result = record_case(output, name, document, datetime.fromisoformat(document['generated_at']))
            result['queries'] = [{'period':v['period'], 'dimension':v['dimension'],
                'events':v['summary']['usage']['event_count'],
                'full':v['summary']['cost']['machine_cash_usd'],
                'partial':v['summary']['cost']['partial_machine_cash_usd'],
                'reasons':v['summary']['cost']['reasons'], 'groups':len(v['groups'])} for v in document['views']]
            result.update(scope='central_and_saved_peer_receipts_plus_explicit_mac_native_roots', production_mutated=False)
            index.append(result)
            return document, result
        document, result = consume('real-mixed-query')
        assert result['priced_views'] > 0, 'mixed source query has no usable known-price subset'
        # Failure/recovery exercise the real collector on frozen source copies,
        # not the live SSH network or a production timer.
        target = transports[-1]
        offline = target.with_suffix('.offline')
        target.rename(offline)
        failed_collection = collect_registered_sources(candidate, ledger)
        failed_sources = {source['source_ref']: source for source in failed_collection['sources']}
        failed_ref = failed_sources[reference('source', 'stats-transport-tcloud')]
        assert {key: failed_ref[key] for key in ('source_ref','kind','status','fact_count','reason')} == {
            'source_ref': failed_ref['source_ref'], 'kind': 'receipts',
            'status': 'unreachable', 'fact_count': 0, 'reason': 'source_unreadable_or_invalid'}
        detail = failed_ref['source_detail']
        assert detail['kind'] == 'receipts' and detail['files_seen'] == 0 and detail['files_read'] == 0
        assert detail['files_invalid'] == 0 and not detail['errors']
        failed_snapshot = ledger.receipt_snapshot()
        failed_coverage = active_coverage(failed_snapshot['facts'], failed_ref['source_ref'])
        assert failed_coverage.payload['status'] == 'unreachable'
        failed, _ = consume('real-mixed-transport-unavailable')
        consumer_transition = consumer_coverage_transition(document, failed)
        offline.rename(target)
        recovered_collection = collect_registered_sources(candidate, ledger)
        recovered_ref = {source['source_ref']: source for source in recovered_collection['sources']}[failed_ref['source_ref']]
        assert recovered_ref['status'] == 'complete' and recovered_ref['reason'] is None
        recovered_coverage = active_coverage(ledger.receipt_snapshot()['facts'], failed_ref['source_ref'])
        assert recovered_coverage.payload['status'] == 'excluded'
        assert recovered_coverage.supersedes == failed_coverage.fact_ref
        restored, _ = consume('real-mixed-transport-recovered')
        restored_unknown = ['source_coverage_unknown' in view['summary']['cost']['reasons'] for view in restored['views']]
        baseline_unknown = ['source_coverage_unknown' in view['summary']['cost']['reasons'] for view in document['views']]
        assert restored_unknown == baseline_unknown, 'recovery falsely changed consumer coverage state'
        consumer_transition['recovery'] = ('recovered' if consumer_transition['failure'] == 'degraded'
                                           else 'not_observable_already_unknown')
        for old, new in zip(document['views'], restored['views']):
            for key in ('machine_cash_usd', 'partial_machine_cash_usd'):
                assert old['summary']['cost'][key] == new['summary']['cost'][key], 'recovery changed cost'
        assert ledger.receipt_snapshot(watermark=frozen['watermark'])['sha256'] == frozen['sha256']
        active, conflicts = current_receipts(frozen['facts'])
        report = {'source_revision':revision, 'scope':result['scope'], 'inputs':inputs,
            'production_source_watermark':snapshot['watermark'], 'first_inserted':first['inserted'],
            'repeat_inserted':repeats, 'collection_outcomes': {'initial': first['sources'],
                'repeats': [collection['sources'] for collection in repeat_collections],
                'unavailable': failed_collection['sources'], 'recovered': recovered_collection['sources']},
            'active_usage':sum(r.kind=='usage' for r in active),
            'revision_conflicts':len(conflicts), 'old_watermark_unchanged':True,
            'copied_transport_failure_recovery':True, 'consumer_coverage_transition':consumer_transition,
            'queries':result['queries'], 'production_mutated':False, 'whole_platform_coverage':False}
        private_bytes(output/'mixed-source-summary.json', canonical(report)+b'\n')
        # The private index contains no source text and is consumed by XCTest.
        (output/'index.json').write_bytes(canonical(index)+b'\n')
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source-revision', required=True)
    args = parser.parse_args()
    run(args.output, args.source_revision)
