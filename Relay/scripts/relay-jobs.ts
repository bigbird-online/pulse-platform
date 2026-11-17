#!/usr/bin/env node
// Compatibility shim. The maintained CLI implementation lives in src/relay-jobs.ts
// and is compiled by `npm run build` to dist/relay-jobs.js.

import { main } from '../src/relay-jobs.js';

try {
  process.exitCode = await main(process.argv.slice(2));
} catch (err) {
  console.error(err);
  process.exitCode = 1;
}
