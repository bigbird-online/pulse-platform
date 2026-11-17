---
feature_name: "node-placement"
version: "0.1.0"
created: "2026-08-17T00:00:00+0800"
modified: "2026-08-17T00:00:00+0800"
upstream_dependency: ["features/node-placement/requirements.md"]
tags: [design, l2-spec, placement, fail-closed]
---

# node-placement - 技术设计

## 1. Contract

```text
node-placement.json (no secrets, desired ownership)
       + relay source (full credential-bearing runtime declarations)
       + cron source (full declarative job definitions, no _state)
                         |
                  validate exact inventory
                         |
                 deterministic project(node)
                         |
                relay.local + cron.local(v3)
                         |
          startup/reload exact-byte preflight
                         |
       Router + TG/Feishu registries + Scheduler
```

Placement 保存 identity 与 ownership，不复制 credential value 或完整 runtime job。`credentialRefs` 只说明激活需要的外部 env contract；投影仍从受控 relay source 保留 credential-bearing agent entry。

### 显式映射

- `agents[].id`：logical agent。
- `agents[].ownerIds[]`：cron source `owner` 可用的历史/当前 ID；必填、全局不重叠。
- `agents[].deliveryBotId`：可选，只有纯 `delivery=none` agent 可缺失。
- `bots[].id`：channel credential/client identity；每个 bot 恰有一个 `activeNode`。
- `jobs[].ownerId` 与 source owner exact equal；验证先 owner map，再 logical agent placements。

## 2. Validator 与 projector

Runtime parser 与 checked JSON Schema 都是 closed-world contract。parser 对 root 和每类 entry 做 allowed-key check，并执行 JSON Schema 难以表达的全局唯一性、引用、principal、node 共置和 inventory exactness。

Projector 排序所有集合后输出 canonical JSON。它只按声明的 active/run node 选择完整对象，不静默修补 invalid binding。`preflight` 重新从 SSOT + sources 计算 expected bytes，与本地文件 byte-for-byte 比较。

CLI 的 `project`/`stage-add-job` 只写不存在的 staging target：同目录 temporary file、fsync、rename，绝不替换 live path。live replacement、previous-generation snapshot 与恢复由 runbook 明确控制。

## 3. Runtime lifecycle

Startup 在任何 TG poll、Feishu WS 或 Scheduler start 前执行 placement preflight。Reload 顺序为：preflight → assert no running jobs → stop scheduler registration → router reload → TG exact reconcile → Feishu exact reconcile → scheduler reload。

每个 registry 的 stop phase 先完成；任一 stop failure 会保留失败的旧 entry 并阻止所有 additions。start phase 失败会 stop/delete 本轮已新增 entry。client 的 `active` gate 在 stop 前关闭，使无法 unsubscribe 的 shared listeners 成为 inert callback。

这个机制保证单进程受控 generation 切换，不宣称跨进程 lease。跨 node 迁移仍必须用 runbook 的旧 writer negative evidence fence。

## 4. Scheduler consistency

Projection version 3 携带 `runNode`。placement 模式的 local filter 与 owner validator同时执行；delivery client 只按显式 `deliveryBotId` lookup。direct CRUD 被禁止，避免 JobStore v2 write 令 local projection 与 SSOT/source 漂移。

Runtime state 独立于 declarative source。切换前必须持久化并复制同一 state；`runNode` 变化令 definition hash 改变，新 node 以切换时刻为 activation floor，因此不 replay 旧 definition slot。此处不是分布式 exactly-once：旧执行未结束时由 reload running-job gate 拒绝。

## 5. Failure behavior

- validation/preflight 失败：零 writer mutation。
- running job：HTTP reload 失败，router/registry/scheduler 均不变。
- client stop/start 失败：返回失败；不启动可能与旧 writer 重叠的新 generation。
- scheduler delivery mapping/client 缺失：job aggregate status `error`，不 fallback 到首个或同名 bot。
