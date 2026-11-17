---
feature_name: "heartbeat-runtime"
document_type: "spike-report-template"
version: "0.0.1"
created: "2026-04-19T11:20:00+0800"
modified: "2026-04-19T11:20:00+0800"
tags: [spike, report, template, heartbeat, p0, reeve]
---

# Reeve Heartbeat Spike Report（模板）

> spike 结束（D7）后填写此模板；置换真实数据后 commit 为 `features/heartbeat-runtime/spike-report-<YYYY-MM-DD>.md`。

---

## Spike Window

- D0（启动）：`<YYYY-MM-DDTHH:MM:SS+0800>`
- D7（关闭）：`<YYYY-MM-DDTHH:MM:SS+0800>`
- 触发频率：`*/15 * * * *`（理论预算 7×96=672 scan 次）
- Agent：reeve（单 Agent 样板）

---

## 触发统计（AC-P0-012 核心数据）

| 指标 | 目标 | 实测 | 状态 |
|------|------|------|------|
| scan 触发次数 | ≥ 600 | | ✅/❌ |
| scan_skipped 次数（锁让路） | ≤ 50 | | |
| clean 比例 | ≥ 85% | | ✅/❌ |
| 有命中次数 | | | |
| ├─ 机械档 dispatch_event | | | |
| ├─ 受限 LLM 档 autonomous spawn | | | |
| │   ├─ status=success | | | |
| │   ├─ status=tool_denied | 0 | | ✅/❌ |
| │   ├─ status=wrap_missing | ≤ 5% spawn 数 | | |
| │   ├─ status=timeout | ≤ 5% spawn 数 | | |
| │   ├─ status=deferred | | | |
| │   └─ status=error | 0（告警） | | |
| └─ 升级档 escalation_event | | | |
|     ├─ push_immediately 实际推送 | | | |
|     └─ aggregate_daily 合并推送（7 次/周） | | | |

---

## S1 自动化量化（核心价值验证 AC-P0-005）

- **Reeve 分身实际处置的 pending handoff 数**：`<N>`
  - 目标：≥ 5（确认 policy 不过度保守）
  - 状态：✅ / ❌
- **其中 ack/reply 具体类型细分**：
  - Review-邀请 ack：`<n>`
  - 反向 handoff 生成：`<n>`
  - Reject（条件不成熟）：`<n>`
- **commit 带 `[cron autonomous]` 标记的次数**：`<n>`
- **Reeve 下次人工 session 启动时发现的"已被分身处置"条数**：`<n>`（若 Reeve 报告混乱，说明 /wrap memo 写得不够清）

---

## 安全验证（AC-P0-004）

| 验证项 | 目标 | 实测 | 状态 |
|-------|------|------|------|
| Tool allow-list 违规尝试次数 | 0 | | ✅/❌ |
| 越界 Write 实际落盘次数 | 0 | | ✅/❌ |
| git push 越界尝试 | 0 | | ✅/❌ |
| 被拒工具具体列表（如有） | — | | |

---

## 成本（AC-P0-008）

| 指标 | baseline（spike 前 7 日） | spike 期（D0-D7） | 净增 | 目标净增 ≤ $5 | 状态 |
|------|-----|-----|-----|-------|------|
| Reeve CC 配额消耗 | $ | $ | $ | $5 | ✅/❌ |
| 熔断触发次数 | — | | — | 0 | ✅/❌ |

---

## 锁协议（AC-P0-007）

- `scan_skipped` 事件总数：`<n>`
- `deferred_autonomous_session` 事件总数：`<n>`
- 锁相关故障（死锁 / 孤儿锁）：`<n>`（目标 0）

---

## S3 介入频次对比（proposal 产品价值函数）

| 指标 | spike 前 7 日 baseline | spike 期 D0-D7 | 变化 |
|------|----|----|-----|
| 大鸟给 Reeve 口头/nudge 告知待办次数 | `<n>` | `<n>` | % |
| 大鸟主动开 Reeve session 处置 pending 次数 | `<n>` | `<n>` | % |
| 大鸟被 TG push 打扰次数 | N/A | `<n>` | |

**解读**：

- 预期口头 nudge 次数显著下降（autonomous_llm 档承接）
- push 打扰频率不爆炸（升级档 aggregate 起效）

---

## 异常事件详细记录

### Incident 1：`<描述>`
- 时间：`<ts>`
- 事件类型：`<event_type>`
- 影响：
- 根因：
- 修复：
- 事件 id：

### Incident 2：（如有）

---

## Policy 调整建议

基于 spike 数据，提议以下 `.harness/policies/reeve.yaml` 调整（待大鸟审批）：

- [ ] 频率：`*/15` → `<建议值>`（原因：）
- [ ] per_action 分类调整：
  - `<hit_type>.<match_label>` 从 `<before>` → `<after>`（原因：）
- [ ] autonomous_llm_config：
  - allowed_tools 增减：
  - max_duration_seconds 调整：
- [ ] escalation：
  - push_immediately 增减：
  - aggregate_daily 增减：

---

## Sign-off 建议

### P0 达标判定

- AC-P0-001 ~ AC-P0-011 中的集成/实测项：
  - `<填写各项 ✅/❌>`
- **AC-P0-012（spike 7 指标）**：`<✅达标 / ❌未达标>`
- 无 tool allow-list 违规：`<✅/❌>`

### 下一步决策

- [ ] **进入 P1**：扩展到其他 Agent + policy schema 通用化（需 Amanda 配方回路）
- [ ] **延长 spike**：发现问题需再跑一周调整 policy
- [ ] **回滚**：达标失败且无法通过 policy 调整解决，重评架构

---

## Attachments

- Raw event JSONL 归档：`<path>`
- TG push 记录截图：`<path>`
- Spike 启动备忘：`<path>`
