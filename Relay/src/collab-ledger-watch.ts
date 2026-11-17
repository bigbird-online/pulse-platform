/**
 * Run an Agent's collaboration-inbox job as soon as a contract that concerns it
 * is in the production ledger, instead of waiting for the job's next slot.
 *
 * The node's production ledger checkout keeps a remote-tracking ref of the
 * ledger branch (`refs/remotes/<remote>/<branch>`). Git moves it on every
 * publication and every read refresh this node's kernel already does, so
 * watching it costs only local reads: a stat of the ref files every few
 * seconds and, when the ref moved, a local diff of the new journal entries.
 * It never contacts the remote.
 *
 * A new entry with `CollaborationOffered` concerns its recipients; one with
 * `CollaborationResponded` other than `accept` concerns the requester, who now
 * has to settle. A pull request result concerns the Agent that registered the
 * round: `PullRequestReviewed` (changes requested, or human confirmation
 * required) and the read-back of a merge effect (merged, checks failed, merge
 * failed or blocked). For each such Agent the watcher triggers the local job whose
 * gate runs `collab-inbox-gate.py check --actor <Agent>`. That gate run is the
 * one delivery path: it reads the inbox, delivers main-only and requester items
 * to the holding conversation and wakes the Agent for the others, with its own
 * state and Relay's delivery ledger deciding what is due. A job already running
 * or held by maintenance is retried until it starts, at most PENDING_LIMIT_MS;
 * a job runs on an event at most once per MIN_INTERVAL_MS. Anything this watcher
 * misses (an unreadable entry, a burst beyond MAX_ENTRIES, a contract offered
 * by another node before this node reads the ledger) is left to the job's
 * schedule, which stays the fallback. `request` asks for the same run from
 * outside (Relay deferred a delivery to a busy conversation that is idle now).
 */
import { relayChildEnv } from './relay-secrets.js';
import { execFile } from 'node:child_process';
import { readFileSync, statSync } from 'node:fs';
import { basename, isAbsolute, join } from 'node:path';
import type { JobTriggerStatus, ScheduledJob } from './types.js';

export const WATCH_INTERVAL_MS = 5_000;
/** At most one event run per job in this window; later events wait. */
export const MIN_INTERVAL_MS = 60_000;
/** A job that stays running or held this long is left to its schedule. */
export const PENDING_LIMIT_MS = 15 * 60_000;
/** New journal entries read per ref move; a larger burst is left to the schedule. */
export const MAX_ENTRIES = 256;
const GATE_SCRIPT = 'collab-inbox-gate.py';
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
const JOURNALS = '.harness/core/journals/';
const OBJECTS = '.harness/core/objects/';
const ENTRY = /^\.harness\/core\/journals\/([A-Za-z0-9_.-]+)\/[0-9]{12}\.json$/;
const SHA = /^[0-9a-f]{40}([0-9a-f]{24})?$/;

export type Git = (args: string[]) => Promise<Buffer>;
type Log = (event: Record<string, unknown>) => void;

/** The Agent whose collaboration inbox the job's gate reads, if it is such a job. */
export function collabInboxActor(job: ScheduledJob): string | undefined {
  const args = job.agent.gate?.args ?? [];
  const script = args.findIndex(arg => basename(arg) === GATE_SCRIPT);
  if (script < 0 || args[script + 1] !== 'check') return undefined;
  const flag = args.indexOf('--actor', script + 2);
  const actor = flag < 0 ? undefined : args[flag + 1];
  return actor && ID.test(actor) ? actor : undefined;
}

/** The production ledger binding of the installed host policy next to `entrypoint`. */
export function ledgerBinding(policyPath: string): { repository: string; trackingRef: string } | undefined {
  const policy = JSON.parse(readFileSync(policyPath, 'utf8')) as { production_ledger?: Record<string, unknown> };
  const ledger = policy.production_ledger;
  if (!ledger) return undefined;
  const { repository, remote, ref } = ledger;
  if (typeof repository !== 'string' || !isAbsolute(repository)
    || typeof remote !== 'string' || !/^[A-Za-z0-9_.-]+$/.test(remote)
    || typeof ref !== 'string' || !/^refs\/heads\/[A-Za-z0-9_./-]+$/.test(ref)) {
    throw new Error('Installed production ledger binding is invalid');
  }
  return { repository, trackingRef: `refs/remotes/${remote}/${ref.slice('refs/heads/'.length)}` };
}

export function localGit(repository: string): Git {
  const env: NodeJS.ProcessEnv = { ...relayChildEnv(), GIT_OPTIONAL_LOCKS: '0', GIT_TERMINAL_PROMPT: '0' };
  for (const name of Object.keys(env)) {
    if (/^GIT_(DIR|WORK_TREE|INDEX_FILE|OBJECT_DIRECTORY|ALTERNATE_OBJECT_DIRECTORIES|COMMON_DIR|NAMESPACE)$/.test(name)) delete env[name];
  }
  return args => new Promise((resolve, reject) => {
    execFile('git', ['-C', repository, ...args], { encoding: 'buffer', env, timeout: 30_000, maxBuffer: 64 * 1024 * 1024 },
      (error, stdout) => error ? reject(error) : resolve(stdout));
  });
}

interface Event { kind?: unknown; payload?: Record<string, unknown> }

/** Event kinds the watcher reads; an inline entry naming none is skipped unparsed. */
const WATCHED = ['Collaboration', 'PullRequestSubmitted', 'PullRequestReviewed', 'EffectRequested', 'EffectObserved'];
const MERGE_EFFECT = 'merge_pull_request';

/** The events of one journal entry: inline, or packed into ledger objects. */
async function entryEvents(git: Git, revision: string, path: string): Promise<Event[]> {
  const raw = (await git(['cat-file', 'blob', `${revision}:${path}`])).toString('utf8');
  // Most entries carry no watched event; skip them without parsing.
  if (!WATCHED.some(kind => raw.includes(kind)) && !raw.includes('"entry"')) return [];
  const wrapper = JSON.parse(raw) as { record?: { events?: Event[] }; entry?: { chunks?: unknown } };
  let record = wrapper.record;
  if (!record && wrapper.entry) {
    const chunks = wrapper.entry.chunks;
    if (!Array.isArray(chunks) || !chunks.every(c => typeof c === 'string' && /^[0-9a-f]{64}$/.test(c))) return [];
    const parts = [];
    for (const sha of chunks as string[]) parts.push(await git(['cat-file', 'blob', `${revision}:${OBJECTS}${sha.slice(0, 2)}/${sha}`]));
    record = JSON.parse(Buffer.concat(parts).toString('utf8')) as { events?: Event[] };
  }
  return Array.isArray(record?.events) ? record.events : [];
}

function actorOf(value: unknown): string | undefined {
  const actor = (value as { actor?: unknown } | undefined)?.actor;
  return typeof actor === 'string' && ID.test(actor) ? actor : undefined;
}

/** Paths under `prefix` at `revision` whose content contains `text`; none when git grep finds nothing. */
async function grepPaths(git: Git, revision: string, text: string, prefix: string): Promise<string[]> {
  let listed: string;
  try {
    listed = (await git(['grep', '-l', '-F', '-e', text, revision, '--', prefix])).toString('utf8');
  } catch {
    return []; // git grep exits 1 when nothing matches
  }
  return listed.split('\n')
    .map(line => line.startsWith(revision + ':') ? line.slice(revision.length + 1) : '')
    .filter(Boolean);
}

/**
 * The payload of the first event of `kind` whose `field` is `id` in the WI's
 * journal (an offer, a round's registration, a merge request). An inline entry
 * names the ID itself; a packed entry names only its chunk hashes, so the
 * chunks that contain the ID lead to their entries.
 */
async function findPayload(git: Git, revision: string, wi: string, kind: string, field: string, id: string):
  Promise<Record<string, unknown> | undefined> {
  const journal = `${JOURNALS}${wi}/`;
  const search = async (paths: Iterable<string>) => {
    for (const path of paths) {
      if (!ENTRY.test(path)) continue;
      for (const event of await entryEvents(git, revision, path)) {
        if (event.kind === kind && event.payload?.[field] === id) return event.payload;
      }
    }
    return undefined;
  };
  const inline = await search(await grepPaths(git, revision, id, journal));
  if (inline) return inline;
  const packed = new Set<string>();
  for (const object of await grepPaths(git, revision, id, OBJECTS)) {
    const chunk = basename(object);
    if (/^[0-9a-f]{64}$/.test(chunk)) for (const path of await grepPaths(git, revision, chunk, journal)) packed.add(path);
  }
  return search(packed);
}

/** Agents a collaboration entry added between `from` and `to` concerns. */
export async function collaborationActors(git: Git, from: string, to: string, log: Log = () => {}): Promise<Set<string>> {
  const actors = new Set<string>();
  const listed = (await git(['diff', '--name-only', '-z', '--no-renames', '--diff-filter=A', from, to, '--', JOURNALS]))
    .toString('utf8').split('\0').filter(path => ENTRY.test(path));
  if (listed.length > MAX_ENTRIES) {
    log({ level: 'warn', msg: 'collaboration watch: too many new journal entries; left to the schedule', entries: listed.length });
    return actors;
  }
  const offers = new Map<string, string>();
  const answered: Array<{ wi: string; contract: string }> = [];
  /** candidate ID -> registering Agent; merge effect ID -> candidate ID, from this range */
  const submitters = new Map<string, string>();
  const merges = new Map<string, string>();
  const reviewed: Array<{ wi: string; candidate: string }> = [];
  const observed: Array<{ wi: string; effect: string }> = [];
  for (const path of listed) {
    const wi = ENTRY.exec(path)![1]!;
    let events: Event[];
    try {
      events = await entryEvents(git, to, path);
    } catch (error) {
      log({ level: 'warn', msg: 'collaboration watch: journal entry unreadable', path, error: String(error) });
      continue;
    }
    for (const { kind, payload } of events) {
      if (!payload) continue;
      const contract = payload['contract_id'];
      if (kind === 'CollaborationOffered') {
        const recipients = Array.isArray(payload['recipients']) ? payload['recipients'] : [];
        for (const r of recipients) {
          const actor = actorOf((r as { principal?: unknown }).principal);
          if (actor) actors.add(actor);
        }
        const by = actorOf(payload['offered_by']);
        if (by && typeof contract === 'string') offers.set(contract, by);
      } else if (kind === 'CollaborationResponded' && payload['decision'] !== 'accept' && typeof contract === 'string') {
        answered.push({ wi, contract });
      }
      const candidate = payload['candidate_id'];
      const effect = payload['effect_id'];
      if (kind === 'PullRequestSubmitted' && typeof candidate === 'string') {
        const by = actorOf(payload['author']);
        if (by) submitters.set(candidate, by);
      } else if (kind === 'PullRequestReviewed' && typeof candidate === 'string') {
        reviewed.push({ wi, candidate });
      } else if (kind === 'EffectRequested' && payload['effect_kind'] === MERGE_EFFECT
        && typeof effect === 'string' && typeof candidate === 'string') {
        merges.set(effect, candidate);
      } else if (kind === 'EffectObserved' && typeof effect === 'string' && effect.endsWith('.merge')) {
        observed.push({ wi, effect });
      }
    }
  }
  for (const { wi, contract } of answered) {
    const actor = offers.get(contract)
      ?? actorOf((await findPayload(git, to, wi, 'CollaborationOffered', 'contract_id', contract))?.['offered_by']);
    if (actor) actors.add(actor);
  }
  for (const { wi, effect } of observed) {
    const candidate = merges.get(effect)
      ?? (await findPayload(git, to, wi, 'EffectRequested', 'effect_id', effect))?.['candidate_id'];
    if (typeof candidate === 'string') reviewed.push({ wi, candidate });
  }
  for (const { wi, candidate } of reviewed) {
    const actor = submitters.get(candidate)
      ?? actorOf((await findPayload(git, to, wi, 'PullRequestSubmitted', 'candidate_id', candidate))?.['author']);
    if (actor) actors.add(actor);
  }
  return actors;
}

export interface CollabLedgerWatchDeps {
  repository: string;
  trackingRef: string;
  /** The scheduler's local jobs. */
  jobs(): ScheduledJob[];
  trigger(id: string): JobTriggerStatus;
  git?: Git;
  now?: () => number;
  log?: Log;
}

export function createCollabLedgerWatch(deps: CollabLedgerWatchDeps) {
  const git = deps.git ?? localGit(deps.repository);
  const now = deps.now ?? Date.now;
  const log = deps.log ?? (event => console.log({ ts: new Date().toISOString(), module: 'collab-ledger-watch', ...event }));
  /** job id -> first moment it was owed a run */
  const pending = new Map<string, number>();
  const lastStart = new Map<string, number>();
  let refFiles: string[] | undefined;
  let signature: string | undefined;
  let baseline: string | undefined;
  let busy = false;
  let timer: NodeJS.Timeout | undefined;

  /** Loose ref and packed-refs: git rewrites one of them whenever the ref moves. */
  function refSignature(): string {
    return refFiles!.map(path => {
      try {
        const s = statSync(path);
        return `${s.ino}:${s.size}:${s.mtimeMs}`;
      } catch {
        return '-';
      }
    }).join('|');
  }

  async function head(): Promise<string> {
    const sha = (await git(['rev-parse', '--verify', '--quiet', `${deps.trackingRef}^{commit}`])).toString('utf8').trim();
    if (!SHA.test(sha)) throw new Error('tracking ref is not a commit');
    return sha;
  }

  function owe(actors: Set<string>): void {
    for (const job of deps.jobs()) {
      const actor = job.enabled ? collabInboxActor(job) : undefined;
      if (actor && actors.has(actor) && !pending.has(job.id)) pending.set(job.id, now());
    }
  }

  function startOwed(): void {
    for (const [id, since] of [...pending]) {
      if (now() - (lastStart.get(id) ?? -Infinity) < MIN_INTERVAL_MS) continue;
      const status = deps.trigger(id);
      if (status === 'started') {
        lastStart.set(id, now());
        pending.delete(id);
        log({ level: 'info', msg: 'collaboration inbox run triggered', job: id, waitedMs: now() - since });
      } else if (status === 'disabled' || status === 'not_found' || now() - since > PENDING_LIMIT_MS) {
        pending.delete(id);
      }
    }
  }

  async function tick(): Promise<void> {
    if (busy) return;
    busy = true;
    try {
      if (!refFiles) {
        const common = (await git(['rev-parse', '--path-format=absolute', '--git-common-dir'])).toString('utf8').trim();
        refFiles = [join(common, deps.trackingRef), join(common, 'packed-refs')];
      }
      const current = refSignature();
      if (current !== signature) {
        const sha = await head();
        signature = current;
        // Advance first: an unreadable range is never retried, the schedule covers it.
        const from = baseline;
        baseline = sha;
        if (from !== undefined && from !== sha) owe(await collaborationActors(git, from, sha, log));
      }
    } catch (error) {
      log({ level: 'warn', msg: 'collaboration watch read failed; the schedule still covers it', error: String(error) });
    } finally {
      busy = false;
    }
    startOwed();
  }

  return {
    tick,
    /** Run these Agents' inbox jobs as for a ledger event. */
    request(actors: Iterable<string>): void {
      owe(new Set(actors));
      startOwed();
    },
    start(intervalMs = WATCH_INTERVAL_MS): void {
      if (timer) return;
      void tick();
      timer = setInterval(() => { void tick(); }, intervalMs);
      timer.unref?.();
    },
    stop(): void { if (timer) clearInterval(timer); timer = undefined; },
  };
}

export type CollabLedgerWatch = ReturnType<typeof createCollabLedgerWatch>;
