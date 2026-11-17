import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {mkdirSync,mkdtempSync,realpathSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {isolateHumanDecisions} from './helpers/scl-human-decision.js';
import {handleFeishuMessage,__resetFeishuInboundDedupeForTests,type MessageHandlerContext} from '../src/channels/feishu.js';
import {latestHumanInput} from '../src/scl-human-decision.js';
import {feishuRecoveryBoundaryToken} from '../src/feishu-inbound-recovery.js';
import type {AgentConfig,PersistedSession,SpawnConfig,UserConfig} from '../src/types.js';

isolateHumanDecisions();
const roots:string[]=[];
beforeEach(()=>{
  __resetFeishuInboundDedupeForTests();
  vi.spyOn(console,'log').mockImplementation(()=>{});
  vi.spyOn(console,'warn').mockImplementation(()=>{});
  vi.spyOn(console,'error').mockImplementation(()=>{});
});
afterEach(()=>{for(const root of roots.splice(0))rmSync(root,{recursive:true,force:true});vi.restoreAllMocks();});
function deferred<T>(){
  let resolve!:(value:T)=>void;
  const promise=new Promise<T>(yes=>{resolve=yes;});
  return {promise,resolve};
}

/** Process and platform I/O are controlled here so races happen at actual
 * handler awaits on an ordinary retained conversation. */
function fixture(cold=false){
  const root=realpathSync(mkdtempSync(join(tmpdir(),'feishu-dispatch-fence-')));roots.push(root);
  const poolKey='human:worker',agentId='worker',sessionId='original-native';
  const workspace=join(root,'workspace');mkdirSync(workspace);
  const session:PersistedSession={poolKey,agentId,sessionId,runtime:'pi',status:'active',turnCount:1,maxTurns:100,lastActivity:1};
  const processInfo={runtime:'pi' as const,state:'ready' as const,sessionId,workspace};
  let current:typeof processInfo|undefined=cold?undefined:processInfo;
  const pool={get:vi.fn(()=>current),spawn:vi.fn(async(_config:SpawnConfig)=>{current=processInfo;return processInfo;}),
    send:vi.fn(async()=>{}),kill:vi.fn(async()=>{}),abortTurn:vi.fn(async()=>{})};
  const agent:AgentConfig={id:agentId,name:'Worker',emoji:'W',workspace,runtime:'pi',
    feishu:{appId:'cli_fixture',appSecret:'fixture-not-a-secret',sessionId}};
  const user:UserConfig={userId:'human',feishuOpenId:'ou_fixture',defaultAgent:agentId,agents:[agent]};
  const typing=new Set<string>();
  const ctx={agent,pool,router:{getAgents:()=>[agent],getAllUsers:()=>[user],getActiveAgent:()=>agent,
    getAgentChannel:()=> 'feishu',resolveByFeishuAgent:()=>({user,agent}),poolKey:()=>poolKey},
    stateManager:{getSession:()=>session,updateSession:vi.fn((_key:string,patch:Partial<PersistedSession>)=>Object.assign(session,patch)),flush:vi.fn()},
    poolKeyToOpenId:new Map([[poolKey,'ou_fixture']]),configPath:join(root,'config.json'),
    sendTextMessage:vi.fn(async()=>{}),sendCardMessage:vi.fn(async()=>{}),
    startTyping:vi.fn((messageId:string)=>{typing.add(messageId);}),
    stopTyping:vi.fn((messageId?:string)=>{if(messageId)typing.delete(messageId);else typing.clear();}),
    fetchReplyText:vi.fn(async()=> 'quoted original'),downloadImage:vi.fn(),
    downloadFileResource:vi.fn(async()=>({buffer:Buffer.from('media fixture'),contentType:'text/plain'})),
    transcribeInboundAudio:vi.fn(async()=> 'transcribed original'),larkClient:{request:vi.fn()}};
  const message=(id:string,text:string,extra:Record<string,unknown>={})=>({sender:{sender_id:{open_id:'ou_fixture'}},
    message:{message_id:id,message_type:'text',content:JSON.stringify({text}),...extra}});
  const handle=(data:ReturnType<typeof message>)=>handleFeishuMessage(ctx as unknown as MessageHandlerContext,data);
  return {root,poolKey,sessionId,workspace,session,pool,ctx,processInfo,message,handle,typing,
    setCurrent:(value:typeof processInfo|undefined)=>{current=value;},current:()=>current};
}

describe('Feishu final business dispatch fences',()=>{
  it('withholds an older message when Stop arrives during successful cold startup',async()=>{
    const f=fixture(true),started=deferred<void>(),finish=deferred<void>();
    f.pool.spawn.mockImplementationOnce(async()=>{started.resolve();await finish.promise;f.setCurrent(f.processInfo);return f.processInfo;});
    const original=f.handle(f.message('old-cold','old business'));await started.promise;
    await f.handle(f.message('stop-cold','/stop'));const stopBoundary=feishuRecoveryBoundaryToken(f.poolKey);
    finish.resolve();await original;
    expect(f.pool.spawn).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({runtime:'pi',sessionId:f.sessionId,
      workspace:f.workspace}));
    expect(f.pool.send).not.toHaveBeenCalled();expect(f.pool.kill).not.toHaveBeenCalled();
    expect(feishuRecoveryBoundaryToken(f.poolKey)).toBe(stopBoundary);
    expect(f.ctx.sendTextMessage).toHaveBeenCalledTimes(1);
  });
  it('delivers the newer message while withholding an older successful cold-start continuation',async()=>{
    const f=fixture(true),started=deferred<void>(),finish=deferred<void>();
    f.pool.spawn.mockImplementationOnce(async()=>{started.resolve();await finish.promise;return f.processInfo;});
    const original=f.handle(f.message('old-cold','old business'));await started.promise;
    // The manager has published the ready child; the earlier caller has not
    // yet returned from its spawn await.
    f.setCurrent(f.processInfo);await f.handle(f.message('new-cold','new business'));
    const latest=latestHumanInput(f.poolKey),boundary=feishuRecoveryBoundaryToken(f.poolKey);
    finish.resolve();await original;
    expect(f.pool.spawn).toHaveBeenCalledTimes(1);expect(f.pool.send).toHaveBeenCalledTimes(1);
    expect(f.pool.send).toHaveBeenCalledWith(f.poolKey,expect.objectContaining({content:expect.stringContaining('new business')}));
    expect(JSON.stringify(f.pool.send.mock.calls)).not.toContain('old business');
    expect(latestHumanInput(f.poolKey)).toBe(latest);expect(feishuRecoveryBoundaryToken(f.poolKey)).toBe(boundary);
    expect(f.pool.kill).not.toHaveBeenCalled();
  });
  it.each(['stop','new-input','session','process'] as const)(
    'checks %s again after text reply enrichment finishes',async change=>{
      const f=fixture(),started=deferred<void>(),finish=deferred<string>();
      f.ctx.fetchReplyText.mockImplementationOnce(async()=>{started.resolve();return finish.promise;});
      const original=f.handle(f.message('old-quoted','old quoted business',{parent_id:'quoted-parent'}));await started.promise;
      expect(f.pool.spawn).not.toHaveBeenCalled();
      if(change==='stop')await f.handle(f.message('stop-quoted','/stop'));
      if(change==='new-input')await f.handle(f.message('new-quoted','new unquoted business'));
      if(change==='session')f.session.sessionId='replacement-session';
      if(change==='process')f.setCurrent({...f.processInfo});
      const latest=latestHumanInput(f.poolKey),boundary=feishuRecoveryBoundaryToken(f.poolKey);
      finish.resolve('original quote');await original;
      expect(f.pool.send).toHaveBeenCalledTimes(change==='new-input'?1:0);
      if(change==='new-input')expect(f.pool.send).toHaveBeenCalledWith(f.poolKey,
        expect.objectContaining({content:expect.stringContaining('new unquoted business')}));
      expect(JSON.stringify(f.pool.send.mock.calls)).not.toContain('old quoted business');
      expect(latestHumanInput(f.poolKey)).toBe(latest);expect(feishuRecoveryBoundaryToken(f.poolKey)).toBe(boundary);
      expect(f.pool.kill).not.toHaveBeenCalled();
      expect(f.ctx.stopTyping).toHaveBeenCalledExactlyOnceWith('old-quoted');
      expect([...f.typing]).toEqual(change==='new-input'?['new-quoted']:[]);
    });
  it.each(['stop','session','process'] as const)('checks %s after a file download finishes',async change=>{
    const f=fixture(),started=deferred<void>(),finish=deferred<{buffer:Buffer;contentType:string}>();
    f.ctx.downloadFileResource.mockImplementationOnce(async()=>{started.resolve();return finish.promise;});
    const original=f.handle(f.message('old-file','',{message_type:'file',content:JSON.stringify({file_key:'file_fixture',file_name:'old.txt'})}));
    await started.promise;
    if(change==='stop')await f.handle(f.message('stop-file','/stop'));
    if(change==='session')f.session.sessionId='replacement-session';
    if(change==='process')f.setCurrent({...f.processInfo});
    finish.resolve({buffer:Buffer.from('old file business'),contentType:'text/plain'});await original;
    expect(f.pool.send).not.toHaveBeenCalled();expect(f.pool.kill).not.toHaveBeenCalled();
  });
  it('honors Stop during audio transcription after download and admission already succeeded',async()=>{
    const f=fixture(),started=deferred<void>(),finish=deferred<string>();
    f.ctx.transcribeInboundAudio.mockImplementationOnce(async()=>{started.resolve();return finish.promise;});
    const original=f.handle(f.message('old-audio','',{message_type:'audio',content:JSON.stringify({file_key:'audio_fixture'})}));
    await started.promise;expect(f.ctx.downloadFileResource).toHaveBeenCalledTimes(1);
    await f.handle(f.message('stop-audio','/stop'));finish.resolve('old spoken business');await original;
    expect(f.pool.send).not.toHaveBeenCalled();expect(f.pool.kill).not.toHaveBeenCalled();
  });
  it('still dispatches an unchanged admitted consumer after enrichment',async()=>{
    const f=fixture();await f.handle(f.message('current-quoted','current business',{parent_id:'current-parent'}));
    expect(f.ctx.fetchReplyText).toHaveBeenCalledExactlyOnceWith('current-parent');
    expect(f.pool.send).toHaveBeenCalledExactlyOnceWith(f.poolKey,expect.objectContaining({content:expect.stringContaining('current business')}));
    expect(f.pool.spawn).not.toHaveBeenCalled();
  });
});
