import { describe, expect, it } from 'vitest';
import type { JobResult, ScheduledJob } from '../src/types.js';
import { startObservation, finishObservation } from '../src/job-run-observation.js';

const job = (): ScheduledJob => ({id:'example',name:'example',enabled:true,owner:'aki',
  schedule:{expr:'0 12 * * *',tz:'Asia/Shanghai'},agent:{workspace:'/fixture',prompt:'private prompt'},
  delivery:{mode:'none'},_state:{consecutiveErrors:0,definitionHash:'a'.repeat(64),activatedAt:'2026-09-18T00:00:00Z'}});
const ok: JobResult = {jobId:'example',runId:'run-one',status:'ok',executionStatus:'ok',deliveryStatus:'sent',output:'private output',durationMs:20};
const failed: JobResult = {...ok,status:'error',executionStatus:'error',deliveryStatus:'failed'};

describe('native cron result observations',()=>{
  it('persists only safe execution and delivery facts, not prompts, output or errors',()=>{
    const j=job();const run=startObservation(j,new Date('2026-09-18T04:00:00Z'));
    const state=finishObservation(j,ok,run);
    expect(state.lastResult?.runId).toBe('run-one');expect(state.lastResult?.deliveryStatus).toBe('sent');
    expect(JSON.stringify(state)).not.toContain('private');expect(state.currentRun).toBeUndefined();
  });
  it('records a pre-LLM gate skip as an intended skip',()=>{
    const j=job();const run=startObservation(j,new Date('2026-09-18T04:00:00Z'));
    const state=finishObservation(j,{...ok,deliveryStatus:'skipped',skipped:true,reasonCode:'gate_skip'},run);
    expect(state.lastResult?.skipped).toBe(true);
    const plain=finishObservation(job(),{...ok,deliveryStatus:'skipped'},run);
    expect(plain.lastResult?.skipped).toBe(false);
  });
  it('keeps only a gate skip summary, bounded, never other output',()=>{
    const j=job();const run=startObservation(j,new Date('2026-09-18T04:00:00Z'));
    const gate=finishObservation(j,{...ok,deliveryStatus:'skipped',skipped:true,reasonCode:'gate_skip',
      output:'owner/repo blocked (already reported): '+'x'.repeat(900)},run);
    expect(gate.lastResult?.skipSummary?.startsWith('owner/repo blocked (already reported)')).toBe(true);
    expect(gate.lastResult?.skipSummary?.length).toBe(500);
    expect(finishObservation(job(),ok,run).lastResult?.skipSummary).toBeUndefined();
  });
  it('a new scheduled slot does not erase prior missing delivery',()=>{
    const j=job();const first=startObservation(j,new Date('2026-09-18T04:00:00Z'));
    j._state=finishObservation(j,failed,first);
    const second=startObservation(j,new Date('2026-09-19T04:00:00Z'));
    j._state=finishObservation(j,{...ok,runId:'run-two'},second);
    expect(j._state.unresolvedResults).toHaveLength(1);
    expect(j._state.unresolvedResults?.[0]?.scheduledFor).toBe(first.scheduledFor);
  });
  it('a convergent check settles its earlier misses with a later success, never with a deferral',()=>{
    const j={...job(),convergent:true};const first=startObservation(j,new Date('2026-09-18T04:00:00Z'));
    j._state=finishObservation(j,failed,first);
    j._state=finishObservation(j,{...ok,runId:'run-deferred',deferred:true},startObservation(j,new Date('2026-09-18T04:30:00Z')));
    expect(j._state.unresolvedResults).toHaveLength(2);
    j._state=finishObservation(j,{...ok,runId:'run-two'},startObservation(j,new Date('2026-09-18T05:00:00Z')));
    expect(j._state.unresolvedResults).toHaveLength(0);
    j._state=finishObservation(j,failed,startObservation(j,new Date('2026-09-18T05:30:00Z')));
    expect(j._state.unresolvedResults).toHaveLength(1);
  });
  it('recovery must match the original scheduled slot and definition',()=>{
    const j=job();const first=startObservation(j,new Date('2026-09-18T04:00:00Z'));
    j._state=finishObservation(j,failed,first);
    j._state=finishObservation(j,ok,first);
    expect(j._state.unresolvedResults).toHaveLength(0);
  });
  it('retains executed definition identity across a configuration reload',()=>{
    const j=job();const first=startObservation(j);
    j._state!.definitionHash='b'.repeat(64);
    j._state=finishObservation(j,ok,first);
    expect(j._state.definitionHash).toBe('b'.repeat(64));
    expect(j._state.lastResult?.definitionHash).toBe('a'.repeat(64));
  });
  it('limits summaries without making missing history look complete',()=>{
    const j=job();
    for(let i=0;i<35;i++)j._state=finishObservation(j,{...failed,runId:String(i)},startObservation(j,new Date(2026,0,i+1)));
    expect(j._state!.unresolvedResults).toHaveLength(32);expect(j._state!.resultHistoryIncomplete).toBe(true);
  });
  it('keeps the verify command receipt and never infers it from the exit status',()=>{
    const verify={command:'/fake/record-check',args:['daily']};
    const j={...job(),agent:{...job().agent,verify}};const run=startObservation(j);
    expect(finishObservation(j,{...ok,verifyStatus:'passed'},run).lastResult?.verifyStatus).toBe('passed');
    expect(finishObservation(j,{...failed,verifyStatus:'failed'},run).lastResult?.verifyStatus).toBe('failed');
    expect(finishObservation(j,ok,run).lastResult?.verifyStatus).toBeUndefined();
    expect(finishObservation(job(),{...ok,verifyStatus:'passed'},run).lastResult?.verifyStatus).toBeUndefined();
  });
  it('does not convert a deferred run or unvalidated artifact into success evidence',()=>{
    const j=job();const run=startObservation(j);
    const state=finishObservation(j,{...ok,deferred:true},run);
    expect(state.lastResult?.deferred).toBe(true);expect(state.lastResult?.artifactVerified).toBeUndefined();
    expect(state.unresolvedResults).toHaveLength(1);
  });
});
