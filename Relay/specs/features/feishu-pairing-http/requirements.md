---
feature_name: "feishu-pairing-http"
version: "0.1.0"
created: "2026-05-05T13:50:00+08:00"
modified: "2026-05-05T13:50:00+08:00"
changelog:
  - "v0.1.0: 初稿；为 platform-tenant-provisioning v0.3.0 三步流提供 Relay 侧 HTTP 接口与飞书 bot 热加载"
time_basis: "北京时间（Asia/Shanghai，UTC+8）"
upstream_dependency:
  - "specs/features/tenant-onboarding-feishu/requirements.md"
  - "specs/features/feishu-channel/requirements.md"
  - "specs/features/logical-gateway/design.md"
  - "../../../Platform/specs/features/platform-tenant-provisioning/requirements.md"
tags: [requirements, l2-spec, feishu, pairing, http, hot-reload]
spec_root: "Relay/specs"
---

# feishu-pairing-http — 需求

> 在 Relay 内嵌一个 **loopback HTTP 服务**，向 Platform 暴露两个内部接口：(1) 完成飞书配对（接管原 `/approve` 命令的能力），(2) 触发飞书 bot 热加载（不重启 Relay 进程）。本 feature **不**改变会话/路由语义，仅打通三步开通流的 Relay 侧。

## 1. 目标

- **G1**：当 Platform 完成 Step 2 写入 `relay.json` 后，Relay 须在 **≤ 1 s** 内 spawn 该新 feishu agent 的 WS client，并把它注册到现有 router；**不**得中断其他活跃 bot 的连接或会话。
- **G2**：Platform Step 3 拿到用户粘贴的 6 位配对码后，可经 HTTP 让 Relay 完成 `bindings[]` 落库，等价于运营在已绑定 bot 内发 `/approve <code>`。
- **G3**：HTTP 服务仅监听 **127.0.0.1**（loopback）；端口可配；**无**额外鉴权（依赖网络层隔离）；可被禁用（默认禁用，需显式 env 开启，避免空白 / 端口暴露）。

## 2. 边界（非目标）

- **不**对外暴露 HTTP；任何远端访问由上游网关/反向代理负责。
- **不**做飞书 OpenAPI 调用（凭证校验在 Platform 侧）。
- **不**做 TG / 其他通道 bot 的热加载（本期仅飞书）。
- **不**替代 `/approve` 命令；该命令仍在 Feishu 通道内可用。

## 3. 功能需求

| ID | 描述 | 优先级 |
|----|------|--------|
| FR-RH-001 | **HTTP 服务**：`RELAY_HTTP_PORT`（数字）显式设置时启动 loopback HTTP；未设则不启动（向后兼容） | P0 |
| FR-RH-002 | **POST `/internal/pairing/approve`**：body `{ code, principalId, agentId }`；行为等价 `/approve <code>` 命令，验证配对码归属 agent，写入 binding 并刷新 router；返回 `200 { ok: true, openId, bindingId }` 或 4xx（`code_unknown`/`agent_mismatch`/`config_missing_agent`） | P0 |
| FR-RH-003 | **POST `/internal/agents/reload`**：重读 `relay.json` 并对**新增**的 feishu agent 启动 WS client（已存在的保持不动），返回 `200 { added: number, total: number }` | P0 |
| FR-RH-004 | **GET `/internal/health`**：返回 `{ status: 'ok', uptime: number, feishuBots: number }`；用于 Platform 探活 | P1 |
| FR-RH-005 | **审计日志**：所有 HTTP 调用 structured log（含 `code` 前 2 位脱敏、`agentId`、`status`） | P0 |

## 4. 验收标准

- **AC-RH-001**：未设 `RELAY_HTTP_PORT` 时 Relay 启动后无 8788/任何额外端口监听（`lsof -iTCP -sTCP:LISTEN -P` 验证）。
- **AC-RH-002**：设 `RELAY_HTTP_PORT=8788` 时 `curl http://127.0.0.1:8788/internal/health` 返回 200；外网 IP 直连超时（仅监听 127.0.0.1）。
- **AC-RH-003**：在 `relay.json` 中追加一个新 feishu agent → `curl -X POST .../internal/agents/reload` → 1s 内日志出现 `Feishu bot started`，新 bot 可接收消息；其它 bot 连接未中断（pid 不变）。
- **AC-RH-004**：用户在新 bot 加入后发任意消息 → bot 回 6 位配对码 → `curl -X POST .../internal/pairing/approve` → 200 + `relay.json.bindings[]` 多一条 `status='active'`、`address.value=<openId>`；下一次发消息直接收到 AI 回复。
- **AC-RH-005**：错误配对码 → 404 `code_unknown`；正确配对码但 `agentId` 不匹配 → 409 `agent_mismatch`；缺 agent → 404 `config_missing_agent`。

## 5. 关联

- 实现：`Relay/src/http-server.ts`（新）、`Relay/src/channels/feishu.ts`（导出 `claimPendingPairing` / `writeOpenIdToConfig`）、`Relay/src/index.ts`（启动 + 热加载钩子）。
- 平台调用方：`Platform/src/relay-pairing-client.ts`（详见 `Platform/specs/features/platform-tenant-provisioning/design.md` §1.5 / §1.12）。
