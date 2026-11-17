/** Isolated real Pi/Relay/core protocol test. No provider credentials or production facts. */
import {createServer} from 'node:http';
import {mkdtempSync,realpathSync,mkdirSync,writeFileSync,readFileSync,existsSync,readdirSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {resolve,join,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFileSync,execFile} from 'node:child_process';
import {randomUUID,createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const [python,pi,runtimeId='pi']=process.argv.slice(2);
assert(['pi','pi-standalone','codex','cc-sdk','cc'].includes(runtimeId));
assert(python&&pi,'Pass canonical Python and the installed Pi CLI; no production configuration is read');
const relay=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const root=realpathSync(mkdtempSync(join(tmpdir(),'wi018-native-rollover-')));
const dirs=Object.fromEntries(['workspace','sessions','config','data','human','views','delivery','dispatch','codex'].map(k=>{
 const p=join(root,k);mkdirSync(p,{mode:0o700});return [k,p];}));
const entry=join(root,'harness.pyz');
execFileSync(python,['-I','-B','-c',String.raw`import pathlib,sys,zipfile
source=pathlib.Path(sys.argv[1]);out=pathlib.Path(sys.argv[2])
with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as z:
 for p in sorted((source/'harness_rewrite').glob('*.py')):z.write(p,'harness_rewrite/'+p.name)
 z.writestr('__main__.py','from harness_rewrite.cli import main\nraise SystemExit(main())\n')
`,resolve(relay,'../agent-harness'),entry]);
const extension=join(relay,'runtime/pi-work-item-core.mjs');
const host={python,entrypoint:entry,extension};
const node=realpathSync(process.execPath);
const canonicalPi=realpathSync(pi);
const codexBinary=join(relay,'node_modules/.bin/codex');
const ccBinary=join(relay,'node_modules/@anthropic-ai/claude-agent-sdk-'+process.platform+'-'+process.arch+'/claude');
const ccRoot=join(root,'.claude/projects');mkdirSync(ccRoot,{recursive:true,mode:0o700});
mkdirSync(join(dirs.codex,'sessions'),{mode:0o700});
const policy={namespace:'wi018-integration-fixture',surface:'isolated-local',database:join(root,'facts.sqlite'),
 pi_binary:canonicalPi,node,producer_node:node,relay_entry:fileURLToPath(import.meta.url),
 python,human_inputs:dirs.human,views_root:dirs.views,delivery_root:dirs.delivery,dispatch_root:dirs.dispatch,
 verification_actor:'isolated-verifier',profiles:[{actor:'isolated-producer',role:'producer',session_root:dirs.sessions,
 artifact_roots:[dirs.workspace],binding_root:join(dirs.data,'native-session-proofs')}]};
if(runtimeId==='codex')policy.profiles.push({actor:'isolated-producer',role:'producer',runtime:'codex',executable:realpathSync(codexBinary),session_root:join(dirs.codex,'sessions'),artifact_roots:[dirs.workspace],workspace_roots:[dirs.workspace],binding_root:join(dirs.data,'native-session-proofs')});
if(runtimeId.startsWith('cc'))policy.profiles.push({actor:'isolated-producer',role:'producer',runtime:'cc',executable:realpathSync(ccBinary),session_root:ccRoot,artifact_roots:[dirs.workspace],workspace_roots:[dirs.workspace],binding_root:join(dirs.data,'native-session-proofs')});
if(runtimeId==='pi-standalone'){policy.relay_entry=join(relay,'dist/cli.js');Object.assign(policy.profiles[0],{standalone:true,workspace_roots:[dirs.workspace]});}
writeFileSync(join(root,'host-policy.json'),JSON.stringify(policy),{mode:0o600});
let wi;let requests=0;const toolResults=[];let serverError;
const server=createServer(async(req,res)=>{try{
 let raw='';for await(const c of req)raw+=c;if(raw.length>2e6)throw Error('fixture request bound');
 if(req.method!=='POST'||!raw){res.writeHead(404,{'Content-Type':'application/json'});res.end(JSON.stringify({error:{type:'not_found_error',message:'Isolated fixture only serves model requests'}}));return;}
 const body=JSON.parse(raw);if(!Array.isArray(body.messages??body.input)){res.writeHead(404,{'Content-Type':'application/json'});res.end('{}');return;}
 assert(++requests<=14,'fixture model exceeded its finite plan');
 const messages=body.messages??body.input;
 const userIndex=messages.findLastIndex(m=>m.role==='user'&&!(Array.isArray(m.content)&&m.content.every(c=>c.type==='tool_result')));
 const text=JSON.stringify(messages[userIndex]?.content);
 const prior=messages.slice(userIndex+1).flatMap(m=>m.role==='tool'||m.type==='function_call_output'?[m]:m.role==='user'&&Array.isArray(m.content)?m.content.filter(c=>c.type==='tool_result'):[]);
 for(const m of prior){const value=m.content??m.output;const t=typeof value==='string'?value:Array.isArray(value)?value.map(v=>v.text??'').join('\n'):JSON.stringify(value);let line=t.split('\n').find(l=>l.startsWith('{'));if(line){let result=JSON.parse(line);if(result.content){const text=result.content.find(c=>c.type==='text')?.text;line=text?.split('\n').find(l=>l.startsWith('{'));if(!line)continue;result=JSON.parse(line);}if(result.work_item?.id)wi=result.work_item.id;toolResults.push(result);assert(!['rejected','unknown'].includes(result.status),JSON.stringify(result));}}
 const continued=text.includes('Host session continuation');
 let args;
 if(!continued){args=prior.length===0?{action:'execute',intent:'create_lightweight',input:{why:'isolated process verification',what:'original fixture work',acceptance:['same WI after rollover'],owner:'isolated-producer',resources:['repo']}}:
 prior.length===1?{action:'execute',work_item_id:wi,intent:'start_work',input:{}}:
 prior.length===2?{action:'execute',work_item_id:wi,intent:'checkpoint',input:{summary:'original source checkpoint',resources:['repo']}}:undefined;}
 else {args=prior.length===0?{action:'resume',work_item_id:wi}:
 prior.length===1?{action:'execute',work_item_id:wi,intent:'checkpoint',input:{summary:'actual successor checkpoint',resources:['repo']}}:undefined;}
 res.writeHead(200,{'Content-Type':'text/event-stream'});
 if(runtimeId.startsWith('cc')){
   const tool=body.tools?.find(t=>t.name?.includes('pulse_work_item'));
   if(!tool)args=undefined;
   const id='msg_fixture_'+requests;const block=args?{type:'tool_use',id:'tool_'+requests,name:tool.name,input:{}}:{type:'text',text:''};
   const event=(type,value)=>res.write('event: '+type+'\ndata: '+JSON.stringify({type,...value})+'\n\n');
   event('message_start',{message:{id,type:'message',role:'assistant',model:body.model,content:[],stop_reason:null,stop_sequence:null,usage:{input_tokens:1,output_tokens:0}}});
   event('content_block_start',{index:0,content_block:block});
   event('content_block_delta',{index:0,delta:args?{type:'input_json_delta',partial_json:JSON.stringify(args)}:{type:'text_delta',text:'Isolated native process step completed.'}});
   event('content_block_stop',{index:0});
   event('message_delta',{delta:{stop_reason:args?'tool_use':'end_turn',stop_sequence:null},usage:{output_tokens:1}});
   event('message_stop',{});res.end();return;
 }
 if(runtimeId==='codex'){
   const tool=body.tools.flatMap(t=>t.type==='namespace'?t.tools.map(v=>({...v,namespace:t.name})):[t]).find(t=>t.name?.includes('pulse_work_item')); assert(tool,'Actual Codex tool inventory: '+JSON.stringify(body.tools.map(t=>({type:t.type,name:t.name,tools:t.tools?.map(v=>v.name)}))));
   const id='resp_fixture_'+requests;const call='call_fixture_'+requests;
   const item=args?{id:call,type:'function_call',call_id:call,name:tool.name,...(tool.namespace?{namespace:tool.namespace}:{}),arguments:JSON.stringify(args),status:'completed'}:
     {id:'msg_'+requests,type:'message',role:'assistant',status:'completed',content:[{type:'output_text',text:'Isolated native process step completed.',annotations:[]}]};
   const event=(type,extra)=>res.write('event: '+type+'\ndata: '+JSON.stringify({type,...extra})+'\n\n');
   event('response.created',{response:{id,object:'response',status:'in_progress',output:[]}});
   event('response.output_item.added',{output_index:0,item:{...item,status:'in_progress',...(args?{arguments:''}:{content:[]})}});
   if(args)event('response.function_call_arguments.delta',{item_id:call,output_index:0,delta:JSON.stringify(args)});
   else {event('response.content_part.added',{item_id:item.id,output_index:0,content_index:0,part:{type:'output_text',text:'',annotations:[]}});event('response.output_text.delta',{item_id:item.id,output_index:0,content_index:0,delta:item.content[0].text});}
   event('response.output_item.done',{output_index:0,item});
   event('response.completed',{response:{id,object:'response',status:'completed',output:[item],usage:{input_tokens:1,output_tokens:1,total_tokens:2}}});
   res.end();return;
 }

 const event=(delta,finish_reason=null)=>res.write('data: '+JSON.stringify({id:'fixture-'+requests,object:'chat.completion.chunk',created:1,model:'fixture',choices:[{index:0,delta,finish_reason}]})+'\n\n');
 event({role:'assistant'});
 if(args)event({tool_calls:[{index:0,id:'call-'+requests,type:'function',function:{name:'pulse_work_item',arguments:JSON.stringify(args)}}]});
 else event({content:'Isolated native process step completed.'});
 event({},args?'tool_calls':'stop');res.end('data: [DONE]\n\n');
}catch(error){serverError=String(error);if(!res.headersSent)res.writeHead(500);res.end('fixture plan failed');}});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const endpoint='http://127.0.0.1:'+server.address().port+'/v1';
if(runtimeId.startsWith('cc')){
 process.env.HOME=root;process.env.CLAUDE_CONFIG_DIR=join(root,'.claude');
 for(const key of ['CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN'])delete process.env[key];
 process.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1';process.env.DISABLE_AUTOUPDATER='1';
 process.env.CLAUDE_BIN=ccBinary;process.env.ANTHROPIC_BASE_URL=endpoint.slice(0,-3);process.env.ANTHROPIC_AUTH_TOKEN='fixture-not-a-credential';
 for(const key of ['RELAY_CC_OAUTH_ENV_FILE','CLAUDE_CODE_OAUTH_TOKEN_FALLBACK'])delete process.env[key];
}
if(runtimeId==='codex'){
 process.env.CODEX_HOME=dirs.codex;process.env.HOME=root;process.env.PATH=dirname(node)+':/usr/bin:/bin';
 process.env.PULSE_CODEX_MODEL='fixture';process.env.PULSE_CODEX_REASONING_EFFORT='low';
 delete process.env.PULSE_COMPUTECOST_RECEIPT_DIR;delete process.env.OPENAI_API_KEY;delete process.env.OPENAI_BASE_URL;
 writeFileSync(join(dirs.codex,'config.toml'),'model="fixture"\nmodel_provider="fixture"\n[model_providers.fixture]\nname="fixture"\nbase_url='+JSON.stringify(endpoint)+'\nwire_api="responses"\nrequest_max_retries=0\nstream_max_retries=0\n');
}

writeFileSync(join(dirs.config,'auth.json'),'{}',{mode:0o600});
writeFileSync(join(dirs.config,'models.json'),JSON.stringify({providers:{'wi018-fixture':{baseUrl:endpoint,api:'openai-completions',apiKey:'fixture-not-a-credential',models:[{id:'fixture',name:'isolated',reasoning:false,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:32768,maxTokens:2048}]}}}),{mode:0o600});
if(runtimeId==='pi-standalone'){
 const runNative=arguments_=>new Promise((ok,fail)=>{const child=execFile(python,['-I','-B',entry,'native-run','pi','--',
  '--print','--mode','json','--provider','wi018-fixture','--model','fixture','--thinking','off','--session-dir',dirs.sessions,...arguments_],
  {cwd:dirs.workspace,env:{PATH:dirname(node)+':/usr/bin:/bin',HOME:root,PI_CODING_AGENT_DIR:dirs.config},timeout:60000,maxBuffer:4*1024*1024},
  (error,stdout,stderr)=>error?fail(new Error('Standalone fixture failed: '+stderr+' '+stdout.slice(-1200))):ok(stdout));child.stdin.end();});
 try{
  await runNative(['Run the isolated original standalone task.']);
  if(serverError)throw Error(serverError);assert(wi,'Standalone source did not create its fixture WI');
  const files=readdirSync(dirs.sessions).filter(name=>name.endsWith('.jsonl'));assert.equal(files.length,1);
  const source=JSON.parse(readFileSync(join(dirs.sessions,files[0]),'utf8').split('\n')[0]).id;
  await runNative(['--session',join(dirs.sessions,files[0]),'Host session continuation: resume the same isolated standalone task.']);
  if(serverError)throw Error(serverError);
  assert(toolResults.some(r=>r.work_item?.generation===1),'Standalone original generation was not preserved');
  const result={isolated_fixture:true,actual_native:'Pi 0.87.1 standalone CLI',work_item:wi,source_session:source,
   same_session_resume:true,original_generation:1,model_requests:requests,production_business_mutation:false,human_uat:false};
  writeFileSync(join(root,'result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify({root,...result}));
 }finally{server.closeAllConnections();await new Promise(r=>server.close(r));}
 process.exit(0);
}
const manifest={schemaVersion:'relay_pi_runtime_manifest/v1',piVersion:'0.87.1',piCommit:'f07218c4d4bbc12bef056a7058c3dd49dfe41abe',workItemCoreHost:host,
 profiles:{fixture:{kind:'host-native',unattended:true,piBinary:pi,workspaceRoots:[dirs.workspace],
 nodeResourceRoots:[root,relay,dirname(node),dirname(realpathSync(python)),dirname(python),dirname(canonicalPi),'/usr/bin','/bin'],
 toolchainPaths:[dirname(node),'/usr/bin','/bin'],sessionRoot:dirs.sessions,envAllowlist:[],extensions:[],skills:[],contextFiles:[],agentConfigDir:dirs.config,workItemCoreHost:host}}};
const manifestFile=join(root,'manifest.json');writeFileSync(manifestFile,JSON.stringify(manifest));
const paths=join(root,'paths.json');writeFileSync(paths,JSON.stringify({schema:'pulse_relay_paths/v1',root,paths:{relayConfig:join(root,'relay.json'),cronConfig:join(root,'cron.json'),dataDir:dirs.data,piManifest:manifestFile}}));
process.env.PULSE_RELAY_PATHS=paths;
process.env.PULSE_SCL_SURFACE_ID='isolated-local';
// Explicit isolation: provider and native auth live under a disposable fixture root.
process.env.SCL_HUMAN_DECISION_KEY_FILE=join(dirs.human,'fixture.key');
const {createPiConversationRuntime}=await import('../dist/pi-conversation-runtime.js');
const {rolloverNativeSession,sessionControl}=await import('../dist/native-session-rollover.js');
const {createConversationRuntimeManager}=await import('../dist/conversation-runtime.js');
const runtime=createConversationRuntimeManager({env:{}});
if(runtimeId==='pi')runtime.register(createPiConversationRuntime({manifest,requestTimeoutMs:20000,turnTimeoutMs:40000,killGraceMs:1000}));
else if(runtimeId==='cc'){const {createPool}=await import('../dist/pool.js');const {createPoolConversationAdapter}=await import('../dist/conversation-runtime.js');runtime.register(createPoolConversationAdapter(createPool({ccMode:'print',killTimeoutMs:1000,maxRateLimitRetriesPerMessage:0})));}
else if(runtimeId==='cc-sdk'){const {createCcSdkConversationRuntime}=await import('../dist/cc-sdk-conversation-runtime.js');runtime.register(createCcSdkConversationRuntime({engineRoute:{baseUrl:endpoint.slice(0,-3),authToken:'fixture-not-a-credential'},firstChunkTimeoutMs:20000,stallRetries:0}));}
else {const {createCodexConversationRuntime}=await import('../dist/codex-conversation-runtime.js');runtime.register(createCodexConversationRuntime({codexBin:codexBinary,additionalWritableRoots:[],timeoutMs:20000,nativeSessionsDirectory:join(dirs.codex,'sessions')}));}
const poolKey='fixture:pulse';const source=randomUUID();
const agent={id:'pulse',name:'fixture',workspace:dirs.workspace,runtime:runtimeId,maxTurns:20,
 conversationRuntime:{runtime:'pi',isolationProfile:'fixture',model:{provider:'wi018-fixture',model:'fixture',reasoning:'off'}}};
if(runtimeId!=='pi')delete agent.conversationRuntime;
const state={poolKey,agentId:'pulse',runtime:runtimeId,sessionId:source,status:'active',lastActivity:1,turnCount:0,maxTurns:20};
const statePath=join(root,'channel-state.json');
const stateManager={getSession:()=>state,updateSession:(_k,p)=>Object.assign(state,p),flushStrict:()=>writeFileSync(statePath,JSON.stringify(state))};
const waitIdle=async()=>{for(let n=0;n<400;n++){if(serverError)throw Error(serverError);const p=runtime.get(poolKey);if(['idle','ready'].includes(p?.state))return;await new Promise(r=>setTimeout(r,100));}throw Error('fixture native turn timed out');};
let result;
try{
 await runtime.spawn({...agent,tenantId:poolKey,sessionId:source,resume:false});
 await runtime.send(poolKey,{type:'human',content:'Run the isolated fixture plan. No production work is requested.'});
 await waitIdle();assert(wi,'actual source did not create fixture WI');
 const command={channel:'telegram',eventId:'explicit-fixture-event',humanRef:'isolated-test-human',message:'/wi rollover '+wi};
 const ref=createHash('sha256').update(JSON.stringify([command.channel,command.eventId,command.humanRef,poolKey])).digest('hex');
 writeFileSync(join(dirs.human,ref+'.json'),JSON.stringify({source:command,poolKey}),{mode:0o600});
 const first=await rolloverNativeSession({poolKey,agent,runtime,stateManager,channel:'telegram',ref});
 assert(first.session_handover?.prepared,JSON.stringify(first));
 await waitIdle();
 result=await rolloverNativeSession({poolKey,agent,runtime,stateManager,channel:'telegram'});
 assert(result.session_handover?.accepted,JSON.stringify(result));
 assert.notEqual(state.sessionId,source);assert.equal(state.nativeSessionHistory[0].sessionId,source);
 assert(toolResults.some(r=>r.work_item?.generation===2),'actual successor did not use generation 2');
 const target=state.sessionId;const requestsBeforeDuplicate=requests;
 const again=await rolloverNativeSession({poolKey,agent,runtime,stateManager,channel:'telegram',ref});
 assert(again.session_handover.accepted);assert.equal(state.sessionId,target);assert.equal(requests,requestsBeforeDuplicate);
 result={isolated_fixture:true,actual_native:runtimeId==='pi'?'pi 0.87.1':runtimeId==='codex'?'installed Codex CLI':runtimeId==='cc'?'installed Claude CLI through Relay pool':'installed Claude SDK engine',source_session:source,target_session:target,
 work_item:wi,prepared:true,accepted:true,generation:2,duplicate_reused:true,provider:'loopback deterministic fixture',
 production_business_mutation:false,human_uat:false,model_requests:requests};
 writeFileSync(join(root,'result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify({root,...result}));
}finally{runtime.dispose();server.closeAllConnections();await new Promise(r=>server.close(r));}
