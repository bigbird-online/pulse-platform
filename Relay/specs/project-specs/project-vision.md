---
version: "1.1.3"
created: "2026-04-06"
modified: "2026-05-01"
changelog:
  - "v1.1.3: 与 PULSE 关系段补充 Platform L1 互链"
  - "v1.1.2: 澄清「不开放注册」— 允许手机白名单 + 先付费后自动 binding；无邀请码；规格见 tenant-onboarding-feishu"
  - "v1.1.1: G1-6 BADPulse 替换收尾——新增 Out of Scope（继承自 BADPulse 已主动收缩）段，明示 sidecar/Factory Director/manifest/L0-L4 分发等不再继承"
  - "v1.1.0: 150-spec-sync 重写，对齐 CC+Relay+harness 三层架构定位"
  - "v0.1.0: 初始版本，基于 PULSE 架构转向决策（OC → CC）"
---

# Relay - 项目愿景

## 产品定位

Relay 是 PULSE 平台的**多租户 AI Agent 中继网关**，将 Claude Code / Codex 的 agent 能力通过 Telegram、飞书等消息渠道共享给多个租户。

**一句话定义**：Relay = 多渠道消息网关 + Agent 路由 + CC/Codex 进程池 + Cron 调度 + agent-harness 协调。

## 架构定位

Relay 是 PULSE 三层架构的执行层（S1）和调度层（S2）：

```
PULSE 平台
├── Relay（S1 执行 + S2 调度）        ← TypeScript，消息网关 + 进程池 + cron
├── agent-harness（S2 协调）           ← Python，wi/handoff/dispatcher/escalation
├── CC / Codex（推理引擎）             ← CLI 子进程
├── pulse-space/（Pulse Agent 工作区）  ← Agent 身份 + 记忆 + ops
├── Tenants/（租户工作空间）            ← 每 agent 独立 workspace
├── Doc/（共享规则）                    ← spec-driven-developer 规则集
├── skills/（技能库）                   ← spec-driven-engineering 等
└── _archived/BADPulse/                ← OC 时代遗留，已归档
```

## 目标用户

**高级外行 + 开发者**：

1. **生活助手用户**：通过 TG/飞书使用预配置 OA（Emi/Aki/Asobi 等），不需了解技术
2. **开发者/管理员**：通过 Relay 管理 Agent cron 任务、查看用量、运维平台
3. **Agent 分身**：通过 scheduler + harness 实现无人监督的自主任务执行

## 价值主张

| 对比维度 | 用户直接用 CC | 通过 Relay 使用 |
|----------|-------------|----------------|
| 入口 | 终端 CLI | TG / 飞书（已有 app） |
| 学习成本 | 需理解 CLI、工作目录 | 零，直接发消息 |
| 人格体验 | 通用 coding assistant | 预配置的专属 OA |
| 持续性 | 用户主动打开 | Agent cron 主动触达 |
| 多模型 | 手动切换 | 自动 fallback（CC → Codex → GPT） |
| 协作 | 无 | agent-harness wi/handoff/escalation |
| 管理 | 用户自行管理 | 平台统一管理 |

**核心价值**："打开 Telegram 就能用的 AI 生活助手 + 无人监督的 Agent 自治平台"。

## 不做什么

- 不做通用 agent 编排平台（CC 已经是）
- 不做模型无关（主力 Claude，fallback 到 Codex/GPT 仅作降级）
- 不做 Web UI（消息渠道是唯一入口，v1.x 不含 Web）
- 不做企业级 SaaS（初期服务 PULSE 社区小规模用户）
- 不让 Relay 替 Agent 做判断（harness 是管道不是 adapter）

## Out of Scope（继承自 BADPulse 已主动收缩）

> 2026-05-01 G1-6 收尾：以下能力是 BADPulse（OC + Docker Sandbox 时代）曾承诺但
> 在新架构下**不再继承**的，避免被误以为只是"还没做"。决议见
> `pulse-space/memo/decision-2026-05-01-badpulse-replacement-closeout.md`。

- **per-tenant Docker sandbox 隔离**：被 per-Agent CC 子进程 + workspace cwd 软隔离取代
- **Pulse-{tenant} / Amanda-{tenant} sidecar 模型**：sandbox 已不存在；类似职责由 agent-harness 中心化承担
- **Factory Director（OA 模板 fork 流水线）**：被 `skills/ws-builder/` 的人工 + 模板模式替代
- **`distribution-manifest.yaml` + VolumeDeployer 同构基线分发**：已归档至 `Bigbird/_archived/BADPulse/`，无替代 runtime
- **L0-L4 规则工业体系分发给工作坊会员**：业务定位已由"工作坊会员"收缩为"PULSE 社区小规模用户"
- **租户自助注册（开放注册）**：与 BADPulse vision 中 *永不含开放注册* 保持一致——此处 **开放** 指 **匿名全网自助注册**（无准入、无付费闸）。
- **澄清（2026-05-01，与平台商业路径对齐）**：不排除在 **手机号白名单** 与 **注册流内先完成支付** 的前提下，由平台 **自动开户** 并 **自动写入 Relay/`bindings[]`（自动 binding）**；**不使用邀请码**；**一户一飞书自建应用**；用户身份以 **已验证手机号** 为准，**不兼容**「飞书登录态无法关联到该手机号」的客户端路径。L2 见 `specs/features/tenant-onboarding-feishu/requirements.md`。

如未来面向工作坊会员重新开放分发能力，应基于 ws-builder + git 等 Relay 时代的轻量
机制重新设计，**不应**回去复活 VolumeDeployer / Subtractive manifest 体系。

## 成功指标

| 指标 | v1.1 现状 | v1.2 目标 | v2.0 目标 |
|------|----------|----------|----------|
| 活跃 bot | 16（13 本机 + 3 阿里云） | 20 | 50 |
| 消息延迟 | 首 token <= 8s | <= 6s | <= 5s |
| 可用性 | 99%（手动恢复） | 99.5%（自动恢复） | 99.9%（自动 + 监控） |
| 测试覆盖 | 260+ tests | 300+ tests | 500+ tests |
| spec 一致性 | drift 已识别 | drift = 0 | drift = 0 + CI 检查 |
| 多渠道 | TG + 飞书 | TG + 飞书 + inline keyboard | + Web UI |

## 与 PULSE 平台的关系

Relay 是 PULSE 的运行时组件，替代已归档的 BADPulse：

- **BADPulse**（已归档）：OC + Docker Sandbox 时代的 Python 平台核心
- **Relay**（当前）：CC + TypeScript + agent-harness 时代的网关

迁移路径已完成（2026-04-09），BADPulse 不再参与任何生产或开发流程。

**会员准入与租户开通**（白名单、支付、写 `relay.json` / `bindings[]`）由并列产品 **`Platform`** 负责；其 **L1** 见 `Platform/specs/project-specs/project-vision.md`。Relay 继续负责本文件定义的网关与运行时能力。
