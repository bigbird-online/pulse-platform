# SPEC：harness 扫描与卫生面 — scan hit 全集 / context_hygiene / 扫描事件

> version: `0.1.2`
> status: `reverse_engineered_from_code`
> authority: 八类 hit 的生产判据、上下文卫生检查与扫描事件流的**现行实现契约**
> owner: Pulse（wi_20260804_070 全面 spec 化；主链 spec 已覆盖 run_scan 与
> 去重/熔断，本文补 hit 生产侧与卫生侧）
> modified: `2026-09-21`
> 版本史：只看 git log

**取证基础**：2026-08-09 代码普查；2026-08-16 HO snapshot 性能回归与
spec-anchor 机器验证。

## 1. 八类 hit 与生产判据

八类唯一生产点都在同一个执行函数里，顺序固定；夜巡模式短路只产 scheduled 一类。
severity 除 ssot 漂移按发现映射外全部硬编码；scan 只产 label 不产自治档
（档位由 policy 的规则匹配决定）。

- [x] hit 模型与退出码
  `@anchor symbol ScanHit`
  `@anchor symbol ScanReport`
  `@anchor const EXIT_CLEAN = 0`
  `@anchor const EXIT_HITS = 1`
  `@anchor const EXIT_ERROR = 2`
  `@anchor test agent-harness/tests/test_scan.py :: test_rejects_unknown_hit_type`
  `@anchor test agent-harness/tests/test_scan.py :: test_rejects_extra_fields`
- [x] pending handoff 分类二值：红线正则命中为 red_line_flag，否则
  format_compliant；红线赢过一切
  `@anchor symbol _classify_handoff`
  `@anchor symbol _RED_LINE_PATTERNS`
  `@anchor test agent-harness/tests/test_scan.py :: test_red_line_flag_wins_over_everything`
  `@anchor test agent-harness/tests/test_scan.py :: test_cross_camp_now_format_compliant`
- [x] 坏 handoff 独立成类：schema 校验失败仍要浮出（label 固定
  schema_invalid，dispatcher 无规则可配即回落 escalate——坏数据必须见人）
  `@anchor symbol list_malformed_handoffs_for`
  `@anchor test agent-harness/tests/test_scan.py :: test_malformed_handoff_surfaces_as_escalate_hit`
  `@anchor test agent-harness/tests/test_scan.py :: test_malformed_handoff_for_other_agent_is_ignored`
- [x] 违例：verdicts 目录按 agent 分桶，任一 VIOLATED 才计入；自他之分看路径
  成分
  `@anchor symbol list_unresolved_violations_for`
  `@anchor symbol _classify_violation`
  `@anchor test agent-harness/tests/test_scan.py :: test_own_violation`
- [x] 陈旧 wi（opt-in）：四条合取——本人所有 ∧ 非终态 ∧ 超龄 ∧ 无 pending
  handoff；accepted 的 handoff 不豁免
  `@anchor symbol list_stale_work_items_for`
  `@anchor test agent-harness/tests/test_scan.py :: test_excludes_wi_with_pending_handoff`
  `@anchor test agent-harness/tests/test_scan.py :: test_accepted_handoff_does_not_exclude`
  `@anchor test agent-harness/tests/test_scan.py :: test_surfaces_old_awaiting_verification_without_pending_handoff`
- [x] HO 加载性能契约：一次 run_scan 中 pending_handoffs、malformed_handoff、
  stale_work_items 三个收集器共用同一份有效/无效记录 snapshot；snapshot 建立全局
  pending handoff 的 wi_id 索引，stale 判定按集合查找，不再逐 WI 重扫 HO。该契约
  **不是“整轮绝对只读一次”**：启用 ledger_invariant_breach 时，lint 为核账本
  不变量而独立读取 raw ledger，属于另一条审计边界
  `@anchor symbol HandoffLedgerSnapshot`
  `@anchor symbol load_handoff_ledger`
  `@anchor test agent-harness/tests/test_scan.py :: test_run_scan_parses_each_handoff_yaml_once_and_preserves_filters`
- [x] 账本违例（opt-in）：lint 新 key 才算，基线内白名单不报
  `@anchor symbol list_ledger_invariant_breaches`
  `@anchor test agent-harness/tests/test_scan.py :: test_baseline_whitelisted_finding_is_not_a_breach`
  `@anchor test agent-harness/tests/test_scan.py :: test_collector_is_off_without_opt_in`
- [x] 探针宕标记（opt-in）：扫 down 标记文件，**扫描本身有副作用**（seen_count
  自增原子回写）；超过升级阈值转 stuck 且 severity 升 critical；损坏 JSON 仍
  产 hit 不静默
  `@anchor symbol list_probe_down_markers`
  `@anchor symbol ProbeDownConfig`
  `@anchor test agent-harness/tests/test_scan.py :: test_seen_count_persists_across_scans`
  `@anchor test agent-harness/tests/test_scan.py :: test_corrupt_marker_still_surfaces`
  `@anchor test agent-harness/tests/test_scan.py :: test_fresh_marker_dispatches_stuck_marker_escalates`
- [x] 三个 opt-in 解析器全部 fail-open（policy 加载失败静默 opt-out）；直接调
  run_scan 的调用方拿不到 opt-in，须自己传配置
  `@anchor symbol _resolve_stale_wi_age_days`
  `@anchor symbol _resolve_ledger_lint_config`
  `@anchor symbol _resolve_probe_down_config`
- [x] ssot 漂移：quick 模式、只留 critical/major；minor 在主链是死分支
  `@anchor symbol collect_ssot_drift`
  `@anchor symbol _classify_drift`
  `@anchor symbol _severity_from_label`
  `@anchor test agent-harness/tests/test_scan.py :: test_ssot_engine_failure_maps_to_error`

## 2. context_hygiene：派发前卫生检查

总纲 NFR-002：任何 IO/解析/子进程失败一律降级为「不跳过/空摘要」，绝不阻塞
派发循环——降级唯一可观测信号是 stderr 前缀行，不落事件流。

- [x] 终态检查只对 pending_handoffs 类 hit 生效；含反向验证豁免——干完活交回
  验收方时终态 wi 不跳过，否则验证请求会被吃掉
  `@anchor symbol check_wi_terminal`
  `@anchor symbol SkipDecision`
  `@anchor symbol _TERMINAL_STATUSES`
  `@anchor test agent-harness/tests/test_context_hygiene.py :: test_wi_completed_skips`
  `@anchor test agent-harness/tests/test_context_hygiene.py :: test_reverse_verification_ho_completed_wi_not_skipped`
  `@anchor test agent-harness/tests/test_context_hygiene.py :: test_non_reverse_ho_same_owner_still_skips`
  `@anchor test agent-harness/tests/test_context_hygiene.py :: test_missing_handoff_does_not_skip`
- [x] 工作区活跃检查：git log 窗口内有**非 cron 分身**的 commit 才算活跃——
  排除 cron 自产 commit 的过滤器是决定性判据（漏掉会让分身产物卡住下一轮），
  docstring 无记载，唯一防线是测试
  `@anchor symbol check_workspace_active`
  `@anchor test agent-harness/tests/test_context_hygiene.py :: test_cron_autonomous_commits_excluded`
  `@anchor test agent-harness/tests/test_context_hygiene.py :: test_only_cron_autonomous_commits_does_not_skip`
  `@anchor test agent-harness/tests/test_context_hygiene.py :: test_git_timeout_does_not_skip`
- [x] 提交摘要（软约束）：无 merge 但**不排除** cron commit——与活跃判定的
  git 参数不对称，属有意差异（摘要给分身看上下文，判定防误跳）
  `@anchor symbol recent_commits_summary`
  `@anchor test agent-harness/tests/test_context_hygiene.py :: test_no_merge_commits_included`

## 3. 扫描事件与报告落盘

- [x] canonical `.harness` 是扫描输入，不是 scan runtime 写入面。scan report、
  scan_event / scan_skipped 与 scan lock 统一落 mutable heartbeat runtime root；
  managed PULSE 安装解析为 `data/agent-harness/runtime/heartbeat/`，其他安装面走
  account-scoped Harness runtime state。Git-backed Harness root 跑 scan 后必须保持 clean
  `@anchor symbol heartbeat_runtime_root`
  `@anchor test agent-harness/tests/test_scan.py :: test_managed_pulse_uses_runtime_heartbeat_root`
  `@anchor test agent-harness/tests/test_scan.py :: test_git_backed_harness_scan_keeps_canonical_checkout_clean`
- [x] 扫描事件两型一类（scan_event / scan_skipped 共用 ScanEvent），锁被占即
  记 skipped 事件返回而非报错；隐私边界有机器锚点（事件不泄原文）
  `@anchor test agent-harness/tests/test_scan.py :: test_emits_scan_event`
  `@anchor test agent-harness/tests/test_scan.py :: test_no_raw_text_leaks`
  `@anchor test agent-harness/tests/test_scan_locks_integration.py :: test_skip_emits_scan_skipped_event`
- [x] report 在 heartbeat runtime 的 scan-reports 目录按 scan_id 命名；manual
  `--no-persist` 用于只读诊断，既不产 report/event，也不取 scan lock
  `@anchor symbol _persist_scan_report`
  `@anchor test agent-harness/tests/test_scan.py :: test_no_persist_manual_scan_writes_no_runtime_artifacts`

## 4. 已知两义与债（登记待裁）

`@anchor manual hit-type 白名单双份维护 :: ScanHit 与 policy 的 ScanItemType 是两份独立字面量清单,无共享常量无交叉校验,任一侧增删另一侧不报错;改一处必须双改`
`@anchor manual event-id 双格式并存 :: 通用 id 生成器与扫描事件 id 生成器都在填事件 id 字段,格式互不兼容且模型无格式校验,按 id 解析关联的下游会在其一失败`
`@anchor manual 死常量与死判据同居 :: 阵营映射表及其注释描述的跨阵营判据已整体退役但仍留在文件里,分类器 docstring 承认移除而常量未删`
`@anchor manual 报告直写非原子 :: 扫描报告用直写落盘,与台账层 tmp+rename 原子模型不一致,进程被杀可能留半文件`

## 5. 覆盖声明

本 spec 给八类 hit 生产判据、opt-in 机制、卫生检查降级总纲、反向验证豁免、
cron commit 排除过滤器、扫描事件隐私边界首次挂机器锚点。
dispatcher-context-hygiene 存量目录的现行契约部分由本文接管。
