#!/usr/bin/env node
// compute-profile — read-only resolver for the central compute profiles used by
// the resident Relay. Managed entry: `pulse-relay compute-profile resolve ...`.

import { pathToFileURL } from 'node:url';
import { resolveProviderComputeProfile, type ComputeProvider } from './compute-profiles.js';

const USAGE = 'usage: compute-profile resolve <name> --provider cc|codex [--json]';

export function main(argv = process.argv.slice(2), out = console.log, err = console.error): number {
  const [command, name, ...rest] = argv;
  let provider: string | undefined;
  let json = false;
  for (let index = 0; index < rest.length; index++) {
    if (rest[index] === '--provider' && index + 1 < rest.length) provider = rest[++index];
    else if (rest[index] === '--json') json = true;
    else { err(USAGE); return 64; }
  }
  if (command !== 'resolve' || !name || name.startsWith('--') || (provider !== 'cc' && provider !== 'codex')) {
    err(USAGE);
    return 64;
  }
  try {
    const resolved = resolveProviderComputeProfile(name, provider as ComputeProvider);
    out(json
      ? JSON.stringify(resolved)
      : `${resolved.profile} ${resolved.provider}: model=${resolved.model} effort=${resolved.reasoning_effort ?? '-'} alias=${resolved.model_alias ?? '-'}`);
    return 0;
  } catch (error) {
    err(`compute profile resolution failed: ${error instanceof Error ? error.message : String(error)}`);
    return 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = main();
}
