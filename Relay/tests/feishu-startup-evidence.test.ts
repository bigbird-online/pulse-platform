import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {mkdtempSync,rmSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {platformProvesStartupRejection,readFeishuRecoveryHistory} from '../src/feishu-inbound-recovery.js';

describe('Feishu startup-failure evidence',()=>{
  let root:string;
  const ref='a'.repeat(64),session='original-native';
  const source={channel:'feishu' as const,eventId:'original-input',humanRef:'human',message:'按规则重试',observedAtMs:1000};
  beforeEach(()=>{root=mkdtempSync(join(tmpdir(),'feishu-startup-evidence-'));});
  afterEach(()=>{rmSync(root,{recursive:true,force:true});});

  it('requires complete original history and rejects another native identity',()=>{
    const path=join(root,'session.jsonl');writeFileSync(path,JSON.stringify({type:'session',id:session}));
    expect(()=>readFeishuRecoveryHistory(path,session,ref)).toThrow();
    writeFileSync(path,JSON.stringify({type:'session',id:'other'})+'\n');
    expect(()=>readFeishuRecoveryHistory(path,session,ref)).toThrow();
  });

  it('reports acceptance and the recovery marker only from user text in the original history',()=>{
    const path=join(root,'session.jsonl');
    const user=(text:string)=>JSON.stringify({type:'message',message:{role:'user',content:[{type:'text',text}]}});
    writeFileSync(path,[JSON.stringify({type:'session',id:session}),
      user(`[Host human input reference: ${ref}.]`),user('MARKER-1')].join('\n')+'\n');
    expect(readFeishuRecoveryHistory(path,session,ref,'MARKER-1')).toEqual({accepted:true,marked:true});
    expect(readFeishuRecoveryHistory(path,session,'b'.repeat(64),'MARKER-2')).toEqual({accepted:false,marked:false});
  });

  function platform(){
    const original={message_id:source.eventId,chat_id:'original-chat',create_time:'1000',msg_type:'text',
      sender:{sender_type:'user',id:'original-user'},body:{content:JSON.stringify({text:source.message})}};
    const failure={message_id:'failed-reply',create_time:'2000',msg_type:'text',sender:{sender_type:'app',id:'original-app'},
      body:{content:JSON.stringify({text:'本次消息未派发：运行会话启动失败 [runtime_start_failed]。'})}};
    const reader={get:vi.fn().mockResolvedValue({code:0,data:{items:[original]}}),
      list:vi.fn().mockResolvedValue({code:0,data:{items:[original,failure],has_more:false}})};
    return {failure,reader,run:()=>platformProvesStartupRejection(reader,source,'original-user','original-app')};
  }
  it('proves an exact original startup rejection from the complete official chat window',async()=>{
    expect(await platform().run()).toBe(true);
  });
  it('does not treat any other platform reply as a startup rejection',async()=>{
    const f=platform();f.failure.body.content=JSON.stringify({text:'已回复'});
    expect(await f.run()).toBe(false);
  });
});
