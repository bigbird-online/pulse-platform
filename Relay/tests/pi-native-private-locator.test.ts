import {execFileSync} from 'node:child_process';
import {chmodSync,mkdirSync,mkdtempSync,realpathSync,rmSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {afterEach,expect,it} from 'vitest';
import {PI_RUNTIME_COMMIT,PI_RUNTIME_VERSION} from './pi-runtime-fixture.js';
import {buildPiCommand,validateConfiguredPiProfiles,PI_RUNTIME_MANIFEST_SCHEMA,type PiRuntimeManifest} from '../src/pi-runtime-config.js';
import {buildRuntimeUsageBinding,runtimeAuthorityRef} from '../src/runtime-usage-binding.js';
import type {ConversationSpawnConfig} from '../src/conversation-runtime.js';

const roots:string[]=[];
afterEach(()=>{for(const root of roots.splice(0))rmSync(root,{recursive:true,force:true});});
function fixture(externalLedger=false){
  const root=realpathSync(mkdtempSync(join(tmpdir(),'pi-private-locator-')));roots.push(root);
  const home=join(root,'PULSE'),state=join(home,'data','agent-harness');
  const tools=join(root,'tools'),workspace=join(root,'workspace'),sessions=join(root,'sessions');
  const ledger=externalLedger?join(root,'external-ledger'):join(state,'ledger');
  const kernel=join(state,'kernel'),registry=join(state,'worktrees'),pythonPath=join(tools,'compute-cost');
  for(const path of [workspace,sessions,ledger,kernel,registry,pythonPath])mkdirSync(path,{recursive:true});
  const pi=join(tools,'pi'),scl=join(tools,'scl'),extension=join(tools,'pi-usage-binding.mjs');
  writeFileSync(pi,`#!/bin/sh\nprintf 'pi ${PI_RUNTIME_VERSION}\\n'\n`);chmodSync(pi,0o755);
  writeFileSync(extension,'export default {};\n');
  writeFileSync(scl,`#!${process.execPath}
const a=process.argv.slice(2),v=k=>a[a.indexOf(k)+1];
if(a[0]==='handshake'){console.log(JSON.stringify({schema_version:'scl_response/v1',status:'read_only',result:{workspace_policy:'managed_workspaces/v1'}}));process.exit(0);}
if(a[0]!=='admit-runtime-workspace-local'||process.env.PULSE_HARNESS_HOME!==${JSON.stringify(home)})process.exit(2);
console.log(JSON.stringify({schema_version:'scl_response/v1',status:'read_only',result:{workspace_admission:{schema_version:'scl_workspace_admission/v1',policy:'managed_workspaces/v1',scope:'process_start_only',execution_authority:false,admitted:true,work_item_id:a[1],agent_id:v('--agent'),surface_id:v('--host-surface-id'),session_ref:v('--session-ref'),epoch:Number(v('--epoch')),requested_workspace:v('--workspace'),workspace:v('--workspace'),registration_sha256:'b'.repeat(64)}}}));\n`);chmodSync(scl,0o755);
  const nodeRoots=[tools,ledger,kernel,registry,'/usr/bin','/bin'];
  const manifest:PiRuntimeManifest={schemaVersion:PI_RUNTIME_MANIFEST_SCHEMA,piVersion:PI_RUNTIME_VERSION,piCommit:PI_RUNTIME_COMMIT,profiles:{native:{kind:'host-native',unattended:true,piBinary:pi,toolchainPaths:[tools,'/usr/bin','/bin'],workspaceRoots:[workspace],nodeResourceRoots:nodeRoots,sessionRoot:sessions,envAllowlist:[],extensions:[],skills:[],contextFiles:[],computeCostHost:{extension,python:pi,pythonPath,sclExecutable:scl,sclHarnessRoot:ledger,sclKernelRoot:kernel}}}};
  const config:ConversationSpawnConfig={tenantId:'isolated-native',sessionId:'actual-child-fixture',workspace,allowedTools:['read'],conversationRuntime:{runtime:'pi',model:{provider:'fixture',model:'fixture-model',reasoning:'low'},isolationProfile:'native'},computeCostBinding:buildRuntimeUsageBinding({workItemId:'wi_20990101_123',agentId:'receiver',phase:'work_item',epoch:1,runtime:'pi',procurementSource:'unknown',authorityRef:runtimeAuthorityRef('actual-child-fixture'),executionSurface:'surface_v1_fixture'})};
  const env={PULSE_HARNESS_HOME:home,HARNESS_WORKTREE_STATE_ROOT:registry,PULSE_SCL_SURFACE_ID:'surface_v1_fixture',SCL_HUMAN_DECISION_KEY_FILE:'/unrelated/must-not-forward'};
  return {root,home,state,ledger,kernel,registry,nodeRoots,manifest,config,env};
}
it.each([false,true])('uses a placement locator without granting its parent repository (external ledger=%s)',external=>{
  const f=fixture(external),before=[...f.nodeRoots];
  expect(f.nodeRoots).not.toContain(f.home);
  expect(f.nodeRoots).not.toContain(f.state);
  const spec=buildPiCommand(f.config,f.manifest,f.env);
  expect(spec.env.PULSE_HARNESS_HOME).toBe(f.home);
  expect(spec.env.HARNESS_ROOT).toBe(f.ledger);
  expect(spec.env.SCL_KERNEL_ROOT).toBe(f.kernel);
  expect(spec.env.SCL_HUMAN_DECISION_KEY_FILE).toBeUndefined();
  expect(f.nodeRoots).toEqual(before);
});
it('preflights the configured host Pi against its manifest version rather than a compiled pin',()=>{
  const f=fixture(),pi=f.manifest.profiles.native!.piBinary;
  f.manifest.piVersion='0.88.0';
  writeFileSync(pi,"#!/bin/sh\nprintf 'pi 0.88.0\\n'\n");chmodSync(pi,0o755);
  expect(()=>validateConfiguredPiProfiles(f.manifest,['native'],{requireComputeCostHost:false})).not.toThrow();
  f.manifest.piVersion='0.88.1';
  expect(()=>validateConfiguredPiProfiles(f.manifest,['native'],{requireComputeCostHost:false}))
    .toThrow('Pi host binary version does not match the pin');
});
it('rejects a locator unrelated to the reviewed private state',()=>{
  const f=fixture(),other=join(f.root,'other-home');mkdirSync(other);
  expect(()=>buildPiCommand(f.config,f.manifest,{...f.env,PULSE_HARNESS_HOME:other})).toThrow('does not describe reviewed private SCL state');
});
it('still rejects an actual state directory omitted from node resource roots',()=>{
  const f=fixture(),outside=join(f.state,'unreviewed-kernel');mkdirSync(outside);
  f.manifest.profiles.native!.computeCostHost!.sclKernelRoot=outside;
  expect(()=>buildPiCommand(f.config,f.manifest,f.env)).toThrow('outside the node resource allowlist');
});

it('native startup is independent of an absent or retired legacy metering client',()=>{
  const f=fixture(),profile=f.manifest.profiles.native!;
  const cost=profile.computeCostHost!;
  const entrypoint=join(f.nodeRoots[0]!, 'harness.pyz');writeFileSync(entrypoint,'sealed core fixture');
  profile.workItemCoreHost={extension:cost.extension,python:cost.python,entrypoint};
  writeFileSync(cost.sclExecutable,'#!/bin/sh\nexit 64\n');
  expect(()=>validateConfiguredPiProfiles(f.manifest,['native'],{requireComputeCostHost:true})).not.toThrow();
  delete profile.computeCostHost;
  expect(()=>validateConfiguredPiProfiles(f.manifest,['native'],{requireComputeCostHost:true})).not.toThrow();
  profile.workItemCoreHost.entrypoint=join(f.root,'missing-core.pyz');
  expect(()=>validateConfiguredPiProfiles(f.manifest,['native'],{requireComputeCostHost:true})).toThrow();
});
