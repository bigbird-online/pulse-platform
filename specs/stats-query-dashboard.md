# WI300 — Stats 查询型成本仪表盘

状态：当前呈现与登记来源同步已部署并完成自然发布/重启/恢复验证；2026-09-19用户确认新版本已基本符合预期。本轮可以收尾，不是整个WI300/WI568、历史月账或全平台健康的完成声明。
承载：`wi_20260825_300`，复用 ComputeCost/WI568；本次采用基线与实证见[已证基线与边界](#已证基线与边界)。设计不代替当前运行态，后续排障须重新取证。

## 用户结果与范围

用户在菜单栏和固定弹窗先看当前运行、额度、新增与关闭的 WI 和标准成本，需要对比时再打开完整明细窗。现行设计包含已采用的Oikos/自动任务、统一卡片、四栏预览与八列明细、双行百分比和事项摘要；最新安装与历史修正分别记录在文末，不把旧版本当作当前。
Oikos 资源移入仪表盘的折叠概览，旧事故卡片由[自动任务](stats-automation-card.md)替代；“要你出手”保留原职责，“在飞项库存”已由[在办](stats-presentation.md#在办)替代。仅提供下述同源完整明细窗，不新建独立账本、自动决策或ROI页面；历史月度用量清理仍不在本轮范围。

## 仪表盘与关键状态

全卡片风格和详情层次见[呈现规范](stats-presentation.md)；读数来源、期间和金额语义仍以本文件为准。

五张卡片使用统一的“小状态点＋半粗标题＋右侧摘要”，不再保留旧事故卡片。仪表盘四格为平台运行、新增 WI、PR 数、总成本。一组**今日／本周／本月**控制后三格和下方明细，默认今日；平台运行固定回答当前观测；日期采用 Asia/Shanghai 半开区间，周一为周起点。“要你出手”只在菜单栏与其卡片显示，不进四格。

| 指标 | 现行含义 |
| --- | --- |
| 平台运行 | 新鲜当前运行与资源观测正常时显示正常，副标题“已监测项均正常”；仅当前事故、关键源缺失/过期才显示异常或待更新。与统计期间、WI 账本核验分开，不外推历史。 |
| 新增 WI | 原生 WI 事实中 `created_at` 落在所选期间的 WI 数（`harness.wi.day_created`／`iso_week_created`／`month_created`）。 |
| PR 数 | 所选期间由内核合并并读回的 PR 个数（`pull_request_outputs`），按 GitHub 合并时间归入 Asia/Shanghai 期间；内核记录读全时为确数，部分 WI 读不到时显示“≥已知数”，未提供时显示“—”，不当作 0。同一期间在 GitHub 已合并、但没有任何 WI 内核合并记录的 PR 有 N>0 个时，副标题下加灰字“另 N 个未登记”（`pull_request_anomalies.unregistered_pull_requests`）；它只作提示，不改 PR 数、产出或每 PR 成本，未核验时不显示。 |
| 总成本 | 完整时为原查询 `machine_cash_usd`，否则为同一查询的 `priced_machine_cash_usd`（全部已计价行，含未关联 WI 与 `join_conflict` 扣住分组金额的行）；没有已计价数据显示横线。副标题只写“标准价”，完整性限制在“产出与成本”的统计详情。不是订阅/API 实际扣款，不因表格筛选而改变。 |

两项 WI 计数与快照同源；快照超过45分钟或已跨北京时间日界时显示横线。活跃 Agent 移入统计详情：人数采用发布器 `active_agents`，同一 Agent 在多个租户下都有活动时按一人计，并在详情点名。阶段产出在“产出与成本”卡展开。

WI 账本核验与两条 7 天额度常驻。前者显示“WI 账本已核验／待核验”，只证明 Stats 完整读到与快照同版的原生 WI 账本，不代表所有机器在线或成本全量覆盖，详情展开可查。
**额度收起只显示状态点、名称、已用比例**；CC 行另附近 5 小时窗口已用比例。重置时间、额度读取时间放在展开内容中；额度遵循提供方滚动 7 天窗口，不是自然周，切到“本月”不变成月额度。两条额度都显示当前 7 天窗口内的最近读数：读取时间不晚于当前时刻 60 秒、重置时间未到、读取时间晚于重置时间前 7 天；不满足时菜单与面板显示横线或待更新，展开说明原因。
额度展开同时保留原今日 Token 四分项：新输入、缓存创建、输出、缓存复读，以及用量截至时间。明确标为**“今日已记录用量 · 全部 Runtime”**，取同份 ComputeCost 今日查询，不以额度百分比换算，也不冒称 Codex 专属用量；切换表格期间不改变这里的今日口径；过期仍显示同日最近读数及用量截至时间，跨日显示未更新。
菜单在同一原生模板图像中并排显示双行 badge：CODEX小标签/7 天额度已用百分比，以及WI小标签/“要你出手”当前计数。CODEX使用与面板相同的有效账号读数；WI直接使用当前 snapshot 的 producer alert_tiers.act，App不自行数。没有 snapshot 时 WI 显示横线；若行动来源只有部分可核验，保留与面板相同的可读汇总数并附 ?，不把部分覆盖说成完整来源覆盖。运行异常、待更新与夜间标记独立保留，不取代两个有效数字。额度不是CPU/内存利用率。
存在读取缺口时，“产出与成本”卡在成本表下方显示“N 项成本用量来源未完整读取”，仪表盘不显示：只数当前来源中读取失败或状态未知的项；节点已退役（现为 Vultr）的历史来源，以及已读取、仅执行清单未证明完整（`execution_inventory_not_observed`）的原生来源不计入，后者在数据源明细中标“已读取 · 执行清单未证明完整”；展开点名来源、原因及最新成本可能不完整，历史事实保留；正常在途单列。来源明细缺失不说0来源，不把传输失败说成主机宕机。

## 固定面板与八栏交互

面板固定 **420×580 点**，主弹窗为Agent/WI/产出/金额四栏轻量清单，最多5行并明确
显示数/筛选总数；仅随主面板滚动，没有嵌套表格滚动或浮动表头。查看全部打开唯一
可调大小明细窗，完整八列仍可调宽、筛选、排序；表头是表体滚动之外的同级裁剪区，
仅同步水平位移。两处共享同一已发布查询及选择，打开窗口不取数或重算。

| 顺序 | 栏名 | 显示语义 |
| --- | --- | --- |
| 1 | Agent | 用量行的实际执行者；仅成果行使用已知产出责任者。 |
| 2 | WI | 真实 WI 只显示编号 `wi_…`，名称与简介在行详情；确证非 WI 的 `cron`/`无`、未配置 WI 的调度任务“自动任务：<任务名>”、证据不足的 `未关联`、多 WI 冲突的 `待核验`。行内有持有补充用量时，WI 前加紫色圆点，详情中同色圆点一行说明条数；悬停显示 WI 名称与简介。 |
| 3 | 产出 | 本 WI 的 Outcome 引用数量；同成果跨入口/模型引用不可逐行相加，不当作 Runtime 贡献分配。 |
| 4 | 金额 | 原标准 Token 金额；未计价不补 0，零额真实活动仍显示。 |
| 5 | 入口 | Codex App、飞书、Telegram 等；确证无交互入口显示“无”。 |
| 6 | 节点 | 实际执行节点，不是集中采集节点。 |
| 7 | Runtime | 实际运行时，与模型和入口分开。 |
| 8 | 模型 | 本笔/本组原生模型，不继承当前配置。 |

表头排序和列筛选取交集；金额按 Decimal、产出按数值排序，未知在正反排序中均置后。普通刷新保留筛选、排序、列宽及按稳定行标识展开的详情；切换期间清空筛选和行展开。
纯执行期望不制造活动行。原 `groups` 只含实际用量组合；只有本期成果、没有本期用量的 WI 通过 `outcome_only_groups` 显示，不假造0成本或Token。Outcome 未提供、确证0项、非WI不适用分别处理，缺产出不抹掉已知成本。

## 统计详情与行详情

表格下只保留一个默认折叠的**“统计详情”**；点击总成本也打开同一入口，平台运行打开Oikos，两格 WI 计数不跳转。旧“产出、用量与数据源”和“口径与凭据”不再并列。
展开先显示所选期间、精确标准金额（有需处理原因时为“完整成本未知”并另列已计价部分；只有完整性未证明时显示已计价金额并注明）、需处理与仅完整性未证明的分组数、用量记录数、活跃Agent人数与名单以及真实非零异常。期间用量分项、成果清单、套餐与采购、数据源、技术凭据分别按需展开；移除重复通用解释和逐项零异常陈列，不删除原查询事实。
行展开先给人读：WI 名称、简介（创建时填写的一行简介，未填写则不显示）、编号与状态；金额、用量区间、Agent、入口、模型一行；需处理原因（橙色）；持有补充（紫色圆点，与表格同色）；本期成果。其余——归属方式条数、完整性未证明原因、租户来源、节点/Runtime、Token 分项、精确金额、价表、调用来源、期间与水位、报告与凭据引用——收进默认折叠的“核对凭据”。结果正文未提供时不编造；引用有界时明示已展示数/全部数，其余保留于同水位原查询。
发布物可带 `work_items`：按编号排序的 `{id,title,summary,status}` 列表，仅含表中出现的 WI，来自已登记的内核 WI 导出（`summary` 为创建 WI 时必填的一行简介，≤120 字；未填写的旧 WI 为空）。它只用于显示，不参与归属、金额与校验。
“已计价部分”及完整覆盖限制保留在统计详情和技术凭据中，不放回主金额徽章；正常在途独立说明，不混成读取故障。折叠是显示行为，不是丢弃证据。

## 唯一数据链与快照

ComputeCost 登记采集/版本化账本 → `computecost_collaboration_query/v1` → `stats_dashboard.py` → `pulse_stats_cost_dashboard/v1` → ownermac 本机固定 exporter → App/cache。
Agent 经只读 `pulse_stats` 工具读同一发布物与 App 缓存，不另算口径，接口见[Agent 查询接口](../Stats/README.md#agent-查询接口)。
一次发布冻结同一账本水位，物化今日／本周／本月三个六维联合 `activity` 成本视图。本周是本周一至今日；周复判读的上周一 00:00 至本周一 00:00（Asia/Shanghai）由同一周期另行发布为 `cost-last-week-v1.json`，只含 `last_week` 一个联合视图，同 schema、同有效期规则，独立于主发布物的 512KiB 上限。上周在结周时算一次并存档，此后 `cost-last-week-v1.json` 每轮取存档视图、只换有效期；结周之前按窗口现算。规则见下文“结周与调整分录”。它的失败不撤回主发布物。六个独立身份分组由原查询按需生成，不再例行预计算；旧21视图发布物继续可读。八列表格消费联合分组和Outcome扩展，不从独立分组拼接；产出、金额、记录数的自然粒度不同，不能做多对多连接再相加。
联合视图同时携带 `outcomes`、行内成果引用、有界 `detail`、`outcome_only_groups` 和 `active_agents`。全局Outcome按稳定对象去重，行内只引用，技术信息不会因为默认收起而在发布层被删掉。
原查询保留 `facts_sha256`；发布视图携带 `query/watermark/recorded_at/catalog_observed_at/catalog_sha256/report_sha256` 和摘要，不重复传全事实集。复算固定窗口、水位、价表和计算规则。

| 情况 | 消费规则 |
| --- | --- |
| `machine_cash_usd` | 原完整查询金额，受独立执行/覆盖限制；不是实付。 |
| `partial_machine_cash_usd` | 只称已计价部分，不拼成被上游扣住的完整成本。 |
| `priced_machine_cash_usd` | 同一查询全部已计价行之和，含 `join_conflict` 扣住金额的行；仅作仪表盘“总成本”的回退读数，不称完整成本。 |
| `anomalies`／`proof_gaps` | 不重不漏地划分 `reasons`。前者需处理，金额标橙；后者只说明完整性未证明，收在行详情与技术凭据，不标异常。旧发布物缺此二字段时，全部原因按需处理显示。 |
| `usage.conflict_reasons` | 摘要与每个分组冲突用量的原因计数，解释 `join_conflict` 为何扣住金额；旧发布物缺此字段时不显示原因。 |
| 两者均 null | 显示未提供/未计价及原因，不补 0。 |
| 有效旧快照缺联合视图或Outcome扩展 | 保留仍有效的成本；缺明细或成果明确等待/未提供，不造行。 |
| 可选Outcome扩展损坏 | 不让Swift丢弃独立有效成本及下方三个卡片；成果验证仍拒绝无效扩展。 |
| 整份快照过期或已跨北京时间日界 | 照常显示最近一次有效发布物，顶部标注其原时间与过期、刷新中或失败原因（[保留上次数据](stats-presentation.md#保留上次数据)）；不伪造新时间。 |
| 整份快照损坏、来自未来或不兼容 | 该区显示真实不可用原因，不破坏其他区域；已显示的有效数据不被它替换。 |

独立查看模式可将现存成本发布物通过 `--cost-dashboard-only` 导出为正常 snapshot envelope，使用相同验证与用量投影。
该模式不运行 live collector，不修改查询、金额、时间、水位或摘要，不产生平台健康和 WI 权威证明；没有采集的能力保持 unknown。
Mac 的显式本地入口只消费该原始发布物，与生产传输/缓存隔离；本地重新读取不延长原成本有效期。关闭唯一的本地快照窗口即终止该查看进程，不能留下没有可见操作入口的后台实例。

## 结周与调整分录

`refresh.json` 的 `incremental.close` 为 `on`（需 `views` 为 `shadow` 或 `on`，经 `deploy.py incremental-mode --close on` 设置），且读取端经 `--probe-cost-adjustments` 声明支持时，发布器结周并发布调整分录；否则发布物与此前相同。

- **周**：周一 00:00 至下周一 00:00（Asia/Shanghai）。用量按起始时刻归周，带计量点的按计量点所在日拆到各周；跨周界的区间和没有起点的累计计数器没有归属周，不结周、不产生分录。
- **结周时点**：周一 00:00 之后，第一轮满足齐全条件的发布结上一周：所有未暂停的 peer 都已读，且其采集开始于周一 00:00 之后（推送型 peer 以回执到达时刻减 15 分钟计；本轮拉取的共享账本 peer 在发布开始前已确认完成的，按本轮结果计，仍在采集的记为未读），发布器没有待读观察包。到周一 06:00 仍不齐，其后第一轮照现有数据结周，缺口记在 `closed_week.gaps`。
- **结周**：用当时的水位算上一周视图（加上该周内记账的分录，包括结周这一轮新发现的），连同该视图所点名 WI 的展示数据（读取端据此核对父子 WI 合计）写成 `~/.local/state/pulse/stats/closed-weeks/cost-week-<周一>.json`，一次写定；此后该周只从存档发布，结周工作本轮没有运行（读取端探测失败、现状库水位不符）也照样取存档，存档读不出时保留已发布的上周文件直到过期，从不现算覆盖；把所有已结周的用量现状记为已记账状态；未结分录定稿后移入已定稿分录（保留到上一个自然月之初，长于一周的窗口据此继续计入其中已入账的分录），再清空。已记账金额只在该条此前已计入、数量与模型未变时沿用，其余取结周时的现价，即本轮分录入账的金额。状态存于 `<账本根>/derived/period-close.sqlite`，不随现状库重建清空。
- **调整分录**：已结周的一条用量（带计量点的按计量日各算一条），现状与已记账状态不同即产生分录，每轮现算：分组键变、数量与模型未变为 `reattributed`（旧键负、新键正，金额相同）；数量或模型变为 `quantity_revised`（旧键负旧额、新键正按现价）；结周后才出现为 `late_usage`；不再计入为 `withdrawn`。只改价表不产生分录。记账日为分录第一次出现那一轮的日期。
- **视图纳入**：记账日在视图窗口内、原用量起始日（带计量点的为该条的计量日）不在窗口内的分录计入该视图。周一结周之前，各轮发现的分录记在上一周最后一天，与结周存档一致：现算的上周视图计入它们，本周视图不计。今日、本周、本月与上周的联合视图带 `adjustments`、`total`、`adjustment_only_groups`，行带 `adjustments`、`total`；原有字段仍是窗口内自身用量。上周视图另带 `closed_week`（结周时刻、水位、缺口；未结时为 `null`）。
- **守恒**：每条已结周用量，已记账状态加未结分录等于现状；`total` 等于自身用量加 `adjustments`，exporter 校验。
- **启用**：第一轮把当前周之前的所有周记为已结，已记账状态取启用时的现状，没有存档，不产生分录。
`@anchor test ComputeCost/tests/test_period_close.py :: PeriodCloseTests.test_each_kind_of_difference_and_the_conservation`
`@anchor test ComputeCost/tests/test_period_close.py :: ViewAdjustmentTests.test_a_month_keeps_the_entries_booked_in_it_after_their_week_closes`
`@anchor test ComputeCost/tests/test_period_close.py :: ViewAdjustmentTests.test_the_closed_week_publication_keeps_the_work_items_its_families_need`
`@anchor test ComputeCost/tests/test_period_close.py :: ViewAdjustmentTests.test_a_closed_week_stays_published_from_its_archive_when_a_reader_probe_fails`

## Agent 日明细查询（agent_day）

`pulse_stats` 的 `agent_day` 给出一个 Agent 在 UTC+8 自然日（`[00:00, 次日 00:00)`）的提交、PR 与 WI 事件明细，供写日志（memory-sleep-field 的 day_registry）调用。实现：`Stats/exporter/pulse_stats/agent_day.py`。

输入：`agent`（必填，须在 CollaborateWithAI 所读提交的 `ssot/registry.yaml` 中；登记表读不到时整个查询 `registry_unavailable`）、`date`（必填）、`end_date`（可选，含当天，最多 120 天）、`ws`（可选，相对 Coevolution 根的路径，如 `Bigbird/PULSE/pulse-space/`；给了只看这一个目录）。路径以 `Bigbird/PULSE/`、`Bigbird/Oikos/` 开头时落在对应仓库，其余落在 CollaborateWithAI；正本工作区或 `ws` 在所读提交里不存在时 `commits` 标 `incomplete`（`ws_path_absent`），不当作空工作区。

不给 `ws` 时，按 CollaborateWithAI `Doc/amanda-space/skills/agent-workspace-builder/references/workspace-recipe.md#一个-agent多个执行面` 的「一个 Agent、多个执行面」读该 Agent 的全部目录：登记表的 `workspace`（正本），加上其 `surface_registry`（`repository` 与 `path`，如 Pulse 的 PULSE `config/scl-surfaces.yaml`）在所读提交及其全部历史版本里点过名的每个执行面目录，路径相对该登记文件所在仓库的根；PULSE 的 `config/scl-surfaces.yaml` 同时读其伴随登记 `config/pulse-workspace-checkouts.yaml`。已退役的执行面照算（历史日期的提交仍归它），其目录在所读提交里不存在不算缺口。结果的 `workspaces` 列出实际读的每个目录：`role`（`canonical`／`surface`／`argument`）、`sources`（点名它的登记文件，`--ws` 时为 `argument`）、`surface_ids` 与 `registered_now`（当前版本是否仍登记）；`ws` 只给正本或 `--ws` 那一个。登记读不全时 `commits` 标 `incomplete`，原因点名为 `surfaces:<原因>`：登记文件或某一历史版本读不到（`registry_unreadable`、`registry_version_unreadable`）、`agent_id` 不是该 Agent（`registry_agent_mismatch`）、条目无效（`registry_entry_invalid`、`invalid_workspace`）、某执行面在任何版本都没有目录（`surface_without_workspace:<登记>:<surface_id>`），或正本旁有 `*-<正本目录名>` 的目录而没有登记点名（`unregistered_sibling:<目录>`；同级目录读不到为 `siblings_unreadable`）。登记表没有 `surface_registry` 的 Agent 只读正本，仍做同目录检查。

数据源全部本地只读，查询不 fetch、不调 GitHub、不调内核命令行：

| 来源 | 内容 | 覆盖到 |
| --- | --- | --- |
| `git.<仓库>` | 账户主目录下 `Coevolution`、`Coevolution/Bigbird/PULSE`、`Coevolution/Bigbird/Oikos` 三个检出：`refs/remotes/origin/main` 最新 reflog 记录所指的提交；四端同步凭据（Git 目录下 `four-end-sync/origin-main.json`）指向该提交或当前提交且更新时改用凭据的提交；本次查询的全部读取都用这一个提交（`tip`），之后 ref 再动只记 `moved_since_read` | reflog 记录或凭据中拉取开始的时刻，`sync_evidence` 为 `reflog` 或 `fetch_stamp`。`FETCH_HEAD` 不算证据（取别的 remote 或 ref 也会改写它）；无变化的 fetch 不留 reflog，由凭据补上；两者都是下界。都没有时同步时刻未知（`no_sync_evidence`） |
| `ledger` | PULSE `origin/main` 上的内核账本：`.harness/core/journals`（WI 事件）、`.harness/core/pull-requests`（PR 认领）、`.harness/events/*.events.yaml`（基线导入的 production_history）。决策用内核自己的 `production_segments.load_entry` 解码与校验，内联 `record` 与大决策的分块 `entry` 编码都读；代码取已安装的 `scl-core/active/harness.pyz`（按 ComputeCost 同样的封装校验），`--kernel` 可指向 `agent-harness` 源码目录。载不进或校验失败、`.harness/core/format.json`（schema 须为 `harness.production-ledger/v2`）、`core/journals`、`core/work-items`、`core/pull-requests`、`.harness/work-items`、`.harness/events` 任一缺失时该来源 `unavailable`，不当空账本；`wi_facts` 中账本读取前已创建、窗口起点后仍有更新的 WI，有原生修订的须有日志、其余须有导入记录，缺了点名为 `ledger:work_item_missing:<wi>`（该 Agent 负责的标 `unavailable`，其余 `incomplete`）。读到的每个日志须从修订 1 连续到 `core/work-items/<wi>.json` 记的头修订，缺号或头文件读不到点名为 `ledger:journal_gap:<wi>:missing_revisions:<区间>`／`head_unreadable` | 同 `git.PULSE` |
| `wi_facts` | Stats 的 `wi-facts-v1.json`：WI 负责人与标题；只用来缩小要读的日志范围，其中没有的 WI 照读，负责人取日志首条 `WorkItemCreated`；自身 `coverage` 非 `complete` 时 `wi_events` 标 `incomplete` | 其 `generated_at` |
| `github.pr_cache` | Stats 的 `pr-bodies-v1.json`（私有；`--pr-bodies` 可指定）：刷新时经 pulse-gh 读的三仓库 PR 标题、正文、开启与关闭时刻，采集见 [ComputeCost 设计](pulse-compute-cost-design.md) 的 `pull_request_bodies.py`。读不到、非私有或 schema 不符时为 `unavailable`，`pull_requests` 标 `incomplete`（`github:pr_cache_unavailable:<原因>`） | 每个仓库从 `covered_since` 到 `observed_at`；窗口不在其中时该仓库点名 `github.<仓库>:pr_cache_not_covering_window` |

规则：

- 提交：各目录所在仓库 `origin/main` 上触及任一目录的非合并提交，按提交者时间归日，同一提交只列一次，`workspaces` 给它触及的目录；根提交照常列出（`root=true`）。主题以 `SCL:`、`audit(SCL)`、`chore(harness)`、`chore(broker)` 开头的是账本提交，单列 `ledger_commits`，不进 `commits`，也不参与 WI 点名。每个提交带 `landed_by`／`landed_at`（first-parent 主线上把它带进来的提交）与所属 PR（合并提交 `Merge pull request #N` 或压缩提交标题 `(#N)`）。
- PR：三仓中（a）内核登记在该 Agent 名下 WI 或由它提交的 PR，（b）当天合入且引入的提交触及该 Agent 任一目录的 PR，（c）分支名以 `<agent>/` 开头的 PR；且当天有事件。事件取内核登记（每轮 `registered`）、`withdrawn`、`merge_confirmed_by_human`（均为内核记录时刻）、合入（`merged`，取同步主线上合并提交的提交者时刻，即 GitHub 合并时刻），以及 PR 缓存里的开启（`opened`）与未合并关闭（`closed`），所以当天开启或关闭未合并的 PR 也会入选。标题取合并提交说明或压缩提交标题，都没有时取缓存（`title_source=github_pr_cache`）；压缩合并没有分支名时取缓存的 `head_ref`。`kernel_merged` 按 PR 查，不受查询窗口限制。认领按内核 `resolve_claim` 校验摘要，按 `check_pull_request` 的规则定归属：每个 WI 只在自身修订内取最新的登记／撤回，最新为登记的那个 WI 持有该 PR，修订号从不跨 WI 比较；全部撤回时归最后撤回（按时刻）的 WI，`kernel_merged=false`，除非撤回前已有合并读回。找到持有的 WI 后，从它最后一次登记往后读日志，读到内核合并读回为 `true`（`kernel_merge_observed_at` 给读回时刻，读回晚多久都算）；没有任何 WI 认领过为 `false`；已认领但同步的账本里没有合并读回为 `null`。合并读回本身作为 `PullRequestMerged` WI 事件，按内核记录时刻归日。入选 PR 在缓存里有记录时带 `body`（GitHub 上为空则 `null`，最长 8000 字，`body_truncated` 标截断）、`body_source=github_pr_cache`、`state`（`open`／`closed`／`merged`）、`opened_at`、`closed_at` 与 `github_observed_at`；缓存里没有时这些为 `null`、`body_source=not_collected`，`pull_requests` 段标 `incomplete` 并点名 `github:pr_body_not_collected:<仓库#号>`。
- WI 事件：该 Agent 负责（owner）、当天行动过（actor）、被发合同（recipient）或被当天提交／PR 点名（named）的 WI。owner 与 named 的 WI 列当天全部业务事件；只因 actor／recipient 入选的，只列该 Agent 自己的事件和发给它的合同。去掉 DiskUsageObserved、Effect*、访问授权、工作区分配、计量等噪声，按 WI 计数在 `noise_excluded`。基线导入事件 `source=production_history`；原生事件带 `revision`、`actor`、`human_input`（原始输入来自人类）与正文摘要（单项最多 2000 字）。内核合并读回在 WI 事件里记为 `PullRequestMerged`。
- 日期段（`end_date` 晚于 `date`）只返回 `days`：每天一行 `commits`、`ledger_commits`、`pull_requests`、`pull_requests_registered`、`pull_requests_merged`、`pull_requests_withdrawn`、`wi_events`、`work_items`、`activity` 与 `sources_cover_day`（三个检出、账本、`wi_facts` 与 PR 缓存都同步到该日结束，PR 缓存的 `covered_since` 不晚于该日开始，当天入选的 PR 都有正文，且没有读不到的来源）。每天的计数与该天单日查询相同：点名按天生效，某天的提交点名一个 WI，只带出这个 WI 当天的事件。

完整性：结果带 `generated_at`、`complete` 和 `completeness`。`completeness.sections` 对 `commits`、`pull_requests`、`wi_events` 各给 `complete`／`incomplete`／`unavailable` 与原因；来源同步时刻早于窗口结束为 `incomplete`，检出或文件读不到为 `unavailable`，都不按 0 计。`sources_synced_through` 是三个检出、账本、`wi_facts` 与 PR 缓存中最早的同步时刻。查询外壳 `pulse_stats_query/v1` 的 `sources` 列出各来源的 `generated_at`，段不完整时 `status=partial` 并在 `errors` 点名该段。

性能：单日在 ownermac 约 2–3 秒（Pulse 读 6 个目录、PR 缓存约 4.5 MB），81 天日期段约 3 秒。PR 正文只读缓存，查询时不逐个调 GitHub。日志按修订二分定位窗口，只读窗口内条目；`wi_facts` 在窗口结束后生成时，未在窗口内更新的 WI 不读。

## Outcome来源与归属约束

ComputeCost 的现行[Outcome设计](pulse-compute-cost-design.md)从新内核 `stats-facts` 的 WI 完成记录读取成果：发布器每轮写入的 `wi-facts-v1.json` 由 `Stats/deploy.py enroll-core-outcomes` 登记为唯一 `harness_core` 来源，登记前须选定含 `kernel_outcomes.py` 的读取端。旧 Harness 的规范 `evidence/outcomes/wi_YYYYMMDD_NNN.md` 来源已退役，已入账的成果保留；不借此扫描未登记目录或回退旧 CWA。
Outcome使用原状态、发生/接受时间、稳定身份及修订链。仅重审、文件格式变化或跨运行组合引用不增加成果；无来源完整性证明时空集合不是确认零。旧评审的 unknown delivery 占位可以被同WI正式Outcome追加取代，旧approval不转给新候选；已知结果冲突仍需明确修订关系。
Agent、WI、入口独立取证，规则见[归属设计](stats-attribution-identity.md)。Pi和Codex共用已登记启动工作区的精确身份匹配，每轮重读登记与工作树关系，不能把Actor固化在解析缓存或由工作区名字推WI。独立非WI诊断需正式控制面与精确原生记录支持，不删除已发生消费。

## 归属、零额与正常在途

详细证据链由[归属设计](stats-attribution-identity.md)规定。`cron`/`none` 不伪造 WI；内部入口 `automation` 只在显示时变成 `无`，未知入口与其严格区分。
四类 Token 均明确为 0，标准 Token 金额就是 0，不需查模型价格；任一数量未知不能补 0，非零且无价继续未计价。已计价正金额小于一美分显示 `<$0.01`，精确值在详情，不因显示舍入冒称免费。零额行仍计活动，不能借计价填全身份或推断实际扣款。
Host 的期望/实收、partial 与完整成本限制保留。缺用量执行均为 accepted/running、起点已知且没有相关捕获缺口时，标 `host_usage_pending`，显示“执行尚未结束，完整用量待回报”，单独统计 `pending_host_sources`，不当作读取故障。
终态缺用量、unknown、无时间、相关捕获缺口、坏文件或断线仍异常。早于待回报调用的已定期历史 gap 不能归罪于未来调用，但其原凭据及对完整成本的影响不删除、不核销。不得要求所有 Agent 停工才允许表格验收。

## 价表、采购与额度

默认 WI、query、发布器与 refresh 共用 `catalog_source.py`：显式快照 > registry 快照 > 正式安装对应的已保存托管价表。任意 registry/裸 ledger 不借用本机其他数据集价格，显式缺失/损坏不得回退。refresh 只更新托管快照，并向派生登记传递实际选择，不覆盖调用者的明确价表。
协作成本是实际模型 flat OpenRouter 标准 Token 估值；订阅费用、充值和 OR 实付独立，不加到标准金额，不通过月费分摊制造 WI 成本。
采购事实以 `ComputeCost/config/procurement-facts.json` 和已有记录为准。用户于 2026-09-13 确认 ChatGPT Pro Max 为 USD 200/月、每月 14 日账单日；当时 Claude Code 已停订。2026-10-04 用户明确确认 Claude Code 已恢复订阅，当前状态为 `active_user_confirmed`，月费与账单日仍未知，不补零、不沿用猜测值。`confirmed_on` 记录确认日期，不推定恢复生效日；这些是带日期的用户确认，不是实时订阅检查或扣款回执。OR 仅汇总实际返回的 UTC 日记录，不补缺日、不猜实时性，无同账期完整实付证据不展示采购合计。
授权的 OR 管理凭据留在 ownermac，不出现在参数、日志或产物中。Codex 额度通过已认证 App Server 的 `account/read`、`account/rateLimits/read` 读取，前后账户一致，只发布匿名摘要及原窗口；不创建模型回合查额度，不给过期记录换时间。成本失败不伪造额度；CC 额度只消费 Relay 从真实回合保存的读数，不为查额度主动发起模型回合。

## 刷新性能与发布体积收敛（2026-09-19）

真实生产在 f696ec0912a7112cef691a5beaf87cda5a34049b 上暴露两个相邻根因：成本发布物增长到 536,527 bytes，越过既有512KiB合同后整份降级为 query_payload_too_large；同时132,472条事实的账本只读约14秒，水位变化后21个视图的冷查询约74秒，且两个peer原本串行采集。修复目标不是提高上限或timeout，而是删除重复工作和重复载荷。

现行UI只消费每个期间的六维 activity 作为产出与成本主事实；本地筛选/排序不依赖六个边际分组。新发布因此只物化 **今日／本周／本月三个activity视图**，旧21视图发布物继续可读。今日Token、身份缺口和验收均改读同一activity summary；需要某个临时边际分析时直接运行ComputeCost原查询，不为无人消费的边际视图每30分钟预计算。

发布仍保持 **512KiB** 上限。行级技术引用只内嵌少量样本并同时保留 fact_ref_count / attribution_ref_count 全数和报告摘要；当前用量引用、归属引用与租户登记引用每行各1项（2026-10-25 月视图 120 行时越过 512KiB，由5/6项收紧），用户可见Outcome、金额、Token、时间、归属维度及总引用数不因压缩消失。不得靠扩大成本发布物或1MiB总快照上限处理增长；读取端在内存中展开的上限（4MiB）只约束解压后的处理量，不经任何传输，不属于扩大发布物或总快照。

原始 JSON 超过发布上限时，快照可以在 `row_contexts` 保存各活动视图中完全相同的行字段。行用整数索引指向该字段的对应对象。读取端在校验快照摘要前还原完整的行。未压缩发布仍可读，摘要和展开后的业务字段使用同一逻辑内容。发布器在写入前用当前 exporter 验证读取能力，有配置的远端接收器也须返回相同能力。检查未通过时保留上一份发布。

行字段共享后仍超过 512KiB 时，可发布 gzip 压缩的完整快照。读取端在内存中最多展开 4MiB；传输仍受发布物 512KiB 与快照 1MiB 约束。读取端只接受单个 gzip 流，
并核对外层版本、时刻、状态和原摘要。发布器验证选中 exporter 和已配置接收器的独立 gzip 探针，在写入前
解码回读并核对原内容。接收端保存已验证的原始编码，保持跨端发布摘要一致。

exporter 在内存中展开并按上述规则核对完整摘要；展开形态放进 App 快照会超过 1MiB 时，`cost_dashboard` 改放同一内容的 gzip 形态（外层版本、时刻、状态、原摘要，`compressed_dashboard` 及其 gzip 字节的 SHA256 `compressed_dashboard_sha256`），用量指标仍取自展开后的看板；该 gzip 形态只受快照 1MiB 约束，可以大于发布物 512KiB（行字段共享能跨视图去重，gzip 不能）。App 先核对该 SHA256，再按单个 gzip 流在内存中最多展开 4MiB，并要求展开内容重述外层元数据；任一项不符只丢弃成本看板，不影响快照其余部分。展开形态的快照继续可读。gzip 形态仍超过 1MiB 时才降级为 `query_payload_too_large`。部署先由 Stats 部署装入能读 gzip 形态的 App 与 exporter，再原生部署采用 4MiB 展开上限的发布器。

健康增量传输以已验证私有前缀的长度+SHA256为信任条件：发送端在一次序列化中验证前缀并只压缩新增尾部；接收端重新哈希已有前缀，但不再次逐条JSON解析此前已经完整验证的Receipt，只校验新增帧和最终全档摘要。前缀不匹配、缓存丢失、强制重算或格式异常退回完整验证。
接收后的 receipt-bundle 解析 cursor 不是独立事实源：只有对应 UsageLedger.ingest_receipts() 已原子成功，才允许把“此前 N 字节可省略”写入持久 cache，并记录当时已提交 ledger watermark。进程在 ingest 前失败、ingest 拒绝、cache cursor 高于当前 ledger watermark，或旧 v1 cursor 均必须完整重验该 bundle；允许“ledger 已提交而 cache 未提交”的安全重复解析，不允许“cache 已提交而 ledger 未提交”的事实丢失。此规则用于自动恢复，不能靠删除生产账本或延长过期时间止血。

同一refresh的peer读取互不依赖，按配置并发执行后按原顺序写派生registry；任何单peer失败仍独立降级，不因另一个成功而补绿。UsageLedger在**同一进程**内只复用经过文件元数据核对的已验证transaction解析；目录或transaction元数据变化立即重读。跨进程的复用只经事实索引与现状库，见 ComputeCost 设计；它们绑定账本前缀、可重建，不是第二账本。该复用只省重复解析，不改变fact identity、revision检查、fsync或旧watermark。
前台 App Refresh 只在 ownermac 本机以当前账户运行只读 snapshot exporter，不触发成本采集。Oikos资源读取（本机文件）与自动任务读取（本机及 TCloud peer，不受暂停采集影响）是两个独立的只读来源，须与本地LiveCollector同时启动并彼此并行；失败各自降级，不改变来源含义。生产端到端验收同时记录 App 固定 exporter 的真实耗时，不能只优化后台30分钟发布链。
性能候选的真实三节点回放仍生成完整周诊断，但**不断变化的周业务是否恰好零未知**不是性能算法等价性的替代指标：性能门要求原查询语义不变、来源读取无新增失败、增量前缀/旧水位不改写、生产未修改，并把当时的周缺口原样保存在证据中。归属类发布若声明“当周无漏”仍使用严格周门；不能把诊断模式当成归属验收通过。生产部署不得把已有未知改成0或隐藏。

完成门：以当前真实账本副本证明精简发布低于512KiB且与原activity三个窗口的金额、Token、event_count、Outcome和联合行逐项一致；热增量peer不重验历史JSON；同一refresh不重复读取未变ledger transaction；生产两次自然发布和App重启后不再出现 query_payload_too_large，并记录采集、联合发布和端到端耗时。提高timeout/size limit不计作通过。

## 解析与计算复用、增量传输

用户确认“计算过的数据默认不重复计算”。沿用既有解析、查询和计价器，不引入另一账本或后台服务；细节见[ComputeCost 设计](pulse-compute-cost-design.md)。
原生内容及解释依赖未变时复用解析；Pi/Host/CC 按文件、Codex 按父子/归档选择域。仍检查文件/内容摘要，SCL/Host 关系每轮重读。追加文件需重解析对应文件/域，不声称通用字节级 tail 已实现。
同一冻结事实集复用准备；视图跨发布复用须事实摘要、水位、入账时间（增量引擎下改为窗口内各会话包现状、公共包与全局贡献）、窗口/分组/过滤、价表及引擎均相同；增量引擎下窗口外的新事实不使视图失效。补归属更新分组不重计旧金额；新增数量新增计价，改量/改模型/改价/代码或日周月边界使对应结果失效。新水位可能重建视图，不能把每次刷新都承诺为零查询。
私有 JSON 缓存可删除、大小有界、摘要校验及原子替换；不安全、损坏或丢失回原计算。原账本/来源仍验证，坏来源不能被缓存涂绿。受管 `refresh --force-recompute` 绕过解析、报表和金额缓存；App 的普通 Refresh 仍只读发布物。现状库的整体重建用 `pulse-compute-cost resolved rebuild`，只在发布器运行；`--force-recompute` 不触发它。
跨节点使用原 imports JSONL/last-good 的长度及 SHA256 证明前缀，只传新增后缀，接收者校验前缀/后缀/最终摘要及全部凭据后原子替换。断线保留原账本、报告缺口；恢复可续传，前缀不符则完整验证，不增加第二游标权威。
当前原refresh调用 `export-framed-gzip`，协议为 `pulse_stats_receipt_frames/v2`。每帧展开最多4MiB、一次线传输最多64MiB，完整展开档按真实磁盘容量暂存并保留512MiB余量；累计历史不再受旧64MiB整档门限制。仍校验全部Receipt、前缀与完整摘要，所有帧成功后才替换，失败暂存删除；旧导出操作仅保留其原兼容边界，不是现行恢复路径。
导出包含采集与传输，预算360秒；其他SSH命令180秒，成本发布物最多512KiB。这些是边界而非性能目标，不以提高上限或延长超时宣告修复；详见[传输设计](pulse-compute-cost-design.md#传输安装与恢复)。
传输恢复沿原收集器将旧transport失败标为excluded、覆盖交还原生来源。同步成功、零读取错误、账本/跨端版本一致、完整执行库存及业务结果是不同事实，不相互替代。

## 安装、调度与恢复

正式入口为[Stats macOS CI](../.github/workflows/stats-macos-ci.yml)：绑定已审 main SHA、已安装前像和 App Build。已审 SHA 可落后发起时 main tip，但只允许中间提交仅触及 `.harness/`（生产账本），部署已审 SHA 的树；等待当前发布周期与部署后读回各最多40分钟。`Stats/deploy.py` 在 ownermac 安装不可变读取端，并在每个 peer 安装其导出与任务快照所用的同版读取包，再在未暂停的 peer 采用来源；按决定暂停的 peer（见[第 7 条](pulse-compute-cost-design.md#传输安装与恢复)）照常分发、预检、切换读取包（自动任务视图仍读它的任务状态），但不准备、不采用来源，`refresh.json` 里它的采集脚本路径保持其上次采集所用的版本；不通过手工覆盖发布包或本机旁路交付。
当前采集路径从实际运行环境/登记读取；已迁入 PULSE 的 kernel、人类输入凭据和 Host 来源不得退回旧 CWA。历史过渡根的实际保留情况另列，不能把文档变更当完成数据迁移。
来源准备按 `run-attempt` 保存 before/after/manifest；prepare/select/restore 使用同一标识。新尝试重新核验，旧计划不覆盖；同尝试并发变化仍拒绝。所有 required readers 支持后才启用新凭据，保护账本、原价表及回退能力。
先安装/激活来源，再启动 `io.coevolution.pulse.stats.cost-refresh`。
**两种模式共用（Oracle 发布器）**：oracle-node-1 以 systemd 用户定时器 `pulse-stats-refresh.timer`（120 秒节拍）运行所选读取包 `stats/current` 的 `refresh`；单元限 MemoryHigh 7G、MemoryMax 9G、OOMScoreAdjust 500、CPUWeight 50，内存紧张时先受限的是 Stats 而不是 Relay。oracle-node-1 须有持久 swap（`/swapfile` 8G，写入 `/etc/fstab`）。Oracle 担任发布器期间，完整刷新只在 Oracle 跑，ownermac 只跑 `collect-local`（旧观测链路下现为 observe 模式，见 [ComputeCost 设计](pulse-compute-cost-design.md#传输安装与恢复)第 12 条；meter 模式下执行 `meter_local_round`），正常采集单轮峰值 RSS 不超过 3.25 GiB（这是验收阈值，不是运行时硬限制）。会重写 Oracle 发布器 `refresh.json` 的入口（`install-oracle-publisher`、`observe-mode`、`incremental-mode`）都只适用旧观测链路，见下文「meter 模式回退」。
**现行（`refresh.json` 的 `meter_mode` 为 `pulse_meter`）**：成本采集与发布走字节游标 meter（`ComputeCost/pulse_meter`，WI413），不调用账本采集、观察推送与 spool 读取；`collect_local_mode` 不选择成本采集路径，但 `collect-local` 后续周期仍校验其取值（须为 full、observe 或 incremental），`switch-publisher --publisher ownermac` 仍要求它为 `full`，切回后保留 `meter_mode`。采集：ownermac 每次 `collect-local` 先执行 `meter_local_round`，增量读本机原始记录并上送发布器，不进入 observe 流程；Oracle 的 `refresh` 每轮先执行 `meter_publisher_round`，读自己的记录并入汇总库（`central.sqlite`）。发布：汇总库水位前移即发布；水位未动时，发布物生成不足 15 分钟不重发，超过则重发（`METER_REPUBLISH_SECONDS`）；额度、WI 事实、PR 审计与价表等非成本任务另按 30 分钟调度（`METER_DUTIES_SECONDS`，`Scripts/pulse-stats-cost-refresh.py`）。有效期：meter 发布物最长 45 分钟，且不晚于北京时间当日结束（`ComputeCost/pulse_meter/publish.py` 的 `VALIDITY`）；下文 75 分钟与到期前启动规则属旧观测链路。节奏：Oracle 定时器 120 秒；ownermac 的 `publication-pull` 在 `meter_mode` 为 `pulse_meter` 时由原生部署生成 120 秒间隔（`STATS_METER_PULL_SECONDS`），否则 5 分钟（`Scripts/pulse_native_ownermac.py`）。
**旧观测链路（observe，meter 未启用时）**：以下 launchd、到期前启动、有效期与 `install-oracle-publisher`、`switch-publisher`、`observe-mode` 的参数和步骤只适用于它。
原生部署（`Scripts/pulse_native_ownermac.py`）写入的 launchd 使用 Standard、RunAtLoad、StartInterval=120，并设 `PULSE_STATS_REFRESH_SCHEDULE=before_expiry`，不再设周一唤醒：每次唤醒只读发布物的 `expires_at` 与周期状态，当剩余有效期不超过“上一完整周期的开始到完成耗时（限 15–70 分钟，未知取 70）＋4 分钟”时才开始新周期；上一周期失败后 10 分钟内不重试；运行中的唤醒被 launchd 丢弃。手动 `refresh` 不设该变量，立即执行。旧 `publisher-exclusive/v1` 发布器（`Stats/deploy.py` 非原生路径）仍按 1800 秒，其接受证据绑定该间隔。不沿用 Background 的低优先级，不提升为 Interactive。成本发布物按真实采集时间和发布器配置的有效期判断：默认 45 分钟，Oracle 发布器经 `publication_validity_minutes` 设 75 分钟（2026-11-13 人类决定）；读取端（exporter 与 App）只接受 0–75 分钟，不另行延长；Codex 额度读数保留原读取时间，按其 7 天窗口判断。
Oracle 发布器的旧观测链路部分（2026-11-13 人类决定把计算迁出 ownermac）：定时器按同一到期前规则起轮。以下发布器安装、切换与回退步骤只适用旧观测链路。其 refresh.json 由 ownermac 执行 `Stats/deploy.py install-oracle-publisher` 生成（从读本机 `refresh.json` 到 Oracle 读回全程持 ownermac 的 deployment.lock，锁被占即拒绝，因此不会把切换前读到的登记装到切换之后）：peers 为 TCloud（以 pulse 账户直连）与推送型 ownermac（只观察时带 `observe_sources`，见下文 `observe-mode`），`paused_peers` 取自 ownermac 配置（缺省 TCloud 暂停，Oracle 不采集它），有效期 75 分钟，周期内持 `collect-shared.lock`，`registry_overlay` 补入 ownermac 发布所用的回执特性、租户登记副本（0600）、Oracle 的工作区登记与本机内核导出；不含额度、OpenRouter 密钥与 GitHub 读取（PR 审计缓存由 ownermac 推送）。Stats 部署切换或回退 Oracle 读取包时，先在 deployment.lock 下等待持有 `cost-refresh.lock` 的发布周期结束（上限 40 分钟，不终止周期），再在同一把周期锁内改写该配置的 `source_revision` 与 peer 脚本、切换 `stats/current`。写入前先把选择器所指版本、配置原文与 sha256 及目标配置写入本次尝试的 `runtime-switch-<版本>--<尝试>.json`（`switching`），先改配置后改选择器；中途失败留下的读取包与配置只会是前像或目标两端的组合，版本不一致时周期读配置即以版本不符退出，不发布。同一尝试重试 `select` 续办到目标（`selected`，记下选择器身份），`restore-runtime` 先记 `restoring`，再把选择器和配置按回执前像恢复（`restored`）；外层部署回退走同一路径。回执未完成时，其他尝试的切换、发布器安装与公开 `select` 都拒绝；配置若已被他人改成两端以外的内容，重试与恢复都拒绝，不覆盖。不带 `--source-plan-attempt` 的公开 `select` 只动选择器，Oracle 已装发布器（存在 `refresh.json`）时在任何写入前拒绝，改用带尝试的 `select`。安装同样在两把锁内核对所选读取包后写入，其中等发布周期一直等到当前这一轮结束（上限 105 分钟，即发布单元 `TimeoutStartSec=100min` 加余量；meter 现行下 Oracle 完整刷新一轮约 20–70 秒（`evidence/wi754/closeout-readback-20261123.md`，近 30 小时 545 轮），计量由 WI413 的 pulse-meter 增量完成；定时器在一轮结束 120 秒后才起下一轮，等待方在这段间隙拿到锁；ownermac 发起安装的 SSH 调用再多留 5 分钟），只写文件并 `daemon-reload`，定时器由运维显式 `systemctl --user enable --now pulse-stats-refresh.timer` 启用。ownermac 以 `Stats/deploy.py switch-publisher --publisher oracle-node-1` 在两轮之间把同一 launchd 任务改为 `collect-local`（refresh.json 加 `publisher`、`peer_id`；`--revision` 是 ownermac 原生版本），切换前先只读查询 Oracle：须已选定带安装器与采集入口（`Scripts/pulse-stats-cost-refresh.py`）的读取包、且其发布器配置的 `source_revision` 就是该读取包，否则在任何写入（含回执）前拒绝；写入的配置把 Oracle peer 的 `script` 改为该读取包的采集入口，此后的拉取与观察推送都跑 Oracle 当前选定版本，原先所指的版本在 Oracle 上被清理也不受影响；原生部署按该配置保留入口；回退为 `--publisher ownermac` 并停用 Oracle 定时器；ownermac 只观察时拒绝回退，须先 `observe-mode --mode full`。发布物由另一个 launchd 任务 `io.coevolution.pulse.stats.publication-pull`（`pull-publication`，每 5 分钟，自己的锁，见 [ComputeCost 设计](pulse-compute-cost-design.md#传输安装与恢复)第 8 条）拉回，采集运行时照常推进；它随原生部署的同一批账户文件安装、停止、启动与回退（停止前等当前一次拉取结束，上限 5 分钟；回退到不带该任务的旧版时删除任务文件，只启动旧版原有入口），`refresh.json` 没有 `publisher` 时不做事，所以 `switch-publisher` 的前像不含它；`install-oracle-publisher` 只安装 Oracle 选定的读取包（`--revision` 不符即在任何写入前拒绝），读回后同样把本机 Oracle 条目改到该包；Stats 部署在发布器为 Oracle 时把 Oracle 条目指到本次版本（Oracle 暂停采集时也一样，部署照样切换它的读取包），回退随配置前像一并恢复。拉取与推送用这个条目前先只读确认它在 Oracle 当前选定的版本上（见 [ComputeCost 设计](pulse-compute-cost-design.md#传输安装与恢复)第 8 条）。`switch-publisher` 在周期锁之后再持 `publication-pull.lock`（等在途拉取结束，上限 5 分钟，超时即拒绝、不改任何文件）改写配置与恢复前像，拉取只在这把锁内读配置。切换全程持 deployment.lock，只在任务已加载且空闲时 bootout；写配置、写任务文件或 bootstrap 失败，即恢复本次前像（配置、任务文件、原已加载的任务），回执写入 `publisher-switch.json`（`restored`）；恢复也失败则记 `failed` 与 `rollback_pending`，下次运行（目标任一发布器）沿该事务续办：任务未加载就不 bootout，再失败时恢复的仍是最初前像。事实都在共享观测流，不搬数据。按上述 `switch-publisher --publisher ownermac` 回退后（旧观测链路），ownermac 的 launchd 入口恢复为 `refresh`，承担完整刷新。
增量开关（仅旧观测链路；`incremental-mode` 同样重装 Oracle 发布器配置，meter 模式下不得执行）：ownermac 的 refresh.json `incremental`（`ingest`、`views`、`collection` 三键，缺省全 `off`）随 `install-oracle-publisher` 带到 Oracle 的 refresh.json；只由 `Stats/deploy.py incremental-mode --views off|shadow|on`（另有 `--ingest`、`--incremental-collection`）在 deployment.lock 与周期锁下修改，回执 `incremental-mode.json`，Oracle 读回不同即恢复原像。`views` 映射为发布的 `--engine full|shadow|incremental`，`collection` 映射为 `--collection-engine`：`shadow` 照常全量采集入账，入账前用同一批输入做一次不入账的增量判定，两边去重后的新事实引用写进计时行 `incremental_collect_shadow`（两边条数、对称差、前 5 个实体；前缀已被别的节点推进或两边读的包不同时只记 `prefix_moved`、`bundles_differ`）；`on` 增量采集。ownermac 的 `collect_local_mode` 取 `full`、`observe` 或 `incremental`（见下文 `observe-mode`）。
**meter 模式回退（已知缺口）**：`install-oracle-publisher` 重建 Oracle 的 `refresh.json` 时不带 `meter_mode`，重装会丢掉它；`switch-publisher --publisher ownermac` 只改配置与本机 launchd 入口，`meter_mode` 保留时 ownermac 的 `refresh` 读本机 `central.sqlite`，汇总库与覆盖不随回退移动。meter 模式下回退尚无经验证流程；回退前须先补流程（含汇总库与覆盖一起回退的验证）并实测，此处不规定步骤。
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: PublisherSwitchTests.test_switch_to_oracle_and_back_restores_the_full_refresh`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: PublisherSwitchTests.test_the_switch_waits_for_a_held_pull_lock_and_refuses_when_it_outlasts_the_bound`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: PublisherSwitchTests.test_after_the_switch_pulls_reach_the_selected_reader_even_once_the_old_release_is_gone`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: OraclePublisherConfigTests.test_selection_probe_names_a_reader_only_with_its_installer_and_cost_refresh_entry`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_an_open_switch_never_lands_an_entry_oracle_left_and_install_takes_only_its_selection`
`@anchor test Stats/exporter/tests/test_stats_deploy_transaction.py :: DeploymentTransactionTests.test_deployment_moves_the_entry_of_a_paused_oracle_publisher_with_the_reader_it_selects`
`@anchor test Scripts/tests/test_native_ownermac_cohort.py :: test_a_release_without_the_pull_job_keeps_only_its_own_entries`
暂停与恢复采集只走 `Stats/deploy.py peer-collection --peer <id> --collection paused|active`：在 ownermac 持 deployment.lock 与 `cost-refresh.lock`（等当前周期结束），只改 `refresh.json` 的 `paused_peers`，原文私有保存为 `refresh-before-peer-collection-<摘要>.json`；拒绝未登记的 peer 与发布器自身。下一周期读取新状态；Oracle 发布器由下一次 `install-oracle-publisher` 带入（仅旧观测链路；meter 模式下该安装会丢掉 `meter_mode`，尚无经验证的对应流程，不得在 meter 模式下执行，除非先补流程并实测）。`deploy`、`deploy-refresh-local`、读取包切换与 `switch-publisher` 都原样保留该字段，重部署不会恢复已暂停的 peer。恢复后的 peer 先由其上次采集所用的脚本采集，下一次完整部署再把采集脚本指到当前版本。
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: PausedPeerTests.test_peer_collection_switch_pauses_and_resumes_between_cycles`
`@anchor test Stats/exporter/tests/test_stats_deploy_transaction.py :: DeploymentTransactionTests.test_default_paused_tcloud_is_never_reached_and_stays_paused`
仅旧观测链路：meter 模式下 `observe-mode` 会经与 `install-oracle-publisher` 相同的安装重写 Oracle 发布器配置并丢掉 `meter_mode`，下一轮 Oracle 的 `refresh` 即退回旧账本链路；尚无经验证的对应流程，不得在 meter 模式下执行，除非先补流程并实测。ownermac 在完整采集、只观察与增量采集（[ComputeCost 设计](pulse-compute-cost-design.md#传输安装与恢复)第 10、12 条）之间切换只走 `Stats/deploy.py observe-mode --peer ownermac --mode observe|full|incremental`，在刷新间隙执行：在 ownermac 持 deployment.lock 与 `cost-refresh.lock`（等当前周期结束，上限 40 分钟；Oracle 端安装按上文等到其当前周期结束），只在 `refresh.json` 已有 `publisher` oracle-node-1 与 `peer_id` ownermac 时执行，拒绝其他 peer 与未了结的 `publisher-switch.json`（`switching` 或 `rollback_pending`）。Oracle 发布器跟随 Oracle 自己选定的读取包（读取包切换会改写其配置），不必等于 ownermac 的原生版本，所以安装一律用 Oracle 选定的版本：写任何文件前先经 SSH 只读查询 Oracle 的 `stats/current`，未选定读取包或该包没有 `Stats/deploy.py` 与 `Scripts/pulse-stats-cost-refresh.py` 即拒绝，不写备份、不写回执；目标配置的 Oracle 条目指向该读取包。续办未了结的回执同样先查，查不到时回执原样保留；续办到目标时 Oracle 若已改选另一读取包（目标配置里的条目已不是它），拒绝并提示先回到起始模式，回执原样保留。原文先私有保存为 `refresh-before-observe-mode-<摘要>.json`，并在任何一端改动前写私有回执 `observe-mode.json`（`pulse_stats_observe_mode/v1`）：前像与目标配置原文及 sha256、起止模式、本机版本（`revision`）与 Oracle 读取包版本（`oracle_revision`），状态 `switching`。切到 observe：先以目标配置、按 Oracle 读取包版本执行与 `install-oracle-publisher` 相同的安装，读回 `observing_peers` 含 ownermac、ownermac 的 `observe_sources` 与本机来源 ID 一致，再把 `collect_local_mode` 写为 `observe`；切回 full 顺序相反：先安装不带 ownermac 登记的配置并读回 `observing_peers` 不含 ownermac，再写 `full`。两端都读回目标后回执记 `completed`。任何一步失败或读回不一致，都把两端按前像恢复（Oracle 重装前像、本机配置写回前像），回执记 `restored` 并报错；恢复也失败（如 Oracle 已写入而回执丢失、随后不可达），或进程在两端之间中断，回执停在 `failed`（`rollback_pending`）或 `switching`，即未了结。未了结期间：`collect-local` 在拿到周期锁后读到它即不采集——既不追加观测流，也不推观察包、不前移索引——只向发布器推采集失败（原因 `observe_mode_unresolved`），Oracle 因此不记 ownermac `read`；`deploy`、`deploy-refresh-local`、`switch-publisher`、`install-oracle-publisher`、`peer-collection` 与 `enroll-oracle-peer` 在任何写入前拒绝。这些入口与 `observe-mode` 都在 ownermac 的 deployment.lock 内读配置并写入（含发往 Oracle 的安装与读回），在途的一方持锁期间另一方即拒绝。再次执行 `observe-mode` 沿回执续办：所请模式为回执目标即按目标配置重装并读回两端（`completed`），否则按前像恢复两端（`restored`），失败仍未了结，之后才解除闸门。`refresh.json` 若已被他人改成前像与目标以外的内容，不覆盖：其模式与本次要落定的一端相同即视为本机已落定，否则失败。重跑同一模式只重装并读回。`collect-local` 在持周期锁后才读 `refresh.json` 决定模式与发布器，切换前读到的旧配置不会在切换后执行。`deploy`、`deploy-refresh-local`、读取包切换、`switch-publisher` 与 `install-oracle-publisher` 都原样保留 `collect_local_mode` 和 Oracle 上的 ownermac 登记。上线顺序：拉取任务加入原生部署的账户文件后，此前准备的回退批次文件集合不同、会被拒，先以本版本的部署程序 `--prepare-native-only --reprepare-rollback` 为在运行的版本重备回退批次（运行版本不带拉取任务，批次把任务文件记为不存在）；常规原生部署（三端同版），读回 `launchctl print gui/<uid>/io.coevolution.pulse.stats.publication-pull` 已加载、`publication-pull-v1.json` 为 `pulled` 且 `cost-dashboard-v1.json` 的 `generated_at` 跟上 Oracle；刷新间隙执行 `observe-mode --mode observe`；本机索引为空时 `collect-local` 要读全部 CC 会话（与 `--observe-backfill` 同量，补录只在 observe 模式可用），每轮读 30 分钟、逐包推送确认，跨多轮补完，未补完的轮次 Oracle 记 ownermac `peer_observe_incomplete`；可在刷新间隙手动连跑 `collect-local --observe-backfill` 直到回执不再带 `remaining`。补完后 Oracle 下一轮入账，读回其 `last-refresh.json` 中 ownermac 为 `read`、`collector-sources.json` 含 `native-push-ownermac`。回退为 `observe-mode --mode full`。离开 observe（切到 full 或 incremental）前，经 SSH 只读查 Oracle 上 ownermac 的 spool：须没有待入账的包，且已入账序号不低于 Oracle 确认过的最高包序号（`observe/publisher-acknowledged.json`，只在 Oracle 确认收包时前移；观察索引的游标在 incremental 下也随本地 spool 前移，不能代替它。Oracle 收进 spool 即确认，撤掉登记后未入账的包就无人消费；该文件出现前升级上来的节点没有它，此时取观察索引的游标，那时只有 Oracle 的确认会前移它）；离开 incremental 前，本地 spool 须没有待判定的包；不满足即在任何写入前拒绝，等下一轮入账后重跑。切到 `incremental` 按切回 full 的顺序（先撤 Oracle 的 ownermac 登记），写任何文件前核对本机事实索引已建成（`pulse-compute-cost fact-index --registry <sources.json>`）且是账本前缀、只观察的 outbox 里没有未确认的包；本地 spool 接着只观察的索引读，已送出的会话不重读。增量模式下 `collect-local` 由看门狗每 2 秒查常驻内存，超过 2 GiB 即结束本轮并推采集失败 `out_of_memory`。退回是同一命令加 `--mode observe`。试跑：只观察的 `refresh.json` 加 `incremental_trial: true` 后，每个推出的包另存一份到 `observe/incremental-trial/spool/`，`pulse-stats-cost-refresh.py incremental-trial` 对它们做不入账的增量判定，结果（读取序号、包、秒数、峰值内存、新事实引用）追加到 `observe/incremental-trial/rounds.jsonl`。
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_a_lost_reply_and_an_unreachable_restore_fence_until_a_rerun_settles_both`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_back_to_full_with_a_lost_reply_is_finished_by_the_rerun`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_a_process_ending_after_the_oracle_write_leaves_the_switch_open`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_an_install_in_flight_holds_off_observe_mode_both_ways`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_observe_mode_in_flight_fences_every_writer_through_its_recovery`
`@anchor test Stats/exporter/tests/test_collect_local_observe.py :: ObserveModeTests.test_an_unresolved_observe_mode_switch_collects_nothing`
`@anchor test Stats/exporter/tests/test_collect_local_observe.py :: ObserveModeTests.test_the_cycle_takes_the_mode_observe_mode_left_before_it_got_the_lock`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_an_oracle_readback_that_differs_restores_the_before_image`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_a_local_write_failure_restores_oracle`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_another_writers_change_is_never_replaced`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_refuses_without_the_oracle_publisher_another_peer_or_mode`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_every_deployment_writer_keeps_the_observe_mode`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: PublisherSwitchTests.test_switch_keeps_the_observe_mode_and_refuses_ownermac_while_observing`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: PublisherSwitchTests.test_switch_to_oracle_refuses_before_any_write_unless_oracle_publishes_from_its_selected_reader`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_without_a_selected_oracle_reader_nothing_is_written_or_left_unresolved`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_waiting_for_a_publisher_cycle_lasts_until_the_oracle_unit_would_end_it`
发布器每个周期写私有 `cost-refresh-status-v1.json`（`pulse_stats_cost_refresh_status/v1`）：开始写 `running`，结束写 `completed`（含各 peer 的读取结果与原因、本轮耗时）或 `failed`（固定原因码，如 `production_ledger_unavailable`）；同一失败在 `cost-refresh-error.log` 的 `stats_cost_publication_failure/v1` 记录里另带 `error_chain`：异常因果链逐环的类名、机器码（如 `ledger_unavailable`、`ledger_busy`）和已登记的固定文本，用来分清 Git 超时、生产 ref 忙或已变，任何其他异常文本都不写出；三种状态都列出按决定暂停的 peer（`paused`，原因 `paused_by_decision`）。exporter 校验后以 `cost_refresh` 附在 snapshot 顶层；超过 90 分钟仍为 `running` 视为 `cycle_interrupted`。
`@anchor test ComputeCost/tests/test_stats_dashboard.py :: StatsDashboardTests.test_a_failed_publication_names_its_cause_chain_without_free_text`
观察包积压未清时照常发布，但标明未收齐：采集把推送型 peer 的部分包留到下一轮时（以 `native_push` 摘要的 `deferred_bundles` 大于 0 为准，同轮有坏包时摘要原因仍是 `observation_bundle_invalid`），当前与上周两份发布物都带顶层 `collection_backlog`（`status` 为 `partial`、`reason` 为 `observation_backlog`、`peers` 列出各 peer 的 `id` 与 `deferred_bundles`；发布器增量采集时自己本地 spool 的积压记在 `id` 为 `local` 的一项），并计入 `sha256`，发布器 stdout 的结果行同样带它；周期状态与 `last-refresh.json` 给该 peer 带上 `deferred_bundles`：本轮推送回执新鲜时记为 `partial`、原因 `observation_backlog`；回执已过期或无效时保留原状态与原因（如 `peer_push_stale`），只补上待读包数。exporter 校验后原样保留，查询入口在 `sources.cost_publication.collection_backlog` 给出，App 在总成本格用醒目色写“未收齐：<peer> 待读 N 包”，在“产出与成本”表上方（菜单栏卡片与完整窗口）整表标出“数据未收齐，金额与归属不完整”并说明表中数字不是完整结果，完整窗口的合计改写为“未收齐 · 已收部分”，来源说明写“观察包积压，分轮读取中，待读 N 包”。积压读完的那一轮起不再带标记。
`@anchor test ComputeCost/tests/test_stats_dashboard.py :: StatsDashboardTests.test_a_collection_backlog_marks_both_publications_and_still_publishes`
`@anchor test Stats/exporter/tests/test_cost_refresh_push.py :: PushedPeerTests.test_a_push_whose_bundles_wait_is_a_partly_read_peer`
`@anchor test Stats/exporter/tests/test_cost_refresh_push.py :: PushedPeerTests.test_waiting_bundles_stay_reported_beside_a_stale_push`
`@anchor test ComputeCost/tests/test_native_observations.py :: ObservationBundleTests.test_an_invalid_bundle_and_a_backlog_are_both_reported`
`@anchor test Stats/exporter/tests/test_query.py :: CollectionBacklogTests.test_the_marker_is_validated_and_reaches_the_query_source`
App 在本机运行固定只读 exporter 获取发布物，打开/筛选/排序不重算全账。exporter 把已过期但校验通过的发布物原样带给 App，查询入口仍按过期处理。失败保留上次发布物及其原时间，过期后仍显示并标注，直到更新的有效发布物替换；新算出的无金额结果（`unavailable`）不覆盖已有金额的发布物，本轮记为失败。不清账、不清事故、不假报恢复。部署失败按原安装器回退本次程序/配置并读回；应用重启不是整机启动恢复的证明。
Peer 读取失败不放弃整轮：本机照常发布，该 peer 的传输来源按原规则记为未读取，周期状态与 `last-refresh.json` 记其原因（`peer_busy`、`peer_ssh_failed`、`peer_production_ledger_unavailable` 等）。远端 stderr 只保留末尾 64KiB 内的 `stats_refresh_progress/v1` 记录中白名单字段（最多 4 条）及 traceback 末行的异常类名，其他行只计数，不记原文。生产账本（PULSE `main` 上的 `.harness/core`）本身不可读写时，两端都无法入账；此时不发布新时间的旧金额，App 按上述规则继续显示上次发布物。

## 验证与发布门

候选测试覆盖未知/零、冲突、跨期、迁移、重放、半行、晚到绑定、缓存失效/强制、传输截断/恢复，及 Python → Swift → 420 点全框架内的滚动/筛选/列宽。真实周期还须使用实际服务登记和 launchd 调度，不以 runner 前台耗时替代。
当周技术验收逐项检查已纳入用量的六维归属、数量、计价、归期、冲突和来源读取错误；表格 event_count 与查询守恒。明确在途单列；历史捕获 gap 与全量覆盖 unknown 不消失。已发生未解释消费仍不能交付。
同 SHA 完成安装身份、触发发布并重启 App 后三个 activity 视图原查询/发布物/App 一致，以及部署后至少一个自然调度周期经自然验收作业复算一致；安装成功或一张测试截图不足以通过。保留失败与未执行证据，不手工补采制造两次自然发布，不代替人类体验或原 WI 的 Outcome/closeout。

## 已证基线与边界

### 当前两卡片采用：PR #645

App与三端Stats读取包采用 `f696ec0912a7112cef691a5beaf87cda5a34049b` /
Build `35428181632-1`。[正式部署35428181632](https://github.com/bigbird-online/PULSE/actions/runs/35428181632)
负责合并主干构建、安装、自然发布与重启回读；不是用候选截图代替安装版。

2026-09-19 15:08北京安装版快照，由与运行版逐字一致的Swift解码器/视图模型回读：
26个在飞项、19条请求，显式关联全部一致，两列表创建最久优先；7项有“自动”标记。
状态与创建年龄色阶来自同一行原字段，0–7天绿、8–30天橙、31天起红，不改故障等级。
缺失关联不从文字推断；内容与数量保持，未知日期保留。完整原生明暗420点渲染及
275项Swift、628项ComputeCost和适用Linux回归通过。三端cron配置摘要不变。

正式部署35428181632已成功。15:11:10与15:30:15两次自然成本发布（水位303/304），
原查询、发布物和App的全部21视图一致；第二次含App重启，原定时器未被加速。
15:34重启后再用同版Swift模型回读，26个在飞项/19条请求/7项自动、创建排序与色阶
仍一致；15:37再次验证三端业务cron配置未变、九项暂停、五节点Oikos与退役历史保留。
实际App/三端读取包版本相同；源明细与缺失边界没有为显示效果被删除。
本轮不重启Relay、不改SCL权限/计价、不复活探针，不将“自动”标签当作cron已运行。
新样式尚待用户直接体验；月度旧历史和原有独立归属冲突仍保留前述边界。

### 前序归属修复采用：PR #635 / #638

App及三端Stats读取包为 `1b99a459ad7ce5155c9a2fac9edb7388ec1c2d1b` /
Build `35420410820-1`；[正式部署35420410820](https://github.com/bigbird-online/PULSE/actions/runs/35420410820)
已成功。#635接通既有Host、原生边界、工作区身份和来源采用；#638修复合法v1/v2混合
计量被误判为坏来源而阻断整批连接。第一版35417939806的真实失败仍保留，不混成通过。

| 北京时间2026-09-19 | 水位 | 实际消费 |
| --- | ---: | --- |
| 12:18:26自然发布，12:20:14核对 | 294 | 原查询/发布物/App全部21视图一致；今日Agent未知、WI未关联均0。 |
| 12:37:22自然发布，12:39:20重启回读 | 295 | 同源21视图一致；原14条逐项身份、原数量/模型及旧285水位再次核对；当前读取错误0。 |

原14条分为Reeve/WI282的10条、Vultr Pulse非WI诊断2条、TCloud Pulse非WI诊断1条、
Mac Codex Pulse非WI1条；补证只对应原精确记录，不给将来消费设默认Agent或WI。
本周仍有1条独立WI归属冲突，月度旧历史仍有未识别记录；未以本轮修复清掉或假装全量完整。
九项业务继续计划暂停，三端来源身份、归属字段和原工作区规则保留；Oikos和六项退役
历史未被改写。详细闭环见[归属回归实证](evidence/stats-attribution-regression-20260919.md)。
原Host捕获代码已通过同版Relay的三端正式采用，实际文件摘要一致；其共同主干ccc43a411
包含并行公开过程显示改动，成本读取包仍为上述1b99a459。两者不虚构为同一安装SHA。
UI本轮未更换，以下#626的人类反馈只适用于其展示，不外推本次所有业务/质量/ROI验收。

### 已接受的显示与传输基线：PR #626

该轮App与三端Stats读取包为 `3ebb262bd484b808b08a1e66f48e09b093578a54` / Build `35355435323-1`，[PR #626](https://github.com/bigbird-online/PULSE/pull/626)、[正式部署35355435323](https://github.com/bigbird-online/PULSE/actions/runs/35355435323)已完成。2026-09-19用户明确反馈“新版本已基本符合预期”，并要求统一README和Specs；此前“尚未取得本轮人类体验反馈”的边界在此更新，不扩张为全部业务验收。

| 北京时间2026-09-18 | 水位 | 证据范围 |
| --- | ---: | --- |
| 22:31:33自然发布 | 249 | 原查询/发布物/App共21视图一致；TCloud跨旧64MiB边界恢复67,813,975字节，原40,493条事实均保留。 |
| 22:49:57自然发布 | 250 | 同源21视图一致且包含App重启；本轮确有TCloud SSH255，数值一致不冒充全部来源read。 |
| 23:08:49第三轮发布，23:11App重启回读 | 251 | 原定时器自动恢复TCloud，67,888,280字节/40,559条凭据，前次40,553条全部保留；三个期间的传输失败退出当前缺口。 |

详细原始证据、270项Swift/606项ComputeCost及418项适用Linux回归见[部署验收记录](evidence/stats-readable-summary-20260918.md)。九项业务仍为迭代期暂停，三端配置不变，Oikos五节点与六项退役历史保留；没有启用付费cron来制造成功样本。
此前采用沿革：[自动任务/退役](stats-automation-card.md#11-2026-09-18-安装版证据)、[五卡片审查](evidence/stats-presentation-audit-20260918.md)、[当前运行与双入口明细 #615](https://github.com/bigbird-online/PULSE/pull/615)、[核验来源及提交边界](evidence/stats-source-reliability-20260918.md)。历史记录不因新版本被删改，旧菜单/旧表格/旧来源说明也不能覆盖本文件现行设计。
2026-09-19 07:37安装版快照复核：仍为上述App，协作核验validated；07:34成本发布及07:35接收回执显示两个peer均read、21视图、无当前读取错误、九项暂停。只是带时点复核，不替代未来运行状态或连续七天完整性证据。
本轮无另行待合并/待部署的Stats显示或TCloud上限修复；历史捕获缺口、全平台coverage unknown、完整Outcome库存、完整WI成本/质量/ROI和原R1–R4退出条件仍保留。逐项恢复cron后的真实服务交付、整机启动恢复和长期可靠性须各自验收；本次只更新文档、不改WI终态或开启任务。


### 历史界面与归属修正：PR #591

[PR #591](https://github.com/bigbird-online/PULSE/pull/591)合并并经[正式部署 35222056468](https://github.com/bigbird-online/PULSE/actions/runs/35222056468)完成三端采用与消费者验收。安装 SHA `4a3e4e299abe8ce8e1212ee06a5a868cbc2aa286`，App Build `35222056468-1`；这是一条有日期的发布基线，不替代当前状态检查。

| 生产验证（北京时间 2026-09-17） | 水位 | 今日用量 | 本周用量 | 结果 |
| --- | ---: | ---: | ---: | --- |
| 20:48:50 发布，20:50:32 App/查询回读 | 164 | 46 | 1,257 | 六维归属、未计价、无法归期、冲突、来源读取错误及待回报来源均0；查询/发布/App一致。 |
| 21:07:53 自然发布，21:09:40 App重启后回读 | 165 | 51 | 1,262 | 同上；新增5条自然入账，scheduled_publications=2、app_restart_verified=true、installed_consumer_verified=true。 |

生产证据见该run的 `installed-readback.log`、`actor-activation.log`、`deployment.log`；artifact `10498374436`，归档 SHA256 `5b657841fc27425332c6efe7211bde82e8e34e8a8d6d901817bb5e77db7ddeae`。正式读取保留原来的6件事故与全平台coverage unknown，没有用本轮成本修复清掉其他问题。
候选界面与精确非WI回放见[35221215494](https://github.com/bigbird-online/PULSE/actions/runs/35221215494)；原生图确认单一统计详情、Codex展开有今日Token且重置时间不外露，渲染使用合成输入，不称生产截图。用户随后反馈“已基本符合预期”，仅作为这轮显示体验反馈，不是整个WI收口。
前序#583的真实Outcome接入回放保留在[35213542188](https://github.com/bigbird-online/PULSE/actions/runs/35213542188)：已登记ownermac来源隔离导入66份正式Outcome，本月9份有名称、4行仅成果。该回放不证明全平台Outcome库存完整；缺数据不以Token、关闭WI或测试数替代。

### 历史七栏成本链基线：PR #574

[PR #574](https://github.com/bigbird-online/PULSE/pull/574)、[正式部署 35186723672](https://github.com/bigbird-online/PULSE/actions/runs/35186723672)：安装 SHA `edeab6984e2148d719084611f709d6f90c7fc62b`，App Build `35186723672-1`，三节点同版；同 run 587 ComputeCost、103 Stats Python、239 Swift 回归通过。

| 生产验证（北京时间 2026-09-17） | 水位 | 已纳入本周用量 | 结果 |
| --- | ---: | ---: | --- |
| 13:50:10 发布，13:51:44 App/原查询回读 | 140 | 1,256 | 六维、计价、归期、冲突、读取错误均 0 缺口；待回报来源 0。 |
| 14:08:50 自然发布，14:10:25 重启后回读 | 141 | 1,256 | 同上；全部 21 视图一致，scheduled_publications=2，app_restart_verified=true。 |

14:13 辅助只读核对，正式 App 缓存与发布物的时间、报告摘要及水位一致，联合表 10 行，TCloud/Vultr 均 read。部署 artifact `10482819605`，SHA256 `2e239b112e38ebc52b9e4862b3963c4add431c86c5533ce6e2c0b4948b5bb7fe`。
性能分层记录：[增量 UAT 35107628877](https://github.com/bigbird-online/PULSE/actions/runs/35107628877)的冻结副本 21 视图冷/热/强制约 57.2/9.4/56.9 秒；热模式 0 查询/0 计价。它不含三端采集、传输与 App 刷新，不能称全链 9.4 秒；上述正式首次启用至 App 回读约 4 分半，两个发布点相隔约 18 分 40 秒，不是永久性能保证。
本次证实的是带日期的已登记成本消费路径。原始历史捕获缺口、全平台覆盖 unknown、进行中 WI 完整成本、质量/ROI 来源和历史月账各保留原限制；不宣称全平台长期无漏或整机重启已验收。后续版本与状态须重新读回，不复用本表作为当前健康证明。


### Bounded unpriced diagnostics (WI300 production recovery)

The 512 KiB carrier limit and original ComputeCost queries are unchanged. Costs with more than eight unpriced observations publish at most two diagnostic samples per reason, plus `unpriced_count`, `unpriced_reason_counts`, and `unpriced_sha256` over the complete ordered diagnostic list. Every reason and its exact count remains visible; the App displays the full count, never sample length. The full query remains reproducible at the existing watermark and `report_sha256`. This projection never truncates activity groups, events, Token totals, amount fields, coverage or reason codes. Both readers reject incomplete or inconsistent count metadata. Installed verification recomputes the full original query and its diagnostic projection before comparing the publisher and App; unknown quantity/price remains unknown.

### 后台刷新与计算复用（2026-10-04）

后台发布器按[安装、调度与恢复](#安装调度与恢复)的到期前启动规则调度；App普通取数和手动刷新不触发全账计算，成本证据有效期按上述发布器设定，不另行延长。查询先应用归属，再按领域建立只读索引；只对实际冲突候选做依赖关联，同一冻结水位的诊断投影在今日／本周／本月间复用；增量引擎下各视图共用同一水位的会话包现状。原始事实、版本链、冲突与归属规则不变。

计算缓存按真实账本（增量引擎下为窗口内会话包现状）／价格内容／期间／代码版本校验；增量视图的 `report_sha256` 是窗口内报告的摘要。只有价格拉取时间变化时可复用原报表，保留其原`catalog_observed_at`和`report_sha256`，不伪造重新计算或价格观察时间；价格文件的`retrieved_at`仍记录最新成功拉取。价格内容、证据有无或倒退、账本、期间和代码变化会重新计算，源校验失败不能靠缓存继续发布。后台日志增加采集、账本快照、各期间计算／命中、总墙钟和CPU耗时，不把计时写进业务账本。

### CC 累计解释与来源快照增量（WI300，2026-10-04）

CC 由 ComputeCost 按[持久累计计数设计](pulse-compute-cost-design.md#cc-请求汇总与持久累计计数)输出明细及有界未展开差额；父子共用原 WI，金额来自标准价表。发布/App 不相加连续累计快照，不把差额当作子代理明细或精确日计数；原始消息、计量终值、剩余冲突及粗时间缺口仍可在同水位查询核对。当前轮是否已部署及真实金额效果以原 WI 的 Actions 回执为准。

每次来源变动只更新来源覆盖指纹。已入账且未变的断言保留原 source-snapshot 见证和稳定 source_ref，不为整段历史重复追加证据版本。新断言、新角色/计数矛盾和新来源仍正常保留；旧账本历史不删。CC Git 工作区校验仅在本次来源采集内共用缓存，下轮重新核验，不持久缓存身份。有效期不变。例行周期不再读取全部历史；全部历史只在重建与每周审计时读。

### 本批归属计算的历史范围（WI300，2026-10-04）

账本与来源验证仍完整执行。会话用途申报的采集投影只解析本批实际用量目标所对应的自身声明版本图，保留图内的所有生产者、分叉、前驱与原用量的更早时间边界；不以当前会话标签覆盖独立证据。原生父子请求的候选构建和旧声明撤回共享同一轮已解析版本图，不再重复全账解析。这里只删除重复工作，不改变 MCP 提示词、调用次数、对象归属规则或凭据内容，也不跨采集周期缓存 authority。增量采集下，各阶段的输入是本轮变化的会话包与公共包。

验证要求：固定完整输入下新旧函数输出及证据逐字相同；相反声明、过早边界、撤回、重复采集和原生父子关系的反例不变。自然刷新时延与函数对照分别报告，不把函数节省当作整轮完成时间。

当原生 Codex 进展观察符合 ComputeCost 的同轮证明时，Stats 消费恢复后的数量和已知部分金额；不得把未完成轮次写成已结算，也不得把仍有来源/身份/价格缺口的部分总额称为完整总额。历史分叉证据不删除，实际采用以同水位 App 回读为准。

### 已记录的人审 v2 消费（WI300，2026-10-05）

ComputeCost 对新 Harness 人审 v2 的消费遵循[已有设计](pulse-compute-cost-design.md#已记录的人审-v2-消费wi3002026-10-05)。Stats 不将已记录的人类豁免显示成新增独立验收通过，亦不从单独人审凭据派生新的成果。三端实际来源读取、发布器及 App 同水位验证须分别通过；没有执行库存分母时，修复文档解析不代表 coverage 完整。
