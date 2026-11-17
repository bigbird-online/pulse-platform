# SPEC：harness outcome 回执与验收链（含 receipt/extractor 旁链消歧）

> version: `0.1.3`
> status: `reverse_engineered_from_code`
> authority: complete 前置的 outcome 回执校验链**现行实现契约**（本仓侧）
> owner: Pulse（wi_20260804_070 全面 spec 化）
> modified: `2026-10-05`
> 版本史：只看 git log

**取证基础**：初始普查与后续实现核对见 Git 历史；锚点存在性检查不代表业务 UAT 或全部文义已验证。

## 0. 最重要的结构事实：两条「回执」链，词同物异

**outcome 验收链**（本文主体）：wi 收口的回执，canonical 真源在 monorepo 的
verify-work-outcome skill（Markdown+frontmatter，七格判定，schema v2），本仓
cli/machine 侧执行随 Harness 包发布、固定摘要的校验器并绑定当前候选。**receipt/extractor 旁链**：session 行为事实
（BehavioralFact）与 Verdict 的 pydantic 模型层，与 outcome 链零调用关系，
名字撞车纯属历史。本文 §1-3 讲前者，§4 讲后者。

## 1. outcome 回执的方法来源与已安装校验器

七格检查名：baseline / mechanism / identity / primary / counterexample /
regression / consumer。status 四值：accepted / rejected / partial / blocked。
accepted 三硬条件：producer 不等于 verifier 且 level 不是自控级；七格全 pass；
无未决高风险。指纹格式为 sha256 冒号 64 位十六进制。方法定义来自 skill；运行时使用本包的 `outcome_validator.py`，来源版本与摘要记录于相邻
`outcome_validator.provenance.json`。更新方法或 schema 时须同步固定副本、摘要、本文和消费者。

- [x] 校验器是已安装包的已知 sibling；定位时不导入可执行代码，校验固定摘要后才复制到独立快照。
  不从业务仓库寻找可替换的 skill 脚本；旧路径参数只作固定字节兼容核对，不获得执行权。
  `@anchor symbol installed_outcome_validator`
  `@anchor symbol outcome_snapshot`
  `@anchor test agent-harness/tests/test_outcome_snapshot.py :: test_package_validator_is_known_sibling_without_import`
  `@anchor test agent-harness/tests/test_outcome_snapshot.py :: test_legacy_validator_rejects_arbitrary_in_root_code`

## 2. 指纹算法（本仓实现）

- [x] 哈希对象是 wi 的 acceptance_criteria 列表：**不排序**、逐条去首尾空白、
  紧凑 JSON 序列化后取 sha256
  `@anchor symbol _acceptance_fingerprint`
  `@anchor test agent-harness/tests/test_cli.py :: test_complete_rejects_mismatched_acceptance_fingerprint`
- [x] 修订 acceptance authority 不得绕过指纹链：`amend-criteria` 要求 caller 先持有当前
  fingerprint，成功后 event 固化 before/after fingerprint；下一次 `ready-for-uat` 必须冻结修订后的
  新 fingerprint。已经进入 `awaiting_verification` 的候选禁止原地修订，须先合法退回执行态。
  `@anchor symbol acceptance_fingerprint`
  `@anchor test agent-harness/tests/test_acceptance_criteria.py :: test_amend_yaml_file_then_ready_for_uat_freezes_new_fingerprint`
- [x] acceptance 修订的 WI 状态与审计 event 对普通 I/O exception 做 byte rollback；失败不能留下
  "标准已改但 event 缺失" 的半提交。该合同明确不扩张为断电/SIGKILL crash transaction。
  `@anchor symbol save_work_item_with_event`
  `@anchor test agent-harness/tests/test_acceptance_criteria.py :: test_persistence_failure_rolls_back_work_item_and_event_bytes`

## 3. complete 强制链（按执行顺序）

- [x] ready-for-uat 固定送验快照：算指纹写入 wi、状态转 awaiting_verification；
  此处不是人审后的最终冻结，受管 SCL 的 producer 权限由当前执行 frontier 决定；
  machine 层前置四件套（claim/change_type/producer/fingerprint 齐全）
  `@anchor test agent-harness/tests/test_cli.py :: test_ready_for_uat_freezes_candidate_and_changes_state`
- [x] complete 时指纹重算比对，验收标准被改过即拒
  `@anchor test agent-harness/tests/test_cli.py :: test_complete_rejects_acceptance_criteria_changed_after_ready_for_uat`
- [x] 回执按当前 WI 的 canonical 路径读取，拒 symlink；一次捕获回执与 bundle，
  将固定包校验器放在材料之外的独立代码快照，以当前 Python 子进程校验（30 秒超时）。
  通用结果校验接受四种合法状态并绑定 WI/claim/producer/change type/验收指纹；
  complete 的兼容入口另要求 accepted。退出快照前重读回执、bundle 与包校验器，变更即拒绝。
  `@anchor symbol _validate_outcome_receipt_result`
  `@anchor symbol _validate_outcome_receipt`
  `@anchor test agent-harness/tests/test_outcome_snapshot.py :: test_outcome_snapshot_rejects_bundle_drift_after_execution`
  `@anchor test agent-harness/tests/test_outcome_snapshot.py :: test_outcome_snapshot_rejects_installed_validator_drift_after_execution`
  `@anchor test agent-harness/tests/test_cli.py :: test_complete_requires_outcome_receipt_before_state_change`
  `@anchor test agent-harness/tests/test_cli.py :: test_complete_rejects_noncanonical_outcome_receipt_path`
  `@anchor test agent-harness/tests/test_cli.py :: test_complete_rejects_symlinked_outcome_receipt`
  `@anchor test agent-harness/tests/test_cli.py :: test_complete_rejects_non_accepted_receipt_before_state_change`
  `@anchor test agent-harness/tests/test_cli.py :: test_complete_rejects_mismatched_receipt_claim_id`
- [x] machine 层终判：回执校验标志必须为真 + 至少一条证据路径 + 无 pending 且
  责任在身的 handoff（见主链 spec §2）；awaiting_verification 回退 in_progress
  时四个 outcome 字段清零
  `@anchor test agent-harness/tests/test_machine.py :: test_awaiting_to_completed_requires_validated_outcome_receipt`

## 4. receipt / extractor 旁链（session 行为事实）

- [x] Verdict 三值（PASS / FAIL / UNVERIFIABLE）与 Receipt 模型读写
  `@anchor symbol VerdictEnum`
  `@anchor symbol load_profiles`
  `@anchor symbol match_profile`
  `@anchor symbol generate_receipt_id`
  `@anchor symbol save_receipt`
  `@anchor symbol save_verdict`
  `@anchor test agent-harness/tests/test_receipt.py :: test_full_receipt`
  `@anchor test agent-harness/tests/test_receipt_serialization.py :: test_verdict_files_not_counted`
- [x] extractor 从 CC session JSONL 抽取行为事实；机器注入文本按**形状**识别
  （块首 wrapper 标签），不靠标签名单
  `@anchor symbol extract_facts`
  `@anchor symbol is_machine_origin_text`
  `@anchor symbol extract_conversation_text`
  `@anchor symbol merge_llm_enrichment`
  `@anchor test agent-harness/tests/test_extractor.py :: test_real_human_confirmation_still_extracted`
  `@anchor test agent-harness/tests/test_extractor.py :: test_human_quoting_a_tag_midtext_still_extracted`

## 5. 已知两义与债（登记待裁）

`@anchor manual 指纹双实现跨仓漂移风险 :: 本仓 cli 与 skill 侧 completion-claim-guard 各有一份指纹实现，后者多一层字符串化；验收标准含非字符串元素时两侧指纹会发散；不变量=两实现对同一列表必须同值`
`@anchor manual 三项 expected 校验在库外 :: 校验器的 producer/change_type/指纹三项比对做在其命令行主函数里而非库函数里，直接调库的调用方会绕过`
`@anchor manual guard 硬编码仓根 :: completion-claim-guard 默认仓根写死绝对路径，与本仓从 store 根动态推导不一致，非默认根下两道门禁看向不同校验器`
`@anchor manual 截断标志恒假 :: extractor 的 transcript_truncated 恒为 False 而文本抽取确会截断，两者不联动，该字段永远说谎`
`@anchor manual receipt 一词三义 :: outcome 回执(Markdown)、receipt 模型(YAML)、session 回执目录名共用一词，跨文档引用必须带链路限定`

## 6. 覆盖声明

本 spec 给 complete 强制链全部关卡、指纹算法、canonical 路径约束、旁链模型与
extractor 首次挂机器锚点。session-receipt-verifier 存量目录的现行契约部分由
本文 §4 接管。
