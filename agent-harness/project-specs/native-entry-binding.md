---
name: native-entry-binding
version: 1.0.0
status: specification
work_item: wi_20261015_422
modified: 2026-10-20
---

# 追加需求、入口绑定与平台确认

本文件给出 AH-081 至 AH-085、AH-087 与 AH-088 的设计。决定来源：大鸟 2026-10-16（f89a4c35…、8cb06ba7…、ca30f8cf…）；AH-087 为大鸟 2026-10-19 方案 A；AH-088 为大鸟 2026-10-20 方案 A，其中同一 Agent 跨会话的请求与委派为大鸟 2026-10-20 授权（c72e155e…）。

## 1. 术语

| 名词 | 含义 |
| --- | --- |
| Agent | 已登记的执行身份，如 pulse、asobi。内核字段 `actor`。 |
| 节点 | 安装新内核的机器，如 ownermac、TCloud。内核字段 `surface`，账本字段名不变。 |
| 入口 | 人或模型接入的方式：telegram、feishu、cli、chatgpt_web。会话属性，不进身份三元组。 |
| 会话 | 一个原生会话日志对应的执行上下文。内核字段 `session`，由节点、Agent 和原始会话 ID 摘要得出。 |
| 绑定方 | 有权声明“某会话代表某 Agent”的一方：Relay（bot 与飞书应用）、本机启动器（独立 Pi、外部入口）。 |

身份三元组 `{actor, session, surface}` 不变；执行权仍绑定完整三元组与代际，唯一例外是第 7 节的同一 Agent 追加记录。

## 2. 追加需求（AH-081）

新意图 `add_requirement`，字段：

- `previous_revision`：当前定义版本，过期返回 `domain_conflict`；
- `what`：新需求说明；
- `acceptance`：新增验收条件，非空；
- `material_refs`：材料引用（PR、文件路径与提交、外部链接），可为空列表；
- `reason`：原因。

权限与 `amend_definition` 相同：Draft 由负责人、执行中由当前执行者提交，必须附人类决定（`human_decision`）。

事件 `RequirementAdded` 产生新定义：原 `why`、`what`、`acceptance` 逐字保留，新增条件追加到 `acceptance` 末尾；`definition.additions` 追加一项 `{what, material_refs, reason, acceptance_from}`，`acceptance_from` 是这批条件在 `acceptance` 中的起始下标。定义版本加 1。旧计划的 `definition_revision` 随即落后，派发停止，直到执行者用 `revise_plan` 修订计划。`amend_definition` 保留，用于整体改写。

## 3. 会话绑定与入口属性（AH-082）

每个原生来源记录入口：

- Relay 写会话证明时加 `entry`：从自身会话键得出 `telegram` 或 `feishu`，无法解析的键记 `relay`；
- 早于本字段的 Relay 证明记 `relay`；
- Relay 调度器拉起的 CC、Codex（含 cc-interactive）记 `scheduler`；
- 无 Relay 证明的 CC、Codex、独立 Pi 记 `cli`；
- 外部入口取绑定记录中的 `entry`。

调度器证明与对话证明同目录、同格式（`pulse.native-session-source/v1`），按调度器启动的进程 PID 命名：CC 批处理为 CC 本身；Codex 为 npm 启动脚本，原生 Codex 是它的直接子进程；cc-interactive 为 PTY 桥，CC 是它的直接子进程。会话号取已知值：CC 用启动参数 `--session-id`，Codex 用 `codex exec` 标准错误开头的 `session id:` 行。记录另含 `tenant = "scheduler:<jobId>"`、`owner`（job 所有者）与 `allocation_id`（运行号），进程结束时 Relay 删除自己写的这份记录。

内核只让 CLI 档案（无证明目录、非独立 Pi）接受调度器证明，查找范围限于原生进程本身及其直接父进程，并核对进程出生与 Relay 祖先。证明只替代从直接子进程环境读取的会话号；Agent 仍由会话日志所在的已登记工作区决定，一个工作区只登记一个 Agent，因此调度器会话被认成 job 工作区所属的 Agent。调度器证明不参与 `relay_tenants` 匹配，也不触发无租户档案的让位规则，即使 `tenant` 字符串与对话租户相同也不会被认成对话会话；会话不带 `PULSE_PI_TENANT_ID`，人类输入仍从原生会话日志读取。没有调度器证明时沿用原环境路径。

证明按 PID 命名，PID 会被新进程复用。内核读证明时先比对记录的进程出生戳（`process`）与该 PID 当前进程：不符即是同号旧进程留下的记录，按没有记录处理，在比对运行时之前跳过，既不授予权限也不拒绝会话，会话按自己的真实档案识别。出生戳相符的记录仍逐项核对运行时与格式（`proof_kind`）、属主、Relay 祖先与 Relay 入口，任一不符即拒绝。证明文件不受保护（符号链接、他人所有、组或其他人可读写、超过 4 KiB）照旧拒绝。对话进程退出时 Relay 删除其证明，启动时清扫无主证明，见 `Relay/specs/relay-scheduler-jobs.md` §4a。

macOS 不向非 root 读取者公开系统平台程序（如 `/bin/zsh`）的环境：Codex 经 `zsh -lc` 运行命令，若 shell 先 fork 再运行 `harness-native`（它不是脚本最后一条命令），原生进程的直接子进程是 zsh，环境读不到。会话号缺失的拒绝原因附带定位信息：`runtime`、`native_pid`、`relay_proof`、`scheduler_proof`、`environment_pid` 与 `environment`（`readable` 或 `unreadable`），不含环境值和会话号。

`NativeHost.entry` 保存入口，写入 Agent 来源原件 `{"native_header", "entry", "command"}`，并在 `identity` 输出中返回。入口不进入 `HostContext`，不参与身份比对，不授予任何权限。ComputeCost 已按 Relay 会话分组用量，Telegram 与飞书会话因此分开统计；外部入口没有本机用量来源，成本不计入。

## 4. 平台确认渠道（AH-083）

`@example_cc_bot` 作为 PULSE 平台机器人，只运行在 ownermac Relay：`relay.json` 的 `platformBot` 写 token 与所有者 Telegram 账号 `ownerChatId`，不得与任何 Agent 共用 token。TCloud 删除其原定义。部署时，配置了平台机器人的节点在内核策略中打开 `external_entries`：`{root, owner, entries: ["chatgpt_web"]}`；Relay 从已安装内核策略读取同一 `root` 与 `owner`。

请求：内核写 `root/requests/<request_id>.json`（`pulse.platform-request/v1`，`kind` 为 `binding` 或 `decision`）。Relay 每 3 秒把未发送请求作为卡片发给所有者，按钮为确认与拒绝。

确认：只认所有者账号，30 分钟内有效，结果写入 `<request_id>.result.json`，重复按按钮返回同一结果。确认时 Relay 用现有人类输入保管写记录：`channel = "telegram"`、`eventId` 为回调 ID、`humanRef` 为所有者账号、`message` 为确认内容 `pulse.platform-confirmation/v1`；`poolKey` 对绑定是 `entry-binding:<request_id>`，对决定是 `entry:<binding_id>`。引用值沿用 `digest([channel, eventId, humanRef, poolKey])`。

## 5. 外部入口（AH-084）

绑定：`harness-native entry-request --entry chatgpt_web --actor <agent> --wait 600`。所有者确认后，Relay 写 `root/bindings/<binding_id>.json`（`pulse.entry-binding/v1`：binding_id、entry、actor、request_id、human_input_ref、confirmed_at）。内核每次使用时重新核对：记录私有、未撤销、其人类输入是所有者对同一请求的确认、Agent 是已登记 producer。

调用：`harness-native entry <binding_id> -- <子命令与参数>`。启动器把命令追加到 `root/sessions/<binding_id>.jsonl`（首行会话头），再以自身为父进程运行内核 CLI。内核在祖先链中找到已安装 Python 执行 `harness.pyz entry <binding_id>` 的进程即识别为外部会话：运行时 `external`，入口取绑定记录，角色与工件、工作区范围取该 Agent 全部 producer 档案的并集。

决定：`harness-native entry <binding_id> -- entry-confirm --input <file>`，文件为将执行的 `{intent, work_item_id, input}`。所有者确认后返回 `human_input_ref`；内核只在意图、WI 与输入摘要都相同时接受它。计划类输入的 `graph.confirmation_ref` 指向这次确认本身，确认前无法填写：摘要不含这一项，使用时它必须等于这次确认的 `human_input_ref`，其余图、资源与业务内容的任何改动仍需重新确认。外部会话文本、绑定确认和其它会话的人类输入都不能充当决定。

信任边界：本机任何进程都能调用启动器，与独立 Pi 相同，信任本机账户；人类决定只能来自平台机器人。所有者可在绑定确认卡片上撤销绑定，撤销后调用拒绝。

## 6. 同一 Agent 跨入口接续（AH-085）

`/wi continue WI` 在调用会话的 Agent 等于持有者 Agent、会话不同时，映射为新意图 `continue_here`（附该斜杠输入的人类决定）。事件 `SessionContinued`：持有者改为调用者三元组，代际加 1，有效范围授权改绑新持有者，原会话的进行中尝试按旧代际处理，此后控制与任务写入被拒，追加记录按第 7 节。Agent 不同返回 `authority_denied`，提示交接（`rollover`）或接管（`/wi reinit`）。

## 7. 同一 Agent 其它会话追加记录（AH-087）

一个 WI 同时只有一个持有会话。与持有者 `actor` 相同的其它会话（例如同一 Agent 的定时无人值守会话）可对该 WI 提交 `checkpoint` 与 `record_work`，条件与持有者自己提交相同：

- WI 为 `active`，无阻断工作的轮次，计划的定义版本为当前版本；
- 调用代际等于 WI 当前代际。CLI 执行前读回当前代际作为调用代际；它只用于比对，记录不推进也不改写代际，持有者此后交接或接续使代际变化时，用旧值的提交返回 `authority_changed`；
- 资源在 WI 资源内，且不与有效委派授权重叠；
- `record_work` 的成果须有本次捕获的实际字节，受管工作区成果按同一范围判断。

其它意图不放宽：领取、交接、结束、计划、定义、授权与撤销、会话访问等仍按各自原规则判断（持有会话、委派授权、预定后继或人类决定），同一 Agent 不构成其中任何一项。`local_work` 对这些会话仍为不可用，原因 `held_by_same_agent_in_another_session`，`/wi continue` 照常据此转移执行权。

事件 `CheckpointRecorded`、`WorkRecorded` 记 `author`（Agent）与 `author_session`（提交会话），据此区分持有会话与追加会话。`author_session` 只在日志中，公开观察的 `records` 不含此项。

## 8. 跨节点协作按 Agent 指定接收方（AH-088）

各节点写同一生产账本，一个节点上的 WI 可以请求另一节点的 Agent 协作，例如 TCloud 上的 cc 请 ownermac 上的 hestia 办家事。

`offer_collaboration` 的每个接收方 `principal` 取两种写法之一：

- 只写 Agent：`{"actor": "hestia"}`；
- 完整三元组：`{"actor", "session", "surface"}`，原有写法。

只写 Agent 的接收方也可直接写成观察结果里的形状 `{"actor": "hestia", "resources": [...]}`，等同于 `{"principal": {"actor": "hestia"}, "resources": [...]}`。

主身合同（`main_only: true`）的只写 Agent 接收方带 `target_work_item_id`：接收方在自己的哪个 WI 上处理。它不能是发起合同的 WI，也不能用于完整责任或精确会话接收方；违反时返回 `invalid_input`。不写时，内核在发起前读取该 Agent 持有的进行中 WI（可读、非子 WI、不是发起合同的 WI）：恰有一个就记为它的 `target_work_item_id`；没有或多于一个返回 `invalid_input`，原因列出候选 WI 与标题（至多 10 个），或说明可改为指定对方的具体会话。记入合同后，该 WI 是否存在、由谁持有，在投递时读取。

`offer_collaboration` 必须写 `main_only`（布尔）：`true` 表示只由主身（与人类对话的常驻会话）处理，`false` 表示可由自动任务（如收件箱定时任务唤起的会话，即分身）处理。合同要用到对方正在进行的工作（未合并改动、手上的授权、在推进的 WI）或需要写权限时写 `true`；只读、只依赖 main 上已有内容就能办完时写 `false`。缺省返回 `invalid_input`，原因写明这条判据与主身接收方的 WI 写法。该字段出现前发起的合同按 `false` 读取。`reassign_collaboration` 可选写 `main_only`，不写沿用原合同的值；主身合同的只写 Agent 接收方同样按上文选定 `target_work_item_id`。Relay 定时任务拉起的会话（来源 `entry` 为 `scheduler`）答复主身合同返回 `authority_denied`。

只写会话或节点之一返回 `invalid_input`。`request` 与 `delegate` 的接收方可在任何节点，也可以是发起会话自己的 Agent，例如 ChatGPT 上的 hestia 会话请 ownermac 上的 hestia 会话调查；此时写完整三元组必须不同于发起会话，否则返回 `invalid_input`。`responsibility` 改变执行持有者，与 `rollover` 相同只接受本节点的一个确切会话：只写 Agent 返回 `invalid_input`，其它节点返回 `domain_conflict`（需迁移）；接收方属发起方自己的 Agent 时返回 `invalid_input`，同一 Agent 内移交执行权用 `rollover` 或 `/wi continue`。

`respond_collaboration` 的响应会话：

| 接收方写法 | 接受、拒绝 | 归还、完成 |
| --- | --- | --- |
| 只写 Agent | 该 Agent 任一节点的任一会话 | 该 Agent 任一会话 |
| 完整三元组 | 只有该会话 | 只有该会话 |

每个接收方只有一个答复状态：首个接受使状态离开 `pending`，此后其它会话再接受或拒绝返回 `domain_conflict`。跨节点并发接受时，后提交者因同一 WI 日志已变化得到 `ledger_conflict`，按原请求重试时重新裁决并得到上述拒绝，因此只有一个会话承担。首个接受的会话称承担会话，记入该接收方答复的 `principal`。`delegate` 接受时授予的 `TASK_RIGHTS` 范围授权绑定承担会话与当前代际，与其它范围授权相同；同一 Agent 的其它会话不因此取得领取或记录任务的权利。归还与完成只结束责任、撤销授权，不取得新权利，所以由该 Agent 任一会话办理，调度器拉起的短时会话结束后，后续会话仍能结清。完成仍须本次会话捕获的实际证据。

请求方会话不答复自己的合同。请求方会话指发起会话（合同 `offered_by` 的完整三元组），以及与发起会话同属一个 Agent、当前持有该 WI 的会话（例如经 rollover 接续发起会话者）。它对该合同的接受、拒绝、归还、完成都返回 `authority_denied`，即使接收方就是它所属的 Agent；同一 Agent 的其它会话按上表答复。因此请求方不能借“同一 Agent”自己接下并自证完成所请求的工作。答复状态仍以接收方 Agent 为键，请求方另记于 `offered_by`，二者不共用答复状态；一个合同中同一 Agent 至多出现一次。

事件 `CollaborationResponded` 记录 `principal`（实际响应会话），据此审计每次接受、归还、完成出自哪个会话。公开观察的 `collaborations` 只显示接收方 Agent，答复与历史不含会话。

接收方 Agent 的会话读取时：

- `list` 每项给出 `awaiting_response`：调用者 Agent 在该 WI 上仍待接受（`pending`）或待结清（`accepted`，完整责任除外）的合同，含 `contract_id`、`kind`、`status`、`required`、`main_only` 与可选 `deadline`，主身合同在前；欠调用者主身合同的 WI 排在列表最前；WI 不是 Draft 或执行中时为空；调用会话是请求方会话的合同不列出；
- 非请求方、非持有会话的 `inspect`、`resume` 在有此类合同时 `next_action` 为 `respond_collaboration`，`reason` 为 `collaboration_pending` 或 `collaboration_accepted`，附 `contract_id`；主身合同另附 `main_only: true` 与 `agent_action`“主身合同：在与人类的会话中处理”，并优先于待结清的答复；
- `watch` 的协作项在接受后以承担会话为执行者。

通知路由按节点安装，只把通知送到本节点 Relay 的人类渠道，不能投递给另一节点的 Agent。接收方会话通过上述读取发现待办；无人在线时，由接收方 Agent 的一条 Relay 定时任务唤醒：其 gate `Scripts/collab-inbox-gate.py` 读 `harness-native collaboration-inbox`（已安装程序只读查询，见 `rewrite-native-operations.md`「Collaboration inbox」），只对 `main_only: false` 的合同、在出现新合同或同一合同间隔若干小时仍未结清时启动该 Agent 的会话，由该会话 `inspect` 并按合同答复。主身合同不启动新会话：写了 `target_work_item_id`、且该 WI 正由接收方在本节点 Relay 托管的对话中持有时，gate 请本节点 Relay 在那一个对话里开一轮（对话忙时排在当前一轮之后），同样的间隔规则重投；其余主身合同留在收件箱，等主身会话读取。接收方完成、退回或拒绝后，同一收件箱以 `role: requester` 列出发起方待结清的合同；结清只能由合同所在 WI 的持有者写入，所以该 WI 由发起方在本节点 Relay 托管的对话持有时，gate 同样请 Relay 投递到那个对话，其余非主身的待结清合同唤醒发起方 Agent。这条任务除按计划运行外，合同入账后由本节点 Relay 立即触发一轮：Relay 只读本节点生产账本检出的远端跟踪引用，新增日志条目向某 Agent 发出合同、或答复了它发出的合同时，跑该 Agent 的这条任务（见 Relay `specs/relay-scheduler-jobs.md` §4e），其它节点发出而本节点尚未读到的合同仍等计划运行。同一 Agent 在多个节点时只在一个节点配置这条任务。

## 9. 实施顺序

1. `add_requirement`（第 2 节）；
2. 入口属性（第 3 节）；
3. 平台机器人与确认记录（第 4 节），含 TCloud 定义迁移；
4. 外部入口绑定与启动器（第 5 节），ChatGPT 端用法写入技能文档；
5. `continue_here`（第 6 节）；
6. 同一 Agent 追加记录（第 7 节）；
7. 跨节点按 Agent 指定接收方（第 8 节）。

每步独立 PR，带对应 A 场景测试，合并后随正常发布安装。
