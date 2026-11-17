import {describe,it,expect} from 'vitest';
import {ContextRolloverHints,carriesRolloverHint,withRolloverHint} from '../src/context-rollover-hint.js';
import {bindHumanMessageProof,humanMessageProof} from '../src/collaboration-capture.js';

describe('context rollover hint',()=>{
  it('hints once when CC reports the context nearly full',()=>{
    const hints=new ContextRolloverHints({} as NodeJS.ProcessEnv);
    hints.observe('t:s',{type:'result',context_window:{used_percentage:40,context_window_size:200000}} as any);
    expect(hints.take('t:s')).toBeUndefined();
    hints.observe('t:s',{type:'result',context_window:{used_percentage:65,context_window_size:200000}} as any);
    const hint=hints.take('t:s');
    expect(hint).toContain('about 65% of its context');
    expect(hint).toContain('request_rollover');
    expect(hints.take('t:s')).toBeUndefined();
  });

  it('reads occupancy from assistant usage over the result model window, and counts compactions',()=>{
    const hints=new ContextRolloverHints({RELAY_ROLLOVER_HINT_RATIO:'0.9'} as NodeJS.ProcessEnv);
    hints.observe('t:s',{type:'result',modelUsage:{'claude-opus':{contextWindow:1000}}} as any);
    hints.observe('t:s',{type:'assistant',message:{usage:{input_tokens:10,cache_read_input_tokens:700}}} as any);
    expect(hints.take('t:s')).toBeUndefined();
    hints.observe('t:s',{type:'system',subtype:'compact_boundary'} as any);
    expect(hints.take('t:s')).toBeUndefined();
    hints.observe('t:s',{type:'system',subtype:'compact_boundary'} as any);
    expect(hints.take('t:s')).toContain('2 compactions');
    hints.forget('t:');
    expect(hints.take('t:s')).toBeUndefined();
  });

  it('rides only on ordinary human text and keeps its provenance',()=>{
    expect(carriesRolloverHint({type:'human',content:'/wi status'} as any)).toBe(false);
    expect(carriesRolloverHint({type:'human',content:'[Host session continuation]'} as any)).toBe(false);
    expect(carriesRolloverHint({type:'human',content:'go on',delivery_visibility:'control'} as any)).toBe(false);
    const message={type:'human',content:'go on'} as any;
    expect(carriesRolloverHint(message)).toBe(true);
    bindHumanMessageProof(message,{ref:'d'.repeat(64),observedAtMs:1700000000000});
    const hinted=withRolloverHint(message,'\n\n[hint]');
    expect(hinted.content).toBe('go on\n\n[hint]');
    expect(humanMessageProof(hinted)?.ref).toBe('d'.repeat(64));
  });
});
