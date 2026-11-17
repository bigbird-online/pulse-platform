/**
 * Caller proof for POST /internal/agent-jobs. Relay issues each conversation
 * it hosts a key, kept in a private file under its data directory (like the
 * native session proofs) and named to that conversation's bridge only; the
 * request is HMAC-signed with it over the key reference, a time, a one-use
 * nonce and the exact body. Relay recognises the conversation by the key it
 * verifies, never by identifiers in the body; a copied public session
 * reference, a request without a valid signature, an old time or a replayed
 * nonce is refused. The key never travels with the request.
 */
import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { lstatSync, mkdirSync, readFileSync, realpathSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export const AGENT_JOBS_PROOF_SCHEMA = 'pulse.agent_jobs_request/v1';
/** How far the signed time may differ from Relay's clock. */
export const AGENT_JOBS_PROOF_WINDOW_MS = 60_000;

export interface SignedAgentJobsRequest {
  schema: string;
  key: string;
  ts: number;
  nonce: string;
  body: string;
  sig: string;
}

export class AgentJobAuthError extends Error {}

function refuse(message: string): never { throw new AgentJobAuthError(message); }

/** The key reference: names the file, reveals nothing of the pool key. */
export function agentJobsKeyRef(poolKey: string): string {
  return createHash('sha256').update(poolKey).digest('hex');
}

function keyRoot(dataDir: string): string {
  const root = join(dataDir, 'agent-jobs-keys');
  mkdirSync(root, { recursive: true, mode: 0o700 });
  const info = statSync(root);
  if (realpathSync(root) !== root || info.uid !== process.getuid?.() || info.mode & 0o077) {
    throw new Error('agent-jobs key directory must be private');
  }
  return root;
}

/** The conversation's key file, created once; its path is what the bridge receives. */
export function issueAgentJobsKey(dataDir: string, poolKey: string): string {
  const path = join(keyRoot(dataDir), `${agentJobsKeyRef(poolKey)}.key`);
  try {
    writeFileSync(path, randomBytes(32).toString('hex'), { flag: 'wx', mode: 0o600 });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
  }
  return path;
}

function readKey(dataDir: string, ref: string): Buffer {
  const path = join(keyRoot(dataDir), `${ref}.key`);
  let info;
  try { info = lstatSync(path); } catch { refuse('unauthenticated: no key for this conversation'); }
  if (!info.isFile() || info.nlink !== 1 || info.uid !== process.getuid?.() || info.mode & 0o077) {
    refuse('unauthenticated: key file is not private');
  }
  return Buffer.from(readFileSync(path, 'utf8').trim(), 'utf8');
}

export function signAgentJobs(key: string, ref: string, ts: number, nonce: string, body: string): string {
  return createHmac('sha256', key).update(`${AGENT_JOBS_PROOF_SCHEMA}\n${ref}\n${ts}\n${nonce}\n${body}`).digest('hex');
}

/**
 * The verifier keeps the nonces it accepted for twice the window, so a
 * captured request cannot be sent again. `hostedPoolKeys` are the
 * conversations this Relay hosts now; a key of any other is refused.
 */
export function createAgentJobsVerifier(dataDir: string, now: () => number = Date.now) {
  const seen = new Map<string, number>();
  return (envelope: unknown, hostedPoolKeys: Iterable<string>): { poolKey: string; body: unknown } => {
    const e = envelope as Partial<SignedAgentJobsRequest> | null;
    if (!e || typeof e !== 'object' || e.schema !== AGENT_JOBS_PROOF_SCHEMA) refuse('unauthenticated: not a signed request');
    const { key: ref, ts, nonce, body, sig } = e;
    if (typeof ref !== 'string' || !/^[a-f0-9]{64}$/.test(ref) || typeof ts !== 'number' || !Number.isInteger(ts)
      || typeof nonce !== 'string' || !/^[a-f0-9]{32}$/.test(nonce) || typeof body !== 'string'
      || typeof sig !== 'string' || !/^[a-f0-9]{64}$/.test(sig)) refuse('unauthenticated: malformed proof');
    const poolKey = [...hostedPoolKeys].find(candidate => agentJobsKeyRef(candidate) === ref)
      ?? refuse('unauthenticated: not a conversation this Relay hosts');
    const expected = Buffer.from(signAgentJobs(readKey(dataDir, ref).toString('utf8'), ref, ts, nonce, body), 'hex');
    if (!timingSafeEqual(expected, Buffer.from(sig, 'hex'))) refuse('unauthenticated: bad signature');
    const at = now();
    if (Math.abs(at - ts) > AGENT_JOBS_PROOF_WINDOW_MS) refuse('unauthenticated: proof expired');
    for (const [used, until] of seen) if (until < at) seen.delete(used);
    if (seen.has(`${ref}:${nonce}`)) refuse('unauthenticated: proof already used');
    seen.set(`${ref}:${nonce}`, at + 2 * AGENT_JOBS_PROOF_WINDOW_MS);
    try { return { poolKey, body: JSON.parse(body) }; } catch { refuse('invalid body'); }
  };
}
