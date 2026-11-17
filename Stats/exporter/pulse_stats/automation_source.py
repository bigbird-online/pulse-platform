"""Bounded read of existing Relay scheduling facts. No execution or source writes."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import os
import platform
import pwd
import stat
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

SCHEMA = 'pulse_stats_automation_source/v1'
MAX_BYTES = 2 * 1024 * 1024
STATE_FIELDS = ('lastRunAt', 'lastStatus', 'consecutiveErrors', 'definitionHash', 'activatedAt')
RESULT_FIELDS = ('runId', 'definitionHash', 'startedAt', 'finishedAt', 'scheduledFor',
                 'status', 'executionStatus', 'deliveryStatus', 'artifactVerified', 'verifyStatus',
                 'skipped', 'deferred', 'reasonCode', 'schedulerPid', 'schedulerStartedAt', 'phase',
                 'skipSummary')


def catalog_digest() -> str:
    return hashlib.sha256((Path(__file__).resolve().parents[1]/'config/automation-tasks.json').read_bytes()).hexdigest()


CATALOG_SCHEMA = 'pulse_stats_automation_catalog/v2'
ADMIT = ('named', 'permitted_own')


def check_catalog(value) -> list[str]:
    """Every difference that keeps the service map from naming each job exactly once."""
    if not isinstance(value, dict) or value.get('schema') != CATALOG_SCHEMA:
        return [f'目录 schema 不是 {CATALOG_SCHEMA}']
    diffs, rows, claimed = [], set(), {}
    services, families = value.get('services'), value.get('families')
    if not isinstance(services, list) or not isinstance(families, list):
        return ['目录缺 services 或 families 列表']
    patterns = []
    for family in families:
        name = family.get('id') if isinstance(family, dict) else None
        if not isinstance(name, str) or name in rows:
            diffs.append(f'家族 id 缺失或重复：{name}'); continue
        rows.add(name)
        try:
            pattern = re.compile(family['pattern'])
            family['label'].format(*(['x'] * pattern.groups), agent='X')
        except (KeyError, TypeError, IndexError, re.error):
            diffs.append(f'家族 {name} 的 pattern 或 label 无效'); continue
        if family.get('admit') not in ADMIT:
            diffs.append(f"家族 {name} 的 admit 不是 {'/'.join(ADMIT)}")
        patterns.append((name, pattern))
    for service in services:
        name = service.get('id') if isinstance(service, dict) else None
        if not isinstance(name, str) or name in rows:
            diffs.append(f'服务 id 缺失或重复：{name}'); continue
        rows.add(name)
        if not all(isinstance(service.get(k), str) and service[k] for k in ('title', 'owner', 'expectation')):
            diffs.append(f'服务 {name} 缺 title、owner 或 expectation')
        stages = service.get('stages')
        if not isinstance(stages, list) or not stages:
            diffs.append(f'服务 {name} 没有阶段'); continue
        jobs = [s.get('job') if isinstance(s, dict) else None for s in stages]
        retired = [r.get('job') if isinstance(r, dict) else None for r in service.get('retired', [])]
        for job in jobs + retired:
            if not isinstance(job, str) or not job:
                diffs.append(f'服务 {name} 有未写任务编号的阶段'); continue
            if job in claimed:
                diffs.append(f'任务 {job} 同时映射到服务 {claimed[job]} 与 {name}')
            claimed[job] = name
            for family, pattern in patterns:
                if pattern.fullmatch(job):
                    diffs.append(f'任务 {job} 同时属于服务 {name} 与家族 {family}')
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            proof = stage.get('proven_by')
            if proof is not None and (proof == stage.get('job') or proof not in jobs):
                diffs.append(f"服务 {name} 的阶段 {stage.get('job')} 由本服务外的 {proof} 证明")
            unproven = stage.get('unproven', [])
            if not isinstance(unproven, list) or not all(isinstance(u, str) and u for u in unproven):
                diffs.append(f"服务 {name} 的阶段 {stage.get('job')} unproven 不是文字列表")
            skip = stage.get('skip')
            if skip is not None:
                try:
                    re.compile(skip.get('attention') or '')
                except (AttributeError, re.error):
                    diffs.append(f"服务 {name} 的阶段 {stage.get('job')} 跳过规则无效")
    return diffs


def catalog() -> dict:
    value = json.loads((Path(__file__).resolve().parents[1]/'config/automation-tasks.json').read_text())
    diffs = check_catalog(value)
    if diffs:
        raise ValueError('自动任务目录映射校验失败：' + '；'.join(diffs))
    return value


# Every node whose scheduler facts the automation card expects. ownermac is the
# reader itself; each peer runs its installed snapshot over the Stats identity
# registered for that host in refresh.json `peers`, with exactly this prefix.
# `coevolution` is the node's Coevolution checkout, whose unified toolchain
# declaration names the Node its scheduler (Relay) runs on.
LOCAL_SITE = 'ownermac'
SITE_SOURCES = {
    'ownermac': dict(coevolution='/Users/owner/Coevolution'),
    'tcloud': dict(host='tcloud-node2', prefix=['runuser', '-u', 'pulse', '--'],
                   coevolution='/Users/owner/Coevolution'),
    'oracle-node-1': dict(host='pulse@oracle-node-1', prefix=[], coevolution='/home/pulse/Coevolution'),
}
SITES = tuple(SITE_SOURCES)
TOOLCHAIN_DECLARATION = 'ssot/toolchain/agent-runtimes.json'
TOOLCHAIN_RUNTIMES = Path(SITE_SOURCES[LOCAL_SITE]['coevolution'])/TOOLCHAIN_DECLARATION
NODE_MISSING = '缺少定义校验依赖：工具链登记的 Node 不可用，尚不能确认运行记录属于当前任务定义。'
NODE_FAILED = '定义校验未完成：登记的 Node 未返回定义摘要，尚不能确认运行记录属于当前任务定义。'


def definition_node(site: str, declaration: Path | None = None, account: str | None = None) -> str | None:
    """Resolve the scheduler's own Node from agent-runtimes.json, never from PATH.

    `{home}` is the executing account's passwd home, not a session HOME; a
    missing or non-executable binding means the definition cannot be checked.
    """
    try:
        declaration = declaration or Path(SITE_SOURCES[site]['coevolution'])/TOOLCHAIN_DECLARATION
        value = json.loads(declaration.read_bytes())
        template = value.get('host_profiles', {}).get(site, {}).get('node') or value['node'][platform.system()]
        node = Path(template.format(home=account or pwd.getpwuid(os.getuid()).pw_dir))
        if node.is_absolute() and node.is_file() and os.access(node, os.X_OK):
            return str(node)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


CONTROL_ENTRY = re.compile(r'^(job|dispatch|resource):[a-zA-Z0-9_.-]{1,160}$')


def read_execution_control(path: Path) -> dict:
    """Read the scheduler's own control file with the Relay acceptance rules."""
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
            or info.st_mode & 0o022 or info.st_size > 65536):
        raise ValueError('unsafe execution control')
    data = json.loads(path.read_bytes())
    if (not isinstance(data, dict) or data.get('schema') != 'pulse_automation_control/v1'
            or type(data.get('automatic_enabled')) is not bool
            or type(data.get('revision')) is not int or data['revision'] < 1
            or not isinstance(data.get('reason'), str) or not data['reason']
            or not isinstance(data.get('decision_ref'), str) or not data['decision_ref']
            or not isinstance(data.get('entries'), dict)):
        raise ValueError('invalid execution control')
    datetime.fromisoformat(data['updated_at'])
    for key, value in data['entries'].items():
        if (not CONTROL_ENTRY.match(key) or not isinstance(value, dict)
                or type(value.get('enabled')) is not bool
                or not isinstance(value.get('owner'), str) or not value['owner']):
            raise ValueError('invalid execution control entry')
    return data


def caption_kind(delivery: dict) -> str | None:
    """Which caption a document delivery carries: `file` (captionPath) or `text` (template)."""
    if delivery.get('mode') not in ('feishu_document', 'telegram_document'):
        return None
    if isinstance(delivery.get('captionPath'), str) and delivery['captionPath'].strip():
        return 'file'
    if isinstance(delivery.get('caption'), str) and delivery['caption'].strip():
        return 'text'
    return None


def select_state(native_state: dict) -> dict:
    """The scheduler state fields Stats reads for one job, bounded and without output."""
    selected = {key: native_state.get(key) for key in STATE_FIELDS}
    receipt = native_state.get('lastResult')
    if isinstance(receipt, dict):
        selected['lastResult'] = {key: receipt.get(key) for key in RESULT_FIELDS}
    selected['unresolvedResults'] = [{k:r.get(k) for k in RESULT_FIELDS} for r in native_state.get('unresolvedResults',[]) if isinstance(r,dict)][:32]
    selected['resultHistoryIncomplete'] = native_state.get('resultHistoryIncomplete') is True
    return selected


def read_json(path: Path) -> tuple[bytes, dict]:
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('oversized source')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('expected object')
    return raw, data


def read_site(home: Path, site: str, observed_at: str) -> dict:
    report = dict(schema=SCHEMA, site=site, observed_at=observed_at, catalog_digest=catalog_digest(), status='unavailable', jobs=[])
    try:
        _, mapping = read_json(home/'.local/share/pulse/config/relay/paths.json')
        if mapping.get('schema') != 'pulse_relay_paths/v1' or mapping.get('site') != site:
            raise ValueError('unregistered site')
        root = Path(mapping['root'])
        if not root.is_absolute() or '..' in root.parts:
            raise ValueError('invalid root')
        # The scheduler's own rule: an undeclared cronState lives in dataDir.
        declared = mapping['paths']
        paths = [Path(declared['cronConfig']),
                 Path(declared.get('cronState') or Path(declared['dataDir'])/'cron-jobs-state.json')]
        for path in paths:
            if not path.is_absolute() or '..' in path.parts or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('source outside installed map')
        config_raw, config = read_json(paths[0])
        state_raw, state = read_json(paths[1])
        if config.get('version') != 2 or not isinstance(config.get('jobs'), list):
            raise ValueError('invalid cron config')
        if state.get('version') != 1 or not isinstance(state.get('jobs'), dict):
            raise ValueError('invalid cron state')
        wanted = {j["id"] for j in config["jobs"] if isinstance(j, dict) and isinstance(j.get("id"), str)}
        try:
            report["execution_control"] = read_execution_control(paths[0].parent / "automation-control.json")
        except (OSError, ValueError, KeyError, TypeError):
            report["execution_control"] = {"automatic_enabled":False, "status":"unavailable", "entries":{}}

        seen = set()
        selected_jobs = [j for j in config['jobs'] if isinstance(j,dict) and j.get('id') in wanted and j.get('enabled') is True]
        hashes = {}
        node = definition_node(site) if selected_jobs else None
        check_reason = NODE_MISSING if selected_jobs and not node else None
        if selected_jobs and node:
            # Match the scheduler's exact canonicalization, not a second hash dialect.
            program = "const fs=require('fs'),crypto=require('crypto'); const c=v=>Array.isArray(v)?v.map(c):v&&typeof v==='object'?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b)).map(([k,x])=>[k,c(x)])):v; console.log(JSON.stringify(Object.fromEntries(JSON.parse(fs.readFileSync(0,'utf8')).map(j=>{const {_state,...d}=j; return [j.id,crypto.createHash('sha256').update(JSON.stringify(c(d))).digest('hex')]}))));"
            try:
                result=subprocess.run([node,'-e',program],input=json.dumps(selected_jobs),capture_output=True,text=True,timeout=3)
                if result.returncode==0:hashes=json.loads(result.stdout)
            except (OSError,ValueError,subprocess.SubprocessError):pass
            if not isinstance(hashes, dict) or not hashes:
                hashes, check_reason = {}, NODE_FAILED
        for job in config['jobs']:
            if not isinstance(job, dict) or not isinstance(job.get('id'), str) or job['id'] in seen:
                raise ValueError('invalid or duplicate job')
            seen.add(job['id'])
            if job['id'] not in wanted:
                continue
            if type(job.get('enabled')) is not bool:
                raise ValueError('enabled must be explicit')
            schedule = job.get('schedule', {})
            if not all(isinstance(schedule.get(k), str) for k in ('expr', 'tz')):
                raise ValueError('missing schedule')
            if not isinstance(job.get('owner'), str):
                raise ValueError('missing owner')
            native_state = state['jobs'].get(job['id'])
            if native_state is not None and not isinstance(native_state, dict):
                raise ValueError('invalid native job state')
            native_state = native_state or {}
            selected = select_state(native_state)
            current = native_state.get('currentRun')
            live = False
            if isinstance(current,dict):
                selected['currentRun']={k:current.get(k) for k in RESULT_FIELDS}
                try:
                    pid=current['schedulerPid']; started=current['schedulerStartedAt']
                    if type(pid) is not int or pid<=0 or type(started) not in (int,float):raise ValueError('invalid PID')
                    result=subprocess.run(['/bin/ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True,timeout=2,env={**os.environ,'LC_ALL':'C'})
                    actual=datetime.strptime(result.stdout.strip(),'%a %b %d %H:%M:%S %Y').astimezone().timestamp()
                    live=abs(actual-started/1000)<2
                except (KeyError,ValueError,OSError,subprocess.SubprocessError):pass
            agent = job.get('agent') or {}
            report['jobs'].append(dict(id=job['id'], owner=job['owner'], enabled=job['enabled'],
                **({'name': job['name']} if isinstance(job.get('name'), str) and job['name'].strip() else {}),
                run_node=job.get('runNode'), schedule={key:schedule[key] for key in ('expr','tz')},
                requires_artifact=isinstance(agent.get('expectArtifact'), dict),
                requires_verify=isinstance(agent.get('verify'), dict),
                delivery=(job.get('delivery') or {}).get('mode'),
                skip_allowed=bool((job.get('delivery') or {}).get('skipIfOutputIncludes')) or bool(agent.get('gate')),
                convergent=job.get('convergent') is True,
                **({'caption': caption} if (caption := caption_kind(job.get('delivery') or {})) else {}),
                state=selected, definition_verified=bool(hashes.get(job['id']) and hashes[job['id']]==selected.get('definitionHash')), current_run_live=live,
                **({'definition_reason': check_reason} if check_reason and job['enabled'] else {})))
        # A retired job the service map names keeps its scheduler state after its
        # definition is deleted; its unsettled rounds are history, never a run.
        for service in catalog()['services']:
            for old in service.get('retired', []):
                native_state = state['jobs'].get(old['job'])
                if old['job'] in seen or not isinstance(native_state, dict):
                    continue
                report['jobs'].append(dict(id=old['job'], owner=service['owner'], enabled=False, retired_state_only=True,
                    schedule=None, delivery=None, requires_artifact=False, requires_verify=False, skip_allowed=False,
                    convergent=False, definition_verified=False, current_run_live=False, state=select_state(native_state)))
        # Configuration edits during this read cannot claim a coherent snapshot.
        if paths[0].read_bytes() != config_raw or paths[1].read_bytes() != state_raw:
            raise ValueError('source changed during observation')
        if check_reason:
            report['definition_check'] = check_reason
        report.update(status='available', config_digest=hashlib.sha256(config_raw).hexdigest(),
                      state_digest=hashlib.sha256(state_raw).hexdigest())
    except (OSError, ValueError, KeyError, TypeError):
        report.update(status='unavailable', jobs=[], reason='当前节点的任务来源不可核验')
    return report


# Both peers install Stats under the same pulse account paths.
PEER_PYTHON = '/home/pulse/.local/share/coevolution/python-env/bin/python'
PEER_SNAPSHOT = '/home/pulse/.local/share/pulse/stats/current/Stats/exporter/automation-snapshot.py'


def read_peer(peer: dict, site: str, observed_at: str) -> dict:
    """Run the peer's installed read-only snapshot over its registered identity."""
    result = dict(schema=SCHEMA, site=site, observed_at=observed_at, status='unavailable', jobs=[])
    try:
        prefix = peer.get('prefix', [])
        if prefix != SITE_SOURCES[site]['prefix'] or peer['python'] != PEER_PYTHON:
            return result
        command = [*prefix, peer['python'], PEER_SNAPSHOT, '--site', site]
        value = subprocess.run(['/usr/bin/ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','ConnectTimeout=5',peer['host'],shlex.join(command)],capture_output=True,text=True,timeout=9)
        if value.returncode != 0 or len(value.stdout.encode()) > 131072:
            return result
        report = json.loads(value.stdout)
        if report.get('site') != site or report.get('schema') != SCHEMA:
            return result
        return report
    except (KeyError, ValueError, OSError, subprocess.SubprocessError):
        return result


def gather(home: Path) -> dict:
    """This node's facts plus each registered peer site, read concurrently."""
    observed = datetime.now(timezone.utc).isoformat()
    try:
        config = json.loads((home/'.local/state/pulse/stats/refresh.json').read_text())
        peers = {p['host']: p for p in config['peers']}
    except (OSError, ValueError, KeyError, TypeError):
        peers = {}
    def remote(site):
        host = SITE_SOURCES[site]['host']
        if host not in peers:
            return dict(schema=SCHEMA, site=site, observed_at=observed, status='unavailable', jobs=[])
        return read_peer(peers[host], site, observed)
    with ThreadPoolExecutor(max_workers=len(SITES)) as pool:
        local = pool.submit(read_site, home, LOCAL_SITE, observed)
        values = list(pool.map(remote, [site for site in SITES if site != LOCAL_SITE]))
        values.insert(0, local.result())
    return dict(schema='pulse_stats_automation_sources/v1', sources=values)
