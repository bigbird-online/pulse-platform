---
name: project-protocol
version: 1.0.0
status: specification
work_item: wi_20260922_018
modified: 2026-10-19
---

# agent-harness 内核接口与数据合同

本文件细化 [功能](project-requirements.md)与[架构](project-architecture.md)。接口由新实现提供；当前仅有隔离核心的部分实现，阶段证据与缺口见 [实施顺序](project-roadmap.md)。所有例示字段均为协议要求，不是对真实 WI 的操作。

## 1. 三个公共调用

```text
execute(Command, HostContext) -> Response
inspect(Query, ReadContext) -> Response
resume(WorkItemId, HostContext) -> Response
```

`Command` 必需字段：`schema`、`request_id`、`intent`、`input`；除创建及明确平台级配置操作外还需 `work_item_id`。request_id 是客户端在首次调用前持久保存的稳定不透明身份；自动重试保留它。内核保存规范化语义摘要，同键异摘要拒绝。忽略 trace/显示时间等非语义字段，但不能忽略输入版本、主体、权限范围和目标。

`HostContext` 来源为受支持 Host 的真实调用证据，包括 actor、私有 session_ref、runtime、surface、资源定位以及可选的原始人类决定。编码器必须验证 Host 来源和实际调用者，不能把任意命令行字符串当作新授权。用户文字表达业务意图，内部身份由 Host 补齐；凭据不进入 Command 或公开输出。

`Query` 包括明确 WI 或有界过滤条件、所需视图和可选历史修订。查询完整历史采用游标分页，不默认读取全会话正文。读取当前事实不要求调用者仍持有历史生产者执行权；数据访问范围仍适用。

`resume` 不是任意执行器：只执行当前已授权图的就绪任务以及内核已记录的外部动作，受本次时间/并发预算约束。没有已确认方案时返回需要的业务决定；等待人类或外部事实时返回等待对象，不发明下一步。

## 2. 命令族与必要语义输入

| 意图族 | 必需业务输入 | 结果与规则 |
| --- | --- | --- |
| create_draft / create_lightweight | WHY、WHAT/验收、owner、来源、流程类型 | 创建一个稳定 WI；批量指派逐项关联原批次身份，部分成功可逐项读回 |
| amend_definition | 当前定义版本、修订内容、理由及所需决定来源 | 新定义版本；原版本保留，受影响候选/图失效范围明确 |
| add_requirement | 当前定义版本、新需求说明、新增验收条件、材料引用、理由及人类决定 | 新定义版本；原定义逐字保留，新条件追加并登记来源；当前计划停止至修订 |
| adopt_plan / revise_plan | 图或计划原文、所依定义版本、确认来源；修订含旧版本及结果复用声明 | 原采用事实/新版本；同一采用重试不增加版本 |
| start_work / claim_task | 当前图、节点、输入绑定；HostContext 指定实际执行者 | 一个任务 attempt 及范围授权；不相交节点可并行 |
| record_task_result | attempt、结果类别、输出/证据引用及执行观察 | 唯一结果；过期结果仅保留历史，不推进新 attempt |
| record_work / checkpoint | 已完成/决定/风险/下一步、版本化成果引用 | 原子进展；不等同任务验收或总体完成 |
| raise_disk_quota | 新限额（大于现限额、不超过 64 GiB）、理由及人类决定 | 仅调高该 WI 的磁盘限额；由负责人或持有者提交 |
| observe_disk_usage（worker） | 总字节、逐资源字节、捕获的测量原件；可选指名该 WI 的副本（逐路径字节）与持有 Agent 的未归属副本（Agent、字节、逐路径字节）；主机取认证的测量面 | 等级或用量实质变化才记录；决定只读最近记录，从不测量 |
| promote_workflow | 原 WI、原因、拟采用标准方案及所需确认 | 当前 profile 转为 standard；启动 profile 与已有事实不变 |
| submit_candidate | 当前定义/图、声明、生产者、成果集合及确切内容 | 不可变 candidate；同内容新的交付轮次仍有独立候选身份 |
| assign_verification / acknowledge_verification | candidate、verifier、职责/范围；ACK 由实际接收者作出 | 原分派/ACK；不等待派生 Git HO 同步 |
| record_verification | candidate、原 assignment、verdict、逐项检查、原回执及证据 | 只接纳被指派独立来源的原结果 |
| record_review | candidate、approve/changes_requested、实际人类输入 | 候选范围内的批准或返工决定；不授予不相关权限 |
| waive_verification | candidate、明确人类来源、原因、风险、观察到的原回执状态 | 豁免事实；不制造独立 accepted |
| revise_work | 反馈来源、执行/方案/目标层级、受影响节点/候选 | 保留历史并进入新轮次，按变化范围消费已有或新的决定 |
| offer / accept / reject / withdraw / reassign | WI、接收者、责任语义、范围、原合同及实际行为人 | 完整责任变化与范围授权区分；多接收者逐一记录 |
| grant_scope / revoke_scope / return_scope | 资源/操作范围、接收者或原 grant、授权来源 | 当前代际内的范围变化，旧 grant 不因重试复活 |
| rollover / transfer | 原执行主体、最小进度、目标能力约束/显式目标 | 一个迁移身份，源先失效、目标后激活；启动进程不是接续成功 |
| authorize_takeover / reinitialize | 原 WI、新人类决定、目标范围、是否明确替换有效目标 | 保留历史；普通接管不伪造旧进度或不必要清空定义 |
| allocate / change_resources | 仓库身份、用途、范围与所需变更 | 已授权真实资源的原操作；不根据登记副本改写真实 Git |
| deliver / observe_effect | 原操作、目标、输入版本；观察含可追溯外部结果 | 持久效果意图/观察；未知先读回，目标不随缓存变化 |
| complete / complete_lightweight | 当前候选/结果、适用验收与交付事实 | 唯一业务终态；标准/快捷仪式不同但无伪完成 |
| stop / supersede / migrate_out / reopen | 原 WI、明确理由/决定、必要关联 | 原身份保留；重开是新有效轮次，旧批准不自动扩展 |
| retire_resources | 原 WI、待退休资源、保留成果和真实占用/发布事实 | 逻辑与物理退役分别读回；不删除唯一成果 |
| record_observation / update_policy | 原观察或配置内容、作用对象、来源及授权 | 原事实/配置版本；只能通过明确的领域命令改变生命周期 |

意图族可以拆为强类型对象，但不得再提供 `set_state`、`save_work_item` 或“任意事件追加”给客户端。平台辅助能力通过自身强类型查询/配置命令暴露；需要创建/修改 WI 时仍调用同一内核。

## 3. Graph 与版本引用

```text
Graph = {
  work_item_id, graph_id, revision, definition_revision,
  objective, acceptance_refs, confirmation_ref,
  nodes: [Node], edges: [Edge]
}
Node = {
  node_id, kind, purpose, executor_role,
  input_refs, output_contract, resource_scope,
  required, completion_rule, acceptance_refs?
}
Edge = {from, to, condition}
Attempt = {graph_revision, node_id, attempt_id, input_digest, executor_ref, generation}
```

Node.kind 为 work、decision、wait、verification、review、delivery。条件必须是可确定判定的已记录结果或被接纳的明确决定，不能执行任意脚本或依赖每次模型重新解释。多前驱默认全部满足；替代分支显式声明所需汇合条件，未选分支不假装已经成功。

执行规则的精确表示：

- `output_contract` 包含命名成果 `artifacts` 和允许的 `outcomes`；`completion_rule` 包含 `join=all|selected` 和 `success_outcomes`。边的 `condition` 是来源节点声明的一个 outcome 或 `*`，不能是脚本或待模型解释的表达式。`all` 要求每条入边成立；`selected` 等全部前驱有固定结果或已确定跳过后，汇合全部选中分支。没有选中入边的节点标记 skipped，不冒充成功。
- 计划采用命令同时保存 `graph` 与 `plan_text`，确认绑定整个命令内容与 WI。修订提供 `previous_revision`、`reuse_results` 和 `reason`；复用必须验证节点契约及前驱结果的输入绑定，不因节点 ID 相同就沿用。显式查询 `compare_plan_text` 只比较用户指定文本，不重新采用或读取当前工作文件。
- `claim_task` 提供 `graph_revision` 与 `node_id`；实际输入摘要、尝试身份和资源占用由内核生成。生产、协作、独立验真、人审、交付角色不能互换；验真/人审/交付节点不能经通用任务结果伪造完成。撤权、图修订和会话交接使原活跃尝试失效。
- `record_task_result` 提供 `attempt_id`、`outcome`、命名 `outputs` 及 `observation_ref`。Host 提交原观察和固定成果字节；输出引用增加 `size`，摘要/大小/对象/版本/访问范围必须全部匹配。原始字节与结果同事务保存。只有原尝试执行者可提交；迟到结果保留为历史，不能更新当前图的节点结果。一个尝试已有的不同结果明确拒绝覆盖。
- 同一任务结果的精确业务重试返回原结果身份。失败或放弃后重新执行必须有新尝试；计划修订对结果复用作显式决定。进程退出、stdout 或空成果摘要不构成任务成功证据。

首次计划需有效确认；同图修订记录原 revision 及变更。新版本的已完成结果复用必须证明输入与输出契约仍成立，否则创建新 attempt。Graph 执行循环通过有来源的新轮次表达；不通过反向修改旧节点结果消除失败。

ArtifactRef 至少包含逻辑仓库/对象身份、相对路径或对象键、版本、内容摘要及访问范围。文件长度/摘要与接纳字节一致。暂存文件、工作目录绝对路径和不断追加日志不是公开事实身份。

### 执行、独立验真与返工的原子衔接

必需执行节点有确定结果且没有仍在运行的尝试时，内核在接纳最后结果的同一事务冻结候选。候选绑定定义、图版本、执行轮次、生产者集合、结果身份和固定成果引用。候选期间不再授予生产者任务写入；正常下一步是给独立验证者分派、由本人 ACK、接纳原回执及人审，而非手工改 WI status。

验证者不能属于候选生产者集合，ACK 和回执绑定同一指派、候选及真实主体。独立回执的源字节须绑定 WI 和整个结果内容；`accepted` 要求全部 AC 有通过结论及捕获的证据原件。其它 verdict 保留其原结论，不能被另一次结果覆盖。人审和豁免绑定确切候选及原人类输入；豁免同时记录原因、风险和已观察到的验真结论，撤回不抹除历史。

`revise_work` 指明当前候选、需修理的执行节点及理由。已有负面验真或要求修改的人审可沿原反馈返工；其它返工须有明确人类决定。内核失效这些节点及下游结果，保留不受影响的上游成果，并在原 WI 建立下一执行轮次。新候选不能继承旧验真或批准。主方案变化使用 `revise_plan` 的新确认与显式复用清单。

标准完成固定所消费的候选、人审、验真/豁免身份。未证明的必需交付和缺少固定成果都拒绝完成；资源退休单独观察。实验核心已接入实际固定文件交付与隔离Git发布/消费检出；必需delivery节点只有真实readback才能解锁下游，不能用模拟验真或进程启动替代实际采用。应用运行进程与生产源的采用仍须另外证明。

### 快捷完成与原 WI 升级

`record_work` 捕获实际成果字节，返回稳定记录身份。快捷完成绑定当前轻量 WI 的全部成果记录和全部 AC 的明确人类确认；没有固定成果不成立。`promote_workflow` 在同一事务改变当前 profile 并采用经确认的 Graph，保留 WI 身份、startup_profile、原成果及有效授权。升级之后不能再走快捷完成，继续按当前 standard 的候选、验真与人审要求。

### 物理资源、固定源与后续交付

`allocate_workspaces` 在一个请求中声明多个仓库的固定base、逻辑范围与稀疏路径；
仓库物理身份来自Host登记。`revise_workspace` 变更现有资源范围；`replace_workspace`
声明原resource_id、原捕获snapshot_id或明确null、目标范围/稀疏/用途。替换只接纳
当前已结原资源，要求旧新范围均被持有且没有活跃任务。一事务记录新allocation和
predecessor/successor关联，新资源实际读回前不放行其业务。旧副本缺失不等于新授权
缺失，但旧路径、分支、登记及唯一文件仍保留在退休库存；替换不隐含删除。

`capture_source` 首次从已接纳资源的真实干净HEAD捕获完整Git bundle，校验commit/
tree/base与实际字节。已捕获的同资源/commit读取其原日志对象，不能因工作树变化
或退休而丢失原请求结果；该读取不声称当前HEAD仍等于历史捕获。新请求继续检查
当前资源权，旧请求重放只返回原结果和当前权限。只有成果输出包含此固定bundle，
候选才会冻结相应source snapshot。

`declare_source_publication` 绑定当前已验真/人审候选、固定snapshot、Host准入target、
expected_head及非空consumer列表。声明与必需publish/adopt效果原子持久，发布不
随当前工作区漂移；非强制快进、远端确切HEAD、每个真实消费检出的HEAD/tree/干净
状态分别读回。发送结果未知沿原attempt观察，不从旧base推断未发或盲重发。

终态责任人可对原批准的固定标准成果或原轻量接受记录继续`declare_delivery`，
使用node_id=null，不重开、不新增生产者grant、不改原完成事实。独立交付义务
仍可pending/unknown，继续限制物理退休。changed-head退休要求该确切捕获commit
已发布且全部声明消费者采用、原bundle保全；未经核心发布的分支头若已被绑定远端
（origin，身份与登记一致）默认分支的已取回引用包含，也可按该头退休。其余唯一提交和
任何dirty内容保留。

以上原生文件与隔离Git实现状态由路线图和精确候选证据说明；跨事实库/跨Host迁移、
应用运行进程与生产源汇聚不由这些记录推断。

## 4. 权威与原始输入

Authority 记录 WI、generation、持有内核位置、真实主体、授权源及资源/操作范围。协作者 scope grant 从属于有效 generation，可被撤销，不等同 owner 变化。持有者所属 Agent 的其它会话只可在持有者自身范围内追加 checkpoint/record_work，不改变持有者或 generation（AH-087）。按 Agent 指定的协作合同由该 Agent 首个接受的会话承担，委派授权绑定该会话（AH-088）。一个节点/资源的执行占用是内核记录，不能由运行进程名或缓存 lock 文件决定业务所有权。

决定源分为原生用户输入、明确的人类签发资料、有效的已有范围授权和被指派独立结果。Host 保存私有原文或内容寻址对象；公开引用只给必要来源标识和摘要。无法证明源内容时不能接纳为批准；已经接纳的原决定不因其导出副本丢失失效。

接纳输入之后，内核拥有确切原件。用户修改工作草稿不修改历史；引用新的草稿需要新命令。独立回执与人审可以描述旧候选，保留其历史意义，但不能转用于当前候选。

## 5. 响应与错误

Response 的最小语义：

```text
schema, request_id, operation_id|null, status,
work_item {id, local_revision, generation, business_progress}|null,
result {original_request_result, current_observation},
capabilities [{name, state, reason, source_revision}],
effects [{id, kind, status, original_request_ref}],
next_action {intent, reason, required_business_input}|null,
error {code, reason, retryable}|null
```

原请求结果与当前观察可以属于不同修订：重试返回原成功，但同时告诉调用者当前已进入下一阶段。不能用当前快照重写原决定的响应，也不能把旧原响应当作当前写权。

| 错误类别 | 允许的后续 |
| --- | --- |
| invalid_input / unsupported_schema | 修正尚未接纳的输入；原已提交请求不可换内容 |
| request_conflict | 指出同键不同意图，不执行第二次 |
| authority_denied / authority_changed | 回报实际范围或代际；新的明确决定经正规业务入口处理 |
| domain_conflict | 指出目标/图/候选/资源的真实语义冲突 |
| source_unavailable / source_invalid | 保留已接纳事实，补原始输入而非修改副本 |
| storage_unavailable / journal_invalid | 只报告可证的持久结果和读能力，不能从视图伪造权威 |
| external_pending / external_unknown | 沿原操作继续/读回，当前本地能力分别报告 |

投影缺失、陈旧和损坏不属于业务拒绝类别；它们是 Views 内部重建原因。重建受容量/权限影响时可输出 view_unavailable 诊断，但读模型和可完成的业务决定仍正常返回。

旧 `scl_response/v1` 的 status/capabilities 由入口适配；不得用一条泛化错误替换其余有效能力。兼容 CLI 的命令级退出码按消费者矩阵保留，新的 JSON 接口以结构化响应为准。

## 6. 支持面与来源清单

[project-interface-map.json](project-interface-map.json) 固定 main 的 185 条 Click 声明（含组与内部命令）及 42 条 SCL parser 命令，并为每项给出 AH 归属和目标职责。这些数字不是业务功能数，也不是已验证的运行入口数；动态挂载和真实调用语法由 A-28 检验。

四个安装入口 `harness`、`harness-scl`、`harness-codex-hook`、`harness-scl-reconciler` 分别承担薄入口、机器入口、原生输入转译和效果推进。Relay、脚本及直接 Python 消费者按实际调用关系补入同一清单；它们只能调用新边界，不能据旧函数仍存在推定兼容。

已有源 Spec 的完整路径和入口声明可定位能力；原任务的实时状态、认证正文和私有日志不进入此清单。任何未分类的新消费者必须明确关联已有 AH 或补充需求，不能偷偷作为旧实现回退理由。


## 7. 图中途验收的候选绑定

验证节点可通过可选 `acceptance_refs` 选择中途需要判断的原 AC；最终候选仍绑定
全部 AC。候选保存对应 verification/review/delivery 节点及先前控制结果，节点
回执不能映射到其它轮次。具名控制输出通过 `outputs` 捕获实际成果引用；后续
节点的输入摘要同时绑定这些结果。交付声明的 `output` 对应 delivery 节点的
命名成果，实际读回才满足依赖。中途候选等待不撤销无关就绪任务的执行权。


## 8. 受管资源意图

allocate_workspaces 输入仅为仓库 ID、固定 base 提交、逻辑 resources、sparse 与用途的
列表；物理 source/common_dir/managed_root/remote 由 Host 已登记绑定提供。多个仓库
的意图同事务记录，实际分配分别观察。revise_workspace 保留原资源身份，要求原
作用域和新作用域均有效且没有运行中占用。受管根迁移后，同一仓库的旧资源留在原路径，
仍可 revise、replace 和退休。

retire_resources 在终态消费确切 resource_ids 清单及 reason 的人类批准。终结 WI
的人类决定同时覆盖其受管资源回收：效果服务对已 completed/terminated、无未结效果和
未结交付的 WI，以 retire_terminal_resources 申请退休全部就绪且未退休的资源；被拒的
退休（dirty、独有提交等）不再自动重提；因 dirty/untracked/ignored 或子模块被拒时，观察写明 `cause` 与至多 5 行 `paths`，结果仍为 unknown，清理后可 `retry_effect`。业务终态不等于资源零：受管 path、branch 和 Git 登记均须实际读回。独有提交、dirty/untracked/
ignored、锁定树及未知外部动作保留。终态 WI 中读回为 observed_absence 的非必需资源意图
（如被拒的修订）阻塞退休，由所有者经人类批准的 cancel_effect 结清。workspace_settlement 只描述新事实库中的
受管清单；空清单不能证明历史资源已退休。派生登记删除不改变日志中的资源身份。

事实库封存迁移的完整目标合同见[封存迁移规范](rewrite-migration-protocol.md)；新格式源封存、认证导出、目标原子接纳/激活和继承效果/资源约束已实现；原生双库采用、跨Host适配器连续性及旧格式转换尚未完成。
