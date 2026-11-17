#!/usr/bin/env node
import { resolveRelayDataDir } from './config-paths.js';
import { enableWorkProcessArchive } from './work-process-archive.js';
// Relay CLI entry point

import { appendFileSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { startRelay } from './index.js';
import { createStateManager } from './state.js';
import { enableFeishuOutboundPersistence, flushFeishuOutboundCache } from './feishu-outbound-cache.js';
import { detectRapidRestart, writeRestartMarker } from './restart-detect.js';
import { formatFatalErrorLog } from './fatal-log.js';
import { classifyRelayCliInvocation, RELAY_CLI_USAGE } from './cli-invocation.js';
import { assertRelayStopped, removeOwnedRelayPid } from './relay-process-owner.js';
import { loadRelaySecrets } from './relay-secrets.js';

const RELAY_DIR = resolveRelayDataDir();
const PID_FILE = join(RELAY_DIR, 'relay.pid');
const STATE_FILE = join(RELAY_DIR, 'state.json');
const FEISHU_OUTBOUND_CACHE_FILE = join(RELAY_DIR, 'feishu-outbound-cache.json');
const RESTART_MARKER = join(RELAY_DIR, '.restart-marker');
const RESTART_AUDIT_LOG = join(RELAY_DIR, 'restart-audit.log');

/** Append a structured audit line tracking restart events. Used to identify
 *  who/what triggered a restart after the fact (memo 2026-06-24).
 *  Process-level (works on both ownermac launchd and vultr systemd; the
 *  vultr unit also writes complementary lines via ExecStartPre/StopPost). */
function writeAuditLine(event: 'START' | 'STOP', extra: Record<string, unknown> = {}): void {
  try {
    mkdirSync(RELAY_DIR, { recursive: true });
    const line = JSON.stringify({
      ts: new Date().toISOString(),
      event,
      pid: process.pid,
      ppid: process.ppid,
      user: process.env['USER'] ?? process.env['LOGNAME'] ?? '?',
      sudoUser: process.env['SUDO_USER'] ?? null,
      ...extra,
    }) + '\n';
    appendFileSync(RESTART_AUDIT_LOG, line);
  } catch { /* best effort, never block startup/shutdown */ }
}

function writePidFile(): void {
  mkdirSync(RELAY_DIR, { recursive: true });
  writeFileSync(PID_FILE, String(process.pid));
}

function removePidFile(): void {
  try { removeOwnedRelayPid(PID_FILE); } catch { /* best effort */ }
}

// --- Main ---

// Classify the invocation before audit/PID/process cleanup. The systemd service
// starts Relay with no arguments; help, version, and invalid arguments are
// deliberately terminal paths with zero Relay lifecycle side effects.
const cliInvocation = classifyRelayCliInvocation(process.argv.slice(2));
if (cliInvocation.kind === 'help') {
  console.log(RELAY_CLI_USAGE);
  process.exit(0);
}
if (cliInvocation.kind === 'version') {
  const packageJson = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf-8')) as { version?: string };
  console.log(packageJson.version ?? 'unknown');
  process.exit(0);
}
if (cliInvocation.kind === 'invalid') {
  console.error(`Unknown Relay argument: ${cliInvocation.argument || '(empty)'}`);
  console.error(RELAY_CLI_USAGE);
  process.exit(2);
}

// Credentials come from the declared private env file, before any other
// configuration is read. A declared file that cannot be trusted stops here.
try {
  loadRelaySecrets();
} catch (error) {
  console.error(`Relay refused to start: ${error instanceof Error ? error.message : 'secrets env file unavailable'}`);
  process.exit(78);
}

assertRelayStopped(PID_FILE);

// Audit: process-level start log (works on both ownermac/launchd and vultr/systemd).
writeAuditLine('START', { argv: process.argv.slice(2) });

writePidFile();

// Initialize state manager
const stateManager = createStateManager(STATE_FILE);
stateManager.load();

// Persist Feishu outbound text cache so replies to cards sent before a restart
// still resolve to real content (otherwise they fall back to an API placeholder).
enableFeishuOutboundPersistence(FEISHU_OUTBOUND_CACHE_FILE);
try { enableWorkProcessArchive(join(RELAY_DIR, 'work-process')); }
catch { console.warn({ msg: 'Work process history unavailable; current view remains usable' }); }

// Detect rapid restart (self-restart loop protection)
const isRapidRestart = detectRapidRestart(RESTART_MARKER);
if (isRapidRestart) {
  console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'Rapid restart detected (< 10s since last shutdown)' });
}

const relay = await startRelay({ stateManager });

// Graceful shutdown
//
// Ordering matters — see memo 2026-06-24 (relay-busy-cc-sigterm-loses-jsonl-tail):
//   1. Write restart marker (loop protection)
//   2. Drain busy CC sessions (up to 30s) — let in-flight turns flush jsonl
//      before we SIGTERM the subprocess. Without this, a "busy CC at SIGTERM"
//      loses its tail (streaming card text + outbox cache survive, but jsonl
//      doesn't), causing the resumed CC to forget the reply it just sent.
//   3. notifyShutdown — for busy sessions still not drained, fires
//      onShutdownInterrupt (wasBusyAtShutdown=true) AND appends an
//      "unfinished-turn-on-shutdown" marker to their jsonl, so the resumed
//      CC sees the gap rather than silently missing the last turn.
//   4. Flush state.json + feishu outbox cache
//   5. Stop all services (this is where CC subprocesses receive SIGTERM)
//   6. Exit
//
// systemd TimeoutStopSec defaults to 90s, which covers the 30s drain + the
// rest of the shutdown sequence comfortably.
const SHUTDOWN_DRAIN_TIMEOUT_MS = 30_000;

async function shutdown(signal?: string): Promise<void> {
  console.log({ ts: new Date().toISOString(), level: 'info', msg: 'Shutting down...', signal: signal ?? 'unknown' });
  // Audit: process-level stop log — captures the signal that triggered shutdown
  // (the systemd ExecStopPost line on vultr is complementary, this one works
  // on ownermac too where launchd has no equivalent hook).
  writeAuditLine('STOP', { signal: signal ?? 'unknown' });

  // 1. Write restart marker for loop detection
  writeRestartMarker(RESTART_MARKER);

  // 2. Drain busy CC sessions — give in-flight turns a chance to write the
  //    `result` line (which flushes jsonl) before we kill the subprocess.
  if (relay.drainBusySessions) {
    try {
      const stillBusy = await relay.drainBusySessions(SHUTDOWN_DRAIN_TIMEOUT_MS);
      if (stillBusy.size > 0) {
        console.log({
          ts: new Date().toISOString(),
          level: 'warn',
          msg: 'Drain timeout — some CC sessions still busy at SIGTERM, jsonl tail may be incomplete',
          stillBusy: Array.from(stillBusy),
        });
      }
    } catch (err) {
      console.log({
        ts: new Date().toISOString(),
        level: 'warn',
        msg: 'drainBusySessions threw (non-fatal, proceeding to shutdown)',
        error: String(err).slice(0, 200),
      });
    }
  }

  // 3. notifyShutdown — for any sessions still busy after drain, fires
  //    onShutdownInterrupt (writes wasBusyAtShutdown=true to state) AND writes
  //    an unfinished-turn marker to jsonl. Must run BEFORE stateManager.flush()
  //    so the next-restart notifyResumed path sees the wake signal.
  if (relay.notifyShutdown) {
    try {
      await Promise.race([
        relay.notifyShutdown(),
        new Promise(resolve => setTimeout(resolve, 3000)),
      ]);
    } catch {
      // Best effort
    }
  }

  // 4. Now flush state — captures wasBusyAtShutdown writes from step 3
  //    (updateSession schedules a debounced save; flush forces synchronous write).
  stateManager.flush();
  flushFeishuOutboundCache();

  // 5. Stop all services (this propagates SIGTERM to CC subprocesses)
  relay.stop();
  // Intake is closed now; save anything recorded since the flush above.
  stateManager.flush();
  stateManager.dispose();
  removePidFile();

  setTimeout(() => process.exit(0), 1000);
}

process.on('SIGTERM', () => { shutdown('SIGTERM').catch(() => process.exit(0)); });
process.on('SIGINT', () => { shutdown('SIGINT').catch(() => process.exit(0)); });
process.on('SIGHUP', () => {
  console.log({ ts: new Date().toISOString(), level: 'info', msg: 'SIGHUP received, reloading scheduler' });
  try {
    relay.reloadScheduler?.();
  } catch (error) {
    // Keep the last known-good writers running; an invalid placement must not
    // replace them and must leave an observable error instead of killing Relay.
    console.error({ ts: new Date().toISOString(), level: 'error', msg: 'SIGHUP reload rejected by preflight', error: String(error) });
  }
});

// An unhandled promise rejection from a single tenant's async path (e.g. a 429
// fallback chain) must NOT take down all bots. Node 22's default mode would
// surface these through uncaughtException → process.exit(1), which crash-loops
// the whole Relay and strands every in-flight Feishu card in "Generating".
// A rejection (unlike a true uncaughtException) does not corrupt the V8 state,
// so we log it with full stack and keep serving.
process.on('unhandledRejection', (reason) => {
  const err = reason instanceof Error ? reason : new Error(String(reason));
  console.error({
    ts: new Date().toISOString(),
    level: 'error',
    msg: 'Unhandled promise rejection (contained, process kept alive)',
    error: String(err),
    stack: err.stack,
  });
});

process.on('uncaughtException', (err) => {
  console.error(formatFatalErrorLog(err));
  stateManager.flush();
  flushFeishuOutboundCache();
  relay.stop();
  stateManager.dispose();
  removePidFile();
  setTimeout(() => process.exit(1), 2000);
});
