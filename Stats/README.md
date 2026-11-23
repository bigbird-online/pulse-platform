# PULSE Stats

Stats 是人类的只读协作看板：看当前运行、自动任务、需要参与的事项、在飞工作及产出与成本。它不拥有执行权，不另建账本，不在 Swift 计价或推断成果。
管理入口见[平台 README](../README.md#stats)；[查询设计](../specs/stats-query-dashboard.md)规定数据口径，[呈现设计](../specs/stats-presentation.md)规定交互，[已证基线](../specs/stats-query-dashboard.md#已证基线与边界)记录安装和验收。

## 当前使用

菜单采用并排的双行 badge：**CODEX** 与 **CC** 下方各显示 7 天额度已用百分比（无有效读数显示横线，不以0替代），**WI** 下方直接显示当前 snapshot 中“要你出手”的 producer 汇总值；没有 snapshot 才显示横线。若部分请求来源待核验，保留可读 WI 数字并在右侧显示 ?；运行异常和夜间状态继续用 ! / ? / ◐ 独立标记，不替换有效数字。
面板固定 **420×580 点**。顶部标题显示 PULSE、状态灯和快照更新时间；运行异常时附运行状态，无待办且运行正常时附“正常”，不重复待办数。五张卡片共用标题、留白和折叠样式，详情默认隐藏、展开后左对齐；正常刷新保留用户展开、选择和列宽。

| 卡片 | 默认信息与展开内容 |
| --- | --- |
| 仪表盘 | 四格为平台运行、新增 WI、PR 数（本期内核合并的 PR，未提供不当作 0）、总成本。平台运行只回答已监测项当前是否正常；今日／本周／本月控制另外三格和下方明细。总成本完整时取完整金额，否则取同一查询全部已计价的标准金额，没有已计价数据显示横线。活跃 Agent 列在统计详情，阶段产出在“产出与成本”。 |
| 产出与成本 | 阶段产出展开期间形成、截止累计及准确版本贡献；成本表保留 Agent／WI／原生 WI 成果／金额四栏、最多5行，查看全部打开唯一、可调整大小的八列明细窗；成本表下方显示来源读取缺口提示。 |
| 自动任务 | 九项重要日常／每周服务及协作收件箱，任务名和状态文字常显，执行与结果依据逐项展开；协作收件箱折叠行直接显示最近交回原因，主身合同标“主身”，再展开到每张合同的时间线与下一步。 |
| 要你出手 | `Pulse · WI编号`为主标题，原WI状态与创建时长为色标，原标题为辅助说明；最久优先，原请求正文折叠。 |
| 在飞项 | 同样按创建最久优先，状态/时长色标一致；明确无需主身参与者附“自动”，活动依据展开。 |

[Oikos 平台概览](../specs/stats-oikos-health.md)在仪表盘总览下方、WI 账本核验上方，默认收起。五节点资源来自原采样，不重新判断阈值；过期读数仅作灰色历史参考，不证明当前健康。
“WI 账本已核验”只表示 Stats 完整读到原生 WI 账本，且与本次快照为同一版本；不等于全部业务或完整成本都正确，读取不完整或版本不符仍明确说明。原生资源与App刷新时效见[来源可靠性](../specs/stats-source-reliability.md)。
Codex 7 天额度（提供方滚动 7 天窗口，不是自然周）展开显示重置/读取时间和“今日已记录用量 · 全部Runtime”的四类Token；不是Codex专属用量，更不由额度换算。读数来自成本发布器的账号查询，与 CC 同一规则：仅当重置时间未到且读取时间落在当前 7 天窗口内时显示最近读数，否则显示待更新及原因。菜单和面板使用同一有效账号读数。
CC 7 天额度（CC：Claude Code订阅）行内同时显示近 5 小时窗口已用比例，展开显示重置时间和额度读取时间。读数来自Relay在真实CC回合记录的最新限额事件，Stats不发起回合、不读凭据；仅当重置时间未到且读取时间落在当前 7 天窗口内时显示，否则显示待更新及原因。

完整明细列为 **Agent／WI／产出／金额／入口／节点／Runtime／模型**，保留调宽、数值排序、筛选与行详情。所有单元格垂直居中，文字左、数字右；表头与表体分区裁剪，只同步水平位移。主弹窗不再嵌套表格滚动，两处共享同一发布数据、期间、筛选与选择。
阶段产出由同一查询的 `stages` 提供，按类别、单位、可比口径和新增／改进／复用分别显示，异类数量不相加。期间形成按发生时间，累计截至该期末及当前知识水位；同一对象的多个版本不重复计整件。展开可见具体范围、WI、准确版本和证据；缺数量保留未知，归属／修订冲突明确提示。Agent 声明不等于核验或人类接受，未接入准确版本验收时不推定为零。旧快照没有阶段字段时显示未接入，不以原生 WI 成果数代填。
原生 WI 成果按稳定 Outcome 去重，成本表行内是引用而非模型贡献；仅成果、无当期消费的 WI 仍可见，成本不补零。这组历史记录与阶段产出独立展示，不相加。成本表行筛选不改变上方查询范围与阶段汇总，换期间清空行筛选；精确金额、Token、来源和技术凭据放入“统计详情”。
正数不足一美分显示 `<$0.01`；未知、零、未计价和缺来源分开。“产出与成本”卡在成本表下方提示“N 项成本用量来源未完整读取”，只数当前来源中读取失败或状态未知的项，已退役节点的历史来源不计入；展开点名来源及最新成本可能不完整的影响，不能说成主机宕机；缺来源明细也不说“0来源”。
WI 流程标签显示 SCL 已证明的当前流程；同一 WI 从 lightweight 正式升级后显示 standard。缺少或冲突的证明不显示标签，不从图、检查点或历史文字猜测。
创建时长取原created_at，不用更新时间、handoff日期或WI编号推算。0–7天绿、8–30天橙、31天起红，只表年龄不判事故；未知时长灰色。“自动”只表参与方式，不表示任务正在执行。

[自动任务](../specs/stats-automation-card.md)为每日早报、每日伴读、S1日复盘、WI日巡、Oikos日检、S1/S2/S3周复盘、独立周对账及协作收件箱。已启用的任务按真实运行结果显示；没在运行的（待恢复、未获许可、已停用）显示灰色“未运行”，不是失败也不是正常；Stats不启用或补跑任务。六个旧探针已退役，原事件保留但不再进入当前告警。

## 发布与消费

ComputeCost 在同一水位物化今日／本周／本月三个联合 activity（活动）视图；临时单维分析通过原查询按需执行，旧21视图发布物继续可读。activity 携带阶段产出、原生 WI 成果总表、行引用、仅成果行及详情；发布器不重新计量产出，读取器校验期间、引用及汇总明细关系，Swift 只显示原查询数量。阶段明细超过单视图64KiB时，该部分明确为 `stage_payload_too_large`，不截取样本冒充完整汇总，也不遮蔽已有用量和成本；原报告仍由水位与摘要定位。App 在 ownermac 本机以当前账户运行固定 exporter（快照读取器），把发布物加入总快照后消费并缓存。采购与协作标准金额独立。
受管发布器在 ownermac 读取本机及 `refresh.json` 登记且未暂停的 peer（现为 Oracle；TCloud 自 2026-11-15 缺省按决定暂停采集，见[传输设计](../specs/pulse-compute-cost-design.md#传输安装与恢复)第 7 条）的原登记凭据，沿原账本核销，不累加节点总数。当前传输使用前缀证明与有界压缩帧，验证全部凭据后原子替换；累计历史超过旧64MiB不再永久停发。失败保留已入账事实，恢复不伪造完整业务覆盖。协议边界见[传输设计](../specs/pulse-compute-cost-design.md#传输安装与恢复)。
App普通Refresh和打开明细只读取发布物，不扫描日志或重算全账。成本发布由原launchd `io.coevolution.pulse.stats.cost-refresh` 以Standard优先级调度；原生部署每120秒唤醒，在发布物过期前按上轮耗时提前开始新周期（规则见[安装、调度与恢复](../specs/stats-query-dashboard.md#安装调度与恢复)）；同一周期把Relay写的 `~/.local/share/pulse/data/relay/quota/cc-quota-v1.json` 校验后发布到本机 `~/.local/state/pulse/stats/cc-quota-v1.json`，拒绝更旧读数，失败保留原读数，状态记入 `last-refresh.json` 的 `cc_quota`；同一周期还按 `refresh.json` 的 `grok_quota` 读一次 GROK 账号周额度（Cursor 内部接口，token 只在内存），脱敏后发布到 `~/.local/state/pulse/stats/grok-quota-v1.json`，状态记入 `grok_quota`，手动单次读取用 `pulse-stats-cost-refresh.py grok-quota --config <配置>`；同一周期还按 `agy_quota` 以账户 HOME 执行一次 `agy -p /quota --output-format json`（仅 ownermac），发布到 `~/.local/state/pulse/stats/agy-quota-v1.json`，读取失败即以带原因的不可用事实替换旧读数，状态记入 `agy_quota`，手动单次读取用 `agy-quota` 模式；间隔不等于端到端时延。`refresh.json` 未配置 destination（远端接收方）时，发布器只写本机，`last-refresh.json` 的 `destination` 记为 `not_configured`。运行包位于各服务账户 `~/.local/share/pulse/stats/releases/<SHA>`，`stats/current`指向已采用版本；不是开发checkout。

本地发布器、部署及原生用途登记只使用业务账户的 `$HOME/.local/share/coevolution/python-env/bin/python`。安装前验证实际解释器和环境目录，并确认依赖已存在；不创建环境或补装包。环境版本和维护由 Coevolution 的 `ssot/toolchain/` 定义。

TCloud peer 的成本导出和自动任务快照使用其 pulse 账户的 `/home/pulse/.local/share/coevolution/python-env/bin/python`，不依赖 SSH 或 cron 的 PATH。完整部署将已识别的历史 Python 入口迁至此路径，再用该入口执行 peer 预检，未知入口拒绝迁移；同时从 `refresh.json` 移除已退役的 Vultr peer 及其 destination：持有 cost-refresh 锁，核对配置原字节后原子替换，原件私有保存为 `refresh-before-<SHA>.json`。仅修复现役 `refresh.json` 的 peer Python 字段时，先验证两端账户的标准环境，再持有现有 deployment/cost-refresh 两把锁，确认发布器空闲并核对配置原字节后原子更新；保留私有原件与回执，其余字段、代码版本、App 和调度不变，下一次自然调用须读回真实解释器。
`deploy-refresh-local` 可将确切登记的旧 ComputeCost 解释器切换到标准环境，保留其它调度字段、私有来源与 App；解释器预检失败、采集器忙碌或登记漂移时停止。失败回退恢复原配置与启动参数，旧环境只有在活动消费者和保留的回退引用均解除后才能退役。

## Agent 查询接口

Agent 不需要 `~/.local/state/pulse/stats/` 的文件授权：Relay 在 ownermac 提供只读工具 `pulse_stats`（CC 与 Codex 经 `pulse` MCP 服务，Pi 经同一 WI 核心扩展；带 `--tools` 白名单的 Pi 会话须列入该名），在宿主账户下运行已安装 Stats 发布包里的 `Stats/exporter/pulse-stats-query.py`。工具只在该脚本和共享 Python 已安装时出现；固定最小环境、60 秒超时、2 MiB 输出上限，不转发 stderr。

```json
{"query": "cost", "period": "week", "group": "agent"}
```

| 字段 | 取值 |
| --- | --- |
| `query`（必填） | `overview` 仪表盘四格与额度；`cost` 产出与成本；`wi` 单个 WI 的三期成本、Outcome、在办与要你出手；`quotas` CODEX/CC/GROK/agy 额度、订阅与 OpenRouter；`health` 平台运行、能力、Oikos 节点与 Relay 运行读数；`board` 要你出手与在办；`automation` 自动任务；`metric` App 任一指标（不带 `metric_id` 时列出全部）；`snapshot` App 显示的整份快照；`agent_day` Agent 日明细；`compactions` 各 CC 原生会话的上下文压缩次数 |
| `period` | `today`（默认）／`week`／`month`，同 App 的 Asia/Shanghai 今日／本周／本月；`last_week` 为上周一至周日，只用于 `cost`，读 `cost-last-week-v1.json` |
| `group` | `cost` 按 `agent`／`wi`／`runtime`／`model`／`node`／`entrypoint`／`tenant` 汇总；不给则返回原发布行 |
| `work_item_id`、`agent`、`runtime`、`metric_id` | 过滤或指定对象；`wi` 必须给 `work_item_id` |
| `session_id` | `compactions`：一个 CC 原生会话 ID，查询时换算成会话范围摘要再比对；也可只给 `work_item_id` 看该 WI 的会话，都不给列出全部 |
| `date`、`end_date`、`ws` | `agent_day`：一个 Agent 在 UTC+8 某天的提交、PR 与 WI 事件明细，必须给 `agent` 与 `date`；给 `end_date` 时改为每天一行计数；不给 `ws` 时读登记表的正本工作区加其全部执行面目录（含已退役的），给了只看这一个目录，实际读的目录列在 `workspaces`；`--pr-bodies` 可指定 PR 缓存。规格见[Agent 日明细查询](../specs/stats-query-dashboard.md#agent-日明细查询agent_day) |

同一查询在宿主账户命令行可直接运行（默认 JSON，`--format table` 为人读表格）：

```bash
"$HOME/.local/share/coevolution/python-env/bin/python" -I -B \
  "$HOME/.local/share/pulse/stats/current/Stats/exporter/pulse-stats-query.py" cost --period week --group agent
```

数据源与 App 同源，且只读：

- 成本、产出与订阅读成本发布物 `cost-dashboard-v1.json`，经 exporter 同一校验器（`load_cost_dashboard`）。
- `wi` 另给 `rollovers`（该 WI 已完成的会话 rollover：`count`、其中 Agent 自己请求的 `by_agent`、逐次 `items`）与 `context_compactions`（该 WI 名下的压缩次数，格式同 `compactions` 查询）。rollover 读 `wi-facts-v1.json`（内核 `stats-facts` 导出各 WI 的 `rollovers`），超过 75 分钟标 `stale`；旧内核导出没有该字段时 `count` 为 `null`（`core_export_without_rollovers`），不当作 0。压缩次数读 `context-compactions-v1.json`（Relay 按会话记录的 `compact_boundary` 次数，只含 CC print 模式；交互模式不计数，见 [Relay 会话运行时设计](../Relay/specs/features/conversation-runtime/design.md)），由刷新周期从 Relay 文件校验、盖上读取时刻 `observed_at` 后发布，并向远端发布器转发；文件缺失时来源为 `unavailable`，不当作没有压缩。每行的 `by_wi` 按每次压缩发生时所挂 WI 拆分次数；按 WI 查询只累加该 WI 名下的次数（每行另给 `wi_compactions`），会话之后改挂别的 WI 不把此前的压缩带过去。计数是 `observed_at` 那一刻的值，超过 75 分钟（同 `wi-facts-v1.json`）来源标 `stale`，`coverage` 的 `uncovered_from`、`uncovered_to` 写明此后未覆盖的时段。该记录只收自 `recording_started_at` 以来至少压缩过一次的会话，30 天未再压缩或超出 2000 个的会话被移出，`dropped_through` 是被移出会话中最晚的末次压缩；`coverage` 的 `last_compaction_from`、`last_compaction_to` 即答案覆盖的末次压缩时段。每个答案带 `status`：单个会话有行且未过期为 `complete`；按 WI 汇总时还要求该 WI 的创建时刻（读 `wi-facts-v1.json`）不早于 `recording_started_at`、晚于 `dropped_through`，否则为 `partial` 并给出 `reason`（`record_stale`、`observation_time_unknown`、`work_item_start_unknown`、`work_item_predates_record`、`record_dropped_sessions_since_work_item_start`）；没有匹配行时 `total_compactions` 为 `null`、`status` 为 `unknown`（`not_in_published_record`）：可能没压缩过，也可能在范围之外，不当作 0。`compactions` 查询不是 `complete` 时整体 `status` 为 `partial`。
- `agent_day` 不读 App 与成本发布物，读三个仓库已同步的 `origin/main`、PULSE 上的内核账本、`wi-facts-v1.json` 与刷新写的 PR 缓存 `pr-bodies-v1.json`，不 fetch、不调 GitHub。
- 额度、健康、在办、自动任务和其他指标读 App 最近一次校验并显示的 exporter 原始响应 `~/Library/Application Support/PulseStats/snapshot-v2.json`。查询不运行 live exporter，因为它会占用 App 刷新所需的事故状态写锁。App 夜间（北京时间 20:00–08:00）不刷新；这时本机成本作业（refresh 或 collect-local）从定时触发起、整个周期内每 2 分钟检查一次：查询可接受的最新快照剩余有效期夜间不足 10 分钟（白天不足 5 分钟，即 App 已停刷）就用 App 同一条 exporter 命令另存 `~/.local/state/pulse/stats/agent-snapshot-v1.json`，查询取两者中较新且可校验的一份（来源 kind 为 `refresh_cache`）。两者都过期时，答案照常返回并标明来源过期。

返回 `pulse_stats_query/v1`：`status` 为 `ok`／`partial`／`unavailable`。`sources` 给出每个来源的 `generated_at`、`expires_at`、`age_seconds` 和 `stale`；`errors` 点名过期、缺失或无效的来源；`code_revision` 是执行查询的发布包版本。金额沿 App 规则：完整金额优先，否则取同一查询的已计价合计，`amount_complete=false`；没有计价时为 `null`，不代表 0。`group` 汇总只把已发布行的金额相加，不重新计价。给了 `agent`／`wi`／`runtime` 筛选时 `scope=filtered`：`summary` 与 `outcomes` 只算筛选后的行，全平台数据放在明确标注 `scope=global` 的 `platform` 中。成本沿 App 的 Asia/Shanghai 日界：发布物不是今天生成的，或视图日期不覆盖其所在日的今日／本周／本月，则成本不可用（`cost_day_rolled_over`／`view_unavailable`），不拿上一期充当本期。发布器结周后（WI754 第 5 步），原有金额与 Token 字段仍是窗口内的自身用量；视图与行另带三样：`adjustment` 是本窗口内记账、针对已结周的调整分录（`entry_count`、`unpriced_entry_count`、带符号的 `amount_usd`、`known_total_tokens` 与四类 `tokens`，`by_origin` 按原属周与原因 `reattributed`／`quantity_revised`／`late_usage`／`withdrawn` 分列），`total` 是自身用量加调整（`amount_usd`、`known_total_tokens`、`tokens`），应展示的数字取它；只有调整、没有自身用量的键列在 `adjustment_only_rows`；`group` 汇总与筛选后的 `summary` 把所含行相加，给出同样结构的 `adjustment` 与 `total`，所含行都没有调整时不带这两个字段。`last_week` 另给 `closed_week`：已结时为结周时刻 `closed_at`、水位 `watermark` 与结周时未读的 peer `gaps`，尚未结（周一结周前）为 `null`；发布物不结周时结果里没有这个键。exporter 用 `--probe-cost-adjustments` 声明支持，发布器探测到才带这些字段。`wi` 的 `project_page` 取 WI 展示数据中的同名字段，发布物未提供时为 `null`。子 WI 的行带 `wi_parent`；`cost` 另给 `work_item_families`（父 WI 本身加子 WI 的金额与 PR 合计，只作展示，不计入 `summary`），`wi` 给 `parent`、`children` 与每期 `family_total`（仅父 WI 有）。

## 开发、验证与维护

### 独立查看已导出的快照

候选 App 可用 `PulseStats.app/Contents/MacOS/PulseStats --inspect-snapshot /绝对路径/snapshot.json`
打开经受支持通道取得的原始发布物。启动后直接显示独立“PULSE · 本地快照”窗口，标明文件名；
使用同一解码器、420 点面板和明细窗口，Refresh 重读该文件。文件必须属于当前用户、为私有普通文件、
不超过 1 MiB；不接受符号链接。路径错误、损坏或过期沿原错误/新鲜度规则显示，绝不回落到生产 exporter。
该入口使用内存缓存，无历史写入、无生产表格偏好写入；关闭本地快照窗口即退出查看进程，退出后不保留快照。它不安装 App、不替换正常菜单进程或生产缓存。

只有成本发布物的节点，可通过 exporter 的 `--json --cost-dashboard-only --cost-dashboard /绝对路径/cost.json`
生成可供 App 读取的快照。此模式仅校验已有查询，保留原查询的时间、水位、源码和摘要；
不采集、不计价、不连接生产节点、不推断平台健康或 WI 权威。平台与 WI 仍为 unknown，过期成本不会因为重新导出而变新。
真实节点数据的独立查看可以证明原生消费者呈现，不能代替正常生产传输、全平台状态或 WI 生命周期验收。

从[Swift测试](PulseStatsTests/)与[exporter测试](exporter/tests/)定位回归，通过[Stats macOS CI](../.github/workflows/stats-macos-ci.yml)、[Linux集成](../.github/workflows/stats-platform-ci.yml)及[适用UAT](../.github/workflows/uat.yml)绑定精确提交。原生验证须覆盖420点整面板、明暗菜单、两行事项、折叠与左对齐，以及实际滚动/调宽/筛选/刷新，不以宽窗静态图代替。
`outcome_readback=true`选择真实登记Outcome回放及对应Swift集成测试；`non_wi_readback=true`核对确证的非WI修正。默认PR测试不读生产私有源；合成图、私有副本回放、真实发布和人类体验分别记录。
发布从已审main经Actions→ownermac，提供精确 `approved_revision` 和 `expected_active_revision`，先安装全部读取器再启动发布器。`approved_revision` 须是发起时 main tip 的祖先（300个提交内），且两者之间每个提交及净差异只触及 `.harness/`（生产账本自动提交）；含其他路径改动即拒绝，构建与安装的是 `approved_revision` 的树。 `expected_active_revision`约束本机发布器，取发起前 `~/.local/state/pulse/stats/refresh.json` 的 `source_revision`（原生部署会改写该值）；安装等待当前发布周期结束、部署后触发发布的读回各最多40分钟（实测一轮15–25分钟），等待期间每分钟在stderr/日志报告进度，不中止正在运行的周期；部署分别采样每个peer的旧版本，验证目标与回退读取能力，并在停发布器前再次核对所有节点。peer切换保留本次build尝试与current链接身份；回退只恢复该尝试拥有的切换，版本或链接已被另一事务改动时保留现场并记录未解决回滚。失败回执原字节保留；旧回执仅在部署前备份与实际节点一致时允许继续。只读 `preflight --revision <SHA>` 检查已暂存版本，可加 `--expected <SHA>` 核对指定旧版本。部署作业触发一次发布并重启App，核对原查询/发布物/App的三个activity视图一致；自然调度周期由每小时一次的自然验收作业单独判定（不等待、不重启App，不能用触发的发布代替）。完整验收含两者；出现来源故障时还须单独确认恢复，不能把查询一致当来源完整。
原始账本、未确认凭据、失败前像和回退版本不可当缓存删除。回退、停用、运行包回收沿[平台入口](../README.md#stats)与原安装器，不影响Relay/SCL；不手改current、旧时间或授权使验收变绿。

部署器按[制品保留契约](../specs/stats-release-retention.md)保存原采用回执与不可变索引，区分本地发布器和完整 App/peer 部署。`plan-retention --expected <current SHA>` 只读列出声明的保护引用；未知历史制品继续保留，采用不等于业务验收、到期或删除授权。本阶段无 apply 入口，计划不触发刷新、不补写历史验收，也不把占用当可释放量。

## 已证范围

当前归属修复[PR #635](https://github.com/bigbird-online/PULSE/pull/635)/[#638](https://github.com/bigbird-online/PULSE/pull/638)已通过自然双周期、重启及原14条身份核对；普通来源重采用不得丢失归属上下文，有效旧计量不能阻断同源精确记录。UI仍采用已获用户确认的#626，不以改标签代替真实关联。当前采用版本、证据和历史限制集中见[已证基线](../specs/stats-query-dashboard.md#已证基线与边界)，不在多份README复制实时状态。
本轮显示、暂停态和登记来源同步可收尾；后续逐项启用cron仍要验真实交付。全入口/历史完整覆盖、全部Outcome库存、完整WI成本/质量/ROI、长期可靠性和整个WI终态仍按各自证据处理，不由这次体验反馈一并关闭。

CC 的持久 `modelUsage` 由唯一 ComputeCost 查询按原生会话解释，原始 Host 字段不改写；已知明细与未展开差额只计一次。差额跨日时保留时间缺口，不把累计数当今日用量。来源内容追加不再触发未变历史凭据的无效版本增长，详见 Stats 查询设计。
