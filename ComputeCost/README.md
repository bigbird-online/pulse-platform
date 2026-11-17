# PULSE ComputeCost

## 现行协作凭据与Stats消费路径

协作凭据查询已用于正式Stats，不再是仅供候选测试的shadow。根[平台入口](../README.md#computecost)说明登记、查询与维护；[需求](../specs/pulse-compute-cost-requirements.md)、[设计](../specs/pulse-compute-cost-design.md)和[任务](../specs/pulse-compute-cost-tasks.md)区分已交付路径与完整R2/R3、质量/ROI等未完成目标。最新已证生产基线集中在[Stats基线](../specs/stats-query-dashboard.md#已证基线与边界)维护。
原UsageLedger和CLI继续使用，legacy与协作凭据不能对同一消费重复相加。[2026-09-13回放](uat/pr513-remediation-uat-20260913.md)是历史限定样本，不是现在的安装状态或全平台完整性证明。
Relay 的 Codex fallback 先排除原生事件已认领的累计区间，再校验未认领增量；已认领行的缓存拆分异常不能导致下一行重复计费。具体计量与去重合同见[设计](../specs/pulse-compute-cost-design.md)。

新增用量消费者应显式声明原生 input 是否含缓存，并复用 `token_semantics.InputTokenClasses`。CC 样本、原生计量、SCL 封存连接、WI 计价及报告均沿此边界换算；未知分区不得补零参与计价。

### 阶段声明保存（WI550 源码接口，未发布采用）

`stage-source --start-byte START --end-byte END` 从当前真实 Pi 会话的完整行边界读取
活动引用、类型和发生时间，不输出消息正文、会话路径或原始身份，也不写账本。
`record-stage --input FILE`（或 `--input -` 读标准输入）将同一有界来源中的声明与
原生用量原子保存到现有 `UsageLedger`；无 WI 也可使用，不创建或启动工作项。
两入口在进程内复用当前安装包的 `NativeHost` 身份检查和 `native_usage.capture`，
不把可提交的 identity JSON 当认证，也不调用旧生命周期写者。

保存目标只取受审原生父进程（Pi、CC 或 Codex）启动时的 `PULSE_COMPUTECOST_SOURCES`，由现有受审
Runtime 环境白名单传入，指向原私有来源登记文件及其 `ledger_root`。Linux 读该进程的
`/proc/<pid>/environ`；macOS 无 procfs，经 `sysctl` `KERN_PROCARGS2`（内核只向同账户提供）读同一份启动环境，
跳过参数串，读不到即拒绝。两者都不读调用进程自身的环境。所有读取消费者
兼容后，登记才可声明 `required_receipt_features: ["stage_semantics.v1"]`；缺少该准入则拒绝保存。
子命令的环境变量、`--ledger-root`、`--source-registry`、会话路径或 actor 参数不能改写目标。
缺登记只令本次保存/查询失败，不阻断已授权工作、启动、派发、接续或新核心恢复入口。ComputeCost 不控制工具集合、不拦截业务调用，也不要求新建 WI/会话或重复授权；计量成功同样不能增加权限。真实 Host/核心权限和计量来源安全检查各守边界。登记目录必须已经存在且归当前账户所有，不能经符号链接重定向。
不得为试用另建生产计量库或绕过候选发布门。

原生 Host 下，普通 `query`、`wi-report WI` 和裸 WI 命令都读取保存入口的同一父进程登记、
原知识水位和价格来源，不自动重扫全部历史。子命令环境、库/价格参数及旧 Harness 快照不能
换掉该来源；缺绑定明确拒绝，不退回旧报告。audit/verifier 可以只读查询，但仍不能保存。
`query --collaboration`、`wi-report` 及其他走协作凭据路径的查询会加载全量计量账本（约 1.9 GB 常驻）；
来源登记中本机原生来源的 `node` 为 `tcloud` 时，CLI 不读账本、不采集，以退出码 69（本节点不提供）拒绝。
这类查询改到 ownermac 执行同一命令，或用 Stats 查询（`Stats/exporter/pulse-stats-query.py` / pulse_stats 工具）。
人类确需在 TCloud 运行时可设 `PULSE_COMPUTE_COST_ALLOW_HEAVY_ON_PEER=1` 绕过。
`source_registration --stage-semantics` 在已有私有 registry 的候选副本加入功能要求，保留
原 ledger、sources、价格及其他设置；不激活、不改父进程环境，也不证明消费者已经兼容。
新节点首次登记可加 `--stage-ledger-root <已存在的规范绝对数据根>`；`--registry` 必须是
尚不存在的正式登记目标，`--output` 仍是另一份排他创建的私有候选。该模式不建目录或账本、
不替换已有登记，不接受采集选择器，不导入 Host 暂存或猜测历史归属；仅显式保存的阶段来源可入账。
正式采用须由节点发布入口绑定真实父进程及所有读取端；准备候选不使当前会话获得绑定。

命令 JSON 封闭为 `{source:{start_byte,end_byte},context_ref,changes}`，最大 64KiB。
`context_ref=null` 时由实际来源首个活动生成；接续使用已保存引用。
每项 change 包含 `key,kind,at,activities,payload,supersedes`：

- `key` 是同一声明的稳定短名称；`kind` 为 `stage_goal`、`stage_output`、`stage_feedback`、`stage_participation` 或 `stage_link`。
- `at` 与非空 `activities` 使用 `stage-source` 返回的活动序号；发生时间来自 `at`，不是保存时间。
- `payload` 只传该领域的语义字段；context、活动引用和 `agent_judgment` 由适配器加入；非 link 不传 WI。
- `supersedes` 默认 null；修正引用同 context、同类型的准确前驱，保留原作者和原事实，新作者另记。
- link 明确列出已知 `target_refs,wi,state,basis`；只可关联有界事实，不接受整会话累计用量、生命周期对象或隐式均摊。

重试保留原命令和来源边界；现有账本负责去重与修正，快照竞争明确失败后沿原输入重试。
原采集器再次看到同一 Pi 调用不增加 Token；真正的新调用仍计入。有界来源缺少继承的模型信息时，
同一调用、原始证据、时间和数量的完整观察只能补齐未知模型，不能抹去已知模型或另增用量；
保存与完整采集的先后顺序不改变结果，已存在的矛盾不能据此自动选边。原文不复制到新库，
来源摘要不另算一笔用量。返回 `saved` 仅证明保存，不证明 Stats 展示、全 WI 覆盖、
真实人时、质量验收或执行授权。当前不自动采集尚未结束的答复、不认定所有 user 记录都是真实人类输入。

### 已保存 SCL 用量接入（WI550 源码接口，未发布采用）

`import-scl-usage --work-item WI --record-id RECORD_ID` 只读取指定原生用量记录，通过当前安装
核心的只读 measurements/artifact 入口取得原记录与私有原件。先核验同一父进程计量库绑定，
再验证查询、原会话身份、区间和原件摘要；不接受调用者给出的原文、路径、身份、计数或库替换。
跨会话导入需要安装核心在原私有 artifact 响应中提供 `harness.saved-usage-source/v1`：
该证据把准确记录连接到原 `open_usage_interval` 已保存的 Host 会话头与命令，并核对原身份和摘要。
消费者只接受该记录的唯一 verified 证据；旧核心或缺失来源仅能核验当前真实原身份，
不能靠相同 WI/actor 猜其它会话。此只读扩展未发布采用，不新增控制入口或原件访问权。

原件沿现有 Pi 解析器生成相同调用引用，用量与确切归属原子进入原 `UsageLedger`，普通查询
直接读取同一水位。SCL 的 assistant 汇总不作为额外用量追加；区间中的独立 compaction 调用
分别保留，tool 子汇总不直接取得区间归属。缺发生时间或未能关联的引用在返回中明示。
不读取范围外模型上下文，不覆盖真冲突，不重计已保存调用；原文不写入 ComputeCost。
来源导入是显式增量动作，不成为日常 query 重扫历史的理由。`saved` 不证明全 WI 覆盖、
Stats 显示、人时或验收；缺正式绑定仍拒绝，不另建生产库或绕过原发布门。

### 准确版本的原生核验观察（WI550 源码接口，未发布采用）

`import-scl-assessments --work-item WI --candidate-id CANDIDATE_ID` 只经当前安装核心的
measurements 查询读取指定候选，不接受外部 JSON、路径、身份、库或决定覆盖。与用量导入一样，
先核验真实父进程的同一计量库绑定；audit/verifier 不可保存。缺绑定或旧核心缺少原决定时间时拒绝，
不补成导入时间、不另建库。候选内容摘要、原结果、核验指派、证据和事件时间须一致。

原核验、人审、质量评分、豁免各形成原 `UsageLedger` 中的 `output_assessment` 观察；它们不是
Agent 声明，`record-stage` 不能提交这种类型。只保存脱敏引用、原决定时间/修订、范围及判据，
不存原文、文件路径或原会话身份。重复导入、账本重开及后来查询修订不重复记录同一原决定，
旧水位不变。零评分不等于未知；撤销豁免作为另一条真实决定保留，不改写独立核验结果。

返回的 `artifact_version_refs` 分别对应完整固定 artifact 引用，`artifact_set_version_ref` 对应
候选完整组成集合（排序不影响引用）。只有内容确实相同的产出声明才能采用对应 `version_ref`；
不把组成文件数计为业务产出数。普通 query 的 `stages.assessments` 仅连接选定范围中同 WI、同
准确版本的产出，保留原候选引用、判据、证据和 `final/intermediate/unknown` 范围；不按同名、
提交前缀或同会话猜测。发布前批准不变为最终批准，旧版本批准不继承给新版本。

这些是已保存的原生观察子集，不是当前有效批准的完整汇总，也不自动把候选整体结论分配成每个
组成的单件验收。普通 query 不重新读取核心。阶段形成数量不变；`outputs.accepted` 仍明确
unavailable。Stats 验收汇总、轻量完成来源、历史 Outcome 对象连接和真实采用尚未验证，不能
凭本接口宣称 A5/A6 完成；该缺口仍属于 WI550 原交付范围。

### 正常事务接续（WI550 源码接口，未发布采用）

已有 WI 用普通 `wi-report WI`；尚无编号时用 `query --context-ref CONTEXT_REF`，引用来自原
`record-stage` 返回。后继沿同一来源登记直接读取 `stages.goals/outputs/feedback/participation`、
`revision_history` 及原 usage/cost 结果。修正历史保留准确前驱、作者、时间和 `current` 标记；
它是解释原目标/产物如何变化的证据，不另计产出或用量。`--watermark` 固定知识截止，晚关联或
重开账本不会改写原查询。正常读取不重采原始会话，不另建恢复文件。

context 选择只包括该事务声明和明确关联的 request/human 事实，不从同会话、同 WI 或同执行
自动扩展。无 WI 可用 unknown-WI link 指明确切活动属于该事务；shared 或 conflict 不均摊。
WI 与 context 同时指定时取交集。重复观察先沿原全局核销，再选范围；已被核销的 request
不会换成整个累计量，其缺项留在 `reconciliation.context_selection.unavailable_usage_refs`。
未关联用量仍保留在原账本，但不冒充该事务的投入。context 的完整库存/ROI 未证实，明确保持
不完整；它不能使用旧 `accepted_wi` 整项验收集合来伪造阶段接受。

生产绑定缺失仍拒绝，不临时注入登记；正常 lifecycle 的计量读取由共享方法指向同一 query，
不修改执行权，也不把 CLI 读取成功当作 Stats 已显示或原生人审通过。

### 新 Harness 查询接入（WI018 隔离验证）

现有 `pulse-compute-cost wi-report WI --harness-query-snapshot FILE` 可以读取新 Harness
`inspect --wi WI --view measurements` 的显式原始 JSON 查询。输出为
`computecost_harness_query_report/v1`，记录文件摘要、原修订及代际；文件不能证明当前
数据新鲜度或独立来源。原私有查询须保存在私有目录，报告仅保留允许的计量字段。
此选项仅用于没有原生 Host 的显式离线历史读回；原生正常查询拒绝该旁路，使用上述同源路径。
离线时它先于旧来源发现，冲突的旧来源或计价选项会报错。它保留部分覆盖、未知值、
原币种及历史否决；不换算账单、不生成新批准或完成、不推断全 WI 生涯成本。

[合同](../agent-harness/project-specs/rewrite-measurements-contract.md#existing-computecost-reader)
及[真实 Muse 读取证据](../agent-harness/project-specs/evidence/rewrite-computecost-consumer-20261008.json)
分别说明边界与结果。当前仅在 WI018 管理工作树通过真实 CLI 读取；生产安装与
自动发布、Stats 消费仍需单独采用和验证。

### WI Outcome与独立归属

`canonical_outcomes.py`读取已登记Harness规范Outcome，`outcome_projection.py`提供稳定对象去重、原状态/时间和有界脱敏名称/结论。通过原delivery/ledger→query→Stats发布物贯通；`wi_outcome_context.v1`须先验证所有读取端兼容再启用。产出不是Token、关闭WI或PR数，同成果跨入口/模型不重复计数，缺成果不抹掉已知成本。
Pi/Codex启动工作区和CC原生assistant消息中的cwd，都按已登记节点、Runtime和精确工作区关系识别Agent，匹配在解析缓存外每轮重读；CC来源须通过既有actor登记启用，不能只设置actor_attribution而缺少workspace_actors。身份不自动推导WI或入口，也不从agent-homes目录名猜测Agent。CC入口独立沿已登记原生消息祖先与同轮有效Host原始输入凭据恢复；未获会话级渠道确认且缺正文／摘要／原Host session等证据仍未知，坏Host文件不阻断其它独立文件，来源仍明确partial。确证的独立非WI诊断沿原reviewed-activity规则追加，不删消费或清空旧水位。

### 现行 Stats 集成与传输

用户于2026-09-19确认新版本基本符合预期。本轮收尾的是登记凭据→原查询→正式Stats消费，不是整个WI568、质量/ROI或全入口覆盖验收；采用版本及实证见[统一基线](../specs/stats-query-dashboard.md#已证基线与边界)。
Stats主弹窗为四栏、最多5行预览，完整八列进入可放大的明细窗口；两处消费同一水位的今日／本周／本月三个联合 activity 视图，旧21视图发布物继续可读；不各自扫描、计价或累加Outcome。菜单的Codex本周百分比来自独立账号额度，Oikos来自独立资源采样，SCL事项来自原工作记录，三者不是本模块的成本或成果凭据。
中央派生登记沿用原source ID和账本；当前同步由 `Scripts/pulse-stats-cost-refresh.py` 调用 `Stats/exporter/pulse_stats/receipt_transport.py` 的 `pulse_stats_receipt_frames/v2`。已证JSONL/last-good前缀只省略已收到的字节，逐帧校验后暂存，完整摘要及全部Receipt验证通过才原子替换。`fact_ref`、修正祖先、旧水位及价格口径不变。
每帧展开上限4MiB、单次线传输上限64MiB，暂存须保留512MiB可用磁盘余量；不是累计历史或完整展开档的64MiB上限，也不是无限容量承诺。未通过容量、前缀、帧、全档校验或SSH断连时保留原事实并报告缺口，不扩大timeout或截断历史。
恢复后，旧transport失败沿原收集器标为excluded、覆盖判断交还原生来源；不造complete业务分母。最新用量读不到会影响当前覆盖，但不删除旧入账用量。缺口、正常在途和未证明完整库存分别解释，精确协议及安装/回退边界见[现行设计](../specs/pulse-compute-cost-design.md#传输安装与恢复)。

### 登记与查询

下列命令从PULSE仓库根运行。配置示例不是生产路径；已有安装按实际服务登记定位，不覆盖运行配置。

After the host installs a private source registry (`0600`), set `PULSE_COMPUTECOST_SOURCES` to it.
The registry provides `ledger_root`, optional `catalog_snapshot`, and explicitly registered sources:

```json
{
  "schema": "computecost_source_registry/v1",
  "ledger_root": "/private/compute-cost",
  "catalog_snapshot": "/private/compute-cost-catalog.json",
  "sources": [
    {"id": "relay-shadow", "kind": "relay_host", "path": "/private/relay-receipts"},
    {"id": "harness", "kind": "harness_root", "path": "/private/canonical-harness/.harness"}
  ]
}
```

These are **configuration placeholders**, not inferred deployment paths. The host chooses reviewed real
roots once, not per-WI paths supplied by users. Set `PULSE_COMPUTECOST_RECEIPT_DIR` on Relay to opt into
its private Host source spool. `pi`, `cc`, `codex` source kinds support registered native log roots;
`harness_bundle` consumes existing validator-bound exports; `receipts` imports typed trusted producer
facts. Root/registry ownership is the trust boundary; hashes are integrity checks, not provider signatures.
Harness YAML import requires PyYAML (`requirements.txt`); JSON sources do not. Source gaps remain visible.
A source may declare a reviewed `work_items` list to bound the impact of its coverage/gaps; this does
not assign native usage to those WIs. Unscoped failures remain global/unknown. A recovered `receipts`
transport retires its old transport gap without manufacturing a complete business denominator.

Usage can carry optional `native_scope_ref` (a native-session correlation, **not** an exact cover list)
and `reported_total_tokens` (a proved inclusive input/output delta). The latter is counted once, never
added to disjoint Token classes or used to price unknown cache partitions. Codex counter origins are
reset only when a first update's native `total == last` proves the restart; unproven changes remain
unknown/partial. Old v1 payloads without these optional fields remain readable.

```bash
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost collect --source-registry /private/sources.json
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost wi_20260828_309
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost query \
  --from 2026-09-01 --to 2026-10-01 --timezone Asia/Shanghai --group-by week,agent
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost query \
  --work-item wi_20260828_309 --scope accepted_wi --from 2026-09-01 --to 2026-10-01 \
  --value-from 2026-09-01T00:00:00Z --value-to 2026-10-01T00:00:00Z
```

The installed Host registry (`~/.local/state/pulse/compute-cost/sources.json`) and
Stats-derived registry reuse the publisher's saved `stats/openrouter-catalog.json`
when no price source is declared. CLI and Stats select one source in the same order:
`--catalog-snapshot`, registry `catalog_snapshot`, then that installed managed cache.
An explicit invalid/missing snapshot is an error, not permission to fall back. An
arbitrary registry or ledger-only read does not inherit another dataset's local prices.
Historical queries never fetch, refresh timestamps, collect or rewrite a ledger.
Receipt-bundle collection hashes a validated prefix through a buffer view and parses
one line at a time. It retains the same v3 committed-watermark and predecessor replay
checks; avoiding byte copies does not authorize dropping ledger history or source facts.
The process-local ledger cache verifies each transaction's byte digest as well as
permissions, ownership and link identity before reusing decoded receipts. Unchanged
timestamps and file sizes cannot conceal changed bytes or permissions.
The refresh writer records its actual selected price in its derived registry and does
not overwrite an explicitly registered snapshot. Missing prices remain unavailable;
known priced usage does not imply complete WI cost, acceptance, quality or ROI.

Outside an installed native Host, the bare-WI examples require the configured environment variable;
explicit offline ledger reads use `--collaboration --ledger-root ...`. Under the native Host, all normal
queries use its authenticated parent registry and are read-only, including live reads. `--as-of`
(ingestion time) or `--watermark` fixes the receipt cutoff; source/price override flags are rejected.
A catalog snapshot is `{ "retrieved_at": "...Z", "catalog": { "data": [...] } }` containing the retained
public OpenRouter catalog. No automatic network lookup or price inference occurs during historical reads.

`activity` reports spending/participation in the window; `accepted_wi` selects WIs by their first known
accepted delivery and reports lifetime costs; `wi_lifetime` has no activity window. Quality and value have
separate natural grain. Role defaults to executor for machine queries; specify `--agent-role responsible`
for owner quality. Runtime/model-filtered WI quality is labeled cohort context, **not causal contribution**.
Monetary ROI requires explicit benefit periods, compatible complete cost, approved/accepted artifact
value and explicit Agent allocation when applicable. Unknown value yields null ROI; it does not hide known
cost or quality. Report-wide hashes bind query/policy/ledger/catalog; full cost needs a WI execution
inventory, not merely all the usage the scanner happened to find.

The canonical Harness review directory supplies exact reviews automatically but cannot prove missing
review history, acceptance, human hours or future value. Historical summary-only attention stays unallocated
in time. Native vs Host cross-source relationships without shared coverage identity remain conflicts;
the current path does not guess or claim all those production relationships have been wired.

### PR #513 source-identity continuation (historical contract)

The actual Telegram/Feishu ordinary text path now retains an authenticated input proof on the
outgoing message object. Participation is captured before startup; the dispatch links that exact
input to its frozen WI binding. A copied prompt, serialized property, or the latest tenant input is
not accepted as provenance. Recovery/fallback retains the original dispatch assignment. Inputs
without a proved WI remain unassigned; this does not claim all command/media paths are wired.

CC Host/native observations of the **same stable request identity and compatible mapping/counts**
are reconciled in the query projection before detail/aggregate cancellation. Original facts stay
immutable, all observation refs are returned, and recorded-at/watermark history remains readable.
Different final quantities/models remain conflicts; conflicting assignment preserves known quantity
in an ambiguous bucket. Native session metadata is taken from the real runtime, not the initial
logical session name. Missing cross-runtime identities are not guessed.

Registered `harness_root` also consumes existing canonical cost-attribution/Token-seal/Pi-pair paths
through the original strict SCL read adapter. `execution_expectation` facts represent **measurement
epochs**, not additional model attempts or Token charges. Queries expose missing and sealed-but-unjoined
epoch refs. The source availability view can be revised when a seal appears/disappears without changing
the sealed artifact or old query view. A sealed epoch is not a complete WI inventory, and no usage is
added just because an expectation/summary exists. An additional partial/conflicting execution source
cannot be hidden by one complete source. Full pre-admission/delegation/child-source inventory and
canonical epoch-to-request quantity joins still require their declared rollout gates.

Source regressions include `test_receipt_source_join.py` and actual TypeScript writer → registry →
existing CLI tests. Their native messages, human identities and benefit values are synthetic; they are
not natural WI UAT or production deployment evidence.

### 部署与混合版本读取不应丢失已有身份

正常重建运行来源选择器时，保留已登记的节点、工作区Actor、SCL根、输入凭据和归属开关；
不能把“重新采用同一目录”变成关闭身份读取。受委派Pi调用沿既有原生前缀/分片连接器，
消费SCL已经接受的计量，而非由主WI owner或目录猜Agent。
合法v1计量不提供v2请求边界，也不能阻断同源v2；复用原validator，损坏摘要仍拒绝。
维护的原生入口从第一笔调用即使用已有Host捕获；已知用途仅为计量，不生成执行权限。
历史精确补证沿原reviewed事实追加，保留Token/价格与旧水位，不将某个目录永久设为Pulse。
实际版本与原14条回读见[归属回归记录](../specs/evidence/stats-attribution-regression-20260919.md)。

## Closeout integrity (2026-09-11)

Canonical machine cash is available only for exact, reconciled usage. A priced known subset remains
`partial_machine_cash_usd` with `cash_coverage=partial_usage`; conflicting producers yield
`cash_coverage=join_conflict` and no canonical cash. The valuation v2 binds the entire source evidence
and independently conserves per-model Token classes; same-WI stale valuations cannot be reused.
Missing historical model partitions preserve incurred Token as partial/unpriced rather than losing it.

Pi capture uses a private OS-locked native writer. Process death releases the lock automatically;
concurrent/live writers cannot be bypassed and retries reuse the immutable final. Existing SCL
baseline/final authority and privacy checks remain mandatory. Install the Python producer and Node
extension as one reviewed release, without overlapping old and new in-flight writers.


下述S1–S4说明保留原库/影子模式的命令边界，不描述现行Stats是否安装；当前部署与Outcome消费以前文和现行Specs为准。

`ComputeCost` is the evidence and valuation core for PULSE compute-cost management. These historical
candidate slices do not register a cron job, enable production configuration, or apply runtime routes.
S1 adds canonical usage adapters and an append-only ledger library; S2 adds a monthly managed-cost
snapshot and a Stats reader that is wired but default-off; S3 adds a shadow window query and a
Duke-facing recommend-only artifact; S4 adds the fixed-order cost-driver bridge plus a digest-bound,
read-only runtime-manager plan. None of these commands authorizes production apply.

**WI machine cash policy:** every Pi, Codex, CC, and OpenRouter producer contributes Token quantities
only. `wi-report` values uncached input, cache read, cache write, and output exactly once at the
model's flat standard rates from a timestamped public `GET /api/v1/models` snapshot, and the
calculated total **is the WI machine cash** (`machine_cash_usd`) used in ROI, independent of
procurement route or subscription. It never reads producer-native `usage.cost`, never selects
`pricing.overrides` or a long-context tier, and never allocates or settles a subscription into a WI or
ROI. A Harness/provider native cash value is only non-authoritative context. Catalog timestamp,
digest, model identity, unit rates, components, and conserving arithmetic remain in the report.

WI730 ComputeCost v2 adds a candidate App Server epoch observer. It is called by the host/client
integration that already receives `thread/tokenUsage/updated` and `turn/completed`; it never asks the
model to self-report usage. Provider thread, turn, and operation identifiers are hashed before the
append-only ledger is written. No production subscription is enabled by this slice.

## Commands

Run tests from the PULSE repository root:

```bash
PYTHONPATH=ComputeCost \
  python3 -m unittest discover -s ComputeCost/tests -v
```

Persist one host-observed App Server lifecycle fact. `bind`, `observe`, and `complete` accept one JSON
notification file; `seal` instead requires the rollover operation reference. The preferred path uses the
host-injected `PULSE_RUNTIME_USAGE_BINDING` JSON or `PULSE_RUNTIME_USAGE_BINDING_FILE`; the explicit WI
flags below remain compatibility-only. Repeating the same logical fact is idempotent and keeps the first
observation time:

```bash
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost app-server-epoch bind \
  --ledger-path /private/compute-cost/app-server-usage.jsonl \
  --work-item wi_20260822_730 --phase work_item --epoch 1 \
  --runtime chatgpt_work --procurement-source chatgpt_subscription \
  --model gpt-5.6-sol --reasoning-effort xhigh \
  --notification /path/from-host/token-usage-notification.json
```

The notification path is a host transport input, not durable evidence. The persisted fact contains only
opaque refs and usage facts; callers should delete transport files according to their host policy.

Query one WI directly. Only uniquely sealed epochs count; open epochs are excluded and make coverage
partial. Missing outcome, rate-card, cash, quota, or downstream evidence remains `unavailable`:

```bash
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost \
  wi_20260822_730 \
  --app-server-ledger /private/compute-cost/app-server-usage.jsonl
```

The query also accepts Harness's read-only `s1_measurement_pilot.v1` projection:

```bash
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost \
  wi_20260822_730 \
  --app-server-ledger /private/compute-cost/app-server-usage.jsonl \
  --measurement-snapshot /private/compute-cost/wi730-measurement.json \
  --review-snapshot /private/compute-cost/wi730-review-show.json \
  --business-value-receipt /private/compute-cost/wi730-business-value.json
```

ComputeCost independently checks the closed schema, WI identity, readout/value agreement, missing
producers, repository-relative provenance, and a canonical content digest before joining quality, exact
human-review status, rework/corrections, downstream effect, efficiency, and prospective human-attention
obligations. The attention vector keeps timer/trusted-telemetry `focused_seconds`, lifecycle
`cycle_elapsed_seconds` exposure, exact bound feedback counts, six category counts, G0 HAU bands/units,
and per-elapsed-day densities separate. Wall clock, waiting, Relay outer turns, verifier handoffs, and
absence never become feedback or focused time; unknown leaves stay `unavailable`. The WI machine cash is
computed only by the flat standard OpenRouter token-cash policy from the digest-bound catalog
(`machine_cash_usd` + `cash_coverage`); a Harness/provider native cash value is surfaced as
non-authoritative `provider_native_cash_usd` and can never become machine cash, basis, coverage, or ROI.
The review input is the non-stale JSON from `harness review show`; ComputeCost recomputes its exact
candidate fingerprint before exposing the reviewed artifact-set digest and repository revisions. Business
value requires a receipt produced by the real downstream consumer, bound to the same artifact set and
independently verified. Quantified baseline/current/delta must reconcile; qualitative value cannot carry
numeric fields. Missing sources remain `unavailable`; subscription balance, quota, Token, model self-review,
or a PR list never fills those gaps.

Probe the current Pi runtime without passing or printing its raw session path/id. Pi injects
`PI_SESSION_FILE`; a managed SCL/runtime host injects a digest-bound v3 binding containing the exact
WI, Agent, phase, epoch, registered execution surface, and opaque native-session authority. Procurement
remains an explicit host fact rather than a provider/model inference:

```bash
PULSE_RUNTIME_USAGE_BINDING_FILE=/private/compute-cost/runtime-binding.json \
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost pi-session-probe
```

Only a post-final host hook may request `--lifecycle-state sealed --measurement-role final`; an Agent-run
probe during generation is an open prefix and cannot seal its own final answer. The legacy bounded
producer accepts only `pulse_runtime_usage_binding/v3`; v1/v2 remain historical compatibility contracts,
not grants. A valid private baseline is required to claim that bounded measurement, never to dispatch
business work. Pending, failed or sealed measurement must not change tools, stop a process, block
compaction or delay dispatch. Real Host and native-core authorization still applies.

Relay startup and turn settlement do not request session statistics. A turn may project its already
received native assistant-call quantities; missing/ambiguous counters, compaction, tool subcalls or
additional usage entries leave that aggregate unavailable, not zero or an assistant-only full cost.
The durable native log retains these facts for bounded capture. An explicit session-statistics query
remains read-only and cannot alter later turn attribution. Retirement neither resurrects a source
for measurement nor waits for sealing: the inline producer is unavailable on this path, without
invalidating historical receipts. Actual busy/starting state, source identity and physical stop failure
still control whether the authorized replacement can proceed.

Native-core profiles do not load this legacy producer or route retained measurement bindings through
the old SCL admission writer. Native saved ranges and the registered ComputeCost reader are the intended
measurement path. The extension's standalone `/wi` controller is retired; historical entries remain
unchanged. Neither missing measurement nor an old entry authorizes another controller or a new WI.
Host conversation-only admission still forbids capture. This limits measurement, not the Host's
chosen tools; a measurement extension never adds tools to a restricted process.

The managed probe includes assistant, nested-tool, compaction, and branch-summary usage, validates Pi's
additive input/output/cache counters, and emits only opaque refs plus immutable source positions/digests.
For a persisted session, the baseline records zero attributable usage at the current append-only position; progress/final snapshots
include only stable entries after that baseline. Exact duplicate entries are folded, changed duplicate IDs
fail closed, post-seal suffixes are excluded, and any changed baseline or sealed prefix is rejected.
Canonical SCL pairs the baseline and final snapshot with the attribution, registered surface, and final
Token seal as a private `scl_pi_usage_measurement/v1` artifact.

These files are rebuildable views of facts already admitted by the SCL kernel.
Missing or damaged views are rebuilt through the original SCL entry or submission retry;
do not recapture a baseline or edit the files to make them agree. Rebuilding retains the
original usage, digests, and timing. ComputeCost reads the resulting artifacts and preserves
missing Outcome and acceptance evidence; read-only measurement queries do not repair files.

Before Pi persists its first assistant response, the Host validates the native in-memory header and
configuration, then records a prospective byte-zero baseline. This boundary survives regenerated
configuration IDs and timestamps on restart and includes all subsequent usage. Host receipt metadata
may be present during idempotent readback; user, assistant, tool and compaction records cannot authorize
an unpersisted origin. A legacy volatile baseline may be migrated only while its transcript is absent:
the old artifact is retained, and SCL must accept the replacement through its immutable-fact check
before the local baseline alias changes. An already accepted different canonical baseline is never
replaced. Ordinary process shutdown records progress; only explicit lifecycle sealing closes the epoch.

A recovered process may retain a valid current v3 binding while referencing the original accepted
measurement through `pulse_canonical_measurement_baseline_reference/v1`. The reference distinguishes
the active binding digest from the original measurement binding and baseline digests; it never
reconstructs a historical binding or changes its `issued_at`. Before reuse, the Host reads the exact
open canonical epoch without initializing it and validates the private baseline against the same
WI/Agent/native Session/epoch/phase/surface and original native cumulative boundary. Progress and final
snapshots continue from that original boundary. Missing or conflicting proof, a changed history prefix,
or a sealed epoch limits measurement reuse without replacing the baseline or fabricating zero usage.
The exact original baseline/final pair and canonical seal remain readable as historical evidence.
A sealed measurement interval never seals execution or blocks WI inspection/recovery. Remaining legacy
coupling must be removed before WI550 delivery; see the [authority review](../pulse-space/memo/2026-10/wi550/authority-boundary-review.md).

`wi-report` discovers that paired SCL artifact automatically from `--harness-root`; it never needs a raw Pi
session path and prices each bounded request from the public OpenRouter catalog at flat standard
uncached-input/cache-read/cache-write/output rates. Catalog overrides are ignored. `--pi-snapshot` remains compatibility-only: a v1 whole-session snapshot
is an unbounded diagnostic even if its caller labels it `sealed`, and a single v2 snapshot is uncommitted
until SCL joins the pair. Neither becomes WI cost or ROI. Native `usage.cost` is always discarded.

For terminal WIs created before that SCL boundary existed, `wi-report` also attempts a separate
`computecost_retrospective_usage_candidate/v1`. It joins the Harness owner and lifecycle events to one
exact Relay Telegram registration/state/Pi-manifest mapping, then windows the registered Pi JSONL and
same pool/session Relay metering. The terminal lifecycle boundary is applied before native usage
validation, so a later record in the reused live Session cannot poison the historical window;
malformed records inside the window still fail closed. It never searches model text for a WI. Ambiguous
mappings fail closed.
The created-to-terminal window is explicitly an estimate that may include unrelated turns; the union of
`in_progress` intervals is an estimate that may omit verification work. Both expose source event counts,
resource totals, reasoning coverage, cross-source divergence, and file modes, but neither enters canonical
usage, Token seal, cost, or ROI. A separately labeled Pi-only OpenRouter estimate may be calculated from
retained per-request quantities; it remains `canonical_attribution=false` and
`canonical_total_eligible=false`. The same terminal read exposes lifecycle elapsed attention exposure and
canonical human approvals while exact feedback/HAU/focus remain unavailable without their producers.
Historical non-0600 usage sources are reported as a privacy defect
and are not modified. Source references are one-way digests; raw tenant/session/chat/path/credential/message
identity is omitted.

A terminal lifecycle disposition and registered/local artifact availability are shown separately from
canonical measurement readiness. Thus a cancelled WI can have retrospective evidence while its canonical
Token and cost remain unavailable. Multi-WI Codex components remain excluded as `join_conflict`, with
candidate evidence typed `present_but_unpartitioned` rather than producer absence.

Read account usage and quota through an explicit App Server host binding. The command does not expose
the binary/CODEX_HOME paths or any account/user/thread identity:

```bash
PULSE_CODEX_APP_SERVER_BIN=/absolute/path/to/codex \
PULSE_CODEX_HOME=/absolute/path/to/codex-home \
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost \
  app-server-account-probe > account-snapshot.json
```

Attach that digest-validated snapshot to a WI report with `--account-snapshot`. Account lifetime/daily
Token remains reconciliation-only; quota is marked `account_not_work_item`, and neither credit balance
nor subscription usage is converted into actual WI cash.

A first-party App Server host that already knows the current raw thread may separately request
`account/usage/read(threadId)`, keep the raw response as transient transport, and sanitize it without putting
the raw id in argv or output:

```bash
PULSE_APP_SERVER_THREAD_ID='<host-only raw id>' \
PULSE_RUNTIME_USAGE_BINDING_FILE=/private/compute-cost/runtime-binding.json \
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost \
  app-server-thread-usage \
  --response /private/transient/account-thread-usage.json \
  > /private/compute-cost/thread-rate-card.json
```

The snapshot keeps only the opaque thread ref, host binding, estimated credits/USD micros, and conserving
group Token facts. Attach it with `--thread-usage-snapshot`. Estimated route credits remain diagnostic;
its non-OpenRouter USD estimate is excluded from machine cost and ROI. The host must delete the raw
response according to its transport policy.

Run the live read-only collector on ownermac, where the management key already exists:

```bash
cd /Users/owner/Coevolution/Bigbird/PULSE
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost \
  openrouter-snapshot \
  --env-file /Users/owner/Coevolution/.env \
  --model deepseek/deepseek-v4-flash-0731
```

The current public OpenRouter unit rates do not require a key:

```bash
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost \
  openrouter-price --model deepseek/deepseek-v4-flash-0731
```

Host integrations can normalize one CC transcript event, CC-SDK event, or OpenRouter request response
with `build_cc_usage_sample()` / `build_openrouter_usage_sample()`. Both require a digest-valid runtime
binding. Claude's uncached/cache-read/cache-creation inputs are added once; OpenRouter prompt input
already includes cached input and is not added twice. Raw event/session/message/request ids, cwd, content,
and credentials are omitted. OpenRouter request `usage.cost` is discarded; request samples carry Token
quantities only. The separately sanitized management activity snapshot remains actual-cash reconciliation
and may corroborate Token/cost conservation, but it is never a competing WI machine-cash authority.


CC/CC-SDK 的受管实际链路为 Relay `cc-usage-measurement.ts` → `cc-session-probe` →
`cc_usage.collect_cc_session_usage` → **`runtime_samples.build_cc_usage_sample`**。每条纳入范围的原生消息
先产生 `runtime_usage_sample/v2`，使用原 Host binding digest，不签发替代 binding。`cc_usage.py` 不再
独立解释 Claude 原始 Token 字段；仅将规范样本投影为原有边界汇总的 uncached input/cache 分区。

`cc_session_usage_snapshot/v2` 是该样本链的字节边界与封存包络，不是第二套样本或计价路径：它保留
baseline/final 前缀摘要、同 message.id 的已验证累积更新、子代理范围和排除量。SCL 消费同一包络，生成
`scl_cc_usage_measurement/v1`；原始 Token、采购来源与价格权威不变。未持有 WI binding 的 CC 日志仍可
通过原凭据采集器记录原始用量和未知归属，不得为构造规范样本而伪造 binding。部署状态按节点单独核验。

Probe current local runtime evidence without exposing prompts, user ids, session ids, or event ids:

```bash
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost usage-probe \
  --machine vultr \
  --relay-metering-dir /home/pulse/.relay/metering \
  --cc-projects-dir /home/pulse/.claude/projects \
  --codex-sessions-dir /home/pulse/.codex/sessions \
  --model-catalog Relay/config/models.json \
  --since 2026-08-01T00:00:00Z
```

A direct WI report can join local Codex and explicitly selected Claude Code evidence without a
prospective App Server ledger. Full and compact WI aliases are canonicalized directly; a short alias
is resolved against canonical WorkItem inventory and is accepted only when unique or disambiguated
by one full/compact identity in the same text. Ambiguous short suffixes fail closed for every candidate.
Codex session-index and user-prompt evidence is joined with canonical Harness lifecycle events, exact
accepted outcome-verifier Handoffs, and their outcome assignment references. An exact assignment ID can seed
one or more child sessions even when the child prompt contains no WI text; active and archived rollout copies
are deduplicated, then opaque delegation/rollover lineage is followed across the complete component. Any
component referencing multiple WorkItems fails closed before event inclusion or valuation. If an assigned
historical session remains indexed but its rollout is gone, assignment/session counts stay observed while
Token coverage is `partial`/`unavailable`, never `producer_missing`. Tool
output and injected workspace instructions never seed attribution. Claude Code discovery is opt-in
with `--cc-projects-dir` and applies the same alias and user-prompt rule at root scope. This discovery
path currently consumes no Task/epoch partition evidence, so it has no exception for a multi-WI
component. A Codex task that has a final `token_count` but no `task_complete` is retained with an
`incomplete_task` quality flag rather than silently losing incurred cost:

```bash
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost wi-report <wi_id> \
  --harness-root /absolute/canonical/.harness \
  --codex-home /absolute/codex-home \
  --cc-projects-dir /absolute/claude/projects \
  --model-catalog Relay/config/models.json
```

The joined report remains host-local. Every automatic report carries a typed `pi_managed_scl` attempt.
A non-Pi/unmanaged legacy execution is `not_applicable`; an expected managed Pi producer is
`observed|producer_missing|producer_unreachable|stale|join_conflict|partial`, with the attempted SCL
attribution/surface/baseline-final/seal adapters listed. Producer absence is never relabeled as a join
conflict. Missing cross-surface Pi or App Server evidence stays explicit in coverage and is never inferred
from another producer. All joined runtimes use the same timestamped OpenRouter
catalog authority and the model's flat standard unit rates. `pricing.overrides`, Pi-derived context tiers,
producer-native cost, and subscription settlement are excluded; missing per-request evidence fails closed.

Query an ISO week directly from the append-only ledger. `--to` is exclusive and both boundaries are
Beijing-local calendar dates. `--price-snapshot` remains the single uniform S3 comparison-price scale.
S4 model-aware counterfactual pricing is supplied separately with repeatable baseline/current bridge
snapshots:

```bash
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost query \
  --ledger-root /path/to/usage-ledger \
  --from 2026-08-31 \
  --to 2026-09-07 \
  --price-snapshot /path/to/comparison-price.json \
  --bridge-baseline-price-snapshot /path/to/model-a-p0.json \
  --bridge-baseline-price-snapshot /path/to/model-b-p0.json \
  --bridge-current-price-snapshot /path/to/model-a-p1.json \
  --bridge-current-price-snapshot /path/to/model-b-p1.json
```

The query emits conserving partitions for agent, consumer, task, model, effort, retry, escalation,
outcome, root agent, execution mode, session role, root task (`lineage.root_run_ref`), cron job, scan,
combined root origin, day, week, and month. It also emits `by_agent`, `by_consumer`, and `by_outcome`
aliases for the weekly consumer. Every partition row carries its current total/share, the same-key
previous-window baseline, and an absolute/relative delta; baseline-only keys remain present so a
negative delta cannot disappear. Current and baseline partition sums are validated independently.
Unknown or missing attribution is `unattributed`; reference-cost shares/deltas fail closed when the
required price evidence is incomplete.

For a seven-day Monday-start window, the output carries the fixed Pro `5000` cent weekly capacity as
context only. It does **not** allocate the monthly `20000` cent managed-cost settlement across weeks,
and it leaves quota pressure/reset/slope `unknown` without producer evidence. S4 emits a fixed
`D → R → E → P → residual` bridge. Complete counterfactual valuation requires producer-owned
`consumer_path/task_family/risk_class`, route/execution evidence, and model prices for both windows.
Missing cohort/counterfactual evidence is never guessed: it produces `insufficient_evidence` and the
unexplained amount remains residual. A weekly Pro-only window also stays insufficient because the
monthly Pro settlement is deliberately not reallocated into a weekly pseudo-cost. Outcome remains a
quality constraint and cost-per-accepted measure, not a fifth cost driver.

Build a Duke-facing artifact from read-only runtime-manager facts:

```bash
PYTHONPATH=ComputeCost python3 -m pulse_compute_cost \
  recommendation-artifact --input /path/to/recommendation-facts.json
```

The input must provide the manager's current/target/rollback presets, approved preset catalog,
allowed transitions, the complete `runtime_priority_plan.v1` returned by manager `plan --json`,
active-route and plan digests, evidence coverage/window, risk/frontier facts, expected
reference-cost/outcome change, and validity timestamps. For promote/rollback, ComputeCost recomputes
the plan's canonical `plan1_` digest and requires its precondition, catalog, transition map, and
single-route diff to match the recommendation facts. The emitted
`recommendation_artifact.v1` is always `recommend_only`; Duke may approve or reject it, but the
artifact never grants production apply authority. Missing/stale price evidence, incomplete outcome
coverage, failed conservation, a critical route below frontier, or an illegal preset/transition
fails closed to `insufficient_evidence` except that an evidence-risk failure may still support an
explicit rollback when the manager transition itself is valid. A missing target preset or a
disallowed manager transition includes the design-contract reason `no_approved_preset`; an arbitrary
model/profile string is never substituted for a named manager preset.


Generate the machine-readable manager plan without writing production state:

```bash
python3 ComputeCost/bin/pulse-runtime-priority.py \
  plan --preset phase23_canary --json
```

`runtime_priority_plan.v1` binds the current/target presets, active/target route digests, current/target
runtime-profile digests, route diff, approved preset catalog, transition map, and a canonical
`plan1_<sha256>`. Any future explicitly authorized `apply --preset` must present the exact
`--expected-active-route-digest` and `--expected-plan-digest` from a fresh plan; the manager rechecks
both before any write and again under its cooperative lock. S4 itself never invokes apply. The
cross-window active-canary lease remains an S5 control, not an S4 shortcut.

`usage_event.v1` normalizes provider token semantics before valuation. Claude/CC reports uncached,
cache-read, and cache-write input as disjoint counters, while Codex reports cached input as a subset
of total input. The adapter reconciles raw runtime events with Relay rows before emitting fallbacks,
so the same turn is not counted twice. Relay/Codex fallback deltas all cumulative counters
independently and never re-adds cache to total input. Recovery or rollover copies are folded by a
one-way hash of stable raw facts before matching.

`build_managed_cost_snapshot()` is a legacy pre-WI-policy monthly shadow retained only for existing Stats
compatibility. Its historical Pro allocation/settlement fields are deprecated and are never consumed by
`wi-report`, WI ROI, or retrospective WI estimates. New WI valuation uses only flat standard OpenRouter
rates. Stats reads the legacy snapshot
file only when the exporter receives an explicit absolute `--compute-cost-shadow` path, or when
`PULSE_COMPUTE_COST_SHADOW_ENABLED=1` and an absolute `PULSE_COMPUTE_COST_SHADOW_PATH` are supplied.
WI300 adds an explicit `managed-snapshot` one-shot materializer; it atomically publishes a private
`0600` file and does not register a daemon or cron.

The OpenRouter snapshot command reads only `OPENROUTER_M_KEY`. It never serializes the key and emits
a sanitized snapshot without API-key hashes, endpoint IDs, workspace IDs, or user IDs.

Candidate contracts:

- `specs/pulse-compute-cost-requirements.md`
- `specs/pulse-compute-cost-design.md`
- `specs/pulse-compute-cost-tasks.md`

受管 Pi 新计量段的 baseline 排除此前全部 usage event。旧前缀仍校验 JSON、entry identity、重复项和源摘要，但不要求被排除的历史 token counters 满足新段计量约束。后续 final 只校验并累计 baseline 之后的用量；旧前缀变更仍拒绝，当前段计数非法仍拒绝。


### WI568 native source registration candidate (Stats integration)

The collector does not discover unregistered native usage. Prepare a separate private registry with the existing ledger and Host/Harness inputs intact:

```sh
PYTHONPATH=ComputeCost python -m pulse_compute_cost.source_registration \
  --registry /private/existing/sources.json --output /private/candidate/sources.json \
  --pi /approved/native/pi-sessions \
  --codex /approved/native/codex/sessions --codex /approved/native/codex/archived_sessions
```

All roots must be explicit existing owned directories; only add `--cc` for an applicable approved real source. The output is newly created mode0600; this command cannot overwrite production, change the ledger root, alter source permissions, or activate collection. Keep active/archive Codex roots in one source (`paths`) so the existing adapter reconciles copied rollouts together. Overlapping incompatible registrations fail rather than silently creating a second input.

After private candidate replay, activate the reviewed registry through the existing controlled deployment. Stats' installed refresh runtime includes its own ComputeCost package: update the publisher's exact runtime as well as relevant collectors; upgrading an unrelated checkout does not update Stats. Do not reinstall the unchanged App merely to update collection. Production two-refresh/restart acceptance remains with WI300; the current evidence is recorded in the [Stats baseline](../specs/stats-query-dashboard.md#已证基线与边界).

已确认的 CC 父子任务共用原 WI，保留各自用量；用户入口可在既有 reviewed-activity 中按原生会话确认一次并由正常采集继承，不要求子代理重新调用 MCP，也不按 WI 名称串联其它会话。

Stats后台计算仅对实际冲突候选进行关联，冻结水位内复用诊断投影；相同价格内容的新拉取可复用原计算见证（原价格观察时间和报表摘要保持），不跳过账本校验。后台日志`stats_cost_calculation_timing/v1`分别记录采集、快照、三个期间计算和缓存命中，业务发布物不含性能噪声。

CC `modelUsage` 的累计解释在 `cc_session_counters.py`，先核验原生引擎、scope/model、精确起始前缀与不回退计数，再保留原生明细和粗时间范围的未展开差额；两者守恒且父子计一次。未知基线、真正数量分叉、相反身份继续拒绝。它是原账本查询投影，不是新账本，详见现行设计；原逐笔价格和源记录不改写。来源追加不再向所有未变旧记录附加新快照指纹，原见证和来源覆盖均保留。

会话用途采集按本批目标限定自身声明版本解析，原生根请求的候选与撤回共用一次解析；历史原始凭据、相反声明和更早用量边界均保留，不增加 MCP 调用或持久 authority 缓存。性能以同输入原输出相等及实际发布器计时分别核对。

同一 Codex 原生轮次的历史进展分叉由现有查询按精确起点及单调证据解释一次；原始版本和真实终值冲突保留，未终结观察仍显示 provisional，详见 ComputeCost 设计。

Harness 人审来源及 `--review-snapshot` 支持原生 v1/v2；v2 保留 exact-candidate 豁免证据，读为人类决策而非独立接受。单独人审不生成新 Outcome，详见 ComputeCost 设计中的“已记录的人审 v2 消费”。

历史读取保持完整校验。单次账本实例按交易前缀增量计算原 facts 摘要；自身追加后及历史水位读取不再解码全部旧 payload。冷读仅共用相等的不可变字符串/字节，内存池有固定上限并随读取结束释放；每条原始凭据、价格、归属、分叉和旧水位不变，不写第二份缓存账本。性能证据须分别报告同输入 CPU/峰值 RSS 和实际刷新时延。


### 历史 verification workspace 身份候选

`verification_workspace.v1` 从显式登记的正式验证 workspace、不可变候选和配对 audit 读取事件发生时有效的 verifier。它不调用当前执行 admission，不将 WI owner 当执行者；每个会话核验自身 native startup 和完整区间。身份/任务绑定 exact usage_fact_ref，旧版本证明不会沿用到越界用量。使用现有 source_registration 的四个 --verification-* 参数生成新私有候选；所有读取端先支持此 feature 后才可采用，生成候选不会部署或激活。缺证/冲突不抹掉用量或补零，真实采用与冻结范围 Stats 回读独立记录。


### Authenticated Relay product tenant context (v2 opt-in)

`pulse_stats_tenant_registry/v2` keeps the v1 exact scope/time `bindings` and adds closed `relay_users` declarations plus `definition_observed_at`. The real Relay `UserConfig.userId` is the product tenant id; its explicit public name is declared separately. The current configuration proves naming only, never past Agent membership. Each historical usage requires the original private Relay input's captured `source.humanRef`, enrolled Host source and node, event time, and the existing exact native/dispatch actor proof. Pool names, paths, WI owners and delivery destinations cannot name a tenant.

The observer extracts identity/time metadata and hashes the immutable original bytes; it does not interpret, retain or export the input body or credentials. CC sidechains must retain the exact original input and dispatch actor witness already validated by the native route reader. An agreeing Host/native request join may refine a coarse Host interval to its proven native request point using the existing quantity reconciliation; projected quantities are never written. Without that compatible witness, stale usage version, contradictory identity, unproved input or direct Desktop/automation origin stays unknown. Independent quantity and actor conflicts remain visible.

Enroll `tenant_context.v1` and `tenant_relay_input.v1` on all readers before preparing the private declaration/source candidate through `source_registration.tenant_candidate_registry`. The real named-user declaration can be prepared with `relay_tenant_context.relay_user_declaration(config_path, registry, names=explicit_public_names, observed_at=actual_naming_time)`. The opt-in publisher still requires `tenant_identity=True`; reader deployment alone does not activate membership or change the current source registry. Historical repair is append-only, anchored to the exact current usage version; old watermarks and original usage stay intact, repeat collection is zero. Missing authenticated membership is not zero cost.
