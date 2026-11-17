#!/usr/bin/env python3
"""Read-only, bounded protocol/authority probe. Never logs model/user content."""
from __future__ import annotations
import collections, concurrent.futures, datetime, hashlib, json, os, pathlib, plistlib, re, shlex, subprocess, sys

REMOTE = r'''
import collections,datetime,hashlib,json,os,pathlib,plistlib,re,sys
home=pathlib.Path.home();node=sys.argv[1]
now=datetime.datetime.now(datetime.timezone.utc)
envs=[]
keys=('HARNESS_ROOT','RELAY_PI_RUNTIME_MANIFEST_PATH','PULSE_COMPUTECOST_RECEIPT_DIR','RELAY_STATE_DIR','CODEX_HOME')
if sys.platform=='darwin':
 p=home/'Library/LaunchAgents/com.pulse.relay.plist'
 envs=[{k:v for k,v in plistlib.loads(p.read_bytes()).get('EnvironmentVariables',{}).items() if k in keys}]
else:
 for proc in pathlib.Path('/proc').glob('[0-9]*'):
  try:
   if proc.stat().st_uid!=os.getuid():continue
   cmd=(proc/'cmdline').read_bytes()
   if b'node' not in cmd or not any(k in cmd.lower() for k in (b'relay',b'index.js')):continue
   env=dict(v.split(b'=',1) for v in (proc/'environ').read_bytes().split(b'\0') if b'=' in v)
   chosen={k:env[k.encode()].decode() for k in keys if k.encode() in env}
   if chosen:envs.append(chosen)
  except (OSError,UnicodeError):continue
reg=json.loads((home/'.local/state/pulse/compute-cost/sources.json').read_text())
print('CONFIG '+json.dumps({'node':node,'observed_at':now.isoformat(),'runtime_env':envs,'registered_sources':reg['sources']},sort_keys=True),flush=True)
roots=[];harness=[]
for env in envs:
 if env.get('HARNESS_ROOT'):harness.append(pathlib.Path(env['HARNESS_ROOT']))
 if env.get('RELAY_PI_RUNTIME_MANIFEST_PATH'):
  manifest=pathlib.Path(env['RELAY_PI_RUNTIME_MANIFEST_PATH']);obj=json.loads(manifest.read_text())
  for name,profile in obj.get('profiles',{}).items():
   if profile.get('sessionRoot'):roots.append(pathlib.Path(profile['sessionRoot']))
   print('PROFILE '+json.dumps({'node':node,'profile':name,'sessionRoot':profile.get('sessionRoot'),'computeCostHost':{k:v for k,v in profile.get('computeCostHost',{}).items() if k in ('sclHarnessRoot','sclKernelRoot')}},sort_keys=True),flush=True)
if not roots:
 roots=[home/('.relay/pi-sessions' if node=='vultr' else '.relay/tcloud-pulse/pi-sessions')]
for root in sorted(set(roots)):
 counts=collections.Counter();samples={}
 for f in sorted(root.rglob('*.jsonl')):
  try:
   if f.is_symlink() or f.stat().st_size>128*1024*1024:counts['unsafe_or_bound']+=1;continue
   with f.open('rb') as handle:raw=handle.readline(256*1024)
   first=json.loads(raw)
   sig=json.dumps({'keys':sorted(first) if isinstance(first,dict) else type(first).__name__,
     'type':first.get('type') if isinstance(first,dict) else None,
     'schema':(first.get('schema') or first.get('schema_version')) if isinstance(first,dict) else None,
     'role':first.get('role') if isinstance(first,dict) else None},sort_keys=True)
   counts[sig]+=1
   if sig not in samples:
    name=re.sub(r'[a-f0-9]{8}(-[a-f0-9-]+)?','<id>',str(f.relative_to(root)))
    samples[sig]={'name_pattern':name,'file_sha256':hashlib.sha256(raw).hexdigest(),'size':f.stat().st_size,'newline':raw.endswith(b'\n')}
  except (OSError,ValueError):counts['unreadable_or_non_json']+=1
 print('PROTOCOL '+json.dumps({'node':node,'root':str(root),'formats':dict(counts),'samples':samples},sort_keys=True),flush=True)
try:import yaml
except ImportError:yaml=None
for root in sorted(set(harness+[pathlib.Path(s['path']) for s in reg['sources'] if s['kind']=='harness_root'])):
 if (root/'.harness').is_dir():root=root/'.harness'
 report={'node':node,'root':str(root),'executions':[],'artifact_schemas':{}}
 for f in sorted((root/'executions').glob('*.yaml')):
  if not yaml:break
  d=yaml.safe_load(f.read_text()); safe={k:d.get(k) for k in ('work_item_id','status','agent_id','active_executor','epoch','phase')}
  def shape(v,depth=0):
   if depth>5:return type(v).__name__
   if isinstance(v,dict):return {k:shape(x,depth+1) for k,x in v.items()}
   if isinstance(v,list):return [shape(v[-1],depth+1)] if v else []
   if isinstance(v,str) and (v.startswith('wi_') or v in ('pi','codex','active','completed','pulse','running')):return v
   return type(v).__name__
  safe['shape']=shape(d);report['executions'].append(safe)
 for folder in ('evidence/compute-cost/attributions','evidence/compute-cost/observations'):
  counter=collections.Counter();examples={}
  for f in (root/folder).rglob('*.json'):
   try:
    d=json.loads(f.read_text());schema=d.get('schema') or d.get('schema_version') or 'none';counter[schema]+=1
    if schema not in examples:examples[schema]=shape(d)
   except (OSError,ValueError):counter['invalid']+=1
  report['artifact_schemas'][folder]={'counts':dict(counter),'shapes':examples}
 print('HARNESS '+json.dumps(report,sort_keys=True),flush=True)
'''

def main():
    home=pathlib.Path.home()
    targets=[('ownermac',[str(home/'.local/share/coevolution/python-env/bin/python'),'-','ownermac'])]
    for name,alias,prefix in [('vultr','vultr-pulse',[]),('tcloud','tcloud-node2',['runuser','-u','pulse','--'])]:
        cmd=prefix+['env','PYTHONDONTWRITEBYTECODE=1','/usr/bin/python3','-',name]
        targets.append((name,['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2',alias,shlex.join(cmd)]))
    def one(item):
        name,cmd=item
        try:
            p=subprocess.run(cmd,input=REMOTE,text=True,capture_output=True,timeout=100)
            if p.returncode:return json.dumps({'node':name,'error':'connect_timeout' if 'Connection timed out' in p.stderr else 'probe_failed','error_hash':hashlib.sha256(p.stderr.encode()).hexdigest()})
            return p.stdout
        except subprocess.TimeoutExpired:return json.dumps({'node':name,'error':'probe_timeout'})
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(one,targets):print(result,flush=True)
if __name__=='__main__':main()
