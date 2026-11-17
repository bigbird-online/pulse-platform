/**
 * spec-drift.test.ts — spec 漂移闸(挂载样板)
 *
 * 挂法见 gate/install.md。放进 Relay 的 `tests/` 目录即被 vitest 收走
 * (vitest.config.ts 的 include 是 `tests/**\/*.test.ts`),**不需要改任何 config**。
 *
 * 这道闸断言的是:**新增漂移数不得增加**,不是"漂移必须归零"。
 * 理由:Relay 现有 167 条未绑锚点 + 47 条存量漂移,要求归零会让闸永远红,
 * 最后必然被人加 `.skip`——一道被 skip 的闸比没有闸更坏,因为它制造已在把关的错觉。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { scanDrift, evaluate, formatFailure, type Baseline } from './spec-drift.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..'); // tests/ 的上一级 = 仓库根

describe('spec 漂移闸', () => {
  it('新增 spec 漂移数不得增加', () => {
    const baseline: Baseline = JSON.parse(
      readFileSync(join(HERE, 'spec-drift.baseline.json'), 'utf8'),
    );
    const result = scanDrift({ repo: REPO });

    // 扫不到 spec 说明路径配错了,静默通过等于闸失效
    expect(result.specFiles).toBeGreaterThan(0);

    const verdict = evaluate(result, baseline);

    if (!verdict.pass) {
      throw new Error(formatFailure(verdict));
    }

    // 修好了旧漂移却没降基线 —— 只提示不失败,免得把"做了好事"变成红灯
    if (verdict.fixed.length > 0) {
      console.log(
        `[spec-drift] 已修好 ${verdict.fixed.length} 条旧漂移,` +
          `跑 \`node tests/update-spec-baseline.mjs\` 把基线从 ${verdict.baseline} 降到 ${verdict.current}`,
      );
    }
  });

  it('闸本身要跑得够快(不显著拖慢测试车)', () => {
    const result = scanDrift({ repo: REPO });
    // Relay 全量测试约 136s。这道闸的预算定 5s,实测远低于此(见 gate/README.md)。
    expect(result.elapsedMs).toBeLessThan(5000);
  });
});
