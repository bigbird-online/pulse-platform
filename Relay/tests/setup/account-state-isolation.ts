import { afterAll, afterEach } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { takeAccountStatePathViolations } from '../../src/test-account-path-guard.js';

// Each test file gets its own Host state root. The values are replaced, not
// defaulted: an Agent shell may export the production human-input directory.
const root = mkdtempSync(join(tmpdir(), 'relay-test-scl-host-'));
process.env['SCL_HUMAN_DECISION_KEY_FILE'] = join(root, 'human-decision.key');
process.env['PULSE_CORE_HUMAN_INPUTS'] = join(root, 'human-inputs');

afterEach(() => {
  const refused = takeAccountStatePathViolations();
  if (refused.length) throw new Error('Test reached account-level Host state:\n' + [...new Set(refused)].join('\n'));
});
afterAll(() => rmSync(root, { recursive: true, force: true }));
