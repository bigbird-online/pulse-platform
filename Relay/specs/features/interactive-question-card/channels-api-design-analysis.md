# Channels API 条件集成设计分析

> Date: 2026-06-11
> Status: **设计分析**
> 前置：AskUserQuestion 已通过路径 A（PTY 键盘模拟）落地；本文分析 Phase 2 路径 B（Channels API）的可行性

---

## 1. 目标

在 Relay 现有多模型架构上，**仅对 Claude 官方模型**启用 CC Channels API，实现完整的工具权限审批透传（Bash/Write/Edit 等）。非 Claude 模型（GLM、DeepSeek 等）保持现有 PTY / `-p` 模式不变。

---

## 2. 当前架构（已有）

### 模型路由链

```
AgentConfig.ccModel
  → tenant override (agent-models.json)
  → model catalog resolve (models.json)
  → provider detection (ProviderDef.protocol)
  → CC spawn args (--model, env injection)
```

### 关键决策点

| 决策点 | 代码位置 | 当前逻辑 |
|--------|----------|----------|
| CC 模式选择 | `pool.ts:1071` `resolveCCMode()` | GLM → interactive，其他 → print |
| spawn 参数构建 | `pool.ts:1093/1078` | interactive 走 PTY bridge，print 走 `-p` stream-json |
| 环境变量注入 | `pool.ts:805` `resolveModelProviderEnv()` | 按 provider 注入 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` |
| Pool 隔离 | `pool.ts:402` `resolvePoolKey()` | `${tenantId}:${provider}` 按 provider 隔离进程 |

### Provider 定义示例

```json
{
  "providers": {
    "anthropic": {
      "protocol": "anthropic",
      "base_url_env": "ANTHROPIC_BASE_URL",
      "api_key_pool": ["ANTHROPIC_API_KEY"]
    },
    "glm": {
      "protocol": "anthropic",
      "base_url_env": "GLM_BASE_URL",
      "api_key_pool": ["GLM_API_KEY"],
      "force_interactive": true
    }
  }
}
```

---

## 3. Channels API 工作原理

### CC `--channels` 标志

```bash
claude --channels server:relay-feishu
# 或
claude --channels plugin:telegram@claude-plugins-official
```

- CC 启动交互式 session（非 `-p`）
- CC 作为 MCP client，通过 stdio 与 channel server 通信
- Channel server 是一个 MCP server（stdio transport），声明 capability
- CC 发送 `notifications/claude/channel/permission_request` → server 转发到 IM
- 用户点选 → server 发送 `notifications/claude/channel/permission` 回 CC

### MCP 通知协议

```
CC → Server:  notifications/claude/channel/permission_request
              { request_id, tool_name, description, input_preview }

Server → CC:  notifications/claude/channel/permission
              { request_id, behavior: "allow" | "deny" }
```

### 关键约束（2026-06-11 源码实证）

1. **需要 OAuth token（已确认，C3 已验证）**：
   - CC 源码 `z4_()` 函数按序检查：capability → feature flag → `Tq()?.accessToken` → org policy → session allowlist
   - `Tq()` 从三个来源取 OAuth token：`CLAUDE_CODE_OAUTH_TOKEN` 环境变量、`OX_()`（二级来源）、`/login` 存储的 `claudeAiOauth`
   - **`Tq()` 不检查 `ANTHROPIC_API_KEY`**——API key 是独立的 auth 路径，用于模型 API 调用，不用于 channels feature gate
   - 无 OAuth token 时，CC 输出 `"channels requires claude.ai authentication (run /login)"` 并跳过 channels
   - **`--bare` 模式也不兼容 channels**（bare 模式跳过 OAuth keychain 读取，`Tq()` 必返回 null）
2. **Console API key 用户的特例**：`claude auth login --console` 在配发 API key 的同时也会存储 OAuth token，所以 Console 用户可以使用 channels。这不是"API key 替代 OAuth"，而是 Console 登录同时提供了两者。
3. **CC 必须是交互式 session**（不能用 `-p`）
4. **每个 CC session 对应一个 channel server 实例**
5. **`--channels` 与 `--dangerously-skip-permissions` 完全正交**——channels 控制 MCP 事件路由，skip-permissions 控制工具执行审批，可独立使用

---

## 4. 条件集成设计

### 4.1 判断条件："是否应该启用 Channels"

```typescript
function shouldUseChannels(ccModel: string | undefined, catalog: ModelCatalog): boolean {
  if (!ccModel) return false;
  const entry = catalog.findById(ccModel);
  if (!entry) return false;
  // Claude 官方模型 = claude_alias 或 provider = 'anthropic'
  if (entry.claude_alias) return true;
  const provider = catalog.getProviderForModel(ccModel);
  // 新增 provider 字段：channels: true
  if (provider && (provider as any).channels === true) return true;
  // 兜底：provider name = 'anthropic' 且 protocol = 'anthropic'
  return false;
}
```

### 4.2 Provider 扩展

```typescript
// model-catalog.ts ProviderDef 扩展
export interface ProviderDef {
  protocol: 'anthropic' | 'openai';
  base_url_env: string;
  api_key_pool: string[];
  force_interactive?: boolean;
  /** If true, spawn CC with --channels for this provider's models.
   *  Requires OAuth token (not API key). Only valid for Claude official models. */
  channels?: boolean;
}
```

### 4.3 新增 CCMode

```typescript
// types.ts
export type CCMode = 'print' | 'interactive' | 'channels';
```

### 4.4 CC 模式选择扩展

```typescript
// pool.ts resolveCCMode
function resolveCCMode(env, spawnMode?, ccModel?): CCMode {
  if (configuredCcMode) return configuredCcMode;
  if (spawnMode) return spawnMode;
  // NEW: Channels mode for Claude models with channels capability
  if (shouldUseChannels(ccModel, modelCatalog)) return 'channels';
  // Existing: GLM providers use interactive
  if (isGlmCcModel(ccModel, env)) return 'interactive';
  return 'print';
}
```

### 4.5 Channel Server（新模块）

```
src/channels/
├── relay-channel-server.ts    // MCP server implementation
├── channel-bridge.ts          // Pool ↔ Channel server lifecycle
└── types.ts                   // Channel-specific types
```

**`relay-channel-server.ts`** — MCP stdio server：

```typescript
// 声明 capability
const capabilities = {
  'claude/channel': {},
  'claude/channel/permission': {},
};

// 收到 CC 的 permission_request
onNotification('notifications/claude/channel/permission_request', (params) => {
  // 转发到飞书/IM channel handler
  emitter.emit('permission-request', tenantId, params);
});

// 收到 CC 的 AskUserQuestion（也走 channel）
onNotification('notifications/claude/channel/ask_user_question', (params) => {
  emitter.emit('ask-user-question', tenantId, params);
});

// IM 回调后回复 CC
function sendPermissionVerdict(requestId: string, behavior: 'allow' | 'deny') {
  sendNotification('notifications/claude/channel/permission', {
    request_id: requestId,
    behavior,
  });
}
```

### 4.6 Spawn 流程扩展

```
Pool.spawn(config)
  → resolveCCMode()
  → if 'channels':
      1. 创建 MCP channel server 子进程
      2. spawn CC: claude --channels server:relay-channel
         (CC 通过 stdio 与 channel server 通信)
      3. channel server 的事件桥接到 pool emitter
      4. IM channel handler 监听事件 → 渲染卡片
  → else (existing flow):
      print / interactive / PTY bridge
```

### 4.7 进程模型

```
                    Claude 模型                           非 Claude 模型
                    ──────────                            ──────────────
用户消息 → Relay → Pool →                                 Pool →
                    │                                     │
                    ├─ CC (--channels)                    ├─ CC (-p / PTY)
                    │  stdio ↔ MCP server                 │  stdout → JSONL watcher
                    │                                     │
                    ├─ Channel Bridge                     ├─ 现有 response emitter
                    │  permission_request ──────┐         │
                    │  ask_user_question ───────┤         │
                    │                           │         │
                    └─ Feishu Handler ←─────────┘         └─ Feishu Handler
                       卡片/按钮                               卡片/按钮
                       → permission verdict                   → PTY injectInput
                       → sendPermissionVerdict()              → pool.send (A' fallback)
```

---

## 5. OAuth 问题（已确认需要）

### 源码实证

CC 二进制（v2.1.47+）中 `z4_()` 函数是 channels 的准入门：

```
z4_() 检查序列:
  1. MCP server 是否声明 claude/channel capability？
  2. feature flag vMH() 是否为 true？
  3. Tq()?.accessToken 是否存在？  ← 如果 null，直接 skip + 输出 "channels requires claude.ai authentication"
  4. org policy 是否允许？
  5. server 是否在 --channels 列表？
  6. 是否在 approved plugin allowlist？
```

`Tq()` 的 token 来源（按序）：
- `process.env.CLAUDE_CODE_OAUTH_TOKEN`
- `OX_()` 二级来源
- `/login` 存储的 `claudeAiOauth`

**`ANTHROPIC_API_KEY` 不在 Tq() 的检查范围内。**

### 当前状态

- Relay 使用 `ANTHROPIC_API_KEY`（API key）连接 Claude
- Channels 需要 `CLAUDE_CODE_OAUTH_TOKEN`（claude.ai OAuth）
- 两者不互通，必须分别配置

### 解决方案选项

| 选项 | 做法 | 优点 | 缺点 |
|------|------|------|------|
| **A. 双 token** | provider 定义同时配置 `api_key_pool` + `oauth_token_env`；channels 用 OAuth，非 channels 用 API key | 最小改动 | 需要用户在 claude.ai 获取 OAuth token |
| **B. 自动 OAuth** | 用 API key 通过 Anthropic API 换取 OAuth session | 用户体验好 | 未验证可行性；可能违反 ToS |
| **C. 仅限 OAuth 用户** | 只在配置了 OAuth token 的 provider 上启用 channels | 明确 | API key 用户无法使用 channels |

**推荐选项 A**：在 provider 配置中新增 `oauth_token_env` 字段。

```json
{
  "providers": {
    "anthropic": {
      "protocol": "anthropic",
      "base_url_env": "ANTHROPIC_BASE_URL",
      "api_key_pool": ["ANTHROPIC_API_KEY"],
      "channels": true,
      "oauth_token_env": "CLAUDE_CODE_OAUTH_TOKEN"
    }
  }
}
```

当 `channels: true` 且 `oauth_token_env` 对应的环境变量有值时，启用 Channels 模式。否则回退到 print 模式。

---

## 6. Multi-Tenant 隔离

### 挑战

CC Channels 设计为 single-user。每个 CC session 对应一个 channel server。Relay 是 multi-tenant。

### 方案：per-tenant channel server

```
Pool
├── tenant "user1:agentA" → CC (--channels) ←→ channel-server-A (MCP stdio)
├── tenant "user1:agentB" → CC (--channels) ←> channel-server-B (MCP stdio)
└── tenant "user2:agentC" → CC (PTY, GLM)   → PTY bridge (现有)
```

每个 channels 模式的 tenant 有独立的 CC 进程 + 独立的 channel server 子进程。channel server 的事件通过 tenantId 标记，路由到正确的 IM handler。

### 进程管理

```typescript
// 新增到 pool 内部状态
const channelBridges = new Map<string, ChannelBridge>();

interface ChannelBridge {
  ccProcess: ChildProcess;
  mcpServer: ChildProcess;
  tenantId: string;
  events: EventEmitter;
}
```

---

## 7. AskUserQuestion 统一

### 当前状态

- PTY 模式：路径 A（键盘模拟 + injectInput）
- Print 模式：A' 回退（pool.send 新文字 prompt）

### Channels 模式

CC 通过 MCP 通知发送 AskUserQuestion 选项 → channel server → 飞书卡片 → 用户点选 → MCP notification 回 CC。

这比路径 A 更优雅（结构化协议，不依赖 TUI 格式），但只在 Claude 模型下可用。

### feishu.ts 统一入口

```typescript
// 现有：pool response 事件 → handleFeishuPoolResponse（拦截 AskUserQuestion tool_use）
// 新增：channel bridge 事件 → handleChannelPermission（处理 permission_request + AskUserQuestion）
// 两者最终都渲染飞书卡片，但回注方式不同：
//   - PTY: injectInput（键盘模拟）
//   - Channels: sendPermissionVerdict（MCP notification）
//   - Print: pool.send（A' 文字 prompt）
```

---

## 8. 改动范围评估

### 新增文件

| 文件 | 职责 |
|------|------|
| `src/channels/relay-channel-server.ts` | MCP stdio server（~300 行） |
| `src/channels/channel-bridge.ts` | CC + MCP server 生命周期管理（~200 行） |
| `src/channels/types.ts` | Channel 事件类型（~50 行） |

### 修改文件

| 文件 | 改动 |
|------|------|
| `src/model-catalog.ts` | `ProviderDef` 新增 `channels?` 和 `oauth_token_env?` |
| `src/types.ts` | `CCMode` 新增 `'channels'`；`PoolOptions` 新增 channel 相关 |
| `src/pool.ts` | `resolveCCMode` 新增 channels 分支；`spawn` 新增 channels 模式构建；新增 channel bridge 生命周期 |
| `src/channels/feishu.ts` | 新增 `handleChannelPermission` 路径（与现有 `handleFeishuPoolResponse` 并存） |
| `src/index.ts` | 集成 channel bridge |

### 不改动的文件

- `pending-interactive.ts` — 路径 A 仍用于非 Claude 模型
- `router.ts` — agent 配置无需变更（channels 由 provider 配置驱动）
- 现有 test — 路径 A 逻辑不变，新增 channels 测试

---

## 9. 风险矩阵

| 风险 | 级别 | 缓解 |
|------|------|------|
| OAuth token 获取/刷新 | 中 | 文档化获取流程；token 过期时回退到 print 模式 |
| CC `--channels` 版本兼容 | 中 | 检测 CC 版本 ≥ 2.1.81；不满足时回退 |
| per-tenant 进程开销 | 低 | channel server 是轻量 Node 进程；无交互时 idle |
| MCP 协议变更 | 中 | 锁定 CC 版本；监听上游 API 变更 |
| 与现有 pool 共存 | 低 | CCMode 区分；互不影响 |

---

## 10. 实施路线建议

```
Phase 2a (POC, 1-2 周)
  ├── 1. MCP channel server 最小实现（仅 permission_request/permission）
  ├── 2. ProviderDef 扩展 + resolveCCMode 分支
  ├── 3. 单 tenant 验证：Claude 模型 + 飞书权限按钮
  └── 4. 确认 AskUserQuestion 在 channels 下自动工作

Phase 2b (生产化, 2-3 周)
  ├── 5. Multi-tenant 进程管理
  ├── 6. OAuth token 生命周期管理
  ├── 7. 回退逻辑（channels 不可用时降级到 print + A'）
  ├── 8. feishu.ts 统一入口（三路径并存）
  └── 9. 测试 + 部署

Phase 2c (TG 对齐, 1-2 周)
  └── 10. TG channel handler（与飞书共享 channel bridge）
```

---

## 11. 关键待验证（含已验证结果）

| # | 问题 | 状态 | 结果 |
|---|------|------|------|
| C1 | CC `--channels server:<name>` 是否支持自定义 MCP server（非 plugin marketplace）？ | ❓ 待验证 | CC 文档提到 `--dangerously-load-development-channels` 可绕过 approved plugin allowlist，暗示自定义 server 可能需加此 flag |
| C2 | AskUserQuestion 是否也通过 channel MCP 通知发送？还是只在 TUI 内处理？ | ❓ 待验证 | 官方 channels reference 未明确提及 AskUserQuestion 的 channel 通知；需实测 |
| C3 | OAuth token 是否能用 API key 替代？ | ✅ **已验证：不能** | `Tq()` 不检查 `ANTHROPIC_API_KEY`；必须有 `CLAUDE_CODE_OAUTH_TOKEN` 或 `/login` 存储的 OAuth |
| C4 | 一个 CC 进程能否同时有多个 channel？ | ❓ 待验证 | `--channels` 接受 space-separated list，理论上支持多个 |
| C5 | Channel server 的 MCP 协议版本？ | ❓ 待验证 | 需检查 `@modelcontextprotocol/sdk` 版本 |

---

## 12. 结论与决策框架

基于已验证的事实，设计决策如下：

### 能力矩阵

| 场景 | 授权请求透传 | AskUserQuestion | 实现路径 |
|------|-------------|-----------------|----------|
| Claude 模型 + OAuth token | ✅ Channels API | ✅ Channels API（优先）/ PTY injectInput（回退） | `CCMode = 'channels'` |
| Claude 模型 + 仅 API key | ❌ 不支持 | ✅ PTY injectInput | `CCMode = 'interactive'`（现有路径 A） |
| 非 Claude 模型（GLM 等） | ❌ 不支持 | ✅ PTY injectInput | `CCMode = 'interactive'`（现有路径 A） |
| Print 模式（DeepSeek 等） | ❌ 不支持 | ⚠️ A' 文字回退 | `CCMode = 'print'`（现有路径） |

### 分层策略

```
provider.channels === true && CLAUDE_CODE_OAUTH_TOKEN 存在?
  ├─ Yes → CCMode = 'channels'
  │         AskUserQuestion: channel 通知 → 飞书卡片 → MCP verdict
  │         Permission: channel permission_request → 飞书按钮 → MCP permission
  │         CC spawn: claude --channels server:relay-feishu
  │
  └─ No → 现有路径
            AskUserQuestion: PTY injectInput / print A'
            Permission: 不支持（--dangerously-skip-permissions 或 dontAsk）
            CC spawn: PTY bridge / -p stream-json
```
