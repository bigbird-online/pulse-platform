import {afterEach, describe, expect, it} from 'vitest';
import {appendFileSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createCollaborationCapture, validateNativeModelUsage} from '../src/collaboration-capture.js';
import {piNativeModelUsage, piNativeTurnUsage} from '../src/native-model-usage.js';
import {captureCodexSourceBoundary, codexNativeModelUsage} from '../src/codex-native-model-usage.js';
import {readdirSync} from 'node:fs';

const roots: string[]=[];
const temp=()=>{const root=mkdtempSync(join(tmpdir(),'cost-model-')); roots.push(root); return root;};
afterEach(()=>{for(const root of roots.splice(0)) rmSync(root,{recursive:true,force:true});});
const pi=(model='model-a',input=100)=>({model,provider:'openai',usage:{input,output:10,cacheRead:20,cacheWrite:0,totalTokens:input+30}});
const piUsage=(input=100)=>({input_tokens:input,output_tokens:10,cache_read_input_tokens:20,cache_creation_input_tokens:0});
const cu=(input=100,output=10)=>({input_tokens:input,cached_input_tokens:20,output_tokens:output});
const wire=(rows:unknown[])=>rows.map(r=>JSON.stringify(r)+'\n').join('');
const meta=(id='session')=>({type:'session_meta',payload:{id}});
const task=(id='turn',model='model-a',total=cu(),last?:unknown)=>[
  {type:'event_msg',payload:{type:'task_started',turn_id:id}},
  {type:'turn_context',payload:{turn_id:id,model,model_provider:'openai'}},
  {type:'event_msg',payload:{type:'token_count',info:{total_token_usage:total,...(last?{last_token_usage:last}:{})}}},
  {type:'event_msg',payload:{type:'task_complete',turn_id:id}},
];

describe('native per-dispatch quantities without a statistics gate',()=>{
  it('sums distinct completed calls and preserves known zero',()=>{
    expect(piNativeTurnUsage([pi()])).toEqual(piUsage());
    expect(piNativeTurnUsage([{...pi(),timestamp:1},{...pi('b'),timestamp:2}])).toEqual({
      input_tokens:200,output_tokens:20,cache_read_input_tokens:40,cache_creation_input_tokens:0});
    expect(piNativeTurnUsage([{usage:{input:0,output:0,cacheRead:0,cacheWrite:0}}])).toEqual({
      input_tokens:0,output_tokens:0,cache_read_input_tokens:0,cache_creation_input_tokens:0});
    expect(piNativeTurnUsage([{...pi(),model:undefined}])).toEqual(piUsage());
  });
  it('keeps gaps, compaction, ambiguous repeats and unsafe sums unknown',()=>{
    for(const rows of [[],[pi(),null],[{usage:{input:1}}],
      [{...pi(),timestamp:1},{...pi(),timestamp:1}],
      [{usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:3}}],
      [{usage:{input:Number.MAX_SAFE_INTEGER,output:1,cacheRead:0,cacheWrite:0}}],
      Array.from({length:10001},()=>pi())])expect(piNativeTurnUsage(rows)).toBeUndefined();
  });
});

describe('native model partitions at the real Host writer',()=>{
  it('retains actual Pi model and provider only when all classes conserve',()=>{
    expect(piNativeModelUsage([pi()],piUsage())?.models[0]).toMatchObject({model:'model-a',provider:'openai'});
    expect(piNativeModelUsage([pi()],piUsage(101))).toBeUndefined();
  });
  it('splits two observed models rather than using the last model',()=>{
    const proof=piNativeModelUsage([pi('a'),pi('b')],{...piUsage(200),output_tokens:20,cache_read_input_tokens:40});
    expect(proof?.models.map(m=>m.model)).toEqual(['a','b']);
  });
  it('does not invent model/quantity for compaction, missing fields, or duplicates',()=>{
    expect(piNativeModelUsage([pi(),null],piUsage())).toBeUndefined();
    expect(piNativeModelUsage([{...pi(),model:undefined}],piUsage())).toBeUndefined();
    expect(piNativeModelUsage([{...pi(),usage:{input:100,output:10}}],piUsage())).toBeUndefined();
    expect(piNativeModelUsage([pi(),pi()],piUsage())).toBeUndefined();
  });
  it('rejects a foreign runtime partition and invalid cache quantities',()=>{
    const proof=piNativeModelUsage([pi()],piUsage());
    expect(validateNativeModelUsage(proof,cu(),'codex')).toBeUndefined();
    expect(validateNativeModelUsage({schema:'pulse_native_model_usage/v1',runtime:'codex',models:[
      {model:'m',provider:null,usage:{...cu(1),cached_input_tokens:2}}]},cu(1),'codex')).toBeUndefined();
  });
  it.each(['pi','codex'] as const)('never uses configured or result-only model for %s history',runtime=>{
    const root=temp();const capture=createCollaborationCapture(root)!;
    const attempt=capture.begin(runtime,{sessionId:'s',ccModel:'configured-model'} as never,'turn');
    capture.response(attempt,{type:'result',model:'last-model',usage:runtime==='pi'?piUsage():cu()});
    const rows=readFileSync(join(root,readdirSync(root)[0]!), 'utf8').trim().split('\n').map(r=>JSON.parse(r));
    expect(rows.filter(r=>r.kind==='usage').map(r=>r.model)).toEqual([null]);
  });
  it('writes scoped Pi proof once with correct mapping, provider and counts',()=>{
    const root=temp();const capture=createCollaborationCapture(root)!;
    const attempt=capture.begin('pi',{sessionId:'s'} as never,'turn');
    const result={type:'result',usage:piUsage(),native_model_usage:piNativeModelUsage([pi()],piUsage())};
    capture.response(attempt,result);capture.response(attempt,result);
    const rows=readFileSync(join(root,readdirSync(root)[0]!), 'utf8').trim().split('\n').map(r=>JSON.parse(r));
    expect(rows.filter(r=>r.kind==='usage')).toMatchObject([{model:'model-a',provider:'openai',mapping:'relay_pi.v1',usage:piUsage()}]);
  });
});

describe('Codex exact native source boundary, no provider call',()=>{
  it('joins the fresh native thread and one task to the factual exec result',()=>{
    const root=temp();const boundary=captureCodexSourceBoundary(root);
    writeFileSync(join(root,'session.jsonl'),wire([meta(),...task()]));
    expect(codexNativeModelUsage(boundary,'session',cu())?.models[0]).toMatchObject({model:'model-a',provider:'openai'});
  });
  it('prices only appended resumed consumption, not the old model or cumulative total',()=>{
    const root=temp(), path=join(root,'session.jsonl');
    writeFileSync(path,wire([meta(),...task('old','old-model')]));
    const boundary=captureCodexSourceBoundary(root,'session');
    appendFileSync(path,wire(task('new','new-model',cu(150,15))));
    const expected={input_tokens:50,cached_input_tokens:0,output_tokens:5};
    expect(codexNativeModelUsage(boundary,'session',expected)?.models[0].model).toBe('new-model');
    expect(codexNativeModelUsage(boundary,'session',cu(150,15))).toBeUndefined();
  });
  it('rejects competing turns, wrong thread and rewritten prefix',()=>{
    const root=temp(), path=join(root,'session.jsonl');
    writeFileSync(path,wire([meta(),...task('old')]));
    const boundary=captureCodexSourceBoundary(root,'session');
    appendFileSync(path,wire([...task('new','a',cu(200,20)),...task('foreign','b',cu(300,30))]));
    expect(codexNativeModelUsage(boundary,'session',cu(200,20))).toBeUndefined();
    expect(codexNativeModelUsage(boundary,'other',cu())).toBeUndefined();
    writeFileSync(path,wire([meta(),...task('new')]));
    expect(codexNativeModelUsage(boundary,'session',cu())).toBeUndefined();
  });
  it('rejects preexisting files for a supposedly fresh dispatch and symlinks',()=>{
    const root=temp(), path=join(root,'session.jsonl');
    writeFileSync(path,wire([meta(),...task()]));
    const boundary=captureCodexSourceBoundary(root);
    expect(codexNativeModelUsage(boundary,'session',cu())).toBeUndefined();
    symlinkSync(path,join(root,'link.jsonl'));
    expect(captureCodexSourceBoundary(root,'session')).toBeUndefined();
  });
  it('splits proven switching counters and refuses an ambiguous switch',()=>{
    const root=temp(), path=join(root,'session.jsonl');const boundary=captureCodexSourceBoundary(root);
    const rows=[meta(),...task().slice(0,-1),
      {type:'turn_context',payload:{turn_id:'turn',model:'model-b',model_provider:'openai'}},
      {type:'event_msg',payload:{type:'token_count',info:{total_token_usage:cu(150,15),
        last_token_usage:{input_tokens:50,cached_input_tokens:0,output_tokens:5}}}},
      {type:'event_msg',payload:{type:'task_complete',turn_id:'turn'}}];
    writeFileSync(path,wire(rows));
    expect(codexNativeModelUsage(boundary,'session',cu(150,15))?.models.map(m=>m.model)).toEqual(['model-a','model-b']);
    delete (rows[5] as any).payload.info.last_token_usage;
    writeFileSync(path,wire(rows));
    expect(codexNativeModelUsage(boundary,'session',cu(150,15))).toBeUndefined();
  });
  it('keeps a model switch ambiguous through a zero-delta cumulative update',()=>{
    const root=temp(), path=join(root,'session.jsonl'), boundary=captureCodexSourceBoundary(root);
    const rows=[meta(),...task().slice(0,-1),
      {type:'turn_context',payload:{turn_id:'turn',model:'model-b',model_provider:'openai'}},
      {type:'event_msg',payload:{type:'token_count',info:{total_token_usage:cu()}}},
      {type:'event_msg',payload:{type:'token_count',info:{total_token_usage:cu(150,15),
        last_token_usage:{input_tokens:40,cached_input_tokens:0,output_tokens:5}}}},
      {type:'event_msg',payload:{type:'task_complete',turn_id:'turn'}}];
    writeFileSync(path,wire(rows));
    expect(codexNativeModelUsage(boundary,'session',cu(150,15))).toBeUndefined();
    (rows[6] as any).payload.info.last_token_usage.input_tokens=50;
    writeFileSync(path,wire(rows));
    expect(codexNativeModelUsage(boundary,'session',cu(150,15))?.models.map(m=>m.model)).toEqual(['model-a','model-b']);
    // Repeated notifications may retain the preceding nonzero last usage.
    (rows[5] as any).payload.info.last_token_usage=cu();
    writeFileSync(path,wire(rows));
    expect(codexNativeModelUsage(boundary,'session',cu(150,15))?.models.map(m=>m.model)).toEqual(['model-a','model-b']);
  });
  it('keeps incomplete, compacted and missing-cache cases unknown',()=>{
    for (const records of [task().slice(0,-1),
      [...task().slice(0,-1),{type:'compaction'},...task().slice(-1)],
      task('turn','model-a',{input_tokens:100,output_tokens:10} as never)]) {
      const root=temp(), boundary=captureCodexSourceBoundary(root);
      writeFileSync(join(root,'session.jsonl'),wire([meta(),...records]));
      expect(codexNativeModelUsage(boundary,'session',cu())).toBeUndefined();
    }
  });
});
