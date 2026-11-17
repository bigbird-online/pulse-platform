import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, symlinkSync, readFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

let root: string;
let file: string;
let paths: Record<string, string>;
const envKeys = ['PULSE_RELAY_PATHS','RELAY_CONFIG_PATH','CRON_JOBS_PATH','CRON_JOBS_STATE_PATH',
  'RELAY_DATA_DIR','RELAY_HOME','METERING_DIR'];
let previous: Record<string, string | undefined>;
beforeEach(() => {
  vi.resetModules();
  previous = Object.fromEntries(envKeys.map(k => [k,process.env[k]]));
  for (const key of envKeys) delete process.env[key];
  root = mkdtempSync(join(tmpdir(),'relay-layout-test-'));
  for (const dir of ['config/relay','secrets/relay','data/relay']) mkdirSync(join(root,dir),{recursive:true});
  file = join(root,'config/relay/paths.json');
  paths = {relayConfig:join(root,'secrets/relay/relay.json'),cronConfig:join(root,'config/relay/cron-jobs.json'),
    cronState:join(root,'data/relay/cron-jobs-state.json'),dataDir:join(root,'data/relay'),
    modelOverridesDir:join(root,'config/relay/agent-models'),meteringDir:join(root,'data/relay/metering')};
  writeFileSync(file,JSON.stringify({schema:'pulse_relay_paths/v1',root,paths}));
  process.env.PULSE_RELAY_PATHS=file;
});
afterEach(() => {
  for (const key of envKeys) previous[key] === undefined ? delete process.env[key] : process.env[key]=previous[key];
  rmSync(root,{recursive:true,force:true});
  vi.resetModules();
});

describe('one managed Relay configuration and state map', () => {
  it('uses the same registered paths for service, jobs, state and provisioning defaults',async () => {
    const p=await import('../src/config-paths.js');
    const j=await import('../src/job-store.js');
    expect(p.resolveConfigPath()).toBe(paths.relayConfig);
    expect(p.resolveCronJobsPath()).toBe(paths.cronConfig);
    expect(p.resolveRelayDataDir()).toBe(paths.dataDir);
    expect(p.resolveMeteringDir()).toBe(paths.meteringDir);
    expect(j.resolveCronJobsStatePath(paths.cronConfig)).toBe(paths.cronState);
  });
  it('does not silently accept a stale service environment or alternate CLI write target',async () => {
    const p=await import('../src/config-paths.js');
    process.env.RELAY_CONFIG_PATH=join(root,'old.json');
    expect(()=>p.resolveConfigPath()).toThrow('differs');
    delete process.env.RELAY_CONFIG_PATH;
    expect(()=>p.resolveConfigPath(join(root,'old.json'))).toThrow('differs');
    expect(p.resolveConfigPath(paths.relayConfig)).toBe(paths.relayConfig);
  });
  it('fails rather than treating a missing instance map as empty initial data',async () => {
    rmSync(file);
    const p=await import('../src/config-paths.js');
    expect(()=>p.resolveConfigPath()).toThrow();
  });
  it.each(['../outside.json','/tmp/outside.json'])('rejects paths outside the declared root: %s',async target => {
    paths.relayConfig=target;
    writeFileSync(file,JSON.stringify({schema:'pulse_relay_paths/v1',root,paths}));
    const p=await import('../src/config-paths.js');
    expect(()=>p.resolveConfigPath()).toThrow('escapes');
  });
  it('rejects an existing symlink into an unrelated root',async () => {
    symlinkSync(tmpdir(),join(root,'escape'));
    paths.relayConfig=join(root,'escape','foreign.json');
    writeFileSync(file,JSON.stringify({schema:'pulse_relay_paths/v1',root,paths}));
    const p=await import('../src/config-paths.js');
    expect(()=>p.resolveConfigPath()).toThrow('alias escapes');
  });
  it('preserves independent development defaults with no production map',async () => {
    delete process.env.PULSE_RELAY_PATHS;
    const p=await import('../src/config-paths.js');
    expect(p.resolveConfigPath('/isolated/test.json')).toBe('/isolated/test.json');
    expect(p.resolveConfigPath()).not.toBe(paths.relayConfig);
  });
  it('persists runtime and model changes only to the active private configuration',async () => {
    writeFileSync(paths.relayConfig,JSON.stringify({users:[{userId:'test-user',chatId:'123',defaultAgent:'pulse',
      agents:[{id:'pulse',name:'Pulse',botToken:'synthetic-not-a-real-token',workspace:root,sessionId:'same-session'}]}]}),{mode:0o600});
    const {createRouter}=await import('../src/router.js');
    const router=createRouter(paths.relayConfig);
    router.setAgentRuntime('test-user','pulse','pi');
    const stored=JSON.parse(readFileSync(paths.relayConfig,'utf8'));
    expect(stored.users[0].agents[0].runtime).toBe('pi');
    expect(stored.users[0].agents[0].sessionId).toBe('same-session');
    expect(statSync(paths.relayConfig).mode&0o777).toBe(0o600);
    router.setAgentModel('test-user','pulse','test-model');
    expect(JSON.parse(readFileSync(join(paths.modelOverridesDir,'test-user.json'),'utf8')).agents.pulse.model).toBe('test-model');
  });
});
