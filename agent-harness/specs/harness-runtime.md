# SPEC：harness 运行时与进程面 — locks / reaper / compute profiles / dispatcher

> version: `0.2.3`
> status: `reverse_engineered_from_code`
> authority: 文件锁、执行者活性、GLM PTY、compute profile 路由与 dispatcher 批次的**现行实现契约**
> owner: Pulse（wi_20260804_070 全面 spec 化）
> modified: `2026-10-06`
> 版本史：只看 git log

**取证基础**：2026-08-09 代码普查 + spec-anchor 机器验证全绿。

## 1. locks：三类锁与三种模式

- [x] 三类锁：agent 锁（locks 平铺）、wi 锁（wi 子目录）、state 全局锁
  （复用 agent 锁、固定名 __state__，是 CLI 状态写的唯一原子性依托）
  `@anchor symbol acquire_lock`
  `@anchor symbol acquire_wi_lock`
  `@anchor symbol _lock_path`
  `@anchor symbol LockHeldError`
  `@anchor test agent-harness/tests/test_locks.py :: test_lock_file_under_wi_subdir`
  `@anchor test agent-harness/tests/test_locks.py :: test_distinct_wi_ids_do_not_block`
  `@anchor test agent-harness/tests/test_locks.py :: test_same_wi_id_is_mutually_exclusive`
- [x] 三种模式：nonblock 试一次即抛；defer 隔十秒再试一次；wait 轮询到超时
  （公开 API 默认超时 30 秒）。失败一律抛错，布尔返回值恒真是死语义
  `@anchor test agent-harness/tests/test_locks.py :: test_second_acquire_fails_fast`
  `@anchor test agent-harness/tests/test_locks.py :: test_defer_succeeds_when_holder_releases_in_window`
  `@anchor test agent-harness/tests/test_locks.py :: test_wait_acquires_after_holder_releases`
  `@anchor test agent-harness/tests/test_locks.py :: test_wait_times_out`
- [x] 崩溃语义：flock 随进程死亡由内核释放，holder 行残留不影响下次加锁；
  残留清理只对 agent 锁有效
  `@anchor symbol clear_released_lock_holder`
  `@anchor test agent-harness/tests/test_locks.py :: test_kill_holder_releases_lock`
  `@anchor test agent-harness/tests/test_locks.py :: test_clear_released_lock_holder_removes_stale_text`
  `@anchor test agent-harness/tests/test_locks.py :: test_clear_released_lock_holder_keeps_active_holder`
- [x] 调用点模式绑定：scan 用 nonblock（拿不到即记 skipped 事件返回）、
  dispatcher 拉分身用 defer、CLI 状态写用 wait；锁强制默认只对
  cron/session_start 两种触发模式生效
  `@anchor symbol wi_lock_enabled`
  `@anchor symbol executor_reaper_enabled`
  `@anchor symbol _should_enforce_lock`

## 2. reaper：执行者活性判定与收割

- [x] 三值裁决与不可变真值快照
  `@anchor symbol Liveness`
  `@anchor symbol LivenessInputs`
  `@anchor symbol classify_executor_liveness`
- [x] 判定链顺序：跨机 claim 先行（他机持有一律 UNKNOWN，不看本地证据）→
  relay 记录解析（裸名精确优先，其次机器后缀变体）→ 存活二判（记录的
  session 在活 resume 集合，或该 agent 任一变体有活 resume）→ 记录在而
  进程无为 UNKNOWN（可恢复保守桶）→ 记录进程双无才 DEAD
  `@anchor symbol _resolve_relay_record`
  `@anchor symbol _agent_has_any_live_resume`
  `@anchor test agent-harness/tests/test_reaper.py :: test_alive_when_live_process_resumes_agent_session`
  `@anchor test agent-harness/tests/test_reaper.py :: test_dead_when_no_relay_record_and_no_live_resume`
  `@anchor test agent-harness/tests/test_reaper.py :: test_unknown_when_cross_machine_claim`
  `@anchor test agent-harness/tests/test_reaper.py :: test_stale_relay_record_wrong_session_is_unknown_not_dead`
  `@anchor test agent-harness/tests/test_reaper.py :: test_bare_ledger_name_matches_relay_machine_suffix_alive`
  `@anchor test agent-harness/tests/test_reaper.py :: test_match_not_over_wide_ken_not_kenny`
- [x] 防 pid 复用：只认 cmdline 里的 resume 会话 id，从不信裸 pid
  `@anchor symbol read_live_resume_session_ids`
  `@anchor test agent-harness/tests/test_reaper.py :: test_pid_reuse_does_not_falsely_read_alive`
  `@anchor test agent-harness/tests/test_reaper.py :: test_pid_reuse_does_not_falsely_read_dead`
- [x] 规划纯函数：DEAD 即释放（无视 SLA）、ALIVE 即保留、UNKNOWN 超 SLA 才告警
  且**不写状态**（严格大于，等于 SLA 保留）
  `@anchor symbol plan_reap`
  `@anchor symbol ReapAction`
  `@anchor symbol ReapDecision`
  `@anchor test agent-harness/tests/test_reaper.py :: test_plan_release_on_dead_ignores_sla`
  `@anchor test agent-harness/tests/test_reaper.py :: test_plan_keep_on_alive_regardless_of_age`
  `@anchor test agent-harness/tests/test_reaper.py :: test_plan_alert_stale_on_unknown_over_sla`
- [x] CLI 侧硬前置：relay state 不可读时把判定钉死为 UNKNOWN——「relay 不可读
  ⟹ 永不释放」这条安全不变量在 CLI 层而非模块层保证
  `@anchor symbol relay_state_available`
  `@anchor symbol read_relay_sessions`
  `@anchor symbol gather_liveness_inputs`
- [x] 收割写路径：持全局 state 锁重读四道幂等校验后才释放并记事件；
  ALERT_STALE 分支绝不写状态
  `@anchor test agent-harness/tests/test_machine.py :: test_reap_clears_active_executor`

## 3. claude_glm_pty：GLM 备胎运行时

- [x] 环境注入：先剥净五个 Anthropic 环境键再从 GLM_ 前缀回填；凭据缺失直接抛
  `@anchor symbol apply_glm_provider_env`
  `@anchor symbol ANTHROPIC_ENV_KEYS`
  `@anchor test agent-harness/tests/test_claude_glm_pty.py :: test_strips_bare_anthropic_and_injects_glm`
  `@anchor test agent-harness/tests/test_claude_glm_pty.py :: test_missing_creds_raises`
- [x] terminal 判定从 session JSONL 抽取：assistant 终止且有非空文本块，或
  result 条目；时间闸拦旧条目
  `@anchor symbol extract_interactive_run_outcome`
  `@anchor symbol get_session_file_path`
  `@anchor symbol find_existing_session_file`
  `@anchor test agent-harness/tests/test_claude_glm_pty.py :: test_terminal_end_turn`
  `@anchor test agent-harness/tests/test_claude_glm_pty.py :: test_timestamp_gating`
  `@anchor test agent-harness/tests/test_claude_glm_pty.py :: test_pending_tool_use_ids`
- [x] 退出码覆写四规则：拿到 terminal 后主动杀（143）算 0；退出 0 但 turn 未
  完成强制 1；命中上下文超限/内容拒绝文本强制 1；超时不改码只补 stderr
  `@anchor symbol run_claude_glm_pty`
  `@anchor symbol _is_immediate_runtime_failure_text`
  `@anchor test agent-harness/tests/test_claude_glm_pty.py :: test_success_returns_jsonl_outcome`
  `@anchor test agent-harness/tests/test_claude_glm_pty.py :: test_incomplete_turn_is_failure`
- [x] bridge 脚本解析：显式环境覆盖或当前安装包内的 canonical 模块文件；
  不依赖 Relay checkout 的绝对位置，wheel 内含同一源文件。
  `@anchor symbol default_bridge_script_path`

canonical `claude_pty_bridge.py` 同时由 Harness wheel 与 Relay 源码链接消费。拥有者
stdin 关闭、输出管道断开或收到终止信号时，它只收尾自己启动的 PTY 子进程组；
SIGTERM/SIGINT 最多保留两秒退出窗口，仍存活的直接子进程触发 SIGKILL 并 wait 收割。
没有按命令行扫描或回收历史进程。此合同不宣称任意已脱离子进程组的后代、桥接器
自身被 SIGKILL 或非 PTY runtime 的孤儿均被覆盖；服务管理及真实节点崩溃验收分别验证。
`@anchor test agent-harness/tests/test_claude_pty_bridge.py :: test_bridge_reaps_its_term_ignoring_child_when_owner_pipe_closes`
`@anchor test agent-harness/tests/test_claude_pty_bridge.py :: test_installed_wheel_contains_and_runs_packaged_bridge`

## 4. Compute profile resolver 与 dispatcher 命令

- [x] 中央 resolver 先读 `PULSE_COMPUTE_PROFILES_PATH`，未设时从 checkout 发现
  Relay `compute-profiles.json`。Codex cache 只接受 `visibility=list`，刷新时间必须在
  7 日内且不得领先当前时间超过 5 分钟；latest alias 必须同时匹配 family 与 effort，
  取最高数值版本。CC latest alias 解析为 Claude Code 原生短别名。缺目录、陈旧、未来
  时间、无 family/effort 匹配都 fail closed，不猜测、不跨 family fallback。
  `@anchor symbol resolve_compute_profile`
  `@anchor test agent-harness/tests/test_compute_profiles.py :: test_frontier_resolves_cc_native_alias_and_codex_highest_visible_match`
  `@anchor test agent-harness/tests/test_compute_profiles.py :: test_codex_catalog_outside_ttl_or_clock_skew_fails_closed`
- [x] CLI 提供 `harness compute-profile resolve NAME --provider codex|cc --json`；成功输出
  稳定 JSON，失败非零且给出可诊断错误。
  `@anchor test agent-harness/tests/test_compute_profiles.py :: test_cli_resolve_emits_stable_json`
  `@anchor test agent-harness/tests/test_compute_profiles.py :: test_cli_resolves_media_for_both_wrap_providers`
- [x] dispatcher 每次 runtime attempt 重新解析 effective profile。单 hit 优先扫描项
  profile，其次 consumer profile；两者都无时才兼容 legacy 字面量。profile 与 legacy
  并存、profile 不可解析或 provider 不支持时均在 subprocess 前 fail closed，并记录
  error event。Codex 走 resolved `-m` 与 `model_reasoning_effort`；CC 走 resolved
  `--model` 与 `--effort`。`codex_yolo=true`（默认）下发
  `--dangerously-bypass-approvals-and-sandbox`，与 CC 的 bypassPermissions 缺省权限
  对齐；显式 false 才恢复 workspace-write + `approval_policy="never"`。
  `@anchor symbol _build_codex_command`
  `@anchor symbol _resolve_runtime_compute_route`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_consumer_compute_profile_drives_cc_model_effort_and_event`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_profile_resolution_failure_records_error_without_llm_spawn`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_codex_runtime_uses_codex_exec_yolo_by_default`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_codex_runtime_can_explicitly_restore_workspace_sandbox`
- [x] 自治事件记录 profile/alias/resolved model/effort 与
  `consumer_path="harness_dispatch"`；started、正常完成与 error 都写同一路径标识。
- [x] Codex 的 runtime-blocked 判定只接受**明确写路径拒绝证据**：stderr，或 stdout
  JSONL 的 top-level `error`、`turn.failed`、以及状态为 `failed`/`declined` 的工具
  item 错误载荷，均须在对应错误载荷内命中已知 write-rejection pattern。普通
  runtime/API/rate-limit failure 本身不等于 blocked；普通 `agent_message`、reasoning、成功工具
  输出与命令文本中的孤立 `BLOCKED`、`BLOCKED_*`、历史
  `HARNESS_RUNTIME_BLOCKED: BLOCKED` 都只是会话内容，不得把 exit=0 的正常轮次改判
  error；明确的 `patch rejected: writing outside of the project` 仍须判 error。
  `@anchor symbol _detect_runtime_blocked`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_codex_failed_command_write_rejection_is_error`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_codex_failure_event_write_rejection_is_error`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_codex_non_write_failure_event_is_not_runtime_blocked`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_codex_benign_blocked_text_is_not_runtime_error`

Phase 1.5 的生产应用范围只含 WI auto-intake 与 `/wrap`：WI 使用 `frontier/high`，
`/wrap` 使用 `media/medium`。这不是全 Harness 或全平台迁移；7 日观察窗与真实正向
outcome 尚待生产观察，不得由单测或 argv 验证替代。

## 5. dispatcher 自治批次与时限口径

- [x] 每轮 dispatch 按 scan 顺序最多取 3 个 `autonomous_llm` hit；每个 hit 建一个
  独立 runtime subprocess，三者**串行**而非合并、并行。第 4 个起标记
  `deferred_serialization`，留到后续 scan；单点失败不阻断同批后续 hit
  `@anchor const _AUTONOMOUS_BATCH_SIZE = 3`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_autonomous_multi_hit_batch3_serializes`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_autonomous_4_hits_defers_beyond_batch3`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_autonomous_batch_fail_fast_per_item`
- [x] `autonomous_llm_config.max_duration_seconds` 是**每个 hit**的 runtime 上限；
  subprocess timeout 在其上再加 30 秒。调用方的 dispatch timeout 是整个 dispatcher
  进程的聚合预算，须覆盖至多 `3 × (max_duration_seconds + 30)`、逐 hit 后处理与
  post-session Harness test gate；不能把单 hit 上限误当整轮上限
  `@anchor symbol spawn_autonomous_session`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_success_branch_and_args`

## 6. 已知两义与债（登记待裁）

`@anchor manual 根解析四套实现 :: scan/policy/dispatcher 各有同名私有根解析函数,locks 另叫一名,四处逻辑相同无测试锁定等价;锚点引用必须带模块限定`
`@anchor manual 无 proc 平台假死入口 :: 活 resume 集合在无 proc 文件系统的平台恒空,判定退化为无 relay 记录即 DEAD,模块层无闸;macOS 上跑 sweep 前必须确认 CLI 前置在`
`@anchor manual resume 匹配不验进程名 :: 判据注释称只认 claude 进程,实现对任何带 resume 参数加会话 id 的进程都算活;实现比文档宽`
`@anchor manual wi 锁零生产调用 :: acquire_wi_lock 只有测试在用,生产路径复用全局 state 锁;主链 spec 锚的是一个未接线的能力`
`@anchor manual GLM 环境注入双实现 :: pty 侧纯函数返回新字典,dispatcher 侧同名近亲原地改传入字典,语义相反;后者当前半死代码但仍被运行时命令路径调用`
`@anchor manual 收割事件一词两义 :: executor_reaped 既是事件流的事件类型又是升级流的分档 label,同一字符串两套语义`

## 7. 覆盖声明

本 spec 给三类锁、三模式、活性三值判定链、防 pid 复用、收割规划、GLM PTY 退出码、
dispatcher 自治串行批次与聚合时限契约首次挂机器锚点。heartbeat-runtime 存量目录的
现行契约部分由本文接管。
