import {describe,it,expect,beforeEach,afterEach,vi} from 'vitest';
import {mkdirSync,mkdtempSync,readFileSync,rmSync,statSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ContextRolloverHints,carriesRolloverHint,compactionSessionRef,resolveContextCompactionsPath,
  resolveContextRolloverStatePath,rolloverHintText,withRolloverHint,withoutRolloverHint} from '../src/context-rollover-hint.js';
import {bindHumanMessageProof,collaborationRef,humanMessageProof} from '../src/collaboration-capture.js';

const SESSION='5f4add7e-4eff-4878-9c67-5c09e94f17f2';
const compact={type:'system',subtype:'compact_boundary'} as any;
// What CC print mode forwards for a declare_session_wi call: its tool_result, never the tool-only call.
const declared=(wi:string,is_error=false,schema='cc_request_wi/v1')=>({type:'user',message:{role:'user',content:[
  {type:'tool_result',tool_use_id:'toolu_wi',is_error,content:[{type:'text',text:JSON.stringify({origin:'agent_declaration',schema,wi})}]}]}}) as any;

describe('context rollover hint',()=>{
  let dir:string;
  let path:string;
  let state:string;
  beforeEach(()=>{dir=mkdtempSync(join(tmpdir(),'rollover-hint-'));path=join(dir,'context','context-compactions-v1.json');
    state=join(dir,'context','context-rollover-sessions-v1.json');});
  afterEach(()=>rmSync(dir,{recursive:true,force:true}));
  const env=(extra:Record<string,string>={})=>({PULSE_CONTEXT_COMPACTIONS_PATH:path,...extra}) as NodeJS.ProcessEnv;

  it('hints once when CC reports the context nearly full',()=>{
    const hints=new ContextRolloverHints({} as NodeJS.ProcessEnv);
    hints.observe('t',{type:'result',context_window:{used_percentage:40,context_window_size:200000}} as any);
    expect(hints.take('t')).toBeUndefined();
    hints.observe('t',{type:'result',context_window:{used_percentage:65,context_window_size:200000}} as any);
    const hint=hints.take('t');
    expect(hint).toContain('本会话上下文已用约 65%');
    expect(hint).toContain('request_rollover');
    expect(hints.take('t')).toBeUndefined();
  });

  it('from the third compaction on, hints on every turn with the count and occupancy',()=>{
    const hints=new ContextRolloverHints({RELAY_ROLLOVER_HINT_RATIO:'0.9'} as NodeJS.ProcessEnv);
    hints.observe('t',{type:'result',modelUsage:{'claude-opus':{contextWindow:1000}}} as any);
    hints.observe('t',compact);
    hints.observe('t',compact);
    expect(hints.take('t')).toBeUndefined();
    hints.observe('t',compact);
    hints.observe('t',{type:'assistant',message:{usage:{input_tokens:10,cache_read_input_tokens:300}}} as any);
    const hint=hints.take('t');
    expect(hint).toBe('[Host 上下文提醒，不是人类决定：本会话已压缩 3 次（上下文约 31%）。若你持有仍有工作的 WI，'
      +'现在先记 checkpoint（已完成、待办、下一步），再对该 WI 执行 request_rollover 并结束本轮；Host 会在新会话里接续。]');
    expect(hints.take('t')).toBe(hint);
    expect(hints.take('t')).toBe(hint);
  });

  it('the compaction threshold follows RELAY_ROLLOVER_HINT_COMPACTIONS',()=>{
    const hints=new ContextRolloverHints({RELAY_ROLLOVER_HINT_COMPACTIONS:'1'} as NodeJS.ProcessEnv);
    hints.observe('t',compact);
    expect(hints.take('t')).toContain('本会话已压缩 1 次');
  });

  it('keeps the count across a Relay restart and a kill, and records the declared WI',()=>{
    const first=new ContextRolloverHints(env(),()=>new Date('2026-11-18T04:00:00Z'));
    const key='pulse';
    first.observe(key,declared('wi_20261118_988'),{sessionId:SESSION});
    first.observe(key,compact,{sessionId:SESSION});
    first.observe(key,compact,{sessionId:SESSION});
    first.forget('pulse');
    first.observe(key,compact,{sessionId:SESSION});
    expect(first.take(key,SESSION)).toContain('本会话已压缩 3 次');

    const document=JSON.parse(readFileSync(path,'utf8'));
    expect(statSync(path).mode&0o777).toBe(0o600);
    expect(document).toEqual({schema:'pulse.stats.context_compactions/v1',source:'relay_cc_compact_boundary',
      updated_at:'2026-11-18T04:00:00.000Z',recording_started_at:'2026-11-18T04:00:00.000Z',dropped_through:null,sessions:[{session_ref:compactionSessionRef(SESSION),runtime:'cc',
        wi:'wi_20261118_988',compactions:3,by_wi:[{wi:'wi_20261118_988',compactions:3}],context_ratio:null,first_compaction_at:'2026-11-18T04:00:00.000Z',
        last_compaction_at:'2026-11-18T04:00:00.000Z'}]});
    expect(readFileSync(path,'utf8')).not.toContain(SESSION);
    expect(statSync(state).mode&0o777).toBe(0o600);
    expect(JSON.parse(readFileSync(state,'utf8')).sessions).toEqual([{session_ref:compactionSessionRef(SESSION),
      tenant_ref:collaborationRef('relay_tenant',['pulse']),wi:'wi_20261118_988',compactions:3,
      by_wi:[{wi:'wi_20261118_988',compactions:3}],context_ratio:null,
      first_compaction_at:'2026-11-18T04:00:00.000Z',last_compaction_at:'2026-11-18T04:00:00.000Z',updated_at:'2026-11-18T04:00:00.000Z'}]);
    expect(readFileSync(state,'utf8')).not.toContain(SESSION);
    expect(readFileSync(state,'utf8')).not.toContain('pulse"');

    const restarted=new ContextRolloverHints(env());
    expect(restarted.take(key,SESSION)).toContain('本会话已压缩 3 次');
    restarted.observe(key,compact,{sessionId:SESSION,wi:'wi_20261101_001'});
    const [row]=JSON.parse(readFileSync(path,'utf8')).sessions;
    // A declaration names the session's WI before the Host metering binding.
    expect([row.compactions,row.wi]).toEqual([4,'wi_20261118_988']);
  });

  it('counts each compaction toward the WI the session had then; a later declaration only relabels the session',()=>{
    const hints=new ContextRolloverHints(env());
    const A='wi_20261118_001', B='wi_20261118_002';
    const rows=()=>JSON.parse(readFileSync(path,'utf8')).sessions;
    hints.observe('pulse',compact,{sessionId:SESSION});
    hints.observe('pulse',declared(A),{sessionId:SESSION});
    hints.observe('pulse',compact,{sessionId:SESSION});
    hints.observe('pulse',compact,{sessionId:SESSION});
    hints.observe('pulse',declared(B),{sessionId:SESSION});
    expect(rows()).toEqual([expect.objectContaining({wi:B,compactions:3,by_wi:[{wi:null,compactions:1},{wi:A,compactions:2}]})]);
    hints.observe('pulse',compact,{sessionId:SESSION});
    expect(rows()[0].by_wi).toEqual([{wi:null,compactions:1},{wi:A,compactions:2},{wi:B,compactions:1}]);
    expect(JSON.parse(readFileSync(state,'utf8')).sessions[0].by_wi).toEqual(rows()[0].by_wi);
    // The split survives a restart and keeps growing under the WI the session has now.
    const restarted=new ContextRolloverHints(env());
    restarted.observe('pulse',compact,{sessionId:SESSION});
    expect(rows()[0]).toEqual(expect.objectContaining({compactions:5,
      by_wi:[{wi:null,compactions:1},{wi:A,compactions:2},{wi:B,compactions:2}]}));
  });

  it('restores from whichever file holds the later copy, and logs a write that failed',()=>{
    const hints=new ContextRolloverHints(env({RELAY_ROLLOVER_HINT_RATIO:'0.99'}));
    hints.observe('pulse',compact,{sessionId:SESSION});
    hints.observe('pulse',compact,{sessionId:SESSION});
    const two=readFileSync(state,'utf8');
    hints.observe('pulse',compact,{sessionId:SESSION});
    // The third compaction reached only the Stats record.
    writeFileSync(state,two);
    const restarted=new ContextRolloverHints(env({RELAY_ROLLOVER_HINT_RATIO:'0.99'}));
    expect(restarted.take('pulse',SESSION)).toContain('本会话已压缩 3 次');
    expect(restarted.take('pulse',SESSION)).toContain('本会话已压缩 3 次');
    restarted.observe('pulse',compact,{sessionId:SESSION});
    expect(JSON.parse(readFileSync(state,'utf8')).sessions[0].compactions).toBe(4);
    expect(JSON.parse(readFileSync(path,'utf8')).sessions[0].compactions).toBe(4);

    // The other way round: the rollover state is ahead of the Stats record.
    const ahead=new ContextRolloverHints(env());
    const record=readFileSync(path,'utf8');
    ahead.observe('pulse',compact,{sessionId:SESSION});
    writeFileSync(path,record);
    expect(new ContextRolloverHints(env()).take('pulse',SESSION)).toContain('本会话已压缩 5 次');

    // A write that fails is logged, and the other file and the turn go on (the record holds 4).
    const warn=vi.spyOn(console,'warn').mockImplementation(()=>{});
    try {
      rmSync(state);
      mkdirSync(state);
      const failing=new ContextRolloverHints(env());
      failing.observe('pulse',compact,{sessionId:SESSION});
      expect(failing.take('pulse',SESSION)).toContain('本会话已压缩 5 次');
      expect(warn).toHaveBeenCalledWith(expect.objectContaining({level:'warn',msg:'Context compaction rollover state write failed'}));
      expect(JSON.parse(readFileSync(path,'utf8')).sessions[0].compactions).toBe(5);
    } finally { warn.mockRestore(); }
  });

  it('a row whose split does not add up to its count is not restored',()=>{
    const hints=new ContextRolloverHints(env());
    for (let i=0;i<3;i++) hints.observe('b',compact,{sessionId:SESSION});
    for (const file of [state,path]) {
      const document=JSON.parse(readFileSync(file,'utf8'));
      document.sessions[0].by_wi=[{wi:null,compactions:2}];
      writeFileSync(file,JSON.stringify(document));
    }
    expect(new ContextRolloverHints(env()).take('b',SESSION)).toBeUndefined();
  });

  it('takes the WI only from a successful declaration acknowledgement',()=>{
    const hints=new ContextRolloverHints(env());
    const key='pulse';
    hints.observe(key,declared('wi_20261118_988'),{sessionId:SESSION});
    hints.observe(key,compact,{sessionId:SESSION});
    // A failed call, a rejected shape, a none declaration and another tool's output change nothing.
    hints.observe(key,declared('wi_20261101_001',true),{sessionId:SESSION});
    hints.observe(key,{type:'user',message:{content:[{type:'tool_result',tool_use_id:'toolu_wi',is_error:true,
      content:[{type:'text',text:JSON.stringify({status:'unavailable',reason:'explicit_wi_required',retry_arguments:{wi:'wi_20261102_002'}})}]}]}} as any,
      {sessionId:SESSION});
    hints.observe(key,declared('none'),{sessionId:SESSION});
    hints.observe(key,{type:'user',message:{content:[{type:'tool_result',tool_use_id:'toolu_cat',
      content:JSON.stringify({origin:'agent_declaration',schema:'other/v1',wi:'wi_20261103_003'})}]}} as any,{sessionId:SESSION});
    hints.observe(key,compact,{sessionId:SESSION});
    expect(JSON.parse(readFileSync(path,'utf8')).sessions[0].wi).toBe('wi_20261118_988');
    hints.observe(key,declared('wi_20261104_004',false,'cc_session_wi/v1'),{sessionId:SESSION});
    expect(JSON.parse(readFileSync(path,'utf8')).sessions[0].wi).toBe('wi_20261104_004');
  });

  it('stops once the session is replaced by a rollover',()=>{
    const hints=new ContextRolloverHints(env());
    for (let i=0;i<3;i++) hints.observe('pulse',compact,{sessionId:SESSION});
    expect(hints.take('pulse',SESSION)).toBeDefined();
    const successor='0a1b2c3d-1111-2222-3333-444455556666';
    expect(hints.take('pulse',successor)).toBeUndefined();
    hints.observe('pulse',{type:'result',context_window:{used_percentage:5}} as any,{sessionId:successor});
    expect(hints.take('pulse',successor)).toBeUndefined();
  });

  it('the Stats window drops an old session, the rollover state keeps it and restores it after a restart',()=>{
    const A='aaaaaaaa-0000-0000-0000-000000000000';
    const old=new ContextRolloverHints(env(),()=>new Date('2026-10-01T00:00:00Z'));
    for (let i=0;i<3;i++) old.observe('a',compact,{sessionId:A});
    const now=new ContextRolloverHints(env(),()=>new Date('2026-11-18T00:00:00Z'));
    now.observe('b',compact,{sessionId:SESSION});
    const record=JSON.parse(readFileSync(path,'utf8'));
    expect(record.sessions.map((s:any)=>s.session_ref)).toEqual([compactionSessionRef(SESSION)]);
    // The record states what it no longer answers for.
    expect([record.recording_started_at,record.dropped_through]).toEqual(['2026-10-01T00:00:00.000Z','2026-10-01T00:00:00.000Z']);

    // Relay restarts and the same session resumes: it still carries the hint, and keeps counting from 3.
    const restarted=new ContextRolloverHints(env(),()=>new Date('2026-11-18T01:00:00Z'));
    expect(restarted.take('a',A)).toContain('本会话已压缩 3 次');
    restarted.observe('a',compact,{sessionId:A});
    expect(restarted.take('a',A)).toContain('本会话已压缩 4 次');
    const back=JSON.parse(readFileSync(path,'utf8'));
    expect(back.sessions.find((s:any)=>s.session_ref===compactionSessionRef(A))?.compactions).toBe(4);
    expect(back.dropped_through).toBe('2026-10-01T00:00:00.000Z');
  });

  it('the rollover state drops only sessions a tenant ended long ago, past its newest sixteen',()=>{
    let clock=Date.parse('2026-11-01T00:00:00Z');
    const hints=new ContextRolloverHints(env(),()=>new Date(clock));
    const sid=(i:number)=>`cccccccc-0000-0000-0000-${String(i).padStart(12,'0')}`;
    hints.observe('other',compact,{sessionId:SESSION});
    for (let i=0;i<17;i++) { clock+=60_000; hints.observe('c',compact,{sessionId:sid(i)}); }
    // Another tenant writes much later; nothing of tenant c is affected by age alone.
    clock+=90*24*3600_000;
    hints.observe('d',compact,{sessionId:'dddddddd-0000-0000-0000-000000000000'});
    const refs=JSON.parse(readFileSync(state,'utf8')).sessions.map((s:any)=>s.session_ref);
    expect(refs).toHaveLength(18);
    expect(refs).not.toContain(compactionSessionRef(sid(0)));
    expect(refs).toContain(compactionSessionRef(sid(1)));
    expect(refs).toContain(compactionSessionRef(SESSION));
  });

  it('a damaged rollover state falls back to the Stats record; both damaged read as none',()=>{
    const hints=new ContextRolloverHints(env());
    for (let i=0;i<3;i++) hints.observe('b',compact,{sessionId:SESSION});
    writeFileSync(state,'{broken');
    expect(new ContextRolloverHints(env()).take('b',SESSION)).toContain('本会话已压缩 3 次');
    writeFileSync(path,'{broken');
    expect(new ContextRolloverHints(env()).take('b',SESSION)).toBeUndefined();
  });

  it('resolves the file under RELAY_DATA_DIR unless named explicitly',()=>{
    expect(resolveContextCompactionsPath({RELAY_DATA_DIR:'/d'} as NodeJS.ProcessEnv)).toBe('/d/context/context-compactions-v1.json');
    expect(resolveContextCompactionsPath({RELAY_DATA_DIR:'/d',PULSE_CONTEXT_COMPACTIONS_PATH:'/x.json'} as NodeJS.ProcessEnv)).toBe('/x.json');
    expect(resolveContextCompactionsPath({} as NodeJS.ProcessEnv)).toBeUndefined();
    expect(resolveContextRolloverStatePath({RELAY_DATA_DIR:'/d'} as NodeJS.ProcessEnv)).toBe('/d/context/context-rollover-sessions-v1.json');
    expect(resolveContextRolloverStatePath({RELAY_DATA_DIR:'/d',PULSE_CONTEXT_ROLLOVER_STATE_PATH:'/s.json'} as NodeJS.ProcessEnv)).toBe('/s.json');
  });

  it('rides on human messages and Host deliveries, never on commands, control or machine envelopes',()=>{
    expect(carriesRolloverHint({type:'human',content:'/wi status'} as any)).toBe(false);
    expect(carriesRolloverHint({type:'human',content:'go on',delivery_visibility:'control'} as any)).toBe(false);
    expect(carriesRolloverHint({type:'human',content:'<task-notification>x</task-notification>'} as any)).toBe(false);
    expect(carriesRolloverHint({type:'human',content:'SCL_HOST_RECOVERY_RESULT: x'} as any)).toBe(false);
    expect(carriesRolloverHint({type:'human',content:'[Host scheduled wake-up] 早报'} as any)).toBe(true);
    expect(carriesRolloverHint({type:'human',content:'合同投递\n\n[Host internal delivery reference: '+'a'.repeat(64)+'. Relay delivery, not a human message.]'} as any)).toBe(true);
    expect(carriesRolloverHint({type:'human',content:'go on'} as any)).toBe(true);
  });

  it('opens the message so the provenance reference stays last, and keeps the proof',()=>{
    const message={type:'human',content:'go on\n\n[Host human input reference: '+'d'.repeat(64)+'. This records message provenance.]'} as any;
    bindHumanMessageProof(message,{ref:'d'.repeat(64),observedAtMs:1700000000000});
    const hinted=withRolloverHint(message,'[hint]');
    expect(hinted.content).toBe('[hint]\n\n'+message.content);
    expect(humanMessageProof(hinted)?.ref).toBe('d'.repeat(64));
    const blocks=withRolloverHint({type:'human',content:[{type:'text',text:'see'}]} as any,'[hint]');
    expect(blocks.content).toEqual([{type:'text',text:'[hint]\n\n'},{type:'text',text:'see'}]);
  });

  it('replaces the hint a resumed record already opens with',()=>{
    const earlier=rolloverHintText(undefined,3);
    const now=rolloverHintText(0.4,4);
    expect(withRolloverHint(withRolloverHint({type:'human',content:'go on'} as any,earlier),now).content).toBe(now+'\n\ngo on');
    const blocks=withRolloverHint(withRolloverHint({type:'human',content:[{type:'text',text:'see'}]} as any,earlier),now);
    expect(blocks.content).toEqual([{type:'text',text:now+'\n\n'},{type:'text',text:'see'}]);
  });

  it('removes an earlier hint and keeps the proof, leaving a message without one unchanged',()=>{
    const message={type:'human',content:rolloverHintText(undefined,3)+'\n\ngo on'} as any;
    bindHumanMessageProof(message,{ref:'e'.repeat(64),observedAtMs:1700000000000});
    const bare=withoutRolloverHint(message);
    expect(bare.content).toBe('go on');
    expect(humanMessageProof(bare)?.ref).toBe('e'.repeat(64));
    const plain={type:'human',content:'go on'} as any;
    expect(withoutRolloverHint(plain)).toBe(plain);
    expect(withoutRolloverHint(withRolloverHint({type:'human',content:[{type:'text',text:'see'}]} as any,rolloverHintText(0.7,0))).content)
      .toEqual([{type:'text',text:'see'}]);
  });
});
