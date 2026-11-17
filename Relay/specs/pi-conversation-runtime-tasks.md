# Pi Conversation Runtime 交付任务与证据

> WI: `wi_20260822_938`
> updated: `2026-08-25`

## 实现与静态闸

- [x] 冻结第四 adapter / 非 ProviderDef ADR。
- [x] structured route、runtime session binding、namespaced options。
- [x] stable turn identity、single winner、effect monotonicity、side-effect no replay。
- [x] Pi v0.84.2 RPC：LF JSONL、settled、stream、state、abort、switch、usage delta。
- [x] Relay 内部命令对齐：`/new`、`/stop`、`/restart`、`/runtime`、`/model`、`/models`、`/think`、`/compact`、`/context`。
- [x] 飞书流式卡在 throttle window 内逐字符累积，close 后精确等于权威最终文本。
- [x] manifest 驱动的 tenant workspace/session/env/extension/skill/context 边界。
- [x] host-native profile：版本 pin、task-context workspace、containment allowlist、真实 toolchain/Agent context、tenant-private state 与最小凭据；worktree 是 Agent 执行决策而非 Pi 前提。
- [x] host-native 节点资源边界：Pi binary/toolchain、共享 agent config、reviewed extension/skill/context 与 ComputeCost host 使用独立 `nodeResourceRoots` realpath containment；Aki/Hestia tenant workspace 不再被错误要求包含节点安装资源，external isolation 不放宽。
- [x] container profile 的 digest/commit/version 验证与无跨-profile fallback。
- [x] 当前回归：fresh current-main worktree 先 `npm run build` 后运行 `npm test`（1444 passed / 1 skipped，
  83 files passed / 1 skipped）、`npm run typecheck`、`npm run build` 全绿。
- [x] PR #163/#165/#168/#170/#181 正常 review/合并；#181 trusted Linux `integration-gate` 绿色，默认分支已复验。

已知 CI 证据边界：hosted `relay-contracts` 因 GitHub hosted-runner billing/quota 在 0 step 失败；保持 unavailable，不伪装为产品失败或 CI 绿色。可信 Linux gate 已实际执行同一交付候选并通过。

## 灰度与真实 UAT

- [x] resident host 已安装并验证 host-native Pi binary/toolchain/tenant credential profile，登记可写 workspace containment root；task context 提供实际 workspace，Agent 自主决定是否创建 worktree；未使用 immutable production release checkout 作为 writable workspace。
- [x] 本次明确选择 host-native canary；未选择 container canary，因此无需构建 Pi image。
- [x] 内部 `tcloud-pulse` tenant 的 Telegram 与飞书绑定同一 logical agent 和 Pi route；Vultr Telegram poller 已无双拉取迁移。
- [x] manifest/credential 已部署并 safe restart；非 canary bot 与三路旧 runtime 回归未改变。
- [x] Telegram：new、restart continuation、native cancel、file/tool、visible failure 全通过。
- [x] 飞书：new、restart continuation、native cancel、file/tool、streaming final equality、visible failure 全通过。
- [x] 两渠道 delivery receipt、Pi session binding、lineage 与 usage raw rows 已对齐；成本因 canonical pricing miss 保持 unknown，不伪报为 0。
- [x] 完整回滚演练：TCloud 退出 → Vultr 恢复唯一 poller → Vultr 再退出 → TCloud 恢复；session/evidence 保留，且无 `--force`/无双 poller。

## 完成闸

- [x] 跨 runtime/channel 的 commentary、activity、stream final 与权威 final 已冻结到共享 rendering contract；TG
  使用一个演进 progress 加一个 fresh authoritative final，飞书使用一个 streaming card 加至多一次完整 fallback。
- [x] Telegram `/model` 与 Pi `/think` 已接原生 inline keyboard；callback 使用有界短期 opaque token，按
  chat/user/agent scope 校验并一次消费，长 model id 不进入 callback data。
- [x] 三端 live release 已逐端核对：macOS 为 `a7d0237f970c331d15e845479b5435874ec7abac`，
  TCloud/Vultr 为其后继 `c3ec8a7ef019a33f4df8f712cb09d6715fcf9dbe`；两者均在冻结的 implementation base
  `1a12446cf029ae376e3de8691d096e5afea3ea4c` 历史中，closeout PR #243 merge
  `bbaa3c1ec1e847a1d58046455c3308930c561cb8` 继续保留该祖先关系。差异来自其它 WI 的后继发布，不回退制造同 SHA；
  本轮只运行 safe wrapper `--check`，TCloud busy gate 正确拒绝，未使用 `--force`。
- [x] Telegram `/model`、`/think`、工具型长任务的唯一 terminal final/no replay 与飞书工具型长任务的
  progress/final/no-duplicate 已由真实客户端复验；用户直接判定“全部测试通过”。
- [x] **N/A：凭据轮换不在本 WI 中执行。** 用户于 2026-08-25 明确裁定该范围边界；closeout 不读取、
  记录、轮换或失效生产凭据。
- [x] 独立 Outcome 覆盖完整 acceptance：producer 为 `pulse`，非 producer 的人类 verifier `owner`
  基于三端长期真实使用、增量 UAT、GitHub、runtime、回滚与 remaining risks 明确同意全面 closeout。
- [x] canonical WI receipt 已冻结在 CollaborateWithAI commit `6daf00d37dcc5263fc434ca28b4bd21ab2e86adf`，
  记录代码/PR/merge、default branch、三端 live release、UAT、cleanup 与遗留风险。
- [x] 未获默认路由晋升的显式人类授权；Pi 保持不在 runtime-priority/default/fallback。

## 终局边界

- GitHub：WI938 相关 PR（含 closeout #243）全部 merged，当前无 open WI938 PR；#243 merge
  `bbaa3c1ec1e847a1d58046455c3308930c561cb8` 的 default-branch verification run `32855632457`
  与 trusted Linux fallback run `32855632547` 均为 success。
- 基础设施红灯：同一 main SHA 的 ComputeCost run `32810475221` 为 `steps=[]`，ChatGPT entry CI run
  `32810474436` 无 job；两者未执行产品命令，保持 infrastructure unavailable，不冒充产品失败或绿色。
- Runtime：macOS、TCloud、Vultr 均 active；macOS 与 Vultr safe gate 绿色，TCloud 因两个真实 busy 会话
  fail closed，未重启。
- Cleanup：7 个旧 WI938 worktree、19 个旧本地分支、12 个远端分支已在 dirty/ancestor/patch-equivalence
  审计后清理；GitHub PR 与 merge history 保留。closeout 自身的临时 branch/worktree 在合并后删除。
- Remaining risk：用户报告偶发 Agent 以“会继续执行”的 final 结束却没有动作。现有样本缺 machine trace，
  尚不能归因到模型、orchestration、Pi RPC 或 Relay；本 WI 不把它伪报为已修复。若未来 trace 证明 tool call
  丢失、提前 settled 或 continuation 丢失，应建立回归并按 runtime 缺陷处理。
- Supply-chain risk：fresh install 的 `npm audit --omit=dev` 报告 7 high、3 moderate production dependency
  advisories（含直接依赖 `ws` 与飞书 SDK 的传递链），均显示存在升级修复。它们是 current-main 跨切面依赖债，
  不是本 WI 引入或本 closeout 自动升级的范围；不得把 WI938 accepted 改写成“Relay 无已知依赖风险”。
