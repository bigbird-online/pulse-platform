---
version: "1.1.3"
created: "2026-04-06"
modified: "2026-05-02"
changelog:
  - "v1.1.3: 飞书 chat_id binding 冻结至语道 Harness 后复盘"
  - "v1.1.2: v1.2 段与 feishu-channel/metering 现状对齐（避免「Feishu P1 未做」误判）"
  - "v1.1.1: G1-6 BADPulse 替换收尾——v1.3/v2.0 段追加收缩说明，标注 Web UI / 自助注册 / 企业级 SaaS 类条目为 Out of Scope"
---

# Relay - 项目路线图

## 已完成版本

### v0.1 — 最小可用（已完成 2026-04-08）

- CC 子进程管理（spawn/kill/crash restart/idle cleanup）
- Telegram 消息收发（grammy long polling）
- 租户路由（chat_id → agent → workspace）
- 会话持久化（session-id + resume）
- 用量计量（JSONL）

### v0.2 — 多租户 + 飞书（已完成 2026-04-12）

- 多租户并发路由
- 飞书渠道（Lark SDK WebSocket + Interactive Card）
- 配对流程（/approve）
- 长文本分片

### v0.3 — Cron 调度 + harness 集成（已完成 2026-04-18）

- Cron 调度器（node-cron）
- agent-harness dispatch 集成
- inject_file 动态文件注入
- Bot 命令 CRUD（/jobs, /job_add, /job_del, /job_run）

### v0.4 — 稳定化 + Conversation Runtime（已完成 2026-04-22）

- Codex conversation runtime
- Runtime preference + fallback
- Token fallback（primary/fallback OAuth）
- Batch runtime orchestrator
- CC interactive PTY 模式

### v0.5 — 生产修复（已完成 2026-04-26）

- TG timeout 改为 activity-based idle timeout
- CC TG context compaction/settings 继承修复
- Context-full hard-wrap recovery
- `/runtime` / `/yolo` 自助切换
- Amanda duplicate cron push dedup
- TG escalation 降噪/中文格式化
- 夜巡 maxTurns 提升到 100

### v1.0 — 三层闭环（已完成 2026-04-25）

- CC + Relay + agent-harness 三层架构闭环运行
- 13 bot 本机 + 3 bot 阿里云
- S1/S2 稳定化收敛
- 全量测试 20+ files / 260+ tests

### v1.1 — Logical Gateway（进行中）

**目标**：内部逻辑 Gateway 层，统一多渠道身份绑定、路由和投递。

| 阶段 | 内容 | 状态 |
|------|------|------|
| P0 | Gateway Event / ChannelAddress / ChannelBinding contract | ✅ |
| P0 | resolveInboundRoute() 路由决策 | ✅ |
| P0 | createGatewayBindingView() 配置视图 | ✅ |
| P0 | DeliveryRequest / DeliveryReceipt 语义 | ✅ |
| P0 | relay.json.bindings[] schema | ✅ |
| P0 | Legacy-derived binding fallback | ✅ |
| P0 | Feishu /approve write path | ✅ |
| P0 | Feishu 双通道 session 隔离 | ✅ |
| P1 | TG chat_id binding write path | 待定 |
| P1 | 飞书 chat_id binding | **冻结**（至「语道」Harness 后复盘；2026-05-02 不立项） |
| P1 | 多 TG chat_id per user | 待定 |
| P1 | Web UI channel | 未开始 |
| P2 | 独立 Gateway 服务拆分 | 未开始 |

## 后续版本规划

### v1.2 — 工程加固

- Gateway 完全集成到主流程
- pulse-trace（job/handoff/agent/session 证据链追踪）
- pulse-verify（统一 build/test/dist 检查）
- Metering v0.2（月度汇总 + 预算检查；当前为 JSONL + 软告警基线，见 `features/metering/tasks.md`）
- 飞书 P1 增强（Streaming / post / file / audio 等）——见 `features/feishu-channel/requirements.md` 各 FR 状态，**大部分已标「已实现」**

### v1.3 — 多模型 + 企业级

> ⚠️ **2026-05-01 收缩说明（G1-6）**：本段下列条目已被 vision 收缩为 *Out of Scope*，
> 仅保留为历史参考；如未来重新评估再单独决议：
> - **Web UI channel**：与 vision *"v1.x 不含 Web UI"* 直接冲突
> - **租户自助注册**：与 vision *"不开放注册"* 一致，无真实场景驱动
>
> 仍可推进、但不在当前周期：非 Claude 模型 runtime（已部分覆盖）、OA 人格模板库、SQLite 数据迁移。

- 非 Claude 模型 runtime（GPT batch fallback 已有）
- 租户自助注册（通过 bot 对话）
- OA 人格模板库
- Web UI channel
- SQLite 数据存储迁移（从 JSON 文件）

### v2.0 — 平台产品化

> ⚠️ **2026-05-01 收缩说明（G1-6）**：本段整体与 vision *"不做企业级 SaaS"* 立场冲突，
> 默认 *Out of Scope*；保留为长期占位，但**不应作为当前规划依据**。
> 触发重新评估的条件见本文件末 *"退出条件"*。

- 独立 Gateway 服务（从 Relay 拆分）
- 企业级多租户（RBAC、配额、审计日志）
- 用量报表 / 可视化
- 租户 SMB 共享
- CI/CD pipeline

## 退出条件

以下任一信号出现时重新评估 Relay 项目：

- CC 原生支持多租户 + messaging channel 路由（Relay 变得多余）
- Anthropic 大幅调整 API 定价导致经济性不成立
- CC stream-json 协议发生破坏性变更且无替代方案
- 连续 3 个月无活跃租户
