---
feature_name: "pulse-compute-cost-management"
version: "1.1.2"
status: "in_progress"
created: "2026-08-17"
modified: "2026-09-19"
owner: "Pulse"
upstream_dependency:
  - "specs/pulse-compute-cost-requirements.md"
  - "specs/pulse-compute-cost-design.md"
tags: ["compute-cost", "tasks", "migration"]
---

# PULSE 算力成本管理 — Tasks

## 当前交付状态（2026-09-19）

本文件分别记录已交付成本消费路径与尚未完成的整体能力，不从代码量、PR 合并或单项 UAT 推断整个 WI closeout。用户已确认本轮新版Stats基本符合预期，当前前沿是文档收尾；月份历史、SCL质量来源与完整WI ROI不在本次发布验收范围，不能因收尾勾完原整体任务。
已证生产 SHA、窗口、水位、App 身份、两次自然发布及重启统一见[Stats 已证基线](stats-query-dashboard.md#已证基线与边界)，本表不重复维护实时数字。

| 工作面 | 已证结果 | 仍保留的边界 |
| --- | --- | --- |
| 协作凭据及原查询 | 原 UsageLedger/CLI、原生/Host 核销、修正链及旧水位用于正式 Stats；默认 WI 价表与 Stats 一致。 | 完整 WI 执行库存、所有 Runtime/入口覆盖不由已扫描集合证明。 |
| 身份和业务归属 | Codex/Pi 与活动SCL绑定、PULSE输入凭据、飞书/Telegram、cron/无标签已采用；#591补通Pi精确工作区Actor及独立非WI诊断归属。 | 不以工作区猜 WI，不改历史模型，不把未知统一改成无；新的遗漏仍须证据链修复。 |
| Stats呈现与同源明细 | 当前运行与期间统计分开；四栏五行预览＋可放大八列明细、统一五卡片、默认折叠左对齐、双行CODEX百分比、元信息优先事项已采用，用户确认基本符合预期。 | 同一成果引用不可逐行相加；全部Runtime今日Token不是Codex专属数量；菜单/资源/事项状态不证明成本完整。 |
| WI Outcome来源 | #583接入已登记规范Outcome文件并提供名称/结论；原评审占位与成果分开，#591保留该链。 | 不重做接受/审核，未证明所有节点历史成果齐全，缺产出不记0。 |
| 解析/计价/传输复用 | 不变解析和金额复用，21视图按依赖缓存；前缀证明后有界分帧增量传输，已验证跨旧64MiB整档边界和断连后自然恢复，历史事实守恒。 | 保留帧/线传输/磁盘/超时边界，仍做原来源/账本/SCL核对；不是无限容量、永久无断连或完整业务分母。 |
| 安装与自然更新 | #626正式两次自然成本发布与App重启一致，第三轮另证TCloud来源恢复；菜单、事项、原Oikos及九项暂停已实际回读。 | 双周期查询一致不代表当轮所有来源read；历史gap、长期可靠性、整机启动及业务cron重新启用后交付仍分别验收。 |
| 质量、人类投入、收益与 ROI | 原合同和有证据时的查询/验证能力保留。 | 缺交付/审核/收益 producer 或库存的自然 WI 不报完整指标；不是默认返回 0 或宣称已完成 R3。 |

## 原 receipt-first 计划的采用情况

[PR #512 方案](pulse-compute-cost-receipt-first-iteration-plan.md)的五类事实、自然粒度与反例仍有效。下表区分已落地子路径与整体退出条件，不能将阶段性 Stats 发布写成全部 R1–R4 完成。

| 切片 | 已采用部分 | 未由本轮完成的退出条件 |
| --- | --- | --- |
| R1 | 版本化凭据与用量/归属/时间/计价合同及反例。 | 全 runtime×entry×host 独立库存、所有异常/子调用样本及长期保留边界。 |
| R2 | 登记采集→既有账本→query/wi-report→Stats 的真实已启用路径与重启/恢复。 | 完整质量/收益来源、全 WI epoch/请求核销和共享分配权威。 |
| R3 | 当前三节点、多 Agent/入口的本周用量与正式消费者读回。 | 五个自然 WI 的全部五域证据、已启用 Runtime 的完整样本；原 S5 七天/十 Outcome 门不替代。 |
| R4 | WI/期间/Agent 查询与日周月 21 视图已用于生产；数量、已计价部分与未知原因可查。 | 月度历史清理、完整质量/价值/ROI 与实际下游收益链不能由表格正常显示推定。 |

## 历史修复与扩展任务记录

以下记录保留各原始时间、候选结论、锚点与未完成门。历史“待部署”“默认关闭”只描述其当时/特定子路径，当前 Stats 的事实以前两节及现行 Specs 为准；不把旧 S0/S2 的订阅分摊 shadow 重新接入生产。

## WI568 independent-review repair — 2026-09-11

PR #493 / Issue #492 repair three blockers under existing WI568/WI779, without creating another WI or
eligible S5 sample. Source acceptance requires valuation source binding, no false-exact coverage, and
actual child-process death recovery/live-owner exclusion. Installed runtime validation and original
program outcome/observation gates remain separate; a green source suite is not whole-WI closeout.

| Item | Required evidence | Status |
|---|---|---|
| Valuation source binding | Same-WI stale/equal-quantity other-source negatives; conserving positive | Implemented in PR #493; exact-head CI required |
| Usage completeness | Missing producer/open epoch/model conflict cannot become exact; retained unknowns | Implemented in PR #493; exact-head CI required |
| Pi writer crash recovery | Actual SIGKILL before sample/publish; live writer exclusion; fresh retries | Implemented in PR #493; Linux and macOS verification required |
| Installed consumer path | Paired producer/extension revision and real entry/readback | Not claimed by source tests |
| Original WI568 completion | Prospective ChatGPT+GitHub evidence and legitimate 7-day/10-eligible-outcome gate | Unchanged; must read actual qualifying evidence |


## 1. 风险与历史切片

- Feature risk：**high-risk**。最终会影响账单语义、跨机数据、Stats、周巡检和生产路由。
- 下列历史 slice：**WI779 machine-cash policy consolidation**。在已合并 flat standard OpenRouter pricing 的 base 上
  收敛剩余语义矛盾：统一 `openrouter_standard_token_cash` 政策（basis/cash coverage），删除
  `actual_marginal_cash` 独立权威（provider/Harness 原生 cash 只作非权威上下文），retrospective Pi estimate
  显式带 `may_include_unrelated_turns`/`noncanonical_total` 标志并暴露 `retrospective_machine_cash_usd`。
  canonical lifecycle/source 不回填，subscription settlement/native cost/overrides 不进入 WI report/ROI，
  single WI machine cash 唯一由 flat OpenRouter standard Token 政策定义。
- 旧 S0-S4 candidate 能力继续可读；v2 已取代“把 ChatGPT 月费按 WI token 分摊”和“浏览器/导出是主路径”
  的假设。旧 PR `#12` 的 S5 canary 不可越过 v2 usage 与 WI-level query 缺口直接晋级。

### WI730 v2 任务

| ID | 任务 | 状态 | 生产影响 |
|---|---|---|---|
| V2-001 | App Server `last/total/context-window` parser、epoch baseline/update/final seal、append-only ledger | 完成：PR #228 merge + exact-main trusted fallback green；生产 observer 仍默认不运行 | candidate library + host-callable CLI；默认不运行 |
| V2-002 | Codex App / ChatGPT Work 常驻 host subscription 与 final-turn ordering | 进行中：embedded 0.149 notification 与 thread-rate-card schema 已对拍，host-bound parser 已实现；App stdio owner 尚未接线 | read-only observer |
| V2-003 | Relay Pi、CC/CC-SDK、OpenRouter producer adapters 与 procurement evidence | 进行中：Pi/App Server 已通过；CC/CC-SDK 的受管 cc-session-probe 已接入既有 runtime_usage_sample/v2 构建器，删去重复 Token 解析；边界包络与原 binding digest 保留。OpenRouter request sample 与 management activity/price read-only probe 保留；WI300 的持久累计解释、差额守恒与来源快照增量按现行设计验证；CC/SDK 真实节点 SCL 采用与封存 UAT 单独核验，不以源码通过替代部署 | read-only evidence |
| V2-004 | `computecost <wi>` 联结产出、质量、成本、成效与效率 | 进行中：usage/quota、Harness quality/effect/efficiency、thread rate-card、exact review artifact set、policy machine cash（flat standard OpenRouter Token）与 consumer-verified business-value receipt joins 已实现；provider 原生 cash 只作非权威上下文；WI730 尚无 approved review/value receipt，真实字段保持 unavailable | read-only query |
| V2-005 | prospective WI epoch、rollover source seal/target baseline、重复与 crash/restart UAT | 进行中：PR #374 已 merge、适用 CI green 并部署；epoch 5 的 189 个与 epoch 6 的 32 个 post-baseline events 均完成自然 final seal、Token 守恒与隐私现场 PASS；cloud-linux-b 窗口已结清；macOS 冻结与容量准入完成，但 current loaded contract 缺少合法 cross-surface workspace-authority transition，registration 在 baseline 前被 `authority_unavailable` 阻断且 partial manifest 不可执行；Tencent Cloud 与受控 live crash UAT 尚待完成 | shadow UAT；见 `ComputeCost/uat/wi568-epoch5-final-seal-uat-20260904.md`、`ComputeCost/uat/wi568-epoch6-final-seal-uat-20260906.md`、`ComputeCost/uat/wi568-macos-prospective-uat-preflight-20260906.md` |
| V2-006 | WI272 retrospective unknown-preserving projection | 完成：真实 canonical measurement projection 接入；历史 usage/token/cash 保持 unavailable，ComputeCost 161/161 PASS | read-only，不改终态 |
| V2-007 | Pi reused/branched session prospective delta 与 zero-manual SCL join | PR #430 已合并并完成 installed UAT；只对 prospective exact pair 成立，不 retroactive seal | read-only report + managed launch fail-closed |
| V2-008 | terminal legacy WI 的 registered Relay/TG Pi retrospective candidate | 完成：PR #460 merge；WI383 cancelled regression 保留 | read-only；不进入 canonical totals/cost/ROI |
| V2-009 | WI342 defects：structured Codex verifier lineage、attention vector、flat WI pricing | PR #467 已合并；历史真实来源证据仍 partial；其已知子集不代表 canonical 全量，PR #493 修正其金额/coverage 消费合同 | read-only report；不改 lifecycle、不 merge/deploy |
| V2-010 | machine-cash policy consolidation：统一 `openrouter_standard_token_cash`；删除 actual_marginal_cash 独立权威；native cash 只作非权威 context；retro 现金估计标志与 exposure；schemas/README/specs/tests 全量对齐 | PR #468 已合并；后续 #479/#488 已合并；PR #493 补齐 source-bound valuation 与 crash-recovery 完整性，不重写历史 UAT | read-only report；不改 lifecycle、不 merge/deploy |

### WI300 consumer 任务

原 C-001/C-002 的 managed shadow 方案已由同一查询型成本路径替代，不再继续等待旧 shadow 上线。

| ID | 当前落点 | 已证状态与限制 |
| --- | --- | --- |
| C-001 | ComputeCost `stats_dashboard.py` 生成同水位 21 视图，受管 refresh 维护原登记/价表。 | 已安装并自然发布；不是订阅分摊 managed snapshot。 |
| C-002 | Vultr固定exporter → 正式App四栏预览/完整八列窗口、Outcome扩展、单一统计详情及本地缓存。 | 已通过同水位对照与重启，当前显示获用户确认；不重计价，不引入旧billing/shadow。 |
| C-003 | ownermac/Vultr/TCloud实际读取端、两次自然发布/重启及同步恢复。 | [35355435323](https://github.com/bigbird-online/PULSE/actions/runs/35355435323)通过，第三轮补验断连恢复；原始记录见[统一基线](stats-query-dashboard.md#已证基线与边界)。整体WI与历史范围不一并关闭。 |

## 2. 任务表

| ID | 任务 | 状态 | 生产影响 |
|---|---|---|---|
| S0-001 | 三件套 Spec、replacement matrix 与 Duke R2 | 完成 | 无 |
| S0-002 | OpenRouter 价格/用量只读合同与脱敏汇总 | 完成 | 无，显式 CLI |
| S0-003 | Decimal 参考估值、Pro 分配与 managed cost 月结 | 完成 | 无，纯函数 |
| S0-004 | 自动测试、live read-only probe、spec-anchor | 完成 | 只读外部 API |
| S1-001 | `usage_event.v1` 与 Relay/Codex/CC adapters | 完成 | candidate library；无生产入口 |
| S1-002 | 幂等 ledger、迟到事件、北京月界 | 完成 | candidate library；未创建生产 ledger |
| S1-003 | P0 lineage contract、Codex parent evidence 与 fork copied-prefix | 完成 | candidate library；无生产入口 |
| S2-001 | `managed_cost_snapshot.v1` 与 Stats shadow | 完成 | Stats feature flag 默认 false；legacy 保持可见真源 |
| S3-001 | 周窗口查询与 Duke recommendation artifact | 完成：实现/合同 CI + real-consumer UAT PASS | shadow / recommend-only；无 apply |
| S4-001 | 四段 bridge、recommend-only、manager 结构化 plan | 完成：implementation/contracts + PR CI + live read-only UAT PASS；以 PR #10 merge/main green 闭环 | 不 apply |
| S5-001 | manager active-canary lease、单 consumer canary 与反馈控制 | 待开始 | high-risk |
| S6-001 | legacy 双跑、系统性舍弃与恢复演练 | 待开始 | high-risk |

## 3. Replacement matrix

此表保留早期整体替换设计；Stats 的前三项已按现行查询型方案落地，不再等待 managed subscription/shadow 切换。Duke、建议/控制及 S5/S6 的历史门保留，不从 Stats 已发布推断已采用。

| 现有能力 | 新归属 | 替换条件 |
|---|---|---|
| Stats `billing.mtd.*` | managed cost subscription view | 一次完整月界 + UI UAT |
| Stats `usage.mtd.*` | reference cost / by-agent view | 事件 coverage 与守恒通过 |
| Stats Swift 成本计算 | ComputeCost snapshot | Swift 只呈现，不再计算 |
| Duke `token-ledger.sh` | usage event adapters/query | 两机与历史保留验证 |
| `team-dashboard.sh` 月度 token | ComputeCost query | 周/月窗口一致 |
| S3 周巡检手工成本段 | weekly cost artifact | 一次真实周巡检消费 |
| runtime cost UAT 手工记录 | feedback controller evidence | outcome 关联与 7d/10 门通过 |

旧链在 S6 前保持可回滚，不提前删除。

## 4. S0 验收

### AC-S0-001：价格合同

- 固定 fixture 能精确解析 prompt/completion/cache-read decimal。
- 缺模型、缺必需价格、负数全部 fail closed。
- 当前 OpenRouter 目录的目标模型能通过真实只读 probe。

### AC-S0-002：activity 合同

- fixture 聚合后的 requests/token/usage 与输入逐项守恒。
- 输出中不存在 endpoint id、key hash、workspace id、user id。
- ownermac 真实 probe 只返回脱敏摘要，secret 扫描 `0` 命中。

### AC-S0-003：估值与 Pro 分配

- input/output/cache 计算使用 Decimal 并精确复算 fixture。
- 任意非负权重分配后总和精确为 `20000` 美分。
- 零权重时 `unallocated_cents=20000`。

### AC-S0-004：锚点门

- 新三件套的已实现锚点 `broken=0`。
- 后续切片保持 manual/pending，不以断裂 symbol 假装完成。
- 全仓既有 runtime Spec 的历史 broken 锚点单独登记，不拿新模块测试掩盖。

## 5. 后续高风险验收门

1. S1：重复事件重放总量不变；乱序、迟到、月界 fixture 通过。
2. S2：Stats 真实 App 路径显示新 snapshot，legacy 可一键恢复。
3. S3：真实周巡检和 Duke 各消费一次，不手工重算。
4. S4：人为改变 retry/model/outcome 后 driver delta 精确解释整体变化。
5. S5：只允许一个 standard consumer；critical 候选低于 frontier 必须拒绝。
6. S5：至少 `7` 天、`10` 个真实 outcome；高风险错误立即局部 rollback。
7. S6：跨过完整月界后，才允许停写 legacy；兼容输出再保留 `1` 个月。
8. S6：演练 candidate→shadow→authoritative 切换及兼容期内恢复，证明任一时刻只有一个用户可见真源。

### AC-S1-003：P0 归因事实与 copied-prefix

1. 新写 v1 event 必有 `lineage` 七字段；历史 v1 缺 `lineage` 读入后七字段均为 `unknown`，不升级
   schema_version，不破坏 replay/idempotency。
2. 所有输出不得含原始 parent/session/user/message/turn id、绝对路径或 prompt；lineage run ref 只可为
   `lineage_v1_` domain-separated digest。Codex 仅在 session metadata 有明确 parent evidence 时填写
   `parent_run_ref/session_role`，root Agent 与 human/automatic 不得用 cwd/prompt 推断。
3. `fork_turns=all` depth 1/2 child 的 copied prefix 只建 baseline、不产生 event；`fork_turns=none`
   和 resume 仍对真正 child turn 产生正确累计差分，整体 token 总量守恒。
4. 月 `$200` cash settlement、周 `$50` capacity、OpenRouter reference cost、Pro quota pressure 在
   contract 和报告中分列；quota 的 reset/slope/percentage 无 producer 事实时为 `unknown`，不得由
   reference cost 推算。
5. S1 完成边界包括 Relay scheduler producer、Harness `scan_ref` consumer、Claude Code sidechain、
   Relay interactive lineage 与 privacy-safe/default-off evidence writer；不把后续 query/report 或
   recommendation 能力倒算成 S1 完成条件。

## 5.1 S1 验收记录

1. `usage_event.v1` 使用 hash id，序列化结果不含 tenant/session/message/turn id、绝对路径或内容。
2. fixture 已证明：CC raw cache 归一化、Codex 多 turn 累计差分、重复 final token_count、Relay 归因
   consumed；Relay Codex fallback 对四个累计计数器独立差分且 input 不再叠加 cache。
3. recovery/rollover 中同一 Relay 原始事实按稳定内容 hash 折叠；文件名、分区和行号变化不产生新成本。
4. 已匹配累计行仍推进四个计数器前值，后续 unmatched fallback 只产生净增量；catalog 未映射模型的
   provider/source/runtime 保持 `unknown`，不使用 session metadata 猜测。
5. ledger fixture 已证明：同 event 重放 no-op、同 id 不同事实 fail closed、北京月界与 corrections
   分流、跨月 conflict 在任何 append 前完成 preflight。
6. Vultr `2026-08-15T00:00:00Z` 起 read-only probe：`657` 个 canonical events；折叠 `112` 个
   Codex 重复副本，`106` 条 Relay 行仅作归因。数字是一次性运行证据，不作为长期基线。
7. ownermac 同窗口临时 scratch read-only probe：`138` 个 canonical events；折叠 `44` 个 Codex
   重复副本；退出后 `/tmp/pulse-compute-cost-s1.*` 为 `0`。未读取或复制 management key。
8. 两机所有事件当前仍为 `partial`：task/outcome/retry/route 关联尚无完整上游证据。S1 只完成
   用量事实层，不据此宣称成本闭环或 downstream replacement 已完成。
9. S1/S2 集成 PR `#7` 已于 `2026-08-18T23:54:50Z` 合入 `main`（merge commit
   `0d9caaa0f82548b523783951375aec902c468db5`）；S1 producer/consumer/lineage/default-off 证据链随该
   集成提交进入 main，未改变生产 route。

`@anchor test ComputeCost/tests/test_adapters.py :: AdapterContractTests.test_runtime_evidence_wins_and_relay_only_attributes_or_falls_back`
`@anchor test ComputeCost/tests/test_ledger.py :: UsageLedgerTests.test_cross_month_conflict_is_preflighted_before_any_append`

## 5.2 S2 验收记录

1. `ManagedCostSnapshot` 已实现 Decimal string、输入事件集 digest、价格 digest、北京月和证据时间边界；
   同聚合不同事件集、真实量级小数、保留分区碰撞、月外事件与未来证据均有反例。
2. Pro 分配与 `by_agent` 同时守恒；零权重生成独立 `unallocated=20000` 分区；unknown source 进入
   unpriced/partial，未伪装成已知零。
3. OpenRouter actual cash 仍是旁路；有金额必须带脱敏 activity digest 与 UTC 日窗口，完整覆盖只允许
   closed 月结。
4. Stats 的 `LivePaths()` 默认关闭；真实入口只认
   `PULSE_COMPUTE_COST_SHADOW_ENABLED=1` + 绝对 path。关闭不读文件、不产新指标；开启后坏文件、
   旧月、closed、stale 和未来快照整束 unavailable。
5. Python 跨模块契约测试由真正 `build_managed_cost_snapshot()` 生成输入，再由 Stats 独立 reader 接收；
   篡改守恒或 identity 后拒绝。
6. Swift typed bundle、Decimal/整数守恒与溢出 fail-closed 反例均已进入集成 PR `#7`。PR head
   `7c71d74713e1c73bf056f659cfd69df771a36bd3` 的 `Stats macOS CI` run `32199059592` 成功：
   scratch DerivedData build/test 未安装或启动生产 App，完整 XCTest 为 **204/204**。
7. 真实 App render UAT 已由 `NSHostingView` 路径执行
   `testManagedCostShadowRenderedAppPathCoexistsAndRollsBackToLegacy`：有效 shadow fixture 出现独立
   managed-cost 区块；关闭 flag 后该区块消失且 legacy billing/usage 展示保持不变。该 UAT 与 CI
   均通过，因此 S2-001 按真实证据完成。
8. 单机独占基线对 100000 条事件连续运行三次为 `4.693498 / 4.661391 / 4.393734` 秒，
   中位数 `4.661391` 秒、最大值 `4.693498` 秒；三次 `snapshot_id` 一致，满足 5 秒目标。

`@anchor test ComputeCost/tests/test_managed_snapshot.py :: ManagedCostSnapshotTests.test_snapshot_id_and_conservation_tampering_fail_validation`
`@anchor test Stats/exporter/tests/test_managed_cost_shadow.py :: ManagedCostShadowTests.test_default_service_requires_exact_flag_and_absolute_path`
`@anchor test Stats/PulseStatsTests/BillingMetricsTests.swift :: BillingMetricsTests.testManagedCostShadowParsesExactBundleAndKeepsCashOutsideManagedCost`
`@anchor manual s2-macos-app-uat :: Stats macOS CI run 32199059592；NSHostingView render UAT testManagedCostShadowRenderedAppPathCoexistsAndRollsBackToLegacy 通过，204/204 XCTest；未安装或启动生产 App`

### AC-S3-001：周窗口查询与 Duke recommendation artifact

1. `query --from/--to` 使用北京本地半开窗口并直接读取 `UsageLedger`；跨产品月时同时读取主分区与
   corrections，不依赖文件名、mtime 或人工 awk/filter。
2. 同一查询引擎支持 Agent、consumer、task、model、effort、retry、escalation、outcome、
   root Agent、human/automatic、main/sub-agent、root task、cron job、scan、日/周/月分区；
   `root_origin` 仅作为额外便利维度。当前与 baseline 每种分区分别守恒，baseline-only key 不能消失；
   每行输出总量、占比、same-key baseline 与 absolute/relative delta，缺归因进入 `unattributed`。
3. 周消费合同直接输出 `by_agent/by_consumer/by_outcome`。四段 `D→R→E→P` bridge 仍属于 S4-001；
   S3 只显式输出 `driver_delta.status=pending_s4`，不得为了提前通过 S3 伪造 bridge 解释。
4. 周 `$50` 只作为 capacity context；月 `$200` managed-cost settlement 不按周/token 再分配；
   quota pressure/reset/slope 无 producer 事实时固定为 `unknown`。
5. `recommendation_artifact.v1` 状态只允许 `hold/promote/rollback/insufficient_evidence`，携带 consumer、
   current/target/rollback preset/profile、evidence window、coverage、expected reference-cost/outcome change、
   risk floor、TTL、active-route digest、plan digest 和唯一 changed route；始终 `recommend_only` 且
   `production_apply_authorized=false`。
6. recommendation 只接受 runtime manager 已批准 preset/transition。缺 preset 或 transition 不允许时
   fail closed 到 `insufficient_evidence` 并含 `no_approved_preset`；stale price、outcome coverage 缺失、
   conservation failure 或 critical below frontier 不得 promote。只有 manager transition 合法时，
   evidence-risk failure 才可生成 rollback；任意 model/profile 字符串不得绕过 catalog。
7. S3 完成门仍是**真实周巡检和 Duke 各消费一次，不手工重算**。unit/CI 只能证明合同和算法，不能
   代替该 high-risk UAT。

## 5.3 S3 当前验收记录

1. `cost_query.v1` 已覆盖跨月 ISO week + late correction、所有 FR-P1-002 分区、current/baseline 双守恒、
   same-key delta 与 baseline-only negative delta、unknown/unpriced fail-closed；周输出保持 `$50` capacity
   与 `$200/month` settlement 分离，driver bridge 明确留给 S4。
2. `recommendation_artifact.v1` 已覆盖 valid promote、stale/outcome/conservation/frontier fail-closed、
   catalog/transition 拒绝、非法任意 profile 不回显、证据风险下合法 rollback、tampered apply authorization
   拒绝；Duke approval 不等同于 production apply authorization。
3. PR `#8` 的旧 head `56d5105c0ed6b4f7ba429e5dd97948ed801e8096` 上，正式 ComputeCost CI run
   `32208149380` 成功；同 head 的一次性 `ComputeCost S3 Runtime UAT` run `32208149369` 随后失败，日志
   根因是 workflow 自身漏设 `PYTHONPATH=ComputeCost`，导致 12 个 test module 全部
   `ModuleNotFoundError`，不是 S3 业务路径失败，而且该 workflow 本身也不能满足真实消费者门。
   closeout commit `d4fe7dfa3a6e15f53e206f64f9ecd6aabcde1b3b` 已删除它；该 commit 的正式 ComputeCost CI
   run `32213137779` 成功并实际运行 **84/84** contract tests。最终 PR head 若继续变化仍须独立 green。
4. 先前“找不到真实 consumer”的判断已被现场证据纠正：真实 Duke 周复盘入口位于
   `duke-space/skills/s3-weekly-review/SKILL.md`，旧成本输入包括 `duke-space/scripts/token-ledger.sh` 与
   `team-dashboard.sh`；`2026-W33` Duke 周复盘的资源段仍明确把“收敛成本”记为不可测，正是 S3 shadow
   输入需要替代的缺口。不得再用“仓库无 consumer”作为 blocker。
5. `2026-08-19` live scratch query 直接从 ownermac CC/Codex 证据生成 `2026-W33` 七日 artifact：
   `event_count=158`、`conservation=pass`；因当前 evidence/catalog coverage 不完整，`priced=0`、
   `outcome_linked=0`，recommendation 正确 fail closed 为 `insufficient_evidence`，changed routes 为空且
   `production_apply_authorized=false`。deterministic weekly/Duke adapter 均能在不手工重算时读取该 artifact，
   但这些 adapter 结果只证明消费合同，不冒充真实 Agent UAT。
6. Vultr production runtime manager 只读 `plan --preset phase23_canary` 证实 active preset 为
   `phase23_canary`，`hestia_weekly_review` effective profile 为 `media`，且命令以 `PLAN ONLY: no files written`
   结束；同时现场存在 `changed_files=18` 与 persisted sessions `mismatched=5` 的 runtime drift。该结果只作
   S4 structured-plan/precondition 的开工事实，不在 S3 做 apply。
7. 早先真实 LLM 尝试曾被 runtime provider 阻塞：ownermac/Vultr Codex 命中 usage limit，Vultr
   Claude Code 返回 `401 OAuth access token has been revoked`，Harness `claude_glm` fallback 返回
   `429 insufficient balance/no resource`。这些是历史失败证据，不能倒推为 S3 业务路径失败，且已被后续
   real-consumer UAT 对完成门的状态所 supersede。
8. real-consumer UAT run `32216817328` 在 self-hosted ownermac 上校验 persisted W33 query SHA-256
   `c1c13101151e2510eb7f69046156e27bdcb66d9cf136309a4ac5ec84a6278a60`，用官方 builder 生成
   recommendation，并通过 `s3-consume.py weekly|duke` 产出 zero-recalculation consumer projections；run
   终态为 `UAT_INVARIANTS_PASS zero_recalculation=true recommend_only=true production_apply=false`。
   随后 GPT-5.6 Sol 真实读取两份 projection：weekly consumer 正确保留 partial/unpriced/outcome-incomplete
   与 `pending_s4`，Duke consumer 对 `insufficient_evidence` 给出 **HOLD / NO CHANGE**，未手工重算成本或
   token，也未把审批解释为 apply 权限。因此 weekly-review 与 Duke 两个真实 consumer 均 **PASS**，未豁免。
9. machine projection 内 `duke_decision=pending` 是 recommendation 交给 Duke 前的授权状态；真实 Duke
   的 HOLD 决策属于下游消费结果，两者不冲突。run `32216817328` 当次 recommendation SHA-256 为
   `541e8f56b4975e89304d860cb809c638ab8c39f090e6edf33681e090d65fd401`；Duke `ops/` 是 runner-local、
   未 Git-track 且可被后续重建覆盖，因此长期 acceptance oracle 是该 Actions run + 本仓已提交 UAT 记录，
   不能用后续 `ops/` 文件的新 validity timestamp/hash 反向改写验收结果。
10. S4 开工边界已核对并已进入实现：四段 bridge 固定为 `D→R→E→P + residual` 且必须精确守恒；
   同一个 `pulse-runtime-priority.py` 补只读结构化 plan（route diff、active digest、plan digest、
   precondition），ComputeCost 不得另建执行控制面。S4 全程保持 recommend-only/no-apply。
11. 本切片没有调用 runtime manager apply，没有修改 production route，没有部署或重启服务；S3
   real-consumer gate 已 PASS。完整现场记录见 `ComputeCost/uat/s3-real-consumer-uat-2026-08-19.md`。

`@anchor test ComputeCost/tests/test_query.py :: CostQueryTests.test_all_required_dimensions_conserve_without_manual_recalculation`
`@anchor test ComputeCost/tests/test_query.py :: CostQueryTests.test_baseline_only_partition_is_retained_for_negative_delta`
`@anchor test ComputeCost/tests/test_recommendations.py :: RecommendationArtifactTests.test_evidence_failure_can_emit_only_an_approved_rollback_transition`
`@anchor manual s3-real-consumer-uat :: PASS 2026-08-19；Actions run 32216817328 产出 zero-recalculation weekly/Duke projection，GPT-5.6 Sol 随后真实消费并判 HOLD / NO CHANGE；production_apply_authorized=false；见 ComputeCost/uat/s3-real-consumer-uat-2026-08-19.md`

### AC-S4-001：driver bridge 与结构化 manager plan

1. bridge 固定按 `D→R→E→P→residual` 顺序；完整证据时四段 + residual 必须精确等于 observed
   delta，算术不守恒直接 invalid。cohort 只接受 producer-owned `consumer_path/task_family/risk_class`；
   缺字段不得用 task_ref/model/route 推断，必须 `insufficient_evidence`。
2. high-risk fixture 同时改变 retry、model 与 outcome：route/execution/price delta 精确重组 overall
   delta，retry 变化留在 execution evidence；outcome 只进入 accepted/rejected/human-correction 与
   cost-per-accepted 约束，不成为第五段成本 driver。
3. weekly query 不把 `$200/month` Pro settlement 人工按周分配；缺月结 counterfactual 时 bridge
   fail closed。S3 单一 comparison price 语义保持不变，S4 model-aware P0/P1 price evidence 独立输入。
4. manager `plan --preset ... --json` 必须只读产生 `runtime_priority_plan.v1`，绑定 active/target route
   digest、runtime-profile digest、route diff、catalog/transition map、precondition 与 canonical plan digest。
5. recommendation 的 promote/rollback 必须绑定完整 manager plan；schema、canonical digest、active
   digest、transition map 或唯一 changed route 任一不匹配均降为 `insufficient_evidence`；artifact 始终
   `recommend_only` 且 `production_apply_authorized=false`。
6. 显式 `apply --preset` 必须携带 fresh active-route/plan digest，manager 在写前及锁内重读后各检查
   一次；stale digest 必须在 backup/write 前失败。该能力只建立未来授权边界，S4 UAT 不执行 apply。
7. S4 UAT 必须从 ownermac 对真实 Vultr active runtime 执行 **read-only structured plan** 并证明受管
   文件字节未变化，同时执行 high-risk bridge fixture；CI green 不能代替该 UAT。

验收证据：ComputeCost `88/88` contracts PASS；runtime-manager `31/31` pytest PASS（Python 3.13）；
PR `#10` head `a5b464c1363a9695e6429255bd6d3f6f014388cf` 的 ComputeCost CI run `32254519724` PASS；
`2026-08-19` 从 ownermac 经 SSH 对 Vultr production active `phase23_canary` 执行 PR-head manager 的
read-only `plan --json`，受管文件 hash 前后完全一致、backup 未创建、apply 未调用；同一实现的
retry/model/outcome 与 recommendation fail-closed 高风险 fixture `4/4` PASS。一次性 Actions UAT run
`32254519773` 因 self-hosted runner 始终 queued 而取消，未冒充 acceptance evidence，临时 workflow 已
删除。完整记录见 `ComputeCost/uat/s4-driver-plan-uat-2026-08-19.md`。

`@anchor symbol build_cost_driver_bridge`
`@anchor test ComputeCost/tests/test_query.py :: CostQueryTests.test_s4_driver_bridge_explains_retry_model_and_outcome_changes`
`@anchor test ComputeCost/tests/test_recommendations.py :: RecommendationArtifactTests.test_promote_without_manager_plan_fails_closed`
`@anchor symbol _structured_plan`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_cost_preset_apply_requires_fresh_plan_preconditions_before_write`
`@anchor manual s4-readonly-uat :: PASS 2026-08-19；ownermac→Vultr production read-only runtime_priority_plan.v1，managed hashes unchanged、backup=false、apply=false；high-risk retry/model/outcome + manager-plan fail-closed 4/4 PASS；见 ComputeCost/uat/s4-driver-plan-uat-2026-08-19.md`

## 6. 回滚

- S0：新模块未接入口，回滚是移除新文件；生产行为不变。
- S1：停只读 collector，保留旧 Stats 与 Duke ledger。
- S2：关闭 Stats feature flag，回到 legacy metrics。
- S3：恢复 token-ledger 和手工周切片。
- S4：停 recommendation artifact，不触碰 active route。
- S5：apply receipt 记录真实 `backup-id`；single-flight 窗口内用
  `rollback <backup-id>` + `verify` 恢复 `rollback_preset`。
- S6：停新 readers/建议，冻结异常 snapshot，重启 legacy writer，从原始输入回放并守恒后切回
  legacy readers；不得通过全平台统一升/降档冒充成本链回滚。

## 7. 历史 S0-S4 切片明确不做

- 不自动修改 runtime route、compute profile 或 cron。
- 不部署/重启 Relay、Stats 或 ownermac 服务。
- 不复制、打印或持久化 `OPENROUTER_M_KEY`。
- 不把 live OpenRouter 汇总数字提交成长期 fixture。
- 不把 `pricing.overrides`、producer-native cost 或 subscription settlement 接入 WI report/ROI。
- 不用 Relay outer turns、Handoff 数、wall clock 或 time-window-only mapping 伪造 human feedback/focus。
- 不在 ownermac 或 Vultr 创建 production usage ledger；S1 live probe 只输出脱敏聚合。
- 不声明 Stats/周巡检/Duke 已被替换。
- 当时不启用 `PULSE_COMPUTE_COST_SHADOW_ENABLED`，不创建生产 snapshot writer；WI300 后续切片只增加
  显式 one-shot materializer 与固定只读 consumer，不注册常驻 writer/cron。

`@anchor manual wi779-material-defects-uat :: ComputeCost/uat/wi779-material-defects-uat-20260909.md`
`@anchor symbol summarize_openrouter_activity`
`@anchor symbol estimate_reference_cost`
`@anchor symbol allocate_subscription_cents`
`@anchor symbol settle_managed_cost`
`@anchor symbol build_managed_cost_snapshot`
`@anchor wire build_managed_cost_shadow_metrics :: wired`
`@anchor manual s2-default-off-boundary :: 核对生产环境与服务未写入 PULSE_COMPUTE_COST_SHADOW_ENABLED=1，未创建 writer/cron，legacy billing/usage 代码与显示仍保留`


## WI568 Stats integration repair — 2026-09-14

- Already-versioned `receipts` transport preserves source `fact_ref`, `supersedes` and provenance; it is not a raw snapshot to be reidentified. Replayed ancestors and local evidence refinements must not strand later source revisions. Missing predecessors still reject the atomic batch; recorded-at watermarks remain immutable.
- Codex receipt materialization retains proven native task/counter intervals. An absent interval on an already-ingested final quantity may be filled by exact same-assertion evidence; known counters/models/boundaries cannot be silently replaced. Unknown or crossing intervals retain explicit unallocated disposition.
- One explicitly registered Codex source can supply `paths` instead of `path` to reconcile active/archive copies together through the existing adapter. Other source kinds remain single-root. No automatic whole-machine discovery or new ledger.
- Pi/Codex Host model claims require native evidence whose per-model quantities conserve the same settled result. Pi uses native assistant usage, Codex the exact returned thread and its immutable pre-dispatch prefix/new task. Configured or last-result models are not historical evidence; unsupported compaction, ambiguous switches or conflicting boundaries stay unknown.
- Regression and real-replay evidence: `Bigbird/ComputeCost/uat/wi568-stats-cost-chain-20260914.md`. Production source activation and the Stats-bundled ComputeCost release remain separate gates, coordinated with WI300/#525. This repair does not close the full WI568 program or waive S5.


## WI568 current-source repair — PR #531

Continuation of WI568 and merged #526, not a new WI. Scope: actual-runtime source selection, per-file isolation, native provenance/attribution, Host/native reconciliation, visible effective-time/coverage diagnostics and controlled production adoption. Original red regressions are retained in test_current_source_contracts.py; native boundary and inventory regressions are in test_native_bound_attribution.py. Exact final CI, current-business replay and production readback are recorded in PR #531; no original S5 task is marked complete by this repair.

### WI300 当前周金额可用性 · 冲突范围修正

代码回归覆盖无 WI／明确普通对话、跨 WI／Runtime／Agent／模型／节点／入口／日期、直接查询过滤、窗外被引用执行与真实同桶数量分叉。发布验收须同水位比较原始用量／价格／全局冲突集，验证正常分组恢复金额，并读回实际 App 的相同发布物；相关用量终值／汇总冲突不因本项完成而结清。
