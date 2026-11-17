# SPEC：PULSE 运行时优先级管理

> version: `1.5.5`
> status: `implemented`
> authority: PULSE 跨 Relay、agent-harness、Stats 与 Codex 用户配置的运行时迁移契约
> owner: Pulse
> modified: `2026-11-10`
> implementation: `ComputeCost/bin/pulse-runtime-priority.py`

## 1. 单一管理契约

`data/runtime-priority.json` 是运行时优先级的共享 profile，schema_version 固定为 1：

```json
{
  "schema_version": 1,
  "runtime_priority": ["codex"],
  "codex": {
    "model": "gpt-5.6-sol",
    "reasoning_effort": "xhigh",
    "yolo": true
  },
  "claude": { "model": "deepseek/deepseek-v4-flash-0731" }
}
```

- `runtime_priority` 必须是由 `codex` / `cc` 组成的非空、无重复有序数组。
- `schema_version` 必须是整数 `1`；默认 `claude.model` 为 `deepseek/deepseek-v4-flash-0731`。新的
  plan/apply 若 desired `claude.model` 含 `fable` 必须 fail closed；历史 profile 若仍是 Fable，只能通过
  显式 `--claude-model` 迁移到批准的非 Fable 模型，禁止无意保留或重新写入。
- `codex.model`、`codex.reasoning_effort` 与
  `claude.model` 必须是非空字符串，`codex.yolo` 必须是布尔值。
- profile 不含时间戳，确保相同意图重复 apply 为零 diff。
- Relay 只有在完整 v1 profile 通过 schema 与字段语义校验后才消费；缺失、JSON 损坏或任一
  必填字段非法时保留 v1 强安全缺省。Stats quota probe 自 phase 2 起不再消费 v1 profile：
  active cost route / compute profile / Codex cache 任一不可验证时不生成 argv、不启动 probe，
  旧额度读数仍按 12 小时规则走向 unknown。管理命令损坏任一受管 schema 时拒绝 apply/verify。
- `codex.yolo` 只管理执行权限，不传给 Stats 探针。

`@anchor symbol resolvePulseRuntimePriority`
`@anchor symbol resolvePulseCodexModel`
`@anchor test Relay/tests/codex-defaults.test.ts :: reads runtime order, model, and reasoning from the shared profile`
`@anchor test Stats/exporter/tests/test_usage_sources.py :: CodexProbeTests.test_codex_probe_none_is_a_direct_noop`

## 2. 受管面与排除面

| 受管面 | 写入/消费规则 |
|---|---|
| Relay conversations | 全部现有 `users[].agents[]` 物化 runtime 顺序、活动模型与 `codexYoloDefault` |
| Relay persisted sessions | `verify` 只读检查 `/home/pulse/.relay/state.json`；既有 session runtime 与 profile 首项不一致时失败，但 `plan/apply` 不改写 session |
| Relay legacy config alias | `/home/pulse/.relay/relay.json` 必须是指向生产真源 `data/relay.json` 的符号链接；plan/apply/verify/backup/rollback 管理链接本身 |
| Relay direct jobs | `execution.mode != harness_dispatch` 的 jobs 只物化 runtime、`computeProfile` 与 `codexYolo`；拒绝 job 级模型/effort 字面量。管理器把共享 profile 的 Sol/Terra/Luna family 投影成中央 latest alias，不写具体版本 |
| harness policies | 全部顶层 `policies/*.yaml` 物化 runtime、fallback、`compute_profile=platform_default` 与 `codex_yolo`；删除三项 legacy 模型/effort 字面量，保留扫描项自己的 profile |
| Relay code defaults | 未显式配置时从共享 profile 读 runtime 顺序与 Codex 模型 |
| Stats probe | 从 active cost route 取 profile alias，经 schema-v2 catalog + 当前用户 Codex cache 解析 model/effort；失败禁用 probe；显式 `read-only` / `never` 覆盖用户全局 YOLO，且顶层 `never` 选项必须位于 `exec` 前 |
| Codex 用户配置 | 同步顶层 model/effort 与 YOLO；主动删除 `[agents]` 下两个不受支持的 scalar default，避免 Codex 在读配置时整体失败 |

管理器完整持有中央 named profiles：`frontier=Opus/Astra high`、`media=Sonnet/Terra medium`、
`economy=Haiku/Luna low`、`automation=Sonnet/Sol high`（自动任务缺省：Codex 优先，CC 备用）；四档只写 latest alias，plan/apply/verify 不接受目录值自行漂移。

`harness_dispatch` jobs 是硬排除面：Relay 只启动 `harness scan/dispatch`，真正 LLM
由对应 harness policy 决定。管理器不得向这 19 类 job 注入普通 batch runtime 字段。

### Backend-only Media UAT overlay

`backend-media-uat-plan|apply|verify|rollback [backup-id]` 是与常规 runtime
迁移分离的、显式的可逆控制面。它只把所有 Relay direct cron 的
`agent.computeProfile`，以及全部 harness policy 顶层和已有
`pending_handoffs` / `stale_work_items` 的 `compute_profile` 渲染为 `media`。
它不改 `data/runtime-priority.json`、Relay 互动会话、Stats 路由、timer，亦不向
`harness_dispatch` job 写 batch runtime 字段。apply 自动留下精确文件快照；rollback
只接受该 overlay 快照并逐字节恢复。显式 id 与默认 latest 均须在写入前一次性校验目录
provenance、manifest 的 `snapshot_type` / `backup_id`、全部 entry 为 existing file、备份路径
不逃逸且 checksum 正确，并要求 target set 精确等于当前 cron 与全部 policy；任一项失败即零
受管文件写入。

`verify` 除 profile 名还必须读取中央 catalog，确认 `media.codex` 与 `media.cc` 均有
非空 `model_alias` / `reasoning_effort`，并输出两项 alias/effort；这里不解析或固化
具体版本模型，真实解析仍由 harness canonical resolver 的 UAT 取证。

`apply --reload-relay` 与 `rollback --reload-relay` 是生产的一条命令激活/恢复路径：先从
可由 `PULSE_RUNTIME_RELAY_PID_PATH` 覆盖的 PID file 做存活 preflight，PID 缺失、非法或
不存活时零写入；写入后仅发送 SIGHUP 并输出 PID/信号证据。SIGHUP 后的加载与真实入口
验真不由此命令伪称完成。
即使 overlay 已无磁盘差异，`apply --reload-relay` 仍会 preflight 后重发一次 SIGHUP，
以修复此前未 reload 或未加载的状态；`plan` 永不 preflight 或发送信号。

常规 manager 仍强制两类 harness hit 为 `frontier`，所以 UAT 结束后再次普通 apply
会恢复其风险地板；不得把此 overlay 用于 `critical_ambiguous` 风险降级。

Direct cron 与 dispatcher 启动的子进程均传 `PULSE_PARENT_COMPUTE_PROFILE`。Duke
嵌套裁决的优先级是 `ADJUDICATE_*` 显式值 > 父 profile > runtime-v1；父 profile
通过 harness 的 canonical resolver 解析，解析失败即拒绝启动，禁止回退至 Sol/xhigh。
Codex 与 CC 都必须把 resolver 返回的 model 和 reasoning_effort 写入最终 argv；CC 在父
profile 或 `ADJUDICATE_CLAUDE_REASONING_EFFORT` 非空时追加 `--effort`，runtime-v1 没有
Claude effort 时维持原 argv。每个 runtime fallback 均须独立正例断言这两个字段。

`@anchor symbol _backend_media_uat_plan_or_apply`
`@anchor symbol _transform_backend_media_uat_policy`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_backend_media_uat_is_narrow_and_rolls_back_exactly`
`@anchor manual nested-duke-parent-profile :: 核 agent-harness/harness/dispatcher.py 的 PULSE_PARENT_COMPUTE_PROFILE、duke-space/scripts/adjudicate-spawn.sh 的 PARENT_COMPUTE_PROFILE 与 canonical harness compute-profile resolve；解析失败必须非零退出`

`state.json` 是只读观测面，不是 apply 写入面。原因是活进程内存会继续回写 state，且
CC `sessionId` 与 Codex `codexThreadId` 不可互换。管理器不得通过改字段伪造迁移；需要
迁移某个现有会话时，由用户对该会话显式执行 `/runtime codex`，让 Relay 终止旧进程、
清理不兼容 session state，再由下一条消息创建 Codex thread。历史 transcript 文件保留。

当前 Codex CLI 版本没有 `--yolo` 参数。Relay `/yolo` 是会话控制命令，on 时下发
`--dangerously-bypass-approvals-and-sandbox`；harness 与 direct cron 同样下发该长参数。
CC 自治链的对等权限是 `--dangerously-skip-permissions` 或
`--permission-mode bypassPermissions`。

`@anchor symbol _is_harness_dispatch`
`@anchor manual harness-codex-yolo-command :: 跨仓到 /Users/owner/Coevolution/Bigbird/Lab/BigbirdEnvironment/agent-harness：grep dispatcher.py 的 _build_codex_command，并跑 tests/test_dispatcher.py::test_codex_runtime_uses_codex_exec_yolo_by_default 与 ::test_codex_runtime_can_explicitly_restore_workspace_sandbox；本仓扫描器不可把跨仓符号伪报为可机检`

## 3. 管理命令

入口：

```bash
python3 ComputeCost/bin/pulse-runtime-priority.py plan \
  --priority codex --codex-model gpt-5.6-sol \
  --codex-reasoning-effort xhigh --codex-yolo on
python3 ComputeCost/bin/pulse-runtime-priority.py apply <同参数>
python3 ComputeCost/bin/pulse-runtime-priority.py verify
python3 ComputeCost/bin/pulse-runtime-priority.py backups
python3 ComputeCost/bin/pulse-runtime-priority.py rollback [backup-id]
python3 ComputeCost/bin/pulse-runtime-priority.py backend-media-uat-plan
python3 ComputeCost/bin/pulse-runtime-priority.py backend-media-uat-apply
python3 ComputeCost/bin/pulse-runtime-priority.py backend-media-uat-apply --reload-relay
python3 ComputeCost/bin/pulse-runtime-priority.py backend-media-uat-verify
python3 ComputeCost/bin/pulse-runtime-priority.py backend-media-uat-rollback [backup-id]
python3 ComputeCost/bin/pulse-runtime-priority.py backend-media-uat-rollback [backup-id] --reload-relay
```

成本切换另加 `--preset current_safe|phase2_stats_canary|phase23_canary`。未显式给 preset 时
沿用 active route 的 preset；active route 缺失才取 `current_safe`。`plan/show` 必须逐 route
打印 consumer、workload/risk、base/floor/effective、verifier 与 rollback，不能只报 preset 名。
`plan --preset <name> --json` 只读输出 `runtime_priority_plan.v1`，其中包含 active/target route digest、
current/target runtime-profile digest、route diff、唯一 effective changed routes、catalog/transition map、
precondition 与 canonical `plan1_<sha256>`；该命令不写任何受管文件。

- `plan` 只报受管计数与会变化的路径，不输出含 token/secret 的配置 diff。
- `--codex-model` 必须是可投影到 `codex-sol-latest` / `codex-terra-latest` /
  `codex-luna-latest` 的 family 型号；无法形成 latest alias 时 plan/apply fail closed，避免管理器
  把具体型号重新写进 direct cron 中央 profile。
- `plan/show/verify` 必须输出 persisted session 的 total/managed/matching/mismatched/unmanaged
  计数；`verify` 还须逐条输出不匹配 pool key 与实际 runtime，并返回 1。
- 显式 `apply --preset` 必须同时携带 fresh plan 的 `--expected-active-route-digest` 与
  `--expected-plan-digest`；manager 在任何写入前检查一次，并在取得 cooperative lock、重读 active
  route/profile/catalog 后再次重算检查。任一 digest stale/mismatch 都在 backup/write 前 fail closed。
- `apply` 在锁内重算，先备份全部变化文件，再用同目录临时文件 + fsync + rename 原子写；
  符号链接用同目录临时链接 + rename 原子替换；写后再次渲染，残余 drift 非零即失败。
- `apply` 不因 persisted session drift 失败，也绝不改写 `state.json`；该 drift 由随后
  `verify` 独立暴露，防止配置收敛与破坏会话被绑成同一动作。
- 备份根固定 `/home/pulse/.relay/runtime-priority-backups/`，目录 0700、文件 0600，
  manifest 对普通文件保存 mode 与 SHA-256，对符号链接保存原始 link target。
- `rollback` 恢复前先留 pre-rollback 安全快照；不存在于原快照的 profile 会被删除。
- 命令从不自动重启 Relay。apply 后先跑 `verify`，最后才走安全重启脚本。

`@anchor symbol _plan_or_apply`
`@anchor symbol _backup`
`@anchor symbol _rollback`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_plan_is_read_only_and_apply_converges`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_rollback_restores_exact_originals`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_manager_refuses_a_codex_model_without_a_latest_family_alias`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_profile_schema_and_field_types_fail_closed`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_verify_detects_persisted_runtime_without_mutating_state`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_apply_repairs_legacy_alias_and_rollback_restores_symlink`
`@anchor symbol _structured_plan`
`@anchor symbol _require_apply_preconditions`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_structured_cost_plan_is_read_only_and_digest_bound`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_cost_preset_apply_requires_fresh_plan_preconditions_before_write`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_structured_plan_identifies_single_effective_route_change`

## 4. 迁移与验收顺序

1. `plan`：确认 agents、direct jobs、harness policies 与排除 jobs 计数符合现场。
2. `apply`：记录输出的 backup id。
3. `verify`：配置与 legacy alias drift 必须为零；persisted session drift 必须如实列出。
   本轮明确保留既有会话时允许该项非零，但不得据此声称“全部会话已迁移”。
4. 跑 Relay、harness、Stats 测试与 Codex `--strict-config --version`。
5. `pulse-relay-restart-safe.sh --check` 无 busy 后，仅在本轮末尾重启一次。
6. 用真实会话、direct job、harness dispatcher 各核一条进程 argv；Stats 同时核模型参数
   与 `--sandbox read-only --ask-for-approval never`。

回滚顺序：`rollback <id>` → 对恢复后的 profile 跑 `verify` → 测试 → 安全重启。
不要只恢复 `relay.json`，否则 cron、harness、Stats 与用户配置会形成混合状态。

现有会话迁移顺序：先用 `/session` 核当前 runtime → 显式 `/runtime codex` → 下一条
普通消息创建/恢复 Codex thread → 再跑管理器 `verify`。禁止直接把 state 的 `runtime`
从 `cc` 改为 `codex`，也禁止把 CC `sessionId` 复制到 `codexThreadId`。

## 5. 权限边界

- YOLO 是 PULSE 当前受管基础设施 agents 的缺省，不是对任意外部 tenant 的授权承诺；
  新增 tenant 时必须显式评估并可写 `codexYoloDefault=false` / `codex_yolo=false`。
- Relay `/yolo off` 是当前 tenant 的内存 override；`/new` 或 Relay 重启恢复 agent 默认。
- Stats quota probe 显式传 `--sandbox read-only --ask-for-approval never`，覆盖
  `/home/pulse/.codex/config.toml` 的全局 YOLO；固定 prompt 不是权限控制，不能替代参数。
- 任何迁移都不得借“运行时切换”扩大 harness path/site policy 的逻辑授权范围；
  YOLO 只改变 Codex 自身 sandbox/approval，PULSE 的职责与写权规则仍有效。

## 6. 成本路由 v2（phase 2–3 staged canary）

本节不改变 §1–§5 的全局 v1 安全缺省。状态必须拆开：phase 1/1.5 的 schema、Hestia
30 分钟 gate 与 Harness per-hit 已 implemented；`current_safe` 是 tracked baseline；
`phase2_stats_canary` 与 `phase23_canary` 是可一键 apply/rollback 的 canary preset。
2026-08-16 生产已按 `current_safe→phase2_stats_canary→phase23_canary` 顺序激活：
phase 2 真实 Stats probe 与 canonical exporter 已有 accepted outcome receipt；phase 23 已被
Relay 加载，Hestia 周复盘父路由与产物路径也已有 accepted outcome receipt。
该回执同时固结了两条重复 Sol/xhigh 嵌套裁决与 `intake-pending` 的负面事实，
因此不得提前声称端到端质量、治理收口或节省效果。

2026-08-16 第一阶段 canary：direct cron
禁止携带模型/effort 字面量，统一引用中央 `compute-profiles.json`；Hestia 30 分钟异常分流
先跑机械 gate，0 新增时不启动 LLM，有新增时引用 `media`（CC=
`claude-sonnet-latest/medium`，Codex=`codex-terra-latest/medium`）。Phase 1.5 再只接两条
consumer path：WI auto-intake 的 `pending_handoffs` / `stale_work_items` 用
`frontier/high`，`/wrap` 收口员用 `media/medium`。其余 direct cron 仍引用
`platform_default`，Stats、Relay 交互缺省与其他 Harness workload 不在 phase 1.5；不得把
这两条路径写成全 Harness 或全平台已完成成本路由。

中央 profile 的 schema v2 把任务档位与供应商型号分离：Codex 只允许
`codex-<family>-latest`，运行时从当前用户的 Codex 模型目录选择该 family 中支持目标 effort
且 `visibility=list` 的最高数值版本；CC latest alias 解析为 Claude Code 原生短别名。目录
超过 7 天未刷新、alias 或 effort 无法验证时 fail closed，禁止回退到另一个 family；运维须
先让当前 Codex CLI 刷新模型目录再重试。2026-08-16 生产当时
`codex-terra-latest` 解析为 `gpt-5.6-terra`，这只是 canary 时点证据，不得回写成规范配置。

### 6.1 成本真相分账

1. **现金账**：只认 owner 可核的实际订阅发票/订单；`subscriptions.json` 的配置金额不是
   外部账单凭据。没有发票时是 `unknown`，不得用官网起售价补成实付。
2. **容量账**：ChatGPT 订阅路径以 Codex session 发布的窗口 `used_percent`、reset 时刻与
   `plan_type` 为主；这是当前最先要优化的稀缺量。
3. **API-key 用量估算**：仅在 API-key 路径且 provider/model price 可追溯时计算；它是
   按公开单价推导的消费估算，仍不得替代实际发票。
4. **订阅路径反事实 API 等价**：ChatGPT 登录路径可选算
   `counterfactual_api_equivalent`，只回答“若同样 token 走 API 约值多少”；它不得改名为
   API 实际成本，也不得并入现金账或容量账。

`@anchor manual cost-truth-ledgers :: 对照最新 Codex session 的 token_count、owner 发票与 Relay model catalog；任一来源缺失就把对应账标 unknown，API-key 用量估算与 counterfactual_api_equivalent 必须分字段且不互相代填`

### 6.2 任务形态与风险地板分轴

基础档只由 `workload_shape` 决定：

| workload shape | 基础路由 | 适用边界 |
|---|---|---|
| `deterministic` | **不调用 LLM** | 阈值判断、文件存在性、格式化、同步、打包；shell/代码先做 gate，只有出现需判断的新事件才升级 |
| `repeatable` | `codex-luna-latest` + `low/medium` | 输入结构稳定，输出可由 schema、测试或固定检查器验收的抽取、分类、简报 |
| `routine_judgment` | `codex-terra-latest` + `medium` | 常规编码、修复、评审与运营分析；验证失败才升 `high` 或下一模型档 |

风险另用 `risk_class`，不与任务形态互斥：`standard` 无额外地板；
`critical_ambiguous` 的 LLM 判断地板为 `codex-sol-latest/high`，没有廉价验收器或需跨模块取舍时
升 `xhigh`。架构、安全、生产变更、Duke 裁决默认归 `critical_ambiguous`；一个任务即使
主体是 deterministic，同批的生产判断/复核仍须过该地板。

先按“显式任务 override → job/policy workload shape → consumer surface 缺省 → v1 profile”
得到 `base_route`，再计算 `effective_route = max(base_route, risk_floor)`。档位序固定为
`none < luna/low < luna/medium < terra/medium < terra/high < sol/high < sol/xhigh`。
显式 override 可升档；`critical_ambiguous` 不得被 override 降到 `sol/high` 以下。
额度耗尽/provider 故障属于可用性 fallback，不得伪装成“任务适合小模型”。

tracked preset 的每条 route 必须显式带 `consumer_path`、`workload_shape`、`risk_class`、
`verifier_id`、`base_profile` 与 `rollback_profile`；active route 再物化
`base_route` / `risk_floor` / `effective_route`。`deterministic` 的正常 effective 永远是 none；
若同批判断属于 `critical_ambiguous`，frontier 地板施加在 `escalation_profile`，而不是让正常
机械路径启动 LLM。active manifest 的根与五路由键集必须精确，且 preset/status、Stats
base/floor/effective/verifier/rollback 必须互相一致；坏 schema、未知 preset/profile 或 critical
路由低于 frontier 全部 fail closed。

plan/apply 另以机械 transition gate 防越级：active 缺失只允许进入 `current_safe`；
`current_safe` 只允许 self/`phase2_stats_canary`；phase 2 才允许升 phase 3，phase 3 可逐档或直接
降回任一安全档。该状态机只证明配置切换顺序，绝不等于真实 UAT；每级生产升级仍须由操作流程
取得真实消费路径 receipt，并经独立 outcome gate 判定后才可继续。

`@anchor manual workload-routing-review :: 抽查每个 direct job 与 harness policy：必须能指出 workload_shape、risk_class、验收器和升级条件；复算 effective_route=max(base_route,risk_floor)，缺任一项或 critical 低于 sol/high 都不得从 v1 缺省降档`

### 6.3 consumer 落点与先后顺序

1. **先去 LLM（Hestia canary 已落）**：Hestia 30 分钟扫描由脚本按 timestamp 筛窗口；
   退出 3 表示 0 新增，scheduler 在 runtime spawn 前成功结束。证据损坏、SSH 失败或 gate
   超时保持 unknown 并 fail closed，不能当 0。账本更新、日志整理、EPUB 打包仍是候选，未实施。
2. **direct cron 档位（phase 3 可回滚 canary，生产已激活）**：job 只能写
   `computeProfile` 名称，load 时机械拒绝模型/effort 字面量；Hestia 30 分钟 gate 的正事件
   路径保持 `media`，周复盘只有在 `phase23_canary` 才从 `platform_default` 切 `media`，其余
   direct jobs 始终 `platform_default`。周复盘同时校准周日 16:00，并以
   `memo/review-${ISO_WEEK}-draft.md` 的 ≥2000 bytes、fresh、status/draft、五节与 `[待确认]`
   作为退出码之外的结构验收；扩大到其他 job 仍须逐任务验收。
3. **Stats quota probe 独立（priority 2）**：`phase2_stats_canary` 只把这一条切到
   `economy`（CC=`claude-haiku-latest/low`，Codex=`codex-luna-latest/low`）；Stats 按 Relay
   同契约核 cache TTL=7d、future skew=5min、visibility=list、family+effort 后才形成真实 argv。
   route/catalog/cache/effort 任一失败都不退 Sol/xhigh、不造 argv、不执行；验收是仍能生成带
   window 与 reset 的有效 `token_count`。先真实 UAT phase 2，才允许升 phase 23。
4. **Harness 按 hit 路由（phase 1.5 仅 WI auto-intake）**：生产 policy 的 consumer
   default 是 `platform_default`；只有既有 `pending_handoffs` / `stale_work_items` 扫描项
   写 `frontier`，单 hit 时覆盖 consumer profile，解析为两端 `high`。其他 scan type
   不造 profile；高风险写操作不得以 policy 或 hit override 绕过 frontier 地板。
5. **`/wrap`（phase 1.5）**：spawn 前按 provider 解析 `media`，两端均使用 resolved
   model + `medium`；解析失败阻断，Codex 不再继承父模型或 full-history fork。
6. **Codex Desktop 互动分身（phase 1.5）**：当前 Codex 配置 schema 不支持
   `[agents].default_subagent_reasoning_effort` / `default_subagent_model` scalar；写入任一项会使
   CLI 在读 `config.toml` 时失败。管理器必须清除它们。普通 Engineer/Reviewer 先用
   `harness compute-profile resolve frontier --provider codex --json` 解析中央 `frontier`，再把
   resolved model、`high`、`fork_turns=none` 显式传给 `spawn_agent`；只读探索或大规模扫描
   改解析 `media` 并显式传 `medium`。解析失败即阻断。Codex Desktop spawn 不接受 PULSE
   alias，因此 resolved 具体型号只允许出现在当次调用参数，不得固化进用户配置或本规范。
   此项只管理本机互动委派，不代表 Relay/Harness 生产消费路径已改变。
7. **最后才试 Relay 交互缺省**：先补 per-agent effort/workload profile，普通会话再做
   Terra canary；共享 v1 profile 在此之前继续承担强安全兜底。

### 6.4 观测、晋级与回滚

每次真实 LLM 消费至少发出：`consumer_path`、agent/job/hit/work-item id、model、effort、
`workload_shape`、`risk_class`、`base_route`、`risk_floor`、`effective_route`、token/窗口消耗、
验证结果、outcome receipt、retry/escalation。字段缺失保持 `unknown`；不得拿 activity、
进程退出 0 或测试启动替代 outcome。

每档晋级至少观察 7 天且有 10 个真实 outcome；不足 10 个仍是 `unknown`。出现一次高风险
错误、静默错误或权限越界立即回滚该档。其余档只有在 accepted 比例不低于基线、人工纠错率
不高于基线、每个 accepted outcome 的周额度消耗下降时才扩大。回滚只撤该 job/policy/profile，
不得把整个 PULSE 一键改成较小模型。

Phase 2 生产验收已证明 `economy`→Luna/low、真实 `token_count`
（7 天窗口、48% 与 reset）及 canonical exporter `available/validated`，回执为
`pulse-cost-route-phase2-stats-luna-20260816-prod-canary`。20:00 的 Hestia 30 分钟
真实回归仍是 `gateStatus=skip` / `runtime=none`。Phase 23 已激活并加载，但周复盘
真跑虽已生成并通过结构验收，却暴露父 Agent 对 yielded 裁决进程重复启动：
两条嵌套 `adjudicate-spawn.sh` 仍消费 Sol/xhigh，而裁决账只留 `intake-pending`。
因此父路由 Terra/medium 生效不等于端到端降本；该嵌套 consumer 与 §6.4 的完整
workload/risk/outcome telemetry 在本版修订时仍未齐。
仍不得声称 7 日节省、金额、质量不降或扩大覆盖面。

`@anchor manual cost-routing-uat :: 对每个 canary 保存 baseline 与 7 天结果，逐项核 accepted、人工纠错、额度/accepted、升级次数和高风险错误；并由 workload_shape、risk_class、base_route、risk_floor 复算 effective_route，任何 critical 低于 sol/high 或少一项即不得扩面`
`@anchor test Relay/tests/scheduler.test.ts :: "pre-LLM gate skip returns ok without spawning a runtime"`
`@anchor test Relay/tests/scheduler.test.ts :: "positive gate resolves media profile before spawning CC"`
`@anchor test Relay/tests/compute-profiles.test.ts :: "resolves latest aliases from runtime-owned model catalogs"`
`@anchor test Relay/tests/compute-profiles.test.ts :: "fails closed when a latest alias has no matching visible model"`
`@anchor test Stats/exporter/tests/test_live_contract_cost_route.py :: test_stats_route_resolves_luna_low_from_visible_fresh_catalog`
`@anchor test Stats/exporter/tests/test_live_contract_cost_route.py :: test_missing_route_materializes_no_probe_argv_and_starts_no_process`
`@anchor test Relay/tests/scheduler.test.ts :: "expands ${ISO_WEEK} and requires every configured content fragment"`
`@anchor test Relay/tests/scheduler.test.ts :: "rejects a weekly draft that is missing one required section"`
`@anchor manual codex-desktop-subagent-default :: 核 ~/.codex/config.toml 不含两个 unsupported scalar；普通 Engineer 从中央 frontier resolver 取 resolved model/high、探索任务从 media 取 resolved model/medium，并显式传给 spawn`
