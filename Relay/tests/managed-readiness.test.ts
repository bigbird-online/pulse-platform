import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { RelayHttpServerDeps, RelayHttpServerHandle } from '../src/http-server.js';
let root:string;let server:RelayHttpServerHandle|undefined;
beforeEach(()=>{
  vi.resetModules();root=realpathSync(mkdtempSync(join(tmpdir(),'relay-readiness-')));
  mkdirSync(join(root,'config/relay'),{recursive:true});mkdirSync(join(root,'data/relay'),{recursive:true});
  const file=join(root,'config/relay/paths.json');
  writeFileSync(file,JSON.stringify({schema:'pulse_relay_paths/v1',root,
    paths:{relayConfig:join(root,'relay.json'),cronConfig:join(root,'cron.json'),dataDir:join(root,'data/relay')},
    requiredChannels:[{agentId:'pulse',channel:'telegram'},{agentId:'pulse',channel:'feishu'}]}));
  vi.stubEnv('PULSE_RELAY_PATHS',file);vi.stubEnv('RELAY_INTERNAL_TOKEN','');
});
afterEach(async()=>{await server?.stop();server=undefined;vi.unstubAllEnvs();vi.resetModules();rmSync(root,{recursive:true,force:true});});
async function health(configured:string[],running:string[]){
  const {startRelayHttpServer}=await import('../src/http-server.js');
  const deps={configPath:join(root,'relay.json'),
    feishuBots:{runningAgentIds:()=>['pulse']},telegramBots:{runningAgentIds:()=>running},
    router:{getAllAgentBots:()=>configured.map(id=>({agent:{id}})),getAllFeishuBots:()=>[{agent:{id:'pulse'}}]},
  } as unknown as RelayHttpServerDeps;
  server=await startRelayHttpServer(deps,0,'127.0.0.1');
  return (await fetch(`http://127.0.0.1:${server.port}/internal/health`)).json();
}
describe('managed readiness includes the original business entry',()=>{
  it('cannot mark a Feishu-only configuration as a healthy required Telegram service',async()=>{
    const value=await health([],[]);expect(value.status).toBe('degraded');
    expect(value.missing).toContainEqual({agentId:'pulse',channel:'telegram'});
  });
  it('reports a configured but absent Telegram client, rather than merely uptime',async()=>{
    const value=await health(['pulse'],[]);expect(value.status).toBe('degraded');
  });
  it('reports both restored channels and the active map only when clients exist',async()=>{
    const value=await health(['pulse'],['pulse']);expect(value.status).toBe('ok');
    expect(value.telegramBots).toBe(1);expect(value.feishuBots).toBe(1);
    expect(value.configuration.map).toBe(join(root,'config/relay/paths.json'));
  });
});
