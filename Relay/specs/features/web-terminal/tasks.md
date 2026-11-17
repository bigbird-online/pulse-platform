---
feature_name: "web-terminal"
version: "0.1.0"
created: "2026-06-18T01:30:00+0800"
modified: "2026-06-18T01:30:00+0800"
upstream_dependency: ["./design.md"]
tags: [tasks, l2-spec, web-terminal]
changelog:
  - "v0.1.0: 初始版本——已完成实现的任务回溯"
---

# web-terminal - 任务清单

> **上游设计**：[design.md](./design.md)
>
> **状态**：✅ 全部完成（2026-06-18）

---

## 任务总览

| ID | 任务 | 状态 | 关联 AC |
|----|------|------|---------|
| T-001 | TerminalManager 类 + token 生命周期 | ✅ | AC-P0-001, 006, 007, 008 |
| T-002 | PTY spawn（Linux sudo -u + macOS 本地） | ✅ | AC-P0-003, 005 |
| T-003 | WebSocket ↔ PTY 桥接 | ✅ | AC-P0-003 |
| T-004 | xterm.js HTML 页面渲染 | ✅ | AC-P0-002 |
| T-005 | HTTP 路由 GET /terminal/:token | ✅ | AC-P0-002 |
| T-006 | WS upgrade 处理 | ✅ | AC-P0-003 |
| T-007 | handleCc 改造（返回 URL） | ✅ | AC-P0-001 |
| T-008 | Agent provider 配置解析 + ccArgs/ccEnv | ✅ | AC-P0-004, 005 |
| T-009 | RELAY_HTTP_HOST / RELAY_PUBLIC_HOST 支持 | ✅ | AC-P0-001 |
| T-010 | node-pty → prebuilt-multiarch 替换 | ✅ | 非功能 |
| T-011 | index.ts 接线 + feishu.ts 注入 | ✅ | 全部 |
| T-012 | deploy.yml 加 npm install | ✅ | 非功能 |

---

## 详细任务

### T-001：TerminalManager 类

- **文件**：`src/web-terminal.ts`（新增）
- **内容**：
  - `TerminalSession` interface（token/expiresAt/pty/ws/cleanupTimer）
  - `createToken(agent)` → UUID v4 + 10min TTL
  - `validateToken(token)` → HTTP GET 验证
  - `cleanup(token)` / `cleanupAll()`
  - `resetCleanupTimer(token)` → TTL 刷新
- **状态**：✅ 完成

### T-002：PTY spawn

- **文件**：`src/web-terminal.ts` attachWebSocket()
- **Linux 路径**：`sudo -u {linuxUser} env KEY=VAL ... claude --resume {sid} {ccArgs}`
- **macOS 路径**：`{claudeAbs} --resume {sid} {ccArgs}`（解析绝对路径）
- **try-catch**：spawn 失败不崩溃 Relay
- **状态**：✅ 完成

### T-003：WS ↔ PTY 桥接

- **文件**：`src/web-terminal.ts` attachWebSocket()
- **PTY→WS**：`pty.onData → ws.send({type:'data',data})`
- **WS→PTY**：`ws.on('message') → pty.write(data)`
- **resize**：`ws {type:'resize',cols,rows} → pty.resize()`
- **exit**：`pty.onExit → ws {type:'exit',code} → cleanup`
- **状态**：✅ 完成

### T-004：xterm.js HTML

- **文件**：`src/web-terminal.ts` renderTerminalHtml()
- **CDN**：xterm 5.5.0 + fit addon 0.10.0
- **WS 协议**：JSON `{type,data|cols|rows|code}`
- **状态**：✅ 完成

### T-005：HTTP 路由

- **文件**：`src/http-server.ts` route()
- **路由**：`GET /terminal/:token` → validateToken → renderTerminalHtml
- **无效 token**：410 JSON
- **状态**：✅ 完成

### T-006：WS upgrade

- **文件**：`src/http-server.ts` startRelayHttpServer()
- **WebSocketServer** noServer + manual upgrade
- **路由匹配**：`/terminal/:token/ws`
- **状态**：✅ 完成

### T-007：handleCc 改造

- **文件**：`src/commands.ts` handleCc()
- **terminalManager 可用时**：kill CC → createToken → 返回 URL 卡片
- **URL 格式**：`https://{RELAY_PUBLIC_HOST}/terminal/{token}` 或 fallback
- **状态**：✅ 完成

### T-008：Agent provider 配置解析

- **文件**：`src/commands.ts` handleCc()
- **解析链**：`ccModel → modelCatalog.findById → provider → api_key_pool`
- **构建**：`ccArgs = [--model, --settings]` + `ccEnv = {ANTHROPIC_AUTH_TOKEN, BASE_URL, DEFAULT_*_MODEL}`
- **状态**：✅ 完成

### T-009：HTTP 绑定 + URL 配置

- **文件**：`src/http-server.ts` + `src/commands.ts`
- **`RELAY_HTTP_HOST`**：默认 127.0.0.1，`resolveRelayHttpHost()` 解析
- **`RELAY_PUBLIC_HOST`**：URL 生成优先使用，走 HTTPS 443 不加端口
- **状态**：✅ 完成

### T-010：node-pty 替换

- **文件**：`package.json`
- **变更**：`node-pty ^1.1.0` → `@homebridge/node-pty-prebuilt-multiarch ^0.13.1`
- **原因**：阿里云缺 g++，node-pty 无 Linux prebuilds
- **状态**：✅ 完成

### T-011：接线串联

- **文件**：`src/index.ts` + `src/channels/feishu.ts`
- **index.ts**：`new TerminalManager()` → feishuDeps + httpServerDeps
- **feishu.ts**：FeishuBotDeps + CommandDeps 传递 terminalManager
- **状态**：✅ 完成

### T-012：deploy.yml 修复

- **文件**：`.github/workflows/deploy.yml`
- **变更**：Build Platform / Build Relay 前加 `npm install`
- **原因**：新增依赖后服务器 node_modules 过期
- **状态**：✅ 完成

---

## 实现提交历史

| Commit | 描述 |
|--------|------|
| `daeec15` | parseResetTime 提取 + 429 fallback + env bridge |
| `36a2b57` | Web Terminal — 浏览器端 CC 接管 |
| `05bdca8` | node-pty + postinstall（后被 prebuilt 替换） |
| `77cd9d3` | deploy.yml 加 npm install |
| `82a89b3` | 换 @homebridge/node-pty-prebuilt-multiarch |
| `83600da` | RELAY_PUBLIC_HOST env var |
| `ad03326` | RELAY_PUBLIC_HOST 用 HTTPS 不加端口 |
| `33577f2` | Web Terminal PTY 传 auth env vars |
| `dc813c5` | Agent-specific provider config |
