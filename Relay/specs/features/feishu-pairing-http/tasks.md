---
feature_name: "feishu-pairing-http"
version: "0.2.0"
created: "2026-05-05T13:50:00+08:00"
modified: "2026-05-29T18:15:00+08:00"
changelog:
  - "v0.2.0: 150-spec-sync — 标记实际实现状态；RH-003/RH-004 已完成，RH-001/RH-002 部分实现，RH-005/RH-006 待办"
  - "v0.1.0: 初稿"
upstream_dependency: ["requirements.md", "design.md"]
tags: [tasks, l2-spec, feishu, pairing, http]
spec_root: "Relay/specs"
---

# feishu-pairing-http — 任务拆分

| ID | 任务 | 状态 | 说明 |
|----|------|------|------|
| RH-001 | feishu.ts 导出 `claimPendingPairing`、保留 `writeOpenIdToConfig`；包内 re-export | [~] | `writeOpenIdToConfig` 已导出；`claimPendingPairing` 未导出（内部使用），实际使用 `peekPendingPairingForHttp` + `consumePendingPairingForHttp` 替代 |
| RH-002 | feishu.ts 暴露 `attach(agent)` 幂等方法；`createFeishuBots` 返回 `{ start, stop, attach, runningIds }` | [~] | 无独立 `attach()` 方法；`reload()` 已实现类似功能（idempotent spawn）；`runningAgentIds()` 已实现 |
| RH-003 | 新建 `src/http-server.ts`：路由 4 个；body 限 16 KiB；loopback 绑定；启动失败抛错 | [x] | 已实现：3 个路由（approve, reload, health），127.0.0.1 绑定，16KiB body 限制 |
| RH-004 | `src/index.ts` 在所有 bot start 后调用 `startHttpServerIfEnabled({ feishuBots, configPath, router })`；`stop` 时关闭 | [x] | 已实现：`startRelayHttpServerIfEnabled` 在 bot start 后调用，stop 时关闭 |
| RH-005 | 测试：`tests/http-server.test.ts` + `tests/feishu-hot-reload.test.ts` | [ ] | 待实现 |
| RH-006 | UAT：`curl` 跑 §requirements §4 五条 AC | [ ] | 待实现 |

---

*Spec：`Relay/specs/features/feishu-pairing-http/` · 代码：`Relay/src/`*
