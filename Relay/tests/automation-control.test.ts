import { describe,it,expect,vi } from 'vitest';
import { mkdtempSync,writeFileSync,chmodSync,rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { automationDecision,AutomaticExecutionPaused } from '../src/automation-control.js';
import { createBatchRuntimeOrchestrator } from '../src/runtime.js';

describe('persistent automatic AI permission',()=>{
 it('denies missing, unregistered, damaged and paused while manual remains explicit',()=>{
  const d=mkdtempSync(join(tmpdir(),'auto-control-')); const p=join(d,'automation-control.json');
  try {
   expect(automationDecision('resource:hestia','hestia',false,p).allowed).toBe(false);
   const data={schema:'pulse_automation_control/v1',revision:1,updated_at:new Date().toISOString(),
    reason:'human pause',decision_ref:'WI219',automatic_enabled:true,
    entries:{'resource:hestia':{owner:'hestia',enabled:true}}};
   writeFileSync(p,JSON.stringify(data),{mode:0o600});
   expect(automationDecision('resource:hestia','hestia',false,p).allowed).toBe(true);
   expect(automationDecision('resource:unknown','unknown',false,p).allowed).toBe(false);
   expect(automationDecision('resource:hestia','pulse',false,p).allowed).toBe(false);
   data.automatic_enabled=false; writeFileSync(p,JSON.stringify(data));
   expect(automationDecision('resource:hestia','hestia',false,p).allowed).toBe(false);
   expect(automationDecision('resource:hestia','hestia',true,p).status).toBe('manual');
   writeFileSync(p,'{}'); expect(automationDecision('resource:hestia','hestia',false,p).allowed).toBe(false);
  } finally {rmSync(d,{recursive:true});}
 });
 it('rechecks before fallback: pause never launches a second provider',async()=>{
  let allowed=true;
  const first=vi.fn(async()=>{allowed=false;return {runtime:'codex' as const,stdout:'',stderr:'429',exitCode:1,durationMs:0,timedOut:false};});
  const second=vi.fn();
  const o=createBatchRuntimeOrchestrator({primary:'codex',fallbacks:['cc'],adapters:[
    {id:'codex',run:first,classifyFailure:()=>({reason:'rate-limit',message:'429'})},
    {id:'cc',run:second,classifyFailure:()=>null}]});
  await expect(o.run({workspace:'/tmp',prompt:'test',timeoutMs:1,
    beforeAttempt:()=>{if(!allowed)throw new AutomaticExecutionPaused();}})).rejects.toBeInstanceOf(AutomaticExecutionPaused);
  expect(first).toHaveBeenCalledTimes(1);expect(second).not.toHaveBeenCalled();
 });
});
