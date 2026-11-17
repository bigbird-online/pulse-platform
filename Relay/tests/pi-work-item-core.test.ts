import {describe,it,expect,vi} from 'vitest';
// @ts-expect-error Reviewed Pi extension is executable ESM, loaded by Pi directly.
import {installPiWorkItemCore,persistOriginalSession,invokeOperation,resultText,BRIEF_LIMIT,PAGE_SIZE,CORE_CALL_TIMEOUT_MS} from '../runtime/pi-work-item-core.mjs';
import {mkdtempSync,readFileSync,writeFileSync,rmSync,chmodSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

describe('native core call limits', () => {
  it('reads back a result larger than 8 MB and allows long writes to finish', async () => {
    const root = mkdtempSync(join(tmpdir(), 'core-call-'));
    try {
      const fake = join(root, 'python');
      // Stands in for the canonical python: ignores its argv and prints a 12 MB result.
      writeFileSync(fake, '#!/bin/sh\nexec node -e \'process.stdout.write(JSON.stringify({status:"read_only",pad:"x".repeat(12*1024*1024)}))\'\n');
      chmodSync(fake, 0o755);
      const result = await invokeOperation({python: fake, entrypoint: '/unused'}, {action: 'identity'});
      expect(result.status).toBe('read_only');
      expect(result.pad.length).toBe(12 * 1024 * 1024);
      expect(CORE_CALL_TIMEOUT_MS).toBeGreaterThanOrEqual(600_000);
    } finally { rmSync(root, {recursive: true, force: true}); }
  });
});

describe('intent input guides', () => {
  it('passes the guide intent to the core, which needs no WI for it', async () => {
    const root = mkdtempSync(join(tmpdir(), 'core-guide-'));
    try {
      const fake = join(root, 'python');
      writeFileSync(fake, '#!/bin/sh\nexec node -e \'process.stdout.write(JSON.stringify({status:"read_only",argv:process.argv.slice(1)}))\' -- "$@"\n');
      chmodSync(fake, 0o755);
      const result = await invokeOperation({python: fake, entrypoint: '/installed/harness.pyz'},
        {action: 'inspect', view: 'guide', intent: 'adopt_plan'});
      expect(result.argv.slice(-5)).toEqual(['inspect', '--view', 'guide', '--intent', 'adopt_plan']);
    } finally { rmSync(root, {recursive: true, force: true}); }
  });

  it('points a shape refusal at its guide within the brief and names a guide read', () => {
    const rejected = resultText({status:'rejected', request_id:'r-adopt', error:{code:'invalid_input',
      reason:'Unsupported business input field: '+'x'.repeat(600), retryable:false,
      input_guide:{intent:'adopt_plan', input:'input {plan_text, graph, resources?}; '+'y'.repeat(400), read:'r'}}}, '', true);
    expect(rejected.length).toBeLessThanOrEqual(BRIEF_LIMIT);
    expect(rejected).toContain('error: invalid_input: Unsupported business input field');
    expect(rejected).toContain('input guide: path error.input_guide, or inspect view=guide intent=adopt_plan');
    expect(rejected).not.toContain('yyyy');
    const guide = resultText({schema:'harness.intent_guide/v1', status:'read_only', intent:'adopt_plan',
      input:'input {plan_text, graph, resources?}; work_item_id: required', guide:{example:'e'.repeat(3000)}});
    expect(guide.length).toBeLessThanOrEqual(BRIEF_LIMIT);
    expect(guide).toContain('guide adopt_plan: input {plan_text, graph, resources?}');
    const index = resultText({schema:'harness.intent_guide/v1', status:'read_only',
      result:{items:[{intent:'adopt_plan'},{intent:'record_work'}]}});
    expect(index).toContain('intents with guides: adopt_plan, record_work');
  });
});

describe('native core crash diagnostics', () => {
  it('returns the exit and stderr tail with unknown when the core prints no result', async () => {
    const root = mkdtempSync(join(tmpdir(), 'core-crash-'));
    try {
      const fake = join(root, 'python');
      writeFileSync(fake, "#!/bin/sh\necho 'Traceback (most recent call last):' >&2\n"
        + "echo '  File \"/Users/someone/private/path.py\", line 1, https://user:secret@example.invalid ghp_abcdefghijklmnopqrstuvwxyz0123456789' >&2\n"
        + "echo \"AttributeError: 'list' object has no attribute 'items'\" >&2\nexit 1\n");
      chmodSync(fake, 0o755);
      const result = await invokeOperation({python: fake, entrypoint: '/unused'}, {action: 'identity'});
      expect(result.status).toBe('unknown');
      expect(result.error.process).toEqual({exit_code: 1, signal: null, timed_out: false});
      expect(result.error.exception).toBe('AttributeError');
      const text = JSON.stringify(result);
      for (const leak of ['secret@', 'ghp_', '/Users/someone', "has no attribute"]) expect(text).not.toContain(leak);
    } finally { rmSync(root, {recursive: true, force: true}); }
  });
});

describe('native daily command extension', () => {
  const ref = 'a'.repeat(64);
  const user = (text:string, role='user') => ({role,content:[{type:'text',text}]});
  function setup(run=vi.fn().mockResolvedValue({status:'local_committed'})) {
    const handlers = new Map<string,Function>(); const tools = new Map<string,any>();
    const operation = vi.fn().mockResolvedValue({status:'human_decision_required'});
    installPiWorkItemCore({on:(name:string,fn:Function)=>handlers.set(name,fn),
      registerTool:(tool:any)=>tools.set(tool.name,tool)},
      {python:'/canonical/python',entrypoint:'/installed/harness.pyz'},run,operation);
    return {handlers,run,tools,operation};
  }
  it('declares request purpose with only a WI and writes nothing',async()=>{
    const {tools,run,operation}=setup();
    const tool=tools.get('declare_session_wi');
    const result=await tool.execute('call',{wi:'wi_20261017_150'});
    expect(JSON.parse(result.content[0].text)).toEqual({schema:'pi_request_wi/v1',wi:'wi_20261017_150',origin:'agent_declaration'});
    await expect(tool.execute('call',{work_item_id:'wi_20261017_150'})).rejects.toThrow('wi_only_arguments_required');
    await expect(tool.execute('call',{wi:'wi_x'})).rejects.toThrow('wi_only_arguments_required');
    await expect(tool.execute('call',{wi_id:'wi_20261017_150'})).rejects.toThrow('retry with {"wi":"wi_20261017_150"}');
    await expect(tool.execute('call',{wi:'None'})).rejects.toThrow('retry with {"wi":"wi_YYYYMMDD_NNN"}');
    // Chat and read-only requests declare none.
    expect(JSON.parse((await tool.execute('call',{wi:'none'})).content[0].text)).toEqual({schema:'pi_request_wi/v1',wi:'none',origin:'agent_declaration'});
    expect(run).not.toHaveBeenCalled(); expect(operation).not.toHaveBeenCalled();
  });
  it('guards write operations until the current request declares, only when enabled',()=>{
    const handlers=new Map<string,Function>();
    const pi={on:(name:string,fn:Function)=>handlers.set(name,fn),registerTool:()=>{}};
    installPiWorkItemCore(pi,{python:'/p',entrypoint:'/e',sessionWiGuard:true});
    const call=(toolName:string,input:any)=>handlers.get('tool_call')!({toolName,input});
    const push={command:'cd repo && git push origin HEAD'};
    expect(call('bash',push)).toBeUndefined();
    handlers.get('input')!({text:'ship it',source:'rpc'});
    expect(call('bash',push)).toMatchObject({block:true});
    expect(call('pulse_work_item',{action:'execute'}).reason).toContain('pulse_work_item execute');
    expect(call('bash',{command:'git status'})).toBeUndefined();
    expect(call('pulse_work_item',{action:'inspect'})).toBeUndefined();
    handlers.get('input')!({text:'more',source:'rpc',streamingBehavior:'steer'});
    handlers.get('tool_result')!({toolName:'declare_session_wi',isError:true});
    expect(call('bash',push)).toMatchObject({block:true});
    handlers.get('tool_result')!({toolName:'declare_session_wi',isError:false});
    expect(call('bash',push)).toBeUndefined();
    handlers.get('input')!({text:'next request',source:'rpc'});
    expect(call('bash',push)).toMatchObject({block:true});
    // A request declared none stays read-only until it declares a WI.
    handlers.get('tool_result')!({toolName:'declare_session_wi',isError:false,details:{wi:'none'}});
    expect(call('bash',push).reason).toContain('本请求声明为 none');
    expect(call('bash',{command:'git status'})).toBeUndefined();
    handlers.get('tool_result')!({toolName:'declare_session_wi',isError:false,details:{wi:'wi_20261017_150'}});
    expect(call('bash',push)).toBeUndefined();
    const plain=new Map<string,Function>();
    installPiWorkItemCore({on:(name:string,fn:Function)=>plain.set(name,fn),registerTool:()=>{}},{python:'/p',entrypoint:'/e'});
    expect(plain.has('tool_call')).toBe(false);
  });
  it('executes after persisted context and reuses the original result on provider retry',async()=>{
    const {handlers,run}=setup();
    const messages=[user('/wi checkpoint original summary\n[Host human input reference: '+ref+'.]')];
    expect(run).not.toHaveBeenCalled();
    const a=await handlers.get('context')!({messages});
    const b=await handlers.get('context')!({messages});
    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith({python:'/canonical/python',entrypoint:'/installed/harness.pyz'},ref);
    expect(a.messages.at(-1).content).toContain('local_committed');
    expect(b.messages.at(-1).content).toBe(a.messages.at(-1).content);
    expect(messages).toHaveLength(1);
  });
  it('never treats assistant text, historical commands or duplicate references as new ingress',async()=>{
    const {handlers,run}=setup();
    const command='/wi cancel wi_fixture reason\n[Host human input reference: '+ref+'.]';
    for(const messages of [
      [user(command,'assistant')], [user(command),user('ordinary input')],
      [user(command+'\n[Host human input reference: '+ref+'.]')], [user('/wi status')],
    ]) expect(await handlers.get('context')!({messages})).toBeUndefined();
    expect(run).not.toHaveBeenCalled();
  });
  it('preserves unknown without replay while the Agent can handle the result',async()=>{
    const {handlers,run}=setup(vi.fn().mockResolvedValue({status:'unknown'}));
    const event={prompt:'/wi continue',systemPromptOptions:{selectedTools:['bash']}};
    expect(handlers.has('before_agent_start')).toBe(false);
    const messages=[user('/wi continue\n[Host human input reference: '+ref+'.]')];
    const result=await handlers.get('context')!({messages});
    await handlers.get('context')!({messages});
    expect(result.messages.at(-1).content).toContain('unknown');
    expect(run).toHaveBeenCalledTimes(1);
  });
  it('returns missing affiliation to the same Agent turn without queuing',async()=>{
    const {tools,operation}=setup();
    const result=await tools.get('pulse_work_item').execute('call-1',
      {action:'resume',work_item_id:'wi_20261008_550'});
    expect(operation).toHaveBeenCalledTimes(1);
    expect(result.details).toEqual({backend:'native_core',result:{status:'human_decision_required'}});
    expect(result.details.queued).toBeUndefined();
    expect(result.content[0].text).toContain('Ask the human only about undecided choices');
    expect(result.content[0].text).toContain('not authorization to implement');
  });
  it('states a rejection reason before a long result body',async()=>{
    const error={code:'invalid_input',reason:'Declare main_only',retryable:false};
    let tool:any;
    installPiWorkItemCore({on:()=>{},registerTool:(t:any)=>tool=t},
      {python:'/canonical/python',entrypoint:'/installed/harness.pyz'},undefined,
      vi.fn().mockResolvedValue({status:'rejected',error,
        effects:Array.from({length:200},(_,i)=>({effect_id:'op_'+i,detail:'x'.repeat(200)}))}));
    const result=await tool.execute('offer',{action:'execute',work_item_id:'wi_20261015_769',
      intent:'offer_collaboration',input:{kind:'request'}});
    const head=result.content[0].text.slice(0,1200);
    expect(head).toContain('status: rejected');
    expect(head).toContain('error: invalid_input: Declare main_only');
    expect(result.isError).toBe(true);
  });
  it('preserves original request identity and actual human provenance',async()=>{
    const {tools,operation}=setup();
    const params={action:'execute',work_item_id:'wi_20261008_550',intent:'record_affiliation',
      input:{type:'operating_area',ref:'OA-PULSE',reason:'human confirmed'},human_input_ref:ref};
    await tools.get('pulse_work_item').execute('same-call',params);
    await tools.get('pulse_work_item').execute('same-call',params);
    const first=operation.mock.calls[0][1].envelope;
    expect(first.request_id).toBe(operation.mock.calls[1][1].envelope.request_id);
    expect(first.human_input_ref).toBe(ref);
  });

});


it('returns the original request readback action after loss, then retries without a new identity',async()=>{
  let tool:any;
  const operation=vi.fn().mockResolvedValueOnce({status:'unknown',
    error:{code:'native_command_result_unavailable'}}).mockResolvedValue({status:'read_only'});
  installPiWorkItemCore({on:()=>{},registerTool:(t:any)=>tool=t},
    {python:'/canonical/python',entrypoint:'/installed/active/harness.pyz'},undefined,operation);
  const params={action:'execute',work_item_id:'wi_20261008_550',intent:'record_affiliation',
    input:{type:'operating_area',ref:'OA-PULSE',reason:'confirmed'},human_input_ref:'a'.repeat(64)};
  const unknown=await tool.execute('original-call',params);
  const request=operation.mock.calls[0][1].envelope.request_id;
  expect(unknown.details.result.next_action).toEqual({action:'inspect',
    work_item_id:params.work_item_id,view:'request',request_id:request});
  await tool.execute('readback-call',unknown.details.result.next_action);
  expect(operation.mock.calls[1][1]).toEqual(unknown.details.result.next_action);
  await tool.execute('retry-call',{...params,request_id:request});
  expect(operation.mock.calls[2][1].envelope).toEqual(operation.mock.calls[0][1].envelope);
});

it('recovers creation with the same request before an allocated WI ID is known',async()=>{
  let tool:any; const operation=vi.fn().mockResolvedValue({status:'unknown'});
  installPiWorkItemCore({on:()=>{},registerTool:(t:any)=>tool=t},
    {python:'/canonical/python',entrypoint:'/installed/active/harness.pyz'},undefined,operation);
  const params={action:'execute',intent:'create_draft',input:{what:'Agreed outcome'}};
  const result=await tool.execute('create-call',params);
  expect(result.details.result.next_action).toEqual({...params,
    request_id:operation.mock.calls[0][1].envelope.request_id});
});

it('recovers an ambiguous invocation across a new tool call and a Pi process restart',async()=>{
  const entries:any[]=[]; let tool:any;
  const api={on:()=>{},registerTool:(t:any)=>tool=t,
    appendEntry:(customType:string,data:any)=>entries.push({type:'custom',customType,data})};
  const ctx={sessionManager:{getEntries:()=>entries}};
  const host={python:'/canonical/python',entrypoint:'/installed/harness.pyz'};
  const operation=vi.fn().mockResolvedValueOnce({status:'unknown'}).mockResolvedValue({status:'local_committed'});
  const params={action:'execute',work_item_id:'wi_20261008_550',intent:'checkpoint',input:{summary:'same intent'}};
  installPiWorkItemCore(api,host,undefined,operation);
  await tool.execute('first-tool-call',params,undefined,undefined,ctx);
  const original=operation.mock.calls[0][1].envelope.request_id;
  installPiWorkItemCore(api,host,undefined,operation);
  await tool.execute('retry-with-new-call-id',params,undefined,undefined,ctx);
  expect(operation.mock.calls[1][1].envelope.request_id).toBe(original);
  await tool.execute('genuine-new-call',params,undefined,undefined,ctx);
  expect(operation.mock.calls[2][1].envelope.request_id).not.toBe(original);
});


it('writes an unflushed Pi Session once and leaves memory-only or written Sessions alone',()=>{
  const directory=mkdtempSync(join(tmpdir(),'pi-wi-session-'));
  try {
    const file=join(directory,'session.jsonl');
    const entries=[{type:'session',id:'s'},{type:'message',message:{role:'user',content:'/wi list'}}];
    const manager={persist:true,flushed:false,fileEntries:entries,getSessionFile:()=>file};
    persistOriginalSession({...manager,persist:false});
    expect(()=>readFileSync(file)).toThrow();
    persistOriginalSession(manager);
    expect(manager.flushed).toBe(true);
    expect(readFileSync(file,'utf8').trim().split('\n').map(row=>JSON.parse(row))).toEqual(entries);
    writeFileSync(file,'existing\n');
    persistOriginalSession(manager);
    expect(readFileSync(file,'utf8')).toBe('existing\n');
  } finally { rmSync(directory,{recursive:true,force:true}); }
});


describe('brief results and on-demand pages', () => {
  // WI422 scale on 11-14: 129 PRs (~1.27M chars) and 378 effects (~1.1M chars).
  const view = {title:'T', business_progress:'active', generation:3,
    collaborations:Array.from({length:24},(_,i)=>({contract_id:'op_c'+i, purpose:'p'.repeat(2000)})),
    pull_requests:Array.from({length:129},(_,i)=>({number:i, rounds:[{review:{reason:'r'.repeat(9800)}}]}))};
  const effects = Array.from({length:378},(_,i)=>({effect_id:'op_e'+i, status:'observed_success', detail:'x'.repeat(2900)}));
  const full = {schema:'harness.response/v1', status:'local_committed', error:null,
    next_action:{intent:'record_work', reason:'active'}, request_id:'r1', operation_id:'op_own',
    work_item:{id:'wi_20261015_422', local_revision:1947}, effects,
    result:{original_request_result:{contract_id:'op_c1', decision:'accept', revision:20}, current_observation:view},
    latest_observation:{status:'read_only', effects, result:{current_observation:view}}};
  const install = (operation:any) => { let tool:any;
    installPiWorkItemCore({on:()=>{},registerTool:(t:any)=>{ if (t.name==='pulse_work_item') tool=t; }},
      {python:'/canonical/python',entrypoint:'/installed/harness.pyz'},undefined,operation);
    return tool; };

  it('keeps every default result within 512 characters with the decision and pointers', async () => {
    expect(JSON.stringify(full).length).toBeGreaterThan(4_800_000);
    const operation = vi.fn().mockResolvedValue(full);
    const tool = install(operation);
    for (const params of [
      {action:'execute', work_item_id:'wi_20261015_422', intent:'respond_collaboration', input:{}},
      {action:'inspect', work_item_id:'wi_20261015_422', view:'collaborations'}]) {
      const text = (await tool.execute('call', params)).content[0].text;
      expect(text.length).toBeLessThanOrEqual(BRIEF_LIMIT);
      expect(text).toContain('status: local_committed request_id: r1 wi: wi_20261015_422@1947');
      expect(text).toContain('next_action: {"intent":"record_work","reason":"active"}');
      expect(text).toContain('result: {"contract_id":"op_c1","decision":"accept","revision":20}');
      expect(text).toContain('parts: result.current_observation.pull_requests');
    }
    const executed = (await tool.execute('call', {action:'execute', work_item_id:'wi_20261015_422', intent:'x', input:{}}));
    expect(executed.content[0].text.split('\n').at(-1)).toBe('read more: inspect view=request, the request_id above and path (this original response)');
    expect(executed.details.result).toBe(full);
  });

  it('reads the cut parts of a rejected or unknown execute from its original response', async () => {
    const reason = 'The recipient holds several active WIs; ' + 'x'.repeat(300)
      + ' Name that WI in target_work_item_id: wi_20261030_991 for session cc:feishu:aki';
    const rejected = {schema:'harness.response/v1', status:'rejected', request_id:'r-reject',
      error:{code:'domain_conflict', reason, retryable:false}};
    const unknown = {schema:'harness.response/v1', status:'unknown', request_id:'r-create',
      error:{code:'native_command_result_unavailable', reason:'timeout', retryable:true}};
    const operation = vi.fn().mockResolvedValueOnce(rejected).mockResolvedValueOnce(unknown);
    const tool = install(operation);
    const brief = (await tool.execute('call', {action:'execute', work_item_id:'wi_20261015_422',
      intent:'offer_collaboration', input:{main_only:true}, request_id:'r-reject'})).content[0].text;
    expect(brief.length).toBeLessThanOrEqual(BRIEF_LIMIT);
    expect(brief).not.toContain('target_work_item_id: wi_20261030_991');
    const read = (await tool.execute('call', {action:'inspect', view:'request', request_id:'r-reject',
      work_item_id:'wi_20261015_422', path:'error.reason'})).content[0].text;
    expect(read).toContain('original execute response');
    expect(read).toContain('Name that WI in target_work_item_id: wi_20261030_991 for session cc:feishu:aki');
    await tool.execute('call', {action:'execute', intent:'create_lightweight', input:{summary:'s'}, request_id:'r-create'});
    const created = (await tool.execute('call', {action:'inspect', view:'request', request_id:'r-create', path:''})).content[0].text;
    expect(created).toContain('"status":"unknown"');
    expect(operation).toHaveBeenCalledTimes(2); // reading never repeats or reaches the core
  });

  it('keeps a rejection, an unknown result and a human decision brief', () => {
    const big = {effects, result:{current_observation:view}};
    const rejected = resultText({...big, status:'rejected', error:{code:'domain_conflict', reason:'Read back the original uncertain effect before retry'}});
    expect(rejected.length).toBeLessThanOrEqual(BRIEF_LIMIT);
    expect(rejected).toContain('error: domain_conflict: Read back the original uncertain effect before retry');
    const unknown = resultText({...big, status:'unknown', request_id:'r9',
      next_action:{action:'inspect', work_item_id:'wi_20261015_422', view:'request', request_id:'r9'}});
    expect(unknown.length).toBeLessThanOrEqual(BRIEF_LIMIT);
    expect(unknown).toContain('"view":"request","request_id":"r9"');
    expect(resultText({...big, status:'human_decision_required'})).toContain('Ask the human only about undecided choices');
  });

  it('keeps production-length identifiers and the read-back line whole', () => {
    const hex = (c:string) => c.repeat(64);
    const request = 'tool.' + hex('a'), candidate = 'op_' + hex('b'), operation = 'op_' + hex('c');
    const text = resultText({status:'local_committed', request_id:request, operation_id:operation,
      work_item:{id:'wi_20261015_422', local_revision:1961}, effects,
      error:{code:'domain_conflict', reason:'Read back the original uncertain effect before retry. '.repeat(20)},
      next_action:{intent:'record_pull_request_review', reason:'pull_request_awaiting_review', candidate_id:candidate,
        runtime:'codex', compute_profile:'review', detail:'d'.repeat(400)},
      result:{original_request_result:{operation_id:operation, candidate_id:candidate, attempt_id:'op_'+hex('d'),
        receipt:'r'.repeat(600)}, current_observation:view}}, '', true);
    expect(text.length).toBeLessThanOrEqual(BRIEF_LIMIT);
    expect(text).toContain('request_id: ' + request);
    expect(text).toContain('"intent":"record_pull_request_review"');
    expect(text).toContain('"candidate_id":"' + candidate + '"');
    expect(text.split('\n').at(-1)).toBe('read more: inspect view=request, the request_id above and path (this original response)');
    for (const line of text.split('\n')) if (line.startsWith('next_action: ')) JSON.parse(line.slice(13).replace(/\+$/, ''));
  });

  it('keeps the deciding fields of identity, request read-back and list', () => {
    const identity = resultText({schema:'harness.native_identity/v1', principal:{actor:'aki', session:'session.' + 'e'.repeat(64)},
      latest_human_input_ref:'f'.repeat(64)});
    expect(identity).toContain('identity: actor=aki session=session.' + 'e'.repeat(64) + ' latest_human_input_ref=' + 'f'.repeat(64));
    const absent = resultText({status:'read_only', request_id:'r9', work_item:{id:'wi_20261015_422', local_revision:3},
      result:{request_observation:{state:'absent'}, current_observation:view}, effects});
    expect(absent).toContain('original request: absent');
    const partial = resultText({status:'read_only', result:{items:[], coverage:'partial', next_offset:100,
      unavailable_items:[{id:'wi_x'}, {id:'wi_y'}]}});
    expect(partial).toContain('items: 0 coverage=partial next_offset=100 unavailable=2');
    for (const text of [identity, absent, partial]) expect(text.length).toBeLessThanOrEqual(BRIEF_LIMIT);
  });

  it('reads one part of a read result in pages on request', async () => {
    const operation = vi.fn().mockResolvedValue(full);
    const tool = install(operation);
    const first = (await tool.execute('read', {action:'inspect', work_item_id:'wi_20261015_422', view:'collaborations',
      path:'result.current_observation.collaborations', page:0})).content[0].text;
    expect(first).toMatch(/^path: result.current_observation.collaborations page 0\/\d+ \(\d+ chars\)\n\[\{"contract_id":"op_c0"/);
    expect(first.length).toBeLessThanOrEqual(PAGE_SIZE + 120);
    expect(operation.mock.calls.at(-1)[1]).not.toHaveProperty('path');
    const missing = (await tool.execute('read', {action:'inspect', work_item_id:'wi_20261015_422', path:'result.nope'})).content[0].text;
    expect(missing).toContain('path not found: result.nope; available: original_request_result, current_observation');
    const beyond = (await tool.execute('read', {action:'inspect', work_item_id:'wi_20261015_422', path:'request_id', page:3})).content[0].text;
    expect(beyond).toBe('page out of range: 3 of 1');
  });

  it('never repeats an execute to read it', async () => {
    const operation = vi.fn().mockResolvedValue(full);
    const tool = install(operation);
    await expect(tool.execute('call', {action:'execute', work_item_id:'wi_20261015_422', intent:'x', input:{}, path:'effects'}))
      .rejects.toThrow('read an execute result back with action inspect, view request and its request_id');
    expect(operation).not.toHaveBeenCalled();
  });

  it('states the declaration, holder, retry and registration rules in the tool guide', () => {
    let tool:any; let declare:any;
    installPiWorkItemCore({on:()=>{},registerTool:(t:any)=>{ if (t.name==='pulse_work_item') tool=t; else if (t.name==='declare_session_wi') declare=t; }},
      {python:'/canonical/python',entrypoint:'/installed/harness.pyz'});
    const guide = tool.promptGuidelines.join(' ');
    expect(guide).toContain('/wi continue <WI>');
    expect(guide).toContain('retry_effect only restarts an effect read back as observed_absence');
    expect(guide).toContain('give it a new request_id');
    expect(guide).not.toContain('immediately: true');
    expect(guide).toContain('work-item-lifecycle');
    expect(guide).toContain('first read its guide: action inspect, view guide, intent <name>');
    expect(tool.parameters.properties.view.enum).toContain('guide');
    expect(declare.description).toContain('一个会话只为一个 WI 谱系（一个 WI 及其子 WI）写代码、提 PR');
    expect(declare.description).toContain('谱系内的提交可登记在谱系任一 WI 上');
  });
});

