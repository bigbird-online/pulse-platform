#!/usr/bin/env python3
"""Read-only inventory of authoritative native measurement bindings, no contents."""
from __future__ import annotations
import concurrent.futures, hashlib, json, pathlib, shlex, subprocess

REMOTE = r'''
import collections,datetime,hashlib,json,os,pathlib,plistlib,sqlite3,sys
home=pathlib.Path.home();node=sys.argv[1];envs=[]
keys=('HARNESS_ROOT','RELAY_PI_RUNTIME_MANIFEST_PATH','PULSE_COMPUTECOST_RECEIPT_DIR','SCL_KERNEL_ROOT')
if sys.platform=='darwin':
 p=home/'Library/LaunchAgents/com.pulse.relay.plist'
 envs=[{k:v for k,v in plistlib.loads(p.read_bytes()).get('EnvironmentVariables',{}).items() if k in keys}]
else:
 for proc in pathlib.Path('/proc').glob('[0-9]*'):
  try:
   if proc.stat().st_uid!=os.getuid() or not pathlib.Path(os.readlink(proc/'exe')).name.startswith('node'):continue
   raw=dict(v.split(b'=',1) for v in (proc/'environ').read_bytes().split(b'\0') if b'=' in v)
   if b'RELAY_PI_RUNTIME_MANIFEST_PATH' not in raw:continue
   envs.append({k:raw[k.encode()].decode() for k in keys if k.encode() in raw})
  except (OSError,UnicodeError):continue
kernels=set();roots=set()
for env in envs:
 if env.get('RELAY_PI_RUNTIME_MANIFEST_PATH'):
  manifest=json.loads(pathlib.Path(env['RELAY_PI_RUNTIME_MANIFEST_PATH']).read_text())
  for profile in manifest.get('profiles',{}).values():
   if profile.get('sessionRoot'):roots.add(profile['sessionRoot'])
   kernel=profile.get('computeCostHost',{}).get('sclKernelRoot')
   if kernel:kernels.add(kernel)
print('LOADED_RUNTIME '+json.dumps({'node':node,'environments':envs,'kernels':sorted(kernels),'roots':sorted(roots)}),flush=True)
for root in sorted(kernels):
 db=pathlib.Path(root)/'kernel.sqlite3'
 if not db.is_file():print('KERNEL_MISSING '+json.dumps({'node':node,'root':root}));continue
 with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as c:
  c.row_factory=sqlite3.Row
  c.execute('BEGIN')
  tables={r[0] for r in c.execute("select name from sqlite_master where type='table'")}
  rows=[dict(r) for r in c.execute('select * from measurement_epochs')] if 'measurement_epochs' in tables else []
  facts=[dict(r) for r in c.execute('select * from measurement_native_facts')] if 'measurement_native_facts' in tables else []
  w={r['work_item_id']:json.loads(r['state_json']) for r in c.execute('select work_item_id,state_json from work_items')}
  summaries=[]
  for row in rows:
   matching=[f for f in facts if f['measurement_epoch_ref']==row['measurement_epoch_ref']]
   details=[]
   for f in matching:
    native=json.loads(f['native_json'])
    details.append({'state':f['lifecycle_state'],'keys':sorted(native),'schema':native.get('schema'),
      'snapshot_keys':sorted(native.get('snapshot',{})),'created_at':f['created_at']})
   state=w.get(row['work_item_id'],{})
   summaries.append({k:row[k] for k in ('work_item_id','execution_epoch','agent_id','status','created_at','updated_at')}|
      {'native':details,'current_epoch':state.get('epoch'),'current_execution_state':state.get('execution_state'),
       'session_matches_current':row.get('host_session_ref')==state.get('session_ref')})
  print('KERNEL_BINDINGS '+json.dumps({'node':node,'root':root,'epochs':summaries},sort_keys=True),flush=True)
for text in sorted(roots):
 root=pathlib.Path(text);snaps=collections.Counter();examples=[];today=collections.Counter();scope_sessions={}
 for f in sorted(root.glob('*/*.jsonl')):
  try:
   with f.open('rb') as h:header=json.loads(h.readline())
   if header.get('type')!='session':continue
   session=header['id'];scope_sessions[session]=f
   for line in f.read_bytes().splitlines():
    try:r=json.loads(line)
    except ValueError:continue
    if r.get('type')=='message' and r.get('message',{}).get('role')=='assistant' and r.get('message',{}).get('usage'):
     today[str(r.get('timestamp',''))[:10]]+=1
  except (OSError,ValueError,KeyError):continue
 for f in sorted(root.glob('*/compute-cost/*.json')):
  try:
   d=json.loads(f.read_bytes());key=(d.get('schema'),d.get('measurement_role'),d.get('work_item_id'));snaps[str(key)]+=1
   if d.get('schema')=='pi_session_usage_snapshot/v2':
    examples.append({k:d.get(k) for k in ('schema','measurement_role','work_item_id','agent_id','epoch','generated_at','runtime_binding_sha256')}|
      {'partitions':len(d.get('summary',{}).get('request_partitions',[]))})
  except (OSError,ValueError):continue
 print('NATIVE_BOUNDARIES '+json.dumps({'node':node,'snapshot_counts':dict(snaps),'bounded':examples,
      'native_usage_by_utc_day':dict(sorted(today.items())[-3:])}),flush=True)
'''

def main():
 home=pathlib.Path.home()
 targets=[('ownermac',[str(home/'.local/share/coevolution/python-env/bin/python'),'-','ownermac'])]
 for node,alias,prefix in [('vultr','vultr-pulse',[]),('tcloud','tcloud-node2',['runuser','-u','pulse','--'])]:
  targets.append((node,['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2',alias,
    shlex.join(prefix+['env','PYTHONDONTWRITEBYTECODE=1','/usr/bin/python3','-',node])]))
 def one(item):
  node,cmd=item
  try:
   result=subprocess.run(cmd,input=REMOTE,text=True,capture_output=True,timeout=120)
   if result.returncode:return json.dumps({'node':node,'failed':True,'error_digest':hashlib.sha256(result.stderr.encode()).hexdigest()})
   return result.stdout
  except subprocess.TimeoutExpired:return json.dumps({'node':node,'failed':'probe_timeout'})
 with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
  for result in pool.map(one,targets):print(result,flush=True)
if __name__=='__main__':main()
