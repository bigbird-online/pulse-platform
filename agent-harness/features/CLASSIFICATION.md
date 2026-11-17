# 存量 spec 分类台账（2026-08-09 · wi_20260804_070 全面 spec 化）

> 判据来源：spec-anchor 装备版 2026-08-09 全量体检（features/ 42 份 + project-specs/ 5 份，
> 读数存 vultr-pulse-space memo 2026-08-09-wi070-kr2b）。**计数口径**：份数与
> 声明/锚点/broken 均按体检 JSON 逐文件复算，分档之和必须等于目录实测总数
> （heartbeat-runtime 实测 7 份 / 54 声明 / 37 锚点 / 1 broken = 4+3 拆分之和）。分类三档：
> **契约**＝仍约束现行行为，authority 归 specs/ 维护期文档；**记事**＝历史设计稿冻结保留，
> 不作为现行契约引用；**退役候选**＝一次性工件，保留仅为可追溯。
>
> **authority 顺序（本台账的核心裁定）**：specs/*.md（显式锚点+机器闸）> 代码 docstring >
> 本目录任何文档。features/ 全目录自本日起冻结：不再更新，也不据以判断现行行为。

## 逐目录分类

| 目录/文件 | 份数 | 声明/锚点/broken | 分类 | 现行契约去向（specs/） |
|---|---|---|---|---|
| core-state-machine | 3 | 14/12/0 | 记事 | harness-main-chain.md（已覆盖；AckState 6 态修正见其 §3） |
| assign-and-flow | 3 | 30/0/0 | 记事 | harness-main-chain.md（已覆盖） |
| dispatcher-context-hygiene | 4 | 10/12/0 | 记事 | harness-scan-hygiene.md（本轮） |
| harness-site-policy | 4 | 17/0/0 | 记事 | harness-policy.md（本轮） |
| agent-self-authored-policy | 3 | 83/17/2 | 记事 | harness-policy.md（本轮） |
| session-receipt-verifier | 3 | 79/9/0 | 记事 | harness-receipt.md（本轮） |
| runtime-guardrail | 3 | 85/8/0 | 记事 | harness-scan-hygiene.md（本轮） |
| heartbeat-runtime（正文 4 份：三件套 + mvs 一份） | 4 | 0/25/1 | 记事 | harness-runtime.md（本轮） |
| heartbeat-runtime/spike-* 3 份（launch-checklist / report-template / report-2026-04-19） | 3 | 54/12/0 | **退役候选**（一次性 spike 工件） | 无（不迁移） |
| heartbeat-policy-path-hardening | 3 | 9/11/2 | 记事 | harness-policy.md（本轮） |
| ssot-governance | 3 | 93/16/1 | 记事 | harness-compliance.md（本轮） |
| agent-registry-extended | 3 | 8/9/1 | 记事 | harness-cli-surface.md / harness-store-models.md（本轮） |
| store-and-cli | 3 | 16/6/1 | 记事 | harness-store-models.md（本轮） |
| project-specs/ 四份 project-*.md | 4 | 0/3/1 | 记事（方向文档，非机器契约） | 无（保留原位） |
| project-specs/proposal-2026-04-18 | 1 | 0/3/0 | 记事（历史提案归档） | 无 |

## 体检查出的 9 条 broken 处置

冻结文档不回改（冻结的意义就是保留当时原貌），逐条记录真相：

| 位置 | 符号 | 定性 |
|---|---|---|
| agent-registry-extended/tasks.md:18 | field_validators | 真删除（设计未落地或已移除） |
| agent-self-authored-policy/requirements.md:276 | self_reported_breach | 改名漂移（现行为 autonomous_self_reported_breach） |
| agent-self-authored-policy/tasks.md:345 | path_in_repo | 改名漂移（现行为 _assert_path_in_repo） |
| heartbeat-policy-path-hardening/requirements.md:188 | gettempdir | **工具误报**（Python 标准库符号被散文反引号推断） |
| heartbeat-policy-path-hardening/tasks.md:49 | allow_with_warn | 真删除 |
| heartbeat-runtime/tasks.md:107 | _classify_hit | 真删除（scan 内部 classifier 已重构） |
| ssot-governance/tasks.md:229 | _compute_verdict | 真删除 |
| store-and-cli/design.md:107 | dump_all | 改名漂移（现行为 yaml.safe_dump_all 调用） |
| project-specs/project-vision.md:98 | file_ops | 改名漂移（现仅存 test_no_file_ops_unverifiable） |
