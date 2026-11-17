#!/usr/bin/env python3
"""Activate existing actor readers after a successful, exact Stats installation.

This is the second phase of the same self-hosted deployment, not a collector.
Only source metadata changes. Native paths, original facts and prices stay put.
All configured readers are checked before any writer is enabled. A failure keeps
upgraded readers in place; it never rolls a new-format ledger back to old code.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'ComputeCost'), str(ROOT / 'ComputeCost/uat'), str(ROOT / 'Stats')]
from pulse_compute_cost.source_registration import actor_candidate_registry
from pulse_compute_cost.receipts import Receipt, reference
from pulse_compute_cost.collaboration_query import CollaborationQuery
from deploy import PEER_HOSTS, adopt_human_input_directory, atomic, collected_peers, peer_command, run, writer_lock

FEATURE = 'actor_context.v1'
HISTORY_FEATURE = 'pi_parent_history.v1'
INPUT_FEATURE = 'host_native_input.v1'
SUBCALL_FEATURE = 'runtime_subcall.v1'
METERING_FEATURE = 'native_metering.v1'
SITES = {'ownermac', 'tcloud'}


def private_bytes(path: Path) -> bytes:
    st = path.lstat()
    if path.is_symlink() or not path.is_file() or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ValueError('source metadata must be a private owner file')
    return path.read_bytes()


def plan(original, surfaces, site, human_directory=None, *, codex_home=None):
    candidate = actor_candidate_registry(original, surfaces, site=site)
    for source in candidate['sources']:
        if source['kind'] in {'relay_host', 'pi'}:
            source['host_native_input'] = True
    if human_directory is not None:
        for source in candidate['sources']:
            if source['kind'] in {'relay_host', 'pi'}:
                adopt_human_input_directory(source, human_directory)
    if codex_home is not None:
        root = Path(codex_home)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ValueError('registered Codex home is invalid')
        for source in candidate['sources']:
            if source['kind'] == 'codex':
                source['counter_dependency_paths'] = [str(p) for p in
                    (root / 'sessions', root / 'archived_sessions') if p.is_dir() and not p.is_symlink()]
    candidate['required_receipt_features'] = sorted(set(original.get('required_receipt_features', [])) | {FEATURE, HISTORY_FEATURE, INPUT_FEATURE, SUBCALL_FEATURE, METERING_FEATURE})
    candidate['required_receipt_features'] = sorted(set(candidate['required_receipt_features']) | {'non_wi_activity.v1', 'wi_outcome_context.v1'})
    # No path/source/ledger migration is permitted in this activation step.
    assert candidate['ledger_root'] == original['ledger_root']
    assert len(candidate['sources']) == len(original['sources'])
    for before, after in zip(original['sources'], candidate['sources']):
        for key in ('id', 'kind', 'path', 'paths', 'runtime_manifest', 'profiles', 'layout'):
            assert before.get(key) == after.get(key), key
    return candidate


def check_installed(home, revision, site):
    if site not in SITES or not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('explicit installed revision and site required')
    current = home / '.local/share/pulse/stats/current'
    if current.resolve() != (home / '.local/share/pulse/stats/releases' / revision).resolve():
        raise ValueError('installed reader is not the requested revision')
    if (current / '.stats-source-revision').read_text().strip() != revision:
        raise ValueError('installed reader marker differs')
    if ROOT.resolve() != current.resolve():
        raise ValueError('activation must run from the installed immutable release')
    from pulse_compute_cost.receipts import SUPPORTED_RECEIPT_FEATURES
    if not {FEATURE, HISTORY_FEATURE, INPUT_FEATURE, SUBCALL_FEATURE, METERING_FEATURE, "non_wi_activity.v1", "wi_outcome_context.v1"} <= SUPPORTED_RECEIPT_FEATURES:
        raise ValueError('installed reader cannot reconcile retained native history')
    # Exercise the actual decoder and query contract, not just a version label.
    Receipt.make('attribution', 'activation-capability-check', {
        'target_ref': reference('usage', 'activation-capability-check'),
        'wi': None, 'executor': 'pulse', 'responsible': None, 'delegator': None,
        'state': 'assigned', 'policy': 'registered_runtime_actor.v1',
        'effective_at': '2026-09-15T00:00:00Z', 'scope': 'actor',
        'context': {'node': site, 'entrypoint': None, 'workspace_ref': None,
                    'input_ref': reference('input_metadata', 'capability-check'), 'turn_final': True},
    }, occurred_at='2026-09-15T00:00:00Z', observed_at='2026-09-15T00:00:00Z', producer='capability_check')
    from pulse_compute_cost.runtime_subcalls import validate_subcall
    validate_subcall({'parent_scope_ref':reference('native_session',['codex','capability-parent']),
        'child_scope_ref':reference('native_session',['pi','capability-child']),
        'call_ref':reference('runtime_tool_call','capability'), 'parent_runtime':'codex',
        'child_runtime':'pi','basis':'native_tool_result'})
    CollaborationQuery(group_by=('agent', 'node', 'runtime', 'model', 'entrypoint'))
    return {'site': site, 'revision': revision, 'reader_compatible': True}


def activate(home, revision, site):
    result = check_installed(home, revision, site)
    state = home / '.local/state/pulse/stats'
    registry = home / '.local/state/pulse/compute-cost/sources.json'
    with writer_lock(state / 'actor-sources.lock'):
        original_bytes = private_bytes(registry)
        original = json.loads(original_bytes)
        surfaces = json.loads((ROOT / 'config/scl-surfaces.yaml').read_bytes())
        # Reuse only an already recorded Host input directory. Never read its key
        # or use a bot's current display name to infer the historical channel.
        from effective_source_replay import runtime_environment
        env = runtime_environment()
        key_path = env.get('SCL_HUMAN_DECISION_KEY_FILE')
        directory = (Path(key_path).parent if key_path else home / '.local/state/agent-harness/scl-host') / 'human-inputs'
        if directory.is_symlink() or not directory.is_dir():
            directory = None
        refresh = json.loads(private_bytes(state / 'refresh.json')) if (state / 'refresh.json').is_file() else {}
        codex_home = refresh.get('codex_quota', {}).get('home')
        candidate = plan(original, surfaces, site, directory, codex_home=codex_home)
        data = json.dumps(candidate, sort_keys=True, separators=(',', ':')).encode() + b'\n'
        backup = state / ('sources-before-actors-' + revision + '.json')
        if not backup.exists():
            atomic(backup, original_bytes)
        else:
            private_bytes(backup)
        if private_bytes(registry) != original_bytes:
            raise ValueError('registry changed while preparing actor metadata')
        if data != original_bytes:
            atomic(registry, data)
        result.update(changed=data != original_bytes,
                      before_sha256=hashlib.sha256(original_bytes).hexdigest(),
                      after_sha256=hashlib.sha256(data).hexdigest(),
                      entrypoint_metadata_configured=directory is not None,
                      ledger_changed=False)
        return result


def activate_all(home, revision):
    state = home / '.local/state/pulse/stats'
    config = json.loads(private_bytes(state / 'refresh.json'))
    if config['source_revision'] != revision:
        raise ValueError('publisher configuration differs from requested release')
    # A peer paused by decision runs no Stats; its reader stays as last installed.
    peers = {p['host']: p for p in collected_peers(config)}
    for peer in peers.values():
        if peer['host'] not in PEER_HOSTS or peer['id'] not in SITES - {'ownermac'}:
            raise ValueError('unexpected configured peer')
        expected = '/home/pulse/.local/share/pulse/stats/releases/' + revision + '/Scripts/pulse-stats-cost-refresh.py'
        if peer['script'] != expected or peer.get('prefix', []) not in ([], ['runuser', '-u', 'pulse', '--']):
            raise ValueError('actual peer publisher has not been upgraded')
    report = {'revision': revision, 'nodes': [], 'status': 'checking_readers'}
    def remote(peer, action):
        script = '/home/pulse/.local/share/pulse/stats/releases/' + revision + '/Stats/activate_actor_sources.py'
        return json.loads(run(peer_command(peer, [peer['python'], script, action,
            '--revision', revision, '--site', peer['id']])))
    with writer_lock(state / 'cost-refresh.lock'):
        check_installed(home, revision, 'ownermac')
        for peer in peers.values():
            remote(peer, 'check')
        try:
            for peer in peers.values():
                report['nodes'].append(remote(peer, 'activate'))
            report['nodes'].append(activate(home, revision, 'ownermac'))
            report['status'] = 'active_pending_consumer_readback'
        except Exception:
            report['status'] = 'partial_activation_readers_retained'
            raise
        finally:
            report['observed_at'] = datetime.now(timezone.utc).isoformat()
            atomic(state / 'actor-activation.json', json.dumps(report, indent=2).encode())
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['check', 'activate', 'all'])
    parser.add_argument('--revision', required=True)
    parser.add_argument('--site', choices=sorted(SITES), default='ownermac')
    args = parser.parse_args()
    fn = {'check': check_installed, 'activate': activate, 'all': lambda h, r, s: activate_all(h, r)}[args.mode]
    print(json.dumps(fn(Path.home(), args.revision, args.site)), flush=True)
