import {isAbsolute} from 'node:path';
import {execFileSync} from 'node:child_process';

interface AdmissionInput {
  executable:string;harnessRoot:string;kernelRoot:string;workItemId:string;
  agentId:string;sessionRef:string;epoch?:number;workspace:string;worktreeStateRoot?:string;executionSurface?:string;pulseHome?:string;
}
type Runner=(file:string,args:string[],options:Record<string,unknown>)=>string;

function hostClientEnvironment():NodeJS.ProcessEnv {
  // Some reviewed Python installations need their Host's shared-library path
  // even for handshake. Preserve that loader dependency, not the Host's full
  // environment (in particular preload hooks, Python imports or credentials).
  return {PATH:process.env['PATH'],
    ...(process.env['LD_LIBRARY_PATH']?{LD_LIBRARY_PATH:process.env['LD_LIBRARY_PATH']}:{})};
}

/** Reject incompatible runtime adoption before any durable WI entry. */
export function requirePiWorkspaceProtocol(executable:string,run:Runner=(file,args,options)=>
  execFileSync(file,args,{...options,encoding:'utf8'})):void {
  const result=JSON.parse(run(executable,['handshake'],{
    timeout:15000,maxBuffer:1024*1024,env:hostClientEnvironment(),
  }));
  if(result?.schema_version!=='scl_response/v1'||result.status!=='read_only'||result.error
    ||result?.result?.workspace_policy!=='managed_workspaces/v1'){
    throw new Error('Canonical SCL runtime does not support managed_workspaces/v1');
  }
}

/** A registered workspace is an exact capability, never a new allowlisted parent. */
export function admitPiWorkspace(input:AdmissionInput,run:Runner=(file,args,options)=>
  execFileSync(file,args,{...options,encoding:'utf8'})):string {
  let raw:string;
  try { raw=run(input.executable,[
    'admit-runtime-workspace-local',input.workItemId,
    '--harness-root',input.harnessRoot,'--kernel-root',input.kernelRoot,
    '--agent',input.agentId,'--epoch',String(input.epoch),
    '--session-ref',input.sessionRef,'--workspace',input.workspace,
    ...(input.worktreeStateRoot?['--worktree-state-root',input.worktreeStateRoot]:[]),
    ...(input.executionSurface?['--host-surface-id',input.executionSurface]:[]),
  ],{timeout:15000,maxBuffer:1024*1024,env:{...hostClientEnvironment(),
    GIT_SSH_COMMAND:process.env['GIT_SSH_COMMAND'],SCL_SESSION_REF:input.sessionRef,
    ...(input.worktreeStateRoot?{HARNESS_WORKTREE_STATE_ROOT:input.worktreeStateRoot}:{}),
    ...(input.pulseHome?{PULSE_HARNESS_HOME:input.pulseHome}:{})}});
  } catch(error) {
    let rejection;
    try {
      const stdout=(error as {stdout?:unknown})?.stdout;
      if(typeof stdout==='string'||Buffer.isBuffer(stdout))rejection=JSON.parse(String(stdout));
    } catch { /* Preserve the original error if no canonical response was returned. */ }
    if(rejection?.schema_version==='scl_response/v1'&&rejection.status==='rejected'
      &&typeof rejection.error?.code==='string'&&typeof rejection.error?.reason==='string'){
      throw new Error(`SCL workspace admission rejected [${rejection.error.code}]: ${rejection.error.reason}`);
    }
    throw error;
  }
  const result=JSON.parse(raw);
  const proof=result?.result?.workspace_admission;
  if(result?.schema_version!=='scl_response/v1'||result.status!=='read_only'||result.error
    ||proof?.schema_version!=='scl_workspace_admission/v1'||proof.admitted!==true
    ||proof.policy!=='managed_workspaces/v1'||proof.scope!=='process_start_only'
    ||proof.execution_authority!==false
    ||proof.work_item_id!==input.workItemId||proof.agent_id!==input.agentId
    ||proof.session_ref!==input.sessionRef||proof.epoch!==input.epoch
    ||(input.executionSurface!==undefined&&proof.surface_id!==input.executionSurface)
    ||typeof proof.workspace!=='string'||!isAbsolute(proof.workspace)
    ||(proof.workspace!==input.workspace&&proof.requested_workspace!==input.workspace)||! /^[a-f0-9]{64}$/.test(proof.registration_sha256??'')){
    throw new Error('Pi managed workspace admission did not prove the exact runtime authority');
  }
  return proof.workspace;
}
