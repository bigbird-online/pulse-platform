---
feature_name: "token-fallback"
version: "0.0.1"
created: "2026-04-13T17:00:00+0800"
modified: "2026-04-13T17:00:00+0800"
upstream_dependency: ["requirements.md", "design.md"]
tags: [tasks, l2-spec, auth, fallback, relay]
---

# token-fallback - 任务拆分文档

> **关联文档**：
> - L2功能需求：`features/token-fallback/requirements.md`
> - L2技术设计：`features/token-fallback/design.md`

---

## 1️⃣ 任务列表

| Task ID | 任务名称 | 估时 | 依赖 | 状态 |
|---------|---------|------|------|------|
| TF-001 | types.ts 扩展 PoolOptions | 5min | — | pending |
| TF-002 | pool.ts: Token 状态管理 + env 注入 | 15min | TF-001 | pending |
| TF-003 | pool.ts: stderr 信号检测 | 10min | TF-002 | pending |
| TF-004 | pool.ts: handleTokenFallback 实现 | 15min | TF-003 | pending |
| TF-005 | 测试: fallback 切换 + 边界场景 | 20min | TF-004 | pending |
| TF-006 | launchd plist 配置更新 | 5min | TF-005 | pending |

---

## 2️⃣ 任务详情

### TF-001：types.ts 扩展 PoolOptions

**描述**：在 PoolOptions 接口中增加 `primaryToken?` 和 `fallbackToken?` 字段。

**输入**：design.md §4 Entity-002

**输出**：types.ts 中 PoolOptions 增加两个可选字段

**验证标准**：typecheck 通过，现有测试不受影响

---

### TF-002：pool.ts: Token 状态管理 + env 注入

**描述**：
1. createPool 读取 options.primaryToken / fallbackToken（或从 process.env 回退读取）
2. 新增 `tokenState: Map<string, TokenTier>` 追踪每租户当前使用的 Token 层级
3. spawnProcess 的 env 构建中，将 `CLAUDE_CODE_OAUTH_TOKEN` 设为当前层级对应的 Token
4. 非 resume 的 spawn 重置 tokenState 为 'primary'

**输入**：design.md §3 Function-001, §4 Entity-001

**输出**：pool.ts 中 Token 管理逻辑

**验证标准**：
- 新 spawn 时 env 包含 primary Token
- resume spawn 保持当前 tokenState
- 无 fallbackToken 时行为不变

---

### TF-003：pool.ts: stderr 信号检测

**描述**：
1. 实现 `detectTokenSignal(text: string): boolean` 函数
2. 在现有 stderr listener 中调用，匹配到信号时触发 fallback

**输入**：design.md §3 Function-002

**输出**：pool.ts 中 detectTokenSignal 函数 + stderr listener 增强

**验证标准**：
- 'rate limit' → true
- 'rate_limit' → true
- 'overloaded' → true
- '401' → true
- 'insufficient_quota' → true
- 普通 stderr 输出 → false

---

### TF-004：pool.ts: handleTokenFallback 实现

**描述**：
1. 实现 handleTokenFallback(tenantId) 异步函数
2. 检查前置条件（fallbackToken 存在、当前为 primary）
3. Kill 当前进程，切换 tokenState，resume spawn
4. 不修改 restartTracking
5. 日志输出切换信息（Token 前 8 字符）

**输入**：design.md §3 Function-003, §5 场景1/2

**输出**：pool.ts 中 handleTokenFallback 函数

**验证标准**：
- 切换后 tokenState 为 'fallback'
- restartTracking.count 不变
- 已是 fallback 时不触发二次切换

---

### TF-005：测试: fallback 切换 + 边界场景

**描述**：新增测试用例覆盖：
1. primary → fallback 切换成功（AC-P0-001）
2. 切换不消耗 restart quota（AC-P0-002）
3. 无 fallback Token 时走正常 crash 流程（AC-P0-003）
4. 两次均失败时走正常 crash 流程（AC-P0-004）
5. detectTokenSignal 各关键词匹配

**输入**：requirements.md §3 验收标准

**输出**：tests/ 目录下新增或扩展测试文件

**验证标准**：所有测试通过，覆盖率 ≥ 80%

---

### TF-006：launchd plist 配置更新

**描述**：在 `com.pulse.relay.plist` 中增加 `CLAUDE_CODE_OAUTH_TOKEN_FALLBACK` 环境变量，值为 Pro Token。

**输入**：当前 plist 中的 Pro Token（`...Y3Ta...`）

**输出**：plist 更新，Primary=Max, Fallback=Pro

**验证标准**：`launchctl print` 显示两个 Token 环境变量

---

## 3️⃣ 任务依赖图

```
TF-001 → TF-002 → TF-003 → TF-004 → TF-005 → TF-006
```

线性依赖，每个任务基于前一个的产出。

---

## 4️⃣ Definition of Done

- [ ] typecheck 通过（`tsc --noEmit`）
- [ ] 现有测试全部通过（`vitest run`）
- [ ] 新增测试覆盖 fallback 核心逻辑
- [ ] 日志不泄露完整 Token
- [ ] launchd plist 配置完成
