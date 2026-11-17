import { existsSync, mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  resolveToolFileOutboxPath,
  scanToolFileOutbox,
  validateToolFileOutboxPath,
} from '../src/tool-file-outbox.js';

const roots: string[] = [];
const originalOutboxRoot = process.env['RELAY_OUTBOX_DIR'];

afterEach(() => {
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
  if (originalOutboxRoot === undefined) delete process.env['RELAY_OUTBOX_DIR'];
  else process.env['RELAY_OUTBOX_DIR'] = originalOutboxRoot;
});

function fixture(): { root: string; workspace: string; outbox: string } {
  delete process.env['RELAY_OUTBOX_DIR'];
  const root = mkdtempSync(join(tmpdir(), 'relay-tool-file-outbox-'));
  roots.push(root);
  const workspace = join(root, 'workspace');
  mkdirSync(workspace, { recursive: true });
  const outbox = resolveToolFileOutboxPath('user:agent', workspace);
  mkdirSync(outbox, { recursive: true });
  return { root, workspace, outbox };
}

describe('tool-file outbox ownership', () => {
  it('rejects ordinary workspace files and accepts only explicit outbox files', () => {
    const { workspace, outbox } = fixture();
    const ordinary = join(workspace, 'evidence.yaml');
    const deliverable = join(outbox, 'report.yaml');
    writeFileSync(ordinary, 'workspace source');
    writeFileSync(deliverable, 'delivery artifact');

    expect(validateToolFileOutboxPath('user:agent', workspace, ordinary).ok).toBe(false);
    expect(validateToolFileOutboxPath('user:agent', workspace, deliverable)).toMatchObject({
      ok: true,
      path: deliverable,
      outbox,
    });
  });

  it('scans only top-level non-hidden regular files and rejects symlink escapes', () => {
    const { root, workspace, outbox } = fixture();
    const deliverable = join(outbox, 'report.txt');
    const outside = join(root, 'outside.txt');
    const escaped = join(outbox, 'escaped.txt');
    writeFileSync(deliverable, 'ok');
    writeFileSync(join(outbox, '.hidden'), 'hidden');
    mkdirSync(join(outbox, 'nested'));
    writeFileSync(join(outbox, 'nested', 'ignored.txt'), 'nested');
    writeFileSync(outside, 'outside');
    symlinkSync(outside, escaped);

    expect(validateToolFileOutboxPath('user:agent', workspace, escaped).ok).toBe(false);
    expect(scanToolFileOutbox('user:agent', workspace)).toEqual([deliverable]);
    expect(existsSync(outside)).toBe(true);
  });

  it('gives each channel session of one workspace its own outbox [wi_20261004_159]', () => {
    const { workspace } = fixture();
    const telegram = resolveToolFileOutboxPath('owner:pulse', workspace);
    const feishu = resolveToolFileOutboxPath('owner:pulse:feishu', workspace);
    mkdirSync(telegram, { recursive: true });
    mkdirSync(feishu, { recursive: true });
    const telegramFile = join(telegram, 'notes.md');
    writeFileSync(telegramFile, 'telegram turn output');
    const legacy = join(dirname(telegram), 'legacy.md');
    writeFileSync(legacy, 'unowned');

    expect(telegram).toBe(join(realpathSync(workspace), '.relay', 'outbox', 'owner_pulse'));
    expect(feishu).toBe(join(realpathSync(workspace), '.relay', 'outbox', 'owner_pulse_feishu'));
    expect(scanToolFileOutbox('owner:pulse:feishu', workspace)).toEqual([]);
    expect(scanToolFileOutbox('owner:pulse', workspace)).toEqual([telegramFile]);
    expect(validateToolFileOutboxPath('owner:pulse:feishu', workspace, telegramFile).ok).toBe(false);
    expect(validateToolFileOutboxPath('owner:pulse', workspace, legacy).ok).toBe(false);
  });

  it('keeps the development override tenant-scoped', () => {
    const { root, workspace } = fixture();
    const override = join(root, 'override');
    process.env['RELAY_OUTBOX_DIR'] = override;
    const first = resolveToolFileOutboxPath('user:agent', workspace);
    const second = resolveToolFileOutboxPath('user:other', workspace);

    expect(first).toBe(join(override, 'user_agent'));
    expect(second).toBe(join(override, 'user_other'));
    expect(first).not.toBe(second);
  });
});
