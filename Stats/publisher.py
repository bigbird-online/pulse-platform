#!/usr/bin/env python3
"""Load the selected local publisher only while holding its deployment lock.

Installed by Stats/deploy.py outside releases under an immutable content name.
This closes the formal launchd consumer's pre-import gap; it grants no payload
expiry or deletion authority to legacy, App, native or other consumers.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import runpy
import stat
import sys
import time


PUBLISHER_PAYLOAD_POLICY = 'publisher-exclusive/v1'

def owned_file(path, *, limit=2 * 1024 * 1024):
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or before.st_mode & 0o022 or before.st_size > limit or before.st_nlink!=1 or path.resolve()!=path):
        raise ValueError('publisher file identity differs')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        data = stream.read(limit + 1)
    after = path.lstat()
    key = lambda s: (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    if len(data) > limit or key(before) != key(opened) or key(opened) != key(after):
        raise ValueError('publisher file changed during read')
    return data


def exclusive_adoption(home, runtime, manifest_body):
    """Bind helper and all code to the original producer before importing either."""
    state=home/'.local/state/pulse/stats'
    directory=state/'publisher-enrollments'
    owner=directory.lstat()
    if (not stat.S_ISDIR(owner.st_mode) or owner.st_uid!=os.getuid()
            or owner.st_mode & 0o077 or directory.resolve()!=directory):
        raise ValueError('private original adoption directory required')
    files=list(directory.iterdir())
    if len(files)>4096:raise ValueError('adoption bound exceeded')
    matches=[]
    for path in files:
        if path.lstat().st_mode & 0o077:raise ValueError('private original adoption required')
        record=json.loads(owned_file(path))
        if not isinstance(record,dict):raise ValueError('publisher adoption must be an object')
        publisher=record.get('publisher',{})
        if not isinstance(publisher,dict):raise ValueError('publisher identity must be an object')
        if record.get('revision')!=runtime.name or not publisher:continue
        if any(not isinstance(record.get(key),dict) for key in ('config','plist','original_receipt')):
            raise ValueError('publisher enrollment metadata must be objects')
        if (record.get('config',{}).get('sha256')!=hashlib.sha256(owned_file(state/'refresh.json')).hexdigest()
                or record.get('plist',{}).get('sha256')!=hashlib.sha256(owned_file(home/'Library/LaunchAgents/io.coevolution.pulse.stats.cost-refresh.plist')).hexdigest()):continue
        digest=record.get('original_receipt',{}).get('sha256','')
        archive=state/'retention-receipts'/(digest+'.json')
        archive_parent=archive.parent.lstat()
        if (archive.parent.resolve()!=archive.parent or archive_parent.st_uid!=os.getuid()
                or archive_parent.st_mode & 0o077 or archive.lstat().st_mode & 0o077):
            raise ValueError('private original receipt required')
        original_body=owned_file(archive);original=json.loads(original_body);identity=runtime.lstat()
        if not isinstance(original,dict):raise ValueError('original publisher receipt must be an object')
        if (path.name!=digest+'.json' or hashlib.sha256(original_body).hexdigest()!=digest
                or record.get('schema')!='pulse_stats_publisher_enrollment/v1'
                or publisher.get('policy')!='publisher-exclusive/v1' or original.get('publisher_policy')!=publisher['policy']
                or record.get('original_receipt',{}).get('immutable_path')!=str(archive)
                or original.get('status')!='publisher_payload_selected'
                or any(original.get(k)!=record.get(k) for k in ('revision','previous'))
                or original.get('selected_at')!=record.get('recorded_at')
                or record.get('config',{}).get('sha256')!=hashlib.sha256(owned_file(state/'refresh.json')).hexdigest()
                or record.get('plist',{}).get('sha256')!=hashlib.sha256(owned_file(home/'Library/LaunchAgents/io.coevolution.pulse.stats.cost-refresh.plist')).hexdigest()
                or publisher.get('path')!=str(runtime)
                or (identity.st_dev,identity.st_ino)!=(publisher.get('device'),publisher.get('inode'))
                or hashlib.sha256(manifest_body).hexdigest()!=publisher.get('manifest_sha256')
                or hashlib.sha256(owned_file(runtime/'Stats/publisher.py')).hexdigest()!=publisher.get('entry_sha256')):
            raise ValueError('original publisher adoption differs')
        matches.append(record)
    if not matches:raise ValueError('original publisher enrollment required')
    capability=lambda record:tuple(record['publisher'].get(key) for key in
        ('policy','path','device','inode','manifest_sha256','entry_sha256'))
    if any(capability(record)!=capability(matches[0]) for record in matches):
        raise ValueError('ambiguous original publisher capability')
    # Repeated original selections with identical verified capability retain all
    # audit. This equality is only a loading fact, never business acceptance.
    return matches[0]


def publish(home, config_path, *, lock_timeout=30, exclusive=True):
    state = home/'.local/state/pulse/stats'
    if config_path != state/'refresh.json':
        raise ValueError('only the formal local publisher configuration is supported')
    owner = state.lstat()
    if (not stat.S_ISDIR(owner.st_mode) or owner.st_uid != os.getuid()
            or owner.st_mode & 0o077 or state.resolve() != state):
        raise ValueError('private real publisher state required')
    lock_path = state/'deployment.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'rb') as lock:
        opened = os.fstat(lock.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or opened.st_mode & 0o077 or opened.st_nlink != 1):
            raise ValueError('publisher deployment lock differs')
        deadline = time.monotonic() + lock_timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('publisher deployment remains busy')
                time.sleep(.05)
        bound = lock_path.lstat()
        if (bound.st_dev, bound.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError('publisher deployment lock was replaced')
        # No release module is opened or imported before the lock above.
        if config_path.lstat().st_mode & 0o077:
            raise ValueError('private publisher configuration required')
        config = json.loads(owned_file(config_path))
        if not isinstance(config,dict):raise ValueError('publisher configuration must be an object')
        revision = config.get('source_revision')
        if not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{40}', revision):
            raise ValueError('exact selected publisher revision required')
        root = home/'.local/share/pulse/stats'
        runtime = root/('publisher-releases' if exclusive else 'releases')/revision
        current = root/'current'
        if (runtime.is_symlink() or runtime.resolve() != runtime
                or (not exclusive and (not current.is_symlink() or current.resolve() != runtime))):
            raise ValueError('publisher selector differs')
        info = runtime.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ValueError('private selected runtime required')
        if owned_file(runtime/'.stats-source-revision').decode().strip() != revision:
            raise ValueError('publisher runtime marker differs')
        script = runtime/'Scripts/pulse-stats-cost-refresh.py'
        if script.parent.is_symlink():
            raise ValueError('publisher source parent differs')
        manifest_body = owned_file(runtime/'.stats-files.json')
        manifest = json.loads(manifest_body)
        if (not isinstance(manifest, dict) or hashlib.sha256(owned_file(script)).hexdigest()
                != manifest.get('Scripts/pulse-stats-cost-refresh.py')):
            raise ValueError('publisher source differs from its manifest')
        retention_module = None
        if exclusive:
            record = exclusive_adoption(home, runtime, manifest_body)
            helper = runtime/'Stats/publisher_retention.py'
            if hashlib.sha256(owned_file(helper)).hexdigest() != manifest.get('Stats/publisher_retention.py'):
                raise ValueError('publisher retention source differs')
            retention_module = runpy.run_path(str(helper))
            retention_module['payload'](runtime, record)
        started_at = time.time()
        previous_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        previous_argv = sys.argv
        try:
            sys.argv = [str(script), 'refresh', '--config', str(config_path)]
            # Keep this lock until the versioned program and its finally blocks
            # return. Deployment cannot switch/delete a loaded runtime meanwhile.
            try:
                runpy.run_path(str(script), run_name='__main__')
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    raise
            if exclusive:
                # Acceptance/retirement failure must not turn successful business
                # into a retry or stop the service. Its private audit is separate.
                retention_module['after_publication'](home, runtime, config_path, started_at, lock.fileno())
        finally:
            sys.argv = previous_argv
            sys.dont_write_bytecode = previous_bytecode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    try:
        if sys.executable != str(home/'.local/share/coevolution/python-env/bin/python'):
            raise ValueError('canonical publisher Python required')
        installed = Path(__file__)
        directory = installed.parent.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or stat.S_IMODE(directory.st_mode) != 0o700
                or installed.parent.resolve() != installed.parent
                or stat.S_IMODE(installed.lstat().st_mode) != 0o600
                or installed.lstat().st_nlink != 1):
            raise ValueError('private immutable publisher entry required')
        if (installed.parent != home/'.local/share/pulse/stats/publishers'
                or installed.name != hashlib.sha256(owned_file(installed)).hexdigest()+'.py'):
            raise ValueError('immutable publisher entry identity differs')
        publish(home, args.config)
    except (OSError, ValueError, KeyError, TypeError, TimeoutError) as exc:
        print(json.dumps({'event': 'publisher_entry_failed', 'error_type': type(exc).__name__}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
