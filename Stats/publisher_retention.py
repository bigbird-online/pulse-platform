"""Prospective formal Mac publisher payloads; never reclaim shared releases.

The original installation and publication receipts are facts; refresh.json is
still the sole selector. This policy covers enrolled consumers only. Unknown
configuration, content or references fail closed and leave private audit.
"""
from __future__ import annotations
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import time

POLICY = 'publisher-exclusive/v1'
LABEL = 'io.coevolution.pulse.stats.cost-refresh'
SHA = re.compile('[a-f0-9]{40}')


def read(path, limit=2*1024*1024, *, private=False):
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid not in (0, os.getuid())
            or before.st_mode & 0o022 or before.st_nlink != 1 or before.st_size > limit
            or path.resolve() != path or (private and (before.st_uid!=os.getuid() or before.st_mode & 0o077))):
        raise ValueError('unsafe publisher evidence')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno()); body = stream.read(limit+1)
    key = lambda s: (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    if len(body)>limit or key(before)!=key(opened) or key(opened)!=key(path.lstat()):
        raise ValueError('publisher evidence changed')
    return body


def object_value(value):
    if not isinstance(value,dict):raise ValueError('publisher evidence must be an object')
    return value


def json_object(body):
    return object_value(json.loads(body))


def epoch(value):
    if not isinstance(value,str):raise ValueError('publisher timestamp must be text')
    stamp=datetime.fromisoformat(value)
    if stamp.tzinfo is None:raise ValueError('publisher timestamp needs timezone')
    return stamp.timestamp()


def destination_identity(config):
    destination=config.get('destination')
    if destination is not None:object_value(destination)
    return hashlib.sha256(json.dumps(destination,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def private_directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode)!=0o700 or path.resolve()!=path):
        raise ValueError('private publisher directory required')
    return (info.st_dev, info.st_ino)


def once(path, value):
    path.parent.mkdir(mode=0o700, exist_ok=True)
    private_directory(path.parent)
    body=json.dumps(value,sort_keys=True,indent=2).encode()+b'\n'
    # A direct exclusive open never overwrites audit, and O_NOFOLLOW rejects aliases.
    try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        if read(path)!=body:raise ValueError('immutable publisher audit differs')
        return
    with os.fdopen(fd,'wb') as out:out.write(body);out.flush();os.fsync(out.fileno())


def adoptions(home):
    state=home/'.local/state/pulse/stats';root=home/'.local/share/pulse/stats/publisher-releases'
    records={}; directory=state/'retention-adoptions'
    private_directory(directory)
    files=sorted(directory.iterdir())
    if len(files)>4096:raise ValueError('publisher adoption bound exceeded')
    for path in files:
        record=json_object(read(path,private=True)); publisher=record.get('publisher')
        if publisher is not None:object_value(publisher)
        if publisher is None:continue  # Immutable legacy indexes remain protected.
        revision=record.get('revision','');digest=object_value(record.get('original_receipt',{})).get('sha256','')
        if (record.get('schema')!='pulse_stats_adoption_retention/v1' or not SHA.fullmatch(revision)
                or publisher.get('policy')!=POLICY or publisher.get('path')!=str(root/revision)
                or path.name!=digest+'.json'):
            raise ValueError('unknown publisher adoption')
        archive=state/'retention-receipts'/(digest+'.json')
        if record['original_receipt'].get('immutable_path')!=str(archive):raise ValueError('receipt path differs')
        body=read(archive,private=True);original=json_object(body)
        if (hashlib.sha256(body).hexdigest()!=digest or original.get('publisher_policy')!=POLICY
                or original.get('status') not in ('installed','activated_pending_business_readback')
                or any(original.get(k)!=record.get(k) for k in ('revision','previous'))
                or original.get('completed_at')!=record.get('recorded_at')):
            raise ValueError('publisher original receipt differs')
        if revision in records:raise ValueError('ambiguous publisher adoption')
        records[revision]=(record,digest)
    return records


def payload(path, record):
    expected=record['publisher']; identity=private_directory(path)
    if identity!=(expected['device'],expected['inode']):raise ValueError('publisher payload identity differs')
    marker=read(path/'.stats-source-revision').decode().strip()
    manifest_body=read(path/'.stats-files.json');manifest=json_object(manifest_body)
    if marker!=record['revision'] or hashlib.sha256(manifest_body).hexdigest()!=expected['manifest_sha256']:
        raise ValueError('publisher manifest differs')
    if not isinstance(manifest,dict) or len(manifest)>4096:raise ValueError('publisher manifest bound')
    actual=set();allocated=0
    for directory,dirs,files in os.walk(path,followlinks=False):
        base=Path(directory);info=base.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_dev!=identity[0]
                or info.st_mode & 0o022):raise ValueError('publisher payload directory differs')
        allocated+=info.st_blocks*512
        for name in dirs:
            if (base/name).is_symlink():raise ValueError('publisher payload symlink')
        for name in files:
            item=base/name;rel=item.relative_to(path).as_posix();actual.add(rel)
            if rel in ('.stats-files.json','.stats-source-revision'):read(item)
            elif rel not in manifest:raise ValueError('unlisted publisher payload')
            else:
                if Path(rel).is_absolute() or '..' in Path(rel).parts:raise ValueError('unsafe manifest path')
                if hashlib.sha256(read(item,64*1024*1024)).hexdigest()!=manifest[rel]:raise ValueError('publisher file differs')
            info=item.lstat()
            if info.st_uid!=os.getuid() or info.st_dev!=identity[0]:raise ValueError('publisher file ownership differs')
            allocated+=info.st_blocks*512
    if actual!=set(manifest)|{'.stats-files.json','.stats-source-revision'}:raise ValueError('publisher payload incomplete')
    return allocated


def loaded(home):
    result=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/{LABEL}'],capture_output=True,timeout=15,check=True)
    text=result.stdout.decode();args=re.search(r'^\s*arguments = \{\n(.*?)\n\s*\}',text,re.M|re.S)
    pid=re.search(r'^\s*pid = (\d+)',text,re.M);runs=re.search(r'^\s*runs = (\d+)',text,re.M)
    interval=re.search(r'^\s*run interval = (\d+) seconds\s*$',text,re.M)
    if not args or not pid or not runs or not interval or int(interval[1])!=1800:
        raise ValueError('formal publisher process or native cadence unconfirmed')
    return [line.strip() for line in args[1].splitlines()],int(pid[1]),int(runs[1])


def expected_arguments(home, record):
    entry=home/'.local/share/pulse/stats/publishers'/(record['publisher']['entry_sha256']+'.py')
    if hashlib.sha256(read(entry)).hexdigest()!=record['publisher']['entry_sha256']:raise ValueError('entry differs')
    return [str(home/'.local/share/coevolution/python-env/bin/python'),str(entry),'--config',str(home/'.local/state/pulse/stats/refresh.json')]


def destination_readback(home, config):
    """Read back the publication the reader consumes, and any remote copy."""
    dashboard=home/'.local/state/pulse/stats/cost-dashboard-v1.json';before=read(dashboard,512*1024)
    local=json_object(before)
    proof={'bytes':len(before),'sha256':hashlib.sha256(before).hexdigest(),'generated_at':local['generated_at']}
    peer=config.get('destination')
    if peer is None:
        if read(dashboard,512*1024)!=before or local['source_revision']!=config['source_revision']:
            raise ValueError('local publication generation differs')
        return proof
    if peer['python']!='/home/pulse/.local/share/coevolution/python-env/bin/python':raise ValueError('destination Python differs')
    import shlex
    code='import pathlib,json,hashlib;p=pathlib.Path.home()/".local/state/pulse/stats/cost-dashboard-v1.json";b=p.read_bytes();print(json.dumps({"bytes":len(b),"sha256":hashlib.sha256(b).hexdigest(),"generated_at":json.loads(b)["generated_at"]}))'
    argv=[*peer.get('prefix',[]),peer['python'],'-B','-c',code]
    result=subprocess.run(['/usr/bin/ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',peer['host'],shlex.join(argv)],capture_output=True,timeout=20,check=True)
    if len(result.stdout)>4096 or result.stderr:raise ValueError('destination readback incomplete')
    remote=json_object(result.stdout)
    if read(dashboard,512*1024)!=before or remote!=proof or local['source_revision']!=config['source_revision']:
        raise ValueError('destination generation differs')
    return proof


def input_signature(home,config):
    compatible={k:v for k,v in config.items() if k!='source_revision'}
    registry=read(home/'.local/state/pulse/compute-cost/sources.json',private=True)
    return hashlib.sha256(json.dumps(compatible,sort_keys=True,separators=(',',':')).encode()+hashlib.sha256(registry).digest()).hexdigest()


def complete_business(report, revision, config=None):
    """Every reported peer was read, or paused by decision, and the publication landed.

    With config, the peers must be exactly those registered in refresh.json
    and the destination state must match its optional destination.
    """
    object_value(report)
    peers=report.get('peers')
    if not (report.get('source_revision')==revision and report.get('destination') in ('published','not_configured')
            and report.get('codex_quota')=='published' and report.get('cc_quota')=='published'
            and isinstance(peers,list) and peers
            and all(isinstance(p,dict) and (p.get('status')=='read'
                    or (p.get('status'),p.get('reason'))==('paused','paused_by_decision')) for p in peers)):
        return False
    if config is None:
        return True
    registered=[p['id'] for p in config['peers']]
    destination='not_configured' if config.get('destination') is None else 'published'
    return report['destination']==destination and sorted(p.get('id') for p in peers)==sorted(registered)


def held_lock(home, name, fd):
    path=home/'.local/state/pulse/stats'/name
    bound=path.lstat();opened=os.fstat(fd)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_uid!=os.getuid() or opened.st_mode & 0o077
            or opened.st_nlink!=1 or (bound.st_dev,bound.st_ino)!=(opened.st_dev,opened.st_ino)):
        raise ValueError('publisher lock identity differs')
    # The caller passes the same open-file description acquired in canonical order.
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)


def accepted(home, record, digest, signature):
    directory=home/'.local/state/pulse/stats/publisher-publications'/digest
    if not directory.exists():return False
    private_directory(directory);proofs=[];paths=list(directory.iterdir())
    if len(paths)>4096:raise ValueError('publication proof bound exceeded')
    config=json_object(read(home/'.local/state/pulse/stats/refresh.json',private=True))
    destination_binding=destination_identity(config)
    for path in paths:
        proof=json_object(read(path,private=True));destination=object_value(proof.get('destination',{}))
        report=json_object(proof.get('report_body',''))
        completed=epoch(report.get('generated_at'));generation=epoch(destination.get('generated_at'))
        started=proof.get('started_at')
        if (proof.get('schema')!='pulse_stats_publisher_publication/v1' or proof.get('adoption')!=digest
                or proof.get('revision')!=record['revision'] or proof.get('report_sha256','')+'.json'!=path.name
                or proof.get('entry_sha256')!=record['publisher']['entry_sha256']
                or not re.fullmatch('[a-f0-9]{64}',proof.get('report_sha256',''))
                or hashlib.sha256(proof['report_body'].encode()).hexdigest()!=proof['report_sha256']
                or not complete_business(report,record['revision']) or completed!=proof.get('completed_at')
                or type(started) not in (int,float) or not epoch(record['recorded_at'])<=started<=generation<=completed
                or not re.fullmatch('[a-f0-9]{64}',proof.get('destination_identity',''))
                or not re.fullmatch('[a-f0-9]{64}',proof.get('input_signature',''))
                or not re.fullmatch('[a-f0-9]{64}',destination.get('sha256',''))
                or type(destination.get('bytes')) is not int or not 0<destination['bytes']<=512*1024
                or type(proof.get('run_count')) is not int or proof['run_count']<1):
            raise ValueError('publication proof differs')
        # Historical input/destination changes hold prior acceptance. They do not
        # make an intrinsically valid immutable publication proof corrupt.
        if proof.get('input_signature')==signature:
            if (proof['destination_identity']!=destination_binding
                    or not complete_business(report,record['revision'],config)):
                raise ValueError('publication destination differs')
            proofs.append(proof)
    proofs.sort(key=lambda p:p['completed_at'])
    return any(b['run_count']>a['run_count'] and b['started_at']-a['started_at']>=1800
               for a,b in zip(proofs,proofs[1:]))


def reference_snapshot(home, candidate):
    """Bounded enrolled consumers; an extra pool reference means HOLD."""
    state=home/'.local/state/pulse/stats';inputs={};needle=str(candidate).encode()
    paths=[state/'refresh.json',state/'deployment.json',home/'.local/state/pulse/compute-cost/sources.json',
           home/'.codex/hooks.json',home/'.codex/config.toml',home/'.claude/settings.json']
    for directory in [home/'Library/LaunchAgents',Path('/Library/LaunchAgents'),Path('/Library/LaunchDaemons')]:
        if not directory.exists():continue
        if directory.is_symlink():raise ValueError('consumer directory differs')
        members=list(directory.glob('*.plist'))
        if len(members)>256:raise ValueError('consumer bound exceeded')
        paths.extend(members)
    for path in paths:
        if not path.exists() and not path.is_symlink():inputs[str(path)]='absent';continue
        body=read(path)
        if needle in body or b'publisher-releases' in body or candidate.name.encode() in body:
            raise ValueError('offline publisher reference')
        info=path.lstat()
        inputs[str(path)]={'sha256':hashlib.sha256(body).hexdigest(),'device':info.st_dev,'inode':info.st_ino,
                           'mode':info.st_mode,'uid':info.st_uid,'gid':info.st_gid,'nlink':info.st_nlink,
                           'size':info.st_size,'mtime_ns':info.st_mtime_ns,'ctime_ns':info.st_ctime_ns}
    cron=subprocess.run(['/usr/bin/crontab','-l'],capture_output=True,timeout=10)
    if cron.returncode not in (0,1) or (cron.returncode==1 and b'no crontab for' not in cron.stderr):raise ValueError('crontab unknown')
    if needle in cron.stdout or b'publisher-releases' in cron.stdout or candidate.name.encode() in cron.stdout:
        raise ValueError('cron publisher reference')
    inputs['crontab']=hashlib.sha256(cron.stdout).hexdigest()
    processes=subprocess.run(['/bin/ps','-axo','pid=,command='],capture_output=True,timeout=10,check=True)
    # The formal entry only names its immutable launcher. Any direct pool command holds.
    if needle in processes.stdout or b'publisher-releases' in processes.stdout:
        raise ValueError('running publisher reference')
    # Exit1 with no output is the documented no-selection lsof result. Any error holds.
    refs=subprocess.run(['/usr/sbin/lsof','-nP','+D',str(candidate)],capture_output=True,timeout=20)
    if refs.returncode!=1 or refs.stdout or refs.stderr:raise ValueError('publisher FD closure unknown or active')
    return inputs


def retire(home, current, records, deployment_fd, cost_fd):
    held_lock(home,'deployment.lock',deployment_fd)
    held_lock(home,'cost-refresh.lock',cost_fd)
    selected,digest=records[current];prior=selected['previous']
    signature=input_signature(home,json_object(read(home/'.local/state/pulse/stats/refresh.json')))
    if prior not in records or not accepted(home,selected,digest,signature):return {'status':'protected','reason':'current_not_accepted'}
    previous,previous_digest=records[prior]
    if not accepted(home,previous,previous_digest,signature):return {'status':'protected','reason':'rollback_not_accepted'}
    expired=previous['previous']
    if expired not in records or expired in (current,prior):return {'status':'protected','reason':'no_exclusive_predecessor'}
    record,_=records[expired];root=home/'.local/share/pulse/stats/publisher-releases';candidate=root/expired
    if not candidate.exists() and not candidate.is_symlink():return {'status':'no_candidates'}
    # Both the active and rollback payload must be complete and able to read the retained registry.
    payload(root/current,selected);payload(root/prior,previous)
    import runpy
    installer=runpy.run_path(str(root/current/'Stats/deploy.py'))
    registry=json_object(read(home/'.local/state/pulse/compute-cost/sources.json'))
    installer['verify_reader_features'](root/prior,registry)
    config_path=home/'.local/state/pulse/stats/refresh.json';config=read(config_path)
    if json.loads(config).get('source_revision')!=current:raise ValueError('publisher CAS changed')
    job=home/'Library/LaunchAgents'/f'{LABEL}.plist';job_bytes=read(job)
    if plistlib.loads(job_bytes)['ProgramArguments']!=expected_arguments(home,selected):raise ValueError('publisher job changed')
    before=reference_snapshot(home,candidate);allocated=payload(candidate,record)
    stage=root/'.retiring';stage.mkdir(mode=0o700,exist_ok=True)
    if private_directory(stage)[0]!=private_directory(root)[0] or list(stage.iterdir()):raise ValueError('publisher stage not empty')
    state=home/'.local/state/pulse/stats';audits=state/'publisher-retirements'
    audits.mkdir(mode=0o700,exist_ok=True);private_directory(audits)
    transaction=hashlib.sha256((digest+expired).encode()).hexdigest();intent=audits/(transaction+'-intent.json')
    receipt={'schema':'pulse_stats_publisher_retirement/v1','policy':POLICY,'revision':expired,
             'current':current,'rollback':prior,'adoption':digest,'allocated_bytes':allocated,
             'identity':record['publisher'],'status':'intent','reference_digests':before}
    once(intent,receipt)
    if adoptions(home)!=records:raise ValueError('publisher original evidence changed')
    if not accepted(home,selected,digest,signature) or not accepted(home,previous,previous_digest,signature):
        raise ValueError('publisher acceptance changed')
    if read(config_path)!=config or read(job)!=job_bytes or reference_snapshot(home,candidate)!=before:raise ValueError('publisher reference CAS changed')
    moved=stage/expired
    os.rename(candidate,moved)
    # Never traverse a failed staged tree again; retain it and the original intent.
    try:
        payload(moved,record)
        if read(config_path)!=config or read(job)!=job_bytes or reference_snapshot(home,moved)!=before:
            raise ValueError('publisher staged CAS changed')
        remove_payload(stage,expired,record)
    except Exception:
        once(audits/(transaction+'-failed.json'),{**receipt,'status':'failed'});raise
    once(audits/(transaction+'-completed.json'),{**receipt,'status':'completed'})
    return {'status':'completed','revision':expired,'allocated_bytes':allocated}


def remove_payload(stage, revision, record):
    """Remove only the verified inode through no-follow directory FDs."""
    stage_fd=os.open(stage,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        root_fd=os.open(revision,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=stage_fd)
        try:
            root_info=os.fstat(root_fd);expected=record['publisher']
            if (root_info.st_uid!=os.getuid() or stat.S_IMODE(root_info.st_mode)!=0o700
                    or (root_info.st_dev,root_info.st_ino)!=(expected['device'],expected['inode'])):
                raise ValueError('staged root inode differs before deletion')
            manifest_body=read(stage/revision/'.stats-files.json')
            if hashlib.sha256(manifest_body).hexdigest()!=expected['manifest_sha256']:
                raise ValueError('staged original manifest changed')
            manifest=json_object(manifest_body)
            digests={**manifest,'.stats-files.json':expected['manifest_sha256'],
                     '.stats-source-revision':hashlib.sha256((revision+'\n').encode()).hexdigest()}
            permitted=set(digests)
            seen=set()
            def erase(fd, prefix=''):
                for name in os.listdir(fd):
                    relative=prefix+name;before=os.stat(name,dir_fd=fd,follow_symlinks=False)
                    if before.st_dev!=root_info.st_dev or before.st_uid!=os.getuid() or before.st_mode & 0o022:
                        raise ValueError('staged ownership or mount differs')
                    if stat.S_ISDIR(before.st_mode):
                        child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                        try:
                            identity=os.fstat(child)
                            if (identity.st_dev,identity.st_ino)!=(before.st_dev,before.st_ino):raise ValueError('staged directory changed')
                            erase(child,relative+'/')
                            bound=os.stat(name,dir_fd=fd,follow_symlinks=False)
                            if (bound.st_dev,bound.st_ino)!=(identity.st_dev,identity.st_ino):raise ValueError('staged directory replaced')
                            os.rmdir(name,dir_fd=fd)
                        finally:os.close(child)
                    else:
                        if relative not in permitted or not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_mode & 0o022:
                            raise ValueError('unlisted staged data')
                        child=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
                        try:
                            identity=os.fstat(child)
                            key=lambda x:(x.st_dev,x.st_ino,x.st_mode,x.st_uid,x.st_gid,x.st_nlink,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
                            if key(before)!=key(identity):raise ValueError('staged file replaced')
                            digest=hashlib.sha256()
                            while block:=os.read(child,65536):digest.update(block)
                            if digest.hexdigest()!=digests[relative]:raise ValueError('staged content changed')
                            if key(identity)!=key(os.fstat(child)) or key(identity)!=key(os.stat(name,dir_fd=fd,follow_symlinks=False)):
                                raise ValueError('staged file changed during deletion')
                            os.unlink(name,dir_fd=fd);seen.add(relative)
                        finally:os.close(child)
            erase(root_fd)
            if seen!=permitted:raise ValueError('staged content disappeared')
            bound=os.stat(revision,dir_fd=stage_fd,follow_symlinks=False)
            if (bound.st_dev,bound.st_ino)!=(root_info.st_dev,root_info.st_ino):raise ValueError('staged root replaced')
            os.rmdir(revision,dir_fd=stage_fd)
        finally:os.close(root_fd)
    finally:os.close(stage_fd)


def after_publication(home, runtime, config_path, started_at, deployment_fd):
    """Caller holds deployment.lock from before import; take cost lock second."""
    state=home/'.local/state/pulse/stats'
    fd=None
    try:
        held_lock(home,'deployment.lock',deployment_fd)
        fd=os.open(state/'cost-refresh.lock',os.O_RDWR|os.O_NOFOLLOW)
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        held_lock(home,'cost-refresh.lock',fd)
        config=json_object(read(config_path));revision=config['source_revision'];records=adoptions(home)
        record,digest=records[revision]
        if runtime!=home/'.local/share/pulse/stats/publisher-releases'/revision:raise ValueError('publisher path differs')
        payload(runtime,record)
        job_path=home/'Library/LaunchAgents'/f'{LABEL}.plist'
        job_body=read(job_path,private=True);job=plistlib.loads(job_body)
        registry=home/'.local/state/pulse/compute-cost/sources.json'
        if (hashlib.sha256(read(config_path,private=True)).hexdigest()!=record['config']['sha256']
                or hashlib.sha256(job_body).hexdigest()!=record['plist']['sha256']
                or hashlib.sha256(read(registry,private=True)).hexdigest()!=record['publisher']['source_registry']['sha256']
                or job.get('StartInterval')!=1800):
            raise ValueError('original publisher installation inputs changed')
        for path,binding in ((config_path,record['config']),(job_path,record['plist']),
                             (registry,record['publisher']['source_registry'])):
            info=path.lstat()
            if (info.st_dev,info.st_ino)!=(binding['device'],binding['inode']):
                raise ValueError('original publisher installation inode changed')
        arguments,pid,runs=loaded(home)
        if pid!=os.getpid() or arguments!=expected_arguments(home,record):raise ValueError('not the formal loaded publisher')
        report_body=read(state/'last-refresh.json');report=json_object(report_body)
        completed=epoch(report['generated_at'])
        if (completed<started_at or completed<epoch(record['recorded_at'])
                or not complete_business(report,revision,config)):
            return {'status':'protected','reason':'business_acceptance_incomplete'}
        publication={'schema':'pulse_stats_publisher_publication/v1','revision':revision,'adoption':digest,
            'report_sha256':hashlib.sha256(report_body).hexdigest(),'report_body':report_body.decode(),'completed_at':completed,
            'run_count':runs,'started_at':started_at,'destination_identity':destination_identity(config),'entry_sha256':record['publisher']['entry_sha256'],
            'input_signature':input_signature(home,config),
            'destination':destination_readback(home,config)}
        directory=state/'publisher-publications';directory.mkdir(mode=0o700,exist_ok=True);private_directory(directory)
        generation=epoch(publication['destination']['generated_at'])
        if not started_at<=generation<=completed:raise ValueError('destination generation outside publication')
        once(directory/digest/(publication['report_sha256']+'.json'),publication)
        result=retire(home,revision,records,deployment_fd,fd)
        print(json.dumps({'event':'publisher_payload_retirement',**result}))
        return result
    except Exception as exc:
        # This boundary is only the retirement tail, after business succeeded.
        # Audit failure must not turn publication into a retry or exit1.
        error={'event':'publisher_retention_failed','error_type':type(exc).__name__}
        try:
            directory=state/'publisher-retention-errors';directory.mkdir(mode=0o700,exist_ok=True)
            once(directory/(str(time.time_ns())+'.json'),error)
        except Exception:
            error['audit_unavailable']=True
        print(json.dumps(error),file=__import__('sys').stderr)
        return {'status':'failed',**error}
    finally:
        if fd is not None:os.close(fd)
