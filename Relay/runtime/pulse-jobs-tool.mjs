/**
 * pulse_jobs: an Agent manages its own scheduled jobs. The owner is the
 * principal the session's core identity names, never an argument. The request
 * to the local Relay (POST /internal/agent-jobs) is signed with the key Relay
 * issued this conversation (PULSE_AGENT_JOBS_KEY_FILE, agent-jobs-auth.ts);
 * Relay recognises the caller by that key and enforces every rule (agent-jobs.ts).
 * CC and Codex conversations have it; scheduled runs and Pi sessions do not.
 */
import {createHmac, randomBytes} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {basename} from 'node:path';

export const JOBS_TOOL_NAME = 'pulse_jobs';
export const JOBS_TIMEOUT_MS = 30_000;

export const jobsParameters = {type:'object', properties:{
  action:{type:'string', enum:['list','add','edit','enable','disable','delete']},
  job_id:{type:'string', pattern:'^[a-z0-9][a-z0-9-]{0,62}$', description:'Starts with "<your agent id>-"'},
  name:{type:'string', maxLength:80},
  schedule:{type:'object', properties:{expr:{type:'string'}, tz:{type:'string'}}, required:['expr'],
    additionalProperties:false, description:'5-field cron with one fixed minute (at most hourly); tz defaults to Asia/Shanghai'},
  prompt:{type:'string', maxLength:4000},
  wake:{type:'object', properties:{workItemId:{type:'string', pattern:'^wi_[0-9]{8}_[0-9]{3}$'}}, required:['workItemId'],
    additionalProperties:false},
  inject_file:{type:'string', description:'Batch jobs: a file inside your workspace prepended to the prompt'},
  timeout:{type:'integer', minimum:60, maximum:3600, description:'Batch jobs: seconds, default 1800'},
}, required:['action'], additionalProperties:false};

export const jobsDescription = 'Manage your own scheduled jobs on this Relay; the owner is always you (your session identity). '
  +'list shows them all. add creates one: with wake {workItemId} it is a wake job, which at the scheduled minute hands prompt '
  +'to the conversation that holds that WI (queued while it is busy) and starts no unattended session; if no conversation holds '
  +'it, your human gets a reminder. Without wake it is a batch job: an unattended session in your workspace runs prompt '
  +'(timeout at most 3600 s). edit, enable and delete act on jobs you created here; disable also stops another of your jobs '
  +'(e.g. an old heartbeat), which only PULSE re-enables. Platform jobs (collab-inbox, collab-deliver, pr-review) are not managed here. '
  +'Limits: at most 8 jobs, each at most hourly. Changes take effect at once; the Stats automation board shows them.';

function relayUrl(env = process.env) {
  const raw = (env.RELAY_HTTP_HOST || '').trim();
  const host = !raw || raw === '0.0.0.0' ? '127.0.0.1' : raw;
  return `http://${host}:${(env.RELAY_HTTP_PORT || '8787').trim()}/internal/agent-jobs`;
}

/** The proof agent-jobs-auth.ts verifies; the key itself never leaves this process. */
export function signedRequest(args, keyFile, now = Date.now()) {
  const ref = basename(keyFile, '.key');
  const key = readFileSync(keyFile, 'utf8').trim();
  const nonce = randomBytes(16).toString('hex');
  const body = JSON.stringify(args);
  const sig = createHmac('sha256', key).update(`pulse.agent_jobs_request/v1\n${ref}\n${now}\n${nonce}\n${body}`).digest('hex');
  return {schema:'pulse.agent_jobs_request/v1', key:ref, ts:now, nonce, body, sig};
}

/** `identity` reads the session's core identity; `post` is replaceable in tests. */
export async function runJobsTool(args, identity, post = fetch, env = process.env) {
  if (!env.PULSE_AGENT_JOBS_KEY_FILE) {
    return {isError:true, text:JSON.stringify({error:'pulse_jobs is only available in a Relay conversation'})};
  }
  const who = await identity();
  const {actor, session, surface} = who?.principal ?? {};
  if (who?.schema !== 'harness.native_identity/v1' || ![actor, session, surface].every(v => typeof v === 'string' && v)) {
    return {isError:true, text:JSON.stringify({error:'session identity unavailable', identity:who})};
  }
  const response = await post(relayUrl(env), {
    method:'POST', headers:{'content-type':'application/json'},
    body:JSON.stringify(signedRequest({...args, actor, session, surface}, env.PULSE_AGENT_JOBS_KEY_FILE)),
    signal:AbortSignal.timeout(JOBS_TIMEOUT_MS),
  });
  const body = await response.json().catch(() => ({ok:false, error:'invalid_response'}));
  return {isError:!body.ok, text:JSON.stringify(body.ok ? body.result : body)};
}
