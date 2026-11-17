import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  resolvePulseCodexModel,
  resolvePulseCodexReasoningEffort,
  resolvePulseRuntimePriority,
} from '../src/codex-defaults.js';

const originalPath = process.env['PULSE_RUNTIME_PRIORITY_PATH'];

afterEach(() => {
  if (originalPath === undefined) delete process.env['PULSE_RUNTIME_PRIORITY_PATH'];
  else process.env['PULSE_RUNTIME_PRIORITY_PATH'] = originalPath;
});

describe('managed PULSE runtime defaults', () => {
  it('reads runtime order, model, and reasoning from the shared profile', () => {
    const dir = mkdtempSync(join(tmpdir(), 'pulse-runtime-profile-'));
    const path = join(dir, 'runtime-priority.json');
    writeFileSync(path, JSON.stringify({
      schema_version: 1,
      runtime_priority: ['cc', 'codex'],
      codex: { model: 'gpt-managed', reasoning_effort: 'high', yolo: true },
      claude: { model: 'claude-managed' },
    }));
    process.env['PULSE_RUNTIME_PRIORITY_PATH'] = path;
    try {
      expect(resolvePulseRuntimePriority()).toEqual(['cc', 'codex']);
      expect(resolvePulseCodexModel()).toBe('gpt-managed');
      expect(resolvePulseCodexReasoningEffort()).toBe('high');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('falls back safely when the profile is malformed', () => {
    const dir = mkdtempSync(join(tmpdir(), 'pulse-runtime-profile-bad-'));
    const path = join(dir, 'runtime-priority.json');
    writeFileSync(path, 'not-json');
    process.env['PULSE_RUNTIME_PRIORITY_PATH'] = path;
    try {
      expect(resolvePulseRuntimePriority()).toEqual(['codex']);
      expect(resolvePulseCodexModel()).toBe('gpt-6-astra');
      expect(resolvePulseCodexReasoningEffort()).toBe('xhigh');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('rejects the whole profile when any runtime or required field is invalid', () => {
    const dir = mkdtempSync(join(tmpdir(), 'pulse-runtime-profile-invalid-'));
    const path = join(dir, 'runtime-priority.json');
    writeFileSync(path, JSON.stringify({
      schema_version: 1,
      runtime_priority: ['cc', 'invalid'],
      codex: { model: 'gpt-managed', reasoning_effort: 'high', yolo: 'yes' },
      claude: { model: 'claude-managed' },
    }));
    process.env['PULSE_RUNTIME_PRIORITY_PATH'] = path;
    try {
      expect(resolvePulseRuntimePriority()).toEqual(['codex']);
      expect(resolvePulseCodexModel()).toBe('gpt-6-astra');
      expect(resolvePulseCodexReasoningEffort()).toBe('xhigh');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
