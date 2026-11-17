import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {installPiUsageLifecycle} from '../runtime/pi-usage-binding.mjs';

const sdkRoot=process.env.PULSE_PI_TEST_SDK_DIRECTORY;
test('real Pi tools write, edit and execute despite failed measurement; Host allowlists remain exact',
  {skip:!sdkRoot,timeout:20000},async()=>{
    const sdk=await import(pathToFileURL(join(sdkRoot,'core/sdk.js')));
    const {DefaultResourceLoader}=await import(pathToFileURL(join(sdkRoot,'core/resource-loader.js')));
    const {SessionManager}=await import(pathToFileURL(join(sdkRoot,'core/session-manager.js')));
    const root=await mkdtemp(join(tmpdir(),'wi018-pi-tool-boundary-'));
    const agentDir=join(root,'agent');await mkdir(agentDir);
    const file=join(root,'proof.txt');
    for (const selected of [undefined,['read','edit'],[]]) {
      for (let restart=0;restart<2;restart++) {
        let captures=0;
        const loader=new DefaultResourceLoader({cwd:root,agentDir,noExtensions:true,noSkills:true,
          noThemes:true,noPromptTemplates:true,noContextFiles:true,
          extensionFactories:[pi=>installPiUsageLifecycle(pi,{captureSnapshot:async()=>{captures++;throw new Error('isolated measurement failure');}})]});
        await loader.reload();
        const {session}=await sdk.createAgentSession({cwd:root,agentDir,resourceLoader:loader,
          sessionManager:SessionManager.create(root,join(root,'sessions')),tools:selected});
        try {
          await session.bindExtensions({});
          await new Promise(resolve=>setImmediate(resolve));
          assert.equal(captures,1);
          assert.deepEqual(session.getActiveToolNames(),selected??['read','bash','edit','write']);
          if(selected)continue;
          const tools=new Map(session.agent.state.tools.map(tool=>[tool.name,tool]));
          await tools.get('write').execute('write-proof',{path:file,content:'before\n'});
          await tools.get('edit').execute('edit-proof',{path:file,edits:[{oldText:'before',newText:'after'}]});
          const result=await tools.get('bash').execute('bash-proof',{command:'cat proof.txt'});
          assert.equal(await readFile(file,'utf8'),'after\n');
          assert.match(JSON.stringify(result),/after/);
        } finally {session.dispose();}
      }
    }
  });
