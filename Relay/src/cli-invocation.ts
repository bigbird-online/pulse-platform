export type RelayCliInvocation =
  | { kind: 'serve' }
  | { kind: 'help' }
  | { kind: 'version' }
  | { kind: 'invalid'; argument: string };

/**
 * The deployed service contract is the historical no-argument invocation.
 * Everything else is informational or invalid and must be classified before
 * any Relay lifecycle side effect occurs.
 */
export function classifyRelayCliInvocation(argv: string[]): RelayCliInvocation {
  if (argv.length === 0) return { kind: 'serve' };
  if (argv.length === 1 && (argv[0] === '--help' || argv[0] === '-h')) return { kind: 'help' };
  if (argv.length === 1 && (argv[0] === '--version' || argv[0] === '-V')) return { kind: 'version' };
  return { kind: 'invalid', argument: argv[0] ?? '' };
}

export const RELAY_CLI_USAGE = [
  'Usage: relay [options]',
  '',
  'With no options, starts the Relay service.',
  '',
  'Options:',
  '  -h, --help     Show this help text',
  '  -V, --version  Show the Relay version',
].join('\n');
