# WI568 UAT：0818 后终态 WI ComputeCost 审查

审计日期：2026-08-28
状态复核：2026-09-01

执行 WI：`wi_20260820_568`

## 1. 边界与当前结论

“0818 后”固定为 **WI identity date ≥ 2026-08-18**，终态包括 `completed`、`failed`、`cancelled`、`superseded`、`migrated`。审计 cohort 固定在 Stats canonical authority 对齐的 SCL tree（revision `c40f0fba8f118ceb35fc0e99e68706db092afe70`），共有 **14 张**；后续新终态不追溯加入该冻结集合：

- completed：5
- failed：3
- cancelled：3
- superseded：2
- migrated：1

此前从 WI568 stale linked projection 得出的 9 张清单无效；遗漏的五张为 `wi_20260823_719`、`wi_20260823_941`、`wi_20260824_582`、`wi_20260825_043`、`wi_20260825_439`。

### Executive result

- **ComputeCost 缺陷修复 UAT：独立复审通过并已合入默认分支**。Review `5048244924` 的 3 个 P1 已关闭；PR #298 re-review 发现的 query-relative short alias collision 已增加失败回归并改为 canonical-inventory resolution，最终以 `46804ee27d28b8770f0e44abebfe1445dc044ca1` 合入。
- **唯一归属的局部用量（observed, partial）**：742,914 resource Token；重新生成时 OpenRouter public catalog 参考价 USD **0.147896702**。
- **歧义用量（excluded）**：WI136/WI938 先前共享的 89,786,412 Token、9 events、USD 45.57484 当前价参考量现在在估值前成为 `join_conflict`，不归给任一 WI，也不进入组合总数。
- **实际现金（unavailable）**：14/14 没有可归到单 WI 的 subscription/API actual-cash 证据。
- **人类注意力（unavailable）**：没有完整计时/可信 telemetry 与用户费率，不计算 HAU→时间→美元。
- **业务价值与 ROI（unavailable）**：14/14 没有 ComputeCost 可消费的 downstream business-value receipt，也没有获授权的 replacement/scenario 假设。
- **仍未完整绿色**：PR #298 的代码、CI 与独立复审门已关闭；WI938、WI941 的 exact-candidate human review artifact set 仍 stale，质量 projection 保持 unavailable。
- **Stats 当前状态**：`ownermac dirty` 结论已撤回。latest canonical `d5638cc4fe740f38990afe34a103369dd275955d` 为 `authority_status=verified`、`coverage=complete`，三面均 clean/at-target；本轮 UAT 报告使用该 exact inventory。

## 2. 逐 WI 结果

| WI | 终态 | 产出 / 质量 | ComputeCost 结果 | ROI |
|---|---|---|---|---|
| `wi_20260818_075` | cancelled | 建议归档，无 accepted delivery | 182,041 Token；当前价参考 USD 0.1156792；coverage partial | unavailable |
| `wi_20260818_288` | cancelled | 数据同步单归档 | producer missing | unavailable |
| `wi_20260819_587` | failed | 被 portfolio 决策取代 | `join_conflict`；Token unavailable，不再显示伪零值 | unavailable |
| `wi_20260819_960` | failed | 被 portfolio 决策取代 | producer missing | unavailable |
| `wi_20260820_126` | completed | Execution Plan Adoption 已真消费；Outcome accepted | producer missing | unavailable |
| `wi_20260820_136` | completed | Epoch Cutover accepted | multi-WI seed `join_conflict`；Token/价格 unavailable | unavailable |
| `wi_20260820_272` | completed | SCL Phase 2 accepted；human review approved；9 rollover、7 recovery、3 segments | canonical-window usage unavailable | unavailable |
| `wi_20260821_742` | cancelled | Workspace recipe pilot 归档 | producer missing | unavailable |
| `wi_20260822_938` | completed | Pi runtime 三端 UAT Outcome accepted | multi-WI seed `join_conflict`；measurement review artifact stale | unavailable |
| `wi_20260823_719` | superseded | 容量告警由 WI825 唯一承接 | `join_conflict`，无可归属 event | unavailable |
| `wi_20260823_941` | completed | 四 surface workspace/双根体系 Outcome accepted | producer missing；measurement review artifact stale | unavailable |
| `wi_20260824_582` | migrated | malformed direct-owner WI 迁移至 WI825 | producer missing；`migrated` 查询已恢复 | unavailable |
| `wi_20260825_043` | failed | 人审判定入口配方不可用且无 salvage value | 560,873 Token；重新生成时参考 USD 0.032217502；actual cash unavailable | unavailable |
| `wi_20260825_439` | superseded | 容量告警由 WI825 唯一承接 | producer missing | unavailable |

## 3. 已修缺陷

### 3.1 多 WI Session 重复计费

修复前，WI136 与 WI938 各自完整吸收同一 opaque root 的 9 events，逐 WI 求和把参考量重复增加 USD 45.57484。

Review `5048244924` 随后证明首轮修复只扫描 full WI identity，且没有在 index+rollout 联合证据与完整 root/delegation component 上传播歧义；因此首轮“已关闭缺陷”结论被撤回。

follow-up 修复后：

- full（`wi_YYYYMMDD_NNN`）与 compact（`wiYYYYMMDDNNN`）seed 直接规范化；short（`WI938`）只在 canonical inventory 唯一，或同一文本由一个 full/compact identity 明确消歧时接受；重复 suffix 无法消歧则让所有候选查询 fail closed；
- Codex session index 与所有 user-authored rollout prompt 先按 session 合并，再按 metadata parent 与 delegation edge 扩展完整 component；
- component 任一成员引用其他 WI，整组 session/root usage 均排除，不能由 closure 或共享 `root_run_ref` 重新引入；
- Claude Code 在 root scope 使用同一 alias 规范化规则；
- 当前实现**不消费** Task/epoch partition evidence，因此不存在多 WI component 例外；
- cohort 返回 `join_conflict`、0 joined event、Token unavailable，OpenRouter pricing 在估值前 fail closed。

### 3.2 unknown-as-zero

多个 producer 都未观测时，不再因为状态不同而合成为 `partial`。无 observed partition 时按 `join_conflict → stale → producer_unreachable → producer_missing → not_applicable` 保留最强 typed gap；顶层 Token 与 cost 为 unavailable。

### 3.3 `migrated` 终态

`load_wi_window()` 现把 `migrated` 纳入 closed attribution window。WI582 已从 CLI 错误 `Work Item has no closed attribution window` 变为正常 unknown-preserving report。

## 4. 不可由代码代替的门与运行状态

### WI938 / WI941 stale review

两张 WI 的 review candidate fingerprint 本身存在，但当前 artifact-set digest 与人审时不同。WI938 的 artifact refs 全部存在，当前 digest 与 reviewed digest 不一致；这是候选在 review 后变化，不是“文件找不到”。安全修复只能是：

1. 用户对当前 exact candidate 重新 review；或
2. 提供真实 outcome-validation 与 human-review-projection authority runs，形成 digest-bound terminal reconciliation。

不得复用普通 CI run 冒充 human-review authority，也不得放宽 measurement gate。

### Stats read model

本轮重新生成前只读复核 canonical `as_of=d5638cc4fe740f38990afe34a103369dd275955d`：`authority_status=verified`、`record_status=found`、`coverage=complete`，cloud-linux-b、ownermac、tcloud-node2 均 clean/at-target。相关 ownermac endpoint 是 `/Users/owner/.local/share/pulse/service-repositories/collaboratewithai/harness/ownermac`，不是人类工作根 `/Users/owner/Coevolution`；旧 `ownermac dirty` 归因已撤回。

## 5. 价格与 ROI 边界

- USD 数字来自 UAT 时点的 OpenRouter public models catalog，是 reference scenario，不是执行时历史价格或 provider cash。
- 组合中只加总唯一归属且有 request evidence 的 WI075 与 WI043。
- ambiguous、producer missing、stale、join conflict 均不补 0。
- business ROI 与 replacement-cost ROI 继续为 unavailable。

## 6. 验证

- 首轮回归：Codex/Claude Code multi-WI session、mixed unobserved gap、`migrated` terminal、join-conflict zero cohort。
- Review `5048244924` follow-up 回归：short/compact alias、index target + rollout other-WI、unique parent + ambiguous child shared-root component；均先红后绿。
- PR #298 re-review follow-up：两个 closed、窗口重叠、同 suffix WI + 单一 short-only provider session；旧 head 可同时观察，新实现两个查询均 `join_conflict`、0 event。
- ComputeCost：184 tests PASS；compileall PASS；ComputeCost spec drift guard PASS。
- 真实 cross-WI UAT：WI136/WI938 均为 root-component `join_conflict`、0 event、Token unavailable、pricing `join_conflict`；diagnostics 明确 `partition_evidence_consumed=false`。
- 组合 Token 不变：WI075 + WI043 为 742,914 Token；重新生成时 OpenRouter 参考价为 USD 0.147896702。价格变化仅反映当前 public catalog，不是历史 actual cash；WI582 可生成 migrated 报告。

## 7. 证据文件

- `inventory.json`：14 张 canonical terminal WI。
- `measurements/*.json`：可安全生成的 Harness projections。
- `measurement-gaps.json`：WI938/WI941/WI043 stale review 门。
- `reports/*.json`：14 张 follow-up 实现重新生成的 ComputeCost report。
- `rollup.json`：unknown-preserving 组合汇总及 review follow-up 状态。
- `overlap-audit.json`：跨 WI 重复归因、三项 P1 与 component-scope 修复证据。
