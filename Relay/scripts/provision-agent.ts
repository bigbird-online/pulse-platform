#!/usr/bin/env node
// Compatibility shim. The maintained CLI implementation lives in src/provision-agent.ts
// and is compiled by `npm run build` to dist/provision-agent.js.
//
// Per pulse-space/memo/decision-2026-05-01-relay-jobs-packaging.md, this shim
// is retained for zero-build dev workflows (`npx tsx scripts/provision-agent.ts ...`).

import { main } from '../src/provision-agent.js';

try {
  process.exitCode = await main(process.argv.slice(2));
} catch (err) {
  console.error(err);
  process.exitCode = 1;
}
