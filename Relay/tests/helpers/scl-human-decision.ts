import { beforeEach,afterEach,vi } from 'vitest';
import { mkdtempSync,rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { randomUUID } from 'node:crypto';
export function isolateHumanDecisions(){
  let root:string;
  beforeEach(()=>{root=mkdtempSync(join(tmpdir(),'relay-human-fixture-'));vi.stubEnv('SCL_HUMAN_DECISION_KEY_FILE',join(root,'key'));vi.stubEnv('PULSE_CORE_HUMAN_INPUTS',join(root,'human-inputs'));});
  afterEach(()=>{rmSync(root,{recursive:true,force:true});vi.unstubAllEnvs();});
}
export function syntheticHumanSource(channel:'telegram'|'feishu'){
  return {channel,eventId:'synthetic-test-'+randomUUID(),humanRef:'synthetic-test-human',message:'Explicit synthetic reinit decision'};
}
