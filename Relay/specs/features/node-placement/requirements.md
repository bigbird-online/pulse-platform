---
feature_name: "node-placement"
version: "0.1.0"
created: "2026-08-17T00:00:00+0800"
modified: "2026-08-17T00:00:00+0800"
upstream_dependency: ["project-architecture.md", "features/logical-gateway/requirements.md", "features/agent-scheduler/requirements.md"]
tags: [requirements, l2-spec, placement, relay, scheduler, single-active]
---

# node-placement - 功能需求

## 1. Minimum Viable Spec

一个无秘密、版本化的 placement SSOT 必须显式声明 node、logical agent、scheduler owner ID、delivery bot identity、bot active node、binding active node、job run node 与每个 user/node 的默认 bot。Relay 与 Scheduler 只消费由该 SSOT 和全局 declarative sources 确定性投影且通过 exact preflight 的本节点配置。

`logical agent`、`ownerId` 和 `bot id` 是三个不同命名空间。禁止用同名关系推断映射。没有 delivery bot 的 logical agent 可以运行 `delivery.mode=none` job；任何需要投递的 job 必须有显式 delivery bot，且 bot 与 job 位于同一 node。

## 2. 功能需求

- **FR-P0-001 单一声明源**：schema 与 runtime parser 均拒绝未知字段、重复 ID、未知引用、空 owner mapping、重叠 owner mapping和 unsupported version。credential 只保存 `env:NAME` 引用，不保存值；`active` 文档拒绝 `TODO`/`EXAMPLE` placeholder。
- **FR-P0-002 Exact inventory**：全局 relay source 的 user/bot/binding 以及 cron source 的 job 必须与 SSOT exact match。重复 user/bot/binding/job、动态 `_state`、额外或缺失项均硬错。
- **FR-P0-003 Binding 闭环**：binding principal 必须存在；address/default/allowed bot 必须属于 principal、位于 binding active node，allowed 非空且包含 default。projector 不修剪非法 allowed 列表。
- **FR-P0-004 确定性投影**：相同语义输入必须产生 byte-stable relay/cron JSON。每个 bot、binding、job 只出现在一个 node projection；cron projection version 为 3 并带显式 `runNode`。
- **FR-P0-005 启动/重载 fail closed**：placement 模式要求 SSOT `status=active`、known `RELAY_CURRENT_NODE`、全局 sources 和两个本地 projection exact digest 全部通过，之后才可启动 writer。SIGHUP/preflight 失败保留当前 generation。
- **FR-P0-006 Client 单活**：Telegram long-poll 与 Feishu WebSocket 均按 exact registry reconcile。全部 removals 真 stop 成功后才允许 additions start；stop 失败零 additions；start 失败回退本轮 additions。移除 generation 的共享 runtime/pool/metering listener 必须 inactive，不能继续 outbound。
- **FR-P0-007 Scheduler node ownership**：只注册 `runNode=currentNode` job。owner 必须经显式 ownerId→logical agent→placement 验证，不得 fallback。announce/document delivery bot 必须在 run node 且 live registry 可解析，否则 job error。
- **FR-P0-008 受控写回**：placement 模式禁用直接 `/job_add`、`/job_del` 和 JobStore config mutation。新增 job 只能 staged 同时更新 SSOT 与 global cron source，再重新 project/preflight。
- **FR-P0-009 迁站 fence**：任一 scheduler job running 时拒绝 placement reload，且 router/client/scheduler 零 mutation。迁站必须携带 durable state；新 runNode 定义建立新 activation generation，不 replay 已完成 slot。

## 3. 验收标准

1. 同一 bot 多 node、重复/未知/悬空 owner、extra/missing bot/binding/job/default 均 validation failure。
2. 未知 schema 字段、placeholder active credential、跨 principal 或跨 node binding 均 validation failure。
3. 两个 node projection 的 bot/binding/job 不重叠，union 与 SSOT 完整一致，重复运行 bytes/digest 相同。
4. 删除 Feishu/TG client 会先真 stop；stop failure 不 start；后续 start failure 回退此前 additions；inactive listener 零 outbound。
5. 本地 projection 多一个 bot/binding/job 或少一个声明项时 startup/reload preflight 拒绝。
6. 非本 node job 不注册；unknown owner、缺显式 delivery mapping、delivery bot 跨 node 均硬错且无 fallback。
7. 旧站完成并 durable 写 state 后，新站加载同一 state 不重复执行；旧站仍 running 时 reload 零 mutation 拒绝。

## 4. 排除范围

- 自动故障转移、跨机 lease、分布式 exactly-once、credential 复制/轮换。
- 修改生产 relay/cron、启停服务或实际迁移 node。
- 将 candidate 文档解释成可直接激活的生产配置。
