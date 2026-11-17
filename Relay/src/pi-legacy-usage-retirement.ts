import {createHash} from 'node:crypto';
import {lstat,readFile,mkdir,open,realpath} from 'node:fs/promises';
import {join,relative,isAbsolute} from 'node:path';
import {validateRuntimeUsageBinding,RUNTIME_USAGE_BINDING_SCHEMA_V2,type RuntimeUsageBinding} from './runtime-usage-binding.js';
import {RuntimeUsageSealError,type RuntimeUsageSealReadback} from './runtime-usage-seal.js';

/** Preserve an unmeasured legacy interval; this is never a bounded cost seal. */
async function captureLegacyPiUsage(input:{binding:RuntimeUsageBinding;sessionId:string;sessionDir:string;nativeState:Record<string,unknown>;outputDir:string}):Promise<RuntimeUsageSealReadback>{
 const binding=validateRuntimeUsageBinding(input.binding);
 const authority='session_v1_'+createHash('sha256').update('pulse-session-v1\0'+input.sessionId).digest('hex');
 if(binding.schema!==RUNTIME_USAGE_BINDING_SCHEMA_V2||binding.runtime!=='pi'||binding.authority_ref!==authority||input.nativeState.sessionId!==input.sessionId){
  throw new RuntimeUsageSealError('authority_mismatch','Legacy source retirement requires exact v2 native authority');
 }
 const source=input.nativeState.sessionFile;
 if(typeof source!=='string'||!isAbsolute(source))throw new RuntimeUsageSealError('artifact_missing','Legacy native history is unavailable');
 const info=await lstat(source);const root=await realpath(input.sessionDir);const resolved=await realpath(source);const rel=relative(root,resolved);
 if(!info.isFile()||info.isSymbolicLink()||rel==='..'||rel.startsWith('../')||isAbsolute(rel))throw new RuntimeUsageSealError('authority_mismatch','Legacy native history is outside the source session directory');
 const bytes=await readFile(source);let header:Record<string,unknown>;
 try{header=JSON.parse(bytes.toString('utf8').split('\n')[0]!);}catch{throw new RuntimeUsageSealError('receipt_invalid','Legacy native history header is invalid');}
 if(header.type!=='session'||header.id!==input.sessionId)throw new RuntimeUsageSealError('authority_mismatch','Legacy native history identity differs from the source');
 const sourceDigest=createHash('sha256').update(bytes).digest('hex');
 const artifact={schema:'pulse_legacy_pi_retirement/v1',status:'legacy_unmeasured',work_item_id:binding.work_item_id,agent_id:binding.agent_id,epoch:binding.epoch,binding_sha256:binding.binding_sha256,authority_ref:binding.authority_ref,source_sha256:sourceDigest,source_bytes:bytes.length,coverage:'unavailable',reason:'legacy_binding_without_bounded_baseline'};
 const content=JSON.stringify(artifact)+'\n';const digest=createHash('sha256').update(content).digest('hex');
 await mkdir(input.outputDir,{recursive:true,mode:0o700});
 const target=join(input.outputDir,`legacy-retirement-${binding.binding_sha256}-${sourceDigest}.json`);
 try{const fd=await open(target,'wx',0o600);try{await fd.writeFile(content);await fd.sync();}finally{await fd.close();}}
 catch(error){if((error as NodeJS.ErrnoException).code!=='EEXIST')throw error;}
 const directory=await open(input.outputDir,'r');try{await directory.sync();}finally{await directory.close();}
 const stored=await lstat(target);
 if(!stored.isFile()||stored.isSymbolicLink()||(stored.mode&0o777)!==0o600||await readFile(target,'utf8')!==content)throw new RuntimeUsageSealError('seal_conflict','Legacy retirement artifact differs from the exact source history');
 // Source must remain byte-identical through durable capture; no missing usage becomes zero.
 if(createHash('sha256').update(await readFile(source)).digest('hex')!==sourceDigest)throw new RuntimeUsageSealError('not_settled','Legacy source history changed during retirement',true);
 return {schema:'pulse_runtime_usage_seal_readback/v1',status:'legacy_unmeasured',work_item_id:binding.work_item_id,epoch:binding.epoch,artifact_ref:target,sealed_digest:digest};
}

export async function retireLegacyPiUsage(input:Parameters<typeof captureLegacyPiUsage>[0]):Promise<RuntimeUsageSealReadback>{
 try{return await captureLegacyPiUsage(input);}catch(error){
  if(error instanceof RuntimeUsageSealError)throw error;
  const code=(error as NodeJS.ErrnoException).code;
  if(code==='ENOENT')throw new RuntimeUsageSealError('artifact_missing','Legacy native history or retirement directory is unavailable');
  if(code==='ENOSPC'||code==='EIO')throw new RuntimeUsageSealError('local_persistence_unavailable','Legacy retirement could not be persisted',true);
  throw new RuntimeUsageSealError('receipt_invalid','Legacy retirement evidence could not be verified');
 }
}
