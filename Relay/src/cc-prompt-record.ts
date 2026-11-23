/**
 * The `ccPrompt` a CC session runs under, fixed at the session boundary.
 *
 * CC keeps the system prompt it recorded when a session began, while the
 * `--settings` of each process (`includeGitInstructions`) apply at once. So a new
 * CC session that opts into a trim records it, and a resumed session gets the
 * recorded value back instead of the Agent's current one: the Git section of the
 * Bash tool and Relay's `CC_GIT_RULES` change together, at the next new session.
 *
 * Every failure falls back to CC's default, which always keeps CC's own Git
 * section, and never stops the session from starting:
 * - a session on the default writes nothing, exactly as before records existed;
 * - a new session applies a trim only once its record is written; otherwise it
 *   starts on the default and the failure is logged;
 * - a resumed session without a record began on the default (or before records
 *   existed) and stays there; a record that cannot be read, parsed or holds an
 *   unknown value is logged and the session resumes on the default.
 */
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { CcPromptOptions } from './cc-session-settings.js';

const SESSION_ID = /^[A-Za-z0-9-]{1,128}$/;

export type CcPromptWarn = (entry: Record<string, unknown>) => void;

const defaultWarn: CcPromptWarn = (entry) =>
  console.warn({ ts: new Date().toISOString(), level: 'warn', ...entry });

/** The trims in `value`, or undefined when it asks for none. Throws on an unknown key or value. */
function parseOptions(value: unknown): CcPromptOptions | undefined {
  if (value === null || value === undefined) return undefined;
  if (typeof value !== 'object' || Array.isArray(value)) throw new Error('ccPrompt is not an object');
  const raw = value as Record<string, unknown>;
  for (const key of Object.keys(raw)) {
    if (key !== 'systemPrompt' && key !== 'gitStatus') throw new Error(`unknown ccPrompt key ${key}`);
  }
  if (raw['systemPrompt'] !== undefined && raw['systemPrompt'] !== 'append' && raw['systemPrompt'] !== 'replace') {
    throw new Error('ccPrompt.systemPrompt is neither append nor replace');
  }
  if (raw['gitStatus'] !== undefined && typeof raw['gitStatus'] !== 'boolean') throw new Error('ccPrompt.gitStatus is not a boolean');
  const options: CcPromptOptions = {};
  if (raw['systemPrompt'] === 'replace') options.systemPrompt = 'replace';
  if (raw['gitStatus'] === false) options.gitStatus = false;
  return Object.keys(options).length ? options : undefined;
}

/**
 * The `ccPrompt` for this spawn: the recorded one when CC resumes `sessionId`,
 * else `current`, applied only once recorded for later resumes. Never throws.
 */
export function ccPromptForSession(
  dir: string | undefined, sessionId: string, resuming: boolean, current: unknown,
  warn: CcPromptWarn = defaultWarn,
): CcPromptOptions | undefined {
  if (resuming) return recorded(dir, sessionId, warn);
  let options: CcPromptOptions | undefined;
  try { options = parseOptions(current); }
  catch (error) {
    warn({ msg: 'ccPrompt config invalid; new CC session starts on the default prompt', sessionId, error: String(error) });
    return undefined;
  }
  if (!options) return undefined;
  if (!dir || !SESSION_ID.test(sessionId)) {
    warn({ msg: 'ccPrompt not recorded (no record directory or unusable session id); new CC session starts on the default prompt', sessionId });
    return undefined;
  }
  const file = join(dir, `${sessionId}.json`);
  const temp = `${file}.${process.pid}.tmp`;
  try {
    mkdirSync(dir, { recursive: true });
    writeFileSync(temp, JSON.stringify({ ccPrompt: options, recordedAt: new Date().toISOString() }) + '\n');
    renameSync(temp, file);
    return options;
  } catch (error) {
    try { rmSync(temp, { force: true }); } catch { /* the directory itself is unusable */ }
    warn({ msg: 'ccPrompt record not written; new CC session starts on the default prompt', sessionId, file, error: String(error) });
    return undefined;
  }
}

function recorded(dir: string | undefined, sessionId: string, warn: CcPromptWarn): CcPromptOptions | undefined {
  if (!dir || !SESSION_ID.test(sessionId)) return undefined;
  const file = join(dir, `${sessionId}.json`);
  let text: string;
  try { text = readFileSync(file, 'utf-8'); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
    warn({ msg: 'ccPrompt record unreadable; resumed CC session keeps CC\'s default Git section and settings', sessionId, file, error: String(error) });
    return undefined;
  }
  try {
    const record = JSON.parse(text) as unknown;
    if (!record || typeof record !== 'object' || !('ccPrompt' in record)) throw new Error('record has no ccPrompt');
    return parseOptions((record as { ccPrompt: unknown }).ccPrompt);
  } catch (error) {
    warn({ msg: 'ccPrompt record invalid; resumed CC session keeps CC\'s default Git section and settings', sessionId, file, error: String(error) });
    return undefined;
  }
}
