// 端到端：真实中毒备份 → repairSessionJsonl（src 真码，含新接线的 Pass 1.5）→ 断言洪水被清、正文保留。
// 用 copy，绝不碰只读原件。
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { copyFileSync, readFileSync, existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { repairSessionJsonl } from '../src/pool.js';

const POISON = '/home/pulse/.claude/projects/-Users-owner-Coevolution-Bigbird-PULSE-vultr-pulse-space/.poison-backup/cd58d561.jsonl.bak-20260726-062200-degen-flood';
const describeWithPoisonFixture = existsSync(POISON) ? describe : describe.skip;

// The historical 5.2MB backup is intentionally not vendored. Generic CI records
// this as SKIP; live-machine UAT runs it only where the read-only fixture exists.
describeWithPoisonFixture('repairSessionJsonl 端到端：2026-07-26 真实 課 洪水备份', () => {
  let dir: string;
  let f: string;
  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), 'charflood-'));
    f = join(dir, 'session.jsonl');
    copyFileSync(POISON, f);
  });
  afterAll(() => rmSync(dir, { recursive: true, force: true }));

  it('清掉五发洪水的绝大部分，同时保留每发前面的真正文', () => {
    expect(existsSync(POISON), '只读中毒备份必须存在').toBe(true);
    const before = readFileSync(f, 'utf-8');
    const beforeCount = (before.match(/課/g) ?? []).length;
    expect(beforeCount).toBeGreaterThan(100000);

    repairSessionJsonl(f);

    const after = readFileSync(f, 'utf-8');
    const afterCount = (after.match(/課/g) ?? []).length;
    expect(afterCount).toBeLessThan(beforeCount / 1000);
    expect(after).toContain('这个估算函数现在描述的是已经不存在的布局');
    expect(after).toContain('分身的诊断我要逐条核实');
    expect(after).toContain('bundle 目录时间戳不动是正常的');
    const lines = after.split('\n').filter((l) => l.trim().startsWith('{'));
    for (const l of lines) expect(() => JSON.parse(l)).not.toThrow();
  });
});
