"""Read-only Stats handoff preflight; test success is not production acceptance.

The configured plist is not evidence of the loaded job.  This probe reads both,
attests the invoked implementation against Git blobs at its declared revision,
and never writes host state or starts a process.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import pwd
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'Stats/exporter'))
sys.path.insert(0, str(ROOT / 'ComputeCost'))
from pulse_stats.cost_dashboard import load_cost_dashboard

# Keys are stable component identities.  Values are the current flattened source
# layout; LEGACY_FILES is only an explicit readback mapping, never a fallback
# hash guessed from a neighbouring release.
FILES = {
    'refresh': 'Scripts/pulse-stats-cost-refresh.py',
    'collaboration_query': 'ComputeCost/pulse_compute_cost/collaboration_query.py',
    'receipt_sources': 'ComputeCost/pulse_compute_cost/receipt_sources.py',
    'ledger': 'ComputeCost/pulse_compute_cost/ledger.py',
    'adapters': 'ComputeCost/pulse_compute_cost/adapters.py',
    'stats_dashboard': 'ComputeCost/pulse_compute_cost/stats_dashboard.py',
    'cost_dashboard': 'Stats/exporter/pulse_stats/cost_dashboard.py',
}
LEGACY_FILES = {
    key: ('Bigbird/' + path if path.startswith(('ComputeCost/', 'Stats/')) else path)
    for key, path in FILES.items()
}


def actual_account_home():
    """Use the UID account record rather than an environment-controlled HOME."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not home.is_absolute():
        raise ValueError('actual OS account home is unavailable')
    return home


def read_json(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('symlink document')
    return json.loads(path.read_text())


def _layout_files(root, layouts=(FILES, LEGACY_FILES)):
    """Return one complete, explicit layout; ambiguity is not attestation."""
    root = Path(root)
    matches = [files for files in layouts if all((root / path).is_file() for path in files.values())]
    if len(matches) != 1:
        raise ValueError('runtime component layout is missing or ambiguous')
    return matches[0]


def _sha256_files(root, files):
    return {key: hashlib.sha256((Path(root) / path).read_bytes()).hexdigest()
            for key, path in files.items()}


def authorized_component_hashes(revision, source_root=ROOT):
    """Hash actual Git blobs from one exact revision, not a release manifest."""
    if not re.fullmatch(r'[0-9a-f]{40}', revision or ''):
        raise ValueError('exact source revision required')
    source_root = Path(source_root)
    for files in (FILES, LEGACY_FILES):
        try:
            blobs = {}
            for key, path in files.items():
                blob = subprocess.run(
                    ['git', '-C', str(source_root), 'show', f'{revision}:{path}'],
                    capture_output=True, check=True, timeout=10).stdout
                blobs[key] = hashlib.sha256(blob).hexdigest()
            return {'source_layout': 'flat' if files is FILES else 'legacy',
                    'component_sha256': blobs}
        except subprocess.CalledProcessError:
            continue
    raise ValueError('authorized revision lacks a complete component set')


def read_runtime(script, authorized=None):
    """Attest the exact invoked release against authorized Git content.

    ``authorized`` is test/remote-probe injection only; production callers use
    ``authorized_component_hashes`` and consequently require readable Git blobs.
    The installed manifest is reported as legacy diagnostic evidence only.
    """
    script = Path(script).resolve(strict=True)
    if script.name != 'pulse-stats-cost-refresh.py' or script.parent.name != 'Scripts':
        raise ValueError('unexpected configured script')
    root = script.parents[1]
    revision = (root / '.stats-source-revision').read_text().strip()
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('invalid runtime revision')
    files = _layout_files(root)
    hashes = _sha256_files(root, files)
    manifest = read_json(root / '.stats-files.json')
    # Old manifests use paths; do not let either path spelling authorize code.
    manifest_matches = all(manifest.get(path) == hashes[key] for key, path in files.items())
    authorized = authorized if authorized is not None else authorized_component_hashes(revision)
    expected = authorized.get('component_sha256') if isinstance(authorized, dict) else None
    if not isinstance(expected, dict) or set(expected) != set(FILES):
        raise ValueError('incomplete authorized component set')
    return {'source_revision': revision,
            'runtime_layout': 'flat' if files is FILES else 'legacy',
            'component_sha256': hashes,
            'authorized_source_layout': authorized.get('source_layout'),
            'authorized_component_sha256': expected,
            'authorized_component_matches': hashes == expected,
            'installed_manifest_matches': manifest_matches}


def invocation_from_argv(argv):
    """Extract a publisher invocation without inferring any missing argument."""
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError('invalid publisher argv')
    if argv.count('--config') != 1:
        raise ValueError('publisher config argument is not unique')
    config_index = argv.index('--config')
    if config_index + 1 >= len(argv) or argv[config_index + 1].startswith('-'):
        raise ValueError('publisher config argument missing')
    scripts = [item for item in argv if item.endswith('pulse-stats-cost-refresh.py')]
    if len(scripts) != 1:
        raise ValueError('publisher script argument is not unique')
    return {'argv': list(argv), 'script': scripts[0], 'config': argv[config_index + 1]}


def parse_launchctl_arguments(text):
    """Parse only launchctl's arguments block; registration alone is insufficient."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == 'arguments = {')
    except StopIteration as exc:
        raise ValueError('loaded job arguments unavailable') from exc
    argv = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped == '}':
            if not argv:
                raise ValueError('loaded job arguments empty')
            return invocation_from_argv(argv)
        if not stripped or '=' in stripped:
            raise ValueError('unparseable loaded job arguments')
        try:
            words = shlex.split(stripped)
        except ValueError as exc:
            raise ValueError('unparseable loaded job arguments') from exc
        if len(words) != 1:
            raise ValueError('unparseable loaded job arguments')
        argv.append(words[0])
    raise ValueError('unterminated loaded job arguments')


def reconcile_invocations(configured_argv, loaded_output, launchctl_returncode=0):
    """Return separately observed configured/loaded invocations and agreement."""
    configured = invocation_from_argv(configured_argv)
    result = {'configured': configured, 'loaded': None, 'matches_loaded': False}
    if launchctl_returncode:
        result['loaded_error'] = 'launchctl_print_failed'
        return result
    try:
        result['loaded'] = parse_launchctl_arguments(loaded_output)
    except ValueError as exc:
        result['loaded_error'] = str(exc)
        return result
    result['matches_loaded'] = configured['argv'] == result['loaded']['argv']
    return result


def _last_exit_code(output):
    return next((line.split('=', 1)[1].strip() for line in output.splitlines()
                 if 'last exit code =' in line), None)


def inspect(home, expected_revision=None, allow_peers=False):
    now = datetime.now(timezone.utc)
    home = Path(home)
    state = home / '.local/state/pulse/stats'
    blockers = []
    report = {'observed_at': now.isoformat(), 'mode': 'read_only',
              'expected_upstream_revision': expected_revision, 'blockers': blockers,
              'source_coverage': 'not_proven_by_transport', 'production_ready': False}
    label = 'io.coevolution.pulse.stats.cost-refresh'
    job = plistlib.loads((home / 'Library/LaunchAgents' / f'{label}.plist').read_bytes())
    status = subprocess.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{label}'],
                            capture_output=True, text=True, timeout=10)
    publisher = reconcile_invocations(job.get('ProgramArguments'), status.stdout, status.returncode)
    publisher['registered'] = status.returncode == 0
    publisher['last_exit_code'] = _last_exit_code(status.stdout)
    report['publisher'] = publisher
    if status.returncode:
        blockers.append('refresh_job_not_loaded')
    if publisher.get('loaded_error'):
        blockers.append('refresh_job_loaded_arguments_unparseable')
    if not publisher['matches_loaded']:
        blockers.append('refresh_job_loaded_arguments_mismatch')
    if publisher['last_exit_code'] not in {None, '0'}:
        blockers.append('refresh_job_not_successful')
    if job.get('StartInterval') != 1800 or job.get('RunAtLoad') is not True:
        blockers.append('refresh_schedule_mismatch')

    # Do not use a disk/default config to green-light a loaded job.  The actual
    # loaded --config is the only config used for the following reconciliation.
    if publisher['loaded'] is None:
        # Preserve the separately observed configured argv in the report rather
        # than replacing it with a generic exception result.
        report['blockers'] = sorted(set(blockers))
        report['transport_and_amount_ready'] = False
        report['remaining_gate'] = 'loaded_publisher_readback_required'
        return report
    try:
        loaded_runtime = read_runtime(publisher['loaded']['script'])
        loaded_config = read_json(publisher['loaded']['config'])
        configured_runtime = read_runtime(publisher['configured']['script'])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        report['publisher']['loaded']['readback_error'] = type(exc).__name__
        blockers.append('loaded_publisher_readback_failed')
        report['blockers'] = sorted(set(blockers))
        report['transport_and_amount_ready'] = False
        report['remaining_gate'] = 'loaded_publisher_readback_required'
        return report
    report['publisher']['configured']['runtime'] = configured_runtime
    report['publisher']['loaded']['runtime'] = loaded_runtime
    report['publisher']['loaded']['config_revision'] = loaded_config.get('source_revision')
    revision = loaded_runtime['source_revision']
    if not loaded_runtime['authorized_component_matches']:
        blockers.append('loaded_publisher_component_not_authorized')
    if not configured_runtime['authorized_component_matches']:
        blockers.append('configured_publisher_component_not_authorized')
    if revision != loaded_config.get('source_revision'):
        blockers.append('loaded_publisher_config_revision_mismatch')
    if expected_revision and revision != expected_revision:
        blockers.append('publisher_has_not_adopted_expected_upstream')

    local = load_cost_dashboard(state / 'cost-dashboard-v1.json', now=now)
    report['publication'] = {'status': local['status'], 'reason': local.get('reason'),
        'accepted_source_revision': local.get('source_revision'),
        'accepted_generated_at': local['generated_at'] if local.get('source_revision') else None,
        'views': len(local['views']), 'watermarks': sorted({v['watermark'] for v in local['views']}),
        'priced_views': sum(v['summary']['cost'].get('machine_cash_usd') is not None or
                            v['summary']['cost'].get('partial_machine_cash_usd') is not None for v in local['views'])}
    if local['status'] != 'available': blockers.append('publication_unavailable_or_stale')
    elif not report['publication']['priced_views']: blockers.append('no_priced_query_view')
    if local.get('source_revision') is not None and local['source_revision'] != revision:
        blockers.append('publication_runtime_revision_mismatch')
    cache = read_json(home / 'Library/Application Support/PulseStats/snapshot-v2.json')
    cost = cache.get('cost_dashboard') or {}
    report['app_cache'] = {'snapshot_generated_at': cache.get('generated_at'),
        'cost_generated_at': cost.get('generated_at'), 'source_revision': cost.get('source_revision'),
        'status': cost.get('status'), 'reason': cost.get('reason'),
        'watermarks': sorted({v['watermark'] for v in cost.get('views', [])})}
    def matrix(doc):
        return {(v['period'], v['dimension']): (v['watermark'], v['report_sha256'],
            v['summary']['cost'].get('machine_cash_usd'), v['summary']['cost'].get('partial_machine_cash_usd'),
            v['summary']['cost']['reasons'], [(g['key'], g['cost'].get('machine_cash_usd'),
            g['cost'].get('partial_machine_cash_usd')) for g in v['groups']]) for v in doc.get('views', [])}
    report['cache_matches_publication'] = matrix(cost) == matrix(local) if len(local['views']) in {3,9,18,21} else None
    if report['cache_matches_publication'] is None: blockers.append('cache_query_parity_not_verifiable')
    elif not report['cache_matches_publication']: blockers.append('cache_not_on_same_query_publication')
    info = plistlib.loads(Path('/Applications/PulseStats.app/Contents/Info.plist').read_bytes())
    report['app'] = {key: info.get(key) for key in ('CFBundleShortVersionString', 'PulseSourceRevision', 'PulseBuildId')}
    registry = read_json(home / '.local/state/pulse/compute-cost/sources.json')
    report['local_source_kinds'] = sorted({s['kind'] for s in registry['sources']})
    report['upstream_inventory_still_required'] = True
    if not {'pi', 'codex'} <= set(report['local_source_kinds']): blockers.append('agreed_local_native_sources_not_registered')
    # Peer checks intentionally remain unavailable until their invocation is
    # separately attested to this same authorized blob set.
    if allow_peers: blockers.append('peer_invocations_not_implemented')
    else: blockers.append('peer_invocations_not_checked')
    report['blockers'] = sorted(set(blockers))
    report['transport_and_amount_ready'] = not blockers
    report['remaining_gate'] = 'real_cohort_reconciliation_and_two_live_refreshes_restart_recovery'
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-upstream-revision')
    parser.add_argument('--peers', action='store_true')
    parser.add_argument('--require-ready', action='store_true')
    args = parser.parse_args()
    if args.expected_upstream_revision and not re.fullmatch(r'[0-9a-f]{40}', args.expected_upstream_revision):
        parser.error('exact upstream SHA required')
    try:
        result = inspect(actual_account_home(), args.expected_upstream_revision, args.peers)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        result = {'mode': 'read_only', 'production_ready': False, 'transport_and_amount_ready': False,
                  'blockers': ['wiring_probe_failed'], 'error_type': type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.require_ready and not result['transport_and_amount_ready']:
        sys.exit(2)
