---
feature_name: "token-fallback"
version: "0.0.1"
created: "2026-04-13T17:00:00+0800"
modified: "2026-04-13T17:00:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md"]
tags: [requirements, l2-spec, auth, fallback, relay]
---

# token-fallback - 功能需求文档

> **功能概述**：支持双 OAuth Token（Max 主力 + Pro 备用），当主 Token 遇到限流或认证失败时自动切换到备用 Token，确保 CC 进程不因单一订阅的配额限制而中断服务。
>
> **关联文档**：
> - L1项目愿景：`project-specs/project-vision.md`
> - L1项目架构：`project-specs/project-architecture.md`
> - L2技术设计：`features/token-fallback/design.md`
> - L2任务拆分：`features/token-fallback/tasks.md`

---

## 1️⃣ 用户故事（User Stories）

**故事1：限流时不中断**
- **作为**：通过 TG 与 OA 对话的用户
- **我希望**：当 Max 订阅限流时，系统自动切换到 Pro 订阅继续服务
- **以便**：我的对话不会因为单一订阅的配额限制而中断
- **优先级**：P0

**故事2：Token 状态可见**
- **作为**：平台管理员（大鸟）
- **我希望**：能在日志中看到当前使用的是哪个 Token（primary/fallback），以及切换原因
- **以便**：我能判断订阅的使用情况和切换频率
- **优先级**：P1

---

## 2️⃣ 功能需求（Functional Requirements）

### P0功能（MVP必需）

**FR-P0-001：双 Token 环境变量**
- **描述**：未声明凭证源文件的实例从环境变量读取 `CLAUDE_CODE_OAUTH_TOKEN`（primary）和 `CLAUDE_CODE_OAUTH_TOKEN_FALLBACK`（fallback），spawn 时通过 env 注入。声明 `RELAY_CC_OAUTH_ENV_FILE` 的实例以该文件中的 primary token 为唯一来源，每次 spawn 重读；禁用继承的 token/provider fallback，不创建 bot 凭证副本。完整契约见 `../../relay-runtime-pool.md` 的“CC 订阅凭证唯一来源”。
- **来源**：故事1

**FR-P0-002：限流/认证失败检测**
- **描述**：通过 CC 子进程的 stderr 输出检测以下信号，判定为需要 Token 切换：
  - `rate limit`（限流）
  - `overloaded`（服务过载）
  - `401`（认证失败/Token 过期）
  - `insufficient_quota`（配额不足）
- **来源**：故事1

**FR-P0-003：Fallback 切换机制**
- **描述**：检测到限流/认证失败信号后：
  1. Kill 当前 CC 进程
  2. 使用 fallback Token 重新 spawn（resume 同一 sessionId）
  3. 此次切换不消耗 restart quota（这不是 crash，是有意切换）
  4. 若 fallback 也失败，走正常 crash restart 流程
- **来源**：故事1

**FR-P0-004：单 Token 兼容**
- **描述**：当 `CLAUDE_CODE_OAUTH_TOKEN_FALLBACK` 未设置时，行为与当前完全一致，不做任何 fallback 尝试。
- **来源**：故事1

### P1功能（增强功能）

**FR-P1-001：Token 切换日志**
- **描述**：每次 Token 切换时，输出结构化日志，包含 tenantId、切换方向（primary→fallback / fallback→primary）、触发原因。
- **来源**：故事2

**FR-P1-002：Primary 自动恢复**
- **描述**：切换到 fallback 后，下次新 spawn（非 resume）优先尝试 primary。避免 fallback 变成永久默认。
- **来源**：故事2

---

## 3️⃣ 验收标准（Acceptance Criteria）

### P0功能验收标准

**AC-P0-001：Fallback 切换成功**
- **条件**：CC stderr 输出包含 `rate limit` 关键词
- **预期结果**：进程在 ≤5s 内用 fallback Token 重新 spawn，sessionId 不变
- **验证方式**：单元测试 — mock stderr emit rate limit，验证 respawn env 包含 fallback Token

**AC-P0-002：不消耗 restart quota**
- **条件**：因 Token 切换而 respawn
- **预期结果**：restartCount 不递增
- **验证方式**：单元测试 — 触发 fallback，检查 restartTracking 计数不变

**AC-P0-003：单 Token 不影响**
- **条件**：`CLAUDE_CODE_OAUTH_TOKEN_FALLBACK` 未设置
- **预期结果**：现有行为完全不变，无 fallback 尝试
- **验证方式**：单元测试 — 不设 fallback env，触发 rate limit，验证走正常 crash 流程

**AC-P0-004：Fallback 也失败**
- **条件**：Primary 和 fallback 均失败
- **预期结果**：走正常 crash restart 流程，消耗 restart quota
- **验证方式**：单元测试 — 两次均触发 rate limit，验证 restartCount 递增

---

## 4️⃣ 非功能需求（Non-Functional Requirements）

### 性能需求

- **切换延迟**：从检测到限流信号到 fallback 进程 ready ≤5s
- **零开销**：未触发 fallback 时，不增加任何运行时开销

### 安全需求

- **Token 不泄露**：日志中不输出 Token 全文，仅输出前 8 字符用于区分

### 可维护性需求

- **测试覆盖率**：新增逻辑 ≥ 80%
- **现有测试**：全部保持通过

---

## 5️⃣ 约束条件（Constraints）

- **技术栈**：TypeScript 5.x, Node.js ≥ 18, vitest
- **影响范围**：仅 pool.ts + types.ts，不改变外部接口
- **环境变量命名**：沿用 CC 的 `CLAUDE_CODE_OAUTH_TOKEN` 前缀

---

## 6️⃣ 排除范围（Out of Scope）

- ❌ 不做 Token 轮询/负载均衡（只做 primary→fallback 单向切换 + 自动恢复）
- ❌ 不做 Token 有效性预检（运行时检测，非启动时验证）
- ❌ 不做多于两个 Token 的支持
