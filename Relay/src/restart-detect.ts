import { readFileSync, writeFileSync, existsSync } from 'node:fs';

const RAPID_RESTART_THRESHOLD_MS = 10_000;

export function writeRestartMarker(markerPath: string): void {
  try {
    writeFileSync(markerPath, JSON.stringify({ ts: Date.now() }));
  } catch {
    // Non-critical — best effort
  }
}

export function detectRapidRestart(markerPath: string): boolean {
  if (!existsSync(markerPath)) return false;
  try {
    const data = JSON.parse(readFileSync(markerPath, 'utf-8')) as { ts: number };
    return Date.now() - data.ts < RAPID_RESTART_THRESHOLD_MS;
  } catch {
    return false;
  }
}

const RESUME_NOTICE_COOLDOWN_MS = 15 * 60 * 1000;

/**
 * Returns true at most once per cooldown window (marker persisted across restarts),
 * so restart storms (e.g. several config-change restarts in a row) notify users only once.
 */
export function consumeResumeNoticeBudget(markerPath: string, cooldownMs: number = RESUME_NOTICE_COOLDOWN_MS): boolean {
  try {
    if (existsSync(markerPath)) {
      const data = JSON.parse(readFileSync(markerPath, 'utf-8')) as { ts: number };
      if (Date.now() - data.ts < cooldownMs) return false;
    }
  } catch {
    // corrupt marker — treat as absent
  }
  try {
    writeFileSync(markerPath, JSON.stringify({ ts: Date.now() }));
  } catch {
    // best effort
  }
  return true;
}
