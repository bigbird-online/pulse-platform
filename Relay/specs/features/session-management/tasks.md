---
feature_name: "session-management"
version: "0.2.0"
created: "2026-04-07T10:45:00+0800"
modified: "2026-06-12T13:15:00+0800"
changelog:
  - "v0.2.0: 150 同步——新增 P0-003 跨渠道 session 访问任务（已完成）"
  - "v0.1.0: 150 对齐——标注核心已在 pool/telegram 实现；本 feature 以验证为主"
upstream_dependency: ["design.md", "requirements.md"]
tags: [tasks, l2-spec, session, relay-m3]
---

# session-management - 任务拆分文档

> **任务概述**：验证 session 持久化和恢复功能的正确性。核心逻辑已在 cc-process-pool 和 telegram-channel 中实现，本 feature 主要是验证和补充测试。
>
> **2026-05-02**：`pool.buildArgs` `--session-id`/`--resume`、`getStatus().sessionId`、`telegram` dead→`resume:true` 等已在主链路运行；下表为**文档对齐**状态。

---

## 1️⃣ 任务拆分

| 任务 ID | 任务名称 | 预估工时 | 依赖任务 | 状态 |
|---------|---------|---------|---------|------|
| P0-001 | 补充 session resume 集成测试 | 2h | cc-process-pool + telegram-channel | 可选（增量） |
| P0-002 | 验证 getStatus 包含 sessionId | 1h | P0-001 | ✅ 已由 `pool`/`getStatus` 行为满足 |
| P0-003 | 跨渠道 session 访问（userHome） | 1h | — | ✅ 已完成 `9168e66` |

---

## 2️⃣ 任务详情

### P0-001：补充 session resume 集成测试

**任务描述**：
- 基于 design.md 的数据流，验证 spawn → kill → spawn(resume) 后 CC 保留上下文
- 测试 telegram.ts handler 在 proc.state === 'dead' 时正确传递 resume:true

**验证标准**：
- [x] **Unit Test-001**：spawn 后 kill 再 spawn 时，SpawnConfig.resume === true
- [ ] **Integration Test-001**：真实 CC 进程 resume 后回复引用历史对话（需 CC CLI，手动验证）

### P0-002：验证 getStatus 包含 sessionId

**验证标准**：
- [x] **Unit Test-002**：pool.getStatus() 返回的条目包含 sessionId 字段（已覆盖，确认）

### P0-003：跨渠道 session 访问（userHome）

**任务描述**：
- 在 `AgentConfig` 和 `SpawnConfig` 中新增 `userHome?: string` 字段
- `pool.ts spawnProcess` 中为非 linuxUser agent 注入 `env['HOME'] = userHome`
- 所有 `tenantHome` 计算处增加 `userHome` fallback
- feishu/telegram/provision 管道透传 `userHome`

**验证标准**：
- [x] **Unit Test**：778 tests passed, 0 failed（全量回归通过）
- [ ] **手动验证**：飞书 bot 对话 → CC 终端 `claude -r` 可发现 session（待部署验证）

**commit**：`9168e66 feat(relay): add userHome for cross-channel session access + fix pendingAsk test leak`

**附带修复**：pendingAsk 模块级状态泄露导致 flaky test（`pendingAsksByPoolKey`/`pendingAsksByToolUseId` 未在 `beforeEach` 中清理）

---

## 3️⃣ 完成标准

- 现有测试已覆盖 session 相关逻辑（pool.test.ts Unit Test-008 验证 --resume 参数）
- 本 feature 的增量测试确认 resume 端到端正确

---

## 4️⃣ 分析结论

session-management 的核心逻辑已在 M1/M2 中实现：
- `pool.ts buildArgs()` 传 `--session-id`
- `pool.ts handleCrashRestart()` 自动 `--resume`
- `telegram.ts` handler 检测 `state === 'dead'` 时 `resume: true`
- `pool.ts getStatus()` 返回 sessionId

**本 feature 实质是验证性任务，无新代码生产**。P0-003 为唯一增量实现。
