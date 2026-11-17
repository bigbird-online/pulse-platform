---
feature_name: "feishu-pairing-http"
version: "0.1.0"
created: "2026-05-05T13:50:00+08:00"
modified: "2026-05-05T13:50:00+08:00"
changelog:
  - "v0.1.0: 初稿；HTTP 路由、热加载策略、错误模型"
upstream_dependency: ["requirements.md"]
tags: [design, l2-spec, feishu, pairing, http, hot-reload]
spec_root: "Relay/specs"
---

# feishu-pairing-http — 技术设计

## 1. 模块拆分

| 模块 | 职责 |
|------|------|
| `src/http-server.ts`（新） | 以 `node:http` 起 loopback server；按 `RELAY_HTTP_PORT` 启动；路由分发；body 限 16 KiB |
| `src/channels/feishu.ts`（修改） | 导出 `claimPendingPairing`（按 code 弹出 pending）、`writeOpenIdToConfig`（已存在）；新增 `attachFeishuBots(...)`（仅启动**新**增 agent；幂等） |
| `src/index.ts`（修改） | 持有 `feishuBots` 句柄；`feishuBots.reload()` → 重读 `relay.json` 并 attach 新 agent；`startHttpServerIfEnabled(...)` |

## 2. 路由

```
POST /internal/pairing/approve
  body: { code: string; principalId: string; agentId: string }
  200:  { ok: true, openId, bindingId }
  404:  { ok: false, error: 'code_unknown' }
  409:  { ok: false, error: 'agent_mismatch', expectedAgentId }
  404:  { ok: false, error: 'config_missing_agent' }

POST /internal/agents/reload
  body: 空 / 忽略
  200:  { ok: true, added: number, total: number, addedIds: string[] }
  500:  { ok: false, error: 'reload_failed', message }

GET /internal/health
  200: { status: 'ok', uptime, feishuBots }
```

- **错误格式**：`{ ok: false, error: 'snake_case_code', ... }`；HTTP status 与 `error` 一一对应。
- **限速 / 鉴权**：本期无；loopback only + 上游网络层兜底。

## 3. 热加载策略（FR-RH-003）

- `feishuBots.reload()` 内部：
  1. 重读 `relay.json` 并 `validateConfig`。
  2. `getAllFeishuBots()` → 与 `runningAgentIds: Set<string>` 取差集，得 **`toAdd`**。
  3. 对每个 `toAdd` 启 WS client（沿用 `lark.WSClient` + 既有事件订阅闭包）。
  4. 已存在 agent **不**重启；缺失（`relay.json` 删除）的 agent 本期**不**主动 stop（避免误杀；后续工单跟进）。
- **并发保护**：reload 用一个 `Promise` 队列串行；并发请求等待前一次完成。
- **错误隔离**：单个 bot 启动失败不影响其它；返回 `addedIds` 只列成功的，`error.firstFailure` 列首失败原因。

## 4. 配对接口（FR-RH-002）

- 复用 `claimPendingPairing(code)` → 取 pending（带 `agentId` + `openId`）。
- 校验 `pending.agentId === body.agentId`；不匹配 409。
- 调用 `writeOpenIdToConfig(configPath, principalId, pending.agentId, pending.openId, 'platform-http', router)`：
  - 已存在的实现会写 binding `status='active'`、`address.value=openId`、`approvedBy='platform-http'`，并 `router.upsertGatewayBinding` 刷新内存。
- 成功返回 `{ ok: true, openId: pending.openId, bindingId: 'binding:...' }`。

## 5. 热加载实现细节

- 当前 `createFeishuBots(deps).start()` 一次性启动所有 agent。改造：
  - `createFeishuBots` 改为返回 `{ start, stop, attach(agent), runningIds }`。
  - `start()` 等价于 `for (agent of getAllFeishuBots) attach(agent)`。
  - `attach(agent)` 幂等：若 `runningIds.has(agent.id)` 直接 return。
- 这个改造**不破坏**现有调用点（仍可 `await feishuBots.start()`）。

## 6. 安全

- HTTP 默认禁用；`RELAY_HTTP_PORT` 必须为数字字符串才启动。
- `server.listen(port, '127.0.0.1')` 显式绑定 loopback。
- body 限 16 KiB；JSON 解析失败 400。
- 日志中 `code` 仅露前 2 位（`AB****`）；`openId` 仅露前 8 位。

## 7. 测试

- `tests/http-server.test.ts`：用 `supertest`（或裸 fetch + `server.address()`）覆盖 4 个路由的 happy / 4xx 路径；mock `claimPendingPairing` / `writeOpenIdToConfig`。
- `tests/feishu-hot-reload.test.ts`：mock `lark.WSClient`，断言 `attach` 幂等、并发 reload 串行。

## 8. 待决

- 后续可加简单 Bearer 鉴权（`RELAY_HTTP_TOKEN`）；本期不做。
- 是否需要在 reload 时主动停掉被删除的 agent —— 留待 ops 反馈后追加。
