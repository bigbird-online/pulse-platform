# PULSE 适配器

PULSE 是 `spec-driven-engineering` 的应用域，不是 skill 主体。

## PULSE 的 MVS

| MVS 字段 | PULSE 含义 |
|----------|------------|
| 目标 | 修复或改进哪条 S1/S2 链路 |
| 边界 | 不改变哪些 runtime、scheduler、handoff、policy、delivery 语义 |
| 上下文 | Relay / harness / `.harness` / cron / logs / JSONL / specs |
| 验收 | 测试、fake runtime、真实 cron/job/handoff/escalation 证据 |
| 风险 | 是否涉及生产任务、静默失败、跨机器、发布重启、数据状态 |

## S1 执行证据

S1 execution 可信时，任务能被追踪为：

```text
trigger
-> authorization / ownership
-> queue or overlap guard
-> runtime selection
-> process execution
-> output extraction
-> delivery or persisted result
-> failure classification
-> escalation when unrecoverable
```

常见证据：

- cron config 含 expected job、workspace、timeout、runtime preference
- Relay log 有 `job started` 和 `job finished`
- attempted runtimes 符合预期 fallback
- output artifact 存在且时间戳匹配 job run
- Telegram delivery 成功，或明确 `delivery=none`
- failure path 留下结构化日志或 escalation

假成功信号：

- exit code 0 但没有 terminal assistant turn
- `status=ok` 但没有 artifact、delivery 或 state transition
- runtime failure 后没有尝试应有 fallback
- CLI path 成功但 cron path 语义不同
- CLI/Codex 直接探针成功，但 TG/cron/scheduler 等原始入口没有验证同一能力

## S2 协调证据

S2 coordination 可信时，工作能被追踪为：

```text
scan
-> hit classification
-> policy decision
-> lock acquisition
-> handoff / autonomous session / escalation
-> event append
-> dedup / retry / audit
```

常见证据：

- handoff state transition 存在
- work-item owner 清楚
- lock conflict 被记录为 skipped、deferred、queued 或 escalated
- dispatch event 记录 action 和 disposition
- repeated hit suppressed 有 dedup state 可查
- escalation push failure 自身可查询

## PULSE 最小真实验收

| 变更类型 | 最小真实验收 |
|----------|--------------|
| scheduler execution | temporary lightweight cron probe 或 manual `runJob` using scheduler path |
| runtime fallback | fake runtime attempted order + 必要时轻量真实 job |
| PTY output extraction | JSONL fixture/unit test + fake `CLAUDE_BIN` end-to-end |
| pool interactive behavior | targeted pool test + Relay startup log after build/restart |
| scan/dispatch dedup | dispatcher test + `.harness/scan-reports/dedup-state.json` 或 dispatch event |
| lock scheduling | queue behavior unit test + timing-dependent real cron observation |
| release/restart | build output、`dist` inspection、launchd/systemd status、startup logs |
| permission / sandbox / writable root | command builder or contract test + isolated CLI probe + original entrypoint UAT + target artifact/state |

## PULSE 发布证据清单

高风险 Ship & Sync 至少记录：

- build/typecheck/test 结果
- dist 或部署产物是否包含预期代码
- 生产配置摘要，避免打印 token
- 生产 state 摘要
- launchd/systemd PID、exit status、入口命令
- startup log：bot/job/service 是否完成注册
- error log tail：是否新增 fatal/error/warning
- user-visible UAT：TG/cron/artifact/escalation 等真实可见结果
- permission/sandbox UAT：必须同时记录近似探针和原始入口探针，避免把 `codex exec` 成功误判为 TG bot 成功
- cleanup：临时文件、子进程、listener、lock
- rollback artifact：备份路径、回滚配置、是否需要清 state 或 `/new`

记录时只写摘要和脱敏证据，不复制完整 token/config。

## PULSE high-risk 默认要求

涉及生产 cron、runtime fallback、harness owner/state、policy/escalation、跨机器部署时，默认 high-risk：

- 明确 MVS
- 开发测试
- fake 或隔离复现
- 真实轻量验收
- spec / MEMORY / PROGRESS 同步
- release evidence 和 rollback path

## PULSE 生产入口规则

下列文件/路径的行为变更默认 slice risk = high-risk，即使本轮不重启生产：

- Relay `startRelay()` / `cli.ts`
- Telegram channel handler / bot command handler
- scheduler cron 注册、manual run、delivery
- runtime registry、runtime fallback、pool/PTY behavior
- state manager 的持久化 schema 或 migration
- harness owner/handoff/store/policy/escalation
- launchd/systemd/plist/env/config

理由：这些改动会在下一次 build/restart/reload 后进入生产路径；未部署只是 release gate 尚未打开，不代表实现风险低。

## PULSE 上线梯度

默认顺序：

```text
1. 自动化验证
2. dark deploy：代码进生产但不改变默认行为
3. single canary：只选一个 agent/job/user
4. canary UAT：真实文本/图片/cron/job/session 等关键路径
5. preference/fallback canary：只在 canary 对象上打开兜底策略
6. 观察日志和 state
7. 再决定扩大
```

每一步都要保留回滚路径。对 Relay TG runtime，回滚时注意 persisted session：如果 state 已写入 `runtime=codex`，只删除 config 字段不一定回到 `cc`，需要显式配置 `runtime="cc"` 或清理 session / 执行 `/new`。
