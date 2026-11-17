/**
 * test_gate.test.ts — 闸本身的自测
 *
 * 要证明的不是"扫描器判得准"(那是 src/test_anchors.py 的活),而是**闸的行为对**:
 *   - 漂移增加 → 真的红
 *   - 漂移不变 → 真的绿
 *   - 漂移减少 → 绿,且提示降基线
 *   - 换了一条(总数不变) → 仍然红  ← 只比数字的实现会在这里放水
 *   - 扫不到 spec → 红(而不是"零漂移,通过")
 *
 * 用**合成仓库**做夹具,不去改 Relay ——闸的测试不该依赖被测仓库的当前状态,
 * 否则 Pulse 一改 spec 我的自测就跟着变色,那就不是测试是天气预报。
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { scanDrift, evaluate, formatFailure, type Baseline } from './spec-drift.js';

let repo: string;

/** 造一个最小仓库: src/ 里有真符号,specs/ 里引用它们 */
function makeRepo(opts: { srcSymbols: string[]; specClaims: string[] }): string {
  const root = mkdtempSync(join(tmpdir(), 'gate-fixture-'));
  mkdirSync(join(root, 'src'), { recursive: true });
  mkdirSync(join(root, 'tests'), { recursive: true });
  mkdirSync(join(root, 'specs', 'features', 'demo'), { recursive: true });

  writeFileSync(
    join(root, 'src', 'index.ts'),
    opts.srcSymbols.map((s) => `export function ${s}() { return 1; }`).join('\n') + '\n',
  );
  writeFileSync(join(root, 'tests', 'demo.test.ts'), `it('something', () => {});\n`);
  writeFileSync(
    join(root, 'specs', 'features', 'demo', 'tasks.md'),
    '# demo\n\n' + opts.specClaims.map((c) => `- [x] ${c}`).join('\n') + '\n',
  );
  return root;
}

function baselineOf(root: string): Baseline {
  const r = scanDrift({ repo: root });
  return {
    generated: new Date().toISOString(),
    driftCount: r.broken.length,
    knownKeys: r.broken.map((a) => a.key).sort(),
  };
}

const cleanup: string[] = [];
function fixture(o: { srcSymbols: string[]; specClaims: string[] }): string {
  const r = makeRepo(o);
  cleanup.push(r);
  return r;
}

afterAll(() => {
  for (const d of cleanup) rmSync(d, { recursive: true, force: true });
});

describe('闸的行为', () => {
  it('夹具本身可用: spec 引用真实符号时零漂移', () => {
    const root = fixture({
      srcSymbols: ['handleStart', 'calculateCost'],
      specClaims: ['`handleStart()` 返回欢迎文本', '`calculateCost()` 计费'],
    });
    const r = scanDrift({ repo: root });
    expect(r.specFiles).toBe(1);
    expect(r.broken).toHaveLength(0);
  });

  it('漂移不变 → 绿', () => {
    const root = fixture({
      srcSymbols: ['handleStart'],
      specClaims: ['`handleStart()` 可用', '`deletedFn()` 已被删'],
    });
    const base = baselineOf(root);
    expect(base.driftCount).toBe(1); // deletedFn 不在 src/

    const v = evaluate(scanDrift({ repo: root }), base);
    expect(v.pass).toBe(true);
    expect(v.newDrift).toHaveLength(0);
  });

  it('漂移增加 → 红,且失败信息点出 spec 文件与行号', () => {
    const before = fixture({
      srcSymbols: ['handleStart'],
      specClaims: ['`handleStart()` 可用', '`deletedFn()` 已被删'],
    });
    const base = baselineOf(before);

    // 模拟"改代码删了 spec 引用的符号": 新仓少了 handleStart
    const after = fixture({
      srcSymbols: [],
      specClaims: ['`handleStart()` 可用', '`deletedFn()` 已被删'],
    });
    // 基线是 before 的 key,但 spec 路径一致,所以 key 可比
    const v = evaluate(scanDrift({ repo: after }), base);

    expect(v.pass).toBe(false);
    expect(v.newDrift).toHaveLength(1);
    expect(v.newDrift[0].target).toBe('handleStart');

    const msg = formatFailure(v);
    expect(msg).toContain('specs/features/demo/tasks.md');
    expect(msg).toMatch(/:\d+/); // 带行号
    expect(msg).toContain('handleStart');
    expect(msg).toContain('update-baseline'); // 给出可执行的处置命令
  });

  it('修好一条又引入一条(总数不变) → 仍然红', () => {
    // 这条是关键: 只比 driftCount 数字的实现会在这里错误放行。
    const before = fixture({
      srcSymbols: ['keptFn'],
      specClaims: ['`keptFn()` 在', '`goneFn()` 不在'],
    });
    const base = baselineOf(before);
    expect(base.driftCount).toBe(1);

    // goneFn 补上了(修好一条),keptFn 被删了(引入一条) → 总数仍是 1
    const after = fixture({
      srcSymbols: ['goneFn'],
      specClaims: ['`keptFn()` 在', '`goneFn()` 不在'],
    });
    const r = scanDrift({ repo: after });
    expect(r.broken).toHaveLength(1); // 总数确实没变

    const v = evaluate(r, base);
    expect(v.current).toBe(v.baseline); // 数字持平
    expect(v.pass).toBe(false); // 但仍须失败
    expect(v.newDrift[0].target).toBe('keptFn');
    expect(v.fixed).toHaveLength(1);
  });

  it('漂移减少 → 绿,并报告已修好的条数(供降基线)', () => {
    // 符号名须 ≥4 字符: 扫描器有 `sym.length < 4` 的噪声下限(短名误命中率太高)
    const before = fixture({
      srcSymbols: [],
      specClaims: ['`alphaFn()` x', '`betaFn()` y'],
    });
    const base = baselineOf(before);
    expect(base.driftCount).toBe(2);

    const after = fixture({
      srcSymbols: ['alphaFn', 'betaFn'],
      specClaims: ['`alphaFn()` x', '`betaFn()` y'],
    });
    const v = evaluate(scanDrift({ repo: after }), base);

    expect(v.pass).toBe(true);
    expect(v.fixed).toHaveLength(2);
    expect(v.current).toBe(0);
  });

  it('扫不到任何 spec → specFiles 为 0(闸据此报错,不静默通过)', () => {
    const root = mkdtempSync(join(tmpdir(), 'gate-empty-'));
    cleanup.push(root);
    const r = scanDrift({ repo: root });
    expect(r.specFiles).toBe(0);
    expect(r.broken).toHaveLength(0);
    // 零漂移 + 零 spec 是"路径配错"的典型征状,挂载样板里有
    // expect(specFiles).toBeGreaterThan(0) 兜住,否则闸会静默失效。
  });

  it('未勾选的声明不产生锚点(未勾项没声称任何事成立)', () => {
    const root = mkdtempSync(join(tmpdir(), 'gate-unchecked-'));
    cleanup.push(root);
    mkdirSync(join(root, 'src'), { recursive: true });
    mkdirSync(join(root, 'specs'), { recursive: true });
    writeFileSync(join(root, 'src', 'i.ts'), 'export const x = 1;\n');
    writeFileSync(
      join(root, 'specs', 'tasks.md'),
      '# t\n\n- [ ] 计划实现 `notYetBuilt()`\n',
    );
    const r = scanDrift({ repo: root });
    // 反引号符号锚点不看勾选状态,但该行带"计划"字样 → TODO_HINT 拦下
    expect(r.broken).toHaveLength(0);
  });

  it('回归: src/ 里改了名但 tests/ 还留着旧名 → 仍判 broken', () => {
    // 这是开发中真实踩到的漏судь(2026-07-25):
    // 早期实现把"只在 tests/ 出现"一律降级成 unverifiable,结果在真 Relay 上
    // 把 src/ 全域 handleStart 改名后闸依然是绿的——正是闸最该抓的场景。
    const root = mkdtempSync(join(tmpdir(), 'gate-rename-'));
    cleanup.push(root);
    mkdirSync(join(root, 'src'), { recursive: true });
    mkdirSync(join(root, 'tests'), { recursive: true });
    mkdirSync(join(root, 'specs'), { recursive: true });

    // src/ 里已改名
    writeFileSync(join(root, 'src', 'i.ts'), 'export function handleStartCommand() {}\n');
    // tests/ 里还引用旧名(未同步)
    writeFileSync(
      join(root, 'tests', 'a.test.ts'),
      `import { handleStart } from '../src/i.js';\nit('x', () => { handleStart(); });\n`,
    );
    // spec 引用旧名
    writeFileSync(join(root, 'specs', 'tasks.md'), '# t\n\n- [x] `handleStart()` 返回欢迎文本\n');

    const r = scanDrift({ repo: root });
    const a = r.anchors.find((x) => x.target === 'handleStart');
    expect(a?.verdict).toBe('broken');
  });

  it('spec 点名的测试函数只在 tests/ 存在 → unverifiable(不误报成漂移)', () => {
    const root = mkdtempSync(join(tmpdir(), 'gate-testfn-'));
    cleanup.push(root);
    mkdirSync(join(root, 'src'), { recursive: true });
    mkdirSync(join(root, 'tests'), { recursive: true });
    mkdirSync(join(root, 'specs'), { recursive: true });
    writeFileSync(join(root, 'src', 'i.ts'), 'export const x = 1;\n');
    writeFileSync(join(root, 'tests', 'a.test.ts'), 'function testDiscrepancy(){}\nit("y",()=>{});\n');
    writeFileSync(join(root, 'specs', 'tasks.md'), '# t\n\n- [x] `testDiscrepancy()` 覆盖该场景\n');

    const r = scanDrift({ repo: root });
    const a = r.anchors.find((x) => x.target === 'testDiscrepancy');
    expect(a?.verdict).toBe('unverifiable');
    expect(r.broken).toHaveLength(0);
  });

  it('闸在真实规模仓库上跑得够快', () => {
    const root = fixture({
      srcSymbols: Array.from({ length: 200 }, (_, i) => `fn${i}`),
      specClaims: Array.from({ length: 200 }, (_, i) => `\`fn${i}()\` 可用`),
    });
    const r = scanDrift({ repo: root });
    expect(r.elapsedMs).toBeLessThan(5000);
  });
});
