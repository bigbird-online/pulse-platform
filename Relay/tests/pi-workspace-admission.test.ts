import {mkdtempSync,writeFileSync,chmodSync,rmSync,realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {describe,it,expect,vi} from 'vitest';
import {admitPiWorkspace,requirePiWorkspaceProtocol} from '../src/pi-workspace-admission.js';

const input={executable:'/sealed/scl',harnessRoot:'/authority/.harness',kernelRoot:'/state/scl',
  workItemId:'wi_20990101_271',agentId:'pulse',sessionRef:`session_v1_${'a'.repeat(64)}`,
  epoch:1,workspace:'/managed/workspaces/org/repo/wi_20990101_271/pulse'};
const admission={schema_version:'scl_workspace_admission/v1',admitted:true,
  policy:'managed_workspaces/v1',scope:'process_start_only',execution_authority:false,
  work_item_id:input.workItemId,agent_id:input.agentId,session_ref:input.sessionRef,
  epoch:input.epoch,workspace:input.workspace,registration_sha256:'b'.repeat(64)};
describe('exact managed Pi workspace admission',()=>{
  it.each([undefined,'old/v0','managed_workspaces/v1'])('preflights installed protocol %s',policy=>{
    const run=vi.fn(()=>JSON.stringify({schema_version:'scl_response/v1',status:'read_only',
      result:{workspace_policy:policy}}));
    if(policy==='managed_workspaces/v1')expect(()=>requirePiWorkspaceProtocol(input.executable,run)).not.toThrow();
    else expect(()=>requirePiWorkspaceProtocol(input.executable,run)).toThrow('managed_workspaces/v1');
    expect(run.mock.calls[0]![1]).toEqual(['handshake']);
  });
  it('uses the sealed read-only client and checks the exact response',()=>{
    const run=vi.fn(()=>JSON.stringify({schema_version:'scl_response/v1',status:'read_only',
      result:{workspace_admission:admission}}));
    expect(admitPiWorkspace(input,run)).toBe(input.workspace);
    expect(run.mock.calls[0]![1]).toContain('admit-runtime-workspace-local');
    expect(run.mock.calls[0]![1]).toContain(input.sessionRef);
  });
  it.each(['workspace','session_ref','agent_id','work_item_id','epoch','admitted'])(
    'rejects mismatched %s',field=>{
      const run=()=>JSON.stringify({schema_version:'scl_response/v1',status:'read_only',
        result:{workspace_admission:{...admission,[field]:'wrong'}}});
      expect(()=>admitPiWorkspace(input,run)).toThrow();
    });
  it('does not turn client failure into root allowlisting',()=>{
    expect(()=>admitPiWorkspace(input,()=>{throw new Error('unavailable');})).toThrow();
  });
});

it('uses only a canonical resolution bound to the requested legacy surface',()=>{
  const resolved={...admission,requested_workspace:'/shared/surface'};
  const run=()=>JSON.stringify({schema_version:'scl_response/v1',status:'read_only',
    result:{workspace_admission:resolved}});
  expect(admitPiWorkspace({...input,workspace:'/shared/surface'},run)).toBe(input.workspace);
  expect(()=>admitPiWorkspace({...input,workspace:'/other/surface'},run)).toThrow();
});

it('preserves the host SSH command without forwarding unrelated environment',()=>{
  vi.stubEnv('GIT_SSH_COMMAND','/usr/bin/ssh -F /host/ssh.conf');
  vi.stubEnv('UNRELATED_SECRET','must-not-forward');
  try {
    const run=vi.fn(()=>JSON.stringify({schema_version:'scl_response/v1',status:'read_only',
      result:{workspace_admission:admission}}));
    admitPiWorkspace(input,run);
    const env=run.mock.calls[0]![2]['env'] as Record<string,string>;
    expect(env['GIT_SSH_COMMAND']).toBe('/usr/bin/ssh -F /host/ssh.conf');
    expect(env['UNRELATED_SECRET']).toBeUndefined();
  } finally {vi.unstubAllEnvs();}
});

it('reports the canonical rejection instead of hiding child stdout',()=>{
  const error=Object.assign(new Error('Command failed'),{stdout:JSON.stringify({
    schema_version:'scl_response/v1',status:'rejected',
    error:{code:'workspace_admission_denied',reason:'runtime identity does not match the current frontier'},
  })});
  expect(()=>admitPiWorkspace(input,()=>{throw error;})).toThrow(
    'SCL workspace admission rejected [workspace_admission_denied]: runtime identity does not match the current frontier');
});


it('passes the explicit Host registration root through an actual admission subprocess',()=>{
  const root=mkdtempSync(join(tmpdir(),'pi-admission-context-'));
  const executable=join(root,'sealed-client');
  const configured=join(root,'host-registration');
  // A real process validates both CLI routing and its environment. The paired
  // installed sequence additionally runs the actual Harness implementation.
  writeFileSync(executable,`#!${process.execPath}
const args=process.argv.slice(2);
if(process.env.HARNESS_WORKTREE_STATE_ROOT!==${JSON.stringify(configured)} || args[args.indexOf('--worktree-state-root')+1]!==${JSON.stringify(configured)})process.exit(23);
process.stdout.write(${JSON.stringify(JSON.stringify({schema_version:'scl_response/v1',status:'read_only',result:{workspace_admission:admission}}))});
`);chmodSync(executable,0o700);
  try {expect(admitPiWorkspace({...input,executable,worktreeStateRoot:configured})).toBe(input.workspace);}
  finally {rmSync(root,{recursive:true,force:true});}
});

it('retains only the Host loader dependency through real handshake and admission subprocesses',()=>{
  const root=mkdtempSync(join(tmpdir(),'pi-admission-loader-'));
  const executable=join(root,'sealed-client');
  const loader=[process.env['LD_LIBRARY_PATH'],join(root,'reviewed-library')].filter(Boolean).join(':');
  writeFileSync(executable,`#!${process.execPath}
if(process.env.LD_LIBRARY_PATH!==${JSON.stringify(loader)})process.exit(23);
for(const key of ['LD_PRELOAD','PYTHONPATH','PYTHONHOME','UNRELATED_SECRET'])if(process.env[key]!==undefined)process.exit(24);
const args=process.argv.slice(2);
const result=args[0]==='handshake'?{workspace_policy:'managed_workspaces/v1'}:
 {workspace_admission:${JSON.stringify(admission)}};
process.stdout.write(JSON.stringify({schema_version:'scl_response/v1',status:'read_only',result}));
`);chmodSync(executable,0o700);
  vi.stubEnv('LD_LIBRARY_PATH',loader);
  vi.stubEnv('LD_PRELOAD','/unreviewed/preload.so');
  vi.stubEnv('PYTHONPATH','/unreviewed/modules');
  vi.stubEnv('PYTHONHOME','/unreviewed/python');
  vi.stubEnv('UNRELATED_SECRET','must-not-forward');
  try {
    expect(()=>requirePiWorkspaceProtocol(executable)).not.toThrow();
    expect(admitPiWorkspace({...input,executable})).toBe(input.workspace);
  } finally {vi.unstubAllEnvs();rmSync(root,{recursive:true,force:true});}
});

it.each([true,false])('checks the v3 surface against canonical admission: match=%s',match=>{
  const run=()=>JSON.stringify({schema_version:'scl_response/v1',status:'read_only',
    result:{workspace_admission:{...admission,surface_id:'surface_v1_existing'}}});
  const call=()=>admitPiWorkspace({...input,executionSurface:match?'surface_v1_existing':'surface_v1_foreign'},run);
  if(match)expect(call()).toBe(input.workspace);else expect(call).toThrow('exact runtime authority');
});


it('passes reviewed storage and caller surface without unrelated Host credentials',()=>{
  const run=vi.fn(()=>JSON.stringify({schema_version:'scl_response/v1',status:'read_only',
    result:{workspace_admission:{...admission,surface_id:'surface_v1_recipient'}}}));
  vi.stubEnv('SCL_HUMAN_DECISION_KEY_FILE','/secret/must-not-forward');
  try {
    admitPiWorkspace({...input,executionSurface:'surface_v1_recipient',pulseHome:'/reviewed/pulse'},run);
    const args=run.mock.calls[0]![1];
    expect(args[args.indexOf('--host-surface-id')+1]).toBe('surface_v1_recipient');
    const env=run.mock.calls[0]![2]['env'] as Record<string,string>;
    expect(env['PULSE_HARNESS_HOME']).toBe('/reviewed/pulse');
    expect(env['SCL_HUMAN_DECISION_KEY_FILE']).toBeUndefined();
    expect(env['SCL_SESSION_REF']).toBe(input.sessionRef);
  } finally {vi.unstubAllEnvs();}
});
