/**
 * Run Relay with Duke bot for local testing.
 * Usage: TELEGRAM_BOT_TOKEN=xxx npx tsx scripts/run-duke.ts
 */

import { startRelay } from '../src/index.js';

const TOKEN = process.env['TELEGRAM_BOT_TOKEN'];
if (!TOKEN) {
  console.error('Set TELEGRAM_BOT_TOKEN env var first');
  process.exit(1);
}

console.log('Starting Relay with Duke bot...');
console.log('Tenants: /tmp/relay-duke-test/tenants.json');
console.log('Metering: /tmp/relay-duke-test/metering/');
console.log('Press Ctrl+C to stop.\n');

const relay = await startRelay({
  tenantsJsonPath: '/tmp/relay-duke-test/tenants.json',
  meteringDir: '/tmp/relay-duke-test/metering/',
});

// Graceful shutdown
process.on('SIGINT', () => {
  console.log('\nStopping...');
  relay.stop();
  process.exit(0);
});
process.on('SIGTERM', () => {
  relay.stop();
  process.exit(0);
});
