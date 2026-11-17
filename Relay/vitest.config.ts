import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    root: '.',
    include: ['tests/**/*.test.ts'],
    setupFiles: ['tests/setup/account-state-isolation.ts'],
    // 测试隔离：默认关闭退化可观测埋点，避免测试触发 repairSessionJsonl 等真路径时
    // 把 emitObsEvent 事件写进生产目录 ~/.relay/obs/（2026-07-07 首次跑测发现的污染）。
    // relay-observability.test.ts 自身显式传 env 参数覆盖，不受此全局开关影响。
    // Shells on runtime hosts may inherit production rollout switches. Keep
    // default-transport tests deterministic; rollout tests inject their profile.
    env: {
      RELAY_OBS_ENABLED: 'false',
      // Mocked runtime events must never enter a resident production spool.
      // Capture tests pass an explicit temporary directory instead.
      PULSE_COMPUTECOST_RECEIPT_DIR: '',
      RELAY_FEISHU_RICHDOC_MODE: 'off',
      RELAY_TELEGRAM_RICHDOC_MODE: 'off',
      RELAY_TELEGRAM_RICH_MESSAGE_MODE: 'off',
    },
    // vitest 默认 testTimeout=5000ms 隐含了「机器是闲的」。生产机 nproc=1，全量
    // `vitest run` 并发跑满单核时，一批本来就要真跑几秒的用例（5.2MB 中毒文件修复、
    // 靠真实定时器走 9 轮退避的 pool 重试上限、scheduler 的 cron 装载）挤不进 5s，
    // 于是「单跑绿、全量红」，且每次红的是哪几条还随机漂。
    //
    // 2026-08-06 实测：人为加载(load≈11)下连跑两次，第一次 6 failed/3 files、第二次
    // 3 failed/2 files，失败项互不相同，全部是 `Test timed out in 5000ms`——没有一条
    // 是断言不成立。即这不是若干条 flaky 测试，是全局超时挡位配错。
    //
    // 逐条加 timeout 是缝补判据（补完一条下次漂到另一条）；挡位上调一次覆盖全族。
    // 代价：真死循环要 60s 才报，比 5s 慢——换掉的是稳定误报，划算。
    testTimeout: 60000,
    hookTimeout: 60000,
    // Full-suite file parallelism makes real fs.watch tests nondeterministic under
    // host load. Serializing files keeps each test's own concurrency semantics
    // intact while removing cross-file event-loop starvation from the CI verdict.
    fileParallelism: false,
  },
});
