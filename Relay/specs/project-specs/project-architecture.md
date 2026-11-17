---
version: "1.2.1"
created: "2026-04-06"
modified: "2026-08-17"
changelog:
  - "v1.2.1: scheduler 与 /internal/push 共享 run-level terminal registry，同 run 终态原子去重"
  - "v1.2.0: 新增 HTTP Server + Web Terminal 模块（/cc 浏览器接管）"
  - "v1.1.0: 150-spec-sync 重写，对齐 Relay v1.1 三层架构 + Gateway + 多渠道"
  - "v0.1.0: 初始版本，基于 CC 子进程验证结论设计"
---

# Relay - 架构设计

## 架构概览

```
用户 (Telegram / 飞书)
        ↓ 消息
┌─────────────────────────────────────────────────────────────┐
│  Relay Gateway (Node.js / TypeScript)                        │
│                                                              │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │ channels/   │  │ gateway.ts   │  │ delivery.ts        │  │
│  │ telegram.ts │  │ ChannelAddr  │  │ DeliveryRequest    │  │
│  │ feishu.ts   │  │ Binding Reg  │  │ DeliveryReceipt    │  │
│  └──────┬──────┘  │ RoutePolicy  │  │ TG/飞书投递         │  │
│         │         └──────┬───────┘  └────────────────────┘  │
│         │                │                                   │
│  ┌──────┴────────────────┴──────────────────────────────┐   │
│  │ conversation-runtime.ts / codex-conversation-runtime  │   │
│  │ CC runtime (pipe) · Codex runtime (exec) · fallback   │   │
│  └──────────────────────┬───────────────────────────────┘   │
│                          │                                   │
│  ┌───────────────────────┴───────────────────────────────┐  │
│  │ pool.ts — CC 子进程池                                  │  │
│  │ spawn/resume/kill · idle 回收 · crash restart          │  │
│  │ token fallback (primary/fallback OAuth)                │  │
│  │ PTY interactive mode · session replay on crash         │  │
│  └──────────────────────┬────────────────────────────────┘  │
│                          │                                   │
│  ┌───────────┐  ┌───────┴───────┐  ┌───────────────────┐   │
│  │ scheduler  │  │ state.ts      │  │ metering.ts       │   │
│  │ cron 调度  │  │ 会话持久化     │  │ usage 提取+计费    │   │
│  │ harness    │  │ 重启恢复       │  │ JSONL 按日分文件   │   │
│  │ inject_file│  │               │  │                   │   │
│  └───────────┘  └───────────────┘  └───────────────────┘   │
│                                                              │
│  ┌───────────┐  ┌───────────────┐  ┌───────────────────┐   │
│  │ commands  │  │ command-menu  │  │ router.ts         │   │
│  │ /start etc│  │ 菜单注册       │  │ chat_id→agent     │   │
│  │ /runtime  │  │               │  │ legacy+binding    │   │
│  │ /yolo /cc │  │               │  │                   │   │
│  └───────────┘  └───────────────┘  └───────────────────┘   │
│                                                              │
│  ┌───────────────────────────────┐  ┌───────────────────┐   │
│  │ http-server.ts                │  │ web-terminal.ts   │   │
│  │ /internal/* (loopback API)    │  │ TerminalManager   │   │
│  │ /terminal/:token (xterm HTML) │  │ token + PTY + WS  │   │
│  │ /terminal/:token/ws (WebSocket)│  │ /cc 浏览器接管    │   │
│  └───────────────────────────────┘  └───────────────────┘   │
└─────────────────────────────────────────────────────────────┘
        ↓ stdin/stdout (NDJSON)          ↓ dispatch/escalation
┌──────────────────────┐    ┌───────────────────────────────┐
│  CC 子进程池          │    │  agent-harness (Python)        │
│  每租户独立进程       │    │  wi/handoff/dispatcher/         │
│  cwd = workspace      │    │  escalation/ssot               │
│  CC 或 Codex runtime  │    │  1059 tests                    │
└──────────────────────┘    └───────────────────────────────┘
        ↓
   Claude API / Codex API
```

## 三层架构

| 层 | 组件 | 语言 | 职责 |
|----|------|------|------|
| S1 执行 | Relay + CC runtime | TypeScript / Node.js | 消息路由、进程池、会话管理、投递 |
| S1 执行 | agent-harness | Python | wi/handoff/dispatcher/escalation/ssot |
| S2 协调 | Relay scheduler | TypeScript | cron 调度、inject_file、harness dispatch |
| 运行时 | CC / Codex | CLI 子进程 | 实际 AI 推理和工具调用 |

## 技术选型

| 层 | 选型 | 理由 |
|----|------|------|
| 运行时 | Node.js >= 18 (ES Modules) | CC 是 Node.js，统一技术栈 |
| 语言 | TypeScript 6.x (strict) | 类型安全，与 CC SDK 对齐 |
| TG Bot | grammy ^1.42 | long polling，社区活跃 |
| 飞书 SDK | @larksuiteoapi/node-sdk ^1.61 | 官方 SDK，WebSocket 连接 |
| 调度 | node-cron ^4.2 | 时区感知 cron |
| WebSocket | ws ^8.20 | 飞书 WebSocket 长连接 + Web Terminal 浏览器桥接 |
| PTY | @homebridge/node-pty-prebuilt-multiarch ^0.13 | Web Terminal 浏览器 ↔ CC 进程桥接（含 Linux prebuilds，无需 g++） |
| 测试 | vitest ^4.1 + @vitest/coverage-v8 | 快速、ESM 原生 |
| 构建 | tsc 直编 | 无需 bundler |
| CC 通信 | child_process.spawn + stream-json NDJSON | 已验证的双向流 |
| 数据存储 | JSON 文件 (relay.json / state.json / JSONL) | 初期无需数据库 |

## 核心模块设计

### 1. channels/ — 渠道适配

**职责**：接收消息 → 统一路由 → 转发回复

| 渠道 | 文件 | 连接方式 | 消息格式 |
|------|------|---------|---------|
| Telegram | `channels/telegram.ts` | grammy long polling | 文本/图片/语音/文件 |
| 飞书 | `channels/feishu.ts` | Lark SDK WebSocket | 文本/图片/Interactive Card |

功能：
- 租户路由（chat_id/open_id → agent）
- 长文本分片（TG 4096 字符 / 飞书 4000 字符）
- 语音转录（TG STT via CC）
- 图片/文件处理
- 命令处理（/start, /new, /runtime, /yolo 等）
- Session 恢复和 hard-wrap 处理

### 2. gateway.ts — 逻辑 Gateway 层 (v1.1)

**职责**：统一渠道地址绑定、路由策略、投递语义

```typescript
interface ChannelAddress {
  channel: 'telegram_chat' | 'feishu_open_id' | 'feishu_chat_id'
  id: string
  botId?: string
}

interface ChannelBinding {
  address: ChannelAddress
  principal: { userId: string; agentId: string }
}
```

功能：
- ChannelAddress 标准化（TG chat_id / 飞书 open_id / 飞书 chat_id）
- ChannelBinding 注册表（relay.json.bindings[]，含 legacy fallback）
- resolveInboundRoute() 路由决策 + 访问控制
- 配对流程（飞书 /approve）

### 3. pool.ts — CC 进程池

**职责**：管理每个租户的 CC/Codex 子进程生命周期

关键行为：
- **双模式**：pipe 模式 (`claude -p`) + PTY interactive 模式 (`claude --bare`)
- **按需启动**：收到租户首条消息时 spawn
- **Session 持久化**：session-id 绑定 + `--resume` 恢复
- **idle 回收**：30 分钟无活动自动关闭
- **崩溃重启**：exit 事件触发 respawn（最多 3 次/小时）
- **Token fallback**：primary/fallback OAuth 双 token，限流/认证失败自动切换
- **Crash replay**：重启后 replay in-flight 消息，避免静默丢失
- **Hard-wrap 恢复**：context 满时标记 recoveryRequired，/wrap 不发旧 runtime
- **Interactive 模式**：PTY + warmup + submit delay + slash command settle
- **Activity-based timeout**：stdout/stderr 刷新 idle timer（替代固定 timeout）

### 4. conversation-runtime.ts — 会话运行时

**职责**：统一 CC 和 Codex 的在线会话接口

```typescript
interface ConversationRuntimeAdapter {
  spawn(config): Promise<CCProcess>
  resume(process, sessionId): Promise<void>
  send(process, message): void
  kill(process): Promise<void>
  status(process): ProcessStatusInfo
  dispose(process): Promise<void>
}
```

- CC runtime：pipe 模式，stream-json NDJSON 双向通信
- Codex runtime：`codex exec` 模式，JSONL 输出解析
- Runtime preference：per-agent 配置，支持 fallback
- 错误分类与用户提示

### 5. runtime.ts — Batch Runtime

**职责**：scheduler 批量任务的 runtime 编排

- BatchRuntimeAdapter 接口（CC / Codex 两个实现）
- createBatchRuntimeOrchestrator() runtime 选择和 fallback
- cooldown + sticky-until-healthy 机制
- 失败分类和自动切换

### 6. scheduler.ts — Cron 调度器

**职责**：定时任务管理 + harness 集成

功能：
- 从 cron-jobs.json / relay.json 加载 Job 配置
- node-cron 时区感知调度
- 可选的 scheduler 实例级全局 FIFO 并发闸（cron / reconciliation / manual 共用；超限排队）
- 同 workspace Job 串行排队（避免争用）
- Telegram 投递（announce 模式）/ 静默执行（none 模式）
- Bot 命令 CRUD：/jobs, /job_add, /job_del, /job_run
- inject_file：fire 时动态读文件拼接 prompt
- harness dispatch：完成后的 escalation 推送
- batch 默认 1000 turns，cron 可配置 maxTurns
- 每次执行生成 `runId` 并注入 batch 环境；失败 fallback 先核共享终态回执

### 6.1 job-terminal-registry.ts — Run 终态协调器

**职责**：在业务内部 push 与 scheduler fallback 之间提供同一 run 的原子投递预留和回执状态

- 状态：`open → sending → sent|failed`
- `sending/sent` 拒绝同 run 的并发或迟到重试，`failed` 允许 scheduler 兜底
- `runId + owner` 双重校验；同 owner 多 run 并发时不允许无 id 猜测
- 不跨 run 共享 cooldown，不吞掉下一次独立故障

### 7. state.ts — 状态持久化

**职责**：session 状态跨重启恢复

- state.json 原子写入（temp + fsync + os.replace）
- 重启检测（restart-detect.ts）
- Session 恢复 + recovery 标记
- Debounce 持久化（2s）

### 8. delivery.ts — 投递层

**职责**：统一的跨渠道消息投递

```typescript
interface DeliveryRequest {
  channel: ChannelAddress['channel']
  recipient: string
  content: { type: 'text' | 'card' | 'document'; payload: string }
}

interface DeliveryReceipt {
  status: 'delivered' | 'failed' | 'rate_limited'
  externalMessageId?: string
  error?: string
}
```

- deliverTelegramText / deliverTelegramDocument
- deliverFeishuText / deliverFeishuCard

### 9. metering.ts — 用量计量

**职责**：从 CC/Codex 输出提取 usage，记录计费

- 提取 total_cost_usd、input_tokens、output_tokens
- 写入 JSONL 按日分文件
- /usage 命令查询

### 10. http-server.ts — HTTP + WebSocket Server

**职责**：Loopback API（Platform 三步开通）+ Web Terminal 浏览器接管

| 端点 | 协议 | 用途 |
|------|------|------|
| `POST /internal/pairing/approve` | HTTP | 飞书配对确认 |
| `POST /internal/agents/reload` | HTTP | 新增 agent 热加载 |
| `POST /internal/push` | HTTP | 事件驱动推送；`runId + terminal:true` 写 run 终态回执并去重 |
| `GET /internal/health` | HTTP | 健康检查 |
| `GET /terminal/:token` | HTTP | xterm.js 终端页面 |
| `WS /terminal/:token/ws` | WebSocket | 浏览器 ↔ PTY 桥接 |

环境变量：
- `RELAY_HTTP_PORT`：监听端口（未设则不启动 HTTP）
- `RELAY_HTTP_HOST`：绑定地址（默认 `127.0.0.1`，`0.0.0.0` 允许远程）

### 11. web-terminal.ts — Web Terminal（/cc 浏览器接管）

**职责**：让用户通过浏览器接管 CC 会话，无需 SSH 或 Linux 密码

- `TerminalManager`：token 生命周期（UUID v4，10 分钟 TTL）+ PTY spawn + WS 桥接
- `/cc` 命令释放 Relay CC 进程，返回一次性浏览器 URL
- PTY 使用 agent 特定的 provider 配置（`--model` + `--settings`），与飞书 bot 一致
- Linux：`sudo -u {linuxUser}` 租户隔离；macOS：直接 spawn
- 详见 `features/web-terminal/`

环境变量：
- `RELAY_PUBLIC_HOST`：URL 生成的公网 hostname（如 `pulse.example.com`），走 HTTPS 443

## 质量属性场景

### 场景1：响应延迟（性能）

- **刺激**：用户在 Telegram/飞书发送文本消息
- **环境**：正常负载（<=10 bot 并发）
- **响应**：首 token <= 8s 到达用户
- **权衡**：idle 进程需 re-spawn 时延迟增加，resume 可缩短

### 场景2：进程隔离（安全性）

- **刺激**：Agent A 的 CC 进程崩溃或 context 满出
- **环境**：其他 Agent 正常运行
- **响应**：其他 Agent 不受影响，A 自动重启或标记 recovery
- **度量**：跨 Agent 零干扰

### 场景3：Token/Rate Limit 恢复（可用性）

- **刺激**：CC API 限流 429 或认证失败
- **环境**：用户正在对话
- **响应**：自动切换到 fallback token 或 Codex runtime，用户不中断
- **度量**：fallback 切换 < 30s

### 场景4：会话连续性（可靠性）

- **刺激**：Relay 服务重启
- **环境**：多个进行中的对话
- **响应**：重启后通过 state.json 恢复 session 映射，resume 恢复上下文
- **度量**：用户感知为短暂无响应后自动恢复

## 部署架构

### 阿里云（生产）

```
阿里云 VPS (Tailscale exit node: bigbird-sg)
├── systemd: relay.service          ← Relay 主进程, 3 bots
├── /opt/PULSE/                     ← 平台根目录
├── /opt/PULSE/Tenants/             ← 租户工作空间
├── ~/.claude/                      ← CC session 持久化
└── CC v2.1.97 + OAuth Token 注入
```

### 本机（开发 + 生产）

```
macOS (launchd)
├── com.pulse.relay                  ← Relay 主进程, 13 bots
├── ~/Coevolution/Bigbird/PULSE/    ← 开发环境
├── Tenants/                         ← 租户工作空间
└── CC + Codex (Codex canary enabled)
```

资源估算：
- Relay 主进程：~100MB
- 每 CC 子进程：~250MB
- 当前规模：本机 13 bot + 阿里云 3 bot
