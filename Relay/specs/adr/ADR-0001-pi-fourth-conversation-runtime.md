# ADR-0001：Pi 作为第四 ConversationRuntimeAdapter

> status: `accepted_for_CR-02_tcloud_node_capability`
> date: `2026-08-22`
> amended: `2026-08-24`
> decision owner: Pulse / `wi_20260822_938`

## Context

Relay 已有 `cc`、`codex`、`cc-sdk` 三路长期会话实现。Pi 同时拥有 provider/model 选择、
持久 session 与 RPC 生命周期；若把它塞进 `ProviderDef`，会把“谁管理会话”和“模型请求
发给谁”合并成一个轴，并继续制造 runtime-specific 散字段。Pi 官方还明确不提供内置
sandbox；隔离因此必须是显式、可审计的运行 profile，不能被误编码成 Pi 的身份或默认能力边界。

## Decision

1. Pi 作为第四 `ConversationRuntimeAdapter`，不是 provider。
2. 新入口是 namespaced `conversationRuntime`：runtime、provider/model、reasoning、
   isolationProfile 分轴；Pi 原生选项进入 `runtimeOptions.pi`。
3. Pi 在 TCloud 是节点级显式 opt-in capability：节点提供受审默认 route，所有 Agent 都可由用户
   主动选择；它永不参与自动 runtime fallback，也不改变任何 Agent 的默认 runtime。
4. turn identity 与 effect 由 manager 统一管理。观察到 side effect 后禁止跨 runtime replay。
5. Pi RPC pin `0.84.2` / commit `914cf147...`; LF JSONL、`agent_settled`、累计 usage
   delta 是协议真值。
6. Pi 的默认 resident profile 是 host-native：版本固定的 Pi 在 task context 提供的实际 workspace
   与真实宿主工具链中运行，读取适用的 Agent 启动契约。profile 的 workspace roots 只做 containment
   allowlist；checkout/worktree 由 Agent 执行任务时决定，不是 Pi 模块的开发、部署或运行前提。它仍
   使用 tenant-private session/agent state、显式 credential allowlist 与受审 workspace/toolchain paths。
7. container、VM、policy-sandbox 是显式可选 isolation profile；选中某 profile 后，其校验
   失败必须 fail closed，绝不静默换成另一种 profile 或扩大凭据。

## Consequences

- 旧三路配置/state 无需假迁移；Pi session binding 可独立持久化和验证。
- `/runtime pi` 对配置了节点 Pi capability 的 TCloud Agent 一致开放；首次选择时物化节点默认 route。
- host-native 节点 capability 必须固定 Pi binary/toolchain、配置独立的 workspace containment roots、
  realpath 后生效且不得为文件系统根的 node resource roots，以及最小凭据；reviewed extension、skill、
  agent config 与 ComputeCost host 属于节点资源，不属于某个 tenant workspace；
  task context 提供实际 writable workspace，且不可使用 immutable production release checkout。
  Agent 是否创建 worktree 不属于部署前置条件。container profile 仍必须构建带正确 OCI revision
  label 的 digest-pinned image。
- availability 低于“自动退回其他 runtime”，但副作用与身份不会因重放产生双写。
- 将 Pi 变成自动默认路由仍需要新的显式人类决策与真实双渠道证据。

## Rejected alternatives

- **Pi 作为 ProviderDef**：无法表达 Pi 自己的 session/RPC/abort/settled 语义。
- **复用 cc-sdk adapter**：协议、完成事件、累计 usage 与隔离契约均不同。
- **Pi 失败自动回 Codex/CC**：在工具/网络副作用之后可能重复执行。
- **把 container 当作 Pi 身份或默认能力边界**：会令 Pi 缺失真实 workspace、工具链和 Agent
  启动契约；隔离是可配置的风险控制轴，不是 runtime 的身份。

`@anchor symbol ConversationRuntimeAdapter`
`@anchor symbol createPiConversationRuntime`
`@anchor symbol fallbackActiveTurn`
