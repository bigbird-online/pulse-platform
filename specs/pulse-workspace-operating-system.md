---
name: Pulse Workspace Operating System
version: 1.0.6
modified: 2026-11-09
# history: git log -- specs/pulse-workspace-operating-system.md
authority: PULSE workspace recipe and dual-root authority map
---

# Pulse Workspace Operating System

## 1. Work object

PULSE 只有一个 Agent：`pulse`。它的唯一 workspace 真身是 `pulse-space/`，canonical Harness principal 也是 `pulse`。`local-pulse-space/`、`tcloud-pulse-space/` 与 `muse-pulse-space/` 是三个 execution surface；只以 `surface_id`、runtime class、capability 及 CLAUDE/HEARTBEAT 入口区分，不生成第二 identity、第二记忆或第二工作状态。

## 2. Authority Source Map

| 层 | 真源 | 负责回答 | 禁止承载 |
| --- | --- | --- | --- |
| L0 人类任务 | 用户明确指令、WI acceptance fingerprint | 本轮目标、授权、验收快照 | 平台长期实现细节 |
| L1 Coevolution | Coevolution `CLAUDE.md`、共同使命、constitution、roster、registry、共享 skills | 共同使命、安全、Git、WI/Harness、Outcome UAT、团队关系 | PULSE live health 与模块实现 |
| L2 PULSE 项目 | PULSE `CLAUDE.md`、相关 Specs、代码、测试 | 平台地图、交付/部署/收敛合同、产品事实 | hostname/path/凭证与动态 WI 前沿 |
| L3 Pulse 真身 | `pulse-space/IDENTITY.md`、`MALIK.md`、`SOUL.md`、`MEMORY.md` | 谁在工作、如何经营 PULSE、稳定判断 | surface 入口差异与任务前沿 |
| L4 Execution surface | 当前 `*-pulse-space/CLAUDE.md`、`HEARTBEAT.md`、bootstrap/entry receipt | 当前入口、capability、带 freshness 的 host/runtime/binding 事实 | 独立 identity、共享规则副本、第二状态机 |
| L5 工作接续 | canonical WI、execution binding、checkpoint、ResumePacket | owner/executor、completed/pending/decision/risk/next action | 稳定知识、业务状态副本 |

冲突顺序：人类 acceptance snapshot > Coevolution 安全/Git/WI/SSOT 纪律 > PULSE 项目合同与 Specs > Pulse 共享内核 > surface adapter。时点事实由当前 receipt 纠正，不能反向覆盖上层规则。

Coevolution `ssot/registry.yaml` 只登记 canonical workspace、`state_profile` 与 PULSE 外部 surface registry 指针；`config/scl-surfaces.yaml` 唯一拥有各 surface 的 `surface_id`、class、runtime、capability 和 broker profile。不得在 Coevolution 再抄 surface 值。

## 3. 文件与状态迁移门

各 surface 的共享核心必须是指向 `../pulse-space/<file>` 的相对 symlink，只有 CLAUDE/HEARTBEAT 是普通文件。旧内容唯一归宿：

| 旧内容 | 唯一归宿 |
| --- | --- |
| Identity、责任、价值判断 | `pulse-space/IDENTITY.md`、`SOUL.md`；大鸟画像为 Coevolution `ssot/USER.md` |
| Objective / KR / 长期管理循环 | `pulse-space/MALIK.md` |
| 跨 surface current facts | Stats 同源查询（`pulse_stats`），必须带 source 与 freshness |
| surface、capability、host、run、binding 时点事实 | 当前 bootstrap/entry receipt |
| objective、当前 WI、pending、decision、risk、next action | WI / checkpoint / ResumePacket |
| 可跨任务复用的触发判断 | `pulse-space/MEMORY.md` |
| 已结束事件与证据 | Git history、memo 或 canonical outcome receipt |

删除活动 PROGRESS/STATE 前必须证明所有消费者已迁移、无独有事实丢失、fresh session 可从 WI/SCL 恢复。历史证据中的旧文件名不机械改写。

## 4. Bootstrap contract

只有正式 WI entry/continuation/adoption/recovery、surface migration，或确需证明双根与执行权时，才由 `Scripts/pulse-workspace-bootstrap.py` 产生 `pulse_workspace_bootstrap_receipt/v1`。普通讨论、诊断、审查和一般仓库工作不以 bootstrap 为启动前提。receipt 必须绑定：

- caller 与 Harness principal 都精确为 `pulse`，workspace 在两份 registry 中唯一解析到当前 `surface_id` / class / runtime；
- Coevolution 与 PULSE 的 registered repository slug、规范化 remote identity、remote default/target ref/SHA、expected exact SHA 与 required-path object digest；
- resident 模式要求两个 working tree clean、local target exact 且 ahead/behind 为 0；actions_bridge 使用 stateless 模式，不把 ephemeral observation 冒充 resident clean；
- Harness 状态由 canonical CLI `execution status --json` 取得且 acceptance fingerprint 非空；resident executing 要求 owner/executor 为 `pulse` 且 binding runtime 精确嵌入当前 `surface_id`，stateless actions bridge 要求 runtime class 与 registry 一致并由 receipt 绑定 `surface_id`；observing 模式不声称 execution authority。

任一根 dirty、ahead、behind/diverged，或 slug/default/target/expected SHA、required object、principal、surface、binding 冲突时 fail closed。另一 surface 的 receipt 不能代验当前入口；receipt 只证明 admission，不证明交付完成或 live health。

每次有状态动作前必须重新查询 canonical binding，并让 receipt 的 `epoch + surface_id + principal` 与当前值精确一致；旧 receipt 不授权下一次 mutation。此 pre-mutation check 不能冒充接收端 fencing：若外部系统可能在迁移后继续接受已经飞出的旧 worker 写入，端到端 single-winner 必须另有接收端 stale-epoch rejection evidence；缺证据时把该路径记为 remaining risk，不宣称已隔离。

注册入口的请求账分两段：进入可观测接入层的 attempt 先分配 `request_id`，在响应前原子持久化 accepted 或 explicit rejection；accepted 请求再进入执行终态账。网络尚未到达注册入口可观测层的流量不冒充平台已接收请求，入口探针须单独说明其观测边界。

## 5. Code convergence contract

GitHub integration SHA 是代码真源。`Scripts/pulse-workspace-converge.py` 先从 checkout registry 唯一解析 repository slug、remote 与 default-ref policy，然后只能执行：

- resident：clean、无 local-only commit、remote target 与 expected integration SHA 精确一致时 fast-forward；
- stateless（actions_bridge）：查询 remote default/target 并产 receipt，不声明本地 checkout；
- 两种模式都校验 registered repository slug；resident 目标 diff 触及 `.harness/` 时拒绝，由 Harness 专用流程处理。

切换 checkout、runner 或 Actions fallback 不迁移 Harness authority。active surface 变化必须另走 dormant checkpoint + adoption/recovery，保持同一 principal 下 single winner。

## 6. Workspace topology 专项 UAT

本节只在 workspace topology、surface registry、bootstrap/entry 或迁移合同发生变化后执行；普通 session 不以双根 bootstrap、身份证明或 WI 恢复为启动前提。

各真实 surface session 在不读旧 transcript 的条件下回答并证明：

1. 自己是唯一 Pulse，canonical principal 为 `pulse`，共享核心全部解析到 `pulse-space/`；
2. 两个根的 exact SHA、remote/default/target 与 clean 状态；
3. 当前 `surface_id` / class、resident 或 brokered entry，以及本 surface capability 边界；
4. 当前 WI owner/executor/epoch 与第一业务动作；
5. PR、Actions、merge、default branch、live smoke 哪些已证、哪些 unknown。

各端 receipt 必须绑定同一候选 PULSE SHA 与明确的 Coevolution SHA。任一端不能用另一端、PR green 或 host-local smoke 代验；跨 surface 接续不得产生两个 active winner。
