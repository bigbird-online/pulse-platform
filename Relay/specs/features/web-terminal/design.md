---
feature_name: "web-terminal"
version: "0.1.0"
created: "2026-06-18T01:30:00+0800"
modified: "2026-06-18T01:30:00+0800"
upstream_dependency: ["bot-commands/design.md", "project-architecture.md"]
tags: [design, l2-spec, web-terminal, xterm, pty, websocket]
changelog:
  - "v0.1.0: 初始版本——TerminalManager + HTTP WS 路由 + handleCc 改造"
---

# web-terminal - 设计文档

> **上游需求**：[requirements.md](./requirements.md)

---

## 1. 架构概览

```
飞书用户发送 /cc
        ↓
┌───────────────────────────────────────────────────┐
│  commands.ts — handleCc()                          │
│  1. runtime.kill(poolKey) — 释放 CC 进程           │
│  2. 解析 agent ccModel → provider → API key        │
│  3. 构建 ccArgs [--model, --settings] + ccEnv      │
│  4. terminalManager.createToken(agentInfo)         │
│  5. 返回飞书卡片 (URL = RELAY_PUBLIC_HOST/terminal) │
└───────────────────────┬───────────────────────────┘
                        │ token
┌───────────────────────▼───────────────────────────┐
│  http-server.ts                                    │
│  GET  /terminal/:token → renderTerminalHtml()      │
│  WS   /terminal/:token/ws → terminalManager        │
│  .attachWebSocket(token, ws)                       │
└───────────────────────┬───────────────────────────┘
                        │
┌───────────────────────▼───────────────────────────┐
│  web-terminal.ts — TerminalManager                 │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────┐ │
│  │ Token Map   │  │ PTY spawn    │  │ WS bridge │ │
│  │ UUID→Session│  │ sudo -u /mac │  │ data/resize│ │
│  │ 10min TTL   │  │ claude resume│  │ exit/close │ │
│  └─────────────┘  └──────────────┘  └───────────┘ │
└───────────────────────┬───────────────────────────┘
                        │ node-pty
                   CC 子进程 (PTY)
                        │
                   Claude API (provider)
```

---

## 2. 核心组件

### 2.1 TerminalManager（`web-terminal.ts`）

**职责**：token 生命周期管理 + PTY spawn + WS ↔ PTY 桥接

**数据模型**：

```typescript
interface TerminalSession extends TerminalAgentInfo {
  token: string;           // UUID v4
  expiresAt: number;       // epoch ms (now + 10min)
  pty?: IPty;              // node-pty 进程
  ws?: WebSocket;          // 浏览器连接
  cleanupTimer?: Timer;    // TTL 定时器
}

interface TerminalAgentInfo {
  tenantId: string;        // poolKey
  workspace: string;       // agent workspace
  sessionId: string;       // CC session UUID
  linuxUser?: string;      // 租户 Linux 用户
  userHome?: string;       // macOS HOME 覆盖
  ccArgs?: string[];       // CC CLI 额外参数 [--model, --settings]
  ccEnv?: Record<string,string>; // Provider env vars
}
```

**关键方法**：

| 方法 | 职责 |
|------|------|
| `createToken(agent)` | 生成 UUID，存入 sessions Map，返回 token |
| `validateToken(token)` | HTTP GET 用，检查存在性 + 未过期 |
| `attachWebSocket(token, ws)` | WS 连接时调用：验证 → spawn PTY → 桥接 |
| `cleanup(token)` | kill PTY + close WS + delete session |
| `cleanupAll()` | Relay 关闭时清理所有 session |

### 2.2 HTTP 路由（`http-server.ts`）

**新增路由**：

| 方法 | 路径 | 行为 |
|------|------|------|
| GET | `/terminal/:token` | 验证 token → 返回 `renderTerminalHtml(token)` |
| WS | `/terminal/:token/ws` | `wss.handleUpgrade` → `terminalManager.attachWebSocket` |

**新增环境变量**：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RELAY_HTTP_HOST` | `127.0.0.1` | HTTP 绑定地址，`0.0.0.0` 允许远程 |
| `RELAY_PUBLIC_HOST` | (无) | URL 生成用的公网 hostname（如 `pulse.example.com`） |

**WS upgrade 处理**：

```typescript
server.on('upgrade', (req, socket, head) => {
  const wsMatch = url.match(/^\/terminal\/([a-f0-9-]+)\/ws$/i);
  if (wsMatch && deps.terminalManager) {
    wss.handleUpgrade(req, socket, head, (ws) => {
      deps.terminalManager.attachWebSocket(token, ws);
    });
  } else {
    socket.destroy();
  }
});
```

### 2.3 handleCc 改造（`commands.ts`）

**原逻辑**：返回 SSH 命令字符串

**新逻辑**（`terminalManager` 可用时）：

1. 终止 Relay CC 进程
2. 解析 agent provider 配置：
   ```
   ccModel = router.getAgentModel(userId, agentId)
   entry = modelCatalog.findById(ccModel)
   provider = modelCatalog.findProvider(entry.provider)
   keyVar = provider.api_key_pool.find(v => process.env[v])
   ```
3. 构建 `ccArgs` + `ccEnv`：
   - `ccArgs = ['--model', ccModel, '--settings', JSON.stringify({env: ccEnv})]`
   - `ccEnv` = ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL + ANTHROPIC_DEFAULT_*_MODEL
4. `terminalManager.createToken({...agent, ccArgs, ccEnv})`
5. URL 生成：
   - 有 `RELAY_PUBLIC_HOST` → `https://{host}/terminal/{token}`
   - 无 → `http://{sshAlias}:{port}/terminal/{token}`

---

## 3. PTY Spawn 策略

### 3.1 Linux（服务器）

```
sudo -u {linuxUser} env \
  HOME={workspace} \
  PATH=/usr/local/bin:/usr/bin:/bin \
  ANTHROPIC_AUTH_TOKEN={key} \
  ANTHROPIC_BASE_URL={url} \
  ... \
  {claudeBin} --resume {sessionId} --model {ccModel} --settings '{json}'
```

- `sudo -u` 实现租户用户隔离
- `env KEY=VAL` 注入 provider 凭据（不依赖 sudoers env_keep）
- `--settings` 作为 CC 首选配置源

### 3.2 macOS（本地开发）

```
{claudeAbs} --resume {sessionId} --model {ccModel} --settings '{json}'
```

- 解析 `claude` 绝对路径（node-pty `posix_spawnp` 不查 PATH）
- env 通过 `spawn({env: {...process.env, ...ccEnv}})` 传递

---

## 4. xterm.js 前端

`renderTerminalHtml(token)` 返回内嵌 CDN 的静态 HTML：

- xterm.js 5.5.0 + fit addon
- WS 协议：JSON 消息 `{type:'data'|'resize'|'exit', ...}`
- 自动连接 + resize 同步 + 断开提示

---

## 5. 依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| `@homebridge/node-pty-prebuilt-multiarch` | ^0.13.1 | PTY 创建（含 Linux prebuilds，无需 g++） |
| `ws` | ^8.20 | WebSocket server（已有） |

> **替换说明**：原 `node-pty ^1.1.0` 无 Linux prebuilds，阿里云服务器缺 g++ 导致编译失败。换用 prebuilt 版本解决。

---

## 6. 与现有组件的关系

```
index.ts
  ├── new TerminalManager()           ← 新增
  ├── feishuDeps.terminalManager = .. ← 注入 feishu channel
  └── httpServerDeps.terminalManager  ← 注入 HTTP server

feishu.ts
  └── commandDeps.terminalManager     ← 传递给 handleCc

commands.ts (handleCc)
  └── deps.terminalManager.createToken()

http-server.ts
  ├── GET /terminal/:token            ← 新路由
  └── WS upgrade → attachWebSocket    ← 新 WS 处理
```
