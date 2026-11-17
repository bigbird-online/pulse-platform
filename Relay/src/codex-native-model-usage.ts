/** Read-only model evidence from the exact exec thread's appended native task.
 * No directory/time/model-name heuristic assigns another task to this dispatch.
 */
import { lstatSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { nativeObject as object, nativeLabel as label, nativeCounts as counts,
  usageKeys as keys, validateNativeModelUsage, type Counts, type ModelQuantity,
  type NativeModelUsage } from './native-model-usage.js';

const MAX_FILE_BYTES = 64 * 1024 * 1024;
const MAX_FILES = 10000;
interface Prefix { path: string; bytes: Buffer; dev: number; ino: number }
export interface CodexSourceBoundary { root: string; priorFiles: Set<string>; session?: string; prefix?: Prefix }
function files(root: string, depth = 0, found: string[] = []): string[] {
  if (depth > 6) throw new Error('source depth limit');
  const st = lstatSync(root);
  if (!st.isDirectory() || st.isSymbolicLink() || st.uid !== process.getuid?.()) throw new Error('unsafe source directory');
  for (const item of readdirSync(root, {withFileTypes: true})) {
    if (found.length >= MAX_FILES) throw new Error('source file limit');
    if (item.isSymbolicLink()) throw new Error('source symlink');
    const path = join(root, item.name);
    if (item.isDirectory()) files(path, depth + 1, found);
    else if (item.isFile() && item.name.endsWith('.jsonl')) found.push(path);
  }
  return found;
}
function read(path: string): Prefix {
  const before = lstatSync(path);
  if (!before.isFile() || before.isSymbolicLink() || before.uid !== process.getuid?.()
      || before.size > MAX_FILE_BYTES) throw new Error('unsafe native source');
  const bytes = readFileSync(path), after = lstatSync(path);
  if (before.dev !== after.dev || before.ino !== after.ino || bytes.length > MAX_FILE_BYTES
      || (bytes.length && bytes[bytes.length - 1] !== 10)) throw new Error('unstable native source');
  return {path, bytes, dev: after.dev, ino: after.ino};
}
function onlySession(paths: string[], session: string): string | undefined {
  if (!/^[a-zA-Z0-9-]{1,128}$/.test(session)) return;
  const candidates = paths.filter(p => p.endsWith(`-${session}.jsonl`) || p.endsWith(`/${session}.jsonl`));
  return candidates.length === 1 ? candidates[0] : undefined;
}
/** Capture BEFORE spawning exec; unavailable evidence never blocks a turn. */
export function captureCodexSourceBoundary(root: string, session?: string): CodexSourceBoundary | undefined {
  try {
    let paths: string[];
    try { paths = files(root); } catch (error) {
      if (!session && object(error).code === 'ENOENT') paths = [];
      else return;
    }
    const path = session && onlySession(paths, session);
    if (session && !path) return;
    return {root, priorFiles: new Set(paths), session, prefix: path ? read(path) : undefined};
  } catch { return; }
}
function rows(bytes: Buffer): Record<string, unknown>[] {
  return bytes.toString('utf8').split('\n').filter(Boolean).map(line => {
    const value: unknown = JSON.parse(line);
    if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid native row');
    return value as Record<string, unknown>;
  });
}
/** Exact thread + immutable pre-dispatch prefix + one new completed native task.
 * Native counters/contexts must partition exec's factual usage exactly. A
 * competing task, rewrite, missing model or unexplained reset yields no claim.
 */
export function codexNativeModelUsage(boundary: CodexSourceBoundary | undefined,
  session: string | undefined, expected: unknown): NativeModelUsage | undefined {
  try {
    if (!boundary || !session || (boundary.session && boundary.session !== session)) return;
    const path = onlySession(files(boundary.root), session);
    if (!path) return;
    const final = read(path), prior = boundary.prefix;
    if (prior) {
      if (prior.path !== path || prior.dev !== final.dev || prior.ino !== final.ino
          || !final.bytes.subarray(0, prior.bytes.length).equals(prior.bytes)) return;
    } else if (boundary.priorFiles.has(path)) return;
    const meta = rows(final.bytes).find(r => r.type === 'session_meta');
    if (object(meta?.payload).id !== session) return;
    const before = prior ? rows(prior.bytes) : [];
    let previous: Counts = Object.fromEntries(keys('codex').map(k => [k, 0]));
    for (const row of before) {
      const payload = object(row.payload);
      if (payload.type === 'token_count') {
        const next = counts(object(payload.info).total_token_usage, 'codex');
        if (!next) return;
        previous = next;
      }
    }
    const added = rows(final.bytes.subarray(prior?.bytes.length ?? 0));
    let turn: string | undefined, completed = false, starts = 0, model: string | null = null;
    let provider: string | null = null, updates = 0, modelChanged = false;
    const models: ModelQuantity[] = [];
    for (const row of added) {
      const payload = object(row.payload);
      if (payload.type === 'task_started') {
        if (++starts !== 1 || !label(payload.turn_id)) return;
        turn = payload.turn_id as string;
      } else if (row.type === 'turn_context' && turn && !completed) {
        if (payload.turn_id !== undefined && payload.turn_id !== turn) return;
        const nextModel = label(payload.model), nextProvider = label(payload.model_provider);
        modelChanged ||= model !== null && (nextModel !== model || nextProvider !== provider);
        model = nextModel; provider = nextProvider;
      } else if (payload.type === 'token_count' && turn && !completed) {
        const info = object(payload.info), total = counts(info.total_token_usage, 'codex');
        const last = counts(info.last_token_usage, 'codex');
        if (!total || !model) return;
        if (keys('codex').some(k => total[k]! < previous[k]!)) {
          if (updates || !last || keys('codex').some(k => total[k] !== last[k])) return;
          previous = Object.fromEntries(keys('codex').map(k => [k, 0]));
        }
        const delta = Object.fromEntries(keys('codex').map(k => [k, total[k]! - previous[k]!]));
        const nonzero = Object.values(delta).some(n => n !== 0);
        if (nonzero && modelChanged && (!last || keys('codex').some(k => delta[k] !== last[k]))) return;
        if (nonzero) models.push({model, provider, usage: delta});
        previous = total; updates++;
        // A repeated cumulative count cannot partition a model switch.
        if (nonzero) modelChanged = false;
      } else if (payload.type === 'task_complete') {
        if (!turn || (payload.turn_id !== undefined && payload.turn_id !== turn) || completed) return;
        completed = true;
      } else if (['context_compacted', 'compaction', 'auto_compaction_start'].includes(String(payload.type ?? row.type))) {
        return;
      }
    }
    if (starts !== 1 || !completed || !updates) return;
    return validateNativeModelUsage({schema: 'pulse_native_model_usage/v1', runtime: 'codex', models}, expected, 'codex');
  } catch { return; }
}
