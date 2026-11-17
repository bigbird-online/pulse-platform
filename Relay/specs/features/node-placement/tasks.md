---
feature_name: "node-placement"
version: "0.1.0"
created: "2026-08-17T00:00:00+0800"
modified: "2026-08-17T00:00:00+0800"
upstream_dependency: ["features/node-placement/requirements.md", "features/node-placement/design.md"]
tags: [tasks, l2-spec, placement]
---

# node-placement - 任务与证据

- [x] T1：closed schema、runtime parser、无秘密 candidate/example。
- [x] T2：exact source inventory validator、canonical per-node projector、startup/reload preflight context。
- [x] T3：staging-only validate/project/preflight/stage-add-job CLI。
- [x] T4：Relay current-node router/bot/binding projection与 TG/Feishu exact registry lifecycle。
- [x] T5：Scheduler runNode、explicit owner/delivery bot mapping、direct CRUD fail closed、version 3 projection。
- [x] T6：schema/source/project/preflight/client/scheduler/reload 反例自动测试。
- [x] T7：受控迁移、previous-generation snapshot 与回退 runbook。
- [ ] T8：生产 credential ref 对照、source binding 收敛、candidate→active 审核（本切片禁止生产操作）。
- [ ] T9：双 node 演练、真实 writer negative evidence 与业务 UAT（需独立生产变更授权）。
