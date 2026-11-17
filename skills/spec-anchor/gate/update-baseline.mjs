#!/usr/bin/env node
/**
 * update-baseline.mjs — 重新生成 spec 漂移基线
 *
 * 什么时候跑:
 *   - Pulse 主动修了 spec,漂移降下来了 → 跑一次把基线降低,锁住成果
 *   - 确认某批新漂移是既有问题而非本次引入 → 跑一次接受它们
 *
 * 挂载到 Relay 后的用法(文件名会变成 tests/update-spec-baseline.mjs):
 *   node tests/update-spec-baseline.mjs
 *
 * 在 ken-space 里直接跑(指向任意仓库):
 *   node gate/update-baseline.mjs --repo /path/to/Relay
 *
 * 注意:这个脚本**只降不升是靠人把关的**,它会如实写入当前读数。
 * 基线文件应当进 git,基线上升在 code review 里必须被看见——
 * 那正是"谁把漂移放进来了"这个问题的答案。
 */

import { writeFileSync, existsSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));

const argv = process.argv.slice(2);
const repoArg = argv.indexOf('--repo');
// 默认:脚本挂在 tests/ 下,仓库根是上一级
const REPO = repoArg >= 0 ? argv[repoArg + 1] : join(HERE, '..');

const outArg = argv.indexOf('--out');
const OUT = outArg >= 0 ? argv[outArg + 1] : join(HERE, 'spec-drift.baseline.json');

// 编译产物或源码,两种都试(ken-space 里跑的是 .ts,需要 tsx/vitest;
// 挂到 Relay 后 vitest 直接吃 .ts,但独立 node 跑需要已编译的 .js)
let mod;
const candidates = [
  join(HERE, 'spec-drift.js'),
  join(HERE, 'spec-drift.mjs'),
];
for (const c of candidates) {
  if (existsSync(c)) {
    mod = await import(c);
    break;
  }
}
if (!mod) {
  console.error(
    '找不到已编译的 spec-drift.js。\n' +
      '在 Relay 里请先 `npm run build`,或改用: npx vitest run tests/spec-drift.test.ts\n' +
      '在 ken-space 里请跑: npx tsx gate/update-baseline.mjs',
  );
  process.exit(2);
}

const { scanDrift } = mod;
const result = scanDrift({ repo: REPO });

let prev = null;
if (existsSync(OUT)) {
  try {
    prev = JSON.parse(readFileSync(OUT, 'utf8'));
  } catch {
    /* 首次生成 */
  }
}

const baseline = {
  generated: new Date().toISOString(),
  repo: REPO,
  driftCount: result.broken.length,
  knownKeys: result.broken.map((a) => a.key).sort(),
};

writeFileSync(OUT, JSON.stringify(baseline, null, 2) + '\n', 'utf8');

const delta = prev ? result.broken.length - prev.driftCount : null;
console.log(
  `[spec-drift] 基线已更新: ${result.broken.length} 条漂移` +
    (delta === null ? '(首次生成)' : delta === 0 ? '(与上次持平)' : delta < 0 ? `(降低 ${-delta} 条 ✓)` : `(**上升 ${delta} 条** —— 确认这是有意接受的吗?)`) +
    `\n  扫描 ${result.specFiles} 份 spec,耗时 ${result.elapsedMs}ms`,
);
console.log(`  写入: ${OUT}`);
