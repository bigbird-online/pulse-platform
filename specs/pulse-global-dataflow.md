# SPEC：PULSE 全局 — 跨模块数据流守恒（Relay ↔ .harness ↔ Stats）

> version: `0.2.0`
> status: `reverse_engineered_from_code`
> authority: 三模块 spec（relay-*.md / harness-*.md / stats-*.md）都管不到的**跨模块缝**的现行契约
> owner: Pulse
> modified: `2026-10-18`
> 版本史：只看 git log

**分层声明**：模块内部契约归各模块 spec 持有，本文只钉「一端写、另一端读」的守恒对。
**跨仓诚实边界**：agent-harness（Coevolution 主仓）侧的代码本仓 scan 够不着，其端点一律
`manual` 锚点 + 观察步骤 + 指针到 `agent-harness/specs/` 对应条目，不假装可机检。

## 1. 已退役：handoff 接活、harness CLI 调用与升级通知

2026-10-18 单轨化后，Relay 不再监听 `.harness/handoffs/`，也不再调用旧 harness 的
scan / dispatch / escalation emit；WorkItem 的接活、派发与通知只经原生核心，契约见
`agent-harness/project-specs/`。cron 中仍声明 `harness_dispatch` 执行或
`harness_escalation` 投递的任务，加载时跳过并告警，不执行。

## 2. Relay 内部推送接收端

- [x] Relay 接收端 `/internal/push` 契约：必填 `channel`+`agentId`，channel 枚举
  telegram/feishu/both，token 非空时精确匹配、**env 为空则放行**
  `@anchor symbol checkInternalToken`
  `@anchor test Relay/tests/relay-http-push.test.ts :: "400 missing_fields when channel absent"`

## 3. Relay（写侧）↔ Stats exporter（读侧）

- [x] **运行观测的双端文件契约只有两条**：obs 事件流目录与 heartbeats.json 路径；
  其余 Relay 产物（state.json / metering JSONL / restart-audit.log / cron 状态文件）
  exporter 一概不读——不是契约不一致，是契约不存在
  `@anchor symbol HEARTBEAT_FILE`
  `@anchor symbol HEARTBEAT_PATH`
  `@anchor manual exporter 不读四类 Relay 产物 :: 在 Stats/exporter/ 跑 grep -rn "state\.json\|metering\|restart-audit\|cron-jobs" pulse_stats/ --include='*.py'，除注释/探针路径外应零消费点`
- [x] 配置面由 manager 分两轴收敛：Relay 继续从 `data/runtime-priority.json`
  读取运行时顺序与平台缺省；Stats quota probe 只从 active
  `data/runtime-cost-route.json` 取得 profile alias，再由 schema-v2
  `compute-profiles.json` 解析 model/effort。Stats 不读 `codex.yolo`，probe argv 仍显式传
  `read-only` / `never` 覆盖用户全局配置；active route/catalog/cache 缺失或坏契约时禁用
  probe，不回退平台缺省
  `@anchor symbol COST_ROUTE_PATH`
  `@anchor symbol COMPUTE_PROFILES_PATH`
  `@anchor test Stats/exporter/tests/test_live_contract_cost_route.py :: test_stats_route_failures_never_fall_back_to_platform_default`
- [x] obs 路径守恒是**半边契约**：写侧 `homedir()/.relay/obs` 且可被 env
  `RELAY_OBS_DIR` 覆盖；读侧硬编码 `/home/pulse/.relay/obs` **不读该 env**——
  写侧一旦设 env，读侧静默读空目录
  `@anchor symbol RELAY_OBS_DIR`
  `@anchor symbol OBS_DIR`
- [x] obs 行 schema 读写对齐：读侧只认三种事件，`turn.end` 取
  outputTokens（强制 int 且排 bool）/agent/session/isError/durationMs，
  `delivery.receipt` 判 `status == "sent"` 读 attempts；写侧可选字段
  undefined 时**整键省略**；行无版本字段，格式变更对读侧是静默丢行
  `@anchor symbol REQUEST_TRACE_EVENTS`
  `@anchor test Stats/exporter/tests/test_usage_sources.py :: test_input_and_output_tokens_stay_separate`
- [x] 分桶时区错位：写侧文件名按 **UTC 日**分桶；读侧日度聚合显式取本地日横跨的
  两个 UTC 文件再逐行回筛，**月度聚合无同等补偿**——月边界跨 UTC 日的行随文件名
  整体归错月
  `@anchor symbol DISPLAY_TZ`
  `@anchor manual 月度无跨日补偿 :: 对比 usage_sources.py 日度路径(:948-1020 取两 UTC 日+逐行 astimezone 回筛)与月度路径(:855-859 直接 glob 月前缀)，月度缺回筛步骤`
- [x] CC quota token 读取契约：先 `/home/pulse/.relay/relay.env` 的
  `CLAUDE_CODE_OAUTH_TOKEN=`（裸值或单双引号皆可），失败退 `~/.claude/.credentials.json`；
  该文件由部署维护，Relay 源码不生成
  `@anchor symbol CC_RELAY_ENV_PATH`
  `@anchor symbol read_relay_env_token`
- [x] `billing_component_id` 是读侧要求、写侧从不产出的字段——归因判据永远停在
  「证据不足」（读侧注释自认）
  `@anchor manual 写侧零产出 :: 本仓跑 grep -rn billing_component_id Relay/src/ 应零命中；读侧要求见 usage_sources.py:816-822 注释`
- [x] 路径根系统性不对称：写侧一律 `$HOME` 派生，读侧一律硬编码 `/home/pulse/`，
  仅 exporter 跑在 pulse 用户下才等价；且读侧同一契约文件混写两台机器的根
  （mac 的 `/Users/owner/...` 与 vultr 的 `/home/pulse/...`）
  `@anchor symbol PULSE_REPO`
  `@anchor symbol HARNESS_ROOT`
- [x] Harness global 指标不再读取 `/Users/owner/Coevolution/.harness` human root；
  `HARNESS_ROOT` 固定派生自 Vultr 登记的 clean service checkout。采集层同时记录该
  checkout 的完整 Git SHA，global authority carrier 必须绑定同一
  `repository@sha` digest；fresh carrier 与实际指标源 SHA 不同仍 fail closed。
  `alerts_dir` 继续读取 human-root runtime events，是本机操作事件证据，不进入
  Harness global ledger authority。
  `@anchor symbol HARNESS_SERVICE_REPO`
  `@anchor symbol authority_revision_for_git`
  `@anchor test Stats/exporter/tests/test_external_input_contract.py :: test_global_wi_metrics_read_the_registered_service_checkout`

## 4. Stats（读侧）↔ harness models（写侧）：跨仓枚举守恒对

- [x] 读侧全部是**硬编码元组，无一处 import harness 的 models.py**；写侧七态
  （draft/ready/in_progress/awaiting_verification/blocked/completed/failed）与
  ack 六态的真源在 agent-harness，读侧维护三张子集表并**刻意**分裂：
  open 表 3/7（动它波及停滞列表+闭环判据两处）、in-flight 表 5/7、
  handoff 终态表刻意排除 `accepted`（已接≠已办完）
  `@anchor symbol WI_OPEN_STATUSES`
  `@anchor symbol WI_IN_FLIGHT_STATUSES`
  `@anchor symbol HANDOFF_TERMINAL_STATES`
  `@anchor symbol HANDOFF_ACTION_STATES`
  `@anchor manual 写侧枚举真源 :: agent-harness/harness/models.py:26-49 七态+六 ack 态；对端契约归 agent-harness/specs/harness-store-models.md。2026-08-10(wi_20260810_631) 起两端一致性**已有机器闸**：Stats/exporter/tests/test_cross_repo_enum_contract.py 子进程向 harness venv 取权威枚举，判读侧无幻影值 + 写侧每值必被显式分类或登记豁免（completed/failed 已登记为终态豁免）`
  `@anchor test Stats/exporter/tests/test_cross_repo_enum_contract.py :: test_every_upstream_value_is_classified`
- [x] 读侧自造哨兵值 `no_handoff`（不在写侧枚举内），main_only 闭环判据**刻意绕开**
  `ack_state`（分身能改的字段不做闸判据），且 main_only 有 handoff 与 wi 两处载体
  `@anchor symbol _bare_main_only_rows`
  `@anchor test Stats/exporter/tests/test_usage_sources.py :: test_bare_main_only_wi_without_any_handoff_is_still_visible`
- [x] exporter 眼中 schema-valid handoff = 5 字段：id / from_owner / to_owner /
  ack_state / asks
  `@anchor manual 五字段清单 :: usage_sources.py:1450 一带的 for field in (...) 元组即真源；grep -n '"id", "from_owner", "to_owner", "ack_state", "asks"' Stats/exporter/pulse_stats/usage_sources.py 应恰 1 处`
- [x] 阈值跨仓对齐靠注释不靠代码：读侧 `IN_FLIGHT_SWEEP_DAYS=3` 自述「对齐 harness
  侧 STALE_WI_DEFAULT_AGE_DAYS=3」，并刻意不复用 `WI_STALLED_DAYS=7`
  `@anchor const IN_FLIGHT_SWEEP_DAYS = 3`
  `@anchor const WI_STALLED_DAYS = 7`

## 5. 版本契约的缺席（登记为债）

- [x] snapshot 输出侧有版本（`pulse.monitoring.snapshot.v2`，producer↔App 契约归
  stats-snapshot-contract.md 持有，本文不重复锚）；但 exporter 对**一切外部输入零
  版本判定**：写侧 state.json `version:1`、cron 配置 `version:2` 读侧全不读，
  obs 行无版本字段。唯一输入版本门是 exporter 自写自读的 incident state
  `@anchor symbol STATE_SCHEMA_VERSION`
  `@anchor manual 输入零版本判定 :: 在 exporter/pulse_stats/ 跑 grep -rn "schema_version\|version" 排除 producer.py/state.py 后，对 Relay 产物无任何版本比较（Relay 产物本就不在消费面内，见 §3「exporter 不读四类 Relay 产物」）。2026-08-10(wi_20260810_631) 起**已消费**的外部输入有版本闸：tests/test_external_input_contract.py 要求凡 live 文件带版本字段者读侧必有显式判定，否则登记豁免`
  `@anchor test Stats/exporter/tests/test_external_input_contract.py :: test_versioned_inputs_are_gated_in_code`
- [x] exporter 三个版本号三处独立维护：`BASELINE_VERSION="0.2.2"`（实传）、
  producer 形参默认 `"0.2.1"`（死默认）、producer name/version `"0.1.0"`（硬编码）
  `@anchor const BASELINE_VERSION = "0.2.2"`
  `@anchor symbol baseline_version`

## 已知两义与债（本文登记，处置另立 wi）

1. ~~`dispatchTimeoutSeconds` 注释 600 vs 实现 3600（原 §2，已退役）。~~
   **已还（2026-08-17）**：`src/types.ts` 注释改为 3600，与 scheduler 实现一致。
2. 2026-08-17 生产 `pulse-vultr-dispatcher-20260608` 的 dispatch 外层预算曾为 1530 秒，
   而 Pulse policy 的单 hit 上限 570 秒对应仅三次 subprocess 理论上界即
   `3 × (570 + 30) = 1800` 秒，尚未计 post-session gate。16:00 UTC 自然调度已在
   scan 19.266 秒成功后于外层 1530 秒超时，证明两端预算漂移。
   **配置已还、自然验收未通过（失败点已迁移）**：reload 前 37/37 job 定义 hash 与 scheduler state
   相等；仅删除本 job 的 1530 特例，回到 `agent.timeout=3600`，reload 后 definition
   hash 更新为 `c3741cee…` 且 Relay PID 未变。17:00 UTC 自然轮次以候选五文件
   identity 完成 scan（37.031 秒、21 hits），未触发 outer timeout，但在首个 autonomous
   started 后 82ms 以内以 CLI code 1 退出；job `lastStatus=error`、
   `consecutiveErrors=7`。因此 scan 性能与外层预算漂移已修，TG 整体故障仍未 closed；
   下一阻塞点是 dispatcher 内层异常，Relay 仅保留截断 traceback，不能伪造根因。
3. ~~obs 读侧不读 `RELAY_OBS_DIR`（§3）——半边契约，写侧设 env 即静默断供。~~
   **已还（wi_20260810_631）**：读侧仍**刻意**不读该 env（Stats 与 Relay 不同进程、
   env 不共享，读同名 env 只会得到自己那份，制造「以为对齐了」的假象），但代价
   已写进 `live_contract.OBS_DIR` 上方注释，并由
   `tests/test_external_input_contract.py::ObsDirSymmetryTests` 钉住两条：
   写侧默认路径 == 读侧常量、写侧 env 名必须在读侧被点到。
   **残余**：env 真被设上时闸看不见（静态比对看不到运行时 env）。
4. ~~跨仓枚举守恒对无机器闸（§4）——harness 改枚举 Stats 不会跟。~~
   **已还（wi_20260810_631）**：建 `tests/test_cross_repo_enum_contract.py`，子进程向
   harness venv 取权威枚举后判两条——读侧无幻影值、**写侧每个值必须被显式分类
   或登记豁免**。harness 新增状态而 Stats 没表态即红。取不到 venv 时 skip（出声，
   非假绿）；取到而关系不成立则红。
5. 月度聚合无跨 UTC 日补偿（§3）——月边界行归错月，影响月度用量呈现精度。
6. ~~exporter 对外部输入零版本判定（§5）——写侧格式变更全部静默。~~
   **部分已还（wi_20260810_631）**：`tests/test_external_input_contract.py::
   VersionGateTests` 守「凡 live 输入文件带版本字段，读侧必须有显式判定，否则
   登记豁免」，防的是**新接外部源时忘了判版本**。
   **残余**：Relay 的 state.json(`version:1`)/cron 配置(`version:2`) 当前**不在
   exporter 消费面内**（§3 已锚「exporter 不读四类 Relay 产物」），故无处可判；
   哪天真去读它们，本闸会因该文件带版本字段而要求补判。

## 机检口径

本文锚点由 `Scripts/hooks/global-spec-drift-guard.sh` 把守（globs 见该脚本）；
跨仓 manual 条目的观察步骤在 agent-harness 仓执行，不计入本仓机检分母。
