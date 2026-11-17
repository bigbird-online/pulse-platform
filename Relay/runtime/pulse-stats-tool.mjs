/** Read-only PULSE Stats query for Agents: the installed Stats release's query CLI, nothing else. */
import {execFile} from 'node:child_process';
import {existsSync} from 'node:fs';
import {userInfo} from 'node:os';
import {join} from 'node:path';

export const STATS_TOOL_NAME = 'pulse_stats';
export const STATS_TIMEOUT_MS = 60_000;
export const STATS_MAX_BYTES = 2 * 1024 * 1024;
const QUERIES = ['overview','cost','wi','quotas','health','board','automation','metric','snapshot','agent_day'];
const NAME = '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$';
const DAY = '^[0-9]{4}-[0-9]{2}-[0-9]{2}$';
const FLAGS = {period:'--period', group:'--group', work_item_id:'--work-item-id', agent:'--agent',
  runtime:'--runtime', metric_id:'--metric-id', date:'--date', end_date:'--end-date', ws:'--ws'};

export const statsParameters = {type:'object', properties:{
  query:{type:'string', enum:QUERIES},
  period:{type:'string', enum:['today','week','month','last_week']},
  group:{type:'string', enum:['agent','wi','runtime','model','node','entrypoint','tenant']},
  work_item_id:{type:'string', pattern:'^wi_[0-9]{8}_[0-9]{3}$'},
  agent:{type:'string', pattern:NAME},
  runtime:{type:'string', pattern:NAME},
  metric_id:{type:'string', pattern:'^[a-z0-9_.]{1,100}$'},
  date:{type:'string', pattern:DAY},
  end_date:{type:'string', pattern:DAY},
  ws:{type:'string', pattern:'^(?:[A-Za-z0-9_-][A-Za-z0-9._-]*/)*[A-Za-z0-9._-]*/?$', maxLength:200},
}, required:['query'], additionalProperties:false};

export const statsDescription = 'Read-only PULSE Stats query: the same published data the PulseStats App shows '
  +'(产出与成本 cost by period and agent/WI/runtime; period last_week is the previous complete Monday..Sunday; '
  +'one WI\'s cost/outcomes/board state, quotas, health, '
  +'在办 board and 要你出手, automation tasks, any App metric, or the whole App snapshot). '
  +'agent_day (agent and date required; optional end_date and ws, a Coevolution-relative workspace path) lists one '
  +'Agent\'s commits, PRs and WI events for a UTC+8 day from the synced Git checkouts and kernel ledger, or one '
  +'count row per day for a date range; its sections say complete/incomplete/unavailable. '
  +'Returns pulse_stats_query/v1 JSON with each source\'s generated_at and stale flag; '
  +'treat stale or unavailable sources as unknown, never as zero.';

/** Agent sessions run with an isolated HOME; the App and its data live under the account home. */
export function statsLocation(home = process.env.PULSE_STATS_ACCOUNT_HOME || userInfo().homedir) {
  return {home, python:join(home,'.local/share/coevolution/python-env/bin/python'),
    script:join(home,'.local/share/pulse/stats/current/Stats/exporter/pulse-stats-query.py')};
}

export function statsAvailable(location = statsLocation()) {
  return existsSync(location.python) && existsSync(location.script);
}

/** Validate exactly the declared schema; return the CLI argv tail. */
export function statsArguments(params) {
  if (!params || typeof params !== 'object' || Array.isArray(params)) throw new Error('pulse_stats arguments must be an object');
  const props = statsParameters.properties;
  for (const [key,value] of Object.entries(params)) {
    const spec = props[key];
    if (!spec) throw new Error('Unknown pulse_stats argument: '+key);
    if (typeof value !== 'string') throw new Error('pulse_stats argument must be a string: '+key);
    if (spec.enum && !spec.enum.includes(value)) throw new Error('Unsupported pulse_stats '+key+': '+value);
    if (spec.pattern && !new RegExp(spec.pattern).test(value)) throw new Error('Invalid pulse_stats '+key);
  }
  if (!params.query) throw new Error('pulse_stats query is required');
  if (params.query === 'wi' && !params.work_item_id) throw new Error('pulse_stats wi requires work_item_id');
  if (params.query === 'agent_day' && (!params.agent || !params.date)) throw new Error('pulse_stats agent_day requires agent and date');
  if (params.ws !== undefined && params.ws.split('/').includes('..')) throw new Error('Invalid pulse_stats ws');
  const argv = [params.query];
  for (const [key,flag] of Object.entries(FLAGS)) if (params[key] !== undefined) argv.push(flag, params[key]);
  return [...argv, '--json'];
}

function unavailable(code) {
  return JSON.stringify({schema:'pulse_stats_query/v1', status:'unavailable', errors:[code], result:null});
}

/** Spawn the installed query with a fixed minimal environment, bounded time and output. */
export function runStatsQuery(params, location = statsLocation(), run = execFile) {
  const argv = statsArguments(params);
  if (!statsAvailable(location)) return Promise.resolve({isError:true, text:unavailable('stats_query_not_installed')});
  return new Promise(resolve => {
    run(location.python, ['-I','-B',location.script,...argv],
      {encoding:'utf8', timeout:STATS_TIMEOUT_MS, maxBuffer:STATS_MAX_BYTES, cwd:'/',
        env:{HOME:location.home, PATH:'/usr/bin:/bin', LANG:'en_US.UTF-8'}},
      (error, stdout) => {
        let value;
        try { value = JSON.parse(stdout); } catch { value = undefined; }
        if (error || !value || value.schema !== 'pulse_stats_query/v1') {
          // Do not relay stderr: it may name private paths; the code is enough to retry or report.
          const code = error?.killed ? 'stats_query_timeout'
            : error?.code === 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER' ? 'stats_query_output_too_large'
            : 'stats_query_result_unavailable';
          resolve({isError:true, text:unavailable(code)});
          return;
        }
        // A stale or unavailable source is still an answer; its errors say which.
        resolve({isError:false, text:stdout.trim()});
      });
  });
}
