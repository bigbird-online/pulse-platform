/** Existing manager -> durable Host/native facts -> ComputeCost -> Stats.
 * Synthetic adapter only: never spawn a provider, mutate SCL or production. */
import {EventEmitter} from 'node:events';
import {mkdtempSync, mkdirSync, writeFileSync, appendFileSync, readFileSync, readdirSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {execFileSync} from 'node:child_process';
import {describe, it, expect} from 'vitest';
import {createConversationRuntimeManager, type ConversationRuntimeAdapter, type ConversationProcess} from '../src/conversation-runtime.js';
import {createCollaborationCapture} from '../src/collaboration-capture.js';
import {buildRuntimeUsageBinding, runtimeAuthorityRef} from '../src/runtime-usage-binding.js';
import type {SpawnConfig} from '../src/types.js';

const wi='wi_20260919_101', agent='test-reviewer', sid='synthetic-integration-session';
describe('first-paid-call integration',()=>{
  it.each([true,false])('captures the first turn with WI purpose=%s before any grant, without double counting',async(hasPurpose)=>{
    const root=mkdtempSync(join(tmpdir(),'pulse-cost-wiring-'));
    const native=join(root,'native');mkdirSync(native);
    const file=join(native,'session.jsonl');
    let ms=Date.UTC(2026,8,19,1), configs:SpawnConfig[]=[];
    const tick=()=>new Date(ms+=100).toISOString();
    writeFileSync(file,JSON.stringify({type:'session',id:sid,version:3,timestamp:tick(),cwd:'/isolated/unmapped'})+'\n',{mode:0o600});
    const events=new EventEmitter();let process:ConversationProcess|undefined;let n=0;
    const capture=createCollaborationCapture(join(root,'host'),{now:tick})!;
    const manager=createConversationRuntimeManager({collaborationCapture:capture});
    const adapter:ConversationRuntimeAdapter={id:'pi',
      async spawn(config){configs.push(config);process={tenantId:config.tenantId,runtime:'pi',sessionId:sid,state:'ready',lastActivity:ms,spawnedAt:ms,restartCount:0,restartWindowStart:ms};return process;},
      async send(tenantId,_message){
        const at=tick();n++;
        appendFileSync(file,JSON.stringify({type:'message',id:'m'+n,timestamp:at,message:{role:'assistant',model:'openai/test',provider:'openai',usage:{input:100,cacheRead:20,cacheWrite:0,output:10,totalTokens:130}}})+'\n');
        const usage={input_tokens:100,cache_read_input_tokens:20,cache_creation_input_tokens:0,output_tokens:10};
        events.emit('response',tenantId,{type:'result',subtype:'success',session_id:sid,usage,
          native_model_usage:{schema:'pulse_native_model_usage/v1',runtime:'pi',models:[{model:'openai/test',provider:'openai',usage}]}});
      },
      async kill(){process=undefined;},async killAll(){process=undefined;},
      get(){return process;},getAll(){return process?[process]:[];},getStatus(){return [];},
      on(event,handler){events.on(event,handler);},dispose(){},emitShutdown(){return new Set<string>();}};
    manager.register(adapter);
    try{
      const config:SpawnConfig={tenantId:'isolated:reviewer',sessionId:sid,workspace:'/isolated/unmapped',
        meteringAgentId:agent,...(hasPurpose?{meteringWorkItemId:wi}:{}),
        conversationRuntime:{runtime:'pi',model:{provider:'openai',model:'test'},isolationProfile:'isolated'}};
      await manager.spawn(config);
      await manager.send(config.tenantId,{type:'human',content:'Only decide whether to accept. This text is NOT identity evidence.'});
      expect(configs[0]?.computeCostBinding).toBeUndefined();
      const first=readdirSync(join(root,'host')).flatMap(name=>readFileSync(join(root,'host',name),'utf8').trim().split('\n').map(line=>JSON.parse(line)));
      expect(first.filter(r=>r.kind==='execution'&&r.expected_usage).every(r=>r.executor===agent&&r.wi===(hasPurpose?wi:null)&&r.assignment_ref===null)).toBe(true);
      await manager.kill(config.tenantId);
      const binding=buildRuntimeUsageBinding({workItemId:wi,agentId:agent,phase:'work_item',epoch:1,runtime:'pi',procurementSource:'unknown',authorityRef:runtimeAuthorityRef(sid),executionSurface:'surface_v1_test'});
      await manager.spawn({...config,meteringWorkItemId:'wi_20260919_999',computeCostBinding:binding});
      await manager.send(config.tenantId,{type:'human',content:'Now complete the already-authorized task.'});
      const python=globalThis.process.env['STATS_PYTHON']??globalThis.process.env['PYTHON']??'python3';
      const source=resolve(__dirname,'../../ComputeCost');
      const program=`import json,sys
from pathlib import Path
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.collaboration_query import CollaborationQuery,query_collaboration
from pulse_compute_cost.stats_dashboard import _view
r=Path(sys.argv[1]);l=UsageLedger(r/'ledger')
sources={'schema':'computecost_source_registry/v1','required_receipt_features':['non_wi_activity.v1'],'sources':[
 {'id':'host','kind':'relay_host','path':str(r/'host'),'actor_attribution':True,'node':'test-node'},
 {'id':'native','kind':'pi','path':str(r/'native'),'actor_attribution':True,'node':'test-node'}]}
collect_registered_sources(sources,l)
catalog={'data':[{'id':'openai/test','pricing':{'prompt':'1','completion':'1','input_cache_read':'0.1','input_cache_write':'2'}}]}
q=query_collaboration(l,CollaborationQuery(scope='activity',from_date='2026-09-19',to_date='2026-09-20',timezone='UTC',group_by=('agent','wi')),catalog=catalog)
v=_view(q,'today','activity')
print(json.dumps({'groups':[(g['key'],g['usage']['event_count']) for g in v['groups']],
 'tokens':q['summary']['usage']['known_total_tokens'],'cost':q['summary']['cost']['partial_machine_cash_usd'],
 'unknown':_view(q,'today','activity')['summary']['usage']['agent_unassigned_event_count'],'replay_inserted':collect_registered_sources(sources,l)['inserted']}))`;
      const report=JSON.parse(execFileSync(python,['-c',program,root],{env:{...globalThis.process.env,PYTHONPATH:source},encoding:'utf8',timeout:30000}));
      expect(report.groups).toEqual(hasPurpose?[[{agent,wi},2]]:[[{agent,wi:'none'},1],[{agent,wi},1]]);
      expect(report.tokens).toBe(260);expect(report.cost).toBe('224');
      expect(report.unknown).toBe(0);expect(report.replay_inserted).toBe(0);
    }finally{manager.dispose();rmSync(root,{recursive:true,force:true});}
  });

  it('does not infer purpose from an environment or from model text, nor reject invalid telemetry',()=>{
    const root=mkdtempSync(join(tmpdir(),'pulse-cost-purpose-'));
    try{
      const capture=createCollaborationCapture(root)!;
      const invalid=capture.begin('pi',{tenantId:'a',workspace:'/tmp',meteringAgentId:agent,meteringWorkItemId:'not-a-wi'},'invalid');
      expect(invalid.common.wi).toBeNull();
      const ordinary=capture.begin('pi',{tenantId:'b',workspace:'/tmp',meteringAgentId:agent},'ordinary');
      expect(ordinary.common.wi).toBeNull();expect(ordinary.assignmentRef).toBeNull();
    }finally{rmSync(root,{recursive:true,force:true});}
  });
});
