# SPEC：harness 策略与权限面 — policy / site_policy

> version: `0.2.0`
> status: `reverse_engineered_from_code`
> authority: heartbeat policy schema 与站点级 namespace 权限的**现行实现契约**
> owner: Pulse（wi_20260804_070 全面 spec 化）
> modified: `2026-08-15`
> 版本史：只看 git log

**取证基础**：2026-08-09 代码普查 + spec-anchor 机器验证全绿。

## 0. 最重要的结构事实：默认方向是混合的

站点权限层**无配置即全放行**（环境变量未设 → policy 为 None → 所有 guard 直接返回，
连 decision 都不看）；但 **registry 派生是 fail-closed**（registry 缺失/空 agents 段
一律抛错）。cron 会话闸也是 fail-closed（policy 加载出错按拒绝处理）。写任何依赖
site policy 的判断前，先确认该环境到底装没装 policy。

## 1. heartbeat policy schema

- [x] 顶层模型与加载链：唯一读盘入口 load_policy；缺文件与校验错分属两个异常
  `@anchor symbol HeartbeatPolicy`
  `@anchor symbol load_policy`
  `@anchor symbol policy_path`
  `@anchor symbol PolicyNotFoundError`
  `@anchor symbol PolicyValidationError`
  `@anchor symbol _parse_and_validate`
- [x] 顶层 schema_version 只接受 1.0（site policy 接受 1.0/2.0——同名字段两套允许域，
  见 §4）
- [x] 自治配置：runtime 三值（claude/codex/claude_glm），平台默认 runtime 为
  codex，fallback_runtimes 默认空；`compute_profile` 可选，Codex/CC 模型字面量保留为
  legacy 显式输入但不再有隐含默认。profile 与任一 legacy 模型/effort 字段并存是歧义，
  schema fail closed；`codex_yolo` 默认 true。
  session_mode 仅两值 memo_only（默认）/ full_execution；
  allowed_tools 拒空；path_constraints 键只许 read/write/write_deny 三种
  `@anchor symbol AutonomousLLMConfig`
  `@anchor symbol _ALLOWED_PATH_CONSTRAINT_KEYS`
  `@anchor test agent-harness/tests/test_policy.py :: test_003b_autonomous_llm_config_accepts_codex_runtime`
  `@anchor test agent-harness/tests/test_policy.py :: test_003_autonomous_config_defaults`
  `@anchor test agent-harness/tests/test_policy.py :: test_003a_compute_profiles_are_optional_at_both_policy_levels`
  `@anchor test agent-harness/tests/test_policy.py :: test_003ab_profile_and_legacy_literals_are_ambiguous`
  `@anchor test agent-harness/tests/test_policy.py :: test_013_illegal_path_constraints_key`
  `@anchor test agent-harness/tests/test_policy.py :: test_017_path_constraints_only_read_allowed`
- [x] 扫描项策略：PerActionRule 首条匹配胜出，否则落 default_autonomy（默认
  mechanical）；扫描项可声明 `compute_profile`。单 hit 派发时扫描项 profile 覆盖
  consumer 的 `autonomous_llm_config.compute_profile`，扫描项未声明才继承 consumer；
  卫生窗口与提交摘要条数有边界校验（0..1440 / 0..100）
  `@anchor symbol ScanItemPolicy`
  `@anchor symbol PerActionRule`
  `@anchor symbol AutonomyTier`
  `@anchor test agent-harness/tests/test_policy.py :: test_006_audited_action_config_requires_paths_when_enabled`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_scan_item_profile_overrides_consumer_profile_for_codex_single_hit`
  `@anchor test agent-harness/tests/test_policy.py :: test_landed_policies_use_profiles_without_model_literals`
- [x] 升级规则：channel 为 feishu/both 时必须给 feishu_recipient
  `@anchor symbol EscalationRules`
- [x] 写入链 Flow-001：草稿校验（不碰落地文件）→ 原子写 → 事件记录；源路径必须在
  仓内
  `@anchor symbol write_policy`
  `@anchor symbol validate_draft`
  `@anchor symbol _assert_path_in_repo`
  `@anchor test agent-harness/tests/test_policy_write.py :: test_source_path_outside_repo_rejected`
  `@anchor test agent-harness/tests/test_policy_write.py :: test_os_replace_failure_no_event`
  `@anchor test agent-harness/tests/test_policy_write.py :: test_mid_validate_exception_no_tmp_residue`
- [x] 导出面冻结契约有机器测试钉着（签名与 __all__ 只增不改）
  `@anchor test agent-harness/tests/test_policy_write_integration.py :: test_load_policy_signature`
  `@anchor test agent-harness/tests/test_policy_write_integration.py :: test_policy_exports_additive`

## 2. site policy：namespace 权限模型

- [x] 全部写权判定收敛到一个点：namespace 是否在本站可写清单
  `@anchor symbol require_namespace_write`
  `@anchor symbol HarnessSitePolicy`
  `@anchor symbol SitePolicyError`
  `@anchor symbol _NAMESPACE_RE`
  `@anchor test agent-harness/tests/test_site_policy.py :: test_vultr_can_create_owned_work_item`
  `@anchor test agent-harness/tests/test_site_policy.py :: test_vultr_cannot_create_local_work_item`
  `@anchor test agent-harness/tests/test_site_policy.py :: test_duplicate_writable_namespaces_rejected`
- [x] 跨站 handoff 判定链顺序敏感（七个出口）：源 wi 可写 → 发起方可写 → 目标同站
  直接放行 → 跨站开关（默认关）→ 目标白名单 → 转移档合法 → 转移档在允许清单。
  transfer 缺省按 not_transferred 对待，**full 档默认被拒**
  `@anchor symbol allow_handoff_create`
  `@anchor symbol ResponsibilityTransferValue`
  `@anchor test agent-harness/tests/test_site_policy.py :: test_vultr_can_create_cross_site_request_handoff_to_amanda`
  `@anchor test agent-harness/tests/test_site_policy.py :: test_vultr_cross_site_handoff_cannot_use_full_transfer`
  `@anchor test agent-harness/tests/test_site_policy.py :: test_cross_site_handoff_target_allowlist_is_enforced`
- [x] v2 registry 派生 fail-closed：registry 缺失 / agents 段空 / 缺 registry_path
  一律抛错，不静默降级
  `@anchor symbol _load_registry_agents`
  `@anchor symbol _derive_policy_v2`
  `@anchor test agent-harness/tests/test_site_policy.py :: test_v2_fail_closed_when_registry_missing`
  `@anchor test agent-harness/tests/test_site_policy.py :: test_v2_fail_closed_when_registry_agents_empty`
  `@anchor test agent-harness/tests/test_site_policy.py :: test_v2_missing_registry_path_rejected`
- [x] v2 派生的可写集 = registry agents + infrastructure + tenants；跨站目标缺省
  只含 agents（不含 infra/tenants）
  `@anchor test agent-harness/tests/test_site_policy.py :: test_v2_derives_writable_from_registry_agents`
  `@anchor test agent-harness/tests/test_site_policy.py :: test_v2_tenant_agent_not_in_cross_site_target`
- [x] tenant 语义：v1 policy 下恒 False；v2 下由 tenant_namespaces 决定
  `@anchor symbol is_tenant_agent`
  `@anchor symbol tenant_of`
  `@anchor test agent-harness/tests/test_site_policy.py :: test_v1_policy_is_tenant_agent_always_false`
- [x] 加载入口三个：显式路径 / 环境变量（未设返回 None）/ ctx 解析
  `@anchor symbol load_site_policy`
  `@anchor symbol load_site_policy_from_env`
  `@anchor symbol resolve_site_policy`
  `@anchor symbol require_allowed`

## 3. 强制执行点（谁真的在查）

CLI 各 guard 前置一律「policy 为 None 即返回」（无配置全放行）；scan 与 dispatcher
在事件写入前查并以非零退出拒绝；escalation 的日聚合刷送是唯一**警告后继续**的消费方
（与其他执行点行为不一致，登记见 §4）。cron 会话闸独立于 site policy：
policy 加载失败按拒绝处理（fail-closed）。

`@anchor test agent-harness/tests/test_cli.py :: test_create_blocked_in_cron_autonomous_session`

## 4. 已知两义与债（登记待裁）

`@anchor manual agent 标识两套合法域 :: policy 侧正则要求 2-31 字符，site_policy 侧 namespace 正则允许 1-64 字符；单字符与 32-64 字符的名字在两模块判定相反`
`@anchor manual path-constraints 报错文案漏键 :: 校验按三键白名单比对，报错文案只列 read/write 两键，用户会被误导`
`@anchor manual write-policy 进程级环境副作用 :: 写入链临时改 HARNESS_ROOT 环境变量再恢复，多线程下是竞态源且 docstring 未列此副作用`
`@anchor manual schema-version 同名两义 :: 顶层 policy 只收 1.0，site policy 收 1.0/2.0；同名字段不同允许域`
`@anchor manual 升级刷送闸行为不一致 :: site policy 拒绝时 scan/dispatch 非零退出，escalation 日聚合只警告继续；同一权限模型三种失败行为`
`@anchor manual session-mode 无专项测试 :: 决定 cron 分身能否写状态的核心键在 policy 测试文件里无直接用例，仅靠 cli 侧闸测试间接覆盖`

## 5. 覆盖声明

本 spec 给 policy schema 全部模型、写入链、site policy 判定链与 v2 派生首次挂
机器锚点。存量 harness-site-policy / agent-self-authored-policy /
heartbeat-policy-path-hardening 三目录的现行契约部分由本文接管（path_guard
运行时钩子归 harness-cli-surface.md）。
