---
status: backlog
priority: P0（heartbeat-runtime 真实启用前阻塞项之一）
triggered_by: "Amanda 2026-04-20 smoke test（commit ec85c87a6）"
related:
  - features/heartbeat-runtime/
  - features/agent-self-authored-policy/
  - handoffs/ho_20260420_680 (rejected)
tags: [backlog, dispatcher, context-hygiene, handoff-lifecycle]
---

# dispatcher-context-hygiene — backlog 占位（未立项 L2）

## 触发事件（证据）

2026-04-20 Amanda 手工跑了一次 smoke test：`harness scan amanda` 命中 `pending_handoffs.ho_20260419_071`，dispatcher 按 `autonomous_llm` 档 spawn CC 分身。分身技术指标**全绿**（exit=0 / status=success / duration=485s / 无 tool_denied / /wrap 完整 / commit `ec85c87a6` 带 `[cron autonomous]` 前缀），但**行为方向错**：

- 分身认真处理了 `ho_071`，不知道它是**已被覆盖的死 ho**（wi_576 / Amanda 今天人工 session 已多次 commit 覆盖）
- 产物反向覆盖了今天的精简成果（HEARTBEAT.md 81→160 行，加回被删的多 how 少 why 反模式）
- 分身按 prompt 指令 0→100% 推进，而 Amanda 的人格要求是"80%→100% 的增量"
- 分身又对 Ken 发了 `ho_20260420_680` 请审（与 Amanda 人工 session 刚发的 `ho_20260420_047` 重复）

**Amanda 诊断原话**："根因不在分身，在 context 缺口——dispatcher prompt 只给 handoff.input_summary + evidence_path，不含'此 ho 生命周期'（是否已被其他 ho / wi / 人工 commit 覆盖）。"

## 问题定位

dispatcher 当前 prompt 构造（参见 `harness/dispatcher.py` + heartbeat-runtime/design.md §5 Flow-002）包含：
- handoff.input_summary
- handoff.expected_output
- handoff.attachment（如有）
- policy 档位元数据

**缺**：
- 同 wi 的其他 handoff 列表 + 状态
- 源头 wi 的当前 status（是否已 completed / failed）
- `supersedes` / `superseded_by` 关系链（目前 schema 没这字段）
- Git log 里 workspace 文件的近期 commit（是否已被人工 session 推进）

## P0 方向（需立项 L2 后细化）

**硬契约新增**（dispatcher 在 spawn 前必做的前置核对）：
1. **wi 生命周期门卫**：命中 handoff 对应的 wi 若 `status ∈ {completed, failed}` → dispatcher 降档为 `escalate` 或直接 `mechanical` 登记，不 spawn
2. **同 wi 新 handoff 优先级**：命中 handoff 若有 `created_at` 更新的同 wi handoff → 跳过本 ho，spawn 处理新 ho
3. **workspace 活性检测**：命中 handoff 对应的 workspace 若最近 N 分钟内有 commit → 跳过本 ho（人工 session 可能在处理）

**软契约新增**（prompt 注入给分身，供分身二级判断）：
4. **handoff 家族上下文**：prompt 含同 wi 的 handoff 链摘要（包括已 rejected 的）
5. **近期 commit 摘要**：prompt 含 workspace 最近 N 条 commit 一行摘要

## Amanda 的后续配合项（不属本 feature）

- HEARTBEAT §A 是否要加"二级核对 wi 生命周期" — Amanda 在 dispatcher 侧硬契约落地后再评估是否需要人格层兜底
- progress.md 是否要固定写入 "ho_xxx 已废弃" 注记 — Amanda 内政

## 未来立项清单（非本次）

立项时走 120 流程出 L2 三件套。关键决策点预留：
- `HandoffContract` schema 是否加 `supersedes_ho_id` 字段（侵入性；要评估对现有 446 handoff 迁移影响）
- dispatcher 查询 wi 生命周期的边界（只查 harness 自己的 WorkItemStatus？还是也 scan workspace git？）
- "workspace 活性检测"的时间窗（30min？1h？policy.yaml 可配？）
- 降档还是跳过（降 mechanical 还是 pop 到下一 hit）

## 追溯

- commit 证据：`ec85c87a6`（分身产物，已 reject ho_680）
- Amanda smoke test 完整分析：Amanda 2026-04-20 session 对话
- 被占位的优先级：**本 feature 必须在 heartbeat-amanda-scan cron enabled=true 前立项并 P0 交付**，否则真实 cron 启用会反复踩同类坑

