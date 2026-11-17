/**
 * Integration test: verify CC subprocess stream-json round-trip.
 * Usage: npx tsx scripts/integration-test.ts
 */

import { createPool } from '../src/pool.js';
import { createMetering } from '../src/metering.js';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const testWorkspace = join(tmpdir(), 'relay-int-test-' + Date.now());
const meteringDir = join(testWorkspace, 'metering');
mkdirSync(testWorkspace, { recursive: true });

writeFileSync(join(testWorkspace, 'CLAUDE.md'), '# Test\nReply in under 10 words. No tools.\n');

async function main() {
  console.log('=== Relay Integration Test ===\n');

  const pool = createPool({
    idleTimeoutMs: 60000,
    cleanupIntervalMs: 999999,
    killTimeoutMs: 5000,
  });

  const metering = createMetering({ meteringDir });

  const responses: Array<{ type: string; [k: string]: unknown }> = [];
  let gotResult = false;

  pool.on('response', (_tid: string, msg: { type: string; content?: string; [k: string]: unknown }) => {
    responses.push(msg);
    if (msg.type === 'assistant') {
      // CC stream-json: content is in message.content[{type:'text', text:'...'}]
      const msgObj = msg.message as { content?: Array<{ type: string; text?: string }> } | undefined;
      const text = msgObj?.content?.filter(c => c.type === 'text').map(c => c.text).join('') ?? String(msg.content ?? '');
      console.log(`  [assistant]: ${text.slice(0, 200)}`);
    }
    if (msg.type === 'result') {
      gotResult = true;
      console.log(`  [result]: cost=$${msg.total_cost_usd}`);
      metering.record('test-tenant', 'int-test-session', msg);
    }
  });

  pool.on('error', (_tid: string, err: Error) => {
    console.error(`  [error]: ${err.message}`);
  });

  // 1. Spawn
  console.log('1. Spawning CC...');
  const t0 = Date.now();
  try {
    const proc = await pool.spawn({
      tenantId: 'test-tenant',
      workspace: testWorkspace,
      sessionId: crypto.randomUUID(),
      maxTurns: 2,
    });
    console.log(`   ✅ Ready in ${((Date.now() - t0) / 1000).toFixed(1)}s (pid=${proc.process.pid})\n`);
  } catch (err) {
    console.error(`   ❌ Spawn failed: ${err}`);
    pool.dispose();
    process.exit(1);
  }

  // Debug: attach direct listeners
  const proc0 = pool.get('test-tenant')!;
  console.log(`   stdin writable: ${proc0.process.stdin?.writable}`);
  console.log(`   stdout readable: ${proc0.process.stdout?.readable}`);
  proc0.process.stdout!.on('data', (d: Buffer) => console.log(`  [raw stdout]: ${d.toString().slice(0, 200)}`));
  proc0.process.stderr!.on('data', (d: Buffer) => console.error(`  [raw stderr]: ${d.toString().slice(0, 200)}`));

  // 2. Send
  console.log('\n2. Sending: "Say hello in 3 words"');
  await pool.send('test-tenant', { type: 'human', content: 'Say hello in 3 words. Do not use any tools.' });

  // 3. Wait for result
  const deadline = Date.now() + 60000;
  while (!gotResult && Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 300));
  }

  console.log();
  if (gotResult) {
    console.log(`3. ✅ Got ${responses.length} responses in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  } else {
    console.log('3. ❌ Timeout');
  }

  // 4. Kill
  await pool.kill('test-tenant');
  pool.dispose();
  metering.dispose();

  const passed = gotResult && responses.some(r => r.type === 'assistant');
  console.log(`\n=== ${passed ? '✅ PASSED' : '❌ FAILED'} ===`);
  process.exit(passed ? 0 : 1);
}

main().catch(err => { console.error(err); process.exit(1); });
