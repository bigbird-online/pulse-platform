import {describe,it,expect} from 'vitest';
import {mkdtempSync,mkdirSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname,join} from 'node:path';
import {statsArguments,statsLocation,runStatsQuery,STATS_TIMEOUT_MS,STATS_MAX_BYTES} from '../runtime/pulse-stats-tool.mjs';

function installed() {
  const root=mkdtempSync(join(tmpdir(),'pulse-stats-tool-'));
  const location=statsLocation(root);
  for (const file of [location.python,location.script]) {mkdirSync(dirname(file),{recursive:true});writeFileSync(file,'');}
  return {root,location};
}

describe('pulse_stats arguments',()=>{
  it('maps the declared fields to fixed CLI flags',()=>{
    expect(statsArguments({query:'overview'})).toEqual(['overview','--json']);
    expect(statsArguments({query:'wi',work_item_id:'wi_20261028_253',period:'month'}))
      .toEqual(['wi','--period','month','--work-item-id','wi_20261028_253','--json']);
    expect(statsArguments({query:'cost',period:'last_week',group:'wi'}))
      .toEqual(['cost','--period','last_week','--group','wi','--json']);
    expect(statsArguments({query:'cost',group:'runtime',agent:'pulse',runtime:'cc'}))
      .toEqual(['cost','--group','runtime','--agent','pulse','--runtime','cc','--json']);
    expect(statsArguments({query:'metric',metric_id:'relay.cc.used_percent'}))
      .toEqual(['metric','--metric-id','relay.cc.used_percent','--json']);
    expect(statsArguments({query:'compactions',work_item_id:'wi_20261118_988',session_id:'5f4add7e-4eff-4878-9c67-5c09e94f17f2'}))
      .toEqual(['compactions','--work-item-id','wi_20261118_988','--session-id','5f4add7e-4eff-4878-9c67-5c09e94f17f2','--json']);
    expect(statsArguments({query:'agent_day',agent:'emi',date:'2026-11-08'}))
      .toEqual(['agent_day','--agent','emi','--date','2026-11-08','--json']);
    expect(statsArguments({query:'agent_day',agent:'pulse',date:'2026-08-20',end_date:'2026-11-08',ws:'Bigbird/PULSE/'}))
      .toEqual(['agent_day','--agent','pulse','--date','2026-08-20','--end-date','2026-11-08','--ws','Bigbird/PULSE/','--json']);
  });
  it('rejects anything outside the schema before spawning',()=>{
    for (const bad of [undefined,[],{},{query:'drop'},{query:'cost',period:'year'},{query:'cost',group:'x'},
      {query:'wi'},{query:'wi',work_item_id:'wi_x'},{query:'cost',agent:'--snapshot=/etc/passwd'},
      {query:'cost',agent:'../x'},{query:'cost',snapshot:'/tmp/x'},{query:'cost',period:1},
      {query:'metric',metric_id:'A;rm'},{query:'compactions',session_id:'../x'},{query:'agent_day',agent:'emi'},{query:'agent_day',date:'2026-11-08'},
      {query:'agent_day',agent:'emi',date:'11/08/2026'},{query:'agent_day',agent:'emi',date:'2026-11-08',ws:'../etc/'},
      {query:'agent_day',agent:'emi',date:'2026-11-08',ws:'/etc/'},{query:'agent_day',agent:'emi',date:'2026-11-08',ws:'a/../b'}])
      expect(()=>statsArguments(bad as any)).toThrow();
  });
});

describe('pulse_stats spawning',()=>{
  it('runs the installed query with a fixed environment and bounds',async()=>{
    const {root,location}=installed();
    let seen:any;
    const out=JSON.stringify({schema:'pulse_stats_query/v1',status:'partial',errors:['app_snapshot:stale'],result:{}});
    try {
      const result=await runStatsQuery({query:'board',agent:'duke'},location,((file:string,args:string[],options:any,cb:Function)=>{
        seen={file,args,options};cb(null,out+'\n','private stderr');}) as any);
      expect(result).toEqual({isError:false,text:out});
      expect(seen.file).toBe(location.python);
      expect(seen.args).toEqual(['-I','-B',location.script,'board','--agent','duke','--json']);
      expect(seen.options).toMatchObject({timeout:STATS_TIMEOUT_MS,maxBuffer:STATS_MAX_BYTES,cwd:'/'});
      expect(seen.options.env).toEqual({HOME:root,PATH:'/usr/bin:/bin',LANG:'en_US.UTF-8'});
    } finally {rmSync(root,{recursive:true,force:true});}
  });
  it('reports failures by code without relaying stderr',async()=>{
    const {root,location}=installed();
    try {
      const cases:[any,string,string][]=[[Object.assign(new Error('t'),{killed:true}),'','stats_query_timeout'],
        [Object.assign(new Error('m'),{code:'ERR_CHILD_PROCESS_STDIO_MAXBUFFER'}),'','stats_query_output_too_large'],
        [null,'not json','stats_query_result_unavailable'],[null,'{"schema":"other"}','stats_query_result_unavailable']];
      for (const [error,stdout,code] of cases) {
        const result=await runStatsQuery({query:'health'},location,((_f:string,_a:string[],_o:any,cb:Function)=>
          cb(error,stdout,'/Users/secret/path')) as any);
        expect(result.isError).toBe(true);
        expect(JSON.parse(result.text).errors).toEqual([code]);
        expect(result.text).not.toContain('secret');
      }
    } finally {rmSync(root,{recursive:true,force:true});}
  });
  it('is unavailable, not spawned, where Stats is not installed',async()=>{
    const root=mkdtempSync(join(tmpdir(),'pulse-stats-none-'));
    try {
      const result=await runStatsQuery({query:'quotas'},statsLocation(root),(()=>{throw new Error('spawned');}) as any);
      expect(result.isError).toBe(true);
      expect(JSON.parse(result.text).errors).toEqual(['stats_query_not_installed']);
    } finally {rmSync(root,{recursive:true,force:true});}
  });
});

describe('pulse_stats in Pi',()=>{
  it('registers before the WI tools only where Stats is installed',async()=>{
    const {installPiWorkItemCore}=await import('../runtime/pi-work-item-core.mjs');
    const {root}=installed();
    const previous=process.env.PULSE_STATS_ACCOUNT_HOME;
    try {
      for (const [home,expected] of [[root,['pulse_stats','declare_session_wi','pulse_work_item']],
        [join(root,'absent'),['declare_session_wi','pulse_work_item']]] as [string,string[]][]) {
        process.env.PULSE_STATS_ACCOUNT_HOME=home;
        const names:string[]=[];
        installPiWorkItemCore({on:()=>{},registerTool:(t:any)=>names.push(t.name)},{python:'/p',entrypoint:'/e'});
        expect(names).toEqual(expected);
      }
    } finally {
      if (previous===undefined) delete process.env.PULSE_STATS_ACCOUNT_HOME; else process.env.PULSE_STATS_ACCOUNT_HOME=previous;
      rmSync(root,{recursive:true,force:true});
    }
  });
});
