# SPEC：harness CLI 剩余面 — 只读命令 / 治理命令 / path_guard 钩子 / 打包资源

> version: `0.2.1`
> status: `reverse_engineered_from_code`
> authority: 主链 spec 未覆盖的 CLI 子命令、CC 写入钩子与打包资源的**现行实现契约**
> owner: Pulse（wi_20260804_070 全面 spec 化）
> modified: `2026-08-23`
> 版本史：只看 git log

命令清单以当前 CLI 的 `--help` 和注册源码为准；装饰器数量不代表业务命令或模块覆盖率。
本文的合同由对应 symbol/test 锚点验证，验证结果应绑定实际被检验的源码版本。

## 1. 只读命令五件（无 cron 闸无状态锁，符合预期）

- [x] show / list / handoffs / log / extract；show 对 legacy 坏记录回落宽容
  展示不崩
  `@anchor symbol _show_legacy_work_item`
  `@anchor test agent-harness/tests/test_cli.py :: test_show_legacy_missing_owner_status_does_not_crash`
  `@anchor test agent-harness/tests/test_cli.py :: test_list_status_skips_invalid_legacy_work_item`
  `@anchor test agent-harness/tests/test_cli.py :: test_log_shows_events`
  `@anchor test agent-harness/tests/test_cli.py :: test_extract_error_exits_2`

## 2. 治理命令组

### 2.1 execution recover-active-surface

- [x] `harness execution recover-active-surface <wi> --agent <owner>` 是 active foreign-surface 的最小人工恢复面，不改变普通 entry/resume 的不可抢占规则。
- [x] 必填 exact `--source-session-ref`、bounded opaque `--human-authorization-ref`、bounded opaque `--operation-ref`、`--reason` 和显式 `--human-confirmed`；target 必须且只能二选一：`--target-runtime-class + --target-surface-id`，或 `--target-runtime + --target-session-ref`。明显 path/token/secret/private 材料 fail closed。
- [x] CLI 把显式 `--agent` 同时交给 site-policy guard 和 manager；manager hard-reject cron 与任何非空 `HARNESS_AGENT_ID`，要求 open WI `main_only=true`，并在 global state lock → WI lock 内核 `--agent = binding Agent = open WI owner`。`human_authorization_ref` 只是审计 join ref，真正 authority 是 unstamped main-body gate + explicit confirm。Harness 只验 surface token 形状，不宣称已完成 registry lookup。
- [x] 成功只做 epoch +1、跨 runtime active binding 替换、current checkpoint pointer 清空和 privacy-safe audit；不复制 coverage/usage。binding/event 成对写失败恢复原字节，binding 内 receipt 可在 post-binding crash 后补 event；receipt event 缺失时不同 operation 不得覆盖，rollback 不完整报 recovery required。
- [x] 相同完整请求幂等，任一 ref/source/target/reason 改变冲突。recovery audit 仅保留 operation join ref、source epoch、target authority，以及 auth/source Session/source runtime/reason digest。
- [x] 退出码沿用 execution 面：10=非法 ref/target，11=session/lifecycle/idempotency conflict，12=global/WI lock，13=not found，14=caller/main_only/owner/site-policy，15=已完整 rollback 的 IO，16=需 reconciliation 或 rollback 不完整。

`@anchor symbol recover_active_surface_command`
`@anchor test test_execution_cli_recover_active_surface_round_trip_and_idempotency`
`@anchor test test_execution_cli_recover_active_surface_failures_are_machine_readable`
`@anchor test test_execution_cli_recovery_replay_requires_exact_authorization_and_is_private`
`@anchor test test_execution_cli_recovery_rejects_stamped_or_cron_callers`

### 2.2 execution recover-dormant-surface

- [x] `harness execution recover-dormant-surface` 只接收 `main_only`、同 owner、已经 dormant 且从未产生 checkpoint 的 exact source authority；有任何 checkpoint revision/pointer、continuation、transition 或 recovery-required 状态都拒绝。
- [x] authority 与隐私闸复用 active recovery：必须 unstamped interactive main body、非 cron、显式 `--human-confirmed`、bounded opaque authorization/operation refs、exact source Session 与唯一 target form。
- [x] 成功 epoch +1 并激活 fresh target，但不补造 source checkpoint、coverage、usage 或 transcript；receipt/event 明确记录 `source_lifecycle=dormant` 与 `source_coverage=unknown_unobserved`，原始 source/auth/reason 仍只留 digest。
- [x] active recovery 的历史 request digest、receipt schema 与事件保持兼容；dormant target Session 使用独立 domain，重复 exact 请求幂等。

`@anchor symbol recover_dormant_surface_command`
`@anchor test test_execution_cli_recover_dormant_surface_round_trip_and_guards`
`@anchor test test_recover_dormant_surface_preserves_unknown_gap_and_rebinds_epoch`
`@anchor test test_recover_dormant_surface_rejects_checkpointed_or_non_dormant_authority`

### 2.3 worktree claim-owner

- [x] `harness worktree claim-owner` 只补齐“直接创建给 owner 的 `main_only`
  WI”在首次 worktree registration 前的 claim 空缺；必须 `in_progress`、
  `active_executor=null`、owner 与 Agent 一致、unstamped interactive、非 cron、显式
  human confirmation，并持有 bounded opaque authorization/operation refs。
- [x] bootstrap claim 只允许写 linked WI worktree 或 Git 外部 authority store；shared
  primary checkout fail closed。审计事件只保留 authorization/reason digest，重复执行在同一
  owner 已 claim 时不新增事件。

`@anchor symbol claim_owner_command`
`@anchor test test_cli_claim_owner_bootstraps_direct_main_only_wi_with_private_audit`
`@anchor test test_cli_claim_owner_fails_closed_outside_exact_bootstrap_case`

- [x] gate check / compliance check 与 create-work-item：退出码 0=过 1=违
  2=前置缺失
  `@anchor test agent-harness/tests/test_cli_guardrail.py :: test_all_compliant_exit_0`
  `@anchor test agent-harness/tests/test_cli_guardrail.py :: test_violated_exit_1`
  `@anchor test agent-harness/tests/test_cli_guardrail.py :: test_guardrails_dir_missing_exit_2`
- [x] ssot check / register；registry show 只读（agent 元数据读写分属两个
  group 是既成事实，见 §5）。register 的硬业务规则：runtime_status 为 active
  必须至少一条 channel
  `@anchor test agent-harness/tests/test_cli_ssot.py :: test_008_full_check_pass`
  `@anchor test agent-harness/tests/test_cli_ssot.py :: test_register_default_identity`
  `@anchor test agent-harness/tests/test_cli_registry.py :: test_show_json_output_parses`
  `@anchor test agent-harness/tests/test_cli_registry.py :: test_show_unknown_agent_exits_nonzero`
- [x] policy validate/write/show/diff：write 有 cron 闸与身份闸（但无全局
  状态锁——与其他写命令的闸组合不一致，登记 §5）
  `@anchor test agent-harness/tests/test_cli.py :: test_legal_write_prints_and_exits_zero`
  `@anchor test agent-harness/tests/test_cli.py :: test_schema_failure_exit_two_with_field`
  `@anchor test agent-harness/tests/test_cli.py :: test_diff_identical_empty_stdout`
- [x] site check-cron-ids：两份 cron 配置查重
  `@anchor symbol _load_cron_job_ids`
  `@anchor test agent-harness/tests/test_cli.py :: test_check_cron_ids_passes_when_disjoint`
  `@anchor test agent-harness/tests/test_cli.py :: test_check_cron_ids_fails_on_duplicate`
- [x] adjudicate verify：裁决台账机检五级退出码（0 过 / 1 缺 key / 2 缺文件 /
  3 空文件 / 4 无 Round 段 / 5 无 verdict）
  `@anchor symbol _ROUND_HEADING_RE`
  `@anchor symbol _VERDICT_LINE_RE`
  `@anchor test agent-harness/tests/test_cli_adjudicate_verify.py :: test_missing_file_exit_2`
  `@anchor test agent-harness/tests/test_cli_adjudicate_verify.py :: test_empty_file_exit_3`
  `@anchor test agent-harness/tests/test_cli_adjudicate_verify.py :: test_no_round_heading_exit_4`
  `@anchor test agent-harness/tests/test_cli_adjudicate_verify.py :: test_round_but_no_verdict_exit_5`
  `@anchor test agent-harness/tests/test_cli_adjudicate_verify.py :: test_full_ledger_single_round_pass`
- [x] 入口保护：三个可直跑模块（cli / claude_glm_pty / hooks/path_guard）各有
  main 守卫（无守卫时静默退出 0 会驱动 spawn 循环——注释绑定既往事故单）。
  锚点口径：if __name__ 块无独立可锚符号，故 symbol 钉 __main__ 记号、claim 钉
  宿主文件路径（=「该文件存在 __main__ 记号」的机器判据）；人工复核跑
  `grep -c __main__` 于三文件各 ≥1，且 `python3 -c "import harness.cli"` 不产生副作用。
  已知弱点：claude_glm_pty.py 的模块 docstring 另有一处 __main__ 字样，单删守卫
  该锚不红——以人工复核步骤兜底
  `@anchor symbol __main__ :: harness/cli.py`
  `@anchor symbol __main__ :: harness/claude_glm_pty.py`
  `@anchor symbol __main__ :: harness/hooks/path_guard.py`

## 3. path_guard：CC 写入钩子（独立脚本，禁止 import harness）

- [x] 三层判决顺序：write_deny 红线最高（赢过任何 allow）→ 内建 CLI-only 闸
  （wi/handoff 台账只能走 CLI 改，受 wi 锁 flag 门控、可被 policy 豁免）→
  allow 白名单
  `@anchor symbol WRITE_TOOLS`
  `@anchor symbol _cli_only_patterns`
  `@anchor symbol _cli_only_exempt_from_policy`
- [x] 失败容忍：任何内部异常一律放行（钩子故障不能挡所有写入），退出码恒 0，
  拒绝走 stdout JSON、诊断走 stderr 前缀行
- [x] dispatcher 注入链：以包资源定位脚本绝对路径，生成临时 settings 挂
  PreToolUse 钩子并传 policy 路径环境变量
  `@anchor test agent-harness/tests/test_path_hardening_integration.py :: test_narrow_memo_pattern_denies_identity_write`
  `@anchor test agent-harness/tests/test_path_hardening_integration.py :: test_dispatch_spawn_injects_real_settings_path`

## 4. 打包资源

- [x] 三份 path-hardened policy 样例是包资源（可发现、可加载、含收紧的写约束），
  无运行时消费方——纯样例
  `@anchor test agent-harness/tests/test_policy_templates.py :: test_template_is_package_resource`
  `@anchor test agent-harness/tests/test_policy_templates.py :: test_template_loads_as_heartbeat_policy`
  `@anchor test agent-harness/tests/test_policy_templates.py :: test_template_declares_write_constraint`
- [x] 顶层验证画像模板五份被 receipt 旁链消费但**不在打包清单里**（wheel 安装
  后不可达——源码树内可用，登记 §5）
  `@anchor test agent-harness/tests/test_verification_profile.py :: test_loads_multiple`

## 5. 已知两义与债（登记待裁）

`@anchor manual 写命令闸组合不一致 :: 廿个剩余命令里四个有 cron 闸、两个有全局状态锁,policy write 与 ssot register 有闸无锁;闸矩阵该统一未统一`
`@anchor manual 同错不同码 :: registry 缺失在 ssot check 退 2、在 registry show 退 1;同一失败两个退出码`
`@anchor manual 元数据读写分家 :: registry group 只读,写入在 ssot register;按 group 划能力边界会切错`
`@anchor manual 隐含默认路径三处重复 :: ssot check / ssot register / registry show 三处各写一份 <harness_root>/../ssot/registry.yaml 推导，改 env 名或仓库布局只改一处则另两处静默不跟；核对跑 grep -n "registry" harness/cli.py 看该推导出现几处（2026-08-10 实测仍三处，债未还）。**2026-08-10 记一次未遂修复**：wi_20260810_949 的 Engineer 报告称已抽 helper `_default_registry_path` 并配结构断言测试，主身复核发现该源码改动**从未落盘**（cli.py 零改动、helper 不存在），测试在验一个不存在的符号故撤除；债维持未还，留待单独处置`
`@anchor manual 钩子文档引用死符号 :: path_guard 豁免函数的 docstring 引用早已删除的常量名,实际实现是同名函数;交叉引用死链`
`@anchor manual 验证画像模板未打包 :: 顶层模板目录不在打包清单,wheel 场景下 load_profiles 找不到模板;源码树运行掩盖了这条缺口`

## 6. 覆盖声明

本 spec 给只读命令宽容路径、治理命令退出码矩阵、裁决台账机检、path_guard 三层
判决与失败容忍、打包资源边界首次挂机器锚点。agent-registry-extended 存量目录的
现行契约部分由本文与 harness-store-models.md 分别接管。features/CLASSIFICATION.md 记录
契约归属映射；新增或迁移模块时须核对对应契约与锚点，不能以历史模块总数推导完整覆盖。
