---
name: Pulse Workspace Predecessor Disposition
version: 1.0.0
modified: 2026-08-23
authority: wi_20260823_941 AC7 closeout evidence
---

# Pulse Workspace Predecessor Disposition

取证时点：2026-08-23。目标是终止旧主线的权威资格，同时保留有用意图；不是把旧 commit 冒充已 merge。

| predecessor | current durable fact | useful intent absorbed by this candidate | disposition |
| --- | --- | --- | --- |
| [PULSE PR #122](https://github.com/bigbird-online/PULSE/pull/122) | `CLOSED`、未 merge；head `6ea8a775727e22894002ac7283eba2299557926f`，其两条 commit 均不是 `origin/main` ancestor | 恢复 Pulse 的具体责任与 PULSE 经营轮盘；本候选保留这些意图，但收敛为一个 `pulse-space` 真身、一个 principal、四个 surface 与双根启动 | **terminated + re-derived**；不 reopen、不 cherry-pick，避免旧 PROGRESS/STATE 或四套 identity 成为第二权威 |
| [PULSE PR #131](https://github.com/bigbird-online/PULSE/pull/131) | `CLOSED`、未 merge；stacked base 为 PR #122，head `7bca46d6f364e42ad890feb037cf68404f19ab2a` 不是 `origin/main` ancestor | resident entry 不绕 Actions、只有 ChatGPT 使用 actions_bridge、0-step infrastructure failure 与 executed failure 分流；本候选在 surface registry、workflow、resource tests 与 bootstrap digest 中重新实现 | **terminated + re-derived**；不恢复 stacked branch，不让旧 workflow 权限/runner 方案成为第二权威 |
| `wi_20260821_001` | canonical Harness 根不存在该 WorkItem 文件；当前 WI source 明确 `supersedes=wi_20260821_001` | 一个 Pulse workspace 与四 surface / Actions 边界被本 WI 的 fingerprinted AC 全量接管 | **superseded / no live object**；不补造历史 WI，不维护并行状态 |

最终权威只剩：当前 `wi_20260823_941` acceptance fingerprint、当前候选 PR/head、合并后的 default SHA 与 canonical Outcome receipt。旧 PR commit 可作历史审计输入，不能作为 bootstrap、UAT 或交付证据。
