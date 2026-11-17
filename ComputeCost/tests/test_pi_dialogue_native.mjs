import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn,execFileSync} from 'node:child_process';
import {mkdtemp,mkdir,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createInterface} from 'node:readline';
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';

const executable=process.env.PULSE_PI_NATIVE_TEST_BIN;
test('pinned native Pi keeps RPC input alive under hung metering and enforces the configured tools',
  {skip:!executable,timeout:20000},async()=>{
  assert.match(execFileSync(executable,['--version'],{encoding:'utf8'}),/0\.84\.2/);
  const root=await mkdtemp(join(tmpdir(),'pulse-native-dialogue-'));
  const home=join(root,'home'),agent=join(root,'agent'),sessions=join(root,'sessions');
  await Promise.all([home,agent,sessions].map(path=>mkdir(path,{mode:0o700})));
  const extension=join(root,'probe.mjs');
  const lifecycle=fileURLToPath(new URL('../runtime/pi-usage-binding.mjs',import.meta.url));
  await writeFile(extension,`
import {installPiUsageLifecycle} from ${JSON.stringify(lifecycle)};
export default function(pi){
 let release;const captured=new Promise(resolve=>{release=resolve;});
 installPiUsageLifecycle(pi,{captureSnapshot:()=>captured});
 pi.registerCommand('inspect-dialogue-guard',{handler:async(_,ctx)=>{
  ctx.ui.notify('GUARD_TEST '+JSON.stringify({active:pi.getActiveTools(),all:pi.getAllTools().map(t=>t.name)}),'info');
 }});
 pi.registerCommand('resolve-test-baseline',{handler:async(_,ctx)=>{
  release({schema:'pi_host_usage_capture_receipt/v1',lifecycle_state:'open',idempotent_replay:true});
  await new Promise(resolve=>setImmediate(resolve));
  ctx.ui.notify('GUARD_TEST '+JSON.stringify({active:pi.getActiveTools(),all:pi.getAllTools().map(t=>t.name)}),'info');
 }});
}
`,{mode:0o600});
  const child=spawn(executable,['--mode','rpc','--no-builtin-tools','--no-extensions','--no-skills',
    '--no-themes','--no-prompt-templates','--no-context-files','--session-dir',sessions,
    '--session-id',randomUUID(),'--extension',extension],{cwd:root,env:{PATH:process.env.PATH,
      HOME:home,PI_CODING_AGENT_DIR:agent,PULSE_PI_MEASUREMENT_TOOLS:JSON.stringify(['read','bash'])},
      stdio:['pipe','pipe','pipe']});
  const pending=new Map(),reports=[];let stderr='';
  child.stderr.on('data',bytes=>{stderr=(stderr+bytes.toString()).slice(-2000);});
  const reader=createInterface({input:child.stdout});
  reader.on('line',line=>{
    let value;try{value=JSON.parse(line);}catch{return;}
    if(value.type==='extension_ui_request'&&value.method==='notify'&&value.message?.startsWith('GUARD_TEST '))
      reports.push(JSON.parse(value.message.slice(11)));
    if(value.type==='response'&&pending.has(value.id))pending.get(value.id)(value);
  });
  const rpc=command=>new Promise((resolve,reject)=>{
    const id=randomUUID();const timer=setTimeout(()=>{pending.delete(id);reject(new Error('native RPC failed to settle: '+stderr));},5000);
    pending.set(id,value=>{clearTimeout(timer);pending.delete(id);value.success?resolve(value):reject(new Error(JSON.stringify(value)));});
    child.stdin.write(JSON.stringify({...command,id})+'\n');
  });
  try{
    await rpc({type:'get_state'});
    await rpc({type:'prompt',message:'/inspect-dialogue-guard'});
    await rpc({type:'prompt',message:'/inspect-dialogue-guard'});
    console.log('native tool registry',JSON.stringify(reports));
    assert.deepEqual(reports.map(r=>r.active),[['read'],['read']]);
    await rpc({type:'prompt',message:'/resolve-test-baseline'});
    assert.deepEqual(reports.at(-1).active,['read','bash']);
  }finally{
    const closed=new Promise(resolve=>child.once('close',resolve));child.kill('SIGTERM');
    const timer=setTimeout(()=>child.kill('SIGKILL'),1000);await closed;clearTimeout(timer);reader.close();
    await rm(root,{recursive:true,force:true});
  }
});
