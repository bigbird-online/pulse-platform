---
feature_name: "pulse-compute-cost-management"
version: "0.8.4"
status: "in_progress"
created: "2026-08-17"
modified: "2026-10-07"
owner: "Pulse"
upstream_dependency:
  - "Stats/specs/stats-billing-usage.md"
  - "specs/pulse-runtime-priority-management.md"
tags: ["compute-cost", "openrouter", "stats", "duke", "feedback-control"]
---

# PULSE 算力成本管理 — Requirements

## WI550 待实现契约

全过程重写以[协作计量契约](pulse-compute-cost-collaboration-contract.md)及[试验与判定依据](pulse-compute-cost-measurement-basis.md)落实原 A1–A10：前 WI 产出/投入、阶段产出与贡献、可继承业务语义、有限人时校准及统一查询。下文为现行已发布能力的边界；不得将待实现设计标为已部署，也不得用旧整项 Outcome 口径否决原 A1–A10。

## 现行交付范围与需求（2026-09-19）

本节是已发布的“协作凭据 → 查询 → Stats 产出与成本”需求；身份细节见[归属设计](stats-attribution-identity.md)，消费者、安装和证据见[仪表盘设计](stats-query-dashboard.md)。原 R0–R4、S0–S6 记录保留各自的验收范围，不能把早期 shadow/待部署说明当作当前 Stats 状态，也不能用本次发布代替整个 WI568 收口。

最新已记录生产采用、自然发布/重启、TCloud同步恢复及2026-09-19用户确认集中维护在[Stats基线](stats-query-dashboard.md#已证基线与边界)。本周门仅覆盖已登记来源和本次实际消费，不外推所有入口、历史月账或平台永久无漏。

### 凭据、归属与时间

- 沿用 `UsageLedger` 的版本化协作凭据；执行/归属、用量、人类参与、交付/评审、收益各按自然粒度保存与汇总，再关联。legacy usage 与协作凭据不得对同一消费重复相加。
- 先按稳定原生身份、覆盖证据和修正链核销，再过滤与分组。相同请求的兼容观察保留全部引用；父子复制历史、活动/归档副本只计一次。已知数量或模型冲突不能用最新值覆盖。
- Agent、节点、Runtime、模型、入口分别取证，WI 是独立业务去向。用量没有 WI 不等于异常：确证的定时任务显示 `cron`，普通非 WI 工作显示 `无`；证据不足仍为 `未关联`。真实 WI 优先，不能把空字段批量改成“无”。
- Codex/Pi 原生 session 与已登记活动 SCL lineage 的绑定按有效区间关联；不能以当前 WI、显示昵称、cwd 子串或旧 CWA execution 记录代替。普通业务归属不授予执行权，不以 Token seal 或 WI 完成为前提。
- 当前根、输入凭据目录与迁移路径由目标运行环境和现有登记共同定位；已采用 PULSE 的源不得回退到旧 CWA 根。历史目录只能是明确登记的只读证据，不借计量查询迁移状态。
- SCL 已接纳的计量原件以本地内核为权威；归因、快照、封存和配对文件是可重建视图。丢失或损坏的副本不得阻止原操作重试，也不得要求重新采样或改写原发生时间。重建后 ComputeCost 须得到相同用量、边界和摘要；延迟请求不得回写旧运行时，计量恢复不能制造 Outcome 或验收通过。
- `activity` 是期间发生额，`wi_lifetime` 是 WI 生命周期集合，`accepted_wi` 是按首次已知接受时间选择的 WI cohort；三者不混算。日期为所选时区半开区间，默认 Asia/Shanghai、周一开始。
- 跨期且无法精确拆分的累计保留 `time_unallocated`，不按时间比例拆 Token。已知累计上界早于窗口的记录不污染该窗口。迟到归属或修正只追加；`--as-of`/`--watermark` 固定的是账本当时已知事实，不被来源旧时间回灌。

### WI 产出：内核合并的 PR

决定见 [PR 产出与审核关卡](../pulse-space/memo/2026-10/2026-10-26-pr-output-review-gate.md)（大鸟 2026-10-26/27）。

- Stats 表格“产出”列是该 WI 本期由内核合并的 PR 个数，单位为个：经同一 Agent 异构旗舰审核（85 分以上或人类确认）、由内核 `merge_pull_request` 合并并读回的 PR，计入 PULSE、CollaborateWithAI、Oikos 三个仓库，只数个数，按 PR 登记所在 WI 归属，按 GitHub 合并时间（缺失时用内核读回时间）落入北京时间的今日/本周/本月。来源是内核 `stats-facts` 各 WI 的 `merged_pull_requests`，不从 GitHub 列表、Token 或提交数推算。
- 每个 WI 显示合并 PR 数、同一视图里该 WI 的金额与每 PR 成本；金额只有已计价部分时每 PR 成本同样标明已计价部分。期间内只有合并、没有用量的 WI 单独成行，成本未提供。旧内核导出没有合并列表为未提供；部分 WI 读不到为部分，读不到的 WI 显示未提供；都不当作 0。
- 两类异常与产出并列显示：(a) 近 7 天 GitHub 上已合并进 main、但没有任何 WI 的内核合并记录、且合并时间不早于强制生效日的 PR。生效日由 Stats 刷新配置 `kernel_merge_enforced_since`（ISO 日期，北京时间当日零点起）给出，默认不设；未设或早于生效日合并的此类 PR 只显示参考计数“近 7 天 GitHub 直接合并 N 个（关卡尚未强制）”，不计入异常、不标橙；(b) 直接提交到 main 的改动，即 main 的 first-parent 单父提交，PULSE 内核账本的 `SCL:` 提交除外；GitHub 网页 squash/rebase 合并按标题 `(#N)` 或 GitHub 的提交-PR 关联归回 (a)。GitHub 只经 pulse-gh 只读会话读取，结果缓存，在刷新采集期间并行读取、最多 3 小时一次；读不到的仓库或缓存超过 12 小时标为未核验，计数未提供而不是 0。
- PR 数旁另标所选期间（今日/本周/本月）在三个仓库已合并、但没有任何 WI 内核合并记录的 PR 个数“另有 N 个已合并 PR 未经内核登记”，按分支前缀 `<agent>/` 提示到团队登记表中的 Agent，标明未核实。它不计入产出、每 PR 成本与 Agent 归属，不改内核合并口径；复用上述同一 GitHub 缓存，不增加读取频率；读不全或未覆盖期间起点时未核验，不写 0。

### WI Outcome 验收成果（与 PR 产出并列）

- 验收成果由ComputeCost提供，单位为项，在行详情与统计详情中与合并 PR 并列保留。沿既有delivery/Outcome稳定身份、原状态、发生/接受时间和修订链去重，不从Token、PR、关闭WI或review次数估算。
- 同WI跨Agent/入口/模型的成果引用不能重复加入总数；只有成果没有当期用量的WI仍可展示，成本保持未提供。成果缺失、确证0项、非WI不适用分别表达，缺产出不影响独立有效成本。
- 已登记规范Outcome的名称/结论、状态及安全引用须贯通collector→query→publisher→App；默认折叠不能成为丢失详情的理由。统计已记录成果不等于重做验收、全部接受或完整库存。

### 计价、质量与 ROI

- 唯一机器标准成本沿用实际模型的 flat OpenRouter 标准 Token 价：uncached input、cache read、cache write、output 互斥计价，reasoning 已包含在 output 时不重复计入。不是订阅/API 实际扣款，不采用 native cash、阶梯 overrides 或月费分摊替代。
- 默认 WI、通用 query 与 Stats 共用 `catalog_source.py`：显式快照 > registry 快照 > 正式安装对应的托管快照。任意 registry 或裸 ledger 不借用另一数据集的价表；显式无效输入必须报错。来源/价表摘要、原观察时间及政策随结果保留。
- 四类 Token 均明确为 0 的记录，其标准 Token 成本为 0，即使模型不在价表中；活动行仍保留。任一数量未知不能补 0，非零数量缺价仍未计价；此规则不推断请求固定费或实际账单。未计价原因先判数量：任一类数量未知记 `unknown_token_quantity`，数量齐全而缺模型或价表才记 `missing_price_or_model`。
- `partial_machine_cash_usd` 只报告已计价部分；完整 `machine_cash_usd` 仍需独立 WI 执行库存、数量/价格覆盖与无未决冲突。归属全部已知不等于全量完整；进行中 WI 的最终成本不可用可以是正常结果。
- 质量只消费对应候选的真实交付/审核/Outcome；比例分别返回分子、分母和未知数。没有可用质量凭据不是产出为 0，合并 PR、测试条数、checkpoint 不能替代质量指标。
- ROI 保留 `(V-C)/C`：同一对象集合、明确收益观察期、完整兼容成本与可验证货币收益；按 Agent 还需收益分配依据。未知收益、零分母或缺完整成本返回 null；不把未知当 0 或 -100%，不按 Token 比例猜收益。

### 查询、复用与生产消费

- `query`/`wi-report` 通过 registry 或 `PULSE_COMPUTECOST_SOURCES` 选择协作凭据路径。未固定水位的已配置查询会先运行登记采集并可能追加账本；固定 `--as-of` 或 `--watermark` 才是历史只读，不采集、不联网换价。无需用户逐 WI 拼装 snapshot。
- Stats 只消费 `computecost_collaboration_query/v1`，由 ComputeCost 生成 `pulse_stats_cost_dashboard/v1`；前端不重计价、不从独立分组拼表、不用汇总掩盖缺口。打开、筛选、排序和正常 App 刷新不扫描原生日志。
- 默认复用内容及解释依赖未变的解析、已计价数量和报表；数量/模型/价表/代码、归属或期间变化按依赖失效。`--force-recompute` 经受管 refresh 贯通解析与报表/计价，缓存可删除且不是另一账本。
- 来源仍需检查，SCL/Host关系每轮读取。坏缓存回原计算，坏来源保留错误。增量传输证明已收前缀后只传新增后缀，完整验证后原子替换；断线不把last-good当本次成功。不能用累计历史64MiB上限使正常增长的已登记来源永久停发，现行有界分帧、磁盘容量与超时合同见[传输设计](pulse-compute-cost-design.md#传输安装与恢复)。
- 传输不得删除/截短历史、改变原fact_ref/修正祖先、提高限制或伪造完整覆盖来消除错误。恢复时验证此前事实仍在；真实后续断连仍显示缺口，原调度恢复后再退役本轮失败。
- Stats轻量预览与完整明细窗口共用同一21视图发布物；筛选/打开窗口不采集或重算。菜单额度、资源健康、SCL事项年龄及计划暂停不当作成本/成果证据。同步失败点名来源与影响，不能以“主机故障”“没有用量”或未知补0代替。
- Host 的 `accepted/running` 待回报执行与读取故障分开，期望/实收及 partial 不变；只有有时间边界且无相关捕获缺口的明确在途才是 `host_usage_pending`。终态缺用量、unknown、文件错误和断线仍异常。
- 发布验收覆盖候选 → 实际安装 → 两次自然更新 → App 重启，同窗口、同水位、同价表核对全部 21 视图。零读取错误和在途分类不撤销历史捕获缺口，也不把完整覆盖从 unknown 改成 complete。

### 本次不扩展的范围

月度历史清理、所有 Runtime/入口的完整执行库存、SCL 质量来源闭环、完整 WI ROI、父账分摊和 S5/S6 建议/控制推广仍按原证据分别验收。[receipt-first 方案](pulse-compute-cost-receipt-first-iteration-plan.md)保留五类凭据的目标，不因用量路径已发布而自动完成。

## WI568 closeout integrity correction — 2026-09-11

This section tightens existing measurement correctness; it does not waive WI568 acceptance criteria.

- A canonical machine-cash valuation must bind its complete usage evidence digest, not only WI identity.
  The consumer independently reconciles model and Token-class quantities with the report's selected sources.
  A valid arithmetic/self-digest on a different cohort cannot price the current report.
- Known subtotal agreement cannot hide partial discovery, unsealed expected epochs, or model disagreement.
  A priced incomplete subset is `partial_machine_cash_usd` with `cash_coverage=partial_usage`;
  `machine_cash_usd` stays unavailable. A producer conflict keeps canonical cash unavailable with
  `cash_coverage=join_conflict`. Unpartitioned historical incurred Token remains visible but incomplete/unpriced.
- Pi sampling and private atomic publication must be covered by the actual writer's OS-owned lock.
  Killing that process releases the lock; killing its parent cannot authorize a contender while the writer
  remains alive. Retrying after a published final must validate and reuse the immutable winner, not resample.
  No directory age, PID guess, timeout, or manual lock deletion grants writer authority.
- Baseline/final source boundaries, exact Host/SCL authority, private modes, and flat OpenRouter standard
  pricing remain unchanged. Ordinary shutdown is progress, not implicit final sealing.


> 本功能建立“采集 → 统一计价 → 解释 → 建议 → 受控执行 → 结果反馈”的算力成本管理闭环。
> 它不是租户商业账单，也不是只读仪表盘。

## 0. WI730 ComputeCost v2 纠正

本节保留 `wi_20260822_730` 的 v2/epoch 计量要求，优先于其后的早期订阅分摊口径。当前协作凭据与 Stats 发布范围见页首；下面的候选状态仅属于当时的 v2 切片，不代表整个生产采集仍未启用。

1. ChatGPT Work / Codex 宿主必须直接消费 App Server 的
   `thread/tokenUsage/updated`。`total` 是线程累计水位；WI/epoch 用受控边界水位做非负差分，
   `last`、输入、缓存输入、输出、推理输出、总 Token 与上下文窗口按 producer 事实分别保留。
2. source epoch 只有同时观察到最后一个 `turn/completed` 和该 turn 的最终 usage 水位后才能封账；
   checkpoint/rollover 自身消耗归 source，target 从新基线开始。重复通知、重启、resume、compaction 和
   rollover recovery 必须幂等；水位倒退、跨 thread、缺基线或缺最终 turn 一律 fail closed。
3. observer 位于宿主 App Server/client integration 层，不得让模型运行命令自报成本。只读 session state、
   DOM、conversation export 和社区估算均是带 coverage 的降级 adapter，不能标成 exact。
4. execution surface、runtime、model/provider、procurement source 与 usage producer 正交建模。采购来源只可
   来自 credential/billing route evidence，不得从模型名、hostname、cwd 或路径推断。
5. `chatgpt_subscription` 月费不按 WI 分摊，procurement route 也不参与 WI machine cash 计算。machine cash
   唯一由 flat OpenRouter standard Token 政策（见 1.5.4 / item 26）得出；可计价时等于政策总额，否则为
   `unavailable`，绝不是 producer 可证明的 cash 或 `0`。Token、provider-native cash、policy machine cash 与
   quota pressure 四列并列，不得互相反推。
6. usage observation 只保存 opaque lineage、WI/phase/epoch、runtime、procurement、model/effort、水位与
   coverage；禁止持久化 raw thread/session/turn id、prompt、response 或 credential。
7. 最终 `computecost <wi>` 必须零手工拼接返回产出、质量、Token/credits/cash/quota、成效、效率、coverage、
   provenance、守恒与缺口告警。estimated/degraded 信号不得无标识进入 WI730 自动决策。
8. Pi 主路径读取 host-injected `PI_SESSION_FILE` 的原生 assistant、nested-tool、compaction 与 branch-summary
   usage。Pi 的 input/output/cache-read/cache-write 是可加计数器，`totalTokens` 必须与四者之和守恒；reasoning
   只作 output 子集。raw session/entry/response id、cwd、正文和源路径不得进入事实或报告。
9. SCL/runtime host 必须以 digest-bound `pulse_runtime_usage_binding/v3` 显式注入 WI、Agent、phase、epoch、
   runtime、procurement source、registered execution surface 与 privacy-safe runtime authority ref；`v1/v2` 只作历史
   历史兼容，不能启动新的 bound Pi epoch。已保存的 v2 绑定在摘要、原 Session 和工作目录准入校验通过后，必须能恢复原对话；保留原绑定，不凭当前 Host 补造历史 execution_surface 或 v3 摘要。该历史会话的 v3 bounded measurement 明确为 unavailable，不能封存为精确计量。绑定不得含 raw session/thread/turn/message/path/content/credential identity；模型、
   provider、cwd、hostname 与文件路径都不能补推缺失绑定。Pi、Codex 与 ChatGPT/App Server producer 必须拒绝
   runtime 不匹配、digest 篡改、双 binding source 或手工参数冲突；未绑定保持 unavailable，不填默认值。
10. source epoch 只能由宿主在最终 assistant 已持久化、自动 retry/compaction/follow-up 均结束后标为 sealed；
    模型进程内自报、最终答复前 probe 或普通 open-prefix snapshot 都不能冒充封账。target baseline 必须消费
    下一 execution epoch 的新 host binding，不能复用 source binding。Pi target 的 `session_start` 可在 canonical
    successor adoption 前预取 baseline；若此时 authority 尚未生效，进程必须保持 non-runnable，Relay 在 adoption
    后通过 host-only baseline command 重试，并同时读回 mode-0600 artifact 与 SCL `open` measurement 后才可发送
    首个模型 turn。失败或 Relay restart 必须复用同一 target/binding，不得放行普通消息或补录已发生的 usage。
    最终持久化后的显式 seal 至多写出一次 sealed snapshot；普通 shutdown 只记录 progress；缺 reviewed host hook、Python/path binding、
    exact runtime binding 或 baseline readback 时必须拒绝业务执行。
11. 运行中 ChatGPT embedded App Server `0.149.0-alpha.4` 是当前 producer contract：cache-write 必须保留，
   context window 可为 unavailable。该版本 `account/usage/read` 已可按 thread 返回 estimated credits/USD；
   它可作 billing-route reconciliation，但不得替代 notification Token 主账，也不得冒充 actual billed cash。
   host 必须用同一 raw thread 在进程内核对后只保存 opaque thread ref、显式 runtime binding、group Token 与
   estimated micros；group credits 与 thread total 必须守恒，cache/net-new/total Token 可复算时也必须守恒。
   raw thread/account/user identity、响应中的 account summary 与 credential 不得进入 thread snapshot。
12. account-wide usage/quota 只作为上下文：lifetime/daily Token 不得进入单 WI usage，quota pressure 必须标
    `account_not_work_item`。无 add-on credit、余额为零或 subscription 额度内，都不能自动推导单 WI machine
    cash 为 `0`：policy machine cash 未由 digest-bound catalog 计价时就是 `unavailable`。
    account/user/thread id、CODEX_HOME、user agent、reset-credit id 与私有 bucket key 不得落入 snapshot。
13. Harness 的 `s1_measurement_pilot.v1` 是 WI 质量、人审、返工、成效与效率的只读 producer；其
    `actual_cash_usd` 携带 provider 原生现金事实。ComputeCost 必须独立复核封闭 schema、WI identity、
    readout/value 一致性、coverage、repo-relative provenance 与内容 digest；缺 producer 保持 `unavailable`。
    原生 cash（包括可证明的 `0`）只投影为非权威 `provider_native_cash_usd`，不得改写 policy machine cash/
    basis/coverage/ROI；不得由订阅余额、quota 或 Token 反推 machine cash。artifact set 与 business value 的
    对应 producer 未提供时继续保持 unavailable，不能用 review 状态替代。
14. 产物集合只可来自 exact-candidate `work_item_review/v1`：ComputeCost 必须重算 AC、artifact-set、repo heads
    组成的 candidate fingerprint，拒绝串 WI、stale export、repo revision 篡改与不安全 evidence ref。业务价值
    只可来自 downstream consumer producer，并由不同 verifier 复核；receipt 必须绑定 accepted outcome、同一
    reviewed artifact set、时间、量化 baseline/current/delta 或明确 qualitative statement，以及 repo-relative
    evidence + SHA-256。缺 approved review、accepted delivery、consumer evidence 或独立 verifier 时 business
    value 保持 unavailable，不允许 Agent 自评填充。
15. CC、CC-SDK 与 OpenRouter request sample 必须消费显式 runtime binding；CC/SDK 的 procurement 不得再由
    model/provider 反推。Claude 原始 uncached/cache-read/cache-creation 三类 input 必须相加为规范 input；
    OpenRouter prompt 已含 cached，不得二次相加，且 prompt+completion=total。OpenRouter response cost 只标
    request-level provider report，管理接口 activity 才是 actual cash reconciliation；两者不得互填。所有 sample
    只保留 opaque event ref、model/effort、Token、binding digest 与 coverage，raw event/session/message/request、
    cwd、正文、credential 和管理 key 均不得落盘。
    受管 CC/CC-SDK 的 `cc-session-probe` 必须实际调用同一 `build_cc_usage_sample`，不得另置原始 Token
    归一化实现；`cc_session_usage_snapshot/v2` 仅封装这些样本的不可变 baseline/final 字节边界与守恒汇总，
    供 SCL 生成 `scl_cc_usage_measurement/v1`。不得因此改变既有用量、价格、采购归属或未绑定来源的 unknown。
16. 本地 Codex/Claude Code WI discovery 接受的 full、compact seed 必须直接规范化；short seed 必须根据
    canonical WorkItem inventory 解析，仅在 suffix 唯一，或同一文本有唯一 full/compact identity 消歧时接受。
    suffix 对应多个 WI 且无法消歧时，所有候选查询均须 fail closed。随后在 Codex session index + user prompt
    的并集及完整 root/delegation component 上判冲突；任一 component 出现多个 WorkItem identity 时，整个
    component 必须在 event inclusion 与估值前 `join_conflict`。当前版本不消费 Task/epoch partition evidence，
    因此不得声称存在多 WI component 的 partition 例外。
17. 同一 WI 的 bound session 不得通过 Pi `/new`、`/resume`、fork/clone，或 Relay `/new`、`/reset`、跨 runtime
    切换绕过 Harness lifecycle。正常替换必须 rollover，失效 source 必须 recovery。生命周期完成与计量完成是
    两条状态轴：source fence、continuation consume/apply 与 binding migration 决定 rollover completion；Token seal
    缺失时记录 `unavailable` 并排除 ComputeCost measurement/cohort eligibility，不得阻止普通故障恢复，也不得写成 0。
18. 新 managed Pi epoch 只接受 `pulse_runtime_usage_binding/v3`，并同时绑定 Host-issued WI、Agent、phase、epoch、
    registered execution surface 与 privacy-safe native Session authority；v1/v2 仅可读历史事实。Relay 只为该 binding
    加载 reviewed usage lifecycle extension，并在任何 business dispatch 前独立读回同 binding 的 mode-0600、digest-valid、
    attributable usage 为零的 baseline。缺 extension command、binding/surface/session 不匹配、baseline 非零或 SCL open
    readback 失败都必须保持 non-runnable。无 binding 的普通 Pi 保持可用，但只能标 `unmanaged_unmeasured`。
19. Pi canonical usage 必须由 immutable baseline source position/digest 与 settled final position/digest 配对：只包含
    baseline-exclusive/final-inclusive 的 stable entry；baseline 前与 final 后 entry 显式排除；相同 stable entry 重试折叠，
    同 id 内容变化、baseline prefix 或 sealed final prefix 变化一律 fail closed。input、cache-read、cache-write、output 与
    reasoning 子集语义逐 request 守恒，不能用 whole reused/branched JSONL 或 WI wall-clock 时间片替代该边界。
20. `wi-report` 必须从 canonical SCL attribution、registered surface、paired Pi baseline/final 和 final Token seal 自动
    发现 Pi，不要求 raw session path。每份报告必须有 typed `pi_managed_scl` attempt；absence 只能按事实标为
    `not_applicable|producer_missing|producer_unreachable|stale|join_conflict|partial`，不得默认为 generic unavailable 或把
    缺 producer 写成 join conflict。显式 `--pi-snapshot` 只作兼容诊断：open prefix 不是 final cost，unbounded whole-session
    即使自称 sealed 也不能进入 WI cost/ROI。ROI-eligible terminal WI 缺 attribution 或 final bounded Token seal 时 measurement
    必须 incomplete；legacy non-eligible terminal WI 保持可读，但 measurement 明示 ineligible/unavailable。
21. 对 baseline/final/Token seal 能力上线前已经终态的 WI，`wi-report` 必须额外尝试
    `computecost_retrospective_usage_candidate/v1`。该 reader 只可用 Harness owner + lifecycle events，以及 Relay
    Telegram registration、state 中 exact pool/agent/runtime/session 一致性和 Pi manifest session root 建立唯一 Host
    映射；不得扫描模型正文中的 WI 字符串。一个 owner 对应多个可成立的 tenant/session 映射、config/state/session
    不一致或 source malformed 时必须 fail closed，不得选一个最像的来源。
22. retrospective candidate 不是 `usage_event` cohort。created-to-terminal 只能标
    `estimate_may_include_unrelated_turns`；由每次 `work_item.in_progress` 到下一次离开 in_progress 组成的多区间窗口
    只能标 `estimate_may_omit_verification_work`。Pi assistant usage 与 Relay settled outer-turn metering 可交叉核对并显示
    event count、input/cache-read/cache-write/output、reasoning coverage、total、绝对差和比例；缺字段/来源保持
    `unavailable`，不得补零。Pi 内部 compaction/tool usage 与 outer turn 不可直接比较时必须单列排除量。
23. retrospective candidate 必须固定 `canonical_attribution=false`、canonical usage/Token seal/cost/ROI eligibility
    全部 false，且永不进入顶层 canonical Token、分区、定价或 measurement readiness。source ref 只能是单向 digest；
    raw tenant/session/chat/path/credential/message 不得输出。历史 usage source 非 0600 必须报告 `privacy_defect`，reader
    不得 chmod。终态状态、registered/local artifact availability 与 canonical measurement readiness 分轴呈现；Codex
    multi-WI component 继续 `join_conflict`，但必须明确 `present_but_unpartitioned`，不得写成 producer absence 或把
    component totals 分给 WI。
24. Codex verifier 归因必须从 canonical WI lifecycle + exact accepted `outcome_verification` Handoff +
    outcome `verification_assignment` 建结构化 assignment inventory。child prompt 不需要出现 WI；只要 exact assignment
    ID 可绑定到 WI，即可沿 session metadata、delegation 与 rollover source lineage 找到 active/archived rollout。
    active/archive 同 Session 的相同或前缀副本只计一次；mixed-WI component 整体拒绝。终态 WI 即使 rollout 已删除，
    仍显示 observed assignment/verifier/session counts；Token 为 `partial|unavailable`，不得写 `producer_missing`，也不得
    从 outcome/verification prose 反推 Token。
25. WI report 的 human attention 必须是向量：timer/trusted telemetry 才能形成 `focused_seconds`；canonical created→terminal
    形成 `cycle_elapsed_seconds`，明确只是 attention exposure/open-loop；exact WI-bound human events 才形成 feedback count、
    `human_touch|review_approval|correction|re_explanation|manual_retry|rework`、G0 HAU bands/units 及 elapsed-day density。
    prospective event set 优先并去重 retrospective subset。terminal retrospective 可从 lifecycle/review 保留 elapsed/approval，
    但 shared-session time window、Relay outer turn、verifier Handoff、waiting 或 absence 都不得变成 feedback/focus；缺 exact
    Relay per-turn WI binding 时明确 `unavailable`，不做 post-terminal canonical backfill。
26. WI machine cash 的现行唯一政策是：每个 producer 只贡献 Token quantity；按 timestamp+digest 绑定的公开
    `GET /api/v1/models` 中该模型 top-level standard prompt/cache-read/cache-write/completion rate，各计一次。
    守恒的 Decimal 总额就是 `wi-report` 的 `machine_cash_usd`，用作 ROI 成本，独立于 procurement route 或
    subscription。`usage.cost`、`provider_reported_cost`、`pricing.overrides`、long-context tier 和 subscription
    allocation/settlement 一律不进入 WI report/ROI。retrospective Pi-only estimate 必须标
    `canonical_attribution=false`、`canonical_total_eligible=false`、`may_include_unrelated_turns=true`、
    `noncanonical_total=true`；模型身份、标准单位价格、component 与总额必须 Decimal 守恒。

`@anchor symbol RuntimeUsageBinding`
`@anchor symbol load_scl_measurement_snapshot`
`@anchor test ComputeCost/tests/test_scl_measurement.py :: SclMeasurementTests.test_zero_manual_join_preserves_unknown_cache_write_and_eligibility`
`@anchor test ComputeCost/tests/test_scl_measurement.py :: SclMeasurementTests.test_cli_auto_loads_harness_evidence_without_per_artifact_flags`
`@anchor symbol load_runtime_usage_binding`
`@anchor symbol AppServerEpochObserver`
`@anchor symbol AppServerUsageLedger`
`@anchor symbol build_wi_report`
`@anchor symbol collect_pi_session_usage`
`@anchor symbol verify_pi_snapshot_source_prefix`
`@anchor symbol discover_pi_scl_wi_usage`
`@anchor symbol discover_relay_pi_retrospective`
`@anchor test ComputeCost/tests/test_retrospective_usage.py :: RetrospectiveRelayPiTests.test_report_surfaces_candidate_without_tokens_cost_or_roi`
`@anchor symbol build_pi_session_snapshot`
`@anchor symbol validate_pi_session_snapshot`
`@anchor symbol probe_app_server_account`
`@anchor symbol validate_app_server_account_snapshot`
`@anchor symbol build_app_server_thread_usage_snapshot`
`@anchor symbol validate_app_server_thread_usage_snapshot`
`@anchor test ComputeCost/tests/test_app_server_thread_usage.py :: AppServerThreadUsageTests.test_wi_report_joins_rate_card_without_claiming_policy_cash`
`@anchor symbol validate_harness_measurement_snapshot`
`@anchor test ComputeCost/tests/test_harness_measurement.py :: HarnessMeasurementTests.test_wi_report_keeps_native_zero_cash_non_authoritative_under_policy`
`@anchor test ComputeCost/tests/test_cash_policy.py :: CashPolicyTests.test_canonical_priced_usage_populates_machine_cash_and_coverage`
`@anchor test ComputeCost/tests/test_cash_policy.py :: CashPolicyTests.test_harness_native_cash_cannot_override_policy_machine_cash`
`@anchor symbol validate_harness_review_snapshot`
`@anchor symbol validate_business_value_receipt`
`@anchor test ComputeCost/tests/test_delivery_value.py :: DeliveryValueTests.test_wi_report_joins_exact_artifacts_and_verified_business_value`
`@anchor symbol build_cc_usage_sample`
`@anchor symbol build_openrouter_usage_sample`
`@anchor test ComputeCost/tests/test_runtime_samples.py :: RuntimeSampleTests.test_openrouter_request_sample_discards_native_cost_and_keeps_tokens`
`@anchor test ComputeCost/tests/test_wi_discovery.py :: WiDiscoveryTests.test_structured_assignment_discovers_multiple_no_wi_prompt_children_and_deduplicates_archive`
`@anchor test ComputeCost/tests/test_wi_discovery.py :: WiDiscoveryTests.test_structured_assignment_without_rollout_preserves_session_count_and_token_gap`
`@anchor test ComputeCost/tests/test_attention.py :: AttentionVectorTests.test_more_exact_feedback_reports_more_burden_at_equal_elapsed_time`
`@anchor test ComputeCost/tests/test_wi_pricing.py :: WiPricingTests.test_historical_pi_flat_standard_subtotal_is_exact`
`@anchor test ComputeCost/tests/test_wi342_uat.py :: Wi342ShapedEndToEndUat.test_structured_children_attention_and_flat_price_are_visible_without_privacy_leaks`
`@anchor manual wi779-material-defects-uat :: ComputeCost/uat/wi779-material-defects-uat-20260909.md`
`@anchor manual wi779-cash-policy-uat :: ComputeCost/uat/wi779-cash-policy-uat-20260909.md`
`@anchor test ComputeCost/tests/test_app_server_usage.py :: AppServerUsageTests.test_prospective_epoch_binds_updates_completes_and_seals`
`@anchor test ComputeCost/tests/test_app_server_usage.py :: AppServerUsageTests.test_raw_thread_turn_and_operation_ids_never_persist`
`@anchor test ComputeCost/tests/test_wi_report.py :: WiReportTests.test_report_sums_only_sealed_epoch_deltas_and_conserves`
`@anchor test ComputeCost/tests/test_wi_discovery.py :: WiDiscoveryTests.test_short_and_compact_aliases_taint_multi_wi_codex_sessions`
`@anchor test ComputeCost/tests/test_wi_discovery.py :: WiDiscoveryTests.test_short_only_seed_fails_closed_for_duplicate_suffix_inventory`
`@anchor test ComputeCost/tests/test_wi_discovery.py :: WiDiscoveryTests.test_full_identity_disambiguates_duplicate_short_suffix_once`
`@anchor test ComputeCost/tests/test_wi_discovery.py :: WiDiscoveryTests.test_index_target_and_rollout_other_wi_taint_the_same_session`
`@anchor test ComputeCost/tests/test_wi_discovery.py :: WiDiscoveryTests.test_ambiguous_child_taints_unique_parent_root_component`
`@anchor test ComputeCost/tests/test_pi_usage.py :: PiUsageTests.test_native_session_usage_includes_assistant_nested_and_compaction`
`@anchor test ComputeCost/tests/test_pi_usage.py :: PiUsageTests.test_branched_reused_session_charges_only_baseline_to_final_delta`
`@anchor test ComputeCost/tests/test_scl_measurement.py :: SclMeasurementTests.test_expected_pi_without_a_bounded_final_is_producer_missing_not_join_conflict`
`@anchor manual wi730-pi-native-live-probe :: ComputeCost/uat/wi730-native-producer-uat-2026-08-25.md`
`@anchor manual wi730-app-server-account-uat :: ComputeCost/uat/wi730-app-server-account-uat-2026-08-25.md`
`@anchor manual wi730-combined-live-report-uat :: ComputeCost/uat/wi730-combined-live-report-uat-2026-08-25.md`

## 1. 已确认的产品口径

1. OpenRouter `GET /api/v1/models` 是 WI machine cash 唯一估值 authority。Pi、Codex、CC 和 OpenRouter producer
   只交付 Token quantity；不能交付或覆盖价格。由 1-4 条算出的 Decimal 总额就是 `wi-report` 的
   `machine_cash_usd`（basis=`openrouter_standard_token_cash`，coverage=`calculated_from_digest_bound_openrouter_catalog`），
   用于 ROI 且独立于 procurement route 或 subscription。
2. 每个已绑定模型使用目录 top-level standard rates；`pricing.overrides` 与 long-context tier 永不选择。
   价格 snapshot 必带 UTC retrieval timestamp、catalog SHA-256、source/canonical model identity。
3. 单次估值固定为 `uncached_input * prompt + cache_read * cache_read + cache_write * cache_write + output * completion`；
   每个 Token class 只计一次，reasoning 不重复计价，所有金额使用 Decimal 并逐 component/partition/overall 守恒。
4. producer-native `usage.cost`、credits/USD estimate 与 Relay legacy `cost_usd` 不进入 WI valuation。
   Harness/provider 原生的 `actual_cash_usd` 只作非权威 `provider_native_cash_usd` 上下文，绝不复写政策
   machine cash、cash basis、cash coverage 或 ROI。OpenRouter management activity 只作 Token/cost 守恒
   旁证，不构成竞争的 WI machine-cash authority。
5. subscription 不按 WI 分配或结算，`wi-report`/ROI 显式输出 `subscription_settlement=excluded_from_wi_report_and_roi`。
   旧 `allocate_subscription_cents` / `settle_managed_cost` 与 `managed_cost_snapshot.v1` 仅为 pre-policy Stats shadow
   compatibility，已 deprecated，不得成为新 WI consumer 的依赖。
6. retrospective Pi window 可在保留逐 request 模型/Token 证据时按同一 flat policy 给 estimate；estimate
   （及 `retrospective_machine_cash_usd`）固定带 `may_include_unrelated_turns=true` 与
   `noncanonical_total=true`，是政策下可见的现金估计，但永远不进入
   canonical total、Token seal、measurement readiness 或 ROI。canonical total unavailable 只能归因于
   Codex/Token 归因不完整，不得写成 provider cash 或 subscription 证据缺失。
7. quota、provider-native cash、policy machine cash 与 attention 是并列事实，不得互相反推；缺 producer 一律
   unknown/unavailable。machine cash 只由 flat policy 计算，provider-native cash（包括 `0`）不参与推导。

`@anchor symbol price_wi_usage_cohort`
`@anchor symbol parse_openrouter_prices`
`@anchor symbol summarize_openrouter_activity`
`@anchor test ComputeCost/tests/test_wi_pricing.py :: WiPricingTests.test_long_context_requests_keep_flat_standard_rates`
`@anchor test ComputeCost/tests/test_wi_pricing.py :: WiPricingTests.test_native_producer_cost_is_rejected_from_wi_valuation`

## 2. 用户故事

### US-001：看清成本现状

作为 PULSE 管理者，我希望按 Agent、consumer path、任务、模型、effort、时间窗口和
outcome 查看统一管理成本，以便知道成本花在哪里。

### US-002：解释成本变化

作为 PULSE 管理者，我希望把成本变化拆解为任务量、模型档位、输入/输出/cache、
retry/escalation、重复执行和无 LLM gate 命中变化，以便知道为什么成本改变。

### US-003：改变成本结构

作为 PULSE 管理者，我希望模块能对 compute profile、runtime route、LLM gate、重试和
调度提出受约束的调整建议，并复用既有 canary / rollback 执行面，以便系统性改变成本。

### US-004：依据结果持续优化

作为 PULSE 管理者，我希望比较调整前后的成本、accepted outcome、人工纠错与高风险错误，
以便晋级、保持或回滚，而不是只按 token 多少优化。

### US-005：一处计算，多处消费

作为 Stats、周巡检和 Duke，我希望消费同一套成本事实与分析结果，以便停止维护三套口径。

## 3. 功能需求

### FR-P0-001：OpenRouter 价格快照

- 从公开 `GET /api/v1/models` 读取指定参考模型。
- 快照至少包含 model id、canonical slug、输入、输出、cache-read 单价、币种和 UTC 抓取时间。
- 缺模型、缺必需价格、负数或非法数值必须失败，不得当成 `0`。
- 历史估值绑定当次价格快照；新价格不得静默改写旧结果。

`@anchor test ComputeCost/tests/test_openrouter.py :: OpenRouterContractTests.test_parse_openrouter_prices_preserves_exact_decimal_rates`
`@anchor test ComputeCost/tests/test_openrouter.py :: OpenRouterContractTests.test_missing_required_price_fails_closed`

### FR-P0-002：OpenRouter 实际用量快照

- ownermac 上的 `/Users/owner/Coevolution/.env` 是 `OPENROUTER_M_KEY` 唯一凭证入口。
- 只读调用 `GET /api/v1/activity`；密钥不得离开 ownermac、不得出现在 argv 输出、日志、
  fixture、Spec 或 snapshot。
- 输出只保留日期、模型、canonical model、provider、请求数、token 与美元用量。
- 当前接口只覆盖最近 `30` 个完整 UTC 日；超出范围必须标为不可用，不能拼成完整月账。

`@anchor symbol load_dotenv_value`
`@anchor symbol fetch_openrouter_activity`
`@anchor test ComputeCost/tests/test_openrouter.py :: OpenRouterContractTests.test_activity_summary_drops_endpoint_identity_and_conserves_totals`
`@anchor manual openrouter-management-live-probe :: 在 ownermac 读取既有 .env 后只读调用 /api/v1/activity；确认 HTTP 200、输出不含 OPENROUTER_M_KEY/key hash/user id，并复算 rows 与 totals`

### FR-P0-003：统一参考成本

每个可计价事件按下式计算：

```text
uncached_input = input_tokens - cache_read_tokens - cache_write_tokens
reference_cost_usd = uncached_input * prompt_price
                   + cache_read_tokens * cache_read_price
                   + cache_write_tokens * cache_write_price
                   + output_tokens * completion_price
```

- `cache_read_tokens + cache_write_tokens > input_tokens`、token 为负、cache 有用量但价格缺失时必须失败。
- WI 只取模型 top-level standard rates；即使 request 超过 272K 也不得选择 override。
- Decimal 精度贯穿计算；不得先转二进制浮点。
- reasoning token 保留为观测字段；参考模型没有独立 reasoning 单价时不重复加价。

`@anchor symbol estimate_reference_cost`
`@anchor test ComputeCost/tests/test_valuation.py :: ValuationTests.test_reference_cost_prices_uncached_cache_and_output_exactly`
`@anchor test ComputeCost/tests/test_valuation.py :: ValuationTests.test_cache_tokens_cannot_exceed_input_tokens`

### FR-P0-004：ChatGPT Pro 月度折算（deprecated compatibility only）

本节只描述旧 `managed_cost_snapshot.v1` Stats shadow，禁止进入 WI report/ROI。新 consumer 不得调用。

- 先按 FR-P0-003 得到每个 Pro 事件的参考成本权重。
- 有权重时用 deterministic largest-remainder 把 `20000` 美分分配到各分区。
- 所有分区之和必须精确等于 `20000` 美分；同余数按稳定 key 升序破平。
- 无正权重时不伪造归因，`unallocated_cents=20000`。
- OpenRouter API 实际消费不进入 Pro 折算。

`@anchor test ComputeCost/tests/test_valuation.py :: ValuationTests.test_subscription_allocation_conserves_20000_cents`
`@anchor test ComputeCost/tests/test_valuation.py :: ValuationTests.test_zero_usage_keeps_subscription_unallocated`
`@anchor test ComputeCost/tests/test_valuation.py :: ValuationTests.test_managed_cost_settlement_applies_pro_adjustment_without_double_counting`

### FR-P1-001：规范化 usage event

事件合同至少包含：稳定 event id、UTC 时间、机器、Agent、consumer path、producer-owned
`task_family/risk_class`、task/work-item/outcome 关联、runtime、provider、model、effort、
输入/输出/cache token、retry/escalation、base/risk-floor/effective route、验证结果与 outcome status，
以及如下 `lineage` 对象：

```text
root_agent: string | unknown
execution_mode: human | automatic | unknown
session_role: main | sub_agent | unknown
root_run_ref: lineage_v1_<sha256> | unknown
parent_run_ref: lineage_v1_<sha256> | unknown
cron_job_ref: opaque producer reference | unknown
scan_ref: opaque producer reference | unknown
```

所有 `*_ref` 都只能由 producer 提供的稳定关联键经 domain-separated 单向 hash 得到；不得输出
原始 session/user/message/turn id，也不得以 cwd、prompt、文件路径或模型名反推 root Agent、执行模式、
cron 或 scan。`usage_event.v1` 新写对象必须带完整 `lineage` 段；读取历史 v1 对象时缺段按字段
`unknown` 归一化，保持 ledger 可读而不伪造覆盖。

缺字段必须显式为 `unknown`；历史 `usage_event.v1` 缺 `task_family/risk_class` 时按 `unknown`
读取并保持兼容，不得把 `task_ref`、model、cwd、route 或文件位置反推为 cohort 字段；也不得用
runtime、provider 或文件位置推断订阅归属。

规范化 `tokens.input` 固定表示“包含 cache 的总输入”。Claude/CC 原始
`input_tokens/cache_read/cache_creation` 是互斥计数，adapter 必须相加；Codex
`input_tokens` 已包含 `cached_input_tokens`，adapter 不得再次相加。reasoning 只保留观测，
不得从 output 再拆出后重复计费。

跨源优先级固定为：raw CC/Codex runtime event 是用量真源，Relay metering 只为相同回合补
Agent/consumer 归因；关联成功的 Relay 行不得再生成第二笔 usage event。Codex 的
`total_token_usage` 是 rollout 内累计计数，同一 rollout 的多 turn 必须在 `task_complete`
处做相邻累计差分，重复 final `token_count` 只保留一次。raw event 缺失时 Relay 才能兜底：
Codex provider 的 Relay 行仍按 session 对 input/output/cache-read/cache-write 四个累计计数器分别
做差分；其中 input 已含 cache，不得二次相加，且必须带 partial quality flag。

`event_id` 由 adapter、machine 与上游稳定 id 做单向 hash，输出禁止保留 user id、session id、
turn id、message id、绝对路径或原始内容。Relay 不提供原生稳定 id 时，adapter 必须先对时间、
租户、会话、模型与原始计数器做单向事实 hash；文件名、分区名与行号只作 provenance，恢复或
rollover 复制不得生成第二笔事件。model catalog 只提供明确的 model→provider/protocol；
ComputeCost policy 再显式映射 `openai-chatgpt→chatgpt_pro`、`openrouter→openrouter_api` 及对应
runtime。无映射即 `unknown`，禁止按模型名字或文件位置猜。

Codex `fork_turns=all` child rollout 的复制前缀不构成 child 用量：在 child 的第一个真实
`turn_context` 前，adapter 只能更新累计 token baseline，禁止产生 usage event。`fork_turns=none`、
depth 1、depth 2 与 resume 都必须有正反 fixture；真正 child turn 的累计差分仍须守恒。

`@anchor symbol UsageEvent`
`@anchor symbol collect_usage_events`
`@anchor test ComputeCost/tests/test_adapters.py :: AdapterContractTests.test_runtime_evidence_wins_and_relay_only_attributes_or_falls_back`
`@anchor test ComputeCost/tests/test_adapters.py :: AdapterContractTests.test_unmatched_codex_relay_rows_are_cumulative_deltas`
`@anchor test ComputeCost/tests/test_adapters.py :: AdapterContractTests.test_relay_recovery_copy_keeps_one_stable_event_identity`
`@anchor test ComputeCost/tests/test_adapters.py :: AdapterContractTests.test_matched_relay_counter_precedes_unmatched_fallback_delta`
`@anchor test ComputeCost/tests/test_adapters.py :: AdapterContractTests.test_unmapped_codex_model_does_not_infer_provider_from_session`

### FR-P1-002：成本驱动解释

同一查询引擎必须支持按 Agent、consumer、任务、模型、effort、retry、escalation、
outcome、root Agent、human/automatic、main/sub-agent、root task/cron job/scan、日/周/月分区，
并输出总量、占比和相对基线增量。每一种分区都必须守恒；
无法归因的成本进入 `unattributed`。

成本变化解释使用固定顺序的四段 bridge evaluator `C(D,R,E,P)`：`D` 是任务量与任务组合，
`R` 是 runtime/model/effort/risk-floor 路由，`E` 是 token/cache/retry/escalation/duplicate
执行量，`P` 是价格快照与 Pro settlement。对 baseline `0` 和 current `1`：

```text
demand_delta    = C(D1,R0,E0,P0) - C(D0,R0,E0,P0)
route_delta     = C(D1,R1,E0,P0) - C(D1,R0,E0,P0)
execution_delta = C(D1,R1,E1,P0) - C(D1,R1,E0,P0)
price_delta     = C(D1,R1,E1,P1) - C(D1,R1,E1,P0)
residual_delta  = observed_delta - sum(the four deltas)
```

固定顺序是合同的一部分；不得让实现自行换序。缺少构造任一 counterfactual 所需的 cohort
字段时，该部分只进入 `residual_delta`，解释状态为 `insufficient_evidence`。`outcome` 不是第五个
成本原因，而是 `managed_cost_usd / accepted_outcome_count` 的质量与效率约束。

`@anchor symbol build_cost_driver_bridge`
`@anchor symbol validate_cost_driver_bridge`
`@anchor test ComputeCost/tests/test_query.py :: CostQueryTests.test_s4_driver_bridge_explains_retry_model_and_outcome_changes`
`@anchor test ComputeCost/tests/test_query.py :: CostQueryTests.test_s4_driver_bridge_fails_closed_without_pro_settlement_evidence`
`@anchor manual cost-driver-conservation :: 对固定事件集分别按 agent/consumer/task/model/outcome 聚合；每个分区加总必须等于同一 overall total，缺关联键的事件只进入 unattributed`

### FR-P1-003：三类消费合同

- Stats：读取版本化只读 snapshot，替代现有 `billing.mtd.*` / `usage.mtd.*` 展示。
- 周巡检：直接请求同一查询引擎的 ISO week 窗口，不再手工过滤 token ledger。
- Duke：读取当前配置、成本驱动、建议动作、证据覆盖、风险约束和 rollback 条件。

替换前必须 shadow 双跑；旧链在真实 Stats UAT、一次完整月界、一次真实周巡检与一次 Duke
资源判断均通过后才可退役。

权威状态只有 `candidate`、`shadow`、`authoritative`：S0-S1 为 candidate，S2-S5 为 shadow，
此时 legacy 仍是用户可见真源；全部替换门通过后才可进入 authoritative。进入 authoritative 后，
旧字段只能由 ComputeCost compatibility projector 生成，旧 writer 必须停写，禁止双真源。

`@anchor manual downstream-shadow-parity :: 新旧 Stats 与周巡检双跑跨过一个完整月界；保存差异解释，并由 Duke 实际消费一次资源建议后才允许关闭 legacy 输出`

### FR-P1-004：幂等 ledger 与迟到事件

- 产品月只由 `occurred_at` 按 `Asia/Shanghai` 判定；文件名、mtime 与 `observed_at` 不得改月。
- `observed_at` 首次写入后保持；相同 event id 重放只计 duplicate，不改变金额或迟到状态。
- 同 event id 的不可变事实不同必须在任何 append 前 fail closed，不允许覆盖旧事件。
- 当 `observed_month > product_month` 时，事件只追加到该产品月的 `corrections` 分区；不得
  重写已存在的主月分区。查询时主分区与 corrections 合并读取。
- ledger 采用进程锁、append、flush + fsync；存储内容只能是脱敏后的 `usage_event.v1`。

`@anchor symbol UsageLedger`
`@anchor test ComputeCost/tests/test_ledger.py :: UsageLedgerTests.test_replay_is_idempotent_and_keeps_first_observation`
`@anchor test ComputeCost/tests/test_ledger.py :: UsageLedgerTests.test_cross_month_late_event_goes_to_corrections`
`@anchor test ComputeCost/tests/test_ledger.py :: UsageLedgerTests.test_cross_month_conflict_is_preflighted_before_any_append`

### FR-P1-005：月度成本快照与 Stats shadow

以下是历史 S2 兼容合同，仅供旧快照/旧测试解释；不再是正式 Stats 的默认数据源、发布条件或回退方案。当前查询型仪表盘不分摊 Pro 月费、不输出旧 billing/shadow 指标，见[现行设计](stats-query-dashboard.md)。

- `managed_cost_snapshot.v1` 必须绑定北京产品月、规范 UTC 生成时间、同一份价格快照、输入
  `usage_event.v1` 集合摘要和内容哈希；不同事件集合即使聚合金额相同，也必须有不同
  `event_set_digest` 与 `snapshot_id`。
- 所有 Decimal 在 JSON 中使用不带科学计数法的 canonical string。输入事件必须全属于目标月，
  `generated_at` 不得早于价格或事件观察时间；重复 event id、保留分区名碰撞、月份越界均 fail closed。
- `source=unknown` 的事件进入 `unpriced_event_count` 与 partial quality，不把未知成本伪装成零；
  `agent=unknown` 进入 `unattributed`，Pro 零权重进入独立 `unallocated` 分区。
- `by_agent` 的事件数、两类 reference cost、Pro allocated/unallocated cents 和 managed cost 必须分别
  与 overall 守恒；所有 Pro cents 合计严格为 `20000`。
- `actual_openrouter_usd` 只有携带脱敏 activity snapshot digest、UTC 日窗口和
  `partial|complete` coverage 才可出现。当前 30 日接口默认只能证明 `partial`；只有 closed 月结且
  UTC 日窗口覆盖完整北京产品月，才能标 `complete`。现金金额和证据仍不进入 managed cost。
- Stats exporter 只有调用方显式传入绝对 `--compute-cost-shadow` 路径，或
  `PULSE_COMPUTE_COST_SHADOW_ENABLED=1` 且 `PULSE_COMPUTE_COST_SHADOW_PATH` 是绝对路径时才读取；
  两种入口都缺席时默认关闭，不得产生任何
  `compute_cost.shadow.*` 指标；启用后缺文件、坏 schema、旧月、closed 历史月、未来时间或
  超过 `45` 分钟的 provisional 快照全部整束 unavailable，不能让主快照失败。
- `managed-snapshot` 只允许从至少一个本地只读 evidence 目录生成当前版本合同，按北京月筛选事件，
  公开价格目录不可读或输出非绝对路径时 fail closed；落盘必须原子替换且最终权限为 `0600`。
- Swift App 只做 typed parse、守恒复核和呈现；仅当两项 shadow metrics 同时有效时显示独立
  “算力成本（Shadow）”区块。legacy billing/usage 始终并存，S2 不替换现有真源。

`@anchor symbol ManagedCostSnapshot`
`@anchor symbol validate_managed_cost_snapshot`
`@anchor test ComputeCost/tests/test_managed_snapshot.py :: ManagedCostSnapshotTests.test_dual_ledgers_and_agent_partitions_conserve`
`@anchor test ComputeCost/tests/test_managed_snapshot.py :: ManagedCostSnapshotTests.test_unknown_source_is_unpriced_partial_and_not_folded_into_zero_evidence`
`@anchor test ComputeCost/tests/test_managed_snapshot.py :: ManagedCostSnapshotTests.test_equal_aggregates_from_different_event_sets_have_distinct_identity`
`@anchor test ComputeCost/tests/test_managed_snapshot.py :: ManagedCostSnapshotTests.test_cli_materializes_current_versioned_snapshot_atomically`
`@anchor symbol build_managed_cost_shadow_metrics`
`@anchor wire build_managed_cost_shadow_metrics :: wired`
`@anchor test Stats/exporter/tests/test_managed_cost_shadow.py :: ManagedCostShadowTests.test_compute_cost_output_crosses_stats_contract_with_exact_strings`
`@anchor test Stats/exporter/tests/test_managed_cost_shadow.py :: ManagedCostShadowTests.test_old_month_closed_or_stale_snapshot_is_withheld`
`@anchor symbol managedCostShadow`
`@anchor test Stats/PulseStatsTests/BillingMetricsTests.swift :: BillingMetricsTests.testManagedCostShadowParsesExactBundleAndKeepsCashOutsideManagedCost`

### FR-P2-001：成本建议

建议状态固定为 `hold`、`promote`、`rollback`、`insufficient_evidence`。每条建议包含目标
consumer、当前/建议 profile、证据窗口、预期参考成本变化、accepted outcome 变化、风险地板、
有效期和 rollback profile，还必须包含 `route_name`、`current_preset`、`target_preset`、
`rollback_preset`、预期唯一 changed route、active route digest 与 plan digest。

价格过期、outcome 缺失、守恒失败或 critical route 低于 frontier 时只能输出
`insufficient_evidence` 或 `rollback`，不得生成可 apply 的 promote。

建议只能引用 runtime manager catalog 中已存在且 transition map 允许的 named preset；没有合法
preset 时输出 `insufficient_evidence(reason=no_approved_preset)`，不得在建议里携带任意模型字符串。
S5 的“单 consumer”定义为 plan diff 中只有一个 route 的 effective profile 改变，且全局 compute
profile 不变；否则不得 apply。`backup_id` 由 apply 成功后写入 apply receipt，不能事前伪造。

`@anchor symbol _materialize_cost_route`
`@anchor manual recommendation-fail-closed :: 删除 price/outcome/route 任一必需证据并生成建议；结果必须是 insufficient_evidence，critical route 的候选 profile 低于 frontier 必须被拒绝`

### FR-P2-002：受控执行与反馈

- 模块不建设第二控制面；执行复用 `pulse-runtime-priority.py` 的 plan/apply/verify/rollback。
- 初始模式为 `recommend_only`；Duke 只审批或拒绝 recommendation，实际生产 apply 仍走既有
  授权边界。本 Spec 不存在 `authorized policy` 或自动审批路径；自动 apply 必须另立 Spec 和生产授权。
- catalog、preset 与 transition map 的 owner 仍是 runtime priority manager；ComputeCost 只有读权。
- S4 必须在同一个 runtime manager 内补结构化 plan（route diff + active digest + plan digest）与
  apply precondition；S5 在任何真实 canary apply 前再补跨观察窗的 active-canary lease。文本 plan
  和进程级文件锁不足以保护 `7` 天 canary，禁止由 ComputeCost 另造锁或 apply 入口。
- apply 前必须核 plan digest 与 active route digest，apply 后保存 stdout 返回的 `backup_id` 并
  立即 verify；rollback 必须显式使用该 `backup_id`，再 verify 恢复到 `rollback_preset`。
- 单 consumer canary 至少观察 `7` 天且包含 `10` 个真实 outcome。
- 任一高风险错误、静默错误或权限越界立即回滚；其余只在 accepted 比例不低于基线、
  人工纠错率不高于基线、每个 accepted outcome 的参考成本下降时晋级。

`@anchor symbol ALLOWED_COST_PRESET_TRANSITIONS`
`@anchor symbol _structured_plan`
`@anchor symbol _require_apply_preconditions`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_cost_preset_apply_requires_fresh_plan_preconditions_before_write`
`@anchor manual compute-cost-control-uat :: 对单一 consumer 保存 baseline、plan、apply、至少 7 天且至少 10 个 outcome、rollback；任一高风险错误立即触发既有局部 rollback`

## 4. 非功能需求

- **安全**：secret 零落盘；外部响应先在 ownermac 脱敏再进入 PULSE 数据面。
- **安全事故恢复**：发现 secret 或身份字段泄漏时，立即停 collector、隔离受影响 snapshot，
  由凭证 owner 吊销/轮换 key；只有 secret scan、脱敏反例与只读 probe 全部通过后才恢复。
- **可审计**：每个金额能追溯到 usage event、price snapshot 或 OpenRouter actual usage。
- **幂等**：相同事件重放不增加总额；相同输入和价格快照产生字节稳定的核心结果。
- **时区**：原始事件保存 UTC；月度产品窗口使用 Asia/Shanghai；OpenRouter activity 的
  `30` 日限制与 UTC 日界显式展示。
- **降级**：外部源不可用时可保留上次文件供审计，但 Stats 在 `45` 分钟后必须整束 withheld 为
  unavailable，不得继续显示为当前值。
- **性能**：单月 `100000` 个事件的本地聚合目标在 `5` 秒内完成；S2 查询引擎实现前需建立基线。

## 5. 排除范围

- 租户商业计费、预付余额和发票系统。
- 将 lifecycle elapsed exposure、feedback count 或 HAU 伪装成人类 labor cash。
- 把 policy machine cash 与服务商扣款账单/订阅结算混为同一 authority；两者可核对守恒但不是同一事实。
- 首个切片自动修改生产 runtime、cron、profile 或订阅。
- 在没有稳定 outcome 关联前宣称实现了“每个有效结果成本优化”。

## 6. 验收总门

完整功能只有在以下条件全部满足后才可称为 implemented：

1. 用量、价格、估值、Pro 折算和各维分区全部守恒。
2. Stats、周巡检、Duke 三个真实消费者均完成 UAT。
3. 单 consumer 控制 canary 满足 `7` 天 / `10` outcome 门槛并可局部回滚。
4. secret 扫描为 `0` 命中，spec-anchor 为 `0` broken。
5. legacy 成本链有明确停写、兼容和恢复路径。
6. `managed_cost_usd`、四段 driver bridge 与 recommendation→preset 映射均能由第二个实现者
   只凭本 Spec 复算出相同结果。


## WI568 Stats integration repair — 2026-09-14

- Already-versioned `receipts` transport preserves source `fact_ref`, `supersedes` and provenance; it is not a raw snapshot to be reidentified. Replayed ancestors and local evidence refinements must not strand later source revisions. Missing predecessors still reject the atomic batch; recorded-at watermarks remain immutable.
- Codex receipt materialization retains proven native task/counter intervals. An absent interval on an already-ingested final quantity may be filled by exact same-assertion evidence; known counters/models/boundaries cannot be silently replaced. Unknown or crossing intervals retain explicit unallocated disposition.
- One explicitly registered Codex source can supply `paths` instead of `path` to reconcile active/archive copies together through the existing adapter. Other source kinds remain single-root. No automatic whole-machine discovery or new ledger.
- Pi/Codex Host model claims require native evidence whose per-model quantities conserve the same settled result. Pi uses native assistant usage, Codex the exact returned thread and its immutable pre-dispatch prefix/new task. Configured or last-result models are not historical evidence; unsupported compaction, ambiguous switches or conflicting boundaries stay unknown.
- Regression and real-replay evidence: `Bigbird/ComputeCost/uat/wi568-stats-cost-chain-20260914.md`. Production source activation and the Stats-bundled ComputeCost release remain separate gates, coordinated with WI300/#525. This repair does not close the full WI568 program or waive S5.


## WI568 actual-source correction (2026-09-15)

- Production native selectors must follow the inspected Relay manifest's named profiles, not a historical replay directory. Pi's declared session-root layout is root/tenant/*.jsonl; tenant HOME and compute-cost artifacts are not recursively interpreted as session logs.
- A malformed/unfinished source file cannot truncate later valid files. Preserve verified prefixes, report the affected files and recover on append without recounting. No source is moved, repaired, deleted or permission-relaxed to achieve a green run.
- The actual Host's immutable dispatch assignment and the registered canonical execution are attribution evidence. A native request additionally needs the identical native-session identity, verified immutable Pi baseline and source prefix; no current Agent/model or directory-name inference is permitted. Missing canonical legacy projections do not erase a valid frozen Host binding. Earlier historical usage without such authority stays unassigned.
- Physical native prefix inventories are coverage facts, never usage or another charge. Host summaries can be reconciled only with the same explicit native session, an exact witnessed request set inside the declared dispatch interval, all four conserved Token classes, and no competing/conflicting assignment. Matching counts or nearby timestamps without the source inventory are insufficient.
- Query/publisher/Stats preserve actual effective usage time and unassigned scale independently of publication freshness. A later record with unknown quantity cannot refresh effective usage time. Explicit source diagnostics remain visible; a current snapshot is not proof of complete collection.
- Final acceptance uses the same current natural WI across actual source, ledger, original query, publisher and Stats, plus replay/incremental and recovery checks. This does not waive WI568/WI779's original whole-program acceptance gates.


### Harness 混合证据读取（WI300，2026-09-15）

注册的 Harness 根必须保留 `evidence/measurements/<wi>/terminal-authority-reconciliation.yaml`
中的 `s1_terminal_authority_reconciliation/v1` 为来源清单材料，不将它当作用量、补齐的历史证据或新的审批。
文件位置与 work_item_id 必须一致；内容仍参与来源摘要。它不应阻断随后合法计量/审核文档。
真正未知 schema 或损坏文档在逐文件处理后保留 `partial` 与分类计数，其他合法文档继续导入；
不得以忽略所有未知 schema、删除历史文件、放宽审批或将未知覆盖补成 complete 来消除提示。
`@anchor test ComputeCost/tests/test_harness_documents.py :: HarnessDocumentTests.test_real_reconciliation_is_in_inventory_not_a_new_approval_or_usage`
`@anchor test ComputeCost/tests/test_harness_documents.py :: HarnessDocumentTests.test_unknown_schema_stays_a_gap_but_does_not_drop_later_valid_facts`

### 分组异常隔离（WI300，2026-10-04）

用量版本冲突、核销冲突和无法归期的诊断必须遵循与查询分组一致的 WI／明确非 WI 分类、Agent、Runtime、模型、节点、入口和期间。空 WI 集合不能解释为所有 WI；无关冲突不改变正常分组的已计价部分。直接引用的执行冲突即使开始于窗外仍影响依赖它的用量；未限定范围的 coverage 仍保留全局缺口。不得通过缩小诊断范围删除原冲突或将部分金额变成完整成本。

### CC 持久累计与采集增长约束（WI300，2026-10-04）

CC 父子任务共用原 WI，但会话累计观察不作为独立单轮消费相加。符合设计中引擎、原生 scope/model、起始前缀及版本条件时，查询保留原生明细并核对未展开差额，四类 Token 守恒；不得臆造差额的子代理归属或精确日分布。相反归属、未知基线、计数回退、缺测与真正终值分叉继续可见。标准金额仍使用同一价格凭据。

已登记来源新增一条记录，不应仅因整份来源指纹变化而为全部未变历史断言追加版本。保留首个来源见证、稳定 source_ref、当前覆盖指纹与所有旧水位；不得删账换速度。运行耗时、实际新增凭据与原消费者金额均需实测，不能以缓存命中或测试通过代替上线效果。

同一原生 Codex 轮次的非最终进展观察不能因历史前驱边缺失而被当成多笔相互矛盾的消费；只有设计规定的同源、同轮、同起点、单调且终值一致条件成立才能合并展示一次，原始版本链和真正相反终值保持可查。

人审来源升级至 `work_item_review/v2` 时，ComputeCost 必须读取并区分显式人类豁免与独立验收，不得将有效新凭据误报为损坏，也不得从豁免生成独立接受或完整覆盖。旧 v1 兼容和全部既存凭据保留。


### WI654 本轮缺失归属修复边界

持久 CC session 的新原始请求须有自己的用途声明；准确的 parent/prompt/配对工具证据可连接同请求子调用，不能按最后声明或父 session 为不同请求填 WI。历史验证者身份须核对自身正式 workspace、不可变候选、接受与失效时间，按 exact usage version 绑定，不恢复当前执行权。已知/明确非 WI/缺证分别报告；缺租户命名或成员有效时间时保留未知，不从 WI owner、路径、poolKey 或投递渠道推导。


### Authenticated Relay product tenant context (v2 opt-in)

`pulse_stats_tenant_registry/v2` keeps the v1 exact scope/time `bindings` and adds closed `relay_users` declarations plus `definition_observed_at`. The real Relay `UserConfig.userId` is the product tenant id; its explicit public name is declared separately. The current configuration proves naming only, never past Agent membership. Each historical usage requires the original private Relay input's captured `source.humanRef`, enrolled Host source and node, event time, and the existing exact native/dispatch actor proof. Pool names, paths, WI owners and delivery destinations cannot name a tenant.

The observer extracts identity/time metadata and hashes the immutable original bytes; it does not interpret, retain or export the input body or credentials. CC sidechains must retain the exact original input and dispatch actor witness already validated by the native route reader. An agreeing Host/native request join may refine a coarse Host interval to its proven native request point using the existing quantity reconciliation; projected quantities are never written. Without that compatible witness, stale usage version, contradictory identity, unproved input or direct Desktop/automation origin stays unknown. Independent quantity and actor conflicts remain visible.

Enroll `tenant_context.v1` and `tenant_relay_input.v1` on all readers before preparing the private declaration/source candidate through `source_registration.tenant_candidate_registry`. The real named-user declaration can be prepared with `relay_tenant_context.relay_user_declaration(config_path, registry, names=explicit_public_names, observed_at=actual_naming_time)`. The opt-in publisher still requires `tenant_identity=True`; reader deployment alone does not activate membership or change the current source registry. Historical repair is append-only, anchored to the exact current usage version; old watermarks and original usage stay intact, repeat collection is zero. Missing authenticated membership is not zero cost.
