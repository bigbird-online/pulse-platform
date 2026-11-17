// fatal-log.ts — uncaught-exception log formatting (split out for testability)

/**
 * Why this is its own module: cli.ts is an entry point with dangerous
 * top-level side effects (it kills stale Relay/CC processes and calls
 * startRelay on import), so it cannot be imported by unit tests. The
 * uncaughtException handler previously logged `String(err)`, which drops the
 * stack trace — so a recurring `TypeError: ...reading 'toLowerCase'` crash
 * could not be located (no file, no line). Splitting the formatting out makes
 * it unit-testable.
 */
export interface FatalErrorLog {
  ts: string;
  level: 'fatal';
  msg: string;
  error: string;
}

export function formatFatalErrorLog(err: unknown): FatalErrorLog {
  const error = err instanceof Error
    ? (err.stack ?? String(err))
    : String(err);
  return {
    ts: new Date().toISOString(),
    level: 'fatal',
    msg: 'Uncaught exception',
    error,
  };
}
