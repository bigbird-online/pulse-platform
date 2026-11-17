#!/usr/bin/env python3
"""Keep the owner-approved Mac heartbeat sender retired; never enable or modify business cron.

The other five retired probes ran on the Vultr node, which no longer exists.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess

MAC_LABEL = 'com.pulse.heartbeat-send'
DECISION = 'PULSE#603:owner-approved-six-probe-retirement-2026-09-18'


def command(args, *, check=True):
    result=subprocess.run(args,capture_output=True,text=True,timeout=30)
    if check and result.returncode:
        raise RuntimeError(f'{Path(args[0]).name}: returncode={result.returncode}')
    return result


def preserve(path: Path, backup: Path) -> dict:
    """Move only a named retired unit, preserving its original bytes privately."""
    if not path.exists() and not path.is_symlink():
        return {'file':path.name,'before':'absent'}
    if path.is_symlink() and os.readlink(path)=='/dev/null':
        return {'file':path.name,'before':'already_masked'}
    if path.is_symlink():
        raise RuntimeError('Unexpected symlink on retired unit; preserve for inspection')
    body=path.read_bytes();digest=hashlib.sha256(body).hexdigest()
    target=backup/path.name
    if target.exists():
        if target.read_bytes()!=body:
            raise RuntimeError('Retirement preimage differs; refusing replacement')
        path.unlink()
    else:
        os.replace(path,target);target.chmod(0o600)
    return {'file':path.name,'before_sha256':digest}


def apply(home: Path, site: str, *, execute=command) -> dict:
    if site!='ownermac' or home!=Path('/Users/owner'):
        raise ValueError('Only the existing service account may retire this sender')
    state=home/'.local/state/pulse/stats';state.mkdir(parents=True,exist_ok=True,mode=0o700)
    backup=state/'legacy-probe-preimages-20260918';backup.mkdir(mode=0o700,exist_ok=True)
    changes=[]
    domain=f'gui/{os.getuid()}'
    execute(['/bin/launchctl','disable',domain+'/'+MAC_LABEL])
    loaded=execute(['/bin/launchctl','print',domain+'/'+MAC_LABEL],check=False)
    if loaded.returncode==0:execute(['/bin/launchctl','bootout',domain+'/'+MAC_LABEL])
    if execute(['/bin/launchctl','print',domain+'/'+MAC_LABEL],check=False).returncode==0:
        raise RuntimeError('Old heartbeat sender is still loaded')
    path=home/'Library/LaunchAgents'/(MAC_LABEL+'.plist')
    if path.exists():
        if plistlib.loads(path.read_bytes()).get('Label')!=MAC_LABEL:
            raise RuntimeError('Unexpected launch agent identity')
    changes.append(preserve(path,backup))
    receipt=dict(schema='pulse_stats_legacy_retirement/v1',site=site,decision_ref=DECISION,
        observed_at=datetime.now(timezone.utc).isoformat(),status='retired',business_cron_modified=False,
        source_revision=(Path(__file__).resolve().parents[1]/'.stats-source-revision').read_text().strip(),changes=changes)
    marker=state/'legacy-probes-retired.json'
    temporary=state/('.legacy-probes-retired-'+str(os.getpid())+'.json')
    temporary.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n');temporary.chmod(0o600)
    os.replace(temporary,marker)
    return receipt


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site',required=True,choices=('ownermac',))
    args=parser.parse_args()
    print(json.dumps(apply(Path.home(),args.site),ensure_ascii=False))
