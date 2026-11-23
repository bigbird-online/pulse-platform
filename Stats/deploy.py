#!/usr/bin/env python3
"""Install one reviewed Stats revision and its actual publisher; preserve source registries.

Invoked by the self-hosted Stats workflow. The caller must supply an exact tested
source and expected active revision. This is the Stats module installer, not a
Harness migration or an authority/ledger repair tool.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager, ExitStack, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

SHA = re.compile(r'[0-9a-f]{40}')
LABEL = 'io.coevolution.pulse.stats.cost-refresh'
PUBLISHER_POLICY = 'publisher-exclusive/v1'
EXECUTING_INSTALLER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
PEER_PYTHON = '/home/pulse/.local/share/coevolution/python-env/bin/python'
COMPONENTS = ('Stats', 'ComputeCost', 'Scripts/pulse-stats-cost-refresh.py', 'Scripts/pulse-meter.py',
              'config/scl-surfaces.yaml')
# The Stats reader runs on ownermac. Oracle joins the original observation ledger.
ORACLE_SITE = 'oracle-node-1'
ORACLE_HOME = Path('/home/pulse')
PUBLISHER_HOME = Path('/Users/owner')
ORACLE_PEER_HOST = 'pulse@oracle-node-1'
PEER_HOSTS = {'tcloud-node2', ORACLE_PEER_HOST}
# Retired on 2026-10-18; deployment removes it from refresh.json.
RETIRED_PEERS = {('vultr', 'vultr-pulse')}
# A paused peer stays registered (the automation view still reads it) but no
# deployment stages, selects or retargets a release there and no publisher
# collects from it. refresh.json `paused_peers` maps peer ID to reason; absent
# means DEFAULT_PAUSED_PEERS, `{}` resumes every peer. Every writer here keeps
# the field; only `peer-collection` changes it. Same rule as the refresh script.
PEER_PAUSE_REASONS = ('paused_by_decision',)
# 2026-11-15 human decision: by default only Oracle computes Stats.
DEFAULT_PAUSED_PEERS = {'tcloud': 'paused_by_decision'}
SITES = ('ownermac', 'tcloud', ORACLE_SITE)
# One cost-refresh cycle holds cost-refresh.lock for its whole run; observed
# cycles take 15-25 minutes (refresh_completed 894/1452/1476 s). Waiting for the
# current publisher covers one full cycle plus margin instead of stopping it.
PUBLISHER_WAIT_SECONDS = 40 * 60
# The Oracle publisher's cycle takes 38-51 minutes and its unit ends one after
# TimeoutStartSec=100min; the timer starts the next 120 s after one ends. The
# publisher installation (install-oracle-publisher, observe-mode) waits there
# until the current cycle ends, so a waiter polling the lock takes it in that gap.
ORACLE_CYCLE_LIMIT_SECONDS = 100 * 60
ORACLE_CYCLE_WAIT_SECONDS = ORACLE_CYCLE_LIMIT_SECONDS + 5 * 60
# The reader's publication pull (pull-publication) holds this lock from reading
# refresh.json to its last write and ends by SIGALRM within four minutes;
# switch-publisher changes `publisher` under it, after the cycle lock.
PUBLICATION_PULL_LOCK = 'publication-pull.lock'
PUBLICATION_PULL_WAIT_SECONDS = 5 * 60
# The launchd job runs the full publisher, or collect-local when refresh.json
# names a remote publisher (Scripts/pulse-stats-cost-refresh.py).
PUBLISHER_MODES = ('refresh', 'collect-local')
# The Oracle publisher: a systemd user timer over the selected Stats reader.
ORACLE_PUBLISHER_UNITS = ('pulse-stats-refresh.service', 'pulse-stats-refresh.timer')
ORACLE_WORKSPACE_REGISTRY = ORACLE_HOME/'Coevolution/ssot/registry.yaml'
ORACLE_PUBLISHER_VALIDITY_MINUTES = 75
PUSHING_PEER = 'ownermac'
# TCloud may push observation bundles instead (refresh script observe-push):
# its entry keeps the reader it runs and names the source IDs its bundles carry.
OBSERVING_PEER = 'tcloud'
# ownermac's refresh.json `collect_local_mode` (only `observe-mode` changes it):
# under observe, the Oracle publisher registers ownermac with every source ID of
# its registry as `observe_sources` and judges its bundles (native_push); under
# full and incremental (WI754) ownermac appends itself and pushes its position.
COLLECT_LOCAL_MODES = ('full', 'observe', 'incremental')
OBSERVE_SOURCE_ID = re.compile(r'[A-Za-z0-9_.-]{1,80}')
PUBLISHER_SWITCH_RECEIPT = 'publisher-switch.json'
# observe-mode's transaction on ownermac; refresh script reads the same name and statuses.
OBSERVE_MODE_RECEIPT = 'observe-mode.json'
OBSERVE_MODE_SCHEMA = 'pulse_stats_observe_mode/v1'
OBSERVE_MODE_SETTLED = ('completed', 'restored')
# incremental-mode's transaction on ownermac: refresh.json `incremental`, which
# the Oracle publisher's configuration carries (WI754).
INCREMENTAL_MODE_RECEIPT = 'incremental-mode.json'
INCREMENTAL_MODE_SCHEMA = 'pulse_stats_incremental_mode/v1'
TENANT_REGISTRY_MAX_BYTES = 1024 * 1024
PUBLISHER_WAIT_PROGRESS_SECONDS = 60


def stderr_tail(stderr, limit=800):
    """The end of a failed command's stderr for the operator; credentials are
    masked across the whole text before cutting, so no cut can expose a part."""
    text = stderr.decode('utf-8', 'replace')
    # Userinfo runs to the last @ before the host: an unescaped @ may sit in a password.
    text = re.sub(r'(://)[^/\s]*@', r'\1***@', text)
    text = re.sub(r'(?<![\w*])[^\s/@:*]+:[^\s/]*@(?=[\w.-])', '***@', text)
    return text[-limit:].strip()


def run(argv, *, data=None, timeout=180):
    p = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f'{Path(argv[0]).name} failed ({p.returncode}); stderr sha256='
                           + hashlib.sha256(p.stderr).hexdigest() + '; stderr tail: ' + stderr_tail(p.stderr))
    return p.stdout


def canonical_python(home):
    """Verify the existing account environment before changing runtime inputs."""
    python = home/'.local/share/coevolution/python-env/bin/python'
    probe = json.loads(run([str(python), '-I', '-c',
        'import json,sys,yaml; print(json.dumps({"executable":sys.executable,"prefix":sys.prefix}))'],
        timeout=15))
    if probe != {'executable': str(python),
                 'prefix': str(home/'.local/share/coevolution/python-env')}:
        raise ValueError('canonical Python identity differs')
    return python


def atomic(path: Path, data: bytes, mode=0o600):
    if path.is_symlink():
        raise ValueError('refusing a symlink write target')
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), mode); out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def retention_bytes(path, *, private=False):
    """Bounded, stable owned-file read; never follow a metadata symlink."""
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or before.st_size > 2 * 1024 * 1024
            or (private and before.st_mode & 0o077)):
        raise ValueError('unsafe retention metadata')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('retention metadata replaced before read')
        data = stream.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError('retention metadata bound exceeded')
    after = path.lstat()
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ValueError('retention metadata changed during read')
    return data


def retention_file(path, *, private=False):
    data = retention_bytes(path, private=private)
    info = path.lstat()
    return {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest(),
            'device': info.st_dev, 'inode': info.st_ino}


def retention_directory_identity(path):
    """Read a private real directory identity; this helper cannot create it."""
    owner = path.lstat()
    if (not stat.S_ISDIR(owner.st_mode) or owner.st_uid != os.getuid()
            or owner.st_mode & 0o077):
        raise ValueError('private owned retention directory required')
    return (owner.st_dev, owner.st_ino, owner.st_ctime_ns)


def retention_directory(path):
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
    retention_directory_identity(path)


def retention_once(path, data):
    """Publish one private immutable file without replacing an existing name."""
    fd, temporary = tempfile.mkstemp(prefix='.retention-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(data); out.flush(); os.fsync(out.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if retention_bytes(path, private=True) != data:
                raise ValueError('immutable retention evidence differs')
    finally:
        Path(temporary).unlink(missing_ok=True)


def publisher_arguments(home, runtime, *, install=False):
    """One permanent immutable launcher; revision payload loads under its lock."""
    body = retention_bytes(runtime/'Stats/publisher.py')
    directory = home/'.local/share/pulse/stats/publishers'
    entry = directory/(hashlib.sha256(body).hexdigest()+'.py')
    if install:
        retention_directory(directory)
        retention_once(entry, body)
    elif retention_bytes(entry, private=True) != body:
        raise ValueError('installed publisher entry differs from runtime source')
    info = entry.lstat()
    retention_directory_identity(directory)
    if directory.resolve() != directory or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise ValueError('private immutable publisher entry required')
    return [str(home/'.local/share/coevolution/python-env/bin/python'), str(entry),
            '--config', str(home/'.local/state/pulse/stats/refresh.json')]


def native_publisher_arguments(home, revision, mode='refresh'):
    if mode not in PUBLISHER_MODES:
        raise ValueError('unknown publisher mode')
    release = home/'.local/share/pulse/releases'/('PULSE-'+revision)
    config = home/'.local/state/pulse/stats/refresh.json'
    return [str(home/'.local/share/coevolution/python-env/bin/python'),
            str(release/'Scripts/pulse-stats-cost-refresh.py'), mode, '--config', str(config)]


def native_publisher(home, revision, arguments):
    """The immutable PULSE release selected as the native cost publisher."""
    release = home/'.local/share/pulse/releases'/('PULSE-'+revision)
    if arguments not in [native_publisher_arguments(home, revision, mode) for mode in PUBLISHER_MODES]:
        return None
    if (release.is_symlink() or not release.is_dir() or release.lstat().st_uid != os.getuid()
            or not (release/'Scripts/pulse-stats-cost-refresh.py').is_file()):
        raise ValueError('native publisher release identity differs')
    return release


def selected_publisher_runtime(home, revision, job):
    """The loaded immutable entry and its original installation define capability.

    refresh.json selects one revision, never a pool. Old writers install their
    original shared launcher; their use does not authorize exclusive retirement.
    """
    root=home/'.local/share/pulse/stats';state=home/'.local/state/pulse/stats'
    arguments=job.get('ProgramArguments');config_path=state/'refresh.json'
    config_sha=hashlib.sha256(retention_bytes(config_path,private=True)).hexdigest()
    job_path=home/'Library/LaunchAgents'/f'{LABEL}.plist'
    job_sha=hashlib.sha256(retention_bytes(job_path)).hexdigest()
    exclusive=root/'publisher-releases'/revision;matches=[]
    directory=state/'publisher-enrollments'
    if directory.exists() or directory.is_symlink():
        retention_directory_identity(directory)
        paths=list(directory.iterdir())
        if len(paths)>4096:raise ValueError('publisher adoption bound exceeded')
        for path in paths:
            record=json.loads(retention_bytes(path,private=True))
            if not isinstance(record,dict):raise ValueError('publisher adoption must be an object')
            publisher=record.get('publisher')
            if record.get('revision')!=revision or publisher is None:continue
            if not isinstance(publisher,dict):raise ValueError('publisher identity must be an object')
            if any(not isinstance(record.get(key),dict) for key in ('config','plist','original_receipt')):
                raise ValueError('publisher enrollment metadata must be objects')
            if record.get('config',{}).get('sha256')!=config_sha or record.get('plist',{}).get('sha256')!=job_sha:continue
            receipt=record.get('original_receipt',{});digest=receipt.get('sha256','')
            archive=state/'retention-receipts'/(digest+'.json')
            body=retention_bytes(archive,private=True);original=json.loads(body)
            if (not isinstance(original,dict) or record.get('schema')!='pulse_stats_publisher_enrollment/v1' or path.name!=digest+'.json'
                    or hashlib.sha256(body).hexdigest()!=digest or receipt.get('immutable_path')!=str(archive)
                    or original.get('publisher_policy')!=PUBLISHER_POLICY
                    or original.get('status')!='publisher_payload_selected'
                    or any(original.get(k)!=record.get(k) for k in ('revision','previous'))
                    or original.get('selected_at')!=record.get('recorded_at')
                    or publisher.get('policy')!=PUBLISHER_POLICY or publisher.get('path')!=str(exclusive)
                    or arguments!=publisher_arguments(home,exclusive)):
                raise ValueError('selected original publisher installation differs')
            info=exclusive.lstat()
            if (info.st_dev,info.st_ino)!=(publisher.get('device'),publisher.get('inode')):
                raise ValueError('selected publisher inode differs')
            if (publisher.get('manifest_sha256')!=hashlib.sha256(retention_bytes(exclusive/'.stats-files.json')).hexdigest()
                    or publisher.get('entry_sha256')!=hashlib.sha256(retention_bytes(exclusive/'Stats/publisher.py')).hexdigest()):
                raise ValueError('selected publisher content differs')
            matches.append(tuple(publisher.get(key) for key in ('policy','path','device','inode','manifest_sha256','entry_sha256')))
    if matches:
        # Failed/retried selections may leave multiple permanent audit facts.
        # Equal current capability facts are not another selector; never pick latest.
        if any(value!=matches[0] for value in matches):raise ValueError('ambiguous selected publisher capability')
        return exclusive
    shared=root/'releases'/revision;config=str(config_path)
    allowed=[[str(entry),str(shared/'Scripts/pulse-stats-cost-refresh.py'),'refresh','--config',config]
             for entry in (home/'.local/share/coevolution/python-env/bin/python',
                           home/'.local/state/pulse/compute-cost/venv/bin/python')]
    if arguments not in allowed:
        try:legacy_arguments=publisher_arguments(home,shared)
        except (OSError,ValueError):raise ValueError('unknown original publisher entry') from None
        if arguments!=legacy_arguments:raise ValueError('unknown original publisher entry')
        if b'PUBLISHER_PAYLOAD_POLICY' in retention_bytes(shared/'Stats/publisher.py'):
            raise ValueError('exclusive entry has no selected original installation')
    if current_revision(home)!=revision:raise ValueError('legacy shared publisher selector differs')
    return shared


def record_retention_adoption(home, receipt_path, *, app_paths=(), enrollment=False):
    """Append producer evidence, without granting acceptance or deletion."""
    state = home/'.local/state/pulse/stats'
    receipt_path.relative_to(state)
    body = retention_bytes(receipt_path, private=True)
    receipt = json.loads(body)
    revision = receipt.get('revision', '')
    local = receipt.get('scope') == 'local_cost_refresh'
    stamp=receipt.get('selected_at') if enrollment else receipt.get('completed_at')
    expected_status='publisher_payload_selected' if enrollment else ('installed' if local else 'activated_pending_business_readback')
    if (not SHA.fullmatch(revision) or not SHA.fullmatch(receipt.get('previous', ''))
            or not stamp or receipt.get('status')!=expected_status
            or (enrollment and receipt.get('publisher_policy')!=PUBLISHER_POLICY)):
        raise ValueError('terminal adoption receipt required')
    runtime = home/'.local/share/pulse/stats/releases'/revision
    if local and receipt.get('publisher_policy') == PUBLISHER_POLICY:
        runtime = home/'.local/share/pulse/stats/publisher-releases'/revision
    if runtime.is_symlink() or not runtime.is_dir():
        raise ValueError('owned runtime directory required')
    info = runtime.lstat()
    if info.st_uid != os.getuid() or retention_bytes(runtime/'.stats-source-revision').decode().strip() != revision:
        raise ValueError('runtime identity differs')
    apps = []
    for app in app_paths:
        if not app.exists():
            continue
        app_revision = plistlib.loads(retention_bytes(app/'Contents/Info.plist')).get('PulseSourceRevision', '')
        if not SHA.fullmatch(app_revision):
            raise ValueError('retained App identity differs')
        apps.append({'path': str(app), **validate_app(app, app_revision)})
    digest = hashlib.sha256(body).hexdigest()
    archive = state/'retention-receipts'
    retention_directory(archive)
    original = archive/(digest+'.json')
    retention_once(original, body)
    record = {'schema': 'pulse_stats_publisher_enrollment/v1' if enrollment else 'pulse_stats_adoption_retention/v1',
        'component': 'local_cost_refresh' if local else 'full_app_peer_deployment',
        'revision': revision, 'previous': receipt['previous'],
        'peer_previous': receipt.get('peer_previous', {}),
        'recorded_at': stamp,
        'original_receipt': {'path': str(receipt_path), 'sha256': digest,
                             'immutable_path': str(original)},
        'runtime': {'path': str(runtime), 'device': info.st_dev, 'inode': info.st_ino,
                    'manifest': retention_file(runtime/'.stats-files.json')},
        'config': retention_file(state/'refresh.json', private=True),
        'plist': retention_file(home/'Library/LaunchAgents'/f'{LABEL}.plist'),
        'app_paths': apps,
        'acceptance': 'unverified', 'supersession': 'unverified',
        'payload_expiry': 'not_authorized', 'audit_retention': 'permanent'}
    if receipt.get('publisher_policy') == PUBLISHER_POLICY:
        exclusive = home/'.local/share/pulse/stats/publisher-releases'/revision
        identity = exclusive.lstat()
        record['publisher'] = {'policy': PUBLISHER_POLICY, 'path': str(exclusive),
            'device': identity.st_dev, 'inode': identity.st_ino,
            'manifest_sha256': hashlib.sha256(retention_bytes(exclusive/'.stats-files.json')).hexdigest(),
            'entry_sha256': hashlib.sha256(retention_bytes(exclusive/'Stats/publisher.py')).hexdigest(),
            'source_registry': retention_file(home/'.local/state/pulse/compute-cost/sources.json',private=True)}
    directory = state/('publisher-enrollments' if enrollment else 'retention-adoptions')
    retention_directory(directory)
    path = directory/(digest+'.json')
    data = json.dumps(record, sort_keys=True, indent=2).encode()
    retention_once(path, data)
    return path


def plan_retention(home, expected):
    """Read declared producer/native references. This mode cannot delete."""
    state = home/'.local/state/pulse/stats'
    shared_before = current_revision(home)
    before = json.loads(retention_bytes(state/'refresh.json',private=True))['source_revision']
    selector = selector_identity(home)
    if before != expected:
        raise ValueError('active runtime changed before retention plan')
    references, inputs, absent, warnings = {shared_before: {'current_selector'}}, {}, [], []
    references.setdefault(before,set()).add('publisher_config')
    pattern = re.compile(r'/\.local/share/pulse/stats/releases/([0-9a-f]{40})(?:/|\b)')
    def protect(revision, role):
        if isinstance(revision, str) and SHA.fullmatch(revision):
            references.setdefault(revision, set()).add(role)
    def scan(value, role):
        if isinstance(value, dict):
            for item in value.values(): scan(item, role)
        elif isinstance(value, list):
            for item in value: scan(item, role)
        elif isinstance(value, str):
            for revision in pattern.findall(value): protect(revision, role)
    config_path = state/'refresh.json'
    job_path = home/'Library/LaunchAgents'/f'{LABEL}.plist'
    for path, role, parser, required in [
            (config_path, 'publisher_config', json.loads, True),
            (job_path, 'publisher_plist', plistlib.loads, True),
            (state/'deployment.json', 'full_deployment', json.loads, False),
            (home/'.local/state/pulse/compute-cost/sources.json', 'source_registry', json.loads, False),
            (home/'.codex/hooks.json', 'native_hook', json.loads, False),
            (home/'.claude/settings.json', 'native_claude', json.loads, False),
            (home/'.codex/config.toml', 'native_mcp', lambda b: b.decode(), False)]:
        if not path.exists() and not path.is_symlink():
            if required: raise ValueError('required retention input missing')
            absent.append(path)
            continue
        data = retention_bytes(path)
        inputs[str(path)] = hashlib.sha256(data).hexdigest()
        value = parser(data); scan(value, role)
        if role == 'publisher_config':
            if value.get('source_revision') != before:
                raise ValueError('publisher configuration differs from selector')
            protect(value.get('source_revision'), role)
        if role == 'publisher_plist':
            loaded_arguments = value['ProgramArguments']
            legacy_arguments = [str(home/'.local/share/coevolution/python-env/bin/python'),
                    str(home/'.local/share/pulse/stats/releases'/before/'Scripts/pulse-stats-cost-refresh.py'),
                    'refresh', '--config', str(config_path)]
            if loaded_arguments != legacy_arguments and loaded_arguments != publisher_arguments(
                    home, selected_publisher_runtime(home,before,value)):
                raise ValueError('publisher plist differs from selected runtime')
            if loaded_arguments != legacy_arguments:
                inputs[loaded_arguments[1]] = hashlib.sha256(retention_bytes(Path(loaded_arguments[1]), private=True)).hexdigest()
            require_publisher_arguments(f'gui/{os.getuid()}', value['ProgramArguments'])
        if role == 'full_deployment':
            protect(value.get('revision'), role); protect(value.get('shared_previous',value.get('previous')), 'full_rollback')
            protect(value.get('previous'), 'publisher_rollback')
            for revision in value.get('peer_previous', {}).values(): protect(revision, 'peer_rollback')
            if value.get('status') != 'accepted': warnings.append('full_deployment_acceptance_unverified')
    # Existing producer receipts can establish a protection reference. Reading
    # one never backfills acceptance or writes it into the new adoption index.
    attempts = list(state.glob('refresh-local-*/receipt.json'))
    if len(attempts) > 4096: raise ValueError('local adoption receipt bound exceeded')
    previous_found = False
    for path in attempts:
        data = retention_bytes(path, private=True)
        receipt = json.loads(data)
        if receipt.get('revision') != before or receipt.get('status') != 'installed':
            continue
        if (receipt.get('scope') != 'local_cost_refresh'
                or not SHA.fullmatch(receipt.get('previous', '')) or not receipt.get('completed_at')):
            raise ValueError('selected local adoption receipt differs')
        inputs[str(path)] = hashlib.sha256(data).hexdigest()
        protect(receipt['previous'], 'local_publisher_previous')
        previous_found = True
    if not previous_found: warnings.append('local_previous_authority_unverified')
    records = []
    index = state/'retention-adoptions'
    index_identity = archive_identity = None
    if index.exists() or index.is_symlink():
        index_identity = retention_directory_identity(index)
        archive_identity = retention_directory_identity(state/'retention-receipts')
        paths = sorted(index.iterdir())
        if len(paths) > 4096: raise ValueError('adoption index bound exceeded')
        for path in paths:
            record_body = retention_bytes(path, private=True)
            record = json.loads(record_body)
            if (record.get('schema') != 'pulse_stats_adoption_retention/v1'
                    or not SHA.fullmatch(record.get('revision', ''))
                    or not SHA.fullmatch(record.get('previous', ''))
                    or record.get('component') not in {'local_cost_refresh', 'full_app_peer_deployment'}
                    or path.name != record.get('original_receipt', {}).get('sha256', '')+'.json'):
                raise ValueError('unknown adoption record schema')
            digest = record['original_receipt']['sha256']
            archived = state/'retention-receipts'/(digest+'.json')
            if record['original_receipt'].get('immutable_path') != str(archived):
                raise ValueError('immutable receipt path differs')
            archived_body = retention_bytes(archived, private=True)
            receipt = json.loads(archived_body)
            local = record['component'] == 'local_cost_refresh'
            if (hashlib.sha256(archived_body).hexdigest() != digest
                    or any(receipt.get(k) != record.get(k) for k in ('revision', 'previous'))
                    or receipt.get('completed_at') != record.get('recorded_at')
                    or receipt.get('status') != ('installed' if local else 'activated_pending_business_readback')
                    or (receipt.get('scope') == 'local_cost_refresh') != local):
                raise ValueError('producer receipt binding differs')
            inputs[str(path)] = hashlib.sha256(record_body).hexdigest()
            inputs[str(archived)] = digest
            protect(record.get('previous'), 'producer_previous')
            for app in record.get('app_paths', []): protect(app.get('source_revision'), 'producer_app')
            records.append({'revision': record.get('revision'), 'record': str(path),
                            'reason': 'acceptance_supersession_expiry_unverified'})
    root = home/'.local/share/pulse/stats/releases'
    artifacts = sorted(root.iterdir())
    if len(artifacts) > 4096: raise ValueError('runtime inventory bound exceeded')
    recorded = {record['revision'] for record in records}
    items = [{'revision': p.name, 'path': str(p),
              'reason': 'declared_reference' if p.name in references else
                        ('producer_recorded_unverified' if p.name in recorded else 'legacy_authority_unknown'),
              'protected': True} for p in artifacts]
    # A changing input invalidates the read-only plan; no policy/index is backfilled.
    if current_revision(home) != shared_before or selector_identity(home) != selector:
        raise ValueError('active runtime changed during retention plan')
    for path, digest in inputs.items():
        if hashlib.sha256(retention_bytes(Path(path))).hexdigest() != digest:
            raise ValueError('retention input changed during plan')
    require_publisher_arguments(f'gui/{os.getuid()}', loaded_arguments)
    if index_identity is None:
        if index.exists() or index.is_symlink():
            raise ValueError('adoption index appeared during plan')
    elif (retention_directory_identity(index) != index_identity
            or retention_directory_identity(state/'retention-receipts') != archive_identity):
        raise ValueError('retention evidence directory changed during plan')
    for path in absent:
        if path.exists() or path.is_symlink():
            raise ValueError('retention reference appeared during plan')
    return {'schema': 'pulse_stats_retention_plan/v1', 'expected_current': before,
        'scope': 'declared_local_producer_and_native_references', 'inputs': inputs,
        'protected_revisions': {k: sorted(v) for k, v in sorted(references.items())},
        'adoption_records': records, 'runtime_items': items, 'warnings': warnings,
        'live_process_fd_closure': 'not_performed', 'app_preimage_retirement': 'not_authorized',
        'eligible_count': 0, 'apply_supported': False, 'automatic_deletion_performed': False}


def cc_quota_source(home):
    """Relay writes this on every CC turn (pulse.stats.cc_quota/v1)."""
    return Path(home)/'.local/share/pulse/data/relay/quota/cc-quota-v1.json'


def publisher_launchagent(original):
    """One half-hour background publisher at normal service priority."""
    return {**original, 'ProcessType': 'Standard', 'StartInterval': 30 * 60}


def refresh_config(original, revision, command, codex_home, cc_quota_path):
    """Update real invoked paths, never native registry/ledger/provider identities."""
    if not SHA.fullmatch(revision) or not command or not all(Path(x).is_absolute() for x in command):
        raise ValueError('exact revision and absolute Codex runtime command required')
    if not Path(codex_home).is_absolute():
        raise ValueError('explicit Codex home required')
    if not Path(cc_quota_path).is_absolute():
        raise ValueError('absolute Relay CC quota path required')
    result = peer_scripts(original, revision)
    result['source_revision'] = revision
    result['codex_quota'] = {'command': list(command), 'home': str(codex_home)}
    result['cc_quota'] = {'path': str(cc_quota_path)}
    return result


def configured_peers(config):
    """Peers plus the optional destination, each a host running a Stats peer."""
    destination = config.get('destination')
    return [*config['peers'], *([] if destination is None else [destination])]


def paused_peers(config):
    """Peer ID -> pause reason; see DEFAULT_PAUSED_PEERS."""
    paused = config.get('paused_peers', DEFAULT_PAUSED_PEERS)
    if (not isinstance(paused, dict)
            or any(not isinstance(i, str) or not re.fullmatch(r'[a-z0-9_-]{1,40}', i) for i in paused)
            or any(reason not in PEER_PAUSE_REASONS for reason in paused.values())):
        raise ValueError('paused peers must map safe peer IDs to a known reason')
    return dict(paused)


def collected_peers(config):
    """The configured peers Stats collects from: every one not paused."""
    paused = paused_peers(config)
    return [peer for peer in configured_peers(config) if peer.get('id') not in paused]


def reader_peers(config):
    """The configured peers that run a Stats reader, paused ones included: the
    automation view still reads their task state, so each deployment stages and
    selects the same reader there. Only collection-side steps skip a paused peer."""
    return [peer for peer in configured_peers(config) if 'host' in peer]


def retire_peers(original):
    """Drop retired nodes from refresh.json; every other field is preserved."""
    result = deepcopy(original)
    retired = lambda peer: (peer.get('id'), peer.get('host')) in RETIRED_PEERS
    result['peers'] = [peer for peer in result['peers'] if not retired(peer)]
    if result.get('destination') is not None and retired(result['destination']):
        del result['destination']
    return result


def peer_scripts(original, revision):
    """Point every peer and any destination at the selected peer reader release."""
    result = retire_peers(original)
    paused = result['paused_peers'] = paused_peers(original)
    for peer in configured_peers(result):
        if peer.get('transport') == 'shared_ledger_push':
            fields = set(peer) - {'observe_sources'}
            if 'observe_sources' in peer and not (isinstance(peer['observe_sources'], list) and peer['observe_sources']
                                                  and all(isinstance(i, str) for i in peer['observe_sources'])):
                raise ValueError('observe_sources must list source IDs')
            if peer.get('id') == PUSHING_PEER and fields == {'id', 'transport'}:
                continue  # It reaches the publisher; the publisher never reaches it.
            if (peer.get('id') != OBSERVING_PEER or 'observe_sources' not in peer
                    or fields - {'prefix'} != {'id', 'transport', 'host', 'python', 'script'}):
                raise ValueError('a pushing peer carries only its identity')
            # The publisher never reaches it, but it runs the selected reader: retarget it below.
        if peer['host'] not in PEER_HOSTS or peer.get('prefix', []) not in ([], ['runuser', '-u', 'pulse', '--']):
            raise ValueError('unapproved peer')
        if peer['host'] == ORACLE_PEER_HOST and (
            peer.get('id') != ORACLE_SITE or peer.get('transport') != 'shared_ledger' or peer.get('prefix')
        ):
            raise ValueError('Oracle must use the original shared ledger and direct pulse account')
        if peer.get('python') not in (PEER_PYTHON, '/usr/bin/python3',
                                     '/home/pulse/.local/state/pulse/compute-cost/venv/bin/python'):
            raise ValueError('unexpected peer Python entrypoint')
        old = peer['script']
        pattern = r'^(/home/pulse/\.local/share/pulse/stats/releases/)[0-9a-f]{40}(/Scripts/pulse-stats-cost-refresh\.py)$'
        match = re.fullmatch(pattern, old)
        if not match:
            raise ValueError('unexpected peer execution path')
        if peer.get('id') in paused:
            continue  # Keeps the release last staged there; resuming reaches it unchanged.
        peer['script'] = match[1] + revision + match[2]
        peer['python'] = PEER_PYTHON
    return result


def validate_app(app, revision):
    if app.is_symlink() or not app.is_dir():
        raise ValueError('regular tested App required')
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('PulseSourceRevision') != revision or not info.get('PulseBuildId'):
        raise ValueError('App source/build identity mismatch')
    binary = app / 'Contents/MacOS/PulseStats'
    return {'source_revision': revision, 'build_id': info['PulseBuildId'],
            'version': info['CFBundleShortVersionString'],
            'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}


def archive(source, revision):
    if run(['git', '-C', str(source), 'rev-parse', 'HEAD']).decode().strip() != revision:
        raise ValueError('source checkout is not pinned to the requested revision')
    # git archive, not copying the checkout: no untracked files, credentials or build output.
    return run(['git', '-C', str(source), 'archive', '--format=tar', revision, *COMPONENTS])


def require_installer_archive(data):
    """The formal full/local caller must run the installer from its exact target.

    Reject mixing a historical installed writer with a newer target archive.
    The workflow already pins both to the same approved checkout.
    """
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:') as tar:
        entries=[member for member in tar.getmembers() if member.name=='Stats/deploy.py']
        if (len(entries)!=1 or not entries[0].isfile() or entries[0].size>2*1024*1024
                or hashlib.sha256(tar.extractfile(entries[0]).read()).hexdigest()!=EXECUTING_INSTALLER_SHA256):
            raise ValueError('executing installer differs from the exact target archive')



def peer_stage_receiver():
    """Load the approved archived installer from a real private file on the peer."""
    return """import io,tarfile,pathlib,sys,tempfile
data=sys.stdin.buffer.read(67108865)
assert len(data)<=67108864
with tarfile.open(fileobj=io.BytesIO(data),mode="r:") as archive:
    body=archive.extractfile("Stats/deploy.py").read()
with tempfile.TemporaryDirectory(prefix="pulse-stats-bootstrap-") as directory:
    with tempfile.NamedTemporaryFile(dir=directory,suffix=".py") as installer:
        installer.write(body)
        installer.flush()
        namespace={"__name__":"stage_library","__file__":installer.name}
        exec(compile(body,installer.name,"exec"),namespace)
        namespace["require_installer_archive"](data)
        print(namespace["stage_runtime"](data,pathlib.Path.home(),sys.argv[1]))
"""

def stage_runtime(data, home, revision, *, publisher=False):
    if not SHA.fullmatch(revision) or len(data) > 64 * 1024 * 1024:
        raise ValueError('invalid or oversized release')
    root = home / '.local/share/pulse/stats' / ('publisher-releases' if publisher else 'releases')
    if root.is_symlink():
        raise ValueError('runtime pool must not be a symlink')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    retention_directory_identity(root)
    if root.resolve() != root:
        raise ValueError('runtime pool parent differs')
    target = root / revision
    members, manifest = [], {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('unsafe release member')
            if member.isfile():
                total += member.size
                if member.name in manifest or total > 64 * 1024 * 1024:
                    raise ValueError('duplicate or oversized release member')
                body = tar.extractfile(member).read()
                manifest[member.name] = hashlib.sha256(body).hexdigest()
                # Git's default tar.umask can produce 0664/0775. The loader
                # requires owned code without group/other write permission.
                members.append((member.name, body, member.mode & 0o755))
    if not manifest or 'Scripts/pulse-stats-cost-refresh.py' not in manifest:
        raise ValueError('incomplete Stats runtime')
    if target.exists():
        retention_directory_identity(target)
        if target.is_symlink():
            raise ValueError('release path is a symlink')
        for name, expected in manifest.items():
            path = target/name
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_nlink != 1
                    or path.resolve() != path):
                raise ValueError('immutable release file identity differs')
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('immutable release differs')
        if (target/'.stats-source-revision').read_text().strip() != revision:
            raise ValueError('release marker mismatch')
        return target
    staging = Path(tempfile.mkdtemp(prefix='.'+revision+'-', dir=root))
    try:
        for name, body, mode in members:
            dest = staging/name; dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body); dest.chmod(mode)
        (staging/'.stats-files.json').write_text(json.dumps(manifest, sort_keys=True))
        (staging/'.stats-source-revision').write_text(revision+'\n')
        os.rename(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


def verify_reader_features(runtime, registry):
    if any(s.get('kind')=='codex' and s.get('request_purpose_directory') is not None
           for s in registry.get('sources', [])):
        # A selected reader may not silently drop an already-enrolled
        # native input producer during an otherwise compatible rollback.
        if not (runtime/'ComputeCost/pulse_compute_cost/native_request_purpose.py').is_file():
            raise ValueError('candidate cannot read enrolled native request purpose inputs')
    if any(s.get('kind') == 'codex' and s.get('session_wi_mcp_since') is not None
           for s in registry.get('sources', [])):
        code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
                'from pulse_compute_cost.mcp_request_purpose import TRANSPORT_SCHEMA;'
                'print(json.dumps(TRANSPORT_SCHEMA))')
        try:
            supported = json.loads(run([sys.executable, '-I', '-B', '-c', code, str(runtime/'ComputeCost')], timeout=15))
        except (RuntimeError, ValueError):
            raise ValueError('candidate cannot read the active native MCP purpose protocol') from None
        if supported != 'codex_session_wi/v1':
            raise ValueError('candidate cannot read the active native MCP purpose protocol')
    if any(s.get('kind') == 'cc' and s.get('session_wi_mcp_since') is not None
           for s in registry.get('sources', [])):
        code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
                'from pulse_compute_cost.mcp_request_purpose import CC_TRANSPORT_SCHEMA;'
                'print(json.dumps(CC_TRANSPORT_SCHEMA))')
        try:
            supported = json.loads(run([sys.executable, '-I', '-B', '-c', code, str(runtime/'ComputeCost')], timeout=15))
        except (RuntimeError, ValueError):
            raise ValueError('candidate cannot read the active CC MCP purpose protocol') from None
        if supported != 'cc_session_wi/v1':
            raise ValueError('candidate cannot read the active CC MCP purpose protocol')
    if any(s.get('kind') == 'pi' and s.get('session_wi_mcp_since') is not None
           for s in registry.get('sources', [])):
        code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
                'from pulse_compute_cost.pi_request_purpose import SCHEMA;'
                'print(json.dumps(SCHEMA))')
        try:
            supported = json.loads(run([sys.executable, '-I', '-B', '-c', code, str(runtime/'ComputeCost')], timeout=15))
        except (RuntimeError, ValueError):
            raise ValueError('candidate cannot read the active Pi purpose protocol') from None
        if supported != 'pi_request_wi/v1':
            raise ValueError('candidate cannot read the active Pi purpose protocol')
    if any(s.get('kind') == 'harness_core' for s in registry.get('sources', [])):
        if not (runtime/'ComputeCost/pulse_compute_cost/kernel_outcomes.py').is_file():
            raise ValueError('candidate cannot read the enrolled core Outcome source')
    if registry.get('core_session_writes') is not None:
        if not (runtime/'ComputeCost/pulse_compute_cost/holder_supplement.py').is_file():
            raise ValueError('candidate cannot read the enrolled core session writes')
    if any(source.get('layout') == 'codex_calendar_months.v1' for source in registry.get('sources', [])):
        code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
                'from pulse_compute_cost.source_layout import CODEX_CALENDAR_LAYOUT;'
                'print(json.dumps(CODEX_CALENDAR_LAYOUT))')
        try:
            layout = json.loads(run([sys.executable,'-I','-B','-c',code,str(runtime/'ComputeCost')],timeout=15))
        except (RuntimeError,ValueError):
            raise ValueError('candidate cannot read enrolled native calendar source') from None
        if layout != 'codex_calendar_months.v1':
            raise ValueError('candidate cannot read enrolled native calendar source')
    required = registry.get('required_receipt_features', [])
    if not required:
        return
    if not isinstance(required, list) or any(not isinstance(v, str) for v in required):
        raise ValueError('invalid retained receipt feature declaration')
    code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
            'from pulse_compute_cost import receipts;'
            'print(json.dumps(sorted(getattr(receipts,"SUPPORTED_RECEIPT_FEATURES",()))))')
    supported = json.loads(run([sys.executable, '-I', '-B', '-c', code, str(runtime/'ComputeCost')]))
    if not set(required) <= set(supported):
        raise ValueError('candidate cannot read retained receipt format')


def current_revision(home):
    current = home/'.local/share/pulse/stats/current'
    if not current.is_symlink():
        raise ValueError('active runtime is not a release selector')
    runtime = current.resolve(strict=True)
    revision = (runtime/'.stats-source-revision').read_text().strip()
    expected_path = current.parent/'releases'/revision
    if (not SHA.fullmatch(revision) or runtime != expected_path.resolve(strict=True)
            or expected_path.is_symlink()):
        raise ValueError('active runtime release identity differs')
    return revision


def preflight_runtime(home, revision, expected=None, *, attempt=None):
    """Read the actual selector and prove both forward and rollback readers.

    A node whose registry names the installed native core reads receipts with
    its PULSE release, so its Stats selector need not read that format.
    """
    if not SHA.fullmatch(revision):
        raise ValueError('exact target revision required')
    before = current_revision(home)
    if expected is not None and before != expected:
        raise ValueError('active runtime changed after preflight')
    base = home/'.local/share/pulse/stats'
    target = base/'releases'/revision
    if target.is_symlink() or (target/'.stats-source-revision').read_text().strip() != revision:
        raise ValueError('target runtime is not staged')
    pending = base/('.current-'+revision)
    if pending.exists() or pending.is_symlink():
        raise ValueError('runtime selection already in progress')
    registry = home/'.local/state/pulse/compute-cost/sources.json'
    original = json.loads(registry.read_bytes()) if registry.exists() else {}
    proposed = original
    if attempt is not None:
        path, old, new = source_plan_inputs(home, revision, attempt=attempt)
        if path.read_bytes() != old:
            raise ValueError('source registry changed after preflight')
        proposed = json.loads(new)
        require_retained_purpose_cutover(proposed, original)
    for runtime in (target, base/'releases'/before):
        if not (runtime/'Scripts/pulse-stats-cost-refresh.py').is_file():
            raise ValueError('forward or rollback publisher is unavailable')
        if not original.get('observation_core_entrypoint'):
            verify_reader_features(runtime, original)
            verify_reader_features(runtime, proposed)
    return {'previous': before, 'revision': revision}


def select_runtime(home, revision, expected, *, strict=False):
    if not SHA.fullmatch(revision) or not SHA.fullmatch(expected):
        raise ValueError('exact runtime SHAs required')
    current = home/'.local/share/pulse/stats/current'
    target = current.parent/'releases'/revision
    if target.is_symlink() or (target/'.stats-source-revision').read_text().strip() != revision:
        raise ValueError('target runtime is not staged')
    registry = home/'.local/state/pulse/compute-cost/sources.json'
    if registry.exists():
        verify_reader_features(target, json.loads(registry.read_bytes()))
    before = current_revision(home)
    if (before != expected if strict else before not in {expected, revision}):
        raise ValueError('active runtime changed; refusing stale deployment')
    if before != revision:
        candidate = current.with_name('.current-'+revision)
        if candidate.exists() or candidate.is_symlink():
            raise ValueError('runtime selection already in progress')
        candidate.symlink_to(current.parent/'releases'/revision)
        os.replace(candidate, current)
    return before


def _source_plan_paths(home, revision, attempt=None):
    if not SHA.fullmatch(revision):
        raise ValueError('exact source-plan revision required')
    if attempt is not None and (not isinstance(attempt, str) or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', attempt)):
        raise ValueError('invalid existing build identity for source plan')
    key = revision if attempt is None else revision+'--'+attempt
    state = home/'.local/state/pulse/stats'
    registry = home/'.local/state/pulse/compute-cost/sources.json'
    return registry, state/('sources-before-'+key+'.json'), state/('sources-for-'+key+'.json'), state/('sources-plan-'+key+'.json')


def oracle_native_usage(home, manifest, registry):
    """Enroll the sources bound by the live Oracle native installation."""
    from pulse_compute_cost.source_registration import candidate_registry

    if home != ORACLE_HOME or os.uname().nodename.split('.')[0] != ORACLE_SITE:
        raise ValueError('Oracle source preparation requires the registered service account')
    core = home/'.local/share/pulse/tools/scl-core'
    active = core/'active'
    observed_entry = Path(json.loads(manifest.read_bytes())['workItemCoreHost']['entrypoint'])
    observed_program = observed_entry.resolve(strict=True)
    if (not active.is_symlink() or observed_program != (active/'harness.pyz').resolve(strict=True)
            or observed_program.parent.parent != core/'releases'
            or registry.get('observation_core_entrypoint') != str(active/'harness.pyz')):
        raise ValueError('Oracle Relay and ComputeCost must use the same installed native core')
    policy_bytes = retention_bytes(observed_program.with_name('host-policy.json'), private=True)
    policy = json.loads(policy_bytes)
    roots = {runtime: set() for runtime in ('cc', 'codex', 'pi')}
    for profile in policy['profiles']:
        runtime = profile.get('runtime')
        if profile.get('role') == 'producer' and (runtime in ('cc', 'codex')
                or (runtime == 'pi' and profile.get('standalone'))):
            roots[runtime].add(profile['session_root'])
    if not all(roots.values()):
        raise ValueError('Oracle native core has incomplete usage source bindings')
    archive = home/'.codex/archived_sessions'
    if os.path.lexists(archive):
        roots['codex'].add(str(archive))
    original_ids = {row['id'] for row in registry['sources']}
    candidate = candidate_registry(registry, {kind:list(paths) for kind, paths in roots.items()})
    for row in candidate['sources']:
        if row['id'] not in original_ids:
            row['node'] = ORACLE_SITE
    return candidate, hashlib.sha256(policy_bytes).hexdigest()


def adopt_human_input_directory(source, directory):
    """The running Host's input directory is current; earlier ones stay readable history."""
    historical = source.get('human_input_directories', [])
    if not isinstance(historical, list):
        raise ValueError('invalid registered historical input directories')
    previous = source.get('human_input_directory')
    source['human_input_directories'] = sorted(set(historical + ([previous] if previous else [])) - {str(directory)})
    source['human_input_directory'] = str(directory)


def oracle_actor_sources(home, candidate, surfaces, env):
    """Give Oracle's Host and Relay Pi sources the actor wiring TCloud has.

    TCloud gained it in activate_actor_sources; Oracle sources are created
    here, and the refresh below only reaches sources already enabled, so
    without this the Oracle Pi usage never resolves its Agent.
    """
    from pulse_compute_cost.source_registration import actor_candidate_registry
    mapped = actor_candidate_registry(candidate, surfaces, site=ORACLE_SITE)
    key = env.get('SCL_HUMAN_DECISION_KEY_FILE')
    directory = (Path(key).parent if key else home/'.local/state/agent-harness/scl-host')/'human-inputs'
    for source, current in zip(candidate['sources'], mapped['sources']):
        if source['kind'] != 'relay_host' and not (source['kind'] == 'pi' and source.get('runtime_manifest')):
            continue
        for field in ('node', 'actor_attribution', 'scl_surface_id', 'scl_kernel_root', 'workspace_actors'):
            if field in current:
                source[field] = current[field]
            else:
                source.pop(field, None)  # No longer registered for this site.
        source['host_native_input'] = True
        if directory.is_dir() and not directory.is_symlink():
            adopt_human_input_directory(source, directory)
    _adopt_team_digest(candidate, mapped)
    return candidate


def _adopt_team_digest(candidate, mapped):
    """The adopted team registry digest is the one just read, or none."""
    if 'workspace_registry_sha256' in mapped:
        candidate['workspace_registry_sha256'] = mapped['workspace_registry_sha256']
    else:
        candidate.pop('workspace_registry_sha256', None)


def load_surfaces(root):
    return json.loads((root/'config/scl-surfaces.yaml').read_text())


def team_registry_root(surfaces, site):
    """The registered {repo_root, checkout_root} of this host's team registry checkout."""
    found = [s['deployment']['team_registry_root'] for s in surfaces.get('surfaces', [])
             if s.get('deployment', {}).get('site') == site and 'team_registry_root' in s['deployment']]
    if len(found) > 1:
        raise ValueError('ambiguous registered team registry root')
    return dict(found[0]) if found else None


def prepare_sources(home, revision, site, *, attempt=None, workspace_registry=None):
    """Prepare the same selector used by the real-source gate, without activation."""
    if site not in SITES:
        raise ValueError('unapproved source site')
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root/'ComputeCost'), str(root/'ComputeCost/uat')]
    from effective_source_replay import runtime_environment
    from pulse_compute_cost.source_registration import runtime_candidate_registry
    registry, before, after, plan = _source_plan_paths(home, revision, attempt)
    if registry.is_symlink() or registry.stat().st_mode & 0o077:
        raise ValueError('private source registry required')
    original = registry.read_bytes()
    env = runtime_environment()
    manifest = Path(env['RELAY_PI_RUNTIME_MANIFEST_PATH'])
    enrolled = json.loads(original)
    surfaces = load_surfaces(root)
    team_root = team_registry_root(surfaces, site)
    if workspace_registry is not None:
        enrolled['workspace_registry_path'] = str(workspace_registry)
    elif (team_root and 'workspace_registry_path' not in enrolled
            and (Path(team_root['checkout_root'])/'ssot/registry.yaml').is_file()):
        # This host's checkout of the team registry, as its publisher reads it.
        enrolled['workspace_registry_path'] = str(Path(team_root['checkout_root'])/'ssot/registry.yaml')
    # The mapping belongs to the registered checkout only. Any other path is
    # checked against its own repo_root; a previous mapping never survives.
    if team_root and enrolled.get('workspace_registry_path') == str(Path(team_root['checkout_root'])/'ssot/registry.yaml'):
        enrolled['workspace_registry_root'] = team_root
    else:
        enrolled.pop('workspace_registry_root', None)
    candidate = runtime_candidate_registry(enrolled, manifest,
        harness_root=Path(env['HARNESS_ROOT']) if env.get('HARNESS_ROOT') else None, receipt_root=Path(env['PULSE_COMPUTECOST_RECEIPT_DIR']),
        relay_config=Path(env['RELAY_CONFIG_PATH']) if env.get('RELAY_CONFIG_PATH') else None)
    native_policy_sha = None
    if site == ORACLE_SITE:
        if env.get('runtime_observation') == 'configured_inactive':
            raise ValueError('Oracle source preparation requires the running Relay')
        candidate, native_policy_sha = oracle_native_usage(home, manifest, candidate)
        for source in candidate['sources']:
            if source.get('runtime_manifest') and source['kind'] == 'pi':
                source['node'] = ORACLE_SITE
        candidate = oracle_actor_sources(home, candidate, surfaces, env)
    # An already-enabled actor reader must receive this release's registered
    # workspace rules along with its code. This does not enable a new feature.
    if any(s.get('actor_attribution') is True for s in candidate['sources']):
        from pulse_compute_cost.source_registration import actor_candidate_registry
        mapped = actor_candidate_registry(candidate, surfaces, site=site)
        _adopt_team_digest(candidate, mapped)
        for source, current in zip(candidate['sources'], mapped['sources']):
            if source.get('actor_attribution') is not True or 'workspace_actors' not in current:
                continue
            rules = {r['workspace_root']:r for r in source.get('workspace_actors', [])}
            for rule in current['workspace_actors']:
                prior = rules.get(rule['workspace_root'])
                if prior and any(prior[k] != rule[k] for k in ('agent','node','runtime')):
                    raise ValueError('registered workspace ownership changed; explicit actor review required')
                rules[rule['workspace_root']] = rule
            source['workspace_actors'] = list(rules.values())
    for source in candidate['sources']:
        source['label'] = site+'.'+('runtime.pi' if source.get('runtime_manifest') and source['kind']=='pi' else source['kind'])
    desired = json.dumps(candidate, sort_keys=True, separators=(',',':')).encode()+b'\n'
    metadata = {'revision': revision, 'before_sha256':hashlib.sha256(original).hexdigest(),
        'after_sha256':hashlib.sha256(desired).hexdigest(), 'manifest':str(manifest),
        'manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest(),
        'runtime_observation':env.get('runtime_observation','loaded_configuration')}
    if native_policy_sha:
        metadata['native_policy_sha256'] = native_policy_sha
    if attempt is not None:
        metadata['attempt'] = attempt
    for path, data in ((before,original),(after,desired),(plan,json.dumps(metadata,sort_keys=True).encode())):
        if path.exists():
            if path.is_symlink() or path.read_bytes()!=data:
                raise ValueError('source plan already exists with different evidence')
        else:
            atomic(path,data)
    return {k:v for k,v in metadata.items() if k!='manifest'}


def require_retained_purpose_cutover(before, after):
    """A generic rollback cannot switch modern requests back to scripts."""
    for source in before.get('sources', []):
        if source.get('layout') == 'codex_calendar_months.v1':
            matches = [s for s in after.get('sources', []) if s.get('id') == source.get('id')]
            if len(matches) != 1 or any(matches[0].get(k) != source.get(k)
                    for k in ('layout','calendar_root','calendar_from')):
                raise ValueError('source rollback cannot erase native calendar enrollment')
        cutoff = source.get('session_wi_mcp_since')
        if cutoff is None:
            continue
        identity = source.get('id')
        matches = [s for s in after.get('sources', []) if s.get('id') == identity]
        if identity is None or len(matches) != 1 or matches[0].get('session_wi_mcp_since') != cutoff:
            raise ValueError('source rollback cannot erase or move native purpose cutover')


def source_plan_inputs(home, revision, *, restore=False, attempt=None):
    """Validate the retained plan without selecting or changing its registry."""
    registry,before,after,plan = _source_plan_paths(home,revision,attempt)
    for path in (registry,before,after,plan):
        st=path.lstat()
        if path.is_symlink() or not path.is_file() or st.st_uid!=os.getuid() or st.st_mode & 0o077:
            raise ValueError('source plan is not private regular owner data')
    metadata=json.loads(plan.read_bytes());old=before.read_bytes();new=after.read_bytes()
    if metadata.get('attempt') != attempt:
        raise ValueError('source plan belongs to a different deployment attempt')
    if metadata['revision']!=revision or hashlib.sha256(old).hexdigest()!=metadata['before_sha256'] or hashlib.sha256(new).hexdigest()!=metadata['after_sha256']:
        raise ValueError('source plan digest mismatch')
    if json.loads(old)['ledger_root']!=json.loads(new)['ledger_root']:
        raise ValueError('source adoption cannot move the ledger')
    manifest=Path(metadata['manifest'])
    if not restore and (manifest.is_symlink() or manifest.stat().st_uid!=os.getuid() or hashlib.sha256(manifest.read_bytes()).hexdigest()!=metadata['manifest_sha256']):
        raise ValueError('runtime manifest changed after source validation')
    if not restore and 'native_policy_sha256' in metadata:
        policy = (home/'.local/share/pulse/tools/scl-core/active/host-policy.json').resolve(strict=True)
        if hashlib.sha256(retention_bytes(policy, private=True)).hexdigest() != metadata['native_policy_sha256']:
            raise ValueError('Oracle native source bindings changed after source validation')
    return registry, old, new


def select_sources(home, revision, *, restore=False, attempt=None):
    """Compare-and-swap only this tested registry; never rewrite ledger facts."""
    registry, old, new = source_plan_inputs(home, revision, restore=restore, attempt=attempt)
    expected,desired=(new,old) if restore else (old,new)
    current=registry.read_bytes()
    if current==desired:
        return {'changed':False,'restored':restore}
    if current!=expected:
        raise ValueError('source registry changed concurrently')
    require_retained_purpose_cutover(json.loads(current), json.loads(desired))
    # A restored selector cannot erase the format already retained in its ledger.
    needed = set(json.loads(current).get('required_receipt_features', []))
    target_registry = json.loads(desired)
    if needed:
        target_registry['required_receipt_features'] = sorted(needed | set(target_registry.get('required_receipt_features', [])))
        desired = json.dumps(target_registry, sort_keys=True, separators=(',', ':')).encode()+b'\n'
    verify_reader_features((home/'.local/share/pulse/stats/current').resolve(), target_registry)
    atomic(registry,desired)
    return {'changed':True,'restored':restore}


def peer_command(peer, argv):
    return ['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=8', peer['host'], shlex.join(peer.get('prefix', []) + argv)]


def select_first_oracle_runtime(home, revision):
    """Select the first staged Stats reader without changing observation inputs."""
    if home != ORACLE_HOME or os.uname().nodename.split('.')[0] != ORACLE_SITE or not SHA.fullmatch(revision):
        raise ValueError('first Stats selection is scoped to the registered Oracle account')
    canonical_python(home)
    state = home / '.local/state/pulse/stats'
    current = home / '.local/share/pulse/stats/current'
    target = current.parent / 'releases' / revision
    if state.parent.resolve(strict=True) != state.parent or state.parent.stat().st_uid != os.getuid():
        raise ValueError('Oracle Stats requires the managed account state directory')
    retention_directory(state)
    with writer_lock(state / 'deployment.lock', timeout=0):
        if target.resolve(strict=True) != target or retention_bytes(target / '.stats-source-revision').decode().strip() != revision:
            raise ValueError('Oracle Stats reader is not staged')
        registry_path = 'config/scl-surfaces.yaml'
        surfaces = retention_bytes(target / registry_path)
        sealed = json.loads(retention_bytes(target / '.stats-files.json'))
        if (sealed.get(registry_path) != hashlib.sha256(surfaces).hexdigest()
                or not any(row.get('deployment', {}).get('site') == ORACLE_SITE
                           for row in json.loads(surfaces)['surfaces'])):
            raise ValueError('staged Stats reader does not contain the Oracle surface registration')
        registry = json.loads(retention_bytes(home / '.local/state/pulse/compute-cost/sources.json', private=True))
        if not registry.get('observation_core_entrypoint'):
            raise ValueError('Oracle native observation authority has not been adopted')
        verify_reader_features(target, registry)
        if not os.path.lexists(current):
            current.symlink_to(target)
        if current_revision(home) != revision:
            raise ValueError('Oracle Stats already uses a different release')
    return {'revision': revision, 'selector': str(current)}


def enroll_oracle_peer(args):
    """Enroll a verified Oracle reader in the existing publisher without restarting it."""
    import runpy
    home = PUBLISHER_HOME
    if Path.home() != home or not SHA.fullmatch(args.revision) or not SHA.fullmatch(args.expected):
        raise ValueError('Oracle enrollment requires the ownermac account and exact revisions')
    source = args.source.resolve(strict=True)
    revision = args.revision
    python = canonical_python(home)
    state = home / '.local/state/pulse/stats'
    config_path = state / 'refresh.json'
    receipt_dir = state / 'peer-enrollment'
    receipt_path = receipt_dir / (revision + '.oracle.json')
    intent_path = receipt_dir / (revision + '.oracle-prepared.json')
    peer_root = f'/home/pulse/.local/share/pulse/stats/releases/{revision}'
    peer = {'id': ORACLE_SITE, 'host': ORACLE_PEER_HOST, 'python': PEER_PYTHON,
            'script': peer_root + '/Scripts/pulse-stats-cost-refresh.py', 'transport': 'shared_ledger'}
    auth = json.loads(run([str(python), '-I', '-B', str(source / 'Scripts/pulse-gh.py'),
        '--transport', 'local', 'run', '--', 'api', '--method', 'GET',
        'repos/bigbird-online/PULSE/actions/workflows/default-branch-verification.yml/runs',
        '-f', f'head_sha={revision}', '-f', 'event=push', '-f', 'status=success',
        '--jq', '.workflow_runs | map(.id)']))
    if not auth:
        raise ValueError('Oracle Stats candidate has no successful exact-main verification')
    with writer_lock(state / 'deployment.lock', timeout=0):
        require_settled_observe_mode(state)
        require_settled_incremental_mode(state)
        previous = retention_bytes(config_path, private=True)
        config = json.loads(previous)
        if config['source_revision'] != args.expected:
            raise ValueError('existing publisher changed before Oracle enrollment')
        for existing in configured_peers(config):
            if existing.get('id') == peer['id'] or existing.get('host') == peer['host']:
                if existing != peer or existing is config.get('destination'):
                    raise ValueError('existing Oracle peer differs')
                if os.path.lexists(receipt_path):
                    return json.loads(retention_bytes(receipt_path, private=True))
                intent = json.loads(retention_bytes(intent_path, private=True))
                if intent.get('config_sha256') != hashlib.sha256(previous).hexdigest():
                    raise ValueError('Oracle enrollment intent does not match the publisher')
                receipt = {**intent, 'status': 'enrolled'}
                retention_once(receipt_path, json.dumps(receipt, sort_keys=True).encode())
                return receipt
        job = plistlib.loads((home / 'Library/LaunchAgents' / f'{LABEL}.plist').read_bytes())
        require_publisher_arguments(f'gui/{os.getuid()}', job['ProgramArguments'])
        registry = json.loads(retention_bytes(home / '.local/state/pulse/compute-cost/sources.json', private=True))
        active = native_publisher(home, args.expected, job['ProgramArguments']) or selected_publisher_runtime(home, args.expected, job)
        api = runpy.run_path(str(active / 'Scripts/pulse-stats-cost-refresh.py'))
        identity = api['shared_ledger_identity'](registry)
        data = archive(source, revision)
        require_installer_archive(data)
        run(peer_command(peer, [PEER_PYTHON, '-I', '-B', '-c', peer_stage_receiver(), revision]), data=data)
        selected = json.loads(run(peer_command(peer, [PEER_PYTHON, '-I', '-B', peer_root + '/Stats/deploy.py',
                                                       'select-first-oracle-runtime', '--revision', revision])))
        if selected.get('revision') != revision:
            raise ValueError('Oracle Stats selection readback differs')
        observed = run(peer_command(peer, [PEER_PYTHON, '-I', '-B', peer['script'], 'collect-shared']), timeout=900)
        collection = api['validate_shared_collection'](observed, identity)
        with writer_lock(state / 'cost-refresh.lock'):
            if retention_bytes(config_path, private=True) != previous:
                raise ValueError('publisher configuration changed during Oracle preparation')
            retention_directory(receipt_dir)
            digest = hashlib.sha256(previous).hexdigest()
            retention_once(receipt_dir / ('before-' + digest + '.json'), previous)
            config['peers'].append(peer)
            desired = (json.dumps(config, sort_keys=True, indent=2) + '\n').encode()
            intent = {'status': 'prepared', 'peer': peer['id'], 'revision': revision,
                      'publisher_revision': args.expected, 'before_sha256': digest,
                      'config_sha256': hashlib.sha256(desired).hexdigest(), 'collection': collection}
            if os.path.lexists(intent_path):
                saved = json.loads(retention_bytes(intent_path, private=True))
                if {**saved, 'collection': collection} != intent:
                    raise ValueError('retained Oracle enrollment intent differs')
                intent = saved
            else:
                retention_once(intent_path, json.dumps(intent, sort_keys=True).encode())
            atomic(config_path, desired)
            if retention_bytes(config_path, private=True) != desired:
                raise ValueError('Oracle Stats enrollment readback differs')
            receipt = {**intent, 'status': 'enrolled'}
            retention_once(receipt_path, json.dumps(receipt, sort_keys=True).encode())
            return receipt


def _publisher_wait_progress(path, waited, timeout):
    """Report a lock wait on stderr; stdout stays the machine-read receipt."""
    event = {'schema': 'stats_deploy_wait/v1', 'lock': Path(path).name,
             'waited_seconds': round(waited), 'limit_seconds': timeout}
    try:
        if Path(path).name != 'cost-refresh.lock':
            raise OSError('not the publisher cycle lock')
        cycle = json.loads((Path(path).parent/'cost-refresh-cycle.json').read_bytes())
        if isinstance(cycle.get('started_at'), str):
            event['cycle_started_at'] = cycle['started_at']
    except (OSError, ValueError, AttributeError):
        pass
    print(json.dumps(event), file=sys.stderr, flush=True)


@contextmanager
def writer_lock(path, timeout=PUBLISHER_WAIT_SECONDS):
    """Share the real publisher lock; never terminate an in-flight collector."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+b') as lock:
        started = time.monotonic()
        deadline = started + timeout
        reported = started
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                now = time.monotonic()
                if now >= deadline:
                    raise TimeoutError('current publisher did not finish within %d seconds' % timeout)
                if now - reported >= PUBLISHER_WAIT_PROGRESS_SECONDS:
                    _publisher_wait_progress(path, now - started, timeout)
                    reported = now
                time.sleep(.2)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


RUNTIME_SWITCH_SCHEMA = 'stats_runtime_switch/v2'


def selector_identity(home):
    path = home/'.local/share/pulse/stats/current'
    stat = path.lstat()
    return {'device': stat.st_dev, 'inode': stat.st_ino, 'mtime_ns': stat.st_mtime_ns,
            'target': os.readlink(path)}


def unfinished_runtime_switch(state, own=None):
    """A runtime switch whose intent is on disk but not finished blocks every other selector writer."""
    for path in sorted(state.glob('runtime-switch-*.json')):
        if path == own:
            continue
        receipt = json.loads(path.read_bytes())
        if receipt.get('schema') == RUNTIME_SWITCH_SCHEMA and receipt.get('status') in {'switching', 'restoring'}:
            raise ValueError('unfinished runtime switch '+path.name+'; retry or restore-runtime that attempt first')


def publisher_config_bytes(home):
    """The Oracle publisher configuration this selector governs; None where there is none."""
    path = home/'.local/state/pulse/stats/refresh.json'
    if home != ORACLE_HOME or not (path.exists() or path.is_symlink()):
        return None
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('private regular publisher configuration required')
    return path.read_bytes()


def publisher_config_for(data, revision):
    """refresh.json retargeted to the reader release its timer runs (stats/current)."""
    original = json.loads(data)
    updated = peer_scripts({**original, 'source_revision': revision}, revision)
    return data if updated == original else json.dumps(updated, indent=2).encode()


def retarget_publisher_config(home, revision):
    data = publisher_config_bytes(home)
    if data is None:
        return None
    updated = publisher_config_for(data, revision)
    if updated != data:
        atomic(home/'.local/state/pulse/stats/refresh.json', updated)
    return revision


def _sha(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def switch_peer_runtime(home, revision, expected, *, attempt, restore=False):
    """Move selector and Oracle publisher configuration together, intent first.

    The receipt records both pre-images and targets before anything is
    written. Configuration changes before the selector. Any partial state is
    therefore a pair inside {pre-image, target}; a publisher cycle in it reads
    a release and configuration that differ and exits without publishing. A
    retry of the same attempt completes the target; restore-runtime returns
    the pre-image.
    """
    _source_plan_paths(home, revision, attempt)
    state = home/'.local/state/pulse/stats'
    path = state/('runtime-switch-'+revision+'--'+attempt+'.json')
    config_path = state/'refresh.json'
    with writer_lock(state/'deployment.lock', timeout=0), publisher_cycle(home):
        unfinished_runtime_switch(state, own=path)
        receipt = json.loads(path.read_bytes()) if path.exists() else None
        identity = {'revision': revision, 'previous': expected, 'attempt': attempt}
        if receipt is not None and any(receipt.get(k) != v for k, v in identity.items()):
            raise ValueError('peer switch belongs to a different deployment')
        before = current_revision(home)
        status = None if receipt is None else receipt.get('status')
        ours = receipt is not None and receipt.get('schema') == RUNTIME_SWITCH_SCHEMA

        def write(**fields):
            atomic(path, json.dumps({**receipt, **fields}).encode())
            receipt.update(fields)

        def owned_pair():
            # Another selector writer is fenced by unfinished_runtime_switch, so a
            # selector at either end belongs to this attempt; a 'selected' one
            # must still be the link this attempt made.
            if before not in {expected, revision}:
                raise ValueError('peer selector changed after this deployment')
            if (status == 'selected' and before == revision
                    and receipt.get('selector') != selector_identity(home)):
                raise ValueError('peer selector changed after this deployment')
            config = receipt['config']
            if config['before'] is not None:
                if _sha(publisher_config_bytes(home)) not in {config['before_sha256'], config['after_sha256']}:
                    raise ValueError('publisher configuration changed after this deployment')
            elif home == ORACLE_HOME and (config_path.exists() or config_path.is_symlink()):
                raise ValueError('publisher configuration changed after this deployment')

        if restore:
            if receipt is None or status == 'restored':
                if before != expected:
                    raise ValueError('peer switch ownership is unconfirmed')
                return {'previous': before, 'selected': before}
            if status not in {'switching', 'selected', 'restoring'} or (status == 'restoring' and not ours):
                raise ValueError('peer switch receipt state is invalid')
            if not ours:  # Receipt from a release that did not record the configuration pre-image.
                if before != expected and receipt.get('selector') != selector_identity(home):
                    raise ValueError('peer selector changed after this deployment')
                select_runtime(home, expected, revision)
                retarget_publisher_config(home, expected)
                atomic(path, json.dumps({**identity, 'status': 'restored'}).encode())
                return {'previous': before, 'selected': expected}
            owned_pair()
            write(status='restoring')
            select_runtime(home, expected, revision)
            config = receipt['config']
            if config['before'] is not None and _sha(publisher_config_bytes(home)) != config['before_sha256']:
                atomic(config_path, config['before'].encode())
            write(status='restored')
            return {'previous': before, 'selected': expected}
        if status == 'restoring':
            raise ValueError('peer switch is being restored; finish restore-runtime first')
        if status in {'switching', 'selected'} and before == revision and (not ours or status == 'selected'):
            if receipt.get('selector') != selector_identity(home):
                raise ValueError('peer selector changed after this deployment')
            return {'previous': expected, 'selected': revision}
        if not (ours and status == 'switching'):
            preflight_runtime(home, revision, expected)
            config = publisher_config_bytes(home)
            target = None if config is None else publisher_config_for(config, revision)
            receipt = {**identity, 'schema': RUNTIME_SWITCH_SCHEMA, 'status': 'switching',
                       'config': {'before': None if config is None else config.decode(),
                                  'before_sha256': _sha(config),
                                  'after': None if target is None else target.decode(),
                                  'after_sha256': _sha(target)}}
            atomic(path, json.dumps(receipt).encode())
            status = 'switching'
        owned_pair()
        config = receipt['config']
        if config['after'] is not None and _sha(publisher_config_bytes(home)) != config['after_sha256']:
            atomic(config_path, config['after'].encode())
        select_runtime(home, revision, expected)
        write(status='selected', selector=selector_identity(home))
        return {'previous': expected, 'selected': revision}


def select_without_attempt(home, revision, expected):
    """Public select without a deployment attempt moves the selector alone.

    It cannot carry an Oracle publisher configuration with it, so where one is
    installed it refuses before any write, and again under the locks.
    """
    def refuse_publisher():
        if publisher_config_bytes(home) is not None:
            raise ValueError('Oracle publisher is installed: use select --source-plan-attempt <attempt>, '
                             'which moves refresh.json with the selector')
    refuse_publisher()
    state = home/'.local/state/pulse/stats'
    with writer_lock(state/'deployment.lock', timeout=0), publisher_cycle(home):
        refuse_publisher()
        unfinished_runtime_switch(state)
        return {'previous': select_runtime(home, revision, expected), 'selected': revision}


def publisher_cycle(home, timeout=None):
    """On Oracle, the publisher's cycle lock: a running cycle ends before the
    reader selector or its configuration changes, and none starts meanwhile.
    Taken after deployment.lock, as every publisher writer here does."""
    if home != ORACLE_HOME:
        return nullcontext()
    return writer_lock(home/'.local/state/pulse/stats/cost-refresh.lock',
                       timeout=PUBLISHER_WAIT_SECONDS if timeout is None else timeout)


def checked_observe_sources(ids):
    """Source IDs an observing pusher's bundles may carry, as the publisher's collection checks them."""
    if (not isinstance(ids, list) or not ids or len(set(map(str, ids))) != len(ids)
            or any(not isinstance(i, str) or not OBSERVE_SOURCE_ID.fullmatch(i) for i in ids)):
        raise ValueError('observe_sources must list source IDs')
    return list(ids)


def collect_local_mode(config):
    mode = config.get('collect_local_mode', 'full')
    if mode not in COLLECT_LOCAL_MODES:
        raise ValueError('collect_local_mode must be full, observe or incremental')
    return mode


def pushing_peer_observe_sources(config, registry):
    """ownermac's registration as an observing pusher: its source IDs in observe mode, else None."""
    if collect_local_mode(config) != 'observe':
        return None
    return checked_observe_sources([s.get('id') for s in registry.get('sources', [])])


def observing_registration(peers, paused):
    """Observing pusher ID -> the source IDs the publisher judges for it; paused ones are not judged."""
    return {p['id']: p['observe_sources'] for p in peers if 'observe_sources' in p and p['id'] not in paused}


def oracle_publisher_config(payload, revision, state):
    """The Oracle publisher's refresh.json, from the inputs ownermac publishes with."""
    # A paused peer stays registered and paused there: Oracle never reaches it either.
    paused = paused_peers(payload)
    peers = [{k: v for k, v in peer.items() if k != 'prefix'} for peer in payload['peers']
             if peer.get('id') != ORACLE_SITE]  # Oracle reaches peers as the pulse account.
    features = payload['required_receipt_features']
    if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
        raise ValueError('publisher receipt features required')
    pusher = {'id': PUSHING_PEER, 'transport': 'shared_ledger_push'}
    if payload.get('observe_sources') is not None:  # ownermac observes: judge its bundles here.
        pusher['observe_sources'] = checked_observe_sources(payload['observe_sources'])
    config = {'source_revision': revision, 'publication_validity_minutes': ORACLE_PUBLISHER_VALIDITY_MINUTES,
              'hold_collect_shared_lock': True, 'paused_peers': paused,
              'peers': [*peers, pusher],
              'registry_overlay': {
                  'required_receipt_features': sorted(features),
                  'tenant_registry_path': str(state/'tenant-registry.json'),
                  'workspace_registry_path': str(ORACLE_WORKSPACE_REGISTRY),
                  'sources': [{'id': CORE_OUTCOME_SOURCE, 'kind': 'harness_core',
                               'path': str(state/'wi-facts-v1.json'), 'label': ORACLE_SITE+'.harness_core'}]}}
    if payload.get('kernel_merge_enforced_since'):
        config['kernel_merge_enforced_since'] = str(payload['kernel_merge_enforced_since'])
    if payload.get('incremental') is not None:
        section = incremental_section({'incremental': payload['incremental']})
        if section is not None:
            config['incremental'] = section
    return peer_scripts(config, revision)


def incremental_modes(config):
    """refresh.json `incremental` as ComputeCost of this release parses it (every switch)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'ComputeCost'))
    from pulse_compute_cost.incremental_modes import incremental_modes as parse
    return parse(config)


def incremental_section(config):
    """The section an Oracle configuration carries: None when every switch is off."""
    modes = incremental_modes(config)
    if set(modes.values()) == {'off'}:
        return None
    # An Oracle release before the week close rejects the key: carry it only when on.
    return {k: v for k, v in modes.items() if k != 'close' or v != 'off'}


def oracle_publisher_units(runtime, home):
    """The unit files as installed: the repository templates with this account's paths."""
    units = {}
    for name in ORACLE_PUBLISHER_UNITS:
        text = retention_bytes(runtime/'Stats/systemd'/name).decode()
        units[name] = text.replace('@HOME@', str(home)).replace('@PYTHON@', PEER_PYTHON).encode()
    return units


def receive_oracle_publisher(home, revision, data):
    """On Oracle: install the publisher configuration, tenant registry and units; never start them."""
    if home != ORACLE_HOME or os.uname().nodename.split('.')[0] != ORACLE_SITE:
        raise ValueError('Oracle publisher installation requires the registered service account')
    payload = json.loads(data)
    tenants = canonical_json(payload['tenant_registry'])
    if len(tenants) > TENANT_REGISTRY_MAX_BYTES or payload['tenant_registry'].get('schema') != 'pulse_stats_tenant_registry/v2':
        raise ValueError('tenant registry required')
    state = home/'.local/state/pulse/stats'
    config = oracle_publisher_config(payload, revision, state)
    unit_dir = home/'.config/systemd/user'
    with writer_lock(state/'deployment.lock', timeout=0), publisher_cycle(home, ORACLE_CYCLE_WAIT_SECONDS):
        # A reader switch holds these same locks, so the selector stays this release.
        unfinished_runtime_switch(state)
        if current_revision(home) != revision:
            raise ValueError('Oracle publisher must run the selected reader release')
        units = oracle_publisher_units(home/'.local/share/pulse/stats/releases'/revision, home)
        written = {}
        for path, body in ((state/'tenant-registry.json', tenants),
                           (state/'refresh.json', json.dumps(config, indent=2).encode()),
                           *((unit_dir/name, body) for name, body in units.items())):
            if path.exists() or path.is_symlink():
                if path.is_symlink() or path.stat().st_uid != os.getuid():
                    raise ValueError('owned regular publisher file required')
                before = path.read_bytes()
                if before == body:
                    written[path.name] = 'unchanged'
                    continue
                atomic(path.with_name(path.name+'.before-'+hashlib.sha256(before).hexdigest()[:16]), before)
            atomic(path, body, mode=0o600 if path.parent == state else 0o644)
            written[path.name] = 'written'
        run(['systemctl', '--user', 'daemon-reload'])
    # Read back what the publisher now collects: the pause table it took and the
    # observing pushers it will judge from their spools (native_push sources).
    observing = observing_registration(config['peers'], config['paused_peers'])
    return {'revision': revision, 'files': written, 'timer_started': False,
            'paused_peers': config['paused_peers'], 'observing_peers': sorted(observing),
            'observe_sources': observing, **({'incremental': config['incremental']} if 'incremental' in config else {})}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode() + b'\n'


def install_oracle_publisher(home, revision):
    """On ownermac: hand Oracle the publication inputs this publisher uses today.

    deployment.lock is held from reading refresh.json to the readback, so an
    install never lands after an observe-mode or peer-collection change that
    it did not read; while either holds it the install refuses.
    It installs only the reader Oracle selected, and leaves refresh.json's
    Oracle entry on that reader: an install after a reader switch there
    retargets this node's pulls and pushes too.
    """
    state = home/'.local/state/pulse/stats'
    config_path = state/'refresh.json'
    with writer_lock(state/'deployment.lock', timeout=0):
        require_settled_observe_mode(state)
        require_settled_incremental_mode(state)
        before = retention_bytes(config_path, private=True)
        config = json.loads(before)
        if oracle_publisher_revision(config) != revision:
            raise ValueError('Oracle installs only the Stats reader it selected')
        result = install_oracle_publisher_locked(home, revision, config)
        updated = oracle_peer_entry(config, revision)
        if updated != config:
            # deployment.lock excludes every other refresh.json writer; readers see one whole file.
            if retention_bytes(config_path, private=True) != before:
                raise ValueError('refresh.json changed by another writer')
            atomic(config_path, json.dumps(updated, indent=2).encode())
        return result


# Read-only, over SSH as the Oracle account: the Stats reader stats/current
# selects there, whether that reader carries the publisher installer and the
# cost-refresh entry ownermac runs there (fetch-publication, the observation
# pushes), and the release the installed publisher configuration names. Plain
# Python, so it answers whatever reader Oracle runs.
ORACLE_SELECTION_PROBE = """import json, pathlib, re
home = pathlib.Path(%r)
current = home/'.local/share/pulse/stats/current'
answer = {'selected': None, 'installer': False, 'publisher_revision': None}
try:
    runtime = current.resolve(strict=True)
    revision = (runtime/'.stats-source-revision').read_text().strip()
    if current.is_symlink() and re.fullmatch('[0-9a-f]{40}', revision) \\
            and runtime == (current.parent/'releases'/revision).resolve(strict=True):
        answer.update(selected=revision, installer=(runtime/'Stats/deploy.py').is_file()
                      and (runtime/'Scripts/pulse-stats-cost-refresh.py').is_file())
except OSError:
    pass
try:
    answer['publisher_revision'] = json.loads((home/'.local/state/pulse/stats/refresh.json').read_text()).get('source_revision')
except (OSError, ValueError, AttributeError):
    pass
print(json.dumps(answer))
""" % str(ORACLE_HOME)


# The refresh script's record of the highest bundle sequence Oracle acknowledged (observe mode).
OBSERVE_PUSHED = 'publisher-acknowledged.json'
# Oracle's spool of one observing peer: bundles not yet ingested and the highest ingested sequence.
ORACLE_SPOOL_PROBE = """import hashlib, json, pathlib, re
spool = pathlib.Path(%r)/'.local/state/pulse/stats/imports'/(%r + '.observations')
answer = {'pending': 0, 'consumed': 0}
digests = {}
try:
    record = json.loads((spool/'.consumed.json').read_bytes())
    answer['consumed'], digests = int(record['last']), dict(record['digests'])
except FileNotFoundError:
    pass
for path in sorted(spool.glob('*.json.gz')) if spool.is_dir() else []:
    name = re.fullmatch(r'(\\d{20})\\.json\\.gz', path.name)
    if name and digests.get(str(int(name.group(1)))) != hashlib.sha256(path.read_bytes()).hexdigest():
        answer['pending'] += 1
print(json.dumps(answer))
"""


def oracle_observation_backlog(config, peer_id):
    """{'pending', 'consumed'} of the Oracle publisher's spool for ``peer_id`` (read only)."""
    oracle = [p for p in config['peers'] if p.get('id') == ORACLE_SITE]
    if len(oracle) != 1:
        raise ValueError('Oracle must be a registered peer')
    probe = ORACLE_SPOOL_PROBE % (str(ORACLE_HOME), peer_id)
    answer = json.loads(run(peer_command(oracle[0], [PEER_PYTHON, '-I', '-B', '-c', probe]), timeout=60))
    if (not isinstance(answer, dict) or any(type(answer.get(k)) is not int or answer[k] < 0
                                            for k in ('pending', 'consumed'))):
        raise ValueError('Oracle observation spool unreadable')
    return answer


def require_observations_ingested(home, config, started_in, mode):
    """Before a switch changes who judges ownermac's bundles: none is left without a consumer.

    Leaving observe, every bundle Oracle acknowledged (OBSERVE_PUSHED) is
    ingested there and its spool holds none waiting; leaving
    incremental, the local spool holds none waiting. Collect-local cannot run
    meanwhile (the caller holds the cycle lock), so nothing new arrives.
    """
    if started_in == mode:
        return
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'ComputeCost'))
    if started_in == 'observe':
        # The publisher's acknowledgements only: incremental mode moves the observe index locally.
        observe = home/'.local/state/pulse/stats/observe'
        pushed = observe/OBSERVE_PUSHED
        if pushed.exists():
            cursor = json.loads(retention_bytes(pushed, private=True)).get('sequence', 0)
        else:
            # A node upgraded from before the acknowledgement file: there only
            # the publisher's acknowledgements moved the observe index.
            from pulse_compute_cost.native_observe import load_index
            cursor = load_index(observe/'index.json')['sequence']
        backlog = oracle_observation_backlog(config, PUSHING_PEER)
        if backlog['pending'] or backlog['consumed'] < cursor:
            raise ValueError('Oracle has not yet ingested every acknowledged bundle of ownermac '
                             f"(pending {backlog['pending']}, ingested to {backlog['consumed']}, sent to {cursor}); "
                             'run observe-mode again after its next cycle')
    if started_in == 'incremental':
        from pulse_compute_cost.fact_index import index_path
        from pulse_compute_cost.native_observations import consumed
        from pulse_compute_cost.observation_authority import registered_ledger
        import hashlib as _hashlib
        registry = json.loads(retention_bytes(home/'.local/state/pulse/compute-cost/sources.json', private=True))
        spool = index_path(registered_ledger(registry, read_only=True)).parent/'local-spool'
        done = consumed(spool)['digests'] if spool.is_dir() else {}
        waiting = [p for p in sorted(spool.glob('*.json.gz')) if spool.is_dir()
                   and done.get(str(int(p.name[:20]))) != _hashlib.sha256(p.read_bytes()).hexdigest()]
        if waiting:
            raise ValueError(f'{len(waiting)} local observation bundles wait to be judged; '
                             'run collect-local in incremental mode first')


def oracle_publisher_revision(config, *, installed=False):
    """The Stats reader Oracle selected: the release its publisher runs and installs from.

    Oracle's publisher follows its own reader selection (a reader switch there
    retargets it), which need not be ownermac's native release. Refuses unless
    Oracle has selected a reader carrying the installer and the cost-refresh
    entry; ``installed`` also requires the publisher configuration there to
    name that reader.
    """
    oracle = [p for p in config['peers'] if p.get('id') == ORACLE_SITE]
    if len(oracle) != 1:
        raise ValueError('Oracle must be a registered peer')
    answer = json.loads(run(peer_command(oracle[0], [PEER_PYTHON, '-I', '-B', '-c', ORACLE_SELECTION_PROBE]),
                            timeout=60))
    selected = answer.get('selected') if isinstance(answer, dict) else None
    if not isinstance(selected, str) or not SHA.fullmatch(selected) or answer.get('installer') is not True:
        raise ValueError('Oracle has no selected Stats reader with the publisher installer and entry')
    if installed and answer.get('publisher_revision') != selected:
        raise ValueError('Oracle publisher is not installed on its selected Stats reader')
    return selected


def oracle_entry(revision):
    """The cost-refresh entry of one Stats reader release on Oracle."""
    return '/home/pulse/.local/share/pulse/stats/releases/'+revision+'/Scripts/pulse-stats-cost-refresh.py'


def oracle_peer_entry(config, revision):
    """``config`` with its Oracle peer running ``revision``, the reader Oracle selected.

    Every ownermac step that reaches Oracle (fetch-publication, the observation
    pushes) runs this entry; an entry on any other release may already be gone there.
    """
    result = deepcopy(config)
    oracle = [p for p in result['peers'] if p.get('id') == ORACLE_SITE]
    if len(oracle) != 1:
        raise ValueError('Oracle must be a registered peer')
    if not isinstance(oracle[0].get('script'), str) or not re.fullmatch(
            r'/home/pulse/\.local/share/pulse/stats/releases/[0-9a-f]{40}/Scripts/pulse-stats-cost-refresh\.py',
            oracle[0]['script']):
        raise ValueError('unexpected peer execution path')
    oracle[0]['script'] = oracle_entry(revision)
    return result


def install_oracle_publisher_locked(home, revision, config):
    """The install for a caller holding ownermac's deployment.lock; ``config``
    stands for refresh.json (observe-mode passes the image it settles).
    ``revision`` is the Stats reader Oracle selected (oracle_publisher_revision)."""
    registry = json.loads(retention_bytes(home/'.local/state/pulse/compute-cost/sources.json', private=True))
    oracle = [p for p in config['peers'] if p.get('id') == ORACLE_SITE]
    if len(oracle) != 1:
        raise ValueError('Oracle must be a registered peer')
    payload = {'peers': config['peers'], 'paused_peers': paused_peers(config),
               'required_receipt_features': registry.get('required_receipt_features', []),
               'tenant_registry': json.loads(retention_bytes(Path(registry['tenant_registry_path']), private=True)),
               'kernel_merge_enforced_since': config.get('kernel_merge_enforced_since')}
    observed = pushing_peer_observe_sources(config, registry)
    if observed is not None:
        payload['observe_sources'] = observed
    incremental = incremental_section(config)
    if incremental is not None:
        payload['incremental'] = incremental
    installer = '/home/pulse/.local/share/pulse/stats/releases/'+revision+'/Stats/deploy.py'
    result = json.loads(run(peer_command(oracle[0], [PEER_PYTHON, '-I', '-B', installer, 'receive-oracle-publisher',
                                                     '--revision', revision]), data=canonical_json(payload),
                            timeout=ORACLE_CYCLE_WAIT_SECONDS+300))
    # peer-collection changes only this refresh.json; Oracle collects (and judges an
    # observing TCloud) by the table it holds, so the install fails unless it took this one.
    pushers = [p for p in config['peers'] if p.get('id') != PUSHING_PEER]
    if observed is not None:
        pushers.append({'id': PUSHING_PEER, 'observe_sources': observed})
    observing = observing_registration(pushers, payload['paused_peers'])
    if result.get('incremental') != incremental:
        raise ValueError('Oracle publisher readback differs from this incremental setting')
    if (result.get('paused_peers') != payload['paused_peers'] or result.get('observing_peers') != sorted(observing)
            or (PUSHING_PEER in observing
                and result.get('observe_sources', {}).get(PUSHING_PEER) != observing[PUSHING_PEER])):
        raise ValueError('Oracle publisher readback differs from this pause table')
    return result


def publisher_job_loaded(domain):
    """Whether launchd holds the publisher job; an unconfirmed absence is an error."""
    observed = subprocess.run(['/bin/launchctl', 'print', domain+'/'+LABEL],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if observed.returncode == 0:
        return True
    if observed.returncode == 113 and ('Could not find service "'+LABEL+'"').encode() in observed.stderr:
        return False
    raise ValueError('scheduler state is unconfirmed')


def switch_publisher(home, revision, target):
    """On ownermac, between cycles: run collect-local under the Oracle publisher, or the full refresh again.

    deployment.lock is held throughout, recovery included; refresh.json changes
    under the cycle lock and publication-pull.lock, so neither a cycle nor a
    publication pull that read the other publisher writes after it. A failure restores
    the before-image: configuration, job file, and the job loaded in launchd
    if it was. A recovery that fails leaves publisher-switch.json unresolved;
    the next run, to either publisher, continues that switch from the state it
    left and restores that same before-image if it fails again.
    """
    state = home/'.local/state/pulse/stats'
    config_path = state/'refresh.json'
    job_path = home/'Library/LaunchAgents'/f'{LABEL}.plist'
    receipt_path = state/PUBLISHER_SWITCH_RECEIPT
    domain = f'gui/{os.getuid()}'
    pull_lock = state/PUBLICATION_PULL_LOCK
    with writer_lock(state/'deployment.lock', timeout=0):
        # A pull in flight finishes before `publisher` changes, and none starts meanwhile.
        with writer_lock(state/'cost-refresh.lock'), writer_lock(pull_lock, timeout=PUBLICATION_PULL_WAIT_SECONDS):
            require_settled_observe_mode(state)
            require_settled_incremental_mode(state)
            current_config = json.loads(retention_bytes(config_path, private=True))
            current_job = plistlib.loads(retention_bytes(job_path))
            if (current_config.get('source_revision') != revision
                    or native_publisher(home, revision, current_job.get('ProgramArguments')) is None):
                raise ValueError('publisher must be the selected native release')
            if target != ORACLE_SITE and collect_local_mode(current_config) != 'full':
                # Only a full collection moves with the publisher; observe-mode full comes first.
                raise ValueError('ownermac does not collect in full for the Oracle publisher; '
                                 'run observe-mode --mode full first')
            if target == ORACLE_SITE:
                # Before any write: Oracle publishes from the reader it selected,
                # and this node's pulls and pushes run that reader's entry there.
                oracle_revision = oracle_publisher_revision(current_config, installed=True)
            prior = json.loads(retention_bytes(receipt_path, private=True)) if os.path.lexists(receipt_path) else None
            continued = prior is not None and (prior.get('status') == 'switching' or bool(prior.get('rollback_pending')))
            loaded = publisher_job_loaded(domain)
            if continued:
                if prior.get('revision') != revision:
                    raise ValueError('an unresolved publisher switch belongs to another release')
                try:
                    original = retention_bytes(state/('refresh-before-switch-'+prior['config_before_sha256'][:16]+'.json'), private=True)
                    original_job = retention_bytes(state/('launchagent-before-switch-'+prior['job_before_sha256'][:16]+'.plist'), private=True)
                except OSError:
                    raise ValueError('publisher switch before-image is missing') from None
                if (hashlib.sha256(original).hexdigest() != prior['config_before_sha256']
                        or hashlib.sha256(original_job).hexdigest() != prior['job_before_sha256']):
                    raise ValueError('publisher switch before-image differs')
                was_loaded = prior['job_was_loaded'] is True
            else:
                if not loaded:
                    raise ValueError('publisher job is not loaded')
                original, original_job, was_loaded = retention_bytes(config_path, private=True), retention_bytes(job_path), True
            config, job = json.loads(original), plistlib.loads(original_job)
            before_arguments = job.get('ProgramArguments')
            if config.get('source_revision') != revision or native_publisher(home, revision, before_arguments) is None:
                raise ValueError('publisher must be the selected native release')
            updated = {k: v for k, v in config.items() if k not in ('publisher', 'peer_id')}
            if target == ORACLE_SITE:
                updated = oracle_peer_entry(updated, oracle_revision)
                updated.update(publisher=ORACLE_SITE, peer_id=PUSHING_PEER)
            mode = 'collect-local' if target == ORACLE_SITE else 'refresh'
            job['ProgramArguments'] = native_publisher_arguments(home, revision, mode)
            if loaded:
                require_idle_publisher(domain)
                if not continued:
                    require_publisher_arguments(domain, before_arguments)
            receipt = {'schema': 'stats_publisher_switch/v1', 'revision': revision, 'publisher': target, 'mode': mode,
                       'started_at': datetime.now(timezone.utc).isoformat(), 'continued': continued,
                       'job_was_loaded': was_loaded, 'config_before_sha256': hashlib.sha256(original).hexdigest(),
                       'job_before_sha256': hashlib.sha256(original_job).hexdigest(), 'status': 'switching'}

            def record(**fields):
                receipt.update(fields)
                atomic(receipt_path, json.dumps(receipt, sort_keys=True).encode())

            def restore():
                # The caller holds the cycle lock; a running cycle is never stopped.
                if publisher_job_loaded(domain):
                    require_idle_publisher(domain)
                    run(['/bin/launchctl', 'bootout', domain+'/'+LABEL])
                atomic(config_path, original)
                atomic(job_path, original_job)
                if was_loaded:
                    run(['/bin/launchctl', 'bootstrap', domain, str(job_path)])
                    require_publisher_arguments(domain, before_arguments)

            def recover(exc, how):
                try:
                    how()
                except Exception as rollback_exc:
                    record(status='failed', error_type=type(exc).__name__,
                           rollback_errors=[type(rollback_exc).__name__], rollback_pending=True)
                else:
                    record(status='restored', error_type=type(exc).__name__, rollback_errors=[], rollback_pending=False)
                raise exc

            if not continued:
                atomic(state/('refresh-before-switch-'+receipt['config_before_sha256'][:16]+'.json'), original)
                atomic(state/('launchagent-before-switch-'+receipt['job_before_sha256'][:16]+'.plist'), original_job)
            record()
            try:
                if loaded:
                    run(['/bin/launchctl', 'bootout', domain+'/'+LABEL])
                atomic(config_path, json.dumps(updated, indent=2).encode())
                atomic(job_path, plistlib.dumps(job))
            except Exception as exc:
                recover(exc, restore)
        # After releasing the cycle lock, so RunAtLoad can take it.
        try:
            run(['/bin/launchctl', 'bootstrap', domain, str(job_path)])
            require_publisher_arguments(domain, job['ProgramArguments'])
        except Exception as exc:
            def locked_restore():
                with writer_lock(state/'cost-refresh.lock', timeout=0), \
                        writer_lock(pull_lock, timeout=PUBLICATION_PULL_WAIT_SECONDS):
                    restore()
            recover(exc, locked_restore)
        record(status='switched', completed_at=datetime.now(timezone.utc).isoformat())
    return {'revision': revision, 'publisher': target, 'mode': mode}


def set_peer_collection(home, peer_id, collection):
    """On ownermac, between cycles: pause or resume one registered peer's Stats collection.

    The publisher's next cycle reads the change. An Oracle publisher takes it
    from the next install-oracle-publisher; a resumed peer is reached by the
    release last staged there until the next deployment stages this one.
    """
    if collection not in ('paused', 'active'):
        raise ValueError('collection must be paused or active')
    state = home/'.local/state/pulse/stats'
    config_path = state/'refresh.json'
    with writer_lock(state/'deployment.lock', timeout=0), writer_lock(state/'cost-refresh.lock'):
        require_settled_observe_mode(state)
        require_settled_incremental_mode(state)
        original = retention_bytes(config_path, private=True)
        config = json.loads(original)
        paused = paused_peers(config)
        if peer_id not in [peer.get('id') for peer in config['peers']]:
            raise ValueError('peer is not registered here')
        if peer_id == config.get('publisher'):
            raise ValueError('the publisher itself cannot be paused')
        if collection == 'paused':
            paused[peer_id] = 'paused_by_decision'
        else:
            paused.pop(peer_id, None)
        updated = json.dumps({**config, 'paused_peers': paused}, indent=2).encode()
        if updated != original:
            atomic(state/('refresh-before-peer-collection-'+hashlib.sha256(original).hexdigest()[:16]+'.json'), original)
            atomic(config_path, updated)
    return {'peer': peer_id, 'collection': collection, 'paused_peers': paused}


def require_settled_incremental_mode(state):
    """Refuse while incremental-mode.json is unresolved: refresh.json may still be its image to restore."""
    path = state/INCREMENTAL_MODE_RECEIPT
    if os.path.lexists(path) and json.loads(retention_bytes(path, private=True)).get('status') not in OBSERVE_MODE_SETTLED:
        raise ValueError('an unresolved incremental-mode switch must finish first; run incremental-mode again')


def require_settled_observe_mode(state):
    """Refuse while observe-mode.json is unresolved: the two nodes may disagree on ownermac's mode."""
    path = state/OBSERVE_MODE_RECEIPT
    if os.path.lexists(path) and json.loads(retention_bytes(path, private=True)).get('status') not in OBSERVE_MODE_SETTLED:
        raise ValueError('an unresolved observe-mode switch must finish first; run observe-mode again')


def require_incremental_collection_ready(home):
    """Before ownermac judges its own sources (WI754): its fact index is built and a
    prefix of the ledger (caught up here), and no observation bundle waits unacknowledged
    in the observe outbox (the local spool continues from the observe index)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'ComputeCost'))
    from pulse_compute_cost.fact_index import FactIndex, IndexMismatch, index_path, stream_identity
    from pulse_compute_cost.native_observe import pending_bundles
    from pulse_compute_cost.observation_authority import registered_ledger
    registry = json.loads(retention_bytes(home/'.local/state/pulse/compute-cost/sources.json', private=True))
    ledger = registered_ledger(registry, read_only=True)
    path = index_path(ledger)
    if not path.is_file():
        raise ValueError('incremental mode needs the fact index; run pulse-compute-cost fact-index first')
    index = FactIndex.open(path, stream_identity(ledger))
    try:
        if index.binding().sequence == 0:
            raise ValueError('incremental mode needs the fact index; run pulse-compute-cost fact-index first')
        try:
            index.catch_up(ledger)
        except IndexMismatch as exc:
            raise ValueError('the fact index is not a prefix of the ledger; rebuild it') from exc
    finally:
        index.close()
    if pending_bundles(home/'.local/state/pulse/stats/observe/outbox'):
        raise ValueError('observation bundles wait unacknowledged; run collect-local in observe mode first')


def set_observe_mode(home, peer_id, mode):
    """On ownermac, between cycles: collect-local as a full collection, observe-only or incremental.

    ownermac's sources have exactly one writer: towards observe the Oracle
    publisher registers ownermac's native_push source first, then ownermac stops
    appending; back to full ownermac's registration on Oracle ends first, then
    full collection resumes. Both locks are held throughout, so no cycle runs in
    between. observe-mode.json records the before-image and the target before
    either node changes and stays unresolved until both nodes read back one of
    them; while it does, collect-local collects nothing and every deployment
    writer refuses. A failed step restores the before-image on both nodes; a
    restore that fails too leaves the receipt unresolved, and the next run
    continues it: to its target when asked for the target mode, else back to
    its before-image. A refresh.json another writer changed is never replaced.
    """
    if mode not in COLLECT_LOCAL_MODES:
        raise ValueError('collect_local_mode must be full, observe or incremental')
    if peer_id != PUSHING_PEER:
        raise ValueError('only the pushing peer ownermac switches its collection mode')
    state = home/'.local/state/pulse/stats'
    config_path = state/'refresh.json'
    receipt_path = state/OBSERVE_MODE_RECEIPT
    with writer_lock(state/'deployment.lock', timeout=0), \
            writer_lock(state/'cost-refresh.lock', timeout=PUBLISHER_WAIT_SECONDS):
        require_settled_incremental_mode(state)
        prior = json.loads(retention_bytes(receipt_path, private=True)) if os.path.lexists(receipt_path) else None
        if prior is not None and prior.get('status') not in OBSERVE_MODE_SETTLED:
            # Continue that switch from its own images, whatever the file holds now.
            before, target = prior['before_config'].encode(), prior['target_config'].encode()
            if (hashlib.sha256(before).hexdigest() != prior['before_sha256']
                    or hashlib.sha256(target).hexdigest() != prior['target_sha256']):
                raise ValueError('observe-mode receipt differs')
            oracle_revision = oracle_publisher_revision(json.loads(before))
            if mode == prior['to'] and oracle_peer_entry(json.loads(target), oracle_revision) != json.loads(target):
                # Oracle selected another reader since; the target would name an entry it left.
                raise ValueError('Oracle selected another Stats reader during this observe-mode switch; '
                                 'run observe-mode --mode '+prior['from']+' first')
            receipt = {k: v for k, v in prior.items() if k not in ('error_type', 'rollback_error_type', 'completed_at')}
            receipt['continued'] = True
        else:
            before = retention_bytes(config_path, private=True)
            config = json.loads(before)
            switch_path = state/PUBLISHER_SWITCH_RECEIPT
            if os.path.lexists(switch_path):
                switch = json.loads(retention_bytes(switch_path, private=True))
                if switch.get('status') == 'switching' or switch.get('rollback_pending'):
                    raise ValueError('an unresolved publisher switch must finish first')
            if config.get('publisher') != ORACLE_SITE or config.get('peer_id') != PUSHING_PEER:
                raise ValueError('observe mode needs the Oracle publisher')
            if not isinstance(config.get('source_revision'), str) or not SHA.fullmatch(config['source_revision']):
                raise ValueError('exact publisher revision required')
            # Before anything is written: Oracle installs from the reader it selected,
            # which need not be this node's release; without one nothing changes.
            oracle_revision = oracle_publisher_revision(config)
            started_in = collect_local_mode(config)
            require_observations_ingested(home, config, started_in, mode)
            if mode == 'incremental' and started_in != 'incremental':
                require_incremental_collection_ready(home)
            target = json.dumps({**oracle_peer_entry(config, oracle_revision), 'collect_local_mode': mode},
                                indent=2).encode()
            atomic(state/('refresh-before-observe-mode-'+hashlib.sha256(before).hexdigest()[:16]+'.json'), before)
            receipt = {'schema': OBSERVE_MODE_SCHEMA, 'peer': peer_id, 'revision': config['source_revision'],
                       'from': started_in, 'to': mode, 'continued': False,
                       'started_at': datetime.now(timezone.utc).isoformat(),
                       'before_sha256': hashlib.sha256(before).hexdigest(), 'before_config': before.decode(),
                       'target_sha256': hashlib.sha256(target).hexdigest(), 'target_config': target.decode()}
        goal = target if mode == receipt['to'] else before

        def record(**fields):
            receipt.update(fields)
            atomic(receipt_path, json.dumps(receipt, sort_keys=True).encode())

        def settle(image):
            """Oracle first, then this node: each end reads back the image's mode."""
            result = install_oracle_publisher_locked(home, oracle_revision, json.loads(image))
            current = retention_bytes(config_path, private=True)
            if current != image:
                if current not in (before, target):
                    # Never replaced; it settles only if it already holds the image's mode.
                    if collect_local_mode(json.loads(current)) != collect_local_mode(json.loads(image)):
                        raise ValueError('refresh.json changed by another writer')
                    return result
                atomic(config_path, image)
                if retention_bytes(config_path, private=True) != image:
                    raise ValueError('refresh.json readback differs')
            return result

        # The intent is durable before either node changes.
        record(status='switching', rollback_pending=False, oracle_revision=oracle_revision)
        try:
            oracle = settle(goal)
        except Exception as exc:
            try:
                settle(before)
            except Exception as rollback_exc:
                record(status='failed', rollback_pending=True, error_type=type(exc).__name__,
                       rollback_error_type=type(rollback_exc).__name__)
                raise RuntimeError('observe-mode failed and the Oracle registration is unconfirmed; '
                                   'observe-mode.json stays unresolved until observe-mode runs again: '
                                   + type(rollback_exc).__name__) from exc
            record(status='restored', rollback_pending=False, error_type=type(exc).__name__,
                   completed_at=datetime.now(timezone.utc).isoformat())
            raise
        record(status='completed' if goal == target else 'restored', rollback_pending=False,
               completed_at=datetime.now(timezone.utc).isoformat())
    return {'peer': peer_id, 'collect_local_mode': mode, 'observing_peers': oracle['observing_peers']}


def set_incremental_mode(home, ingest, views, collection, close=None):
    """On ownermac, between cycles: set refresh.json `incremental` and install it on the Oracle publisher.

    Unset switches keep their current value; all off removes the section, so
    the Oracle configuration is the one it was before. The same locks as
    observe-mode are held throughout; incremental-mode.json records the
    before-image and the target first. A failed install restores the
    before-image on both nodes; a restore that fails too leaves the receipt
    unresolved and the deployment writers refuse until a later run settles it.
    """
    state = home/'.local/state/pulse/stats'
    config_path = state/'refresh.json'
    receipt_path = state/INCREMENTAL_MODE_RECEIPT
    with writer_lock(state/'deployment.lock', timeout=0), \
            writer_lock(state/'cost-refresh.lock', timeout=PUBLISHER_WAIT_SECONDS):
        require_settled_observe_mode(state)
        prior = json.loads(retention_bytes(receipt_path, private=True)) if os.path.lexists(receipt_path) else None
        if prior is not None and prior.get('status') not in OBSERVE_MODE_SETTLED:
            # An unresolved switch first returns to its before-image, but only over
            # one of its own images: any other content came from another writer.
            before = prior['before_config'].encode()
            if hashlib.sha256(before).hexdigest() != prior['before_sha256']:
                raise ValueError('incremental-mode receipt differs')
            current = hashlib.sha256(retention_bytes(config_path, private=True)).hexdigest()
            if current not in (prior['before_sha256'], prior['target_sha256']):
                raise ValueError('refresh.json changed outside the unresolved incremental-mode switch; '
                                 'reconcile it by hand before running incremental-mode again')
            atomic(config_path, before)
        before = retention_bytes(config_path, private=True)
        config = json.loads(before)
        if config.get('publisher') != ORACLE_SITE:
            raise ValueError('incremental views run on the Oracle publisher')
        current = incremental_modes(config)
        wanted = {**current, **{k: v for k, v in (('ingest', ingest), ('views', views),
                                                  ('collection', collection), ('close', close)) if v is not None}}
        if wanted['close'] == 'on' and wanted['views'] == 'off':
            raise ValueError('closing weeks reads the resolved store: views must be shadow or on')
        section = incremental_section({'incremental': wanted})
        target_config = {k: v for k, v in config.items() if k != 'incremental'}
        if section is not None:
            target_config['incremental'] = section
        target = before if target_config == config else json.dumps(target_config, indent=2).encode()
        oracle_revision = oracle_publisher_revision(config)
        receipt = {'schema': INCREMENTAL_MODE_SCHEMA, 'from': current, 'to': wanted,
                   'started_at': datetime.now(timezone.utc).isoformat(),
                   'before_sha256': hashlib.sha256(before).hexdigest(), 'before_config': before.decode(),
                   'target_sha256': hashlib.sha256(target).hexdigest(), 'status': 'switching'}
        atomic(receipt_path, json.dumps(receipt, sort_keys=True).encode())

        def settle(image):
            result = install_oracle_publisher_locked(home, oracle_revision, json.loads(image))
            if retention_bytes(config_path, private=True) != image:
                atomic(config_path, image)
                if retention_bytes(config_path, private=True) != image:
                    raise ValueError('refresh.json readback differs')
            return result

        try:
            result = settle(target)
        except Exception as exc:
            try:
                settle(before)
            except Exception as rollback_exc:
                receipt.update(status='failed', error_type=type(exc).__name__,
                               rollback_error_type=type(rollback_exc).__name__)
                atomic(receipt_path, json.dumps(receipt, sort_keys=True).encode())
                raise RuntimeError('incremental-mode failed and the Oracle configuration is unconfirmed; '
                                   'run incremental-mode again') from exc
            receipt.update(status='restored', error_type=type(exc).__name__,
                           completed_at=datetime.now(timezone.utc).isoformat())
            atomic(receipt_path, json.dumps(receipt, sort_keys=True).encode())
            raise
        receipt.update(status='completed', completed_at=datetime.now(timezone.utc).isoformat())
        atomic(receipt_path, json.dumps(receipt, sort_keys=True).encode())
    return {'incremental': {k: v for k, v in wanted.items() if k != 'close' or v != 'off'},
            'oracle_incremental': result.get('incremental')}


def require_reconciled_deployment(state, receipt, local_previous, peer_previous):
    """Do not discard unresolved rollback evidence; read old backups conservatively."""
    if not receipt or not (receipt.get('rollback_errors') or receipt.get('rollback_pending')
                           or receipt.get('status') in {'preparing', 'switching'}):
        return
    previous = receipt.get('peer_previous')
    if previous is None:
        revision = receipt.get('revision', '')
        if not SHA.fullmatch(revision):
            raise ValueError('prior deployment identity is invalid')
        backup = json.loads((state/('refresh-before-'+revision+'.json')).read_bytes())
        if backup.get('source_revision') != receipt.get('previous'):
            raise ValueError('legacy rollback backup differs')
        previous = {}
        # Retired nodes are not part of any rollback this installer performs.
        for peer in reader_peers(retire_peers(backup)):
            match = re.fullmatch(r'/home/pulse/\.local/share/pulse/stats/releases/([0-9a-f]{40})/Scripts/pulse-stats-cost-refresh\.py', peer['script'])
            if not match or peer['host'] in previous and previous[peer['host']] != match[1]:
                raise ValueError('legacy peer rollback identity is unavailable')
            previous[peer['host']] = match[1]
    if receipt.get('previous') != local_previous or previous != peer_previous:
        raise ValueError('prior deployment rollback remains unresolved')


def start_installed_app(installed):
    """Launch the persistent consumer without the ephemeral Actions job tag.

    Runner cleanup stays enabled for every test and build child. Only the exact
    production App is a user application, not an orphan of this deployment job.
    """
    if installed != Path('/Applications/PulseStats.app'):
        raise ValueError('only the exact installed App can leave the job lifecycle')
    env = dict(os.environ)
    env.pop('RUNNER_TRACKING_ID', None)
    result = subprocess.run(['/usr/bin/open', str(installed)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        raise RuntimeError('installed App launch failed')


def stop_installed_app(installed):
    processes = run(['/bin/ps','-axo','pid=,uid=,command=']).decode().splitlines()
    pids=[]
    for line in processes:
        bits=line.split(None,2)
        if len(bits)==3 and bits[1]==str(os.getuid()) and bits[2].split()[0]==str(installed/'Contents/MacOS/PulseStats'):
            pids.append(int(bits[0])); os.kill(int(bits[0]), signal.SIGTERM)
    deadline=time.monotonic()+8
    while pids and time.monotonic()<deadline:
        alive=[]
        for pid in pids:
            try: os.kill(pid,0);alive.append(pid)
            except ProcessLookupError: pass
        pids=alive
        if pids:time.sleep(.1)
    if pids:
        raise ValueError('installed App did not exit; no forced kill')


def local_refresh_inputs(home, revision, expected, original, job):
    """Adopt local publisher identity/cadence, preserving peer and source fields."""
    if not SHA.fullmatch(revision) or not SHA.fullmatch(expected) or revision == expected:
        raise ValueError('different exact runtime SHAs required')
    if original.get('source_revision') != expected or job.get('Label') != LABEL:
        raise ValueError('publisher identity changed')
    python = home/'.local/share/coevolution/python-env/bin/python'
    legacy_python = home/'.local/state/pulse/compute-cost/venv/bin/python'
    runtime=selected_publisher_runtime(home,expected,job)
    script=runtime/'Scripts/pulse-stats-cost-refresh.py'
    config=home/'.local/state/pulse/stats/refresh.json'
    updated_config={**deepcopy(original),'source_revision':revision}
    updated_job = publisher_launchagent(deepcopy(job))
    # The destination is selected after staging, under the original dual locks.
    updated_job['ProgramArguments'] = [str(python),str(script.parents[1].parent/revision/'Scripts/pulse-stats-cost-refresh.py'),
                                      'refresh','--config',str(config)]
    return updated_config, updated_job


PUBLISHER_EXIT_GRACE_SECONDS = 30


def require_idle_publisher(domain, grace=PUBLISHER_EXIT_GRACE_SECONDS):
    # Caller holds the publisher lock. An interval racing this check cannot
    # collect: the entrypoint takes that same lock nonblocking before any work.
    # A run that has just released the lock is still exiting for a moment
    # (11-16: the deployment acquired the lock in the same second a refresh
    # finished); allow it a bounded exit before refusing.
    deadline = time.monotonic() + grace
    while True:
        observed = run(['/bin/launchctl','print',domain+'/'+LABEL]).decode()
        if not re.search(r'^\s*pid = \d+', observed, re.MULTILINE) and re.search(
                r'^\s*state = (?:not running|waiting)\s*$', observed, re.MULTILINE):
            return
        if time.monotonic() >= deadline:
            raise ValueError('publisher is active or its idle state is unknown')
        time.sleep(1)


def require_publisher_arguments(domain, expected):
    observed = run(['/bin/launchctl','print',domain+'/'+LABEL]).decode()
    match = re.search(r'^\s*arguments = \{\n(.*?)\n\s*\}',observed,re.MULTILINE|re.DOTALL)
    if not match or [line.strip() for line in match[1].splitlines()] != expected:
        raise ValueError('loaded publisher entrypoint differs')


def checked_native_publisher(home, source, revision, data, writer):
    python = canonical_python(home)
    result = json.loads(run([str(python), '-I', '-B', str(source/'Scripts/pulse-gh.py'),
        '--transport', 'local', 'run', '--', 'api', '--method', 'GET',
        'repos/bigbird-online/PULSE/actions/workflows/default-branch-verification.yml/runs',
        '-f', 'head_sha='+revision, '-f', 'event=push', '-f', 'status=success']))
    matching = [row for row in result['workflow_runs']
                if (row['head_sha'] == revision and row['head_branch'] == 'main'
                    and row['event'] == 'push' and row['conclusion'] == 'success'
                    and row['path'] == '.github/workflows/default-branch-verification.yml')]
    if not matching:
        raise ValueError('native publisher requires successful exact-main verification')
    target = native_publisher(home, revision, native_publisher_arguments(home, revision))
    if target is None or writer['verify_release'](target, revision)['provenance'] != 'managed':
        raise ValueError('native publisher requires the release writer sealed program')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive_file:
        for member in archive_file.getmembers():
            if not member.isfile() or not (member.name.startswith(('ComputeCost/', 'Stats/exporter/'))
                    or member.name == 'Scripts/pulse-stats-cost-refresh.py'):
                continue
            asset = target/member.name
            info = asset.lstat()
            if (asset.resolve() != asset or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid() or info.st_mode & 0o222
                    or info.st_nlink != 1 or info.st_size != member.size
                    or asset.read_bytes() != archive_file.extractfile(member).read()):
                raise ValueError('native publisher asset differs from the verified source')
    row = matching[0]
    return target, f"github:{row['id']}:{row['run_attempt']}"


def deploy_refresh_local(args, *, home=None):
    """Adopt a compatible publisher fix without changing peers or the App."""
    if home is None:
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    state = home/'.local/state/pulse/stats'
    config_path = state/'refresh.json'
    job_path = home/'Library/LaunchAgents'/f'{LABEL}.plist'
    registry_path = home/'.local/state/pulse/compute-cost/sources.json'
    revision, expected = args.revision, args.expected
    domain = f'gui/{os.getuid()}'
    for path in (config_path,job_path,registry_path):
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
            raise ValueError('owned regular publisher inputs required')
    if config_path.stat().st_mode & 0o077 or registry_path.stat().st_mode & 0o077:
        raise ValueError('private publisher configuration required')
    source = args.source.resolve()
    native_scope = getattr(args, 'native_publisher_only', False)
    with ExitStack() as locks:
        if native_scope:
            import runpy
            if (home != PUBLISHER_HOME or not SHA.fullmatch(revision) or not SHA.fullmatch(expected)
                    or revision == expected or getattr(args, 'defer_refresh', False)):
                raise ValueError('native publisher requires the resident account and exact revisions')
            writer = runpy.run_path(str(source/'Scripts/pulse-release-writer.py'))
            locks.enter_context(writer['operation_lock'](home/'.local/state/pulse-release-writer'))
        locks.enter_context(writer_lock(state/'deployment.lock',timeout=0))
        require_settled_observe_mode(state)
        require_settled_incremental_mode(state)
        original = config_path.read_bytes(); old_job = job_path.read_bytes()
        registry = registry_path.read_bytes()
        old_config, original_job = json.loads(original), plistlib.loads(old_job)
        canonical_python(home)
        data = archive(source,revision)
        require_installer_archive(data)
        if native_scope:
            previous = native_publisher(home, expected, original_job['ProgramArguments'])
            if previous is None or old_config.get('source_revision') != expected or original_job.get('Label') != LABEL:
                raise ValueError('native publisher changed before deployment')
            local, authorization = checked_native_publisher(home, source, revision, data, writer)
            config = {**old_config, 'source_revision':revision}
            job = {**original_job, 'ProgramArguments':native_publisher_arguments(
                home, revision, original_job['ProgramArguments'][2])}
            publisher = {'publisher_owner':'pulse_release', 'authorization':authorization}
            # Both bounded carriers must be readable before selecting the publisher.
            exporter = (home/'.local/share/pulse/stats/current').resolve(strict=True)/'Stats/exporter/pulse-stats-snapshot.py'
            for encoding, schema in (('row-contexts', 'pulse_stats_cost_row_contexts/v1'),
                                     ('compression', 'pulse_stats_cost_compression/v1')):
                observed = json.loads(run([str(canonical_python(home)), '-I', '-B', str(exporter),
                                           '--probe-cost-'+encoding, '--json'], timeout=15))
                if observed != {'schema': schema}:
                    raise ValueError('selected Stats reader does not support compact cost publications')
        else:
            config, job = local_refresh_inputs(home,revision,expected,old_config,original_job)
            local = stage_runtime(data,home,revision,publisher=True)
            job['ProgramArguments'] = publisher_arguments(home, local, install=True)
            publisher = {'publisher_policy':PUBLISHER_POLICY}
        verify_reader_features(local,json.loads(registry))
        # Every attempt retains its own immutable before-image, including retries.
        attempt = Path(tempfile.mkdtemp(prefix='refresh-local-'+revision+'-',dir=state))
        receipt = {'scope':'local_cost_refresh','revision':revision,'previous':expected,
            'started_at':datetime.now(timezone.utc).isoformat(),'status':'staged',
            'source_registry_changed':False,'peer_paths_changed':False,'app_changed':False,
            **publisher,'shared_selector_changed':False}
        receipt_path = attempt/'receipt.json'
        def record():
            atomic(receipt_path,json.dumps(receipt,indent=2).encode())
        atomic(attempt/'refresh-before.json',original)
        atomic(attempt/'launchagent-before.plist',old_job)
        record()
        stopped = False
        activation_confirmed = False
        try:
            with writer_lock(state/'cost-refresh.lock',timeout=0):
                if (config_path.read_bytes()!=original or job_path.read_bytes()!=old_job
                        or registry_path.read_bytes()!=registry):
                    raise ValueError('publisher inputs changed during staging')
                require_idle_publisher(domain)
                require_publisher_arguments(domain,plistlib.loads(old_job)['ProgramArguments'])
                if native_scope:
                    previous_runtime = native_publisher(home,expected,original_job['ProgramArguments'])
                    if previous_runtime != previous:
                        raise ValueError('active native publisher changed before deployment')
                else:
                    previous_runtime=selected_publisher_runtime(home,expected,original_job)
                    if (previous_runtime.is_symlink() or previous_runtime.resolve()!=previous_runtime
                            or (previous_runtime/'.stats-source-revision').read_text().strip()!=expected):
                        raise ValueError('active local publisher changed before deployment')
                    if previous_runtime.parent.name == 'releases' and current_revision(home) != expected:
                        raise ValueError('legacy publisher differs from shared selector')
                verify_reader_features(previous_runtime,json.loads(registry))
                receipt['status'] = 'switching';record()
                # A timeout has an uncertain outcome; reconcile even if run raises.
                stopped = True
                run(['/bin/launchctl','bootout',domain+'/'+LABEL])
                try:
                    atomic(config_path,json.dumps(config,indent=2).encode())
                    atomic(job_path,plistlib.dumps(job))
                    receipt.update(status='publisher_payload_selected',selected_at=datetime.now(timezone.utc).isoformat())
                    record()
                    if not native_scope:
                        record_retention_adoption(home,receipt_path,enrollment=True)
                except Exception:
                    # Restore under the writer lock, before any scheduler returns.
                    atomic(config_path,original);atomic(job_path,old_job)
                    raise
            # Enrollment uses the same publisher lock. The existing workflow
            # defer-refresh flag keeps that registration before the first run.
            if not getattr(args, 'defer_refresh', False):
                run(['/bin/launchctl','bootstrap',domain,str(job_path)])
                require_publisher_arguments(domain,job['ProgramArguments'])
            else:
                receipt['publisher_start_deferred'] = True
            activation_confirmed = True
            stopped = False
            receipt.update(status='installed',completed_at=datetime.now(timezone.utc).isoformat())
            record()
            if not native_scope:
                record_retention_adoption(home, receipt_path)
        except Exception as exc:
            receipt.update(status='installed_receipt_failed' if activation_confirmed else 'failed',
                activation_confirmed=activation_confirmed,error_type=type(exc).__name__)
            # A failed bootstrap can be uncertain. Never stop a new active refresh
            # or overwrite its facts; require an idle lock before restoring inputs.
            errors = []
            if stopped:
                try:
                    with writer_lock(state/'cost-refresh.lock',timeout=0):
                        observed = subprocess.run(['/bin/launchctl','print',domain+'/'+LABEL],
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
                        if observed.returncode == 0:
                            require_idle_publisher(domain)
                            run(['/bin/launchctl','bootout',domain+'/'+LABEL])
                        elif not (observed.returncode == 113 and
                                ('Could not find service "'+LABEL+'"').encode() in observed.stderr):
                            raise ValueError('scheduler absence is unconfirmed')
                        atomic(config_path,original);atomic(job_path,old_job)
                        run(['/bin/launchctl','bootstrap',domain,str(job_path)])
                    require_publisher_arguments(domain,plistlib.loads(old_job)['ProgramArguments'])
                except Exception as rollback_exc:
                    errors.append(type(rollback_exc).__name__)
            receipt['rollback_errors'] = errors
            receipt['rollback_pending'] = bool(errors)
            record()
            raise
        print(json.dumps(receipt))


def deploy(args):
    import pwd
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    # Keep the local fence through failed activation, rollback and receipt writes.
    with writer_lock(home/'.local/state/pulse/stats/deployment.lock', timeout=0):
        _deploy_locked(args, home)


def _deploy_locked(args, home):
    state = home/'.local/state/pulse/stats'
    receipt_path = state/'deployment.json'
    prior_bytes = receipt_path.read_bytes() if receipt_path.exists() else None
    prior = json.loads(prior_bytes) if prior_bytes else None
    config_path = state/'refresh.json'
    require_settled_observe_mode(state)
    require_settled_incremental_mode(state)
    if config_path.is_symlink() or config_path.stat().st_mode & 0o077:
        raise ValueError('private regular deployment config required')
    source = args.source.resolve(); revision = args.revision
    if not SHA.fullmatch(revision) or not SHA.fullmatch(args.expected):
        raise ValueError('exact source and expected active SHAs required')
    original_bytes = config_path.read_bytes(); original = json.loads(original_bytes)
    if original['source_revision'] != args.expected:
        raise ValueError('publisher changed before deployment')
    shared_previous = current_revision(home)
    loaded_job=plistlib.loads((home/'Library/LaunchAgents'/f'{LABEL}.plist').read_bytes())
    native=native_publisher(home,args.expected,loaded_job.get('ProgramArguments'))
    old_publisher=native or selected_publisher_runtime(home,args.expected,loaded_job)
    if native is None and (old_publisher.is_symlink() or (old_publisher/'.stats-source-revision').read_text().strip() != args.expected):
        raise ValueError('original publisher identity differs')
    verify_reader_features(old_publisher,json.loads((home/'.local/state/pulse/compute-cost/sources.json').read_bytes()))
    target_config = peer_scripts(original, revision) if native else refresh_config(original, revision, args.codex_command, args.codex_home, cc_quota_source(home))
    if target_config.get('publisher') == ORACLE_SITE:
        # This deployment selects ``revision`` on Oracle, paused or not; the entry
        # ownermac pulls and pushes through follows it, and the rollback restores both.
        target_config = oracle_peer_entry(target_config, revision)
    verify_reader_features(source, json.loads((home/'.local/state/pulse/compute-cost/sources.json').read_bytes()))
    identity = validate_app(args.app, revision)
    # A release SHA identifies immutable code, not a deployment's live inputs.
    # Reuse the existing Actions build identity; never overwrite an old plan.
    plan_attempt = identity['build_id']
    _source_plan_paths(home, revision, plan_attempt)
    plan_args = ['--source-plan-attempt', plan_attempt]
    payload = archive(source, revision);require_installer_archive(payload)
    peers = {p['host']: p for p in reader_peers(target_config)}
    collected = {p['host'] for p in collected_peers(target_config) if 'host' in p}
    python = canonical_python(home)
    job_path = home/'Library/LaunchAgents'/f'{LABEL}.plist'
    old_job_bytes = job_path.read_bytes()
    job = publisher_launchagent(plistlib.loads(old_job_bytes))
    installed = Path('/Applications/PulseStats.app')
    previous = state/('PulseStats-before-'+revision+'.app')
    if previous.exists():
        raise ValueError('prior deployment backup exists; reconcile it before another install')
    staged = Path(tempfile.mkdtemp(prefix='.PulseStats-stage-',dir=installed.parent))/'PulseStats.app'
    receipt = {'revision':revision,'previous':args.expected,'started_at':datetime.now(timezone.utc).isoformat(),
               'source_registry_changed':False,'source_plan_attempt':plan_attempt,'app':identity,'status':'preparing',
               'shared_previous':shared_previous,
               **({'publisher_owner':'pulse_release'} if native else {'publisher_policy':PUBLISHER_POLICY})}
    switched = []
    source_switched = []
    local_sources_changed = False
    adopt_sources = bool(getattr(args, "adopt_runtime_sources", False))
    entry_started = False
    app_replaced = False
    activation_confirmed = False
    recorded = False
    domain=f'gui/{os.getuid()}'
    def peer_preflight(peer, expected=None):
        installer='/home/pulse/.local/share/pulse/stats/releases/'+revision+'/Stats/deploy.py'
        argv=[peer['python'],installer,'preflight','--revision',revision]
        if expected is not None:argv += ['--expected',expected]
        if adopt_sources and peer['host'] in collected:argv += plan_args
        result=json.loads(run(peer_command(peer,argv)))
        if result.get('revision') != revision or not SHA.fullmatch(result.get('previous','')):
            raise ValueError('peer preflight identity differs')
        if expected is not None and result['previous'] != expected:
            raise ValueError('peer changed after preflight')
        return result['previous']
    def peer_select(peer, expected, *, restore=False):
        installer='/home/pulse/.local/share/pulse/stats/releases/'+revision+'/Stats/deploy.py'
        # Oracle first lets its running publisher cycle finish.
        run(peer_command(peer,[peer['python'],installer,'restore-runtime' if restore else 'select',
            '--revision',revision,'--expected',expected,*plan_args]),
            timeout=PUBLISHER_WAIT_SECONDS+300 if peer['id'] == ORACLE_SITE else 180)
    try:
        local=stage_runtime(payload,home,revision)
        publisher_local=None if native else stage_runtime(payload,home,revision,publisher=True)
        # Only Git-archived, approved installer bytes cross the configured SSH boundary.
        receiver=peer_stage_receiver()
        for peer in peers.values():
            run(peer_command(peer,[peer['python'],'-c',receiver,revision]),data=payload)
        if adopt_sources:
            plans = {'ownermac': prepare_sources(home, revision, 'ownermac', attempt=plan_attempt, workspace_registry=getattr(args, 'workspace_registry', None))}
            for peer in peers.values():
                if peer['host'] not in collected:continue
                installer='/home/pulse/.local/share/pulse/stats/releases/'+revision+'/Stats/deploy.py'
                plans[peer['id']]=json.loads(run(peer_command(peer,[peer['python'],installer,'prepare-sources',
                    '--revision',revision,'--expected',args.expected,'--site',peer['id'],*plan_args])))
            receipt['source_plans']=plans
        preflight_runtime(home, revision, shared_previous, attempt=plan_attempt if adopt_sources else None)
        peer_previous = {host:peer_preflight(peer) for host,peer in peers.items()}
        require_reconciled_deployment(state, prior, args.expected, peer_previous)
        receipt['peer_previous'] = peer_previous
        # Preserve old-format receipts and unresolved errors before a fresh attempt.
        if prior_bytes is not None:
            history=state/('deployment-history-'+hashlib.sha256(prior_bytes).hexdigest()+'.json')
            if not history.exists():atomic(history,prior_bytes)
        atomic(receipt_path,json.dumps(receipt,indent=2).encode());recorded=True
        quota_code=('import sys,pathlib;sys.path.insert(0,sys.argv[1]);'
                    'from pulse_stats.codex_quota import read_account_quota;'
                    'q=read_account_quota(sys.argv[3:],codex_home=pathlib.Path(sys.argv[2]));'
                    'print(q["status"])')
        run([str(python),'-c',quota_code,str(local/'Stats/exporter'),str(args.codex_home),*args.codex_command],timeout=35)
        run(['/usr/bin/ditto',str(args.app),str(staged)])
        validate_app(staged,revision)
        if not native:
            job['ProgramArguments']=publisher_arguments(home, publisher_local, install=True)
            job['StartInterval']=30 * 60;job['RunAtLoad']=True
        with writer_lock(state/'cost-refresh.lock'):
            if config_path.read_bytes()!=original_bytes or job_path.read_bytes()!=old_job_bytes:
                raise ValueError('publisher configuration changed during staging')
            atomic(state/('refresh-before-'+revision+'.json'),original_bytes)
            atomic(state/('launchagent-before-'+revision+'.plist'),old_job_bytes)
            # Staging can be long. Recheck every captured selector before stopping.
            require_idle_publisher(domain)
            require_publisher_arguments(domain,plistlib.loads(old_job_bytes)['ProgramArguments'])
            preflight_runtime(home, revision, shared_previous, attempt=plan_attempt if adopt_sources else None)
            for host,peer in peers.items():peer_preflight(peer,peer_previous[host])
            receipt['status']='switching'
            atomic(receipt_path,json.dumps(receipt,indent=2).encode())
            # No task can collect while entrypoints are changed. Always restore
            # original entrypoints on a failed activation; never roll back ledger facts.
            entry_started=True
            run(['/bin/launchctl','bootout',domain+'/'+LABEL])
            for peer in peers.values():
                if peer_previous[peer['host']] == revision:continue
                switched.append(peer)
                peer_select(peer,peer_previous[peer['host']])
            select_runtime(home,revision,shared_previous,strict=True)
            if adopt_sources:
                for peer in peers.values():
                    if peer['host'] not in collected:continue
                    source_switched.append(peer)
                    installer='/home/pulse/.local/share/pulse/stats/releases/'+revision+'/Stats/deploy.py'
                    run(peer_command(peer,[peer['python'],installer,'select-sources','--revision',revision,'--expected',args.expected,*plan_args]))
                local_sources_changed=True
                select_sources(home,revision,attempt=plan_attempt)
                receipt['source_registry_changed']=True
            atomic(config_path,json.dumps(target_config,indent=2).encode())
            if not native:
                atomic(job_path,plistlib.dumps(job))
            stop_installed_app(installed)
            if installed.exists():os.rename(installed,previous)
            try:os.rename(staged,installed)
            except Exception:
                if previous.exists():os.rename(previous,installed)
                raise
            app_replaced=True
            start_installed_app(installed)
            receipt.update(status='publisher_payload_selected',selected_at=datetime.now(timezone.utc).isoformat())
            atomic(receipt_path,json.dumps(receipt,indent=2).encode())
            if not native:
                record_retention_adoption(home,receipt_path,enrollment=True)
        # Bootstrap only after releasing the publisher lock: RunAtLoad must
        # be able to acquire it, not fail immediately against the installer.
        # Actor activation takes the same writer lock. Start the one
        # publisher only AFTER that step when this deployment requests it.
        # Otherwise RunAtLoad can lose the lock and sleep an entire interval.
        if not getattr(args, 'defer_refresh', False):
            run(['/bin/launchctl','bootstrap',domain,str(job_path)])
        activation_confirmed=True
        receipt['publisher_start_deferred'] = bool(getattr(args, 'defer_refresh', False))
        receipt.update(status='activated_pending_business_readback',completed_at=datetime.now(timezone.utc).isoformat())
        atomic(state/'deployment.json',json.dumps(receipt,indent=2).encode())
        record_retention_adoption(home, state/'deployment.json', app_paths=(installed, previous))
        print(json.dumps(receipt))
    except Exception as exc:
        if not recorded:
            raise
        receipt.update(status='activated_receipt_failed' if activation_confirmed else 'deployment_failed',
            activation_confirmed=activation_confirmed,error_type=type(exc).__name__)
        errors=[]
        if entry_started and not activation_confirmed:
          try:
            with writer_lock(state/'cost-refresh.lock',timeout=0):
                observed=subprocess.run(['/bin/launchctl','print',domain+'/'+LABEL],
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
                if observed.returncode == 0:
                    require_idle_publisher(domain)
                    run(['/bin/launchctl','bootout',domain+'/'+LABEL])
                elif not (observed.returncode == 113 and
                        ('Could not find service "'+LABEL+'"').encode() in observed.stderr):
                    raise ValueError('scheduler absence is unconfirmed')
                if app_replaced:
                    try:
                        stop_installed_app(installed)
                        os.rename(installed,staged)
                        if previous.exists():os.rename(previous,installed)
                        start_installed_app(installed)
                    except Exception as rollback_exc:errors.append('app:'+type(rollback_exc).__name__)
                if local_sources_changed:
                    try:select_sources(home,revision,restore=True,attempt=plan_attempt)
                    except Exception as rollback_exc:errors.append('local_sources:'+type(rollback_exc).__name__)
                for peer in reversed(source_switched):
                    try:
                        installer='/home/pulse/.local/share/pulse/stats/releases/'+revision+'/Stats/deploy.py'
                        run(peer_command(peer,[peer['python'],installer,'restore-sources','--revision',revision,'--expected',args.expected,*plan_args]))
                    except Exception as rollback_exc:errors.append(peer['id']+'_sources:'+type(rollback_exc).__name__)
                for path,old,new in ((config_path,original_bytes,json.dumps(target_config,indent=2).encode()),
                                     (job_path,old_job_bytes,plistlib.dumps(job))):
                    if path.read_bytes() not in (old,new):
                        raise ValueError('publisher inputs changed before rollback')
                atomic(config_path,original_bytes)
                if not native:
                    atomic(job_path,old_job_bytes)
                try:select_runtime(home,shared_previous,revision)
                except Exception as rollback_exc:errors.append('local:'+type(rollback_exc).__name__)
                for peer in reversed(switched):
                    try:peer_select(peer,peer_previous[peer['host']],restore=True)
                    except Exception as rollback_exc:errors.append(peer['id']+':'+type(rollback_exc).__name__)
            try:run(['/bin/launchctl','bootstrap',domain,str(job_path)])
            except Exception as rollback_exc:errors.append('scheduler:'+type(rollback_exc).__name__)
          except Exception as rollback_exc:errors.append('rollback:'+type(rollback_exc).__name__)
        receipt['rollback_errors']=errors
        receipt['rollback_pending']=bool(errors)
        atomic(state/'deployment.json',json.dumps(receipt,indent=2).encode())
        raise
    finally:
        if staged.exists():shutil.rmtree(staged)
        staged.parent.rmdir()


def enroll_native_purpose(args, *, home=None):
    """Configure an explicitly reviewed Hook without granting native trust."""
    if home is None:
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if (not SHA.fullmatch(args.revision) or args.expected != args.revision
            or not re.fullmatch(r'[0-9a-f]{64}', args.expected_native_registry or '')
            or not (args.expected_native_hooks == 'absent'
                    or re.fullmatch(r'[0-9a-f]{64}', args.expected_native_hooks or ''))):
        raise ValueError('exact selected revision and reviewed native configuration hashes required')
    import importlib.util
    module_path = Path(__file__).with_name('request_purpose.py')
    spec = importlib.util.spec_from_file_location('stats_native_request_enrollment',module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.install(home,Path(__file__).resolve().parents[1],
        canonical_python(home),args.revision,
        expected_registry_sha256=args.expected_native_registry,
        expected_hooks_sha256=None if args.expected_native_hooks=='absent' else args.expected_native_hooks,
        site=args.site)



def session_wi_source_registry(original, source_id, since, *, calendar_root=None, calendar_from=None):
    """Plan one existing source's session reader; never re-register its identity."""
    from datetime import datetime, timezone
    if not isinstance(since, str):
        raise ValueError('explicit session enrollment time required')
    cutoff = datetime.fromisoformat(since.replace('Z', '+00:00'))
    if cutoff.tzinfo is None or cutoff > datetime.now(timezone.utc):
        raise ValueError('session enrollment time must be past and timezone-aware')
    result = deepcopy(original)
    matches = [source for source in result.get('sources', []) if source.get('id') == source_id]
    if len(matches) != 1 or matches[0].get('kind') not in ('codex', 'cc', 'pi'):
        raise ValueError('exact existing Codex, CC or Pi source required')
    if matches[0]['kind'] != 'codex' and (calendar_root is not None or calendar_from is not None):
        raise ValueError('native calendar enrollment is Codex-only')
    source = matches[0]
    if source.get('session_wi_mcp_since') not in (None, since):
        raise ValueError('session enrollment boundary is immutable')
    source['session_wi_mcp_since'] = since
    if calendar_root is not None or calendar_from is not None:
        # The standalone installer resolves only its own archived reader.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'ComputeCost'))
        from pulse_compute_cost.source_layout import CODEX_CALENDAR_LAYOUT, codex_calendar_paths
        if calendar_root not in source.get('counter_dependency_paths', []):
            raise ValueError('calendar root must already be an enrolled native dependency')
        selected = {'layout':CODEX_CALENDAR_LAYOUT, 'calendar_root':calendar_root,
                    'calendar_from':calendar_from}
        if any(source.get(k) not in (None,v) for k,v in selected.items()):
            raise ValueError('native calendar enrollment boundary is immutable')
        source.update(selected)
        codex_calendar_paths(source)
    return result


def enroll_session_wi(args, *, home=None):
    """Activate the installed reader for one reviewed source under publisher lock."""
    if home is None:
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not SHA.fullmatch(args.revision) or args.expected != args.revision:
        raise ValueError('exact selected reader revision required')
    state = home/'.local/state/pulse/stats'
    runtime = (home/'.local/share/pulse/stats/current').resolve()
    if (runtime/'.stats-source-revision').read_text().strip() != args.revision:
        raise ValueError('session reader is not selected')
    path = home/'.local/state/pulse/compute-cost/sources.json'
    with writer_lock(state/'deployment.lock', timeout=0), writer_lock(state/'cost-refresh.lock'):
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise ValueError('private regular source registry required')
        original = path.read_bytes()
        if hashlib.sha256(original).hexdigest() != args.expected_native_registry:
            raise ValueError('source registry changed after review')
        candidate = session_wi_source_registry(json.loads(original), args.native_source_id, args.session_wi_since,
            calendar_root=getattr(args,'codex_calendar_root',None), calendar_from=getattr(args,'codex_calendar_from',None))
        verify_reader_features(runtime, candidate)
        desired = json.dumps(candidate, sort_keys=True, separators=(',', ':')).encode()+bytes([10])
        changed = json.loads(original) != candidate
        before = state/('session-wi-source-before-'+hashlib.sha256(original).hexdigest()+'.json')
        if before.exists() and (before.is_symlink() or before.read_bytes() != original):
            raise ValueError('source before-image differs')
        if not before.exists():
            atomic(before, original)
        if path.read_bytes() != original:
            raise ValueError('source registry changed during enrollment')
        if changed:
            atomic(path, desired)
        if json.loads(path.read_bytes()) != candidate:
            raise ValueError('source enrollment readback differs')
        return {'status':'session_reader_enabled', 'revision':args.revision,
                'source_id':args.native_source_id, 'since':args.session_wi_since,
                'changed':changed, 'registry_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'ledger_rewritten':False, 'hooks_changed':False}


CORE_OUTCOME_SOURCE = 'harness-core-outcomes'


def core_outcome_source_registry(original, facts, site):
    """Plan the one core Outcome source; its identity and path never move."""
    result = deepcopy(original)
    if not facts.is_absolute():
        raise ValueError('absolute core stats facts path required')
    wanted = {'id': CORE_OUTCOME_SOURCE, 'kind': 'harness_core', 'path': str(facts), 'label': site+'.harness_core'}
    existing = [s for s in result.get('sources', []) if s.get('kind') == 'harness_core' or s.get('id') == CORE_OUTCOME_SOURCE]
    if existing:
        if existing != [wanted]:
            raise ValueError('core Outcome source enrollment is immutable')
        return result
    result['sources'].append(wanted)
    return result


def holder_supplement_registry(original, facts, surface):
    """Plan the holder supplement: the core export path, this node's surface, the feature."""
    result = deepcopy(original)
    if not facts.is_absolute() or not isinstance(surface, str) or not re.fullmatch(r'surface_v1_[a-z0-9_]+', surface):
        raise ValueError('absolute core stats facts path and core surface required')
    wanted = {'path': str(facts), 'surface': surface}
    if result.get('core_session_writes', wanted) != wanted:
        raise ValueError('holder supplement enrollment is immutable')
    result['core_session_writes'] = wanted
    result['required_receipt_features'] = sorted(set(result.get('required_receipt_features', [])) | {'holder_supplement.v1'})
    return result


def enroll_holder_supplement(args, *, home=None):
    """Enable the holder supplement from the installed core's stats facts under publisher lock."""
    return _enroll_core_facts(args, home, lambda original, facts: holder_supplement_registry(original, facts, args.surface),
                              {'status':'holder_supplement_enabled'})


def enroll_core_outcomes(args, *, home=None):
    """Register the installed core's stats facts as the WI Outcome source under publisher lock."""
    return _enroll_core_facts(args, home, lambda original, facts: core_outcome_source_registry(original, facts, args.site),
                              {'status':'core_outcome_reader_enabled', 'source_id':CORE_OUTCOME_SOURCE})


def _enroll_core_facts(args, home, plan, result):
    if home is None:
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not SHA.fullmatch(args.revision) or args.expected != args.revision:
        raise ValueError('exact selected reader revision required')
    state = home/'.local/state/pulse/stats'
    runtime = (home/'.local/share/pulse/stats/current').resolve()
    if (runtime/'.stats-source-revision').read_text().strip() != args.revision:
        raise ValueError('core facts reader is not selected')
    path = home/'.local/state/pulse/compute-cost/sources.json'
    with writer_lock(state/'deployment.lock', timeout=0), writer_lock(state/'cost-refresh.lock'):
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise ValueError('private regular source registry required')
        original = path.read_bytes()
        if hashlib.sha256(original).hexdigest() != args.expected_native_registry:
            raise ValueError('source registry changed after review')
        candidate = plan(json.loads(original), state/'wi-facts-v1.json')
        verify_reader_features(runtime, candidate)
        desired = json.dumps(candidate, sort_keys=True, separators=(',', ':')).encode()+bytes([10])
        changed = json.loads(original) != candidate
        before = state/('core-outcome-source-before-'+hashlib.sha256(original).hexdigest()+'.json')
        if before.exists() and (before.is_symlink() or before.read_bytes() != original):
            raise ValueError('source before-image differs')
        if not before.exists():
            atomic(before, original)
        if path.read_bytes() != original:
            raise ValueError('source registry changed during enrollment')
        if changed:
            atomic(path, desired)
        if json.loads(path.read_bytes()) != candidate:
            raise ValueError('source enrollment readback differs')
        return {**result, 'revision':args.revision, 'changed':changed,
                'registry_sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'ledger_rewritten':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['plan-retention','deploy','deploy-refresh-local','select','preflight','restore-runtime','prepare-sources','select-sources','restore-sources','enroll-native-purpose','enroll-session-wi','enroll-core-outcomes','enroll-holder-supplement','select-first-oracle-runtime','enroll-oracle-peer',
                                  'install-oracle-publisher','receive-oracle-publisher','switch-publisher','peer-collection',
                                  'observe-mode','incremental-mode'])
    p.add_argument('--revision');p.add_argument('--expected')
    p.add_argument('--source',type=Path);p.add_argument('--app',type=Path)
    p.add_argument('--codex-home',type=Path);p.add_argument('--codex-command',nargs='+')
    p.add_argument('--adopt-runtime-sources',action='store_true')
    p.add_argument('--defer-refresh', action='store_true', help='Workflow starts publisher after actor activation')
    p.add_argument('--native-publisher-only', action='store_true',
                   help='Switch an existing native publisher to an exact sealed PULSE release')
    p.add_argument('--source-plan-attempt', help='Existing App build identity; scopes source prepare/select/rollback')
    p.add_argument('--workspace-registry',type=Path, help='Explicit canonical team registry; local source plan only')
    p.add_argument('--site',choices=list(SITES),default='ownermac')
    p.add_argument('--expected-native-registry', help='Reviewed private source-registry SHA256; explicit native enrollment only')
    p.add_argument('--expected-native-hooks', help='Reviewed Hook-config SHA256 or absent; never grants native trust')
    p.add_argument('--native-source-id')
    p.add_argument('--surface', help="This node's core surface for the holder supplement")
    p.add_argument('--session-wi-since')
    p.add_argument('--codex-calendar-root')
    p.add_argument('--codex-calendar-from')
    p.add_argument('--publisher', choices=[ORACLE_SITE, 'ownermac'], help='switch-publisher: where the cost publication is computed')
    p.add_argument('--peer', help='peer-collection, observe-mode: the registered peer ID')
    p.add_argument('--mode', dest='collect_local_mode', choices=list(COLLECT_LOCAL_MODES),
                   help='observe-mode: ownermac collect-local as a full collection, observe-only or incremental')
    p.add_argument('--collection', choices=['paused', 'active'], help='peer-collection: pause or resume its Stats collection')
    p.add_argument('--ingest', choices=['off', 'on'], help='incremental-mode: ingestion by fact index lookups')
    p.add_argument('--views', choices=['off', 'shadow', 'on'], help='incremental-mode: publisher views from the resolved store')
    p.add_argument('--incremental-collection', dest='incremental_collection', choices=['off', 'shadow', 'on'],
                   help='incremental-mode: collection of changed session packages only')
    p.add_argument('--close', choices=['off', 'on'], help='incremental-mode: close weeks and publish adjustment entries')
    a=p.parse_args()
    if a.native_publisher_only and a.mode != 'deploy-refresh-local':
        p.error('--native-publisher-only requires deploy-refresh-local')
    if a.mode=='plan-retention':
        if not a.expected or not SHA.fullmatch(a.expected):p.error('expected active revision required')
        import pwd
        print(json.dumps(plan_retention(Path(pwd.getpwuid(os.getuid()).pw_dir), a.expected)));return
    if a.mode=='peer-collection':
        if a.peer is None or a.collection is None:p.error('--peer and --collection required')
        print(json.dumps(set_peer_collection(PUBLISHER_HOME, a.peer, a.collection)));return
    if a.mode=='incremental-mode':
        if a.ingest is None and a.views is None and a.incremental_collection is None and a.close is None:
            p.error('--ingest, --views, --incremental-collection or --close required')
        print(json.dumps(set_incremental_mode(PUBLISHER_HOME, a.ingest, a.views, a.incremental_collection,
                                              a.close)));return
    if a.mode=='observe-mode':
        if a.peer is None or a.collect_local_mode is None:p.error('--peer and --mode required')
        print(json.dumps(set_observe_mode(PUBLISHER_HOME, a.peer, a.collect_local_mode)));return
    if a.revision is None:p.error('revision required')
    if a.mode=='receive-oracle-publisher':
        print(json.dumps(receive_oracle_publisher(Path.home(), a.revision, sys.stdin.buffer.read(4*1024*1024))));return
    if a.mode=='install-oracle-publisher':
        print(json.dumps(install_oracle_publisher(PUBLISHER_HOME, a.revision)));return
    if a.mode=='switch-publisher':
        if a.publisher is None:p.error('--publisher required')
        print(json.dumps(switch_publisher(PUBLISHER_HOME, a.revision, a.publisher)));return
    if a.mode=='select-first-oracle-runtime':
        print(json.dumps(select_first_oracle_runtime(Path.home(),a.revision)));return
    if a.mode=='preflight':
        print(json.dumps(preflight_runtime(Path.home(),a.revision,a.expected,attempt=a.source_plan_attempt)));return
    if a.expected is None:p.error('expected revision required')
    if a.mode=='enroll-oracle-peer':
        if a.source is None:p.error('source checkout required')
        print(json.dumps(enroll_oracle_peer(a)));return
    if a.mode=='restore-runtime':
        if a.source_plan_attempt is None:p.error('deployment attempt required')
        print(json.dumps(switch_peer_runtime(Path.home(),a.revision,a.expected,attempt=a.source_plan_attempt,restore=True)));return
    if a.mode=='enroll-session-wi':
        print(json.dumps(enroll_session_wi(a)));return
    if a.mode=='enroll-core-outcomes':
        print(json.dumps(enroll_core_outcomes(a)));return
    if a.mode=='enroll-holder-supplement':
        print(json.dumps(enroll_holder_supplement(a)));return
    if a.mode=='enroll-native-purpose':
        print(json.dumps(enroll_native_purpose(a)));return
    if a.mode=='deploy-refresh-local':
        if a.source is None:p.error('source checkout required')
        deploy_refresh_local(a);return
    if a.mode=='prepare-sources':
        print(json.dumps(prepare_sources(Path.home(),a.revision,a.site,attempt=a.source_plan_attempt,workspace_registry=a.workspace_registry)));return
    if a.mode in {'select-sources','restore-sources'}:
        print(json.dumps(select_sources(Path.home(),a.revision,restore=a.mode=='restore-sources',attempt=a.source_plan_attempt)));return
    if a.mode=='select':
        if not SHA.fullmatch(a.revision) or not SHA.fullmatch(a.expected):p.error('exact SHAs required')
        if a.source_plan_attempt is not None:
            print(json.dumps(switch_peer_runtime(Path.home(),a.revision,a.expected,attempt=a.source_plan_attempt)))
        else:
            print(json.dumps(select_without_attempt(Path.home(),a.revision,a.expected)))
    else:
        if any(x is None for x in [a.source,a.app,a.codex_home,a.codex_command]):p.error('deployment inputs missing')
        deploy(a)

if __name__=='__main__':
    main()
