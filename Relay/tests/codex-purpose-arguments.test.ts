import { describe, expect, it } from 'vitest';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { codexPurposeArguments, REQUEST_PURPOSE_SERVER } from '../src/native-core-runtime.js';

describe('codex request-purpose server', () => {
  it('registers declare_session_wi from the release server, pre-approved', () => {
    const dir = mkdtempSync(join(tmpdir(), 'purpose-')); const server = join(dir, 'codex-mcp-request-purpose.py');
    writeFileSync(server, '');
    const args = codexPurposeArguments('/py', server);
    expect(args).toEqual([
      '-c', 'mcp_servers.pulse-request-purpose.command="/py"',
      '-c', `mcp_servers.pulse-request-purpose.args=${JSON.stringify([server])}`,
      '-c', 'mcp_servers.pulse-request-purpose.enabled_tools=["declare_session_wi"]',
      '-c', 'mcp_servers.pulse-request-purpose.tools.declare_session_wi.approval_mode="approve"',
    ]);
  });
  it('adds nothing when the release has no server', () => {
    expect(codexPurposeArguments('/py', join(tmpdir(), 'absent', 'server.py'))).toEqual([]);
  });
  it('resolves the server inside this release', () => {
    expect(REQUEST_PURPOSE_SERVER.endsWith(join('ComputeCost', 'runtime', 'codex-mcp-request-purpose.py'))).toBe(true);
  });
});
