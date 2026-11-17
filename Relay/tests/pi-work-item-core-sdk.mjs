// Run with the operator-selected installed Pi SDK directory. No network/auth use.
import assert from 'node:assert/strict';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {installPiWorkItemCore} from '../runtime/pi-work-item-core.mjs';

const sdk=resolve(process.argv[2]);
const {createAgentSession,DefaultResourceLoader,SessionManager,SettingsManager,ModelRuntime} =
  await import(pathToFileURL(join(sdk,'dist/index.js')));
const {fauxProvider,fauxAssistantMessage,InMemoryCredentialStore} =
  await import(pathToFileURL(join(sdk,'node_modules/@earendil-works/pi-ai/dist/index.js')));
async function scenario(warmup) {
const directory=mkdtempSync(join(tmpdir(),'native-command-pi-sdk-'));
let session;
try {
  const models=await ModelRuntime.create({credentials:new InMemoryCredentialStore(),
    modelsPath:null,modelsStorePath:join(directory,'models.db'),refreshOnCreate:false,allowModelNetwork:false});
  const faux=fauxProvider({tokensPerSecond:100000});
  models.registerNativeProvider(faux.provider);
  faux.setResponses([...(warmup ? [fauxAssistantMessage('Synthetic warmup')] : []),
    fauxAssistantMessage('Native command result'),fauxAssistantMessage('Ordinary turn')]);
  const manager=SessionManager.create(directory,join(directory,'sessions'));
  const settings=SettingsManager.inMemory({compaction:{enabled:false},retry:{enabled:false},
    cacheWarming:{enabled:false}});
  let calls=0, persisted=false;
  const ref='a'.repeat(64);
  const loader=new DefaultResourceLoader({cwd:directory,agentDir:directory,settingsManager:settings,
    noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true,
    extensionFactories:[pi=>installPiWorkItemCore(pi,{python:'fixture',entrypoint:'fixture'},async(_host,input)=>{
      calls++;assert.equal(input,ref);
      const rows=readFileSync(manager.getSessionFile(),'utf8').trim().split('\n').map(JSON.parse);
      persisted=rows.some(row=>row.message?.role==='user' &&
        JSON.stringify(row.message.content).includes('Host human input reference: '+ref+'.'));
      assert.equal(persisted,true,'Native user message must be on disk before command dispatch');
      return {status:'read_only',channel_command:{backend:'native_core',input_ref:ref}};
    })]});
  await loader.reload();
  ({session}=await createAgentSession({cwd:directory,agentDir:directory,modelRuntime:models,
    model:faux.getModel(),sessionManager:manager,settingsManager:settings,resourceLoader:loader,tools:['read']}));
  await session.bindExtensions({});
  // A new Feishu Session may begin with /wi, before Pi has written any log.
  if (warmup) await session.prompt('Synthetic existing conversation');
  const toolsBefore=session.getActiveToolNames();
  await session.prompt('/wi list\n\n[Host human input reference: '+ref+'.]');
  assert.equal(calls,1);
  // Pi isolates extension errors, so check the dispatch-time observation here.
  assert.equal(persisted,true,'Native user message must be on disk before command dispatch');
  assert.deepEqual(session.getActiveToolNames(),toolsBefore,'Ordinary tools must return after slash result');
  await session.prompt('Ordinary input');
  assert.equal(calls,1,'A historical command must not replay on the next turn');
  assert.equal(faux.state.callCount,warmup ? 3 : 2);
  const rows=readFileSync(manager.getSessionFile(),'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(rows.filter(row=>row.type==='session').length,1,'Pi must keep one Session header');
  assert.equal(rows.filter(row=>row.message?.role==='assistant').length,warmup ? 3 : 2);
  return {warmup,persistedBeforeDispatch:persisted,commandInvocations:calls};
} finally { session?.dispose();rmSync(directory,{recursive:true,force:true}); }
}

const scenarios=[await scenario(true),await scenario(false)];
console.log(JSON.stringify({sdk,realSdk:true,syntheticProvider:true,scenarios,
  ordinaryToolsRestored:true,networkRequests:0}));
