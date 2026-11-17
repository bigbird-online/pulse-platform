import {mkdtempSync,writeFileSync,rmSync,symlinkSync,readFileSync,chmodSync} from 'node:fs';
import {join} from 'node:path';import {tmpdir} from 'node:os';
import {afterEach,it,expect} from 'vitest';
import {retireLegacyPiUsage} from '../src/pi-legacy-usage-retirement.js';
import {buildRuntimeUsageBinding,runtimeAuthorityRef} from '../src/runtime-usage-binding.js';
const roots:string[]=[];afterEach(()=>{for(const r of roots.splice(0))rmSync(r,{recursive:true,force:true});});
function fixture(){const root=mkdtempSync(join(tmpdir(),'legacy-retirement-'));roots.push(root);const sessionId='c3e2f842-5517-4196-a663-bb9bf2b49028';const file=join(root,'history.jsonl');writeFileSync(file,JSON.stringify({type:'session',id:sessionId})+'\n');return {sessionId,sessionDir:root,nativeState:{sessionId,sessionFile:file},outputDir:join(root,'compute-cost'),binding:buildRuntimeUsageBinding({workItemId:'wi_20260101_123',agentId:'aki',epoch:6,phase:'work_item',runtime:'pi',procurementSource:'unknown',authorityRef:runtimeAuthorityRef(sessionId)})};}
it('retains immutable unmeasured receipts across retry and resumed legacy history',async()=>{const f=fixture();const a=await retireLegacyPiUsage(f);expect(await retireLegacyPiUsage(f)).toEqual(a);const old=readFileSync(a.artifact_ref,'utf8');writeFileSync(f.nativeState.sessionFile,readFileSync(f.nativeState.sessionFile,'utf8')+JSON.stringify({type:'message',message:{role:'assistant'}})+'\n');const b=await retireLegacyPiUsage(f);expect(b.artifact_ref).not.toBe(a.artifact_ref);expect(readFileSync(a.artifact_ref,'utf8')).toBe(old);});
it.each(['native','header','binding','symlink','mode','tamper'])('rejects %s evidence drift',async(kind)=>{const f=fixture();if(kind==='native')f.nativeState.sessionId='other';if(kind==='header')writeFileSync(f.nativeState.sessionFile,JSON.stringify({type:'session',id:'other'})+'\n');if(kind==='binding')f.binding=buildRuntimeUsageBinding({...{workItemId:'wi_20260101_123',agentId:'aki',epoch:6,phase:'work_item',runtime:'pi' as const,procurementSource:'unknown' as const},authorityRef:runtimeAuthorityRef('other')});if(kind==='symlink'){const alias=join(f.sessionDir,'alias.jsonl');symlinkSync(f.nativeState.sessionFile,alias);f.nativeState.sessionFile=alias;}if(kind==='mode'||kind==='tamper'){const a=await retireLegacyPiUsage(f);if(kind==='mode')chmodSync(a.artifact_ref,0o644);else writeFileSync(a.artifact_ref,'{}');}await expect(retireLegacyPiUsage(f)).rejects.toThrow();});

it.each(['v1','v3','future','unbound','digest','missing','corrupt'])('fails closed without retrying incompatible %s evidence',async(kind)=>{
  const f=fixture();
  const base={workItemId:'wi_20260101_123',agentId:'aki',epoch:6,phase:'work_item',runtime:'pi' as const,procurementSource:'unknown' as const};
  if(kind==='v1')f.binding=buildRuntimeUsageBinding(base);
  if(kind==='v3')f.binding=buildRuntimeUsageBinding({...base,authorityRef:runtimeAuthorityRef(f.sessionId),executionSurface:'surface_v1_local_macos'});
  if(kind==='future')f.binding={...f.binding,schema:'pulse_runtime_usage_binding/v4'} as never;
  if(kind==='unbound')f.binding=undefined as never;
  if(kind==='digest')f.binding={...f.binding,binding_sha256:'0'.repeat(64)};
  if(kind==='missing')rmSync(f.nativeState.sessionFile);
  if(kind==='corrupt')writeFileSync(f.nativeState.sessionFile,'{broken');
  await expect(retireLegacyPiUsage(f)).rejects.toMatchObject({name:'RuntimeUsageSealError',retryable:false});
});
