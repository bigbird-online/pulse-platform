---
feature_name: "web-terminal"
version: "0.1.0"
created: "2026-06-18T01:30:00+0800"
modified: "2026-06-18T01:30:00+0800"
upstream_dependency: ["bot-commands/design.md", "project-architecture.md"]
tags: [requirements, l2-spec, web-terminal, xterm, pty, relay-m3]
changelog:
  - "v0.1.0: 初始版本——/cc 浏览器终端接管 CC 会话"
---

# web-terminal - 功能需求文档

> **功能概述**：用户发送 `/cc` 后，Relay 释放当前 CC 进程并返回一个一次性浏览器 URL。用户点击 URL 在浏览器中打开 xterm.js 终端，通过 WebSocket 桥接到 node-pty 创建的 CC 进程，直接接管会话。终端里的 CC 使用与飞书 bot 相同的 agent 配置（model / provider / API key），无需用户手动 `/login` 或输入 Linux 密码。
>
> **设计原则**：Web Terminal 是 `/cc` 命令的**交互接管层**，不新增命令语义，复用现有 `handleCc` 的进程释放逻辑 + agent provider 配置解析。
>
> **关联文档**：
> - 上游依赖：`features/bot-commands/`（`/cc` 命令语义）、`project-architecture.md`（HTTP server 架构）
> - 复用：`modelCatalog` provider 解析、`TerminalManager` token 生命周期

---

## 1️⃣ 用户故事

**故事1：无密码浏览器接管**
- **作为**：租户用户（不知晓自己的 Linux 密码）
- **我希望**：发送 `/cc` 后收到一个可点击的链接，浏览器打开即进入终端，直接和 CC 对话
- **以便**：无需 SSH、无需密码、无需手动配置环境，降低接管门槛
- **优先级**：P0

**故事2：终端 CC 配置与飞书一致**
- **作为**：通过终端接管 CC 的用户
- **我希望**：终端里的 CC 使用与我飞书 bot 相同的 model 和 provider，不会遇到认证失败或用量限额错配
- **以便**：接管后的对话体验无缝衔接，不出现 `/login` 提示或错误的 provider 429
- **优先级**：P0

**故事3：会话隔离与自动回收**
- **作为**：平台管理员
- **我希望**：每个终端 token 是一次性的（10 分钟 TTL），WebSocket 断开或超时后 PTY 自动清理
- **以便**：防止终端会话泄露或资源泄漏
- **优先级**：P0

---

## 2️⃣ 功能需求

### P0 功能

#### FR-P0-001：`/cc` 返回浏览器终端 URL

- 当 `terminalManager` 可用时，`handleCc` 不再返回 SSH 命令，而是：
  1. 终止 Relay 管理的 CC 进程（`runtime.kill`）
  2. 创建一次性 token（UUID，10 分钟 TTL）
  3. 返回带 URL 的飞书交互卡片
- URL 格式：`https://{RELAY_PUBLIC_HOST}/terminal/{token}`（有 `RELAY_PUBLIC_HOST` 时）
- Fallback：`http://{sshAlias}:{RELAY_HTTP_PORT}/terminal/{token}`（无 `RELAY_PUBLIC_HOST` 时）

#### FR-P0-002：xterm.js 终端页面

- `GET /terminal/:token` 返回内嵌 xterm.js 5.5.0 的 HTML 页面
- 页面通过 WebSocket 连接 `/terminal/:token/ws`
- 支持 resize（`fit` addon）+ 键盘输入

#### FR-P0-003：WebSocket ↔ PTY 桥接

- `WS /terminal/:token/ws` 连接时：
  1. 验证 token（无效/过期 → close 4004）
  2. 检查 session 未被占用（已被占用 → close 4009）
  3. spawn CC PTY 进程
  4. PTY `onData` → WS `{type:'data',data}`
  5. WS `{type:'data',data}` → PTY `write`
  6. WS `{type:'resize',cols,rows}` → PTY `resize`
- PTY exit → WS `{type:'exit',code}` → close 1000

#### FR-P0-004：Agent 特定 Provider 配置

- Terminal CC 使用 agent 的 `ccModel` 对应的 provider，不使用全局 env 碰运气
- `handleCc` 解析 `ccModel → modelCatalog → provider → API key`，构建：
  - `--model {ccModel}` 参数
  - `--settings {"env":{"ANTHROPIC_AUTH_TOKEN":...,"ANTHROPIC_BASE_URL":...,"ANTHROPIC_DEFAULT_*_MODEL":...}}`
- Linux 路径通过 `sudo env KEY=VAL` 注入；macOS 路径通过 `process.env` 注入

#### FR-P0-005：Linux 租户隔离

- 当 `agent.linuxUser` 存在时，PTY 以 `sudo -u {linuxUser}` 方式 spawn
- `HOME` 设为 agent workspace，确保 CC 配置隔离

#### FR-P0-006：Token 生命周期与自动回收

- Token 创建时设定 10 分钟 TTL
- 每次活动（WS message / resize）刷新 TTL
- TTL 到期：close WS + kill PTY + 删除 session
- PTY exit / WS close：立即清理 session
- Relay 关闭：`cleanupAll()` 清理所有 session

---

## 3️⃣ 验收标准

| AC ID | 验收条件 | 优先级 |
|-------|---------|--------|
| AC-P0-001 | `/cc` 返回含 `https://` URL 的飞书卡片（有 `RELAY_PUBLIC_HOST` 时） | P0 |
| AC-P0-002 | 浏览器打开 URL 后显示 xterm.js 终端，自动连接 WebSocket | P0 |
| AC-P0-003 | 终端内输入消息，CC 正常响应（PTY ↔ WS 双向桥接） | P0 |
| AC-P0-004 | 终端内 CC 不提示 `/login`（provider 凭据正确注入） | P0 |
| AC-P0-005 | 终端 CC 使用的 model 与飞书 bot agent 配置一致 | P0 |
| AC-P0-006 | 10 分钟无活动后终端自动断开，PTY 进程已清理 | P0 |
| AC-P0-007 | 同一 token 第二次连接被拒（close 4009） | P0 |
| AC-P0-008 | 无效/过期 token 返回 410 / close 4004 | P0 |

---

## 4️⃣ 非功能需求

### 安全性

- Token 是 UUID v4，不可猜测
- HTTP server 默认绑定 `127.0.0.1`，通过 `RELAY_HTTP_HOST=0.0.0.0` 开放远程访问
- 生产环境通过 nginx 反向代理 + HTTPS（如 `pulse.example.com`）
- Token 一次性使用，不持久化（Relay 重启后所有 token 失效）

### 可靠性

- PTY spawn 失败不崩溃 Relay（try-catch 包裹）
- WebSocket 断开时 PTY 立即清理，无孤儿进程
- `cleanupAll()` 在 Relay 关闭时执行

### 兼容性

- node-pty 替换为 `@homebridge/node-pty-prebuilt-multiarch`（Linux 无需 g++ 编译）
- macOS 本地路径解析 `claude` 绝对路径（node-pty `posix_spawnp` 不查 PATH）
