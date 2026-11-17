# SPEC：harness 持久层与模型 — store / models / events

> version: `0.2.1`
> status: `reverse_engineered_from_code`
> authority: 台账持久化与数据模型的**现行实现契约**（由代码反推）
> owner: Pulse（wi_20260804_070 全面 spec 化）
> modified: `2026-08-16`
> 版本史：只看 git log

**取证基础**：2026-08-09 代码普查（file:line 逐条核）；2026-08-16 HO snapshot
性能回归与 spec-anchor 机器验证。

## 0. 最重要的结构事实

持久层是**两套并存、互不相通**的实现：store 模块（YAML 台账：WorkItem / Handoff /
SignalEvent）与 events 模块（JSONL 事件流：EventBase 家族）。二者各有一个同名的
追加函数、各自解析 root、且把文件写进**同一个 events 目录**（命名后缀不同纯属巧合）。
Escalation 的数据模型不在 models 模块而在 events 模块。本 spec 对两套分别立约，
「事件」一词在每节内单独消歧。

## 1. store：台账读写 API

- [x] 核心类与 root 解析：显式 root → 环境变量 HARNESS_ROOT → 自 cwd 向上探（上界
  32 层）找已存在的 .harness/
  `@anchor symbol HarnessStore`
  `@anchor symbol _discover_root`
  `@anchor test agent-harness/tests/test_store.py :: test_walks_up_to_existing_harness_root`
- [x] **另有四套模块级 root 解析与上面这套不同**：dispatcher / policy / scan 的
  `_resolve_harness_root`、locks 的 `_default_harness_root`、events 的
  `_harness_root`，优先级均为「显式参数 → `$HARNESS_ROOT` → `cwd/.harness`」
  （**不向上探**，与 `HarnessStore._discover_root` 的语义差别在这里）。
  四套目前行为等价，但仍是四份实现——本轮只加等价性测试锁住，不做归一
  `@anchor symbol _resolve_harness_root`
  `@anchor symbol _default_harness_root`
  `@anchor test agent-harness/tests/test_harness_root_equivalence.py :: test_all_agree_on_cwd_fallback`
  `@anchor test agent-harness/tests/test_harness_root_equivalence.py :: test_all_agree_when_env_set`
  （债登记，2026-08-10 / wi_20260810_949：归一重构要动五个模块的调用面，风险与本批次
  不匹配，故只把「等价」变成机器可查——任一实现单独漂移即变红。已知残余差异：
  dispatcher 版对显式 str 入参不包 `Path()`，见测试 `TestKnownDivergence`）
- [x] ID 生成：仅 wi/ho 前缀做碰撞重试（默认 50 次，耗尽抛错），其余前缀直落通用生成
  `@anchor symbol generate_unique_id`
  `@anchor test agent-harness/tests/test_store.py :: test_never_collides_with_existing_handoff_file`
  `@anchor test agent-harness/tests/test_store.py :: test_exhausted_attempts_raises`
  `@anchor test agent-harness/tests/test_store.py :: test_unknown_prefix_falls_back_to_plain_generate_id`
- [x] WorkItem/Handoff 读写六件套 + 宽容列表（坏 YAML 静默跳过，另有 invalid 清单口径）
  `@anchor symbol save_work_item`
  `@anchor symbol load_work_item`
  `@anchor symbol load_work_item_mapping`
  `@anchor symbol list_work_items`
  `@anchor symbol list_invalid_work_items`
  `@anchor symbol save_handoff`
  `@anchor symbol load_handoff`
  `@anchor symbol list_handoffs`
  `@anchor symbol list_all_handoffs`
  `@anchor symbol list_invalid_handoffs`
  `@anchor symbol NotFoundError`
  `@anchor test agent-harness/tests/test_store.py :: test_list_skips_yaml_scanner_error`
  `@anchor test agent-harness/tests/test_store.py :: test_list_invalid_reports_yaml_scanner_error`
- [x] HO 单次快照读 API：load_handoff_ledger 每次调用都从文件系统新读，不是跨调用
  持久缓存；HandoffLedgerEntry 同时保留有效合同与 invalid 投影，HandoffLedgerSnapshot
  建立全 ledger 的 pending work_item_id 集合索引，供一次上层操作复用
  `@anchor symbol HandoffLedgerEntry`
  `@anchor symbol HandoffLedgerSnapshot`
  `@anchor symbol load_handoff_ledger`
  `@anchor test agent-harness/tests/test_scan.py :: test_run_scan_parses_each_handoff_yaml_once_and_preserves_filters`

### 1.1 原子性契约（两套写路径，模型不同）

- [x] **台账文件写 = tmp+rename**：同目录临时文件 → 写入 → fsync → 原子替换；失败时
  删临时文件并重抛，旧文件不被破坏
  `@anchor symbol _atomic_write_text`
  `@anchor test agent-harness/tests/test_store.py :: test_failed_replace_preserves_existing_work_item`
  `@anchor test agent-harness/tests/test_store.py :: test_failed_replace_preserves_existing_handoff`
- [x] **事件追加 = O_APPEND + fsync**（append-only 日志，非 tmp+rename——有意设计，
  与台账写模型不同）；分桶键 = 事件的 work_item_id，空则落孤儿桶
  `@anchor const ORPHAN_EVENTS_FILE = "_orphan.events.yaml"`
  `@anchor test agent-harness/tests/test_store.py :: test_events_file_uses_yaml_multi_doc`
  `@anchor test agent-harness/tests/test_store.py :: test_append_event_with_null_wi_lands_in_orphan_bucket`
  `@anchor test agent-harness/tests/test_store.py :: test_load_events_by_wi_does_not_pick_up_orphan_events`
- [x] 事件写入有隐式副作用：环境变量 HARNESS_SCAN_ID 非空时注入 payload 的
  harness_scan_id 键（落盘可能比调用方传入多一字段）
  `@anchor test agent-harness/tests/test_store.py :: test_autonomous_event_is_correlated_to_dispatch_scan`
- [x] 序列化：枚举转字符串、datetime 转 ISO8601，时区经往返保持
  `@anchor symbol _serialize`
  `@anchor test agent-harness/tests/test_store.py :: test_datetime_serialized_as_iso8601`
  `@anchor test agent-harness/tests/test_store.py :: test_roundtrip_preserves_datetime_timezone`

### 1.2 所有权修正链（root-only 硬化）

- [x] 非 root 直接返回零副作用；root 下按 SUDO_UID/GID 或父目录归属修正，失败只告警
  **永不作为写入前置条件**
  `@anchor symbol _fixup_ownership`
  `@anchor symbol _resolve_target_owner`
  `@anchor const _LEDGER_FILE_MODE = 0o644`
  `@anchor test agent-harness/tests/test_store.py :: test_chmod_normalises_mode`
  `@anchor test agent-harness/tests/test_store.py :: test_chown_failure_is_swallowed`

### 1.3 文件布局

| 路径 | 内容 |
|---|---|
| work-items/{id}.yaml | 单文档 YAML |
| handoffs/{id}.yaml | 单文档 YAML |
| events/{wi_id}.events.yaml | 多文档 YAML（store 一套） |
| events/_orphan.events.yaml | 无主事件审计桶 |
| events/{type}-YYYY-MM.jsonl | JSONL 事件流（events 一套，见 §3） |

## 2. models：台账数据模型（主链 spec 已覆盖三枚举与四 validator，此处补全）

- [x] WorkItem 补充 validator：id 格式、标题非空
  `@anchor symbol validate_work_item_id`
  `@anchor symbol validate_title_not_empty`
- [x] awaiting_verification 前置：四个 outcome 字段全非空才放行（缺项列名报错）——
  **测试缺口：models 测试文件里无该 validator 的直接测试**，仅靠上层间接覆盖，
  补齐前不得删改该 validator
- [x] HandoffContract 收发一致性五段语义：to_owner/to_owners 至少其一、双给时首位
  一致、扇出条目非空去重且不含发起方、**双向投影**（单收补列表、列表补单收）、
  reject/withdraw 理由与状态双向绑定
  `@anchor symbol validate_handoff_consistency`
  `@anchor symbol validate_not_empty`
- [x] responsibility_transfer 字段默认是 None 而**不是** not_transferred——语义上
  None 交由 machine 层按 full 对待（见主链 spec §4），模型层不补默认
- [x] SignalEvent（store 一套的事件模型）：payload 默认空字典，id 须匹配 ev_ 格式
  `@anchor symbol SignalEvent`
  `@anchor symbol validate_event_id`
  `@anchor test agent-harness/tests/test_models.py :: test_backward_compat_legacy_yaml_without_fields`
- [x] ID 体系四正则（wi/ho/ev + 通用）
  `@anchor symbol _WORK_ITEM_ID_RE`
  `@anchor symbol _HANDOFF_ID_RE`
  `@anchor symbol _EVENT_ID_RE`
  `@anchor const _ID_DATE_FMT = "%Y%m%d"`

## 3. events：JSONL 事件流模型（另一套）

- [x] 基类禁未知字段、时间戳强制带时区；schema_version 固定 1.0
  `@anchor symbol EventBase`
  `@anchor symbol _ensure_tz_aware`
  `@anchor test agent-harness/tests/test_events.py :: test_occurred_at_must_be_tz_aware`
- [x] 五个事件类型（模块 docstring 只列了四个，以代码与测试为准）
  `@anchor symbol ScanEvent`
  `@anchor symbol DispatchEvent`
  `@anchor symbol AutonomousExecutionEvent`
  `@anchor symbol EscalationEvent`
  `@anchor symbol PolicyChangeEvent`
  `@anchor test agent-harness/tests/test_events.py :: test_all_five_event_types_write_correctly`
- [x] EscalationEvent 契约要点：severity 三档（critical/major/minor）、聚合策略两值
  （push_immediately/aggregate_daily）、push_status 三值默认 pending、演练标志
  is_drill 默认 False、显式处置三字段默认空
  `@anchor test agent-harness/tests/test_events.py :: test_uncounted_tool_calls_are_none_not_zero`
- [x] 并发追加不丢失（线程 100 并发 + 多进程双验）
  `@anchor test agent-harness/tests/test_events.py :: test_threaded_append_100_no_loss`
  `@anchor test agent-harness/tests/test_events.py :: test_multi_process_append_no_loss`

## 4. 已知两义与债（登记待裁，不判定）

`@anchor manual 追加函数同名双实现 :: store 的方法与 events 的模块级函数同名，写同一 events 目录；scan 模块裸名 import，存在静默覆盖风险；归一或改名前保持警惕`
`@anchor manual 两套事件 schema 并存 :: SignalEvent(操作者+无时区要求) 与 EventBase(agent_id+强制时区) 完全不同，且 events 一套生成的 id 不满足 models 的 ev 正则；spec 层已按节消歧，代码层未归一`
`@anchor manual convert-datetimes 死代码 :: store 尾部的递归转换工具生产路径已不用，仅测试自引用；有测试不等于是活的，待删`
`@anchor manual invalid 语义超载 :: schema 合法但无 wi 关联的独立 handoff 在 invalid 清单算无效、在 list_all_handoffs 算正常；同一记录两 API 两判，是有意还是缺陷待裁`
`@anchor manual 事件文件权限口径不一 :: 台账写后归一 0644；store 事件追加按 0666 受 umask 削减且仅 root 下归一；events 的 JSONL 完全无归属修正；非 root 严格 umask 下与 canonical 模式不符`

## 5. 覆盖声明

本 spec 给 store 全部读写 API、原子性契约、所有权修正链、models 其余 validator、
events 五事件类型首次挂机器锚点。store-and-cli 与 agent-registry-extended 两个
存量目录的现行契约部分由本文接管（分类台账见 features/CLASSIFICATION.md）。
