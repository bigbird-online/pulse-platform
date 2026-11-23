# SPEC：PULSE Stats — macOS App 呈现与传输侧（从代码提炼）

> Version: 0.2.1 · Created: 2026-08-01 · Modified: 2026-08-10 ·
> Status: 已实现（本 spec 描述现状）；**例外：§6.1 为设计前瞻，尚未实现**
> 版本史见 `git log`。
>
> **这份 spec 是反向提炼的**：靶子是 `Bigbird/Stats/PulseStats/` 全部 Swift
> （行数会漂不写死，`wc -l` 现算），逐文件读出来的现存契约，不是设计出来的目标。
> 写它的理由是 Stats 有近两万行 Python + Swift（2026-08-01 时点实测）却只有
> 一份 README，大鸟判「越优化问题越多」＝设计漂移。
>
> **与 v2 / v3 spec 的关系**：`pulse-stats-menubar-v2.md` 与
> `pulse-stats-v3-alert-triage.md` 写的是**当时要做什么**，本文写的是**现在代码是什么**。
> 两者不一致处一律以代码为准，并登记在 §9。
>
> **呈现类契约标 `manual`**：2026-07-31 装机后大鸟连报四处真 bug，其中「wi 四格误删」
> 与「事故卡片全英文」都在呈现侧，**代码门与 fixture 全发现不了，只有真人看一眼才发现**。
> 标 manual 不是偷懒，是本项目实测教训。

---

## 1. 传输契约

采数只有一条路径：`SSHSnapshotTransport` 跑一条**固定的** ssh 命令，参数写死在
`SSHSnapshotTransport.command` 这个 `static let` 里，运行期不可配置、不接受外部输入。

- 可执行文件一律是 `/usr/bin/ssh` 绝对路径，不得走 `PATH` 查找。
  `@anchor const executable :: "/usr/bin/ssh"`
- ssh 参数必须是 `BatchMode=yes`（禁止任何交互式口令提示）加 `ConnectTimeout=5`，
  远端固定跑 `/usr/bin/python3 …/exporter/pulse-stats-snapshot.py --json`。
  `@anchor test Bigbird/Stats/PulseStatsTests/SSHSnapshotTransportTests.swift :: testSSHCommandIsFixedAndNonInteractive`
- 单次采集总超时 `timeoutSeconds` = 90 秒，不得下调。
  天花板必须按**耗时分布**定，不是按中位数定：2026-08-03 从 ownermac 打生产，
  同一次采集跨一小时四次测得 17.1 / 17.8 / 10.9 / 23.5 秒。采集成本随机器当时
  的负载走，这个离散是常态不是异常。
  历史：2026-07-26 按 11.8 秒的测量定了 30 秒，对 23.5 秒只剩 6.5 秒余量，
  峰值一动就整条丢弃（08-03 实测撞上）。更早的 10 秒天花板则让**每次**刷新
  都超时。
  超时太紧的代价是双份的：取数被丢弃，且丢弃后的陈旧被渲染成「数据已过期」
  ——看起来像数据坏了，实际是我们挂断得太早。
  这个天花板的用途是兜住挂死的 ssh，对那个用途 90 秒仍然很短。
  `@anchor const timeoutSeconds :: 90`
- 输出上限 `maximumOutputBytes` = 1048576 字节（1 MiB）；超限即 `outputTooLarge`，
  **不得**截断后当正常数据用。
  `@anchor const maximumOutputBytes :: 1_048_576`
- 成本看板放不进 1 MiB 快照时以 gzip 形态到达（`compressed_dashboard` 与其字节的
  `compressed_dashboard_sha256`）。`CostDashboardCarrier` 先核 SHA-256，只接受单个 gzip 流，
  内存展开上限 4 MiB（与 ComputeCost `MAX_EXPANDED_BYTES` 相同），展开内容须重述外层元数据；
  任一项不符只丢弃成本看板。展开形态继续可读。规则见 `specs/stats-query-dashboard.md`。
  `@anchor const maxExpandedBytes :: 4 * 1024 * 1024`
  `@anchor test Stats/PulseStatsTests/CostDashboardTests.swift :: testGzipTransportRefusesChangedHashExtraMemberExcessExpansionAndMetadata`
- 超限检测必须在进程运行期间轮询，不能只在退出后查一次——`FoundationFixedCommandRunner`
  每 0.02 秒看一次 stdout 文件大小，超限立刻 `terminate()`，
  否则一个疯狂输出的远端会一直写到 30 秒超时才被发现。
  `@anchor test Bigbird/Stats/PulseStatsTests/SSHSnapshotTransportTests.swift :: testFoundationRunnerStopsStreamingOversizeBeforeTimeout`
- 子进程终止必须两段式：先 `terminate()`（SIGTERM），宽限
  `terminationGraceSeconds` = 2 秒仍在跑则 `SIGKILL`，最后 `waitUntilExit()` 收尸。
  **不得**留下游离的 ssh 子进程。
  `@anchor const terminationGraceSeconds :: 2`
- 子进程的 stdout/stderr 一律重定向到临时目录里的文件，`standardInput` 必须是
  `FileHandle.nullDevice`——ssh 永远读不到任何输入，这是 `BatchMode` 之外的第二道闸。
  `@anchor symbol nullDevice`
- 临时目录权限必须是 0700、其中的 stdout/stderr 文件必须是 0600，且函数返回前
  `defer` 必删整个目录，不得残留。
  `@anchor symbol posixPermissions`
- 失败一律落到 `FixedCommandError` 五个具名分支之一，**不得**吞掉：
  `launchFailed` / `nonzeroExit(status)` / `timedOut` / `outputTooLarge` / `temporaryFileFailed`。
  `@anchor symbol FixedCommandError`

### 1.1 single-flight（同一时刻只有一次采集）

- `SnapshotStore.refresh()` 进门先看 `refreshInFlight`，为真即**直接返回**，
  不排队、不重试。并发的多个调用者只会触发一次 ssh。
  `@anchor test Bigbird/Stats/PulseStatsTests/SnapshotStoreTests.swift :: testConcurrentRefreshesUseOneTransportFetch`
- App 只构造一个 `SnapshotStore` 实例（`PulseStatsApp` 的 `@State private var store`），
  面板与菜单栏共用它；**不得**各建一个，那会让一次刷新打两次 ssh。
  `@anchor test Bigbird/Stats/PulseStatsTests/MenuBarSharingTests.swift :: testBothMenuBarItemsDrivingOneStoreCauseOneFetch`
- 构造 view model（`RelayViewModel` / `HarnessViewModel` / `TriageViewModel`）
  一律不得有副作用——它们在 `body` 里每次重绘都会重建，若构造时取数就是无限刷新。
  `@anchor test Bigbird/Stats/PulseStatsTests/MenuBarSharingTests.swift :: testViewModelConstructionIsSideEffectFree`

### 1.2 缓存文件

- 缓存落 `~/Library/Application Support/PulseStats/snapshot-v2.json`，
  由 `FileSnapshotCache.defaultURL()` 算出；`applicationSupportDirectory` 取不到即
  `applicationSupportUnavailable`，**不得**回落到别处写。
  `@anchor symbol applicationSupportUnavailable`
  `@anchor test Bigbird/Stats/PulseStatsTests/FileSnapshotCacheTests.swift :: testSaveLoadUses0600AndLeavesNoTemporaryFile`
- 目录权限必须 0700、文件必须 0600，且每次 `save()` 都重新 `setAttributes` 压一遍；
  写盘走 `.atomic`，读盘走 `.mappedIfSafe`。
  `@anchor test Bigbird/Stats/PulseStatsTests/FileSnapshotCacheTests.swift :: testSaveLoadUses0600AndLeavesNoTemporaryFile`
- 缓存内容解不出来时必须报 `invalidCache`，**不得**当成空快照或半个快照用。
  `@anchor test Bigbird/Stats/PulseStatsTests/FileSnapshotCacheTests.swift :: testInvalidCacheIsRejectedAndNeverDecodedAsSnapshot`
- 历史文件 `history-v1.jsonl` 同权限（目录 0700 / 文件 0600），保留窗口
  `HistoryStore.retention` = 7 天，写入时顺带裁剪；写法是**临时文件 + 原子改名**，
  临时文件建出来就是 0600，不得出现「先写后 chmod」的可读窗口。
  `@anchor test Bigbird/Stats/PulseStatsTests/HistoryStoreTests.swift :: testAppendUses0600FileIn0700DirectoryAndLeavesNoTemporaryFile`
- 历史写失败**不得**影响本次刷新的结果——`appendIgnoringFailure` 吞掉一切错误，
  且不碰 `transportState`。
  `@anchor test Bigbird/Stats/PulseStatsTests/HistoryStoreTests.swift :: testHistoryWriteFailureLeavesTheRefreshFullyLive`
- 历史行只存聚合计数，结构上就没有地方放 session id、飞书 open_id、agent 名、
  wi 标题或任何 token 明文；`agentCount` / `sessionCount` 是**数量**不是名单。
  `@anchor test Bigbird/Stats/PulseStatsTests/HistoryStoreTests.swift :: testEncodedRowCarriesNoSessionOrIdentityDetail`

### 1.3 解码闸

- `SnapshotDecoder.decode()` 必须先只解 `schema_version` 一个字段，
  不等于 `pulse.monitoring.snapshot.v2` 就抛 `unsupportedSchema`，**不得**继续解正文。
  `@anchor const supportedSchemaVersion :: "pulse.monitoring.snapshot.v2"`
- 解码必须校验守恒：`overall.open_incident_count` 不等于 `incidents` 里
  `state == open` 的条数，即 `invalidPayload`，整份快照拒收。
  `@anchor symbol openIncidentCount`
- 版本闸必须在解正文**之前**拦下，不得先解一遍再回头判版本。
  `@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testRejectsUnknownMajorBeforePayloadDecode`
- 时间戳解析必须两段兼容：先试带小数秒的 ISO-8601，再试不带的；两者都不成即
  `dataCorruptedError`，不得猜。
  `@anchor symbol parseISO8601`
- 新增字段一律**不得**成为必填：`alert_tiers` 缺失读作 `AlertTiers.zero`，
  alert 的 `tier` 缺失或取值不认识一律落 `AlertTier.fallback`（= `watch`）。
  理由写在代码里：一个必填的新字段会让老 exporter 的快照整份解不开，一块空白面板
  比一条降级的告警坏得多。
  `@anchor const fallback :: .watch`

---

## 2. 刷新策略（`RefreshPolicy`）

**这一节的值一律以代码为准，README 只写了「30 分钟 + 20:00-08:00」两个数，
代码里还有第三个数（轮询步长 60 秒）README 没写。**

- 定时刷新间隔 `RefreshPolicy.interval` = 30 × 60 = 1800 秒。
  它刻意**不用**快照自己的 TTL 当间隔——TTL 说的是「过了这条线算陈旧」，
  不是「该多久取一次」；照 TTL 取数等于单核机器上一天跑 480 次 exporter。
  `@anchor const interval :: 30 * 60`
- 静默窗口是北京时间 20:00 起到次日 08:00 止，共 12 小时。
  `@anchor const quietStartHour :: 20`
  `@anchor const quietEndHour :: 8`
- 时区必须固定为 UTC+8，**不得**用机器本地时区——两台机器不一致（vultr 跑 UTC、
  笔记本跑 UTC+8），用「本地时间」会让同一份代码在两台机器上是两个行为。
  `@anchor const timeZone :: 8 * 3600`
- 窗口跨零点，判定必须写成两段的并集（`hour >= 20 || hour < 8`），
  **不得**写成单个 `start..<end` 区间——后者的 bug 是静默的：它永远判不出静默。
  `@anchor test Bigbird/Stats/PulseStatsTests/RefreshPolicyTests.swift :: testTheQuietWindowSpansExactlyTwelveHours`
- 边界是**左闭右开**：20:00 整已静默，08:00 整已不静默。
  `@anchor test Bigbird/Stats/PulseStatsTests/RefreshPolicyTests.swift :: testTheQuietWindowIsTwelveHoursOfBeijingNight`
- 冷启动（`lastRefresh == nil`）**必须**刷新，不看时辰——夜里挂着「没数据」比
  多打一次 ssh 坏。
  `@anchor test Bigbird/Stats/PulseStatsTests/RefreshPolicyTests.swift :: testAColdStartRefreshesEvenAtNight`
- 时钟倒退（`elapsed < 0`）必须判为「该刷」，不得傻等一个虚假的间隔跑完。
  `@anchor test Bigbird/Stats/PulseStatsTests/RefreshPolicyTests.swift :: testAClockMovingBackwardsDoesNotStallRefreshes`
- 节流的时钟是**尝试时刻**（`lastRefreshAttempt`）不是成功时刻。若按成功记，
  机器不通时每个轮询 tick 都会重试，把一次故障变成一串 ssh。
  `@anchor symbol lastRefreshAttempt`
- 轮询步长 `SnapshotStore.startRefreshLoop(pollSeconds:)` 默认 60 秒，
  **远小于** 30 分钟间隔。短 tick 只花一次日期比较，换来的是静默窗口结束后、
  或 Mac 睡醒后，一分钟内就能取到数，而不是被开机时刻钉死的半小时相位。
- **刷新循环归 `SnapshotStore` 持有，且必须从 `PulseStatsApp.init` 启动**，
  不得挂在任何 view 上。踩过两次（均为 2026-08-03 实测）：
  ① 循环本体写成 MenuBarExtra label 的 `.task` —— SwiftUI 把 `.task` 绑在
  view 生命周期上，label 每次重建都取消它；实测进程活了 16 小时只烧 38 秒
  CPU、52 分钟没取数。
  ② 循环搬进 store 但**入口仍是那个 `.task`** —— label 不保证渲染，实测
  静坐 3.5 分钟一次没取，`startRefreshLoop` 从没被调用过。
  `init` 每进程执行一次、先于任何 view 存在，是唯一没有渲染前提的地方。
  start 必须幂等：`.task` 会被反复触发，不守卫就叠出第二个循环，对单核机器
  意味着每轮双倍 ssh。
  `@anchor symbol startRefreshLoop`
- 两条绕闸路径**必须**走无条件的 `refresh()` 而非 `refreshIfDue()`：
  面板页脚的「刷新」按钮，和打开面板本身（`StatsPopoverView` 的 `.task`）。
  大鸟专门点开面板的那一刻正是最想要现值的时刻，静默窗口不该拦他。
  `@anchor test Bigbird/Stats/PulseStatsTests/RefreshPolicyTests.swift :: testTheManualButtonIsNeverRateLimitedOrSilenced`
- 到不了点的 tick 仍**必须**推进 `presentationTime`——否则 SwiftUI 观察不到时钟变化，
  一份曾经绿过的快照会在 TTL 过后继续显示为绿，直到下次真取数。
  `@anchor test Bigbird/Stats/PulseStatsTests/RefreshPolicyTests.swift :: testUndueBackgroundTickStillExpiresThePresentation`

---

## 3. 状态与降级（**出过 bug 的地方**）

这一节答一个问题：数据取不到、过期了、或格式对不上时，屏幕上究竟显示什么。

### 3.1 `TransportState` 六态

`SnapshotStore.transportState` 只有六个取值，页脚文案由 `PanelStatusText.describe()`
一一映射，**不得**在别处另造说法：

| 状态 | 何时进入 | 页脚显示 |
|---|---|---|
| `idle` | 从没取过数 | `还没取数` |
| `loading` | 正在取 | `正在取数…` |
| `live(generatedAt, cacheError)` | 取到且解开了 | `MM-dd HH:mm 更新（UTC）`；缓存写失败时追加 ` · 这次没存下来` |
| `cached(generatedAt, expired, fallback)` | 取失败但缓存可用 | `MM-dd HH:mm 的旧数据（UTC）`；过期时追加 ` · 已经过期` |
| `failed(String)` | 取失败且无缓存可用 | `这次没取到数` |
| `incompatibleSchema(String)` | 版本不认且无缓存 | `数据格式对不上，没敢用` |

- 取数失败必须先试缓存：缓存里有货就进 `cached` 并保留 `fallback` 说明因何回落
  （`transportFailure` 或 `incompatibleSchema`），缓存为空才进 `failed`。
  `@anchor test Bigbird/Stats/PulseStatsTests/SnapshotStoreTests.swift :: testExpiredCacheFallsBackToStaleWhenTransportFails`
- 缓存**写**失败**不得**把已经取到的好数据作废——仍是 `live`，只在 `cacheError`
  里记一笔，页脚显示「这次没存下来」。
  `@anchor test Bigbird/Stats/PulseStatsTests/SnapshotStoreTests.swift :: testCacheWriteFailureKeepsLiveSnapshotButIsExplicit`
- 版本不认但缓存可用时，快照仍显示，但健康度**必须**降为 `stale`——
  这是「给你看旧的，并且告诉你它是旧的」，不是「一切正常」。
  `@anchor test Bigbird/Stats/PulseStatsTests/SnapshotStoreTests.swift :: testUnknownMajorPreservesLastSuccessButMarksItStale`
- 缓存本身坏掉时**不得**静默当空：`snapshot = nil` 且进 `failed`，页脚说得出因由。
  `@anchor test Bigbird/Stats/PulseStatsTests/SnapshotStoreTests.swift :: testInvalidPersistentCacheIsRejectedExplicitly`

### 3.2 stale / unknown 的判定

`SnapshotStore.displayRuntimeHealth` 是唯一出口，四条规则按序：

- 没有快照 → `unknown`，**不得**读成 `ok`。
- `transportState` 是 `cached` 且 `expired` → `stale`。
- `transportState` 是 `cached` 且回落原因是版本不认 → `stale`。
- 快照自己的 `expiresAt <= presentationTime` → `stale`，哪怕它是这一秒刚取来的。
- 以上都不命中，才用快照自报的 `overall.runtime_health`。
  `@anchor test Bigbird/Stats/PulseStatsTests/SnapshotStoreTests.swift :: testExpiredSnapshotPresentationDrivesMenuBarAndPanelToStale`

- 灯色由 `StatusPresentation.tone` 定，**两条轴都要看**：`critical` 红；
  `warning` 黄；`stale` 黄——**除非 `pausedOvernight` 为真则降灰**（见下条）；
  `unknown` 灰；只有 `ok` 时才看覆盖率，`full` 才给绿，`partial` 黄、`unknown` 灰。
  换言之**「运行正常但覆盖不全」不得显示为绿**——这是防假绿的那道闸。
  `@anchor test Bigbird/Stats/PulseStatsTests/StatusPresentationTests.swift :: testCoveragePreventsFalseGreen`
- **`pausedOvernight` 轴（2026-08-07 加）**：数据旧**只因为**夜间静默窗
  （20:00–08:00）没取数时，stale 不是故障——灯降灰、图标换 `moon.zzz.fill`
  （睡着的钟不是报警的钟）。置位条件两条缺一不可，全在
  `SnapshotStore.staleIsMerelyPaused`：①上次取数**成功**（读
  `lastFetchSucceeded`，不读瞬时的 `transportState`——后者在每次开面板时闪过
  `.loading`，曾让黄灯每点必闪，2026-08-02 大鸟撞过）；②时钟规则
  `RefreshPolicy.staleIsExplainedByQuietWindow` 成立。取数失败/缓存回落/版本
  不认时**不置位**，黄灯照常——静默窗只解释「没去问」，不解释「问了没答上」。
  且 `StatusPresentation` 的 init 强制它只对 `runtimeHealth == .stale` 生效：
  凌晨三点取回来的 `critical` 仍是 critical。
  `@anchor symbol pausedOvernight`
  `@anchor symbol staleIsMerelyPaused`
  `@anchor test Bigbird/Stats/PulseStatsTests/RefreshPolicyTests.swift :: testOvernightPauseReadsAsPausedNotBroken`
  `@anchor test Bigbird/Stats/PulseStatsTests/SnapshotStoreTests.swift :: testRefreshInFlightDoesNotBlinkStaleDuringTheQuietWindow`
- 灰是承重的：任何算不出来的东西一律落灰，**不得**把「不知道」打扮成绿。
  `@anchor symbol StatusLamp`

### 3.3 状态一句话（`SnapshotStatusSummary`）

菜单栏与面板头部由**同一个类型**算，判据不两处各写；但 2026-08-02 起
（ce69c13，大鸟拍）两处呈现**不再是同一句话**——差异只有一个开关
`includesCoverage`：

- **面板头部与 VoiceOver 传 `true`**：覆盖轴进句子、参与灯色，行为如下述。
- **菜单栏传 `false`**（`PanelViewModels.swift` 的菜单栏 view model）：覆盖轴
  **不进句子也不参与灯色**。理由：覆盖轴说的是监控自己建成了几分，会连着
  好几天不变，对大鸟不构成行动（大鸟问「覆盖未知是什么意思」即证据）；
  `runtime=ok` 时菜单栏长期挂灰字，看着像出事。覆盖状态在面板里一个字没少，
  只是挪回里层。
  `@anchor symbol includesCoverage`
  `@anchor test Bigbird/Stats/PulseStatsTests/StatusPresentationTests.swift :: testMenuBarDropsCoverageButKeepsRuntimeAndAction`
  `@anchor test Bigbird/Stats/PulseStatsTests/StatusPresentationTests.swift :: testPanelStillShowsCoverageThatMenuBarDrops`

- 没快照 → 灰灯 + `没数据`。
- `act` 计数 > 0 → 一律红灯，正文 `N 件要出手`，并把每条非满格的轴以 ` · ` 追加
  （运行在前、覆盖在后；菜单栏那档没有覆盖轴可追加）。**行动计数永不被隐藏**。
  `@anchor test Bigbird/Stats/PulseStatsTests/StatusPresentationTests.swift :: testActionCountStaysVisibleWithEveryUntrustedState`
- `act` = 0 但有轴不满格 → 那些理由本身成为正文（`数据已过期` / `数据未知` /
  `运行严重异常` / `运行有异常` / `覆盖不全` / `覆盖未知`），灯取其中最重的一档。
  stale 且 `pausedOvernight`（§3.2）时这句换成灰灯 + `夜间暂停刷新`——
  「暂停」不是「过期」，天天误报的黄灯会被学会无视。
  `@anchor test Bigbird/Stats/PulseStatsTests/StatusPresentationTests.swift :: testSnapshotStatusSummaryNamesEveryUntrustedState`
- `正常` 的判据按面分两档：面板（`includesCoverage: true`）须
  「运行 ok + 覆盖 full + act = 0」三条同时成立；菜单栏（`false`）覆盖轴
  已摘，「运行 ok + act = 0」即读 `正常`。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testRuntimeWarningNeverReadsNormal`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testPartialCoverageDoesNotReachTheMenuBar`
- 额度烧到 100% **不得**影响这句话：配额与「有没有事要人干」是两条线，
  配额满格仍可显示 `正常`。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testExhaustedQuotaStillReadsNormalWhenNothingNeedsAction`

### 3.4 缺数一律读「未知」，不得读 0

- `SnapshotMetrics` 的每个取值口都返回可选值，metric 缺失、`status` 不是
  `available`、或形状不对，一律 `nil`；全文件**没有任何默认值**。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testUnavailableMetricStatusIsTreatedAsMissing`
- `StatsFormat` 每个函数吃可选值、缺失时返回字面量 `未知`，**没有**一条路径把
  缺失的数变成 0。
  `@anchor const unknown :: "未知"`
- 空数组与 `nil` 必须区分：空 = 「查过了，确实没有」，`nil` = 「读不到」，
  两者渲染不同。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testEmptyStalledListDiffersFromMissingStalledList`
- 24 点小时序列长度不对必须整条拒收，**不得**补零凑长度——补出来的零是我们从没测过的小时。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testWrongLengthHourlySeriesIsRejectedRatherThanPadded`
- 「今天真用掉」的三个组成项（新输入 / 缓存创建 / 输出）缺任一即整体未知，
  **不得**把老快照缺失的缓存创建当 0——那会造出一个看着精确、实际偏低的总量。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testMissingCacheCreationMakesWorkingTokensUnknown`

---

## 4. 灯的判据（`LampRules`）

四个规则，全部纯函数、可单测，判据**不得**散落到 view 里。

- 配额灯：`> 95` 红、`> 90` 黄、其余绿；边界是**开区间**——正好 90 是绿，正好 95 是黄。
  缺数读灰，**不得**读成 0%。
  `@anchor test Bigbird/Stats/PulseStatsTests/LampRulesTests.swift :: testQuotaThresholds`
- CC 存活灯：近 24 小时撞过限流即红；进程数读不到或为 0 即灰；
  进程在跑但**限流状态未知**时仍是灰，**不得**给绿——「不知道有没有出事」不是「没出事」。
  `@anchor test Bigbird/Stats/PulseStatsTests/LampRulesTests.swift :: testCCIsGrayWhenRateLimitStateIsUnknown`
- 配额灯**不得**混入进程存活与限流状态：`ccLamp` 只看百分比，
  存活另有 `ccLivenessLamp`。混在一起会让同一个配额数字在菜单栏与面板不一致。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testCCQuotaLampDoesNotMixInProcessOrRateLimitHealth`
- wi 批次灯：停滞 > 14 天红、> 7 天黄；新建数读不到即灰。
  `@anchor const staleWorkItemDays :: 7`
  `@anchor const abandonedWorkItemDays :: 14`
- 完成率灯：≥ 0.95 绿、< 0.80 红、中间黄；**当日新建为 0 时必须灰**——
  没有分母就没有完成率，那既不是红也不是 100% 的绿。
  `@anchor test Bigbird/Stats/PulseStatsTests/LampRulesTests.swift :: testZeroCreatedTodayMakesCompletionLampGray`
- 同一个阈值必须在规则层、菜单栏、面板、提醒四处读数一致，不得各判一次。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testQuotaBoundariesAgreeAcrossRuleMenuPanelAndConcerns`

---

## 5. 菜单栏呈现契约

现状是**一个** `MenuBarExtra`，`.menuBarExtraStyle(.window)`，
渲染 `PulseMenuBarLabel`。形态：`MenuBarArtwork` 画四个等宽槽，依次为
`CODEX`、`CC`、`AGY`、`WI`（小字标签在上、读数在下），其后是可选状态标记。

- AGY 槽读 `MenuBarViewModel.menuAgyText`：取 `RelayViewModel.agyLowestRemainingPercent`
  （所有当前桶中最低的剩余比例），换成已用比例显示，与左侧 CODEX、CC 同为「已用」口径，
  例如最低剩 7% 显示 `AGY 93%`；`agyLamp` 与面板「agy 额度」行同灯。读数过期、读取失败、
  无发布物或全部桶已重置时显示 `—`，**不以 0% 或旧数顶替**；
  无障碍标签读「agy 额度待更新，不以旧数或零替代」。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testAgyMenuBarSlotReadsLowestRemainingBucket`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testAgyMenuBarSlotUnknownShowsDashNeverZero`
- GROK 不进菜单栏，只留面板「GROK 周额度」行。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testGrokQuotaStaysInPanelButLeavesMenuBar`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testGrokQuotaUnavailableShowsDashAndReasonNeverZero`
- 面板「GROK 周额度」行：有读数时「已用 N%」，不适用时「不适用」，其余「待更新」；
  展开后显示重置时间、额度读取时间（失败事实显示那次读取的时间）、套餐名与原因白话，
  并注明这是账号级周额度、不计入成本合计。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testGrokPooledAllowanceIsNotApplicableNotAFabricatedQuota`
  `@anchor test Bigbird/Stats/PulseStatsTests/CostDashboardTests.swift :: testGrokQuotaPanelShowsResetReadTimeOrReason`
- App 侧新鲜度与 Codex / CC 同规则：读取时刻不晚于现在 60 秒、`resets_at` 在未来、
  读取时刻不早于 `period_start`；否则视为过期，面板显示「待更新」与「当前周期内尚无 GROK 额度读数」。
- 面板「agy 额度」行：有读数时显示所有当前桶中最低的剩余比例「最低剩 N%」，否则「待更新」；
  展开后按组列出「<组名> · 周」与「<组名> · 5 小时」的剩余比例和重置时间（Argon 组只有周），
  再列额度读取时间；无读数时显示原因白话。`RelayViewModel.agyQuotaGroups` 只收重置时间在未来的桶，
  读取时刻须在 1 小时内且不晚于现在 60 秒，否则不显示旧数。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testAgyQuotaShowsThreeGroupsWithRemainingAndReset`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testAgyQuotaFailureOrStaleReadingShowsReasonNotOldFigure`
  `@anchor test Bigbird/Stats/PulseStatsTests/CostDashboardTests.swift :: testAgyQuotaPanelShowsGroupsOrReason`

- 标题必须是**一条合成的 `Text`**（用 `+` 拼接），**不得**用 `VStack` 堆视图。
  代码注释记了五次尝试：形状视图被整个丢掉、emoji 圆点尺寸失控、
  `Text(Image(systemName:))` 附件不存活、着色不存活；第四版假设 `label:` 闭包能布局
  真视图层级，上屏后大鸟只看见「CC」一个词。**这是本文件最贵的一条契约。**
  `@anchor symbol PulseMenuBarLabel`
- 颜色**永不**是唯一载体：数字带 `%`、状态短语把「N 件要出手」写全，
  看不见色差的人从文本读到同一件事。
  `@anchor symbol accessibilityLabel`
- 宽度上限 `MenuBarWidth.budget` = 210pt（四槽三位数加状态标记的最坏情况约 203pt），
  由测试守着而不是靠眼看截图。
  `@anchor const budget :: 210`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testMenuBarArtworkPlacesAgyAfterCCWithinBudget`
- 超预算时**必须让句子让位，不得让配额让位**：`shouldShortenStatus()` 为真就把状态句
  截到第一个分句（`shortStatusText`），配额两格原样保留。
  2026-07-31 之前的回落方向是反的——一超宽就把两个配额都丢掉，大鸟当场报
  「任务栏为啥不再显示 CC 和 Codex 的配额了？而且还显示了一些莫名其妙的信息」。
  配额是这一栏存在的理由，后面那两个分句是挂好几天的内部诊断。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testThreeDigitActionWithEveryStatusSuffixStaysWithinBudget`
- `act` 计数必须直接读快照的 `alert_tiers.act`，App 端**不得**自己数。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testActCountDrivesStatusWord`
- `watch` 与 `done` 两档**不得**上菜单栏。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testWatchAndDoneDoNotReachTheMenuBar`

---

## 6. 面板呈现契约（`StatsPopoverView`）

固定尺寸 420 × 580，结构自上而下：头部 → 分隔线 → 可滚动正文（四段）→ 分隔线 → 页脚。

- 正文四段的顺序必须是：①仪表盘 ②未闭环事故 ③要你出手 ④在飞项。
  顺序即优先级，且**与 07-28 那版相反**：那次把「要你出手」放第一因为它最紧急，
  但紧急的东西大多数时候是空的，于是打开面板第一眼常是一片「没事」，
  日常要看的常态读数反被压到最底下。日常放最上，例外紧随其后。
  `@anchor symbol dashboardSection`
- 面板全文必须可选中复制（最外层挂 `.textSelection(.enabled)`）。
  大鸟 2026-07-29「大部分文字都无法拷贝，体验不好」。加在外层而不是逐个 `Text`：
  漏一个就是一个复制不了的坑。
  `@anchor symbol textSelection`
- 「未闭环事故」与「要你出手」**不得**互相拷贝卡片：前者是 reducer 支撑的当前状态
  （有异常证据、无恢复证据），后者是等人裁决的事件。把任一方整卡复制进另一方，
  会把一件事读成两件活。该断言跑在**真实生产快照**上，不是构造数据。
  `@anchor test Bigbird/Stats/PulseStatsTests/LiveTriageContractTests.swift :: testEveryReportedOpenIncidentHasOneVisibleCardSource`
- 事故排序必须确定：先按严重度（critical > warning > info），再按最近更新时间降序，
  时间缺失的排最后，最后用 `incidentKey` / `episodeID` 破平——**不得**依赖数组原序。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testOpenIncidentsExcludeResolvedAndSortBySeverityThenRecentUpdate`
- `state == unknown` 的事故**不得**混进可见的开放列表，但也不得让它把
  `open_incident_count` 说小——两者是独立的事实。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testUnknownStateCannotHideAReportedOpenIncidentCount`
- 仪表盘必须显示 canonical carrier 的跨端真源状态。只有受支持的嵌套 schema、
  `validated/global/fresh` 与完整端覆盖同时成立时才可显示「四端真源已验证」；
  缺 carrier 显示「真源范围未知」，版本不兼容单独标明，二者均不得画绿。
  `@anchor symbol GlobalAuthorityViewModel`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testUnsupportedNestedAuthoritySchemaCannotClaimValidatedGlobalTruth`
- 「在飞项」卡片及其人工 / 自动两档已于 2026-10-26 退役（WI422），由「在办」看板替代，
  现行契约见 `specs/stats-presentation.md` 的「在办」一节。`harness.wi.in_flight` 仍导出
  全部非终态 WI，供「要你出手」显示 WI 身份与创建时长，以及 WI 日巡读取。
- 每个在飞条目必须带 `work_item_id` 与 `status` 两个字段并可单独选中复制。
  大鸟 2026-07-29「缺少 wi id 等信息」：只有天数/负责人/标题时，要去 harness 里
  找它得拿被截断的标题去猜 id；而 `draft` 与 `in_progress` 的处置完全不同，
  「摊着」这个词盖住了两者的区别。
  `@anchor symbol workItemID`

### 6.1 退役边界：旧五档顶层分组（2026-08-07 旧约定）

> **旧五档分别铺成五个顶层分组仍退役。** 2026-10-26 起面板已无在飞项卡片，
> 在办事项见「在办」看板。以下五档判据继续是
> exporter 的当前数据契约，但本节描述的五组同时铺开不再是当前 UI。

**起因**：大鸟问「在飞项还是很多，这些 wi 分身都无法处理？」。查证结论是
**不是处理不了，是大部分没有任何机制在推**——而旧版把「在等人」和「没人管」
铺成一个平列表，这个区别读不出来。大鸟定：「需要分清，标记『在等人』和
『没人管』」。判据契约见 `stats-exporter-collection.md` §6.7.1，本节只管呈现。

- 分组顺序固定，**按「该不该由大鸟操心」降序**，不按数量或字母：
  ① 真没人管 ② 阻塞待人看 ③ 等兜底·已超期 ④ 在等人 ⑤ 新建观察中。
  第①组排最前是因为它是这次改动**唯一新增的信息**——旧面板看不出这一档。
  `@anchor symbol InFlightDisposition`
- 当前 UI 将**人工与自动两层**一起折叠为一行库存摘要。这既保留全量台账，又避免
  人工项与行动队列重复、普通队列铺满 420 × 580 面板；用户展开时全部在飞项必须可见。
  `@anchor symbol inFlightInventoryRows`
- 分组为空时**不显示该组标题**，不留「0 件」的空壳行。但「真没人管」为空是
  一个**值得说出来的好消息**，显示为一行「没有无人认领的活」——
  与「读不到」（`nil`）必须可区分。
  `@anchor symbol inFlightUnowned`
- 「真没人管」组每条必须写明**为什么没人管**（该 owner 未开启兜底扫描），
  不能只标一个红字。大鸟看到这行要能直接决定「给这家开扫描」还是「这活不要了」。
  `@anchor symbol inFlightGroups`
- 熔断项（`circuit_broken`）**不得**混进「等兜底」，单独标「机制已放弃」。
  归进等兜底会让读者以为还有机制在管，实际它已经连续失败到停止重试。
  `@anchor symbol circuitBroken`
- 「静默 N 天」与「从未有过动静」必须用不同措辞，**不得**都渲染成
  「静默 N 天」。前者是被反复跳过（危险），后者多为刚建单（正常）。
  此旧呈现已随推进态分组退役，不再要求 App 保留对应符号。
- `blocked_reason` 原文透出，面板**不得**替大鸟判断理由是否成立。
  机器判不了搪塞与真阻塞的区别；给它编判据只会制造第二种假绿。
  `@anchor symbol blockedReason`
- 分组名**不得**出现内部机制名。「兜底扫描」`stale_work_items` 这类词属于
  黑话，第一次出现必须用大白话解释（如「等系统自动捡起」）。
  按 monorepo 根 CLAUDE.md § 对大鸟说话的语言。
  `@anchor symbol InFlightGroup`
- 分组后各组计数之和必须等于在飞总数，且总数**不得**因分组而改变。
  分组是切分不是筛选——任何一件在飞的活都必须落进恰好一个组。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testInFlightGroupsPartitionWithoutLoss`
- **验证方式的例外**：本节断言经 ssh 跑 `xcodebuild test` 必崩在
  `childPID > 0`（GUI 宿主起不来），那是测法失效不是代码失效。绕法＝`swiftc`
  单独编 Core 层跑真断言，见 auto-memory
  `swift-gui-tests-unrunnable-over-ssh-compile-core-standalone`。2026-08-07
  实测：13 条断言全过 + 真实生产快照（17 件）守恒与排序均正确 + 逐条撤销
  修复可让对应断言变红。
- 「已经过去」段必须是**一句汇总**不是逐条铺开。
  `@anchor symbol doneSummary`
- 能力清单页脚必须四类齐全（已验证 / 部分接入 / 未实现 / 已退役），
  构成一个完备且互斥的划分；未实现项**不得**显示为绿。
  实测为空的清单仍要报出四个 0，只有「没拿到快照」才说「能力清单还没拿到」。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testCapabilityFooterShowsAllFourZeroGroupsForMeasuredEmptyInventory`
- 告警正文必须按全角竖线 `｜` 切成「exporter 算的元信息」与「escalation 自己的话」
  两截，且**只切第一个**——正文里合法地可能还有更多。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testOnlyTheFirstSeparatorSplits`
- 切出来的尾巴为空时**不得**留下孤零零的元信息行，要把原文整段留着可见。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testReasonWithEmptyTailKeepsTheTextVisible`
- 面板**不得**自己判档：`TriagedAlert` 只读 exporter 给的 `tier`。
  同一件事在两个地方各判一次就会漂。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testSplitsAlertsIntoThreeSections`

### 6.1 计数标签的口径（数什么 · 哪个窗口 · 哪个时区）

**这一小节是照 auto-memory `counter-label-must-name-unit-and-window` 写的**：
2026-07-28 有过「今日完成」实为「今日新建里完成的」、「本周」实为滚动 7 天的事故。

| 面板文案 | metric id | 数什么 | 窗口 | 时区 |
|---|---|---|---|---|
| 今天收口 | `harness.wi.day_completed` | work item 条数 | 自然日 | 见下「未决」 |
| 今天新建 | `harness.wi.day_created` | work item 条数 | 自然日 | 见下「未决」 |
| 本周收口 | `harness.wi.iso_week_completed` | work item 条数 | **自然周**（北京时间周一清零） | UTC+8 |
| 本周新建 | `harness.wi.iso_week_created` | work item 条数 | **自然周**（北京时间周一清零） | UTC+8 |
| （不在四格里） | `harness.wi.week_created` | work item 条数 | **滚动 7 天** | — |
| 今天真用掉 | 新输入＋缓存创建＋输出 | token | 自然日 | UTC |
| 缓存复读 | `usage.today.cache_read_tokens` | token | 自然日 | UTC |
| 本月各 Agent 用量 | `usage.mtd.by_agent` | token + 摊销美元 | 月初至今 | Asia/Shanghai |

- `harness.wi.week_created`（滚动 7 天）与 `harness.wi.iso_week_created`（自然周）
  是**两个不同的窗口，数值必然不等**，文案上必须各自标清窗口，
  **不许只写「本周」**。滚动窗口天天在变，没法跟上周比。
  `@anchor const wiIsoWeekCreated :: "harness.wi.iso_week_created"`
- 四格吞吐必须**四个数一起出现**，不得只留其中一部分。
  2026-07-31 我把「今天」两格删了，理由是上面的 `todayLine` 那句话已经说过一次，
  大鸟当天就发现「今日的 wi 汇总也都没有了」。**一句中文要读完才知道数字，
  四个格子是一眼扫过去**；删掉之后「今天」与「本周」还失去了并排对照。
  教训：**「重复」不等于「冗余」**——同一个数字以叙述与可扫视表格两种形式出现，
  那是两个用途。判断一处呈现该不该删，得问「谁在用它、怎么用」。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testAllFourThroughputNumbersStayVisible`
- 四格里读不到的数必须显示「未知」，**不得**显示 0。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testUnknownThroughputCountsStayUnknown`
- 「今天真用掉」必须是「新输入 + 缓存创建 + 输出」，**不得**含缓存复读。
  实测 2026-07-29：1.2245 亿输入里 1.2069 亿是缓存复读，面板因此显示「1.2 亿」，
  而真实工作量只有 37 万——数字大三百倍，且涨跌主要取决于上下文被重读几遍。
  缓存复读单独一行报出，并写明「不计入上方总量」。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testWorkingTokensIncludeCacheCreationButExcludeCacheRead`
- 摊销表最多画 `PanelThresholds.monthlyCostRows` = 6 行，
  被截掉的行**必须**以「另有 N 个，合计 $X」出现，钱不得静默消失。
  `@anchor const monthlyCostRows :: 6`
- 「本月固定订阅」（账单，整数分、精确守恒）与「本月各 Agent 用量」
  （按输出占比估算的摊销）是**两个问题**，呈现上必须让读者分得清，
  **不得**把估算当账单。两者用不同的格式化函数：`usdCents` 走整数分，`usd` 保两位小数。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testPerAgentMonthlyUsageSurvivesAlongsideTheBill`
- 账单配置事实与归因结果是两个独立状态域：归因证据不足时，
  已知的组件与总额**仍须可见**，不得一起消失。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testKnownBillFactsSurviveInsufficientAgentAttribution`
- 覆盖天数必须说出口（「数据覆盖本月 N 天」）——报「本月」却只覆盖 17 天而不说，
  读者会以为那是整月的数。
  `@anchor symbol mtdDaysCovered`

### 6.2 措辞纪律

- 「要你操心的」与在飞项等面向大鸟的文案**一律不得**出现能力 id、metric id、
  英文缩写、内部代号。`s2.ledger_invariants: 5 findings` 是错的，
  `协作账本有 5 件事没收尾` 是对的。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testConcernWordingContainsNoInternalJargon`
- 停滞提醒必须说清「几件」和「最久几天」两个数。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testStalledConcernSaysHowManyAndHowLongInPlainWords`

### 6.3 时区标注

- 面板头部时刻必须是**北京时间**并显式标注，走 `StatsFormat.clockBeijing()`，
  时区钉死 `Asia/Shanghai`，**不得**用机器本地时区。
  `@anchor const displayTimeZone :: "Asia/Shanghai"`
- 走 UTC 显示的地方必须**当场标注 UTC**：配额重置时刻的提醒文案、
  残留对话的「最后说话」时刻、以及页脚的 `PanelStatusText`。
  `@anchor symbol clockUTC`
- 快照时间戳晚于当前时间时，新鲜度必须显示 `时间对不上`，
  **不得**显示「-2 分钟前」那种谜语。
  `@anchor symbol age`

> **登记一处不一致（非 bug，但值得盯）**：同一块面板上，
> 头部与事故卡片用北京时间，页脚 `PanelStatusText.describe()` 用 UTC。
> 两处都带了时区标注所以不算错，但 v3 spec §4 把「UTC/CST 混写」点名为既有病，
> 而页脚这一处仍是 UTC。改不改属产品判断，本 spec 只如实登记。

---

## 7. 必须由真人看的（`manual`）

**这一节不是偷懒。** 2026-07-31 装机后大鸟连报四处真 bug，其中「wi 四格误删」与
「事故卡片全英文」都在呈现侧，**代码门与 fixture 全绿、只有真人看一眼才发现**
（auto-memory `tests-encode-intent-so-wrong-intent-passes-green`）。
另有实测教训：菜单栏 popover 用脚本点不开，GUI 行为只能真人验
（`menubar-popover-not-drivable-by-applescript`）；Swift GUI 测试经 ssh 必崩
（`swift-gui-tests-unrunnable-over-ssh-compile-core-standalone`）。

以下每条都必须在 **ownermac 的 GUI 会话**里做，vultr 上做不了。

- 菜单栏必须四个读数槽齐全（CODEX、CC、AGY 三个配额 + WI）。
  `@anchor manual 菜单栏四槽齐全 :: 看屏幕右上角菜单栏。应同时读到「CODEX nn%」「CC nn%」「AGY nn%」与「WI n」四个槽；某个配额读不到时该槽显示「—」而不是 0%。缺槽或出现 0% 顶替都是违约`
- 菜单栏数字必须不被 22pt 栏高裁切。
  `@anchor manual 菜单栏字不被裁 :: 菜单栏高约 22pt。凑近看「51%」这类数字的上下缘，字形不得被切掉一截；与旁边系统项（时钟、电池）的基线比，应大致齐平`
- 仪表盘 wi 四格必须都在。
  `@anchor manual wi 四格齐全 :: 点开面板，看最上面的仪表盘段。「今天收口 / 今天新建 / 本周收口 / 本周新建」四个方格必须同时存在，缺任何一个 = 2026-07-31 那次误删复发；四个格子上方还应有一句「今天 收口 N 件 · 新建 N 件 · …」的中文结论`
- 跨端真源状态必须与 live exporter 同一份快照一致。
  `@anchor manual 四端真源可见 :: 先在生产 exporter 的同一份 live snapshot 核对 global_authority.status/scope/freshness 与端覆盖；再点开 App 仪表盘。只有 validated/global/fresh 且 4/4 时可见「四端真源已验证」绿灯与同 revision 前缀；carrier 缺失、过期、部分覆盖或版本不兼容时必须显示未知/不完整且不绿。Agent 回复或单机 exporter 输出不能替代这一眼。`
- 在飞项默认只能是一份折叠库存摘要。
  `@anchor manual 在飞库存不重复行动 :: 点开面板看「要你出手」与底部「在飞项库存」。库存默认只显示总数及「人工 N · 自动 N」，不得默认再铺一份人工 WI 清单；点「展开下钻」后应能看到全部在飞 WI，且可复制 WI ID/status。`
- 事故卡片必须是中文。
  `@anchor manual 事故卡片说中文 :: 点开面板看「未闭环事故」段。若有卡片，标题、「影响：」、「最近更新：」、「建议：」四行必须都是中文。整张卡英文 = 上游 escalation 塞了英文模板（2026-07-31 大鸟报过），代码门与 fixture 都发现不了`
- 面板文字必须可选中复制。
  `@anchor manual 面板文字可复制 :: 在面板里按住拖选一段文字，重点试「在飞项」那行的 wi id。应能高亮并 Cmd-C 拷出；选不动 = 最外层 textSelection 挂掉了`
- 深浅色两种模式下都要读得出。
  `@anchor manual 深浅色对比度 :: 系统设置 → 外观，切浅色看一遍面板、再切深色看一遍。重点看三处：事故/告警卡片左侧的红色竖条、卡片的浅灰底色、tertiary 级的小灰字。两种模式下都必须读得出，不得有一种模式下糊成一片`
- act 满载时面板不该显挤。
  `@anchor manual 满载面板不挤 :: 准备一份 act ≥ 10 件的快照喂给 App，点开面板。看 420×580 这个固定尺寸下卡片是否挤成一团、要滚多久才能到最底下的「在飞项」段`
- 长列表滚动不该卡。
  `@anchor manual 长列表滚动 :: 准备一份 watch ≥ 60 件的快照，点开面板上下快速滚动十几次，看有无掉帧或卡顿`
- 用量表表头必须点得动且真排序。
  `@anchor manual 表头排序可用 :: 点开面板找到分 Agent 用量表。依次点「输出」「回合」「会话」三个表头：每点一次箭头应换向，且下方行序真的跟着变；再点「Agent」列，默认方向应是 A→Z 而不是 Z→A`
- 刷新按钮与 R 快捷键必须都生效。
  `@anchor manual 刷新按钮生效 :: 点开面板，记下页脚的时刻，点页脚「刷新」，十几秒内页脚时刻应变新；关掉面板重开，改按键盘 R 键，应有同样效果`
- 静默窗口内主动点开面板仍须取数。
  `@anchor manual 夜间开面板仍取数 :: 在北京时间 20:00 到次日 08:00 之间点开面板，记下页脚时刻，等十几秒。时刻应更新到当前——静默窗口只拦后台定时轮询，不该拦大鸟主动查看`
- 退出 App 后不得留下子进程。
  `@anchor manual 退出无残留子进程 :: 点面板页脚「退出」，然后在终端跑 pgrep -f pulse-stats-snapshot 和 pgrep -f "ssh.*vultr-pulse"，两条都应无输出。有残留 = 子进程两段式终止没收干净`

---

## 8. 已知的死代码（登记，不清理）

以下类型在 `PulseStats/` 里**零引用**，既不被生产代码调用，也不被测试引用。
它们是 v2「双菜单栏 + 五段面板」形态被 v3 合并后留下的残骸。
本 spec 只登记，清不清是另一件事——但**不得**把它们当作「已实现的能力」。

- `RelayPanelView`（261 行，整个文件）确实无生产调用点。
  `@anchor wire RelayPanelView :: unwired`
- `HarnessPanelView`（137 行，整个文件）确实无生产调用点。
  `@anchor wire HarnessPanelView :: unwired`
- `RelayMenuBarLabel` 确实无生产调用点（v2 的 ⚡ 栏）。
  `@anchor wire RelayMenuBarLabel :: unwired`
- `HarnessMenuBarLabel` 确实无生产调用点（v2 的 🔧 栏）。
  `@anchor wire HarnessMenuBarLabel :: unwired`
- `MenuBarSlot` 只被那份「上小下大两行槽」的注释提到，实际未被 `PulseMenuBarLabel` 用。
  `@anchor symbol MenuBarSlot`

> 连带影响：`PanelComponents.swift` 里的 `PanelSection` / `ConcernList` /
> `DualFigure` / `MeterBar` / `Sparkline` / `UsageBarRow` / `PanelFooter` 七个组件
> 也只被这两个死面板引用。`StatRow` / `QuadStats` / `LampDot` 三个仍在用。
> **这意味着 v2 §2.3 那条「24 小时曲线」在现在的面板上根本没有渲染点**——
> `Sparkline` 活着，被 `RelayPanelView` 调着，而 `RelayPanelView` 本身没人调。
>
> **这七个组件刻意没绑 `wire` 锚点**，因为工具在这里会给出误导性的 `holds`：
> `wire` 判据是「在 `--src-glob` 范围内有没有调用点」，它答不了「调用它的那个人
> 自己是不是死的」。传递性死代码（A 调 B，但没人调 A）超出 `wire` 的表达能力，
> 详见 §13。

---

## 9. README / 旧 spec 与代码不一致处（漂移证据）

按 SPEC-RULES「README 与代码不一致时以代码为准」，逐条如实登记：

| # | 出处 | 文档说 | 代码实际 | 性质 |
|---|---|---|---|---|
| 1 | v2 spec §2.1 | 两个 `MenuBarExtra`（⚡ Relay + 🔧 harness），**必须**共享 store | 只有一个 `MenuBarExtra`（v3 §3 合并），共享问题已消失 | v3 已推翻 v2，v2 未标注作废 |
| 2 | v2 spec §2.2 | Relay 面板五块：要你操心的 / 算力 / 今天用量 / 谁在烧 / 投递质量 | 该面板（`RelayPanelView`）**零引用**，实际面板是四段式 | 死代码，见 §8 |
| 3 | v2 spec §2.5 | harness 面板四块 | `HarnessPanelView` **零引用** | 死代码 |
| 4 | v2 spec §2.3 | 一条 24 小时曲线，「本面板唯一需要曲线的地方」 | `Sparkline` 存在但无调用点，现面板不画曲线 | 能力存在但未接线 |
| 5 | v3 spec §4 | 面板三段：要你出手 / 我在盯 / 已经过去 | 实际四段：仪表盘 / 未闭环事故 / 要你出手 / 在飞项；`watch` `done` **不再渲染**（07-28 大鸟「我不用看」） | v3 之后又改过，v3 未回写 |
| 6 | v3 spec §3 | 菜单栏形态「上小下大两行」，标签 6.5pt / 数值 11pt | 实际是单行合成 `Text`，字号 12pt（`MenuBarMetrics.fontSize`）；两行槽 `MenuBarSlot` 未被使用 | 设计上屏失败后改法，spec 未回写 |
| 7 | README | 「periodic refresh every 30 minutes with a 20:00-08:00 Beijing quiet window」 | 属实。但 README **没写**轮询步长 60 秒，也没写「打开面板即无条件刷新」这条绕闸 | 文档不全，非错 |
| 8 | README | 「168 Swift tests and 212 Python tests pass」 | 2026-08-10 现算 `PulseStatsTests/` 共 198 个 `func test*`（口径 = grep -c 'func test' PulseStatsTests/*.swift 逐文件合计；仅计数未跑） | 数字已过时 |
| 9 | README | 「Not implemented: … Preferences/Clear Cache UI」 | 属实：`FileSnapshotCache.clear()` 有实现且有测试，但**无 UI 入口** | 文档与代码一致 |
| 10 | v2 spec §5 | 历史落盘每次成功刷新追加一行 | **2026-08-01 已接线**：`PulseStatsApp.init()` 现构造 `HistoryStore` 并传入 `history` + `historySample`（映射为 `HistorySample(snapshot:)`）。构造失败则两者一并留空，优雅降级回「不写历史」，与 `FileSnapshotCache` 失败回落 `MemorySnapshotCache` 同形 | 一致（**Swift 侧未编译验证**，见 §11.1） |
| 11 | v3 spec §7 | S4「飞书推送闸（只推 act）」未做 | Swift 侧确认无任何推送代码 | 一致 |
| 12 | v1 引用 | README 指 `../../Stats/specs/pulse-stats.md` v0.1.2 | 该文件存在 | 一致 |

---

## 10. 本 spec 明确不覆盖的

- **exporter 侧（Python）**：分档判据、事件收敛、metric 生产口径、账单归因算法。
  那是另一份 spec 的事，本文只写「App 拿到之后怎么呈现」。
- **wire shape 冻结**：`billing.mtd.*` 六个 metric 的线上形状由 v2 spec §4 定义，
  本文不复刻。
- **v1 的技术架构 / 安全与隐私 / 无障碍 / 回滚**：继承 `pulse-stats.md`，不重复。
- **构建与测试命令**：README 已有，且 ssh 环境跑不了（本次亦未跑）。

---

## 11. 未决问题（判断不了的，如实挂着，不注水）

1. ~~**历史存储实际没接**~~ **已接线（2026-08-01），但接线本身未经编译验证。**
   `PulseStatsApp.init()` 现构造 `HistoryStore()` 并传 `history` +
   `historySample: { HistorySample(snapshot: $0) }`；映射实现在
   `Transport/HistoryStore.swift` 的 `extension HistorySample`，
   20 个字段逐个取自 `SnapshotMetrics` 的可选访问器，**取不到一律 nil 不填 0**。
   `deliveryTotal` 无独立 metric，取自 `delivery.today.sent` 的 `sample_count`
   （exporter 侧 `sample_count=usage["delivery_total"]`）。
   `@anchor symbol HistorySample`
   `@anchor test Bigbird/Stats/PulseStatsTests/HistoryStoreTests.swift :: testRealProductionSnapshotProducesANonEmptySample`
   `@anchor test Bigbird/Stats/PulseStatsTests/HistoryStoreTests.swift :: testMissingMetricsMapToNilNeverZero`

   **残余未知**：改动方（vultr）无 Swift 工具链，**编译与测试都没跑过**，
   正确性只靠逐符号 grep 核对。要证成得在 ownermac 上跑一次
   `xcodebuild test`；在那之前「接上了」只是代码事实，不是运行事实。
2. **`harness.wi.day_created` / `day_completed` 的时区**。Swift 侧只是读数，
   自然日边界由 exporter 定；v2 spec §3.2 说 obs 聚合按 **UTC** 自然日，
   而四格标题写的是中文「今天」，面板头部又是北京时间。
   同一屏上「今天」可能指 UTC 的今天——**这正是 counter-label 那条教训的形态**。
   要断需读 exporter，超出本次范围。
3. **死代码清不清**。§8 那 4 个类型 + 7 个组件近 400 行，留着会让下一个人以为
   那些面板还活着（我这次就先信了 v2 spec 才发现）。删是不可逆动作，需主身定。
4. **`MenuBarSlot` 的去留**。它连着一整段解释「为什么两行槽是对的」的注释，
   而实际上屏失败后改成了单行。注释与现实相反，比没注释更误导。
5. **`inFlightNotEscalated` 与 `inFlightStale` 两个属性是死代码。**
   实测全仓（`PulseStats/` + `PulseStatsTests/`）零引用——连测试都没有，
   面板用的是 `harness.inFlight` 全量。它们对应 v3 时期「在飞 − 要你出手」
   与「停太久那几件」两个切法，后来面板改成全量铺开，这两个切法就悬空了。
   删还是接，需主身定；本 spec 只登记。
6. **README 的「168 Swift tests」与现算 198 个 `func test*`（2026-08-10）** 有差，
   可能是计数口径（参数化/跳过）差异，未跑测试无法证实。

---

## 12. 取证命令

```bash
# 本 spec 的锚点自检（在 vultr 上可跑，不需要 GUI）
cd /Users/owner/Coevolution/Bigbird/PULSE/skills/spec-anchor
python3 src/scan.py --repo /Users/owner/Coevolution/Bigbird/PULSE \
  --spec-glob 'Stats/specs/stats-app-presentation.md' \
  --claim-dialect prose \
  --src-glob 'Bigbird/Stats/PulseStats/**/*.swift' \
  --test-glob 'Bigbird/Stats/PulseStatsTests/**/*.swift' \
  --history-path 'Bigbird/Stats/'
```

§7 的 manual 项**不在**这条命令的判定范围内，必须由真人在 ownermac 的 GUI 会话执行。

---

## 13. 工具在 Swift 上的两处可疑判定（登记，供 spec-anchor 改进）

Swift 的 `wire` 判定路径此前从未经真实检验（今天刚补方言支持）。本次实跑
24 个探针符号，发现两类判定不可直接采信——**记在这里免得下一个人被它误导**。

1. **`wire` 抓不到传递性死代码。** `Sparkline` / `PanelSection` / `MeterBar` /
   `DualFigure` / `UsageBarRow` / `PanelFooter` / `ConcernList` 七个组件全判 `holds`
   （「有 N 处生产调用点」），但调用它们的唯一去处是 `RelayPanelView` 与
   `HarnessPanelView`——而这两个面板自己**零引用**。
   判据是「有没有人调我」，答不了「调我的人自己是不是活的」。
   这不是误判逻辑错，是 `wire` 的表达能力边界；要抓这类得做可达性分析
   （从 `@main` 出发算传递闭包）。**结论：`holds` 的 wire 锚点不能单独当「这段代码活着」的证据。**

2. **Swift 的「纯类型引用」被判成空接线。** `HistorySample` 判 `broken`
   （「12 处命中均为定义/声明」），但它是**真实使用中**的类型——
   `SnapshotStore` 拿它当闭包签名 `(@Sendable (PulseSnapshot) -> HistorySample?)?`、
   当函数形参、当返回类型。`wire` 的 `call_like` 判据只认 `符号(` 与 `.符号`
   两种形态，而 Swift 里一个类型可以全程只出现在类型位置上，一次都不「被调用」。
   `MenuBarSlot` 判 `broken` 同理，但那一条**结论恰好是对的**（它确实没被用）——
   **对的结论配错的理由，比错的结论更危险**，因为它会让人以为判据可靠。

> 两条合起来的实操建议：Swift 仓里 `wire` 锚点只适合绑
> **函数/方法**（有真实调用点形态），绑 **类型/View struct** 时正反都可能错。
> 本 spec 只对四个「整文件级死代码」用了 `wire :: unwired`，
> 且每一条都另用 grep 独立复核过（见 §8）。
