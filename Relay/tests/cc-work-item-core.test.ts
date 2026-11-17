import {it,expect} from 'vitest';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,realpathSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname,join,resolve} from 'node:path';

it('the real MCP transport exposes the same core and preserves an unknown request across restart',async()=>{
  const root=realpathSync(mkdtempSync(join(tmpdir(),'native-mcp-')));
  const python=join(root,'fixture-host'),record=join(root,'observed.json');
  // Explicit transport fixture, not a native identity or a production decision.
  writeFileSync(python,`#!${process.execPath}
const fs=require('node:fs');const a=process.argv.slice(2);const action=a[3];
if(action==='identity'){console.log(JSON.stringify({schema:'harness.native_identity/v1',principal:{actor:'fixture',session:'fixture-session',surface:'fixture'}}));}
else {let s='';process.stdin.on('data',c=>s+=c);process.stdin.on('end',()=>{
const v=JSON.parse(s);let old;try{old=JSON.parse(fs.readFileSync(${JSON.stringify(record)},'utf8'))}catch{}
if(!old){fs.writeFileSync(${JSON.stringify(record)},JSON.stringify(v));process.stdout.write('lost result');}
else {console.log(JSON.stringify({status:'local_committed',request_id:v.request_id,original:old.request_id,same:JSON.stringify(v)===JSON.stringify(old)}));}
});}
`,{mode:0o700});
  let client:Client|undefined;
  async function connect(){
    const c=new Client({name:'isolated-protocol-test',version:'1'});
    await c.connect(new StdioClientTransport({command:process.execPath,
      args:[resolve('runtime/cc-work-item-core.mjs')],env:{HOME:root,PULSE_STATS_ACCOUNT_HOME:root,
        PULSE_WORK_ITEM_CORE_HOST:JSON.stringify({python,entrypoint:join(root,'fixture.pyz')}),
        PULSE_CORE_REQUEST_STATE_ROOT:join(root,'requests')}}));return c;
  }
  try {
    client=await connect();
    const listed=await client.listTools();
    expect(listed.tools.map(t=>t.name)).toEqual(['pulse_work_item','pulse_jobs']);
    expect(JSON.stringify(listed)).not.toContain('scl_intent');
    const args={action:'execute',work_item_id:'wi_fixture',intent:'checkpoint',input:{summary:'private transport fixture'}};
    const first=await client.callTool({name:'pulse_work_item',arguments:args});
    expect(JSON.stringify(first)).toContain('unknown');
    const original=JSON.parse(readFileSync(record,'utf8'));
    await client.close();client=await connect();
    const second=await client.callTool({name:'pulse_work_item',arguments:args});
    expect(JSON.stringify(second)).toContain('local_committed');
    expect(JSON.stringify(second)).toContain(original.request_id);
    expect(JSON.stringify(second)).toContain('"same":true');
  } finally {await client?.close();rmSync(root,{recursive:true,force:true});}
},15000);

it('lists and answers the read-only pulse_stats query where the Stats release is installed',async()=>{
  const root=realpathSync(mkdtempSync(join(tmpdir(),'native-mcp-stats-')));
  const python=join(root,'.local/share/coevolution/python-env/bin/python');
  const script=join(root,'.local/share/pulse/stats/current/Stats/exporter/pulse-stats-query.py');
  mkdirSync(dirname(python),{recursive:true});mkdirSync(dirname(script),{recursive:true});
  writeFileSync(script,'# fixture\n');
  // Fixture interpreter echoes the argv and environment it was given.
  writeFileSync(python,`#!${process.execPath}
console.log(JSON.stringify({schema:'pulse_stats_query/v1',status:'ok',errors:[],
  result:{argv:process.argv.slice(2),home:process.env.HOME,relay:Object.keys(process.env).filter(k=>k.startsWith('PULSE_'))}}));
`,{mode:0o700});
  const client=new Client({name:'isolated-stats-test',version:'1'});
  try {
    await client.connect(new StdioClientTransport({command:process.execPath,
      args:[resolve('runtime/cc-work-item-core.mjs')],env:{HOME:join(root,'agent-home'),PULSE_STATS_ACCOUNT_HOME:root,
        PULSE_WORK_ITEM_CORE_HOST:JSON.stringify({python:join(root,'none'),entrypoint:join(root,'none.pyz')}),
        PULSE_CORE_REQUEST_STATE_ROOT:join(root,'requests')}}));
    const listed=await client.listTools();
    expect(listed.tools.map(t=>t.name)).toEqual(['pulse_work_item','pulse_stats','pulse_jobs']);
    const answer=await client.callTool({name:'pulse_stats',arguments:{query:'cost',period:'week',group:'agent'}});
    expect(answer.isError).toBe(false);
    const value=JSON.parse((answer.content as any)[0].text);
    expect(value.result.argv).toEqual(['-I','-B',script,'cost','--period','week','--group','agent','--json']);
    expect(value.result.home).toBe(root);
    expect(value.result.relay).toEqual([]);
    const rejected=await client.callTool({name:'pulse_stats',arguments:{query:'cost',path:'/etc/passwd'}});
    expect(rejected.isError).toBe(true);
    expect(JSON.stringify(rejected)).toContain('Unknown pulse_stats argument');
  } finally {await client.close();rmSync(root,{recursive:true,force:true});}
},15000);
