import { afterEach, describe, expect, it } from 'vitest';
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createCollaborationCapture } from '../src/collaboration-capture.js';
import type { SpawnConfig } from '../src/types.js';

const roots: string[] = [];
function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'cc-receipts-')); roots.push(root);
  let sequence=0;
  const capture=createCollaborationCapture(root, {now:()=> '2026-09-13T01:00:00Z',nonce:()=>String(sequence++)})!;
  const rows=()=>readdirSync(root).filter(p=>p.endsWith('.jsonl')).flatMap(p=>readFileSync(join(root,p),'utf8').trim().split('\n').map(s=>JSON.parse(s)));
  const config={tenantId:'private-tenant',workspace:'/private/workspace',sessionId:'private-session',
    computeCostBinding:{work_item_id:'wi_20260913_101',agent_id:'pulse',binding_sha256:'private-binding'}} as SpawnConfig;
  return {root,capture,rows,config};
}
afterEach(()=>{for(const root of roots.splice(0))rmSync(root,{recursive:true,force:true});});

describe('collaboration Host capture',()=>{
  it('records failed entry separately from a model dispatch without inventing usage',()=>{
    const f=fixture(); const entry=f.capture.begin('codex',f.config,'spawn',undefined,false);
    f.capture.finish(entry,'failed');
    expect(f.rows().map(r=>[r.kind,r.state,r.expected_usage])).toEqual([['execution','accepted',false],['execution','failed',false]]);
  });
  it('freezes assignment per attempt and preserves retry parent identity',()=>{
    const f=fixture(); const a=f.capture.begin('cc',f.config,'private-turn');
    f.config.computeCostBinding!.work_item_id='wi_20260913_102';
    f.capture.finish(a,'failed'); const b=f.capture.begin('cc',f.config,'private-turn',a.ref);
    f.capture.finish(b,'completed');
    const rows=f.rows();expect(rows[0].wi).toBe('wi_20260913_101');expect(rows[1].wi).toBe(rows[0].wi);
    expect(rows[2].wi).toBe('wi_20260913_102');expect(rows[2].attempt_ref).not.toBe(rows[0].attempt_ref);
    expect(rows[3].parent_ref).toBe(a.ref);
  });
  it('records missing usage as missing and explicit zeros as zeros',()=>{
    const f=fixture(); const a=f.capture.begin('codex',f.config,'one');
    f.capture.response(a,{type:'result'});
    const b=f.capture.begin('codex',f.config,'two');
    f.capture.response(b,{type:'result',usage:{input_tokens:0,cached_input_tokens:0,output_tokens:0}});
    const usage=f.rows().filter(r=>r.kind==='usage');
    expect(usage[0].usage).toEqual({});expect(usage[1].usage.input_tokens).toBe(0);
    expect(usage.every(r=>r.model===null)).toBe(true);
  });
  it('names a Codex fallback result by its exec model, never a guessed default',()=>{
    const f=fixture(); const a=f.capture.begin('codex',f.config,'one');
    f.capture.response(a,{type:'result',requested_model:'gpt-launched',usage:{input_tokens:5,cached_input_tokens:1,output_tokens:2}});
    const cc=f.capture.begin('cc',f.config,'two');
    f.capture.response(cc,{type:'result',requested_model:'gpt-launched',usage:{input_tokens:5,output_tokens:2}});
    const usage=f.rows().filter(r=>r.kind==='usage');
    expect(usage.map(r=>[r.runtime,r.model,r.mapping])).toEqual([['codex','gpt-launched','codex_cli.v1'],['cc',null,'cc_result.v1']]);
    expect(usage[0].coverage_ref).not.toBe(usage[1].coverage_ref);
  });
  it('records a Codex turn as its thread total minus the previous turn total',()=>{
    const f=fixture(); const total={input_tokens:117302,cached_input_tokens:106496,output_tokens:475};
    const zero={input_tokens:0,cached_input_tokens:0,cache_write_input_tokens:0,output_tokens:0};
    const cases=[{thread_usage_baseline:zero,usage:{input_tokens:16114,cached_input_tokens:12160,output_tokens:13}},
      {thread_usage_baseline:{input_tokens:16114,cached_input_tokens:12160,output_tokens:13},usage:total},
      {usage:total},
      {thread_usage_baseline:{input_tokens:200000,cached_input_tokens:0,output_tokens:0},usage:total}];
    cases.forEach((c,i)=>f.capture.response(f.capture.begin('codex',f.config,`turn-${i}`),{type:'result',...c}));
    expect(f.rows().filter(r=>r.kind==='usage').map(r=>r.usage)).toEqual([
      {...zero,input_tokens:16114,cached_input_tokens:12160,output_tokens:13},
      {input_tokens:101188,cached_input_tokens:94336,cache_write_input_tokens:0,output_tokens:462},
      {input_tokens:null,cached_input_tokens:null,output_tokens:null},
      {input_tokens:null,cached_input_tokens:null,output_tokens:null}]);
  });
  it('uses each SDK model partition and does not publish messages or raw identities',()=>{
    const f=fixture();const a=f.capture.begin('cc-sdk',f.config,'private-correlation');
    f.capture.response(a,{type:'assistant',message:{id:'private-message',model:'model-a',stop_reason:'tool_use',
      content:[{type:'text',text:'PRIVATE-CONTENT'}],usage:{input_tokens:2,output_tokens:1,cache_read_input_tokens:0,cache_creation_input_tokens:0}}});
    f.capture.response(a,{type:'result',modelUsage:{'model-a':{inputTokens:2,outputTokens:1,cacheReadInputTokens:0,cacheCreationInputTokens:0},
      'model-b':{inputTokens:3,outputTokens:1,cacheReadInputTokens:0,cacheCreationInputTokens:0}}});
    const rows=f.rows(), aggregates=rows.filter(r=>r.granularity==='turn_aggregate');
    expect(aggregates.map(r=>r.model)).toEqual(['model-a','model-b']);
    expect(aggregates[0].covers).toHaveLength(1);expect(aggregates[1].covers).toHaveLength(0);
    expect(rows.every(r=>r.runtime==='cc_sdk')).toBe(true);
    const raw=JSON.stringify(rows);for(const s of ['private-tenant','private-session','private-message','PRIVATE-CONTENT','private-correlation','private-binding'])expect(raw).not.toContain(s);
    for(const name of readdirSync(f.root))expect(statSync(join(f.root,name)).mode&0o077).toBe(0);
  });
  it('keeps one final result when a resumed session first answers a turn without a request',()=>{
    const f=fixture();const a=f.capture.begin('cc',f.config,'private-turn');
    const zero={input_tokens:0,output_tokens:0,cache_read_input_tokens:0,cache_creation_input_tokens:0};
    const before={inputTokens:10,outputTokens:5,cacheReadInputTokens:100,cacheCreationInputTokens:1};
    f.capture.response(a,{type:'result',session_id:'private-session',usage:zero,modelUsage:{'model-a':before,'model-b':before}});
    expect(f.rows().filter(r=>r.kind==='usage')).toEqual([]);
    // That turn made no provider request: its settlement owes no usage.
    expect(f.rows().filter(r=>r.kind==='execution').map(r=>[r.state,r.expected_usage])).toEqual([['accepted',true],['completed',false]]);
    f.capture.response(a,{type:'assistant',session_id:'private-session',message:{id:'m1',model:'model-a',stop_reason:'end_turn',
      usage:{input_tokens:2,output_tokens:3,cache_read_input_tokens:50,cache_creation_input_tokens:0}}});
    f.capture.response(a,{type:'result',session_id:'private-session',
      usage:{input_tokens:2,output_tokens:3,cache_read_input_tokens:50,cache_creation_input_tokens:0},
      modelUsage:{'model-a':{inputTokens:12,outputTokens:8,cacheReadInputTokens:150,cacheCreationInputTokens:1},'model-b':before}});
    const results=f.rows().filter(r=>r.granularity==='turn_aggregate');
    expect(results.map(r=>[r.model,r.covers.length,r.usage.outputTokens])).toEqual([['model-a',1,8],['model-b',0,5]]);
    expect(new Set(results.map(r=>r.usage_ref)).size).toBe(2);
  });
  it('still records a result that reports its own usage or follows observed messages',()=>{
    const f=fixture();const counters={inputTokens:1,outputTokens:1,cacheReadInputTokens:0,cacheCreationInputTokens:0};
    const a=f.capture.begin('cc-sdk',f.config,'one');
    f.capture.response(a,{type:'result',usage:{input_tokens:1,output_tokens:1,cache_read_input_tokens:0,cache_creation_input_tokens:0},modelUsage:{'model-a':counters}});
    const b=f.capture.begin('cc-sdk',f.config,'two');
    f.capture.response(b,{type:'result',modelUsage:{'model-a':counters}});
    const c=f.capture.begin('cc',f.config,'three');
    f.capture.response(c,{type:'usage_observation',message:{id:'m2',model:'model-a',usage:{input_tokens:0,output_tokens:0}}});
    f.capture.response(c,{type:'result',usage:{input_tokens:0,output_tokens:0,cache_read_input_tokens:0,cache_creation_input_tokens:0},modelUsage:{'model-a':counters}});
    expect(f.rows().filter(r=>r.granularity==='turn_aggregate').map(r=>r.attempt_ref)).toEqual([a.ref,b.ref,c.ref]);
  });
  it('does not throw into business flow when the destination cannot be written',()=>{
    const f=fixture(), bad=join(f.root,'not-directory');writeFileSync(bad,'x');
    let errors=0;const c=createCollaborationCapture(bad,{onError:()=>{errors++;throw new Error('callback failure');}})!;
    expect(()=>{const a=c.begin('cc',f.config,'x');c.finish(a,'failed');c.human('input',Number.NaN);}).not.toThrow();
    expect(errors).toBeGreaterThan(0);
  });
  it('does not count interactive final-message summary a second time',()=>{
    const f=fixture(),a=f.capture.begin('cc',f.config,'turn');
    f.capture.response(a,{type:'result',usage_scope:'final_message',usage:{input_tokens:100,output_tokens:10}});
    expect(f.rows().filter(r=>r.kind==='usage')).toHaveLength(0);
    expect(f.rows().at(-1).state).toBe('completed');
  });
  it('deduplicates real print telemetry plus assistant observation with an immutable clock identity',()=>{
    const root=mkdtempSync(join(tmpdir(),'cc-receipts-')); roots.push(root);
    let tick=0;
    const capture=createCollaborationCapture(root,{now:()=>`2026-09-13T01:00:0${tick++}.000Z`,nonce:()=>`n${tick}`})!;
    const config={tenantId:'private',workspace:'/private',sessionId:'native-session'} as SpawnConfig;
    const attempt=capture.begin('cc',config,'turn');
    const native={id:'m1',model:'m',usage:{input_tokens:2,output_tokens:1}};
    // pool print mode emits this exact sequence, with a real advancing clock.
    capture.response(attempt,{type:'usage_observation',message:native});
    capture.response(attempt,{type:'assistant',message:native});
    const usages=readdirSync(root).flatMap(p=>readFileSync(join(root,p),'utf8').trim().split('\n').map(s=>JSON.parse(s))).filter(r=>r.kind==='usage');
    expect(usages).toHaveLength(1);
    expect(usages[0].native_scope_ref).toBeTruthy();
  });
  it('keeps CC partial/final content versions distinct while reusing exact duplicates',()=>{
    const f=fixture(),a=f.capture.begin('cc',f.config,'turn');
    const partial={id:'m1',model:'m',usage:{input_tokens:2}};
    const final={id:'m1',model:'m',usage:{input_tokens:2,output_tokens:1},stop_reason:'end_turn'};
    f.capture.response(a,{type:'usage_observation',message:partial});
    f.capture.response(a,{type:'usage_observation',message:final});
    f.capture.response(a,{type:'usage_observation',message:final});
    const usages=f.rows().filter(r=>r.kind==='usage');
    expect(usages).toHaveLength(2);
    expect(new Set(usages.map(r=>r.event_ref)).size).toBe(2);
    expect(usages.map(r=>r.usage_ref)).toEqual([usages[0].usage_ref,usages[0].usage_ref]);
  });
  it('reads an old accepted dispatch after restart and durably marks recovery_unknown',()=>{
    const root=mkdtempSync(join(tmpdir(),'cc-receipts-')); roots.push(root);
    const config={tenantId:'private',workspace:'/private',sessionId:'native-session'} as SpawnConfig;
    createCollaborationCapture(root,{nonce:()=> 'first'})!.begin('codex',config,'turn:entry:operation_v1_test');
    const restarted=createCollaborationCapture(root,{nonce:()=> 'second'})!;
    expect(restarted.dispatchDisposition?.('codex',config,'turn:entry:operation_v1_test')).toBe('accepted');
    restarted.markRecoveryUnknown?.('codex',config,'turn:entry:operation_v1_test');
    expect(restarted.dispatchDisposition?.('codex',config,'turn:entry:operation_v1_test')).toBe('recovery_unknown');
    const rows=readdirSync(root).flatMap(p=>readFileSync(join(root,p),'utf8').trim().split('\n').map(s=>JSON.parse(s)));
    expect(rows).toContainEqual(expect.objectContaining({kind:'gap',disposition:'recovery_unknown'}));
    expect(createCollaborationCapture(root,{nonce:()=> 'third'})!
      .dispatchDisposition?.('codex',config,'turn:entry:operation_v1_test')).toBe('recovery_unknown');
  });
  it('reuses the result version after cancellation',()=>{
    const f=fixture(),a=f.capture.begin('cc',f.config,'turn');
    f.capture.finish(a,'cancelled');
    const result={type:'result' as const,usage:{input_tokens:3,output_tokens:2}};
    f.capture.response(a,result); f.capture.response(a,result);
    expect(f.rows().filter(r=>r.kind==='usage')).toHaveLength(1);
  });
  it('retains provider settlement after cancellation without reopening execution',()=>{
    const f=fixture(),a=f.capture.begin('cc',f.config,'turn');
    f.capture.finish(a,'cancelled');
    f.capture.response(a,{type:'assistant',message:{id:'settled',model:'m',usage:{input_tokens:100,output_tokens:10}}});
    expect(f.rows().filter(r=>r.kind==='usage')).toHaveLength(1);
    expect(f.rows().filter(r=>r.kind==='execution').map(r=>r.state)).toEqual(['accepted','cancelled']);
  });
});

it('links human receipt once to each frozen assignment without copying private identity',()=>{
  const f=fixture();
  const proof={ref:'a'.repeat(64),observedAtMs:Date.parse('2026-09-13T00:59:59Z')};
  const first=f.capture.begin('cc',f.config,'first');
  f.capture.human(proof.ref,proof.observedAtMs);
  f.capture.linkHuman(first,proof);
  f.config.computeCostBinding!.work_item_id='wi_20260913_102';
  const second=f.capture.begin('cc',f.config,'second');
  f.capture.linkHuman(second,{...proof,ref:'b'.repeat(64)});
  f.capture.linkHuman(second,undefined);
  const attrs=f.rows().filter(r=>r.kind==='attribution');
  expect(attrs.map(r=>r.wi)).toEqual(['wi_20260913_101','wi_20260913_102']);
  expect(attrs[0].target_ref).not.toBe(attrs[1].target_ref);
  expect(JSON.stringify(attrs)).not.toContain(proof.ref);
  expect(attrs.every(r=>r.responsible===null)).toBe(true);
});

it('takes native init identity before the first request and refuses a mid-attempt identity change',()=>{
  const f=fixture(),a=f.capture.begin('cc',f.config,'fresh');
  const native={id:'message',model:'m',stop_reason:'end_turn',usage:{input_tokens:1,output_tokens:1,cache_read_input_tokens:0,cache_creation_input_tokens:0}};
  f.capture.response(a,{type:'system',subtype:'init',session_id:'actual-native-session'});
  f.capture.response(a,{type:'assistant',message:native});
  f.capture.response(a,{type:'assistant',session_id:'wrong-session',message:{...native,id:'other'}});
  expect(a.nativeSession).toBe('actual-native-session');
  expect(f.rows().filter(r=>r.kind==='usage')).toHaveLength(1);
  expect(f.rows().filter(r=>r.kind==='gap')).toHaveLength(1);
});


it('captures authenticated dispatch input independently of WI and SCL assignment',()=>{
  for(const wi of [undefined,'wi_20260913_101']) {
    const f=fixture();delete f.config.computeCostBinding;
    f.config.meteringAgentId='pulse';f.config.meteringWorkItemId=wi;
    const a=f.capture.begin('pi',f.config,'ungranted');
    const proof={ref:'a'.repeat(64),observedAtMs:Date.parse('2026-09-13T00:59:59Z')};
    f.capture.linkHuman(a,proof);
    const linked=f.rows().filter(r=>r.human_ref);
    expect(linked).toHaveLength(1);
    expect(linked[0]).toMatchObject({kind:'execution',state:'accepted',executor:'pulse',
      wi:wi??null,assignment_ref:null,attempt_ref:a.ref});
    expect(f.rows().filter(r=>r.kind==='attribution'||r.kind==='usage')).toHaveLength(0);
    expect(JSON.stringify(linked)).not.toContain(proof.ref);
    f.capture.finish(a,'cancelled');const before=f.rows();
    f.capture.linkHuman(a,proof);expect(f.rows()).toEqual(before);
  }
});
