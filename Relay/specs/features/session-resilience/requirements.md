---
feature_name: "session-resilience"
version: "0.0.1"
created: "2026-04-09T11:30:00+0800"
modified: "2026-04-09T11:30:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md"]
tags: [requirements, l2-spec, session, lifecycle, relay]
---

# session-resilience - 功能需求文档

> **功能概述**：确保 Relay 的 session 在任何中断场景下都能自动恢复，且用户始终得到可见反馈——消除 Relay 通信中的静默失败。
>
> **关联文档**：
> - L1项目愿景：`project-specs/project-vision.md`
> - L1项目架构：`project-specs/project-architecture.md`（场景4：会话连续性）
> - L2技术设计：`features/session-resilience/design.md`
> - L2任务拆分：`features/session-resilience/tasks.md`

---

## 1️⃣ 用户故事（User Stories）

**故事1：重启后无缝恢复**
- **作为**：通过 TG 与 OA 对话的租户
- **我希望**：Relay 重启后，我发消息能继续之前的对话，不需要任何额外操作
- **以便**：对话不会因为运维操作而中断，我不需要关心底层系统状态
- **优先级**：P0

**故事2：中断时收到通知**
- **作为**：正在与 OA 对话的租户
- **我希望**：当对话因任何原因中断时（崩溃、轮次耗尽、重启），我能在 TG 中看到原因说明
- **以便**：我知道发生了什么，不会在等待一个永远不来的回复
- **优先级**：P0

**故事3：自毁循环不再发生**
- **作为**：平台管理员（大鸟）
- **我希望**：当 OA 执行了导致 Relay 重启的命令时，系统能自动打破循环，不再反复重启
- **以便**：不需要人工介入修复反复重启的问题
- **优先级**：P0

**故事4：Session 由我控制**
- **作为**：通过 TG 与 OA 对话的租户
- **我希望**：只有我主动执行 `/new` 时才开始新对话，其他所有中断都自动恢复到之前的上下文
- **以便**：我的对话历史不会意外丢失
- **优先级**：P1

---

## 2️⃣ 功能需求（Functional Requirements）

### P0功能（MVP必需）

**FR-P0-001：chatId 映射启动时初始化**
- **描述**：Relay 启动时，从 relay.json 配置中提取所有 user-agent 的 chatId 映射，预填充 `poolKeyToChatId` Map。不再依赖用户发送首条消息来建立映射。
- **来源**：故事1
- **示例**：Relay 重启后，Ken 的 CC 进程产生 assistant 消息 → 通过预填充的 chatId 映射直接路由到 TG → 用户无感知。

**FR-P0-002：Session 状态持久化**
- **描述**：Relay 在运行时将活跃 session 的状态（poolKey, agentId, sessionId, turnCount, maxTurns, lastActivity, status）持久化到 `~/.relay/state.json`。采用防抖写入（2s），关闭时立即 flush。文件缺失或损坏时回退到空状态。
- **来源**：故事1
- **示例**：Relay 运行中 state.json 实时反映 12 个 OA 的 session 状态；硬重启后读取 state.json 恢复 turn 计数器。

**FR-P0-003：Session 终止事件与通知**
- **描述**：Pool 在以下场景 emit `session-end` 事件，TG 层收到后向用户发送通知：
  - **turns 耗尽**（exit code 0，turns 已满）：通知用户，说明发新消息可恢复
  - **CC 崩溃不可恢复**（重启次数超限）：通知用户，说明发消息可重试
  - **Relay 关闭**：关闭前向所有活跃 session 的 chatId 发送重启通知
  - **idle 回收**：不通知（下次消息自动 resume，用户无感）
  - **CC 崩溃自动恢复成功**：通知用户已自动恢复
- **来源**：故事2
- **示例**：Ken 的 turns 用完 → TG 收到 "⚠️ Ken 已达到轮次上限 (100/100)，发新消息自动恢复。" → 用户发消息 → Ken resume，counter 重置。

**FR-P0-004：自重启检测与循环打断**
- **描述**：Relay 关闭时写入 `~/.relay/.restart-marker`（时间戳）。启动时检测：若距上次关闭 < 10s，判定为快速重启。对之前处于 busy 状态的 session，在 CC resume 后注入系统警告消息，告知 CC 不要再次执行重启命令。
- **来源**：故事3
- **示例**：Ken 执行 `launchctl kickstart -k` → Relay 重启 → 检测到快速重启 → Ken resume 时收到 "Relay 刚重启过，请勿再次执行重启命令" → 循环打断。

### P1功能（增强功能）

**FR-P1-001：Session 生命周期显式控制**
- **描述**：只有 `/new` 命令能终结 session 身份（更换 sessionId）。其他所有中断（idle、crash、turns 耗尽、Relay 重启）只杀进程，不改变 sessionId，下次消息自动 resume。
- **来源**：故事4
- **示例**：用户发 `/new` → Ken 获得新 sessionId → 全新对话。但 idle 30min 后被 cleanup，再发消息 → 同一 sessionId resume → 上下文保留。

### P2功能（锦上添花）

**FR-P2-001：Turn 计数器跨重启恢复**
- **描述**：Relay 重启后，从 state.json 恢复每个 session 的 turnCount，初始化 turnCounters Map。避免重启后 turn 计数器归零，导致 80% 预警失效。
- **来源**：故事1
- **示例**：Ken 已用 75 turns → Relay 重启 → resume 后 turnCounter 从 75 继续计数 → 到 80 时触发预警。

---

## 3️⃣ 验收标准（Acceptance Criteria）

### P0功能验收标准

**AC-P0-001：重启后响应路由正常**
- **条件**：Relay 重启后，CC 进程通过 `--resume` 恢复并产生 assistant 消息
- **预期结果**：assistant 消息在 ≤2s 内路由到正确的 TG chatId（不需要用户先发消息）
- **验证方式**：集成测试 — mock pool emit response，验证 bot.api.sendMessage 被调用且 chatId 正确

**AC-P0-002：state.json 持久化正确**
- **条件**：Relay 运行中有活跃 session 交互
- **预期结果**：state.json 在最后一次状态变更后 ≤3s 内写入磁盘，包含正确的 poolKey、turnCount、status 字段
- **验证方式**：单元测试 — 调用 updateSession 后等待 flush，读取文件验证内容

**AC-P0-003：Turns 耗尽通知**
- **条件**：CC 进程因 max-turns 退出（exit code 0）
- **预期结果**：用户在 TG 中收到包含当前轮次数和最大轮次数的通知消息
- **验证方式**：单元测试 — mock pool emit session-end(turns-exhausted)，验证 sendMessage 被调用且消息包含数字

**AC-P0-004：Relay 关闭通知**
- **条件**：Relay 收到 SIGTERM
- **预期结果**：所有有活跃 session 的 TG chatId 在 ≤3s 内收到重启通知
- **验证方式**：集成测试 — 触发 shutdown，验证每个活跃 chatId 收到通知

**AC-P0-005：自重启循环打断**
- **条件**：Relay 在 10s 内连续重启 2 次
- **预期结果**：第 2 次启动时检测到快速重启，对之前 busy 的 session 注入警告消息。CC 不再执行重启命令。
- **验证方式**：单元测试 — 写入 restart-marker，模拟启动，验证警告注入逻辑

### P1功能验收标准

**AC-P1-001：Session 身份仅 /new 可终结**
- **条件**：CC 进程因 idle/crash/turns 退出后用户再次发消息
- **预期结果**：使用相同 sessionId 通过 `--resume` 恢复，对话上下文完整保留
- **验证方式**：端到端测试 — 模拟 idle 超时后发消息，验证 spawn args 包含原 sessionId

---

## 4️⃣ 非功能需求（Non-Functional Requirements）

### 性能需求

- **状态写入延迟**：state.json 写入不阻塞消息处理（异步防抖写入）
- **启动时间影响**：读取 state.json 增加启动时间 ≤100ms
- **shutdown 通知**：所有 TG 通知在 3s 内完成发送

### 安全需求

- **state.json 不含敏感信息**：不存储 botToken、chatId（这些在 relay.json 中）
- **原子写入**：write-temp-then-rename 防止中途断电导致文件损坏

### 可用性需求

- **容错降级**：state.json 缺失或损坏时，回退到当前懒加载行为，不影响核心功能
- **重启恢复时间**：从 SIGTERM 到新实例就绪 ≤5s（当前约 3s，允许通知增加 2s）

### 可维护性需求

- **测试覆盖率**：state.ts ≥ 80%，新增 pool events ≥ 80%
- **单文件限制**：state.ts ≤ 150 行

---

## 5️⃣ 约束条件（Constraints）

### 技术约束

- **技术栈**：TypeScript 5.x, Node.js ≥ 18, vitest（遵循 project-standards.md）
- **数据存储**：JSON 文件（遵循 project-architecture.md 初期方案）
- **文件路径**：`~/.relay/state.json`（遵循现有 `~/.relay/` 约定）

### 资源约束

- **开发资源**：单人（Ken agent）开发
- **现有代码**：85 个测试必须保持通过

---

## 6️⃣ 排除范围（Out of Scope）

- ❌ 不做 SQLite 迁移（state.json 足够，后续版本再考虑）
- ❌ 不做跨机器 session 同步（单机部署场景）
- ❌ 不做拦截 CC 危险命令（采用轻量的标记+警告方案）
- ❌ 不做自动 eager-spawn（重启后仍为 lazy-spawn，但 chatId 映射预填充保证响应路由正常）
