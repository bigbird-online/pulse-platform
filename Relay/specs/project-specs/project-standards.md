---
version: "1.2.0"
created: "2026-04-06"
modified: "2026-05-12"
changelog:
  - "v1.2.0: 150-spec-sync — 新增 vision-proxy feature spec；目录结构补 vision-proxy.ts；测试数更新"
  - "v1.1.0: 150-spec-sync 重写，对齐当前目录结构和技术栈"
  - "v0.1.0: 初始版本"
---

# Relay - 项目规范

## 技术栈

| 项目 | 选型 |
|------|------|
| 运行时 | Node.js >= 18 (ES Modules) |
| 语言 | TypeScript 6.x，strict 模式 |
| 包管理 | npm |
| 构建 | tsc 直编 |
| 测试 | vitest ^4.1 + @vitest/coverage-v8 |
| TG Bot | grammy ^1.42 |
| 飞书 SDK | @larksuiteoapi/node-sdk ^1.61 |
| 调度 | node-cron ^4.2 |
| WebSocket | ws ^8.20 |
| CC 通信 | child_process.spawn + stream-json NDJSON |

## 目录结构

```
Relay/
├── specs/
│   ├── project-specs/              # L1 项目层 Spec
│   │   ├── project-architecture.md
│   │   ├── project-roadmap.md
│   │   ├── project-standards.md
│   │   └── project-vision.md
│   ├── features/                   # L2 功能层 Spec
│   │   ├── logical-gateway/
│   │   ├── conversation-runtime/
│   │   ├── cc-process-pool/
│   │   ├── telegram-channel/
│   │   ├── feishu-channel/
│   │   ├── session-resilience/
│   │   ├── session-management/
│   │   ├── runtime-fallback/
│   │   ├── token-fallback/
│   │   ├── bot-commands/
│   │   ├── agent-scheduler/
│   │   ├── command-feedback/
│   │   ├── metering/
│   │   └── vision-proxy/
│   └── business-flow.md
├── src/
│   ├── index.ts                    # 主入口 + startRelay()
│   ├── gateway.ts                  # 逻辑 Gateway（ChannelAddress/Binding/Route）
│   ├── delivery.ts                 # 投递层（DeliveryRequest/Receipt）
│   ├── pool.ts                     # CC 进程池（spawn/resume/kill/token fallback）
│   ├── conversation-runtime.ts     # 会话运行时管理器
│   ├── codex-conversation-runtime.ts # Codex runtime 适配
│   ├── runtime.ts                  # Batch runtime（CC/Codex orchestrator）
│   ├── scheduler.ts                # Cron 调度 + harness dispatch
│   ├── state.ts                    # 状态持久化
│   ├── commands.ts                 # Bot 命令处理
│   ├── command-menu.ts             # 命令菜单注册
│   ├── job-commands.ts             # Job CRUD 命令
│   ├── job-store.ts                # Job 持久化
│   ├── metering.ts                 # 用量计量
│   ├── router.ts                   # 租户路由（legacy + binding）
│   ├── restart-detect.ts           # 重启检测
│   ├── session-wrap-guard.ts       # Session wrap guard
│   ├── stream.ts                   # NDJSON 流处理
│   ├── stt.ts                      # 语音转录
│   ├── cli.ts                      # CLI 入口
│   ├── types.ts                    # 全局类型定义
│   ├── vision-proxy.ts             # 辅助视觉模型代理（图片→文本描述）
│   └── channels/
│       ├── telegram.ts             # TG Bot 适配
│       └── feishu.ts               # 飞书适配
├── tests/                          # 26 个测试文件（含 vision-proxy.test.ts）
├── config/
│   ├── relay.example.json          # 配置示例
│   ├── cron-jobs.sample.json       # Cron 任务示例
│   └── tenants.example.json        # 租户配置示例
├── scripts/
│   ├── claude_pty_bridge.py        # PTY 桥接
│   ├── integration-test.ts         # 集成测试
│   ├── relay-jobs.ts               # Job 管理
│   └── run-duke.ts                 # Duke 运行
├── package.json
├── tsconfig.json
└── vitest.config.ts
```

## 代码规范

- **命名**：文件 kebab-case，类/接口 PascalCase，函数/变量 camelCase
- **导出**：named export，不用 default export
- **错误处理**：只在系统边界做 try-catch，内部逻辑让错误冒泡
- **日志**：console.log/warn/error + 结构化 JSON（`{ts, level, msg, ...}`）
- **异步**：全部 async/await
- **类型**：strict: true，零 any，接口定义集中在 types.ts
- **模块划分**：单文件 <= 300 行（pool.ts 例外，约 900 行，因进程池逻辑密集）

## Commit 规范

格式：`<type>(pulse): <description>` 或 `<type>(pulse/<scope>): <description>`

类型：`feat | fix | docs | style | refactor | test | chore`

scope 示例：`pulse/gateway`, `pulse/pool`, `pulse/telegram`, `pulse/feishu`

## 追溯机制

- Feature spec 路径：`specs/features/{feature-name}/`
- 每个 feature 包含：requirements.md → design.md → tasks.md
- Task 引用 design，design 引用 requirements
- 提交后修改 Relay 源码须同步更新对应 feature spec（150-spec-sync）

## 质量标准

| 指标 | 标准 |
|------|------|
| 类型安全 | strict: true，零 any |
| 测试 | vitest，26 files / 593 tests，核心模块 >= 80% 覆盖 |
| 单文件行数 | <= 300 行（pool.ts 例外） |
| 生产依赖 | grammy + @larksuiteoapi/node-sdk + node-cron + ws = 4 个 |
| 构建 | `npx tsc` 零错误 |
| 修改后验证 | typecheck + `npm test` + 重启 Relay |

## 安全规范

- **Token 管理**：TG bot token / 飞书 app secret / CC OAuth token 通过环境变量注入，不进 git
- **租户隔离**：每 agent 独立 CC 进程 + 独立 workspace
- **工具白名单**：`--allowed-tools` 限制 agent 可用工具
- **Codex 提权**：`/yolo` 仅当前 session 生效，不持久化，重启后失效
- **原子写入**：state/job/harness 使用 temp+fsync+os.replace 防半写
