---
version: 1.2.0
status: candidate
work_item: wi_20260827_282
owner: Pulse
upstream_contract: Bigbird/Lab/BigbirdEnvironment/agent-harness/specs/harness-scl-golden-path.md
---

# PULSE SCL Golden Path

Harness `harness-scl-golden-path.md` 是 WI lifecycle/UX 真源；本文件只定义 PULSE、Relay 与 Pi 的物理实现映射。

## Relay command contract

- `/wi status <wi>` 必须从该节点 converged Harness service authority 读取，不能读取落后的 human shared checkout。
- `/wi <wi>` 表示 enter/continue，不表示清空 Session。Relay 在 planning 前以当前 Host surface、WI、caller agent 与 repository slug 调用 sealed `resolve-managed-workspace-local`；static Agent workspace 只可定位 repository，不得作为 admission/spawn path。首次进入开放、未 claim 的 `main_only` WI 时，Relay 以 authenticated caller agent 解析当前 host surface，要求已有 clean 的 `<wi>-<agent>` linked worktree，调用 bounded `activate-owner`、登记外部 workspace authority，再进入 execution；全过程不改 unrelated shared-root dirt。回执必须给出 `entry_action`、`physical_session=fresh|reused`、epoch 与 checkpoint revision。
- `/wi rollover <wi>` 内部执行 checkpoint、prepare、single-flight physical replacement、consume，并把 source retirement/seal（Pi seal 绑定 exact WI/epoch）、真实成功 result 事件的 digest/timestamp 与 fresh readback 组成 `rollover_completion_evidence/v1` 后才 apply；Pi target 还必须由 readback 证明 epoch/usage-binding digest。用户不接触 continuation id 或 raw Session identity。
- 回执使用 `rollover_prepared|rollover_completed|rollover_recovered`；冲突或证据不足返回 `recovery_required`，不得把 prepared 写成 completed。
- same-Agent replacement 保持同 WI/owner 且不创建 handoff。

`@anchor symbol handleWorkItemContinue`
`@anchor symbol handleWorkItemRollover`
`@anchor symbol WorkItemSessionController`
`@anchor symbol bootstrap_owner_entry`
`@anchor test Scripts/tests/test_scl_resume.py :: test_owner_bootstrap_activates_then_registers_clean_existing_worktree`

## Pi continuation 与其它生命周期恢复

`pulse_wi_continue` 只接收一次明确 WI 的 continuation intent，仍由 Relay 在当前 turn 结束后执行；Agent 不从 Bash 重放该 continuation。这条规则不得被扩成禁止所有其它已授权的 sealed 生命周期入口。

Host lifecycle control 开启且存在已审查的 Host 配置时，Pi 即使尚未绑定业务 worktree，也取得经 node resource realpath/allowlist 核验的 `PULSE_SCL_BIN`、`HARNESS_ROOT`、`SCL_KERNEL_ROOT`，并沿用同一 Host 派生的 `SCL_SESSION_REF`。其它已授权动作（例如 `adopt-workspace-authority-local --add-repository <descriptor-file>`）直接消费 canonical CLI；不替换身份，不把 continuation 当作追加登记入口。配置缺失时保持资源不可用，不猜路径。此环境注入不签发 authority，owner/Session/fence/registration 和当前 consumer admission 仍由 SCL 核验。

独立 kernel target 的显式 `/wi continue` 若首先读到 unbound，只能经 Host-owned dispatcher 完成 descriptor、source seal、mode-0600 digest transport 与 target import；strict import readback 后重验 resolver 结果，再由 target `continue-local` 完成 claim、v2 runtime readback 与 apply。任一失败禁止 target business dispatch，且不得退回 orient/bootstrap 或 local-isolated。

`@anchor test Relay/tests/pi-scl-intent.test.ts :: reserves continuation for Host without banning other sealed owner operations`
`@anchor test Relay/tests/scl-entry-transaction.test.ts :: never bootstraps an explicit continuation on an unbound independent target`
`@anchor test Relay/tests/scl-entry-transaction.test.ts :: recovers the same independent-kernel handoff without business dispatch after a transport failure`

## Shared physical runtime contract

`RolloverCoordinator` 同时支持 CC 与 Pi，并共享 validate、single-flight、persist、ACK、consume 与 recovery 状态机。Pi adapter 必须：

1. source settled 后先完成 digest-valid usage seal，seal 失败时 source 不 retirement；
2. target 使用 fresh Session 与 epoch+1 runtime usage binding；
3. target ACK 与真实成功 runtime result 事件的 digest/timestamp 均持久后才允许 consume；apply 前 live target 必须等于 Harness 已 consume 的 target ref；CC readback 证明 live session/runtime，Pi 还必须证明 target epoch/usage-binding digest；
4. timeout、target failure 或 consume failure恢复 exact source Session 与 source usage binding；无法证明时 fail closed。

`@anchor symbol RolloverCoordinator`
`@anchor symbol RelayRolloverRuntime`
`@anchor test Relay/tests/session-rollover.test.ts :: test_rollover_two_phase_order follows validate/persist/kill/spawn/bootstrap/ACK/consume`

## Pi usage seal

`/usage_seal` 返回 `pulse_runtime_usage_seal_readback/v1` 的用户投影：首个 `sealed`、幂等 `idempotent_replay`、artifact ref 与 sealed digest。Pi RPC notification 只是 receipt transport；Relay 必须独立读回并验证 exact binding、mode 0600、regular file、canonical snapshot digest、privacy redaction 与 single receipt。Replay 必须重采当前语义 usage；seal 后若 usage 已变化则返回 `seal_conflict`，不得把旧 artifact 冒充当前 seal。

`@anchor symbol verifyPiUsageSealArtifact`
`@anchor test Relay/tests/pi-conversation-runtime.test.ts :: returns a mode-0600 digest-validated usage artifact readback without an LLM turn`
`@anchor test ComputeCost/tests/test_pi_usage_extension.mjs :: host capture rejects stale target authority, tampered replay, and ambiguous binding source`

## Compatibility and exclusions

checkpoint、ResumePacket、opaque refs、privacy、unknown-not-zero、source recovery material 与 exact candidate review 不回退。`/new`、`/reset`、`/kill` 不是 Golden Path。此 WI 不启用 autonomous rollover、S1 shadow 或 suggestion，也不制造 terminal UAT sample。
