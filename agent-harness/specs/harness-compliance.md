# SPEC：harness 合规与规则引擎面 — lint / ssot / gate / compliance / guardrail

> version: `0.1.0`
> status: `reverse_engineered_from_code`
> authority: 账本不变量、SSOT 漂移检查与 guardrail 双引擎的**现行实现契约**
> owner: Pulse（wi_20260804_070 全面 spec 化）
> modified: `2026-08-09`
> 版本史：只看 git log

**取证基础**：2026-08-09 代码普查 + spec-anchor 机器验证全绿。

## 0. 结构事实

guardrail 模块是纯模型层（零判据零拦截），判据在 gate 引擎（GATE 型）与
compliance 引擎（BEHAVIORAL 型）两处；ssot 模块是纯数据层，判据在 ssot 引擎。
汇流点只有 cli 与 scan 两处。三套 verdict 枚举无共同基类且大小写不一致
（gate/compliance 大写、drift 小写）——序列化陷阱，登记于 §5。

## 1. lint：账本八条不变量

- [x] 不变量清单唯一真源是常量元组（INV1..INV8），有元测试反射实现集双向对齐——
  全域最硬的锚点
  `@anchor symbol _ALL_INVARIANTS`
  `@anchor symbol check_ledger`
  `@anchor symbol run_lint`
  `@anchor symbol Finding`
  `@anchor symbol LintResult`
  `@anchor test agent-harness/tests/test_lint.py :: test_finding_key_format`
- [x] 八条判据（object_id 除 INV7 用文件名外均为业务 id）：
  INV1 pending 指向终态或缺失 wi；INV2 pending 超期或收方无 policy/无
  dispatcher；INV3 非终态 wi 陈旧；INV4 wi schema 缺陷；INV5 同 owner 同标题
  重复开单；INV6 accepted 指向终态或缺失 wi；INV7 账本文件未提交/未推送
  （git 不可用则 fail-open 零发现）；INV8 accepted 且责任在身却静默超期
  `@anchor test agent-harness/tests/test_lint.py :: test_inv1_pending_on_completed_wi`
  `@anchor test agent-harness/tests/test_lint.py :: test_inv2_over_sla`
  `@anchor test agent-harness/tests/test_lint.py :: test_inv3_stale_in_progress`
  `@anchor test agent-harness/tests/test_lint.py :: test_inv5_duplicate_owner_title`
  `@anchor test agent-harness/tests/test_lint.py :: test_inv6_accepted_on_missing_wi`
  `@anchor test agent-harness/tests/test_lint.py :: test_not_transferred_is_not_inv8`
  `@anchor test agent-harness/tests/test_lint.py :: test_committed_but_unpushed_is_flagged`
  `@anchor test agent-harness/tests/test_lint.py :: test_git_binary_missing_skips_silently`
- [x] 基线机制：只有基线外的新 key 才算 breach；lint 命令按 new_keys 非空退出 1
  `@anchor symbol load_baseline`
  `@anchor symbol write_baseline`
  `@anchor test agent-harness/tests/test_lint.py :: test_write_then_read_baseline_exit_zero`
  `@anchor test agent-harness/tests/test_scan.py :: test_baseline_whitelisted_finding_is_not_a_breach`
- [x] 阈值：INV3 陈旧 7 天、INV8 静默 3 天、INV2 SLA 7 天、git 超时 10 秒
  `@anchor const _GIT_TIMEOUT_SECONDS = 10`

## 2. ssot：注册表与漂移检查

- [x] 数据层：注册表模型 + 引用完整性 + DFS 环检测；verdict 三值小写，规则=
  空则 pass、任一 critical 则 fail、否则 pass_with_findings
  `@anchor symbol SSOTRegistry`
  `@anchor symbol SSOTEntry`
  `@anchor symbol AgentDecl`
  `@anchor symbol DriftFinding`
  `@anchor symbol DriftVerdict`
  `@anchor symbol compute_verdict`
  `@anchor symbol build_drift_report`
  `@anchor symbol load_registry`
  `@anchor symbol save_registry`
  `@anchor symbol _detect_cycles`
  `@anchor test agent-harness/tests/test_ssot.py :: test_013_verdict_critical_fail`
  `@anchor test agent-harness/tests/test_ssot.py :: test_020_circular_dependency_detected`
- [x] 判据层有效检查集 = S1/S2/S3/S5/S6 + C1/C2（S4 已退休恒空，STRUCTURAL 层
  从未使用——见 §5）；quick 模式跳过 entry 内容检查但 agent 静态与 S6 照跑
  `@anchor symbol check_all`
  `@anchor symbol check_agent`
  `@anchor symbol check_entry`
  `@anchor test agent-harness/tests/test_ssot_engine.py :: test_002_quick_mode_no_c_checks`
  `@anchor test agent-harness/tests/test_ssot_engine.py :: test_bold_table_roles_and_inline_north_star_count_as_anchors`

## 3. guardrail 双引擎

- [x] gate 引擎：file_exists 与 command 两种检查，command 超时按 FAIL 处置
  `@anchor symbol run_gate_check`
  `@anchor symbol GateVerdict`
  `@anchor symbol GateResult`
  `@anchor test agent-harness/tests/test_gate_engine.py :: test_behavioral_type_raises`
  `@anchor test agent-harness/tests/test_gate_engine.py :: test_command_timeout_fail`
- [x] compliance 引擎：pattern_match 与 temporal_check 两种，不支持的类型判
  UNVERIFIABLE 不判 VIOLATED
  `@anchor symbol run_compliance_check`
  `@anchor symbol ComplianceResult`
  `@anchor symbol ComplianceVerdict`
  `@anchor test agent-harness/tests/test_compliance_engine.py :: test_gate_type_skipped`
  `@anchor test agent-harness/tests/test_compliance_engine.py :: test_unsupported_check_type`
- [x] 模型层读写六函数
  `@anchor symbol load_guardrails`
  `@anchor symbol save_gate_result`
  `@anchor symbol save_facts`
  `@anchor symbol load_facts`
  `@anchor symbol save_compliance_verdict`
  `@anchor symbol load_compliance_verdict`

## 4. 下游消费

scan 消费 lint 的新 key 生成账本违例 hit（label 为 lint_ 前缀小写不变量名），
消费 ssot 引擎 quick 模式且只留 critical/major；escalation 档位表为八条不变量
各登记 C 档 label，有从常量元组反射生成的完备性测试钉着（见
harness-escalation.md §2）。

## 5. 已知两义与债（登记待裁）

`@anchor manual 八条写成七条 :: check_ledger 及 lint 命令帮助文本三处自称七条不变量，实现与常量元组是八条；文档口径落后`
`@anchor manual INV8 阈值无入口 :: run_lint 不透传 accepted_stale_days，CLI 与 scan 都改不了 INV8 的三天默认值——有判据无旋钮`
`@anchor manual 三套 verdict 大小写不一 :: gate 与 compliance 落盘大写、drift 落盘小写，同语义不同序列化形态`
`@anchor manual S4 退休测试恒真 :: 十一个测试保留原判据名字而断言全反转为恒空，对空实现零约束，属无效锚点`
`@anchor manual 检查类型裸字符串 :: guardrail 模型的检查类型无枚举约束，合法值只在注释里，拼错静默不跑`
`@anchor manual 结构层枚举无实现 :: CheckLayer 声称三层，全部发现只用静态与内容两层`
`@anchor manual minor 漂移进不了 hit :: 数据层允许 minor 推高 verdict，scan 只取 critical/major，跨模块语义断层`
`@anchor manual 状态集镜像无守护 :: lint 三个状态 frozenset 注释自称镜像 models 的枚举，无测试锁定该镜像`
`@anchor manual verdicts 读写路径靠约定 :: 落盘目录由调用方定，scan 读死路径 verdicts 目录按 agent 分桶，两侧无共享常量`

## 6. 覆盖声明

本 spec 给八条不变量、基线机制、SSOT 漂移分层检查、guardrail 双引擎首次挂机器
锚点。存量 ssot-governance / runtime-guardrail 目录的现行契约部分由本文接管。
