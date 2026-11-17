---
feature_name: "pulse-compute-cost-management"
version: "0.8.5"
status: "in_progress"
created: "2026-08-17"
modified: "2026-10-07"
owner: "Pulse"
upstream_dependency:
  - "specs/pulse-compute-cost-requirements.md"
  - "specs/pulse-runtime-priority-management.md"
tags: ["compute-cost", "design", "control-loop"]
---

# PULSE 算力成本管理 — Design


## 现行设计：凭据查询与生产 Stats（2026-09-19）

对应[现行需求](pulse-compute-cost-requirements.md)；本节整合截至 #626 已采用的用量、身份、价表、缓存和发布设计，不再按每次修复叠加候选说明。已记录安装基线、生产证据及边界统一维护在[仪表盘设计](stats-query-dashboard.md#已证基线与边界)，具体身份规则见[归属设计](stats-attribution-identity.md)。整个 ComputeCost/WI568 仍不因本切片已发布而自动 closeout。

### 一条事实链，分别管理写入与消费

```text
已登记原生 Pi / Codex / CC + Relay Host + Harness Outcome/评审/计量
                  ↓ 校验、稳定身份、原生计数与边界
各节点 UsageLedger 协作凭据（追加修正，保留旧水位）
                  ↓ 已收前缀证明与增量凭据传输
既有 ownermac ComputeCost ledger + 原价表快照
                  ↓ 同水位核销/归属 → 期间/分组 → 原标准计价
computecost_collaboration_query/v1
                  ↓ ComputeCost stats_dashboard 物化 21 个视图
pulse_stats_cost_dashboard/v1 → ownermac 本机固定 exporter → 正式 App/cache
```

图示是已采用成本路径，不代表 CC 全入口已自然验收、所有来源均完整或每次调用都有质量/收益。运行态、业务生命周期和采购账单仍是独立事实；不新建 ledger、身份服务或计价器。

### Outcome 到 Stats 的现行消费路径

沿用现有 delivery、review、coverage 的修订与时间规则，在原协作查询内提供独立 Outcome 结果与安全详情。主表不得使用 Token 或 quality.review_activity_count 作为产出；重复评审不产生新 Outcome，跨运行组合不分摊或复制成果总数。成果来源缺失与确证无结果不同，只有上游已提供的状态、产物、验收/评审引用可进入详情，不推断新授权/接受。

Stats 发布器在同一个冻结水位携带成果总表、行内引用及无当期用量的成果行，保留原成本/用量视图与金额；不从多个独立分组作笛卡尔连接。行级用量/归属引用可有界展示，但保留完整查询的报告摘要和总引用数。旧不含扩展的发布物仍能显示原成本，新成果区明确未提供。

`canonical_outcomes.py` 只枚举已登记根下规范的 WI Outcome 文件，校验 `verify-work-outcome/v2` 原身份、状态、时间和声明结构，沿原delivery域保存有界脱敏的名称/结论；原文件摘要留在独立evidence。读取既有结论不是重新验收，附属Markdown、嵌套副本和task级回执不增加WI成果数。
`kernel_outcomes.py` 读取已登记 `harness_core` 来源，即新内核 `stats-facts` 导出（`harness.stats_facts/v1`）中各 WI 的 `completions`。每项是一次 `WorkCompleted`：内核按完成当时的状态给出完成修订号、时间、执行者、标题，以及被接受记录或候选、产物、验收决定的摘要；路径、正文和会话不出内核。同一导出的 `session_writes` 列出原生会话对该 WI 的每次已提交写命令：执行者、提交时间和会话键 `digest(["stats_session_write", native_session_ref])`。会话键是对内核已有会话摘要再做一次带域单向摘要，只有持有原生会话 ID 的读取端能重算比对；原生 ID、surface、内核主体引用、正文与路径都不导出，交付服务等非会话提交不算写入。ComputeCost 将其映射为 `accepted` delivery（`harness_core_outcome.v1`），身份取（命名空间、WI、完成修订号），两节点读同一导出得到相同事实；同一 WI 重开后再次完成算另一项成果。覆盖分母是导出中的 WI 数：全部可读且带完成列表才为 complete；旧内核导出没有该列表时为 unknown，不当作确证零。
`outcome_projection.py` 按原发生/接受时间与稳定对象统计，保留状态和已知范围，并带出 `verification_level`（旧发布物无此字段）；内核 `agent_review`（父 WI 持有会话经 `review_child` 关闭的子 WI）映射为 `self_with_controls`，与 `accepted` 并存，Stats 显示“主会话审核通过（非人类验收）”，不当作人类或独立验收；仅评审unknown占位不计成果。同WI的canonical结论追加替代unknown占位不转移其旧approval；已知Outcome冲突仍须明确修订，历史水位不改写。显示数量不是质量通过率，更不是货币收益。
现行界面已采用该扩展，安装与验证见[Stats基线](stats-query-dashboard.md#已证基线与边界)。Outcome来源完整性、完整WI质量和ROI仍分别检查，不能把一批可读成果称为所有成果已覆盖。

### 内核合并 PR 产出与异常

内核 `stats-facts` 每个 WI 带 `merged_pull_requests`：该 WI 上登记、合并效果观测为成功（内核自己合并并读回）的 PR，含仓库、号、被审提交、合并提交、GitHub 合并时间、内核读回时间、合并轮审核分数、审核轮数与放行方式（审核或人类确认）；不含会话标识。GitHub 上绕过内核合并的 PR 在内核里是失败观测，不出现在列表中。
`pull_request_outputs.py` 读同一份内核导出，按 WI 与北京时间期间统计；不进 receipt 账本，发布器在缓存的查询视图之后附加 `pull_request_outputs` 与 `pull_request_only_groups`，所以 PR 记录变化不依赖凭据水位。WI 金额取该视图中同一 WI 各行完整或已计价金额之和，`cost_complete` 标明是否完整。
`pull_request_audit.py` 经 `Scripts/pulse-gh.py` 的只读 `session` 读三仓库近 7 天：`pulls?state=closed&base=main` 的已合并 PR，以及 `commits?sha=main&since=` 沿 first-parent 的单父提交；跳过 PULSE `SCL:` 账本提交，标题 `(#N)` 或 `commits/{sha}/pulls` 关联到已合并 PR 的提交归回 PR。结果写 `~/.local/state/pulse/stats/pr-audit-v1.json`（私有），`Scripts/pulse-stats-cost-refresh.py` 在采集期间用独立线程读取、最多 3 小时一次、单次总时长不超过 5 分钟（超时的仓库未核验），也可手动 `pr-audit`；发布器用 `--pull-request-audit` 读缓存，与内核合并记录对齐成文档级 `pull_request_anomalies`。没有内核记录的 GitHub 合并按 `--kernel-merge-enforced-since`（刷新配置 `kernel_merge_enforced_since`，默认不设）分开：生效日（北京时间）及以后进 `unrecorded_merges` 算异常；未设或更早的进 `unenforced_merges` 只作参考；时间未知的按已强制处理。生效日格式无效时整体未核验（`enforcement_date_invalid`）。直接提交 main 不受生效日影响。仓库、分页或关联读不到时该部分未核验；缓存超过 12 小时整体未核验；内核合并记录未读全时 (a) 未核验。未核验计数为 null，不是 0。

`pull_request_bodies.py` 为 Stats 的 `agent_day` 查询采集 PR 正文：同一 GitHub 线程在审计之后、经同一只读 `session`，最多 3 小时一次，过北京时间 0 点后的首次刷新必读（前一天的 `agent_day` 随之齐全；也可手动 `pr-bodies`），按更新时间倒序读三仓库 `pulls?state=all&base=main`，回到上次读取前 1 小时（首次 120 天，最多 30 页），每个 PR 存编号、状态、开启／更新／关闭／合并时刻、标题（300 字）、正文（8000 字，超出标 `body_truncated`）、作者与分支名，合并进 `~/.local/state/pulse/stats/pr-bodies-v1.json`（私有，`pulse_stats_pull_request_bodies/v1`），历史随读取累积、保留 400 天。每个仓库记 `observed_at` 与 `covered_since`：从后者起到前者，有过任何更新的 PR 都在缓存里；与上次读取衔接时沿用上次的起点，超页数时只覆盖到读到的最早更新。读不到的仓库保留原条目与覆盖范围并标 `unverified`；没有任何仓库读到时不写文件。
已合并 PR 列表另向前读到本月与本周起点中较早者（缓存 `pulls_since`），每条带分支名 `head_ref`；(a) 仍只取近 7 天。`pull_request_anomalies.unregistered_pull_requests` 按发布期间（today/week/month，北京时间半开区间，带 `from_date`/`to_date`）列出该期间合并、没有任何 WI 内核合并记录的 PR：`count`、按分支前缀 `<agent>/`（须是团队登记表 Agent）提示的 `agents` 计数、`unhinted_count` 与至多 20 条 `items`。缓存过期、任一仓库 PR 列表或内核合并记录未读全、`pulls_since` 晚于期间起点时该期间未核验，计数为 null。它只作提示，不进 `pull_request_outputs`、每 PR 成本或 Agent 归属；GitHub 读取频率不变。
子 WI（内核导出带 `parent`）：PR 仍按登记所在 WI 计，子 WI 行照常单独成行；`work_items` 中子 WI 带 `parent`，并补入其父 WI 的展示数据。有子 WI 出现在视图中时，发布器附加 `work_item_families`：每个父 WI 一项，`cost_usd`／`cost_complete` 为父 WI 与这些子 WI 的金额按上述同一规则之和，`pull_request_count` 为其合并 PR 之和（PR 记录不可用或任一成员不可读时为 null）。它只作展示，父 WI 自身行与视图合计都不变，不重复相加。
`Stats/exporter/pulse_stats/pull_request_view.py` 校验以上字段：计数守恒、PR 落在视图期间、未核验不为 0、仅合并行不虚构用量；`work_item_families` 的子 WI 须在 `work_items` 中以该父 WI 为 `parent` 且出现在本视图行中，金额与 PR 数须能由本视图行与 PR 产出重算一致。

### 核心对象与接线

`token_semantics.InputTokenClasses` 负责 inclusive/disjoint input 的换算和缓存分区守恒，缺失分区保持未知；需要确定总输入或非缓存输入的消费者须显式要求该值。原生解析器保留来源字段、时间与身份检查，CC 运行样本、Codex 原生计量点、SCL 封存连接、WI 计价及报告调用同一换算边界，删除各自加减缓存的决策。各公开入口保留原领域错误和失败语义，不以换算模块修改账本、累计水位或价格。
`@anchor test ComputeCost/tests/test_native_metering.py :: NativeMeteringTests.test_missing_counter_negative_delta_and_excess_cache_remain_unknown`
`@anchor test ComputeCost/tests/test_wi_pricing.py :: WiPricingTests.test_excess_cache_preserves_public_pricing_error`

| 位置 | 责任与不变量 |
| --- | --- |
| `receipts.py` / `ledger.py` | 各域封闭 payload、原生与互斥数量、来源摘要和修正链；原锁内写入、fsync、原子发布。原记录不可变，重放不新增消费，旧 as-of 可复现。 |
| `receipt_sources.py` / `adapters.py` | 读取明确登记的 native/Host/Harness/receipts，保留有效前缀和错误；Codex 沿用父子/归档选择，不能重新制造平行消费。 |
| `scl_session_attribution.py` | 从原生稳定 ID 计算对应 Host 引用，按活动 lineage 的 attachment/history、epoch 和有效区间归属；只读现有状态，不初始化或迁移 kernel。 |
| `pi_context.py` / `native_inventory.py` / `pi_history.py` | 读取精确输入来源与原生模型状态；按 session、区间、Token 守恒核销 Host 汇总及原生明细，按显式父历史核销复制。 |
| `activity_classification.py` | 确证的非 WI 分类与原有 attribution 共存；空 WI 本身不是“无”，分类不生成 WI 或执行权。Relay 派发未带 WI 的 Pi 轮次只作兜底（证据含 `HOST_FALLBACK`）：同一请求经持有补充或探索得到 WI 时以 WI 为准，否则记为无 WI。 |
| `standing_non_wi.py` | 人工长期决定（`standing-non-wi.json`：Agent、节点、运行时、入口及决定凭据）：命中的用量在没有任何声明、分配或分类时记为 `none`；已有任务主张的用量不改。 |
| `turn_residual_purpose.py` | CC 单模型会话计数（`turn_aggregate`，如轮内 Haiku）没有自己的请求记录：在同一原生会话、计数区间内的主模型请求全部由请求用途或人工核定归到同一 WI 时，计数归该 WI；任一请求未归属或不一致则不归。其他主任务主张优先，持有补充不算主张。 |
| `collaboration_query.py` | 修正/as-of → 兼容观察投影 → 任务与身份 → 全局用量核销 → 窗口/过滤 → 各自然粒度汇总；冲突保留全部证据，不用最后写入获胜。 |
| `catalog_source.py` / 原计价函数 | CLI 与发布器使用同一份价表选择和验证；金额绑定模型/四类 Token/价表，订阅及 provider cash 不覆盖标准金额。 |
| `stats_dashboard.py` | 冻结一个 receipt snapshot，生成日/周/月 × 六种独立分组及六维联合 activity 共 21 视图；原activity groups只输出 event_count > 0 的行；可选扩展携带Outcome总表、行内引用、有界detail、仅成果行及活跃Agent，以及内核合并 PR 产出、仅合并行和 PR 产出异常，原消费分母不变。 |
| `Scripts/pulse-stats-cost-refresh.py` / `Stats/exporter/pulse_stats/receipt_transport.py` / `Stats/deploy.py` | 原refresh登记采集、受界分帧传输、发布与确认；传输模块负责前缀/全档验证和原子安装；原安装器管理同SHA的 ownermac 读取端与未暂停的 peer（现为 Oracle）、来源计划、App及调度采用，并安装 Oracle 发布器、以 `observe-mode` 切换 ownermac 的完整采集与只观察（见[Stats 调度](stats-query-dashboard.md#安装调度与恢复)与第 12 条）。仅复制源码或构建成功不是发布。 |

`execution_expectation` 仍只表示计量 epoch 的 missing/sealed/unjoined 状态，不增加 Token，也不是完整 WI 执行库存。Harness 目录中的旧审核只证明对应候选，不证明全部历史评审或新的交付接受。

### 数量、业务去向与覆盖分离

原生累计差分必须有起点及类别证据；Codex 的 `total == last` 仅在原协议条件成立时证明新零起点。分页继承用被引用父段的基线，不按文件名猜；跨任务/跨日不可拆分时显式不确定。`reported_total_tokens` 与四类数量是同一消费的不同表达，不相加。

Pi/Codex启动工作区及CC原生assistant消息的cwd共用精确登记的Actor匹配，并置于解析缓存外每轮重读；CC与其它原生运行时由actor_candidate_registry生成相同含义的workspace_actors和kernel/surface规则。节点、Runtime或路径不符不猜身份；不从agent-homes目录名、前一条消息或WI owner推断Agent，Agent证据不能自动推出WI或入口。原始用量、价格凭据与旧水位不因新增Actor证据改写。

`registered_runtime_actor.v1` actor 凭据的实体只由稳定字段确定：目标、生产路径（含来源 ID）、节点、入口、工作区、输入、子调用与租户（ID 和登记引用）；执行者、生效时间、证据和租户所证的用量版本在 payload 中。采集在本来源版本判定之后才结算 actor：生效时间取目标当前版本（账本头或本轮已接受的修订），不取本轮重新解析的候选；同一结算列表里同一实体只取最后一条。实体已有头时只与本实体的头比较：payload 字节与 `occurred_at` 都相同则不追加，否则追加 supersedes 修订并合并证据；其它实体（另一来源或路径）的同内容头不能代替本实体的修订。实体尚无头时，只有旧身份公式（目标、执行者、上下文、生效时间、证据）为这条凭据算出的实体已有内容相同的当前头才不追加，那是旧生产者写下的同一主张；其它实体的同内容头（稳定公式的另一来源或路径，或证据不同的旧头）证明不了同源，本实体照常新建，各自保留独立的头。租户成员只指向采集开始时账本已有的用量版本，本轮新增的版本由下一轮证明。锚定具体用量版本的 `usage_fact_ref` 凭据（verification workspace）仍把该版本放在身份里，不参与此结算。

Actor attribution 和 task attribution 分开投影。`cron`/`none` 是有证据的非 WI 业务分类，原始 `wi=null` 不改写；UI 将 `none` 显示为“无”。内部入口 `automation` 显示为“无”，并不把未知入口也转换为无。真实 WI、互相冲突的任务或中途切换仍按原有效边界处理。

`host_usage_coverage()` 保留期望数、实收数及 partial：只有未回报执行均为 accepted/running、起点已知且无相关捕获缺口才附 `host_usage_pending`。更早的历史 gap 保留并继续限制完整成本；终态/unknown、无时间或相关 gap 不能用在途解释。UI 与 UAT 使用同一读取故障/在途分类，不能只改错误码让检查变绿。

### 增量解析、计价与报表

缓存只存可丢弃的私有 JSON 衍生值，不取代原账本校验、来源权威或捕获水位。对应实现为 `calculation_cache.py` 和原生解析缓存；用户要求默认复用，显式强制才绕过。

| 复用层 | 复用条件 | 变化时的处理 |
| --- | --- | --- |
| 原生解析 | 内容及解释依赖摘要相同；Pi/Host/CC 按文件、Codex 按既有父子/归档选择域 | 文件追加/同大小改写、路径迁移、输入证据或代码变化重新解析对应域；不是任意日志逐字节尾部解析。SCL/Host 每轮重读。 |
| 单次查询准备 | 冻结事实集、水位及摘要相同 | 同批准备可供多个视图复用；返回报表容器独立，不改变 Receipt 副本语义。 |
| 标准计价 | 模型、四类数量、价表与引擎一致 | 新数量新增计价；补 WI/入口仅更新归属，不重算旧金额。零数量规则保持，改价/模型/数量失效对应键。 |
| 发布视图 | 完整事实摘要、水位、入账时间、查询范围/分组/过滤、价表/观察时间及计算代码一致 | 新水位或时间窗变更重新构建需要的视图；不宣称每轮都 0 查询。代码升级失效相关缓存。 |

同一发布周期由 `CollaborationQuerySession` 持有固定事实快照、一次证据准备和可复用计价状态。`stats_dashboard` 只选择期间与分组，不给账本注入缓存字段。会话拒绝跨水位查询或另加 `as_of`；新快照必须新建会话。各次报表、输入计价缓存和导出的计价缓存独立持有可变数据，调用方修改不得污染后续查询。跨发布复用仍由上表的事实、水位、窗口、价表和引擎摘要合同裁定，不产生第二账本。对应回归为 `ComputeCost/tests/test_publication_preparation.py`。

计算缓存有权限/类型/大小/摘要校验与原子替换；损坏、缺失或不安全按 miss 回原计算。`refresh --force-recompute` 贯通远端导出、原生解析及本地计价/报表。不改原数量、旧价表时间或旧水位；来源错误不能由旧缓存覆盖。

### 传输、安装与恢复

当前原refresh使用 `export-framed-gzip` / `pulse_stats_receipt_frames/v2`，编码、游标及安装在 `Stats/exporter/pulse_stats/receipt_transport.py`；不改变原收集器、source ID、Receipt语义或账本。旧 `export` / `export-gzip` / `export-delta-gzip` 仅保留原有兼容合同与大小限制，不是现行增量发布路径。

1. 既有私有 `imports/*.jsonl` 或 `.last-good` 的长度/SHA256证明已收前缀，源变化期间拒绝不稳定游标；不另建游标权威。发送端校验前缀对应记录边界，相同则传后缀，不同则完整重建。
2. `PULSE-RECEIPTS/2`头包含原始完整长度/摘要和前缀信息，后接独立gzip帧。每帧展开上限4MiB，单条Receipt不能跨越此限制；线传输总上限64MiB，拒绝截断、额外成员、非法schema、异常尺寸及非规范Receipt。
3. 接收方在私有目录逐帧校验并写暂存，同时验证既有前缀、最终长度和SHA256；完整展开档须小于实际可用磁盘减512MiB保留量。只有全部通过才原子替换live输入；失败移除未完成暂存，不改原账本或历史事实。
4. 累计展开历史不再被旧64MiB整档上限永久卡住；但单帧、线传输、磁盘和运行预算仍有限制，不承诺任意大小或任意网络都成功。导出采集/传输预算360秒，其余SSH命令180秒，最终成本发布物最多512KiB，不靠延长timeout恢复服务。
5. 断连/无效输入使该来源明确失败，已入账事实仍在，last-good不能冒充本轮成功。下一次原调度可按原前缀恢复；来源恢复后原collector将历史transport失败标为excluded，完整覆盖交还原生事实，不造complete业务分母，也不取消真实历史gap。
6. 原 refresh 锁内，全部登记 peer 本轮读取成功、已配置的目的端接收成功（未配置时只写本机）且 last-refresh 已落盘后，只退休与 current 完整字节一致的 last-good 传输副本。精确 peer 路径、私有属主、普通单链接文件、同卷与稳定身份、逐块比较、lsof 前后及私有 staging 全部通过才删除，并保留持久 intent/结果；当前输入、原始账本、不同内容与未知/活引用均保留。失败 stage 阻止该 peer 后续自动退休，清理结果与已成功的业务发布分别记录。下一次断线仍由 current 生成 last-good，不新增调度或游标权威。
7. `shared_ledger` peer 由 `collect-shared`（peer 在本机把来源追加到共享观测流，只回传位置）采集。peer 侧入口先对登记表目录内私有 `collect-shared.lock`（0600、O_NOFOLLOW）做非阻塞 flock，拿不到即以 75 退出、不读账本；ownermac 记 `peer_busy`（不记 `peer_read_failed`），状态与覆盖仍按未读 peer 处理。持锁后在 Linux 设 RLIMIT_DATA（堆、匿名与线程栈映射的上限）为本机内存的 60%，不低于 3.25 GiB、不高于 7 GiB（TCloud 3.25 GiB，Oracle 约 7 GiB），内存耗尽以 71 退出；截止为 SSH 预算减 60 秒（840 秒），到期以 124 退出，另有晚 30 秒的内核 SIGALRM 兜底；stdout 读端关闭或父进程消失时以 74 退出。锁随进程退出释放，不加 SSH prefix，不改其它模式。
   按决定暂停的 peer 仍登记在 `peers`，但发布器不连它：不发起 `collect-shared`、接收端导出或 WI 事实推送，派生登记表不列它的传输来源，覆盖因此不声称含该节点。`refresh.json` 的 `paused_peers` 把 peer ID 映射到原因（现仅 `paused_by_decision`）；缺省为 `{"tcloud": "paused_by_decision"}`（2026-11-15 人类决定：缺省只由 Oracle 跑 Stats 计算），`{}` 恢复全部。周期状态（`running`、`completed`、`failed`）与 `last-refresh.json` 把它记为 `paused` 并附原因，不记 `peer_read_failed` 或未读；它的已入账事实不动。
`@anchor test Stats/exporter/tests/test_cost_refresh.py :: CostRefreshTests.test_paused_peer_is_not_reached_covered_or_failed`
8. `shared_ledger_push` peer 不由发布器经 SSH 采集（Oracle 发布器之下的 ownermac；内存只够只观察的 TCloud，见第 11 条）。它自己按周期跑 `collect-local`：持同一 `collect-shared.lock`（拿不到记 `peer_busy`，本轮不采集）做同一采集，45 分钟未完由 SIGALRM 结束，以 `receive-shared-collection --peer <id>` 把本轮结果推给发布器：完整采集的位置回执、只观察采集已 spool 的包数（`pulse_stats_observe_push/v1`，包数须等于已推数），或采集失败及其原因（`pulse_stats_collection_failed/v1`，原因为封闭小写词，内存耗尽记 `out_of_memory`；推送失败只记本机状态）。发布器只接受 refresh 配置中登记为推送型的 peer、且位置回执的观测流身份与本机一致，最新一次推送覆盖上一次，写入私有 `imports/<id>.shared-receipt.json`（`pulse_stats_shared_push/v1`，带接收时刻）。发布器本轮不连该 peer，在发布前读回执并更新覆盖标记，同一发布物的覆盖与 peer 报告一致：30 分钟内收到的成功结果、且没有被此前已完成的发布计过，记 `read`；否则未读，原因 `peer_push_missing`、`peer_push_stale`（超时，或此后该 peer 没有新一轮结果）、`peer_collection_failed`、`peer_push_mode_mismatch`（结果与发布器登记的模式不符：登记了 `observe_sources`（第 11 条）的 peer 推来完整采集的位置，或未登记的推来只观察结果；前者意味着两处写同一来源，后者的包无人判定）或 `peer_receipt_invalid`。计过的结果随本轮 `last-refresh.json` 记入 `imports/<id>.push-counted.json`；发布失败不计。采集失败或只剩上一轮结果时节点因此不再报 `read`。覆盖标记沿用第 7 条。持有 OpenRouter 密钥的推送型 peer 只推脱敏活动摘要（`receive-openrouter-snapshot`，形如 `--openrouter-snapshot` 输入），密钥不出该节点。发布物经 `fetch-publication` 打包、由读取端 `pull-publication` 拉回，dashboard 与 last-week 走与 `receive` 相同的读取端校验、不回退旧版、无金额不覆盖有金额，周期状态原样保存。
`@anchor test Stats/exporter/tests/test_cost_refresh_push.py :: PushedPeerTests.test_missing_stale_or_damaged_push_is_an_unread_peer`
`@anchor test Stats/exporter/tests/test_cost_refresh_push.py :: PublicationPullTests.test_reader_installs_newer_publications_and_the_cycle_status`
`@anchor test Stats/exporter/tests/test_tcloud_observe_push.py :: PushResultTests.test_a_push_counts_for_one_completed_publication_only`
`@anchor test Stats/exporter/tests/test_tcloud_observe_push.py :: PushResultTests.test_a_pushed_failure_replaces_the_previous_success`
`@anchor test Stats/exporter/tests/test_tcloud_observe_push.py :: PushResultTests.test_a_result_counts_only_under_the_mode_the_publisher_registered`
9. 观察包（`native_observations.py`，`pulse_native_observations/v1`）：只观察的推送型 peer 按来源送出读取器产出、尚未版本判定的行，在该来源全部文件上测得的覆盖（状态、原因、分母、水位、来源明细、诊断、原始条数），以及原生输入回执；来源配置只带判定所需字段（ID、种类、节点、标签、域、WI 范围、actor 开关、主体别名历史、验证工作区标记），路径与私有输入不出节点。发布器以 `receive-native-observations --peer <id>` 接收：只收 refresh 配置登记为推送型的 peer，整包校验后按序号写入私有 spool `imports/<id>.observations/`；同序号同字节为重复，不同字节拒收；已入账的包在 spool 的 `.consumed.json` 记下序号与摘要（保留最近 1024 个），重推同字节仍答重复、不再读取，不同字节或不高于已入账最大序号的包一律拒收，迟到的旧包不能覆盖较新的来源状态。接收、读取清单与登记已入账都在 spool 的同一把排他锁内进行，新包以不覆盖的硬链接落位，同序号的并发接收只有一份内容成功。判定端登记表的 `native_push` 来源（`peer`、spool `path`、允许的 `source_ids`）在采集时把 spool 中的包按序号展开为原来源：行依次拼接，覆盖与配置取最后一包；走与本机来源相同的逐行版本判定、actor 结算、覆盖版本、主体别名纠正（范围含带该来源证据的当前头）与归属阶段；包内输入经快照版本判定后随本轮入流，并与本轮已判定的用量头一起参与本轮的启动、请求用途、根链接与持有补充判定，不等下一轮。来源身份沿用观察节点的 `reference("source", id)`，覆盖实体在切换前后连续。入账成功后才删除已读的包；失败或换水位重采时下一轮再读。已入账记录与未解决的包分开跟踪：包不通过校验、含未登记的来源或序号不高于已入账序号时留在 spool、不读取，此后每轮 `native_push` 摘要都记 `observation_bundle_invalid`，直到被处理；更高序号的有效包照常读取。快照输入的读取水位存于判定端 `native-cache/input-read-marks.json`，只随读取它的那次入账保存。未登记 `native_push` 时采集不变。
`@anchor test ComputeCost/tests/test_native_observations.py :: ObservationBundleTests.test_judging_collector_reaches_the_observing_nodes_usage_and_coverage`
10. 只观察模式：推送型 peer 的 refresh 配置 `collect_local_mode` 为 `observe`（缺省 `full`，即第 8 条）时，`collect-local` 不读也不写观测流（`native_observe.py`）。小来源（Relay Host、Pi、Codex、回执包、Harness）每轮经解析缓存全读，不可变行只送出已确认的包尚未送过的；快照输入只要与该 key 最近确认的值不同就送出（A、B、A 时第三次照送）；CC 按原生会话（主转写及其子代理）只读需要重读的会话，一次一个，整会话送出：文件签名有变化，或读取依赖有变化（解析引擎、来源配置、工作区与租户登记、持有补充的执行者集合、该会话的 Host 事实、其转写引用的 Host 输入元数据文件状态、转写同级 `compute-cost/` 中属于该会话授权的有界快照及其 baseline 的内容摘要、文件类型、属主与权限位），此前因依赖缺失而不完整的会话因此在依赖补齐后重读，快照晚于转写落盘、更新或恢复时同样重读。读取失败（来源根不可读或不安全、读取抛错、文件签名或引用扫描读不到）的会话只让该会话失败，其余会话与来源照常，失败会话保留失败状态，按完整采集同样的规则聚合为来源的 unreachable 或 partial，并在下一轮重读，直到一次成功读取；来源根本身不可读时整源报告不可达、不读取也不前移索引。Host 授权与执行者取本轮先读到的 Relay 行；别名纠正、actor 结算、历史版本判定与各归属阶段都留给判定端。每个来源的覆盖由私有索引在全部文件上聚合（文件数、无效数、错误、最近用量时间、诊断、水位）。包按行切分，每包规范字节不超过 32 MiB（含配置、覆盖、inventory 与 appended），写入私有发件箱；会话的索引记录随载有其最后一行的包，全部包确认后才前移；经 `receive-native-observations` 推给发布器；发布器 spool 成功即确认，确认后才更新索引；本轮未确认或被拒的包丢弃，其会话下轮重读。仍持同一 `collect-shared.lock`。`collect-local --observe-backfill`（仅 observe 模式）逐会话重读全部 CC 会话一次，用于切换当天补录历史输入。实测入口 `Scripts/pulse-stats-observe-measure.sh`（backfill 上界与普通一轮，记 macOS 峰值 footprint）。
`@anchor test ComputeCost/tests/test_native_observe.py :: ObserveTests.test_three_rounds_match_a_full_collection`
11. 只观察的推送型 peer 在发布器 refresh 配置里带 `observe_sources`（该节点自己登记表中、其包可携带的来源 ID）；发布器每轮为它登记一个 `native_push` 来源（`native-push-<id>`，spool 为 `imports/<id>.observations/`），按第 9 条判定。暂停的 peer 不登记。TCloud（3.7 GiB）只做这一件事：`observe-push` 要求 `collect_local_mode` 为 `observe`，按 15 分钟间隔读自己的来源、推包与本轮结果，不读不写观测流，不读额度、GitHub、OpenRouter，也不拉发布物；持 `collect-shared.lock`（拿不到本轮不跑）、数据段上限同第 7 条、45 分钟截止，失败同样推送。部署允许 TCloud 以推送型登记（须带 `observe_sources`，保留 host、python、script，部署照常把读取包换到所选版本）。首次推送时本机索引为空，小来源的全部行都会送出，内容寻址去重，所以这一次就补齐了采集中断期间的用量。
`@anchor test Stats/exporter/tests/test_tcloud_observe_push.py :: PushResultTests.test_an_observing_pusher_is_judged_from_its_spool`
`@anchor test Stats/exporter/tests/test_tcloud_observe_push.py :: ObservePushTests.test_observe_push_sends_bundles_then_the_result_and_nothing_else`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: OraclePublisherConfigTests.test_tcloud_may_push_observations_and_keeps_its_reader`
   `peer-collection` 只改 ownermac 本机的 `refresh.json`；Oracle 发布器按自己持有的暂停表采集，这张表只由 `install-oracle-publisher` 复制过去，`native_push` 来源也只为未暂停的观察节点登记。`install-oracle-publisher` 因此读回 Oracle 收下的 `paused_peers` 与观察节点清单，与本机不一致即失败。TCloud 切到只观察推送，按以下顺序做，前提是 Oracle 已是发布器、TCloud 的 pulse 账户能免密 SSH 到 `pulse@oracle-node-1`：
   1. 常规原生部署，发布器先于推送端换到新版本（旧发布器拒收新的结果格式）。
   2. 在 ownermac 的 `refresh.json` 中把 tcloud 改为推送型并带 `observe_sources`（取 TCloud `sources.json` 的来源 ID），再执行 `peer-collection --peer tcloud --collection active`。
   3. 执行 `install-oracle-publisher`，读回其输出的 `paused_peers` 不含 tcloud、`observing_peers` 为 `["tcloud"]`；Oracle 下一轮的 `collector-sources.json` 含 `native-push-tcloud`。
   4. 在 TCloud 写只观察配置（`peer_id` tcloud、`publisher` oracle-node-1、`collect_local_mode` observe、`peers` 只有 Oracle），装每 15 分钟一次的 `observe-push`。
   5. 补录：避开 Oracle 刷新，在 TCloud 手动跑一次 `observe-push`；索引为空，全部行送出，Oracle 去重后只补中断期间漏计的用量。记峰值内存，须低于第 7 条的数据段上限。
   6. 读回：Oracle `last-refresh.json` 中 tcloud 为 `read`；查询中 TCloud 各来源的最近用量时间跟上 TCloud 会话；漏计期的用量条数与 TCloud 会话计数一致。
   回退：先停 TCloud 的 `observe-push`，再在 ownermac 执行 `peer-collection --peer tcloud --collection paused`，然后 `install-oracle-publisher`，读回 `paused_peers` 含 tcloud、`observing_peers` 为空；已入流的事实保留。
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: PausedPeerTests.test_install_hands_oracle_the_pause_of_this_publisher`
12. ownermac 在 Oracle 发布器之下同样可以只观察，切换只走 `Stats/deploy.py observe-mode --peer ownermac --mode observe|full`（用法、锁与失败恢复见[Stats 调度](stats-query-dashboard.md#安装调度与恢复)）。ownermac `refresh.json` 的 `collect_local_mode` 为 `observe` 时，`install-oracle-publisher` 把 ownermac 登记表（`sources.json`）的全部来源 ID 作为 `observe_sources` 交给 Oracle，Oracle 的 ownermac 条目带上它，发布器每轮为它登记 `native-push-ownermac`，按第 9 条判定；读回的观察节点清单与来源 ID 须与本机一致。ownermac 的来源始终只有一个写入方：切到 observe 时 Oracle 先登记 `native_push`，ownermac 再停止自己追加；切回 full 时先停 Oracle 的 `native_push`，再恢复 ownermac 完整采集。切换在两端改动前写下回执 `observe-mode.json`，两端读回同一端之前它未了结，其间 `collect-local` 不采集、只推采集失败 `observe_mode_unresolved`，部署与发布器写入方都拒绝，直到再次执行 `observe-mode` 落定两端；`collect-local` 持周期锁后才读模式。首轮只观察结果到达后 Oracle 记 ownermac `read`，此后按第 8 条（失败或只剩旧结果不记 `read`）。只观察期间 ownermac 新登记来源后要再执行一次 `observe-mode --mode observe`，Oracle 才收含该来源的包。已入流的事实与 spool 中未入账的包在切回 full 时保留。
`@anchor test Stats/exporter/tests/test_tcloud_observe_push.py :: PushResultTests.test_ownermac_observing_is_read_from_its_first_push_and_judged_from_its_spool`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: OraclePublisherConfigTests.test_ownermac_observing_registration_reaches_oracle_and_survives_reader_switches`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_observe_registers_oracle_first_and_full_ends_it_first`
`@anchor test Stats/exporter/tests/test_stats_deploy_oracle_publisher.py :: ObserveModeTests.test_a_lost_reply_and_an_unreachable_restore_fence_until_a_rerun_settles_both`
`@anchor test Stats/exporter/tests/test_collect_local_observe.py :: ObserveModeTests.test_an_unresolved_observe_mode_switch_collects_nothing`

新旧传输读取端必须由原安装器配对更新，先 ownermac 与未暂停 peer 的 reader 再 publisher；原 `fact_ref`、`supersedes` 和历史水位连续保留。传输成功、原查询/发布/App一致、来源核验与完整WI执行库存分别验真。实际越界/断连/自然恢复证据见[统一基线](stats-query-dashboard.md#已证基线与边界)。

来源准备按 App Build（Actions run/attempt）保存 before/after/manifest；同尝试重复使用同证据，新尝试重新观察，旧计划不覆盖。CAS、读写兼容和原发布锁仍有效。先安装所有读取端、采用 PULSE 现行来源，再启动发布器；回退恢复对应尝试配置而不回滚新业务事实。

实际 launchd 使用 Standard，按[Stats 调度](stats-query-dashboard.md#安装调度与恢复)在发布物过期前启动周期，不沿用低优先级 Background，也不升为 Interactive。调度间隔不是打开面板等待时间或端到端实时承诺；同机器普通 runner 的性能不能代替实际服务测量。正式验收含部署时触发发布后的 App 重启读回，以及部署后自然调度周期的自然验收；自然验收不能用触发或手工补采的发布代替。

### Stats的只读消费边界

现行Stats四栏五行预览与可放大的完整八列明细共享同一发布数据和查询选择，不重计价、不拼独立分组。五卡片的折叠、文字层次和双行菜单由[呈现规范](stats-presentation.md)负责，不改变Receipt或查询结果。
CODEX本周百分比是独立账号额度；Oikos是资源条件；SCL负责人/事项年龄/请求与自动任务暂停也是独立事实，不成为Token、Outcome、执行成功或ROI依据。来源缺口必须点名并说明影响，微额/未知不因呈现美化变零。2026-09-19用户确认新版本基本符合预期，完成本轮呈现与已登记同步路径的体验收尾，不代表原R1–R4、S5/S6或全部历史能力完成。

### 兼容与未完成范围

旧 `usage_event.v1`、`computecost_wi_report/v4`、App Server epoch/封存和历史 S0–S6 合同保留原含义。新版协作路径已用于 Stats，不再回到订阅分摊 shadow；建议、受控 apply、全入口 inventory、共享成本分摊和质量/收益自动闭环不随本次上线宣告完成。

[2026-09-13 原生回放](../ComputeCost/uat/pr513-remediation-uat-20260913.md)仅是当时的有限证据；当前生产与历史样本分开记录。
`@anchor manual pr513-private-native-replay :: uat/pr513-native-replay-20260913.json; three bounded source snapshots; complete platform coverage not claimed`

## WI568 closeout integrity implementation — 2026-09-11

`computecost_openrouter_cash_valuation/v2` adds `usage_evidence_sha256`, calculated from the complete
source cohort without its self-digest. The CLI passes the original cohort through instead of reconstructing
an observed totals-only object. Report consumption verifies that source binding and independently checks
per-model Token classes. Retrospective valuation binds the candidate plus its exact request projection;
it never becomes a canonical WI total. Legacy unbound valuation v1 is not silently upgraded.

`computecost_wi_report/v4` adds `partial_machine_cash_usd` for a priced, nonconflicting incomplete subset.
Canonical `machine_cash_usd` requires exact usage; `cash_coverage` distinguishes `partial_usage` from
`join_conflict`. Ledger/discovery reconciliation compares model partitions as well as aggregate counters.
Missing models retain incurred quantities and a warning, not a fabricated model or total cash.

`pi_capture.capture_pi_session_snapshot` runs inside the native Python probe. It locks the persistent
mode-0600 `.capture-lock` inode with `fcntl.flock`, validates the prior immutable artifact, samples,
fsyncs a private same-directory temporary file, publishes atomically, and fsyncs the directory before
releasing the lock. The lock inode is never unlinked; its existence is not liveness. Process death releases
the kernel lock. Node independently verifies artifact, prefix, binding and canonical SCL readback.
The internal `pi_atomic_capture/v1` transport contains snapshot plus idempotent-replay status; the
existing public snapshot/receipt schemas and raw-identity privacy boundary stay unchanged.

Deployment must replace the paired Python producer and Node extension together after retiring old
in-flight writers; old mkdir-lock artifacts are not permission to overlap old and new capture code.
An old already-published valid final remains readable and immutable.


## 1. 架构结论

模块物理归属 `ComputeCost/`。协作凭据查询已作为 Stats 成本计算来源。下图保留早期 S0–S6 的建议/控制扩展目标；其晋级门不等于当前 Stats 仍是 shadow，也不代表控制推广已获授权：

```text
Relay / Codex / CC / OpenRouter / outcome receipts
                      │
                      ▼
            ComputeCost evidence plane
         usage events + price snapshots
                      │
                      ▼
       valuation → drivers → recommendations
           │          │              │
           ▼          ▼              ▼
         Stats     周巡检          Duke
                                     │
                                     ▼
                    existing runtime priority manager
                                     │
                                     ▼
                         canary outcome feedback
```

Stats 只负责呈现；周巡检只负责周期解释；Duke 保留资源配置判断；既有 runtime priority
manager 是唯一执行器。

`@anchor wire parse_openrouter_prices :: wired`
`@anchor symbol _desired_cost_route`

### 1.1 WI730 v2 App Server 主路径

v2 在宿主 client integration 旁建立独立、append-only 的 epoch usage facts；模型本身不参与观测：

```text
App Server thread/tokenUsage/updated + turn/completed
                         │ host notification
                         ▼
                 AppServerEpochObserver
               bind → turn usage → seal
                         │ opaque refs only
                         ▼
                 AppServerUsageLedger
                         │
                         ▼
           WI/epoch report + rollover shadow evaluator
```

`bind_baseline` 固定 source epoch 起点；后续累计水位只允许前进，`last` 必须与相邻累计差分相符。
`turn_completed` 与同 turn 的最终 usage 都存在后，带 operation reference 的 `source_seal` 才能写入。
所有 fact id 与 provider thread/turn/operation reference 都做 domain-separated SHA-256；原始身份只存在于
宿主进程内。ledger 以进程锁、append、flush 和 fsync 写入；重启后从 ledger 重建，不依赖内存缓存。

首个切片提供 host-callable `app-server-epoch bind|observe|complete|seal`。它接收宿主已经观察到的通知，
不是模型自查询，也不启动、代理或模拟 App Server。Codex App/ChatGPT Work 的常驻订阅接线与
`computecost <wi>` 聚合属于后续 v2 切片；在接线和 prospective UAT 前保持 candidate/default-off。

`@anchor symbol AppServerTokenUpdate`
`@anchor symbol AppServerEpochObserver`
`@anchor symbol AppServerUsageLedger`

### 1.2 WI 查询的渐进式完整性

`computecost <wi>` 首先只汇总具有唯一 `source_seal` 的 epoch；未封账 epoch 不进入 Token 合计并把报告
降为 `partial`。runtime/model/procurement 分区必须与 overall Token 逐字段守恒。没有 usage evidence 时
是 `unavailable`，不是零。WI machine cash 只有唯一政策：1.5.4 的 flat standard OpenRouter Token 现金；
procurement route 或订阅都不改变它。canonical usage 无法计价（缺 digest-bound catalog/valuation）时
machine cash 保持 `unavailable`，不降为 0。

首个 query slice 输出 usage、分区、seal digest 和明确缺口；artifact/outcome/human review/downstream
value、rate card、quota 与 provider-native cash joins 尚未接入时逐项 `unavailable`，不得由 Token 推断。
policy machine cash 见 1.5.4。后续 slice 在
同一 report schema 上补 producer join，不另造第二套 WI 查询。

`@anchor symbol build_wi_report`

### 1.3 Pi native producer

Pi 自身的 append-only session JSONL 是 Pi runtime 的 usage producer。adapter 从宿主注入的
`PI_SESSION_FILE` 读取，不让 Agent 提供或回显 raw path/session id：

```text
Pi session JSONL assistant/toolResult/compaction/branch_summary usage
                                │
                                ▼
                    collect_pi_session_usage
                                │ opaque ids + exact counters
                                ▼
                     privacy-safe summary / WI join
```

assistant 与 nested tool usage 直接使用条目 producer/model；compaction/branch summary 使用之前明确的
model/thinking-level state。procurement source 必须由 host binding 显式传入，绝不从 `openai-codex`、模型名、
路径或 cwd 推断。运行中 JSONL 若末行未闭合则 fail closed，不静默漏掉正在写入的 usage；重复 entry id、
计数不守恒或 reasoning 超过 output 同样拒绝。

adapter 读取的是工作真实产生的 assistant sampling、nested LLM 与 summary 用量，不读取正文。managed epoch
先把当前 append-only source position/digest 固定为 attributable-zero baseline；final 重新验证 immutable prefix 后只读取
baseline-exclusive/final-inclusive entry。stable duplicate 原样重放只计一次，同 id 内容变化 fail closed；sealed replay 只
复核已封 prefix，显式排除后来 suffix。source boundary 只留下 SHA-256、byte position 与 opaque entry ref，不暴露正文、
路径或 raw identity。

`@anchor symbol collect_pi_session_usage`

Relay 遇到本轮做过上下文压缩或分支摘要时不报整轮汇总，记一条四类数量全空的 `relay_pi.v1` 轮次（Host 缺计量）。原生会话文件只追加，所以查询只在有采集覆盖证明时，才把这条空轮次并入已核销、不再记 `unknown_token_quantity`。证明要求同一 `native_scope_ref` 有一份完整解析的原生前缀清单（`pi_native_prefix_inventory`），并同时满足三点：

- 清单包含该轮 `[interval_start, interval_end]` 内的全部 `pi_native.v1` 请求；
- 清单还含一条时间晚于 `interval_end` 的请求，说明这份文件已写过本轮结束；
- 区间内每条请求都有模型和四类数量。

本轮以报错结束同样适用。以下情况空轮次照旧保留为未知：只读到工具调用、副本落后、部分采集、没有前缀清单、区间内没有明细、明细缺模型或数量。核销只去掉空轮次，不把它的 WI 或执行者转给明细。

`@anchor test ComputeCost/tests/test_missing_host_measurement.py :: MissingHostMeasurementTests.test_native_prefix_past_the_turn_backfills_the_missing_turn`
`@anchor test ComputeCost/tests/test_missing_host_measurement.py :: MissingHostMeasurementTests.test_lagging_copy_without_the_later_request_keeps_it_unknown`
`@anchor test ComputeCost/tests/test_host_native_input.py :: HostNativeInputTests.test_turn_read_only_up_to_a_tool_use_keeps_unknown_placeholder`

### 1.4 App Server account reconciliation

Host 以环境绑定明确提供 App Server binary 与 CODEX_HOME；probe 启动独立 stdio process，只执行
`initialize → account/usage/read → account/rateLimits/read`，随后终止。argv/output 不含 credential，输出也
不复制 initialize 的 codexHome/userAgent 或任何 account/user/thread identity。

account usage 的 lifetime/daily bucket 只进入 account reconciliation。rate-limit primary/secondary、credits、
individual spend control 与 reset 时间进入 quota context，但在 WI report 中固定标记
`scope=account_not_work_item`。snapshot 自带 canonical SHA-256；WI query 重算 digest 后才接入。缺 sealed WI usage 时，account lifetime
Token 不能填补 WI Token；credit balance 与 provider 原生 cash 也不能直接变成 policy machine cash。

`@anchor symbol probe_app_server_account`
`@anchor symbol validate_app_server_account_snapshot`

### 1.5 Bounded Pi SCL report join

Managed Pi probe 包装为 digest-bound `pi_session_usage_snapshot/v2`，显式携带 WI、Agent、registered surface、
phase、epoch、opaque Session authority、binding digest、measurement role 与 baseline/final source positions。
SCL 将同 binding 的 zero baseline 与 settled final 配对，再与 cost attribution、surface registry 和 final Token seal
共同发布 mode-0600 `scl_pi_usage_measurement/v1`。ComputeCost 只从该 canonical pair 生成 `usage_event.v1` 与
per-request pricing partition，不扫描 JSONL 中的 WI 字符串，也不按 WorkItem wall clock 截取。

SCL 内核中的 attribution 与已接纳 baseline/final 原件是计量权威。归因、原生快照、Token seal 和 bounded pair 文件均由同一内核快照派生；写入后的入口和原提交重试会重建全套视图。缺失、坏 JSON、内容或文件模式漂移不能推翻原事实，重建恢复原摘要与 mode-0600；符号链接和非普通文件仍是路径边界错误。投影器串行读取最新快照，延迟的旧请求不能把已确定的 Pi/CC runtime 写回 resident_harness。删除运行时切换专用的副本字节比较与替换分支，原事实的幂等重试也不修改其时间戳。只读查询不重建文件；原始计量输入的身份、边界、mode-0600 与不可变事实冲突检查保持生效。

`@anchor symbol SCLMeasurementManager._materialize_epoch`

每份 auto-discovery 报告都包含 `pi_managed_scl` producer attempt。SCL 明确期望 Pi 却无 final pair 时是
`producer_missing`/typed gap，不是 `join_conflict`；没有 managed Pi attribution 的普通 CLI session 是
`not_applicable + unmanaged_unmeasured`。`--pi-snapshot` 保留 v1/v2 显式兼容，但 whole-session v1（包括自称
sealed）只作 unbounded diagnostic；单个 v2 未经 SCL pair/Token seal 也只作 uncommitted diagnostic。两者都不进入
final WI cost、ROI 或 pricing。

`@anchor symbol build_pi_session_snapshot`
`@anchor symbol validate_pi_session_snapshot`

#### 1.5.1 Historical Relay/TG Pi retrospective candidate

SCL baseline/final binding 上线前的 terminal WI 不能补造 canonical seal，但也不能把已存在的 registered producer
证据说成“无数据”。read-side adapter 从 Harness 取得 owner、terminal transition、每段 in-progress 边界和 artifact
registration，再把 owner 与 Relay Telegram registration、state 的 exact pool/agent/runtime/session、Pi manifest
session root 做唯一映射。它不读取 prompt/message 来找 WI；session 已被后续 WI 复用也只形成时间窗 estimate。

```text
Harness lifecycle events ─┐
Relay config + state ─────┼─ unique mapping ─ Pi JSONL assistant usage
Pi manifest ──────────────┘                         │
Relay metering (same exact pool/session) ──────────┘ cross-check
                                                     │
                                                     ▼
                         computecost_retrospective_usage_candidate/v1
                         canonical usage / seal / cost / ROI: all false
```

candidate 同时保留 `created_to_terminal=estimate_may_include_unrelated_turns` 与多个
`lifecycle_active=estimate_may_omit_verification_work` 区间。Pi assistant records 与 Relay settled outer turns 比较；
compaction 等不可比内部 usage 单列，reasoning 在 Relay 缺失时保持 unavailable。当历史窗口保留逐 request
Token 时，report 按唯一 flat policy 计算并暴露 `retrospective_machine_cash_usd`，estimate 对象固定带
`may_include_unrelated_turns=true` 与 `noncanonical_total=true`——它是政策下可见的现金估计，但绝不进入
canonical total/Token seal/cost/ROI。canonical total 保持 unavailable 的原因只能是 Codex/Token 归因不完整，
而不是 provider cash 或 subscription 证据缺失。所有源只输出 domain-separated
SHA-256 ref；source mode 独立报告，0644 是 privacy defect，reader 不修改文件。报告把 lifecycle/artifact availability
与 canonical measurement readiness 分轴；因此 cancelled + registered artifacts + retrospective usage 可以是
“evidence available, canonical measurement unavailable”，而不是伪零或伪 seal。

`@anchor symbol discover_relay_pi_retrospective`
`@anchor symbol validate_retrospective_candidate`

#### 1.5.2 Structured verifier assignment and archived Codex lineage

Codex discovery first builds an assignment inventory from the canonical WI event stream, exact
`outcome_verification` Handoff records, acceptance events, and outcome `verification_assignment` refs. An
exact assignment ID in a child session index/user envelope is a WI seed even when no WI text appears. The
reader then closes over explicit parent/delegation/rollover source identities across both `sessions` and
`archived_sessions`. Raw identities exist only in memory; output keeps assignment/session refs as
one-way digests and counts.

Same-session active/archive streams are one producer: identical streams or immutable prefixes select the
longest version once; conflicting versions fail closed. Parent copied prefixes remain baselines, so
controller and child usage cannot double count. Any WI seed or assignment for another WI taints the entire
component. An assigned historical session with missing rollout remains `partial` with Token
`unavailable`; exact assignment/verifier/session counts survive independently of Token files. Outcome prose
is never a Token source.

#### 1.5.3 Human-attention vector

`computecost_human_attention_vector/v1` separates:

- `focused_seconds`: only explicit timer/trusted telemetry;
- `cycle_elapsed_seconds`: canonical created→terminal exposure, never focused time;
- exact WI-bound feedback count and six explicit categories;
- G0 HAU micro/bounded/judgment/framing counts and units;
- feedback/HAU density over elapsed days.

Prospective SCL events precede overlapping Harness projections and retrospective facts. Terminal
retrospective reads only canonical lifecycle/review facts, so it can safely retain elapsed exposure and human
approval after raw conversation sources disappear. Without an upstream per-turn WI binding, Relay outer
turns and time windows remain unavailable; Handoff, wall clock, waiting, and absence never become feedback.
No terminal source is backfilled.

#### 1.5.4 Flat WI pricing（唯一 machine cash 政策）

WI valuation groups per-request normalized total input into uncached/cache-read/cache-write and output, then
multiplies each exactly once by the model's top-level standard rates in a timestamped OpenRouter models
catalog. The conserving Decimal total **is the WI machine cash** (`machine_cash_usd`, basis
`openrouter_standard_token_cash`, coverage `calculated_from_digest_bound_openrouter_catalog`) and is the
cost used in ROI, independent of procurement route or subscription. `pricing.overrides`,
context-derived tiers, producer-native costs, and subscription settlement have no edge into WI report or
ROI. A Harness/provider native cash field is carried only as non-authoritative `provider_native_cash_usd`
context and can never override machine cash, cash basis, cash coverage, or ROI. A retrospective Pi-only
estimate uses the same function but remains a separately bound, noncanonical object that never joins the
canonical total.

`@anchor symbol build_attention_vector`
`@anchor symbol price_wi_usage_cohort`

### 1.6 SCL/runtime host binding

宿主从 canonical entry plan 取 WI、Agent、phase、target epoch 与 registered surface，并把 runtime、procurement
route 和 privacy-safe native Session authority 作为显式 host facts 合成 `pulse_runtime_usage_binding/v3`。canonical JSON 自带 SHA-256，且字段集合封闭；raw provider
session/thread/turn/message、路径、正文和 credential 根本不属于 schema。

```text
Harness ResumePacket + explicit runtime/procurement route
                         │
                         ▼
             RuntimeUsageBinding (digest-bound)
                  │                       │
          Pi/Codex child env       ChatGPT App host seam
                  │                       │
                  └──── usage producer ───┘
```

同一进程只能消费 inline JSON 或 absolute regular binding file 之一；两个同时存在、digest 不符、runtime、
registered surface、opaque Session authority 错配或 legacy 手工字段冲突都 fail closed。v1/v2 只读兼容，不能启动
managed Pi。未绑定 producer 不根据 provider/model/cwd 猜归属。Pi open-prefix probe 可在运行中生成，但只有宿主
post-final lifecycle hook（Pi `agent_settled`/`session_shutdown` 或 App Server 最终 `turn/completed` + usage）可写
`sealed`；target admission 必须带新 epoch/session/surface binding。

Persisted v2 Pi conversation recovery does not create v3 measurement authority. At rollover,
Host verifies the exact settled native Session and its regular history file, then durably writes
an immutable `pulse_legacy_pi_retirement/v1` receipt with binding digest and source bytes/digest.
`legacy_unmeasured` means historical coverage remains unavailable, never zero or a bounded cost
seal; canonical legacy completion reports `unavailable + producer_unavailable`. An unbound/v1
source, identity mismatch, missing history or corrupt receipt still fails closed. A resumed source
may append history and produce a different content-addressed retirement receipt; old receipts stay
immutable. Only after this preservation or a real v3 seal may Host retire the source. The target
always requires its own v3 baseline. Typed deterministic seal failures remain non-retryable through
entry adapters; transport/settlement retryability must come from the underlying error.

Relay 的 Pi 与 Codex launcher 只转发已验证 binding，不自行发明 WI 或采购来源。Pi successor 为满足 Harness
live readback 会先启动，但此时 canonical adoption 可能尚未完成；`session_start` baseline 失败只能留下一个
non-runnable process，不能接收业务输入。adoption 完成后 Relay 调用 host-only `runtime-usage-baseline`，该命令
重放同一 immutable baseline、取得 canonical SCL `open` readback，并由 Relay 独立复核 artifact mode、digest、
privacy 与 binding。只有该 gate 通过才发送 ResumePacket；Pi adapter 自身还在 `send` 前复核 baseline-activated 状态，避免调用方
漏 gate 后静默 dispatch。restart 与 retry 复用同一 target/binding/baseline，禁止事后补 baseline。
ChatGPT Work/App Server 沿同一 contract 构造 `EpochUsageContext`，原始 thread/turn identity 只在 host observer
内瞬时存在，ledger 继续只保存 opaque refs。

`@anchor symbol RuntimeUsageBinding`
`@anchor symbol runtimeUsageBindingEnvironment`
Terminal completion ends the active WI runtime binding, not the native conversation. Relay startup and
process recovery use `plan-entry-local` to prove the same WI/epoch is terminal before archiving its
binding as historical attribution and returning to the configured conversation workspace. A stopped
entry for a different WI and native target does not block this release: its pending record and user
scope fence remain unchanged, with no claim, activation or business replay. Active, ambiguous or
same-target entries still require reconciliation. Terminal workspace execution admission remains denied.

`@anchor test ComputeCost/tests/test_runtime_binding.py :: RuntimeUsageBindingTests.test_build_validate_and_replay_are_byte_stable`
`@anchor test Relay/tests/runtime-usage-binding.test.ts :: runtime usage host binding`

### 1.7 Harness measurement join

Harness 已有只读 `s1_measurement_pilot.v1`，它从 canonical WI events、Outcome receipt 与 exact human review
投影质量、review/rework、effect、cycle/session/recovery 等效率字段；其 `actual_cash_usd` 携带 provider
原生现金事实。ComputeCost 只消费该投影，不读取或复制 Harness 私有 ledger 细节：

```text
Harness canonical facts → s1_measurement_pilot.v1
                              │ strict consumer validation
                              ▼
                 computecost_wi_report/v4
       quality / human review / rework / effect / efficiency / native-cash context
```

consumer 采用封闭字段集，逐项核 `readout.status/value` 与顶层投影一致，复核 missing producer、sensor
health、WI identity 和 repo-relative provenance 后再计算 canonical SHA-256。部分 producer 缺失时只把对应
字段保留 `unavailable`；有任一可靠叶子时 outcome coverage 为 partial，全部质量/效率叶子 computable 才为
exact。Harness provider 原生的 `actual_cash_usd`（包括 `0`）只投影为非权威
`provider_native_cash_usd`，绝不覆盖 1.5.4 政策 machine cash/basis/coverage/ROI；WI machine cash 仅由
digest-bound OpenRouter catalog 计算，不可计价时不补 0、不反推。artifact set 与 business value 没有此
producer，继续显式 unavailable。

`@anchor symbol validate_harness_measurement_snapshot`
`@anchor symbol measurement_join_coverage`

### 1.8 App Server thread rate-card join

App Server host 可在已知当前 raw thread 的进程内调用 `account/usage/read(threadId)`；raw identity 仅用于
核响应归属并立即转换成与 notification observer 相同域的 opaque `thread_ref`。持久 snapshot 只带 runtime
binding、estimated credits/USD micros 与 model/effort/speed group Token：

```text
host raw thread + runtime binding + account/usage/read response
                              │ identity match + conservation
                              ▼
                 app_server_thread_usage_snapshot/v1
                              │ digest validation
                              ▼
       WI report reference credits + provider-estimated USD
```

thread 总 credits 必须与非空 groups 之和相等；group 的 cache≤input、net-new=input-cache、
`total=input+output` 在字段齐备时必须逐项复算。USD 缺失保持 unavailable，不补 `0`。该路径是 billing-route
estimate，既不替代 notification Token 主账，也不填 actual billed cash；account summary/quota 与 raw
thread/account/user/credential identity 均不进入 snapshot。

`@anchor symbol build_app_server_thread_usage_snapshot`
`@anchor symbol validate_app_server_thread_usage_snapshot`

### 1.9 Reviewed artifacts and consumer value

产物来源复用 Harness `work_item_review/v1`，不另建产物台账。`repo_heads[].repo` 遵循 Harness `ReviewRepoHead` 的稳定逻辑名称契约，既可为 `PULSE` 也可为 `bigbird-online/PULSE`；保留名称及大小写，不猜组织或互换身份，空值、绝对/家目录路径和 `..` 仍拒绝。原生 `reviewed_at` 接受明确时区的 ISO 时间，读取投影统一为 UTC Z；不改原文件、候选摘要或原发生时刻，无时区时间仍拒绝。此兼容不放宽业务价值回执的 canonical UTC 要求。ComputeCost 重算 exact candidate identity：

```text
WI + AC fingerprint + content-bound artifact set + sorted repo heads
                              │ SHA-256
                              ▼
                    candidate fingerprint
```

report 只投影 approved/changes-requested 的 exact artifact-set digest、repo revisions、review time 与
privacy-safe evidence refs；CLI 消费 Harness `review show --json` 时还要求 `review_state` 非 stale。它不把 PR
列表、checkpoint artifact 或模型自报集合冒充 reviewed artifacts。

业务价值使用 `computecost_business_value_receipt/v1`。producer 必须是实际 downstream consumer，verifier
必须与 producer/consumer 不同；receipt 绑定 canonical outcome ref 与同一 reviewed artifact set。量化值固定
保存 canonical Decimal `baseline/current/delta` 并复算 `current-baseline=delta`；定性值禁止夹带数字字段。
每条 evidence 是 repo-relative ref + SHA-256。report 只有在 exact approved review，且已接 Harness measurement
时 delivery status=accepted，才把它标为 `consumer_verified`；其它情况保持 unavailable 或 fail closed。

`@anchor symbol validate_harness_review_snapshot`
`@anchor symbol build_business_value_receipt`
`@anchor symbol validate_business_value_receipt`

### 1.10 CC/CC-SDK/OpenRouter samples

`runtime_usage_sample/v2` 复用 SCL runtime binding，把 producer wire semantics 与采购归因分开；v2 删除 native cost 字段：

```text
CC transcript / CC-SDK event / OpenRouter response + runtime binding
                              │ normalize + privacy hash
                              ▼
                    runtime_usage_sample/v2
```

CC/SDK 的 `input_tokens` 是 uncached，须加 cache-read 与 cache-creation；OpenRouter `prompt_tokens` 已含
cached，只保留 subset，不再相加。两类 sample 都检查 input+output=total、cache≤input、reasoning≤output。
raw event/message/request identity 只参与 domain-separated event ref；session/cwd/content/credential 不进入输出。

OpenRouter response `usage.cost` 直接丢弃，runtime sample 只保留 Token quantity。OpenRouter 生成
billing（`openrouter.activity` 管理接口）只作 Token/cost 守恒旁证，不构成与 1.5.4 政策 machine cash
竞争的 WI 估值 authority；30 天 coverage 不得扩写成完整月。CC/SDK 的
`procurement_source` 完全来自 host binding，即使 model catalog 显示 OpenRouter provider 也不能替代绑定。


CC/CC-SDK 的受管实际链路为 Relay `cc-usage-measurement.ts` → `cc-session-probe` →
`cc_usage.collect_cc_session_usage` → **`runtime_samples.build_cc_usage_sample`**。每条纳入范围的原生消息
先产生 `runtime_usage_sample/v2`，使用原 Host binding digest，不签发替代 binding。`cc_usage.py` 不再
独立解释 Claude 原始 Token 字段；仅将规范样本投影为原有边界汇总的 uncached input/cache 分区。

`cc_session_usage_snapshot/v2` 是该样本链的字节边界与封存包络，不是第二套样本或计价路径：它保留
baseline/final 前缀摘要、同 message.id 的已验证累积更新、子代理范围和排除量。SCL 消费同一包络，生成
`scl_cc_usage_measurement/v1`；原始 Token、采购来源与价格权威不变。未持有 WI binding 的 CC 日志仍可
通过原凭据采集器记录原始用量和未知归属，不得为构造规范样本而伪造 binding。部署状态按节点单独核验。

`@anchor symbol build_cc_usage_sample`
`@anchor symbol build_openrouter_usage_sample`
`@anchor symbol validate_runtime_usage_sample`

## 2. 数据对象

### 2.1 `price_snapshot.v1`

```json
{
  "schema_version": 1,
  "source": "openrouter.models",
  "retrieved_at": "UTC timestamp",
  "model_id": "deepseek/deepseek-v4-flash-0731",
  "canonical_slug": "provider permanent slug",
  "currency": "USD",
  "unit": "per_token",
  "pricing": {
    "prompt": "decimal string",
    "completion": "decimal string",
    "input_cache_read": "decimal string or null"
  }
}
```

所有价格用 decimal string；不把每 token 价格转换为二进制 float。

`@anchor symbol OpenRouterPriceSnapshot`
`@anchor test ComputeCost/tests/test_openrouter.py :: OpenRouterContractTests.test_parse_openrouter_prices_preserves_exact_decimal_rates`

### 2.2 `openrouter_activity_snapshot.v1`

仅保留脱敏后的 `period`、`totals`、`by_day` 和 `by_model`。禁止输出 management key、
API key hash、endpoint id、workspace id 或 user id。

`@anchor symbol summarize_openrouter_activity`

### 2.3 `usage_event.v1`

`event_id` 是脱敏幂等键；`occurred_at` 与 `observed_at` 分开；`source` 只允许
`chatgpt_pro/openrouter_api/unknown`；`consumer_path`、`task_ref`、`outcome_ref` 连接执行与结果。
token 字段固定为 `input`、`output`、`cache_read`、`cache_write`、`reasoning`，其中 input 已规范为
包含 cache 的总输入；未知的数字字段为 null，未知的字符串字段为字面量 `unknown`。

每个新写 v1 事件还包含 `lineage`：

```json
{
  "root_agent": "unknown",
  "execution_mode": "human|automatic|unknown",
  "session_role": "main|sub_agent|unknown",
  "root_run_ref": "lineage_v1_<sha256>|unknown",
  "parent_run_ref": "lineage_v1_<sha256>|unknown",
  "cron_job_ref": "opaque producer reference|unknown",
  "scan_ref": "opaque producer reference|unknown"
}
```

`lineage_v1_` digest 的输入必须含固定 domain tag 和 producer 原始稳定键，保证不同键域不碰撞；
digest 能跨同一原始 run 关联，却不能还原原始 id。历史 v1 文件可不含该对象，reader 将七个字段
规范为 `unknown`。这是一项 v1 的兼容性扩展，不升级 ledger schema：当前没有 production writer 或
持久 ledger，且 snapshot 只消费 canonical semantic facts。

事件不保存原始 tenant/session/message/turn id、绝对路径或 prompt/response；稳定上游 id 只参与
SHA-256 event id 或上述 lineage digest。provenance 只保留 adapter、UTC 日分区和行号。缺
task/outcome/retry/route/lineage 关联时 quality 为 `partial`，允许呈现覆盖缺口，但不允许把 unknown
当 0。

`@anchor symbol UsageEvent`
`@anchor symbol TokenUsage`

### 2.4 `usage_ledger.v1`

目录固定分为 `usage/YYYY-MM.jsonl` 与 `corrections/YYYY-MM.jsonl`。北京产品月内首次观察的事件
追加到前者；跨月首次观察的迟到事件追加到后者。两者都是 append-only，查询逻辑合并读取。
重放比较 event 的不可变事实，忽略首次观察时间与本地 source position；完全相同则 no-op，
冲突则整批在首次 append 前失败。

`@anchor symbol UsageLedger`
`@anchor symbol LedgerIngestResult`

### 2.5 `managed_cost_snapshot.v1`（deprecated pre-WI-policy compatibility）

该对象只为旧 Stats monthly shadow 保留；subscription allocation/settlement 不得进入 `wi-report`、WI ROI
或 retrospective estimate。新 consumer 只认 1.5.4 flat policy。

根对象固定包含 `schema/schema_version/snapshot_id/settlement_status/product_month/timezone/generated_at`、
`input_identity`、`price_snapshot`、`actual_openrouter_evidence`、`coverage`、`totals`、`by_agent` 和
`quality`。`snapshot_id=mcs1_<sha256>` 对除自身外的 canonical JSON 求 hash；`input_identity` 另含
`usage_event.v1`、事件数与 `uevset1_<sha256>`，hash 输入是按 event id 排序的不可变事件事实。
它让“两个事件集碰巧聚合成同一金额”仍可区分，同时不把 event id 列表透给 Stats。

核心字段固定为：

- `gross_reference_cost_usd`：全部可计价事件的 OpenRouter 参考成本。
- `api_reference_cost_usd` / `pro_reference_cost_usd`：按来源拆开的 gross reference。
- `pro_allocated_cents` / `unallocated_cents`：合计严格为 Pro `20000` 美分。
- `pro_settled_cost_usd`：上述两种 cents 之和除以 `100`。
- `pro_settlement_adjustment_usd`：`pro_settled_cost_usd - pro_reference_cost_usd`。
- `managed_cost_usd`：`gross_reference_cost_usd + pro_settlement_adjustment_usd`，等价于
  `api_reference_cost_usd + pro_settled_cost_usd`。
- `actual_openrouter_usd`：OpenRouter activity 的现金对账旁路，不进入 `managed_cost_usd`。
- `unattributed`：缺归因键的成本；不得丢弃或按零处理。
- `unpriced_event_count`：来源为 unknown、无法按价格快照估值的事件数；只增加 evidence 缺口，
  不把其成本写成已知 `0`。

每个北京自然月独立 settlement。月内 snapshot 标 `provisional`，随着可归因用量重算；月结后
价格快照和 allocation 固化。分区合计包含 `unallocated` 特殊分区，必须等于 overall
`managed_cost_usd`。对外默认展示 `managed_cost_usd`，明细页保留上述完整计算链。

`actual_openrouter_evidence` 在现金金额存在时必需，固定带 `openrouter.activity`、脱敏快照 digest、
UTC 日起止与 coverage。`partial` 允许进入旁路对账；`complete` 只允许 closed settlement，且窗口
至少覆盖北京月起点对应的前一个 UTC 日至北京月最后一天。保留的 `unattributed` 与 `unallocated`
是特殊分区名，真实 Agent 不得占用。

`@anchor symbol ManagedCostSnapshot`
`@anchor symbol build_managed_cost_snapshot`
`@anchor test ComputeCost/tests/test_managed_snapshot.py :: ManagedCostSnapshotTests.test_complete_cash_coverage_requires_closed_full_month_window`

## 3. 已实现的 candidate slices

S0 历史实现包含无生产入口的价格、现金对账与 subscription settlement 纯函数；其中
`allocate_subscription_cents` / `settle_managed_cost` 已 deprecated，只供旧 monthly Stats shadow：

```text
pulse_compute_cost.openrouter
  load_dotenv_value
  fetch_openrouter_activity
  fetch_openrouter_models
  parse_openrouter_prices
  summarize_openrouter_activity

pulse_compute_cost.valuation
  estimate_reference_cost
  allocate_subscription_cents
  settle_managed_cost
```

CLI 仅在显式调用时联网并向 stdout 输出脱敏 JSON；不落盘、不注册 cron、不接 Stats。

`@anchor symbol main`
`@anchor test ComputeCost/tests/test_openrouter.py :: OpenRouterContractTests.test_dotenv_loader_returns_only_requested_value`
`@anchor symbol settle_managed_cost`

S1 增加 `events/adapters/ledger`：

```text
Relay metering ─┐
                ├─ reconcile → usage_event.v1 → UsageLedger
CC transcript ──┤                    │
Codex rollout ──┘                    └─ usage / corrections monthly partitions
```

采集顺序是 CC → Codex → unmatched Relay fallback。CC 以最终 `message.id` 去重；Codex 在每个
`task_complete` 使用累计差分，跨 rollout 重复 turn 以 hash id 折叠，且优先保留 Relay 已补归因
的副本。Relay 的 Codex fallback 对 session 的 input/output/cache-read/cache-write 分别累计差分，
input 不再叠加 cache；Relay 原始事实先按内容 hash 折叠 recovery/rollover 副本，再参与关联和差分。
已被原生事件认领的 Relay 行必须推进该 session 的累计去重边界，即使其缓存拆分不合法，
也不得让下一条未认领行跨过已计费区间。认领行本身不再生成 fallback 用量；未认领行仍须通过
token 类别校验，坏行不能充当已校验的计量基线。

`@anchor test ComputeCost/tests/test_adapters.py :: test_claimed_invalid_relay_counter_still_advances_deduplication_boundary`

CLI `usage-probe` 只输出聚合诊断，
不输出事件 id 或明细，也不写 ledger；ledger 当前只有显式 library API 和测试，没有生产 writer。

为避免长期 session 大行拖垮 collector，JSONL 按行流式读取，并在 JSON decode 前按事件类型过滤
prompt、tool output、world state 等无关行；传入 `since` 时只扫描 mtime 覆盖该窗口的 runtime 文件。

P0 attribution 先只修改 candidate evidence plane。Codex adapter 从 `session_meta.parent_thread_id`
和 `source.subagent.thread_spawn.parent_thread_id` 取得显式 parent 证据，生成 `parent_run_ref` 与
`session_role=sub_agent`；没有可验证 root 链、root Agent 或 human/automatic producer 键时保持
`unknown`。它不得用 cwd 或 prompt 填这些 lineage 字段。对于 `fork_turns=all`，child 文件的 copied
prefix 只推进 cumulative token baseline；只有 child 的首个真实 `turn_context` 之后的
`task_complete` 才能发 event。adapter 以 child 的显式 `parent_thread_id` 定位 parent rollout，并从 child
本地 session metadata 之后与 parent stream 相同的**最长前缀**确定 copied-prefix；这个前缀只建
baseline。找不到 parent rollout 或无法确证前缀时必须 fail closed 为 lineage partial，不能以 token 值、
cwd 或路径猜测。Relay scheduler、Harness scan、nested Duke 和 Relay interactive producer
键留给后续独立切片。

月度现金结算、周容量、reference cost 与 quota pressure 也分开建模：前者进入
`managed_cost_snapshot.v1`，后两类在原生 query/report 进入容量事实；reference cost 不得预测
quota pressure，缺 reset/slope 一律 unknown。

`@anchor symbol collect_usage_events`
`@anchor symbol UsageLedger`
`@anchor symbol _usage_probe`

S2 增加 `managed_snapshot` 与 Stats default-off shadow reader：

```text
UsageEvent[] + one OpenRouterPriceSnapshot
                  │
                  ▼
       managed_cost_snapshot.v1
          │ strict read-only file
          ▼
Stats exporter compute_cost.shadow.{summary,by_agent}
          │ typed parse + conservation
          ▼
Swift 独立 Shadow 区块 + legacy 成本区块（并存）
```

ComputeCost 构造与 validator 维护生产者合同；Stats 在独立模块中重复最小严格校验，跨模块测试用真实
ComputeCost 产物防两份 schema 各自变绿却互不兼容。Stats feature flag 的真实入口在
`LiveSnapshotService.default()`：仅精确字符串 `1` 启用，path 必须绝对；`LivePaths()` 本身始终
默认 false。关闭时 collector 返回空指标列表，旧指标的集合与语义不变；开启但输入坏时固定输出
两项 unavailable 指标，主快照继续生成。

当前月入口只接受 `provisional`、北京当前月且生成时间距 Stats collection 不超过 `45` 分钟的文件；
closed 历史月留给后续查询，不进入 MTD App。Swift 不复写估值，只复核两个指标 bundle 的类型、
整数与 Decimal 守恒；bundle 缺一或畸形则整个 shadow 区块消失，legacy 区块照常显示。

`@anchor symbol ManagedCostShadowError`
`@anchor symbol build_managed_cost_shadow_metrics`
`@anchor wire build_managed_cost_shadow_metrics :: wired`
`@anchor test Stats/exporter/tests/test_managed_cost_shadow.py :: ManagedCostShadowTests.test_default_off_emits_no_metrics_and_does_not_read_path`
`@anchor test Stats/exporter/tests/test_managed_cost_shadow.py :: ManagedCostShadowTests.test_tampered_conservation_or_identity_fails_as_atomic_unavailable_bundle`
`@anchor symbol ManagedCostShadow`
`@anchor test Stats/PulseStatsTests/BillingMetricsTests.swift :: BillingMetricsTests.testManagedCostShadowAbsenceOrDamagedBundleFallsBackToLegacyOnly`

## 4. 跨机与 secret 边界

1. `OPENROUTER_M_KEY` 留在 ownermac `/Users/owner/Coevolution/.env`。
2. 价格目录公开读取，不需要 secret。
3. activity collector 必须在 ownermac 进程内读取 secret 和完成响应脱敏。
4. PULSE 后续只接收 `openrouter_activity_snapshot.v1`；不得同步 `.env` 或原始 key 清单。
5. 网络、auth、schema 任一失败时 collector 非零退出且不输出伪 snapshot。

部署方式仍在后续跨机切片决定。S1 仅在
ownermac 临时 scratch 中执行同版 read-only probe，退出后清除；没有常驻代码、ledger 或 cron。

## 4.1 跨源对账与防双计

| 证据 | 用量职责 | 归因职责 | 重复处理 |
|---|---|---|---|
| CC transcript | raw message usage；Claude cache 三项归一化 | cwd 只能给候选 Agent | 同 message id 取最终记录 |
| Codex rollout | task_complete 前最终累计值减前一 task 累计值 | cwd 只能给候选 Agent | 同 session+turn hash 折叠 |
| Relay metering | raw 缺失时才做 fallback | 优先提供 Agent/consumer | 稳定事实 hash 折叠副本；已关联行 consumed |
| OpenRouter activity | 仅实际现金总账 | 不提供 Agent/task | 不进入 usage_event 明细 |

model catalog 给出 model→provider/protocol；ComputeCost 的显式 policy 再把
`openai-chatgpt→chatgpt_pro/codex`、`openrouter→openrouter_api/cc` 映成成本来源与 runtime。
除此之外，runtime、provider、文件路径都不能反向替代 source 归属。Relay ↔ CC 用 session、token
与 30 秒窗口关联；Relay ↔ Codex 用 model、累计 input/output 与 30 秒窗口关联。关联只消费内存中的
原始 id，不把 id 写进事件。

## 5. 成本解释模型

成本驱动使用固定顺序的四段 bridge，不把可重叠维度冒充独立“层”：

```text
C(D0,R0,E0,P0)  baseline
  + demand_delta     -> C(D1,R0,E0,P0)
  + route_delta      -> C(D1,R1,E0,P0)
  + execution_delta  -> C(D1,R1,E1,P0)
  + price_delta      -> C(D1,R1,E1,P1)
  + residual_delta   = observed current managed cost
```

`C` 使用同一 cohort key（consumer path、producer-owned task family、producer-owned risk class）
上的事件计数/组合 `D`、路由 `R`、执行量 `E` 和价格/settlement `P` 复算 managed cost。
`usage_event.v1` 缺 cohort 字段时不得以 `task_ref` 或 route 近似替代。替换顺序固定为
`D → R → E → P`；四段和 residual 必须精确等于 overall delta。缺 cohort 或 counterfactual
输入时差额进入 residual，状态为 `insufficient_evidence`；算术不守恒时状态为 `invalid`。

outcome 独立作为约束层，报告 accepted/rejected/unknown/human-correction，并计算
`managed_cost_usd / accepted_outcome_count`；accepted 为零时该比率为 null，不进入成本 bridge。

## 6. 控制模型

控制器不直接选择任意模型字符串，只能在中央 compute profile 和已批准 named preset 中行动：

```text
observe
  -> compare baseline
  -> recommend hold/promote/rollback/insufficient_evidence
  -> Duke approves or rejects
  -> runtime manager plan/apply
  -> verifier + outcome receipt
  -> 7d/10 outcomes gate
  -> promote or local rollback
```

约束优先级固定为：risk floor → outcome quality → evidence completeness → reference cost。
“更便宜”不能覆盖 critical risk floor、accepted 下降或人工纠错上升。

### 6.1 recommendation → runtime manager 适配合同

recommendation artifact 必须携带 `route_name/current_preset/target_preset/rollback_preset`、
`expected_changed_routes`、active route digest、plan digest、证据窗口和有效期。适配器只读 runtime
manager 的 preset catalog 与 `ALLOWED_COST_PRESET_TRANSITIONS`：

1. `target_preset` 不存在或 transition 不允许：`insufficient_evidence(no_approved_preset)`。
2. 用 `plan --preset <target_preset>` 生成 plan；只有一个 route 的 effective profile 改变且
   全局 compute profile 不变，才满足 single-consumer canary。
3. Duke 审批 recommendation；实际 `apply --preset` 仍需既有生产授权。本设计没有自动审批者。
4. apply 前 active route digest 和 plan digest 必须仍匹配；否则作废建议并重新 observe。
5. apply stdout 的 `APPLY OK backup=<backup-id>` 写入不可变 apply receipt，随后立即 `verify`。
6. rollback 使用 `rollback <backup-id>`，再 `verify` 并确认 active preset 等于
   `rollback_preset`。canary 窗口内禁止并发的无关 runtime 配置 apply，避免整笔 backup 回退他人变更。

catalog、preset 和 transition map 归 runtime priority manager；ComputeCost 不写这些对象。当前
`current_safe → phase2_stats_canary` 只允许 Stats route canary，
`phase2_stats_canary → phase23_canary` 只新增 weekly-review route canary；活动 preset 已无合法
单 route promote 时只能 hold/rollback，新增 preset 必须另改 runtime Spec、catalog 和测试。

控制面按切片拆开但仍复用同一个 manager：S4 增加只读 `runtime_priority_plan.v1`（route diff、
active/target digest、runtime-profile digest、catalog/transition map、plan digest）并要求显式 preset apply
携带 fresh active-route/plan digest precondition；S5 在任何真实 canary apply 前再增加跨观察窗的
active-canary lease，lease 存在时拒绝无关 apply，直到 promote 或指定 backup-id rollback。S4
保持 recommend-only，不调用 apply；该扩展始终属于既有 manager，不在 ComputeCost 内新增执行器。

`@anchor symbol build_cost_driver_bridge`
`@anchor symbol _structured_plan`
`@anchor test ComputeCost/tests/test_query.py :: CostQueryTests.test_s4_driver_bridge_explains_retry_model_and_outcome_changes`
`@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_structured_plan_identifies_single_effective_route_change`

`@anchor symbol _profile_rank`
`@anchor symbol _materialize_cost_route`
`@anchor manual feedback-controller-boundary :: 用一个 standard 与一个 critical fixture 生成建议；standard 可降档，critical 低于 frontier 必须拒绝；不调用 apply 时生产 route 文件保持字节不变`

## 7. 集成合同

### 7.1 Stats

现行接线是协作凭据 query → `pulse_stats_cost_dashboard/v1` → 固定 exporter → App；详见[仪表盘设计](stats-query-dashboard.md)。同水位原查询、发布物、App 缓存的数量、金额与联合分组必须一致；客户端不计价、不从独立分组拼表。

查询的每个 `cost`（摘要与分组）带 `priced_machine_cash_usd`（已计价合计：本次选择中全部已计价用量行的标准金额之和，不论完整性原因）与 `priced_event_count`（这些行的条数）。二者不改变 `machine_cash_usd`/`partial_machine_cash_usd` 的含义：完整时与 `machine_cash_usd` 相等，存在 partial 时与之相等，`join_conflict` 扣住 partial 时仍给出已计价行之和；没有已计价行时为 null。已计价条数加未计价条数等于该范围用量记录数，exporter 按此校验；缺这两个字段的旧发布物仍可读，因此 Stats 读取端须先于发布器部署。

Stats 发布物的每个 `cost` 另带 `anomalies`（需处理）与 `proof_gaps`（完整性未证明），二者不重不漏地划分原 `reasons`；`reasons` 与全部金额字段不变。`proof_gaps` 只收无法证明完整的原因：`source_coverage_unknown`、`wi_execution_inventory_unknown`（尚无来源发出用量覆盖或执行清单凭据）、`execution_link_unknown`（原生用量不带执行引用）、`context_inventory_unknown`（所选事务范围的清单未证明完整）、`provisional_usage`（未定稿明细按下限计）与 `no_usage_observations`（本期没有用量凭据）。其余原因及今后新增的原因都归 `anomalies`，包括冲突、无 WI 归属、缺价或缺量、执行或计量缺失、无法归期和价表时间未知。分类只由 `PROOF_GAP_REASONS` 定义；exporter 与 App 只校验划分。exporter 拒绝只带其一或划分不符的 `cost`，因此 Stats 读取端须先于发布器部署；App 把旧发布物的全部原因按需处理显示。
`@anchor symbol classify_cost_reasons`
`@anchor test ComputeCost/tests/test_stats_dashboard.py :: StatsDashboardTests.test_reason_classes_separate_proof_gaps_from_anomalies`
`@anchor test Stats/exporter/tests/test_cost_dashboard.py :: BoundedDiagnosticsReaderTests.test_reason_classes_must_partition_reasons`
`@anchor test Stats/PulseStatsTests/CostDashboardTests.swift :: CostDashboardTests.testProofGapsAreNotAnomaliesAndKeepTheAmount`

原 S2 `managed_cost_snapshot.v1`、`compute_cost.shadow.*` 与订阅分摊测试只保留为兼容历史；正式 exporter 不再混入旧 billing/usage/shadow 指标。关闭旧 shadow 参数不是当前版本的回退入口。运行恢复由 `Stats/deploy.py` 的程序/配置前像与读取兼容检查完成，不恢复旧账本覆盖新事实。

历史锚点只定位仍保留的兼容代码，不代表当前启用：
`@anchor symbol managedCostShadow`
`@anchor symbol _managed_snapshot`
`@anchor test Stats/PulseStatsTests/BillingMetricsTests.swift :: BillingMetricsTests.testManagedCostShadowRejectsOverflowingIntegerConservationWithoutTrap`

### 7.2 周巡检

周报调用 `query --from <ISO week start> --to <ISO week end>`，直接消费 by-agent、by-consumer、
by-outcome 与 driver delta，替代人工 awk/filter。

### 7.3 Duke

Duke 输入是 recommendation artifact，不是原始 token dump。每条建议必须带当前/目标 profile、
证据窗口、coverage、预期成本、质量约束、rollback 和有效期。Duke 审批或拒绝建议；模块不替代
判断，也不把审批等同于生产 apply 授权。

## 8. 迁移与回滚

以下 S0–S6 是早期整体建议/控制权威切换方案，保留作历史验收依据，不是当前 Stats 的部署步骤。现行成本链的安装、失败恢复和旧数据保护见页首与[仪表盘设计](stats-query-dashboard.md)；不得照此历史表重启旧计价 writer 或恢复订阅分摊。

| 阶段 | 默认行为 | 晋级门 | 回滚 |
|---|---|---|---|
| S0 规格/纯函数 | candidate；不接生产 | unit + spec-anchor | 删除新模块即可 |
| S1 shadow collector | candidate；只读、无 UI | 价格/用量真实探针、secret 零泄漏 | 停 collector；legacy 不变 |
| S2 Stats shadow | shadow；legacy 用户可见真源 | UI UAT + 一次完整月界 | feature flag 回 legacy |
| S3 周巡检/Duke | shadow；新报告不驱动 apply | 一次真实周报与 Duke 决策 | 恢复旧 ledger/filter |
| S4 recommend-only | shadow；只给建议 | bridge 守恒、risk floor | 停建议产物 |
| S5 single canary | shadow；单 consumer apply | 7 天/10 outcome | 指定 backup-id rollback + verify |
| S6 authoritative | ComputeCost 成为唯一计算真源 | S0-S5 全过 + 切换演练 | 重启 legacy writer、回放原始输入、切回 legacy readers |

权威切换的原子顺序是：冻结并校验最后一份 shadow 对账 → 停 legacy writer → 启用
ComputeCost readers → 启动只读 compatibility projector。兼容 projector 只从 ComputeCost 生成旧字段，
不得继续旧算法；保留一个月后才删除旧字段。

兼容月内恢复顺序是：停止 ComputeCost recommendations/readers → 冻结异常 snapshot → 重启保留的
legacy writer → 从保留的原始输入回放并完成守恒检查 → 切回 legacy readers。兼容月后恢复需部署
最后一个 legacy release 并从原始输入重建，不承诺即时回滚。

任何 secret 泄漏、守恒失败、高风险错误、静默错误或权限越界立即停止 collector、建议和执行面。
secret 事故还必须隔离受影响 snapshot，由凭证 owner 吊销/轮换 key；脱敏反例、secret scan 与只读
probe 全过后才能恢复。

## 9. 关键设计风险

- OpenRouter activity 是按 endpoint/day 聚合，不能单独提供 PULSE Agent/task 归因。
- ChatGPT Pro token 与 outcome 覆盖不完整时，只能展示已覆盖部分。
- 模型 alias 和价格会漂移，必须保存 canonical slug 与价格快照。
- outcome 关联缺失时不得启用按结果优化。
- 现有 runtime Spec 有失效测试锚点；在控制接入前必须先恢复其 spec-anchor 为 `0` broken。

## Physical Host continuity within an execution epoch

An SCL execution epoch can retain its logical frontier while an authorized new Host
Session takes over. Native measurement intervals are keyed by the attached Host;
each new interval begins prospectively at that physical transcript's current prefix.
The old immutable baseline/seal/attribution remains independently addressable. A
same-Host restart reuses its existing interval. Missing historical final evidence
remains a coverage gap and blocks eligible terminal measurement completeness.

The extension reads sealed SCL rejection envelopes even when the CLI exits nonzero.
Only safe typed codes and retryability cross into Relay. Deterministic fact/epoch/
authority conflicts are not runtime transport failures and must not schedule endless
activation retries. A committed attachment remains visible when activation fails.
No actor may erase a baseline, impersonate the prior Host, or fabricate zero historical
usage to recover a conversation.


## WI568 Stats integration repair — 2026-09-14

- Already-versioned `receipts` transport preserves source `fact_ref`, `supersedes` and provenance; it is not a raw snapshot to be reidentified. Replayed ancestors and local evidence refinements must not strand later source revisions. Missing predecessors still reject the atomic batch; recorded-at watermarks remain immutable.
- Codex receipt materialization retains proven native task/counter intervals. An absent interval on an already-ingested final quantity may be filled by exact same-assertion evidence; known counters/models/boundaries cannot be silently replaced. Unknown or crossing intervals retain explicit unallocated disposition.
- One explicitly registered Codex source can supply `paths` instead of `path` to reconcile active/archive copies together through the existing adapter. Other source kinds remain single-root. No automatic whole-machine discovery or new ledger.
- Pi/Codex Host model claims require native evidence whose per-model quantities conserve the same settled result. Pi uses native assistant usage, Codex the exact returned thread and its immutable pre-dispatch prefix/new task. Configured or last-result models are not historical evidence; unsupported compaction, ambiguous switches or conflicting boundaries stay unknown.
- Regression and real-replay evidence: `Bigbird/ComputeCost/uat/wi568-stats-cost-chain-20260914.md`. Production source activation and the Stats-bundled ComputeCost release remain separate gates, coordinated with WI300/#525. This repair does not close the full WI568 program or waive S5.


## Effective runtime sources and native coverage (WI568, 2026-09-15)

The existing private registry accepts a runtime_manifest selector plus explicit profiles for Pi and Harness. Registration verifies the inspected Host receipt and Harness roots; collection resolves the selector on each refresh. Previously declared direct/native history is retained, but no HOME discovery is implicit. Inactive nodes' installed manifests are historical declarations, not running-service proof.

Coverage receipts may carry optional source_detail diagnostics; native inventories use the existing coverage source_ref/watermark/evidence fields, not a new receipt kind or charge. source_detail contains bounded labels, read/error counts and the last observed quantity timestamp, never source paths. The pi_native_inventory.v1 producer binds a complete parsed Pi prefix to opaque native request identities in evidence, its native session in source_ref and the physical prefix digest in watermark. Reconciliation must validate those native facts and exact Host quantity conservation before suppressing an overlapping summary. It never assigns a WI merely because a summary matches. Legacy inputs without the witness retain conservative overlap handling.

Attribution receipts may carry an optional assignment_ref (and execution_ref when genuinely observed). The native boundary importer validates the existing v2 snapshot, immutable baseline, byte digests, native session hash and canonical or frozen Host assignment. The same request may gain a late attribution without altering its quantity, inventing an execution attempt or changing prior ingestion watermarks.

Stats consumes the original query's last_usage_at, unassigned_event_count, unassigned_known_total_tokens and source_details. These are separate from generated_at/expires_at. Procurement and the three non-cost panels remain unchanged.


### Runtime 子调用的表格投影

`runtime_subcall.v1` 是既有 actor attribution 的可选上下文，不是新增用量、WI、执行授权或第二账本。
它以不透明的父 native scope、工具调用和子 native scope 记录调用关系，分别保留实际父/子 runtime。
来源入口与 WI 独立：只传递已证根入口，不从父会话当前 WI、cwd、时间邻近或提示词继承任务和 Agent。
历史更正保存在 `reviewed-activity.json` 的有限 `subcalls` 集合，逐条绑定原 fact_ref，标记 reviewed_launch；
不得扩大到同一 session 的后续请求。自动关联只接受已登记 Codex 原生工具调用与返回的子 runtime UUID，
仅对调用开始至结果返回内的原生用量生效。数字型工具进程编号不是子会话身份。

表格保留八列和现有联合分组。Runtime、模型、金额仍来自原始叶子用量；入口旁标“子调用”，
同组混有直接调用时标“含子调用”。详情显示来源链和摘要凭据。完整原查询保留全部调用关系，
App 最多携带五项并同时保留完整数量和摘要。产出继续引用 WI 的 Outcome，不按子调用次数增殖。
重复或相互冲突的父关系不得标为已证子调用；分类不得改变旧水位、Token、价格或 WI 未知边界。

本切片支持有原生 UUID 返回的直接 CLI 调用，以及 literal custom_tool_call 执行封装。
无法静态确认的 shell/动态程序、仅输出数字进程编号、没有子用量记录、跨节点调用或缺少父入口时，
保留缺证，不按命令名称默认归类。无持久化会话且未采到原生用量的调用不能声称计量已覆盖。
新格式只在所有 ComputeCost 读取端通过兼容检查后启用；Stats 仅消费版本化投影。

### 原生输入回执（`native_input.v1`）

持有原生文件的节点可把归属阶段所需的输入记为 `native_input` 事实，由读共享观测流的采集器统一判定（`native_inputs.py`）。payload 为 `input_class`、`source_ref` 与按类别封闭校验的 `value`；身份是（类别、payload 摘要），因此同一输入重复推送是重复事实，不产生新版本；不得带 supersedes，证据至少含来源引用。值只含不透明引用、节点、入口与时间，原生 ID、路径和正文不入流。现有类别只有 `runtime_launch.v1`：一条原生工具调用与结果配对出的子调用（同 `runtime_subcall.v1` 上下文，basis 必须为 `native_tool_result`）。采集器在启用 `runtime_subcall.v1` 时，把本轮自己读出的启动与流中全部启动输入去重合并，再做启动归属与子调用用途，结果与在持有转写的节点上判定相同。输入是采集证据，不是活动：查询准备前即剔除，报表与无输入时逐字节一致（流摘要与入账时间除外）。读取端先随本特性部署；写入端须等全部读取端支持 `native_input.v1` 后再由登记表启用。
`@anchor test ComputeCost/tests/test_native_inputs.py :: NativeInputTests.test_collector_without_the_transcript_judges_the_recorded_launch`

请求用途读取的结果是“快照”类输入（`request_inputs.py`）：`codex_request_input.v1`（按目标用量，Codex 请求输入与其原生声明）、`session_declaration.v1`（按原生会话，Codex/CC 会话 WI 声明）、`cc_request_binding.v1` 与 `pi_request_binding.v1`（按用量，逐请求绑定）。value 为 `{key, data}`：key 是不透明引用；data 按类别校验完整形状（必填字段、类型、引用关系，如 Codex 输入与声明须指向同一 key），每个对象键须在该类别的允许表内，每个字符串须是不透明引用、WI、`none`、64 位摘要、时间或已知协议常量；不合格的输入不能入流。每个类别、每个 key 是一个实体，`occurred_at` 是持有节点读到该值的时刻。写共享流的采集器把收到的快照对该实体的最新读取做版本判定。最新读取是当前头的读取时刻；之后若又读到相同值，写入端在私有读取水位（每个实体一条，随入账持久保存）里把它推后，不追加内容相同的版本。早于最新读取的输入（重传或乱序到达）不追加；更晚且值相同只推后水位；更晚且值不同则 supersedes 当前头，因此 A、B、A 依次读到时以最后的 A 为准，A@0、A@2 之后迟到的 B@1 不采用；与最新读取同一时刻而值不同的并存为冲突，该 key 不采用。Codex 请求输入的声明须与所附输入同请求、同 scope、摘要一致、协议与来源相符且不早于捕获时刻，与原读取器的关联条件相同；Pi 未声明请求的 WI 列表为空。快照类别允许 supersedes，其余类别仍不可修订。采集器对本轮自己读过的 key 只用自己的读取；其余 key 用记录的值，配上流中这些会话或用量的当前版本，交给原阶段函数判定。Pi 绑定在记录前把（会话、条目、活动）换成用量实体引用，原生 ID 不入流。
`@anchor test ComputeCost/tests/test_request_inputs.py :: RecordedRequestInputTests.test_cc_request_bindings_and_session_declarations`

持有补充与 Codex 根会话链接同样按快照记录。`holder_session.v1` 以原生会话为 key，data 为 mode、请求起点与候选会话键（对导出中每个执行者算一次 `session_key`）；原生 ID 只在持有节点参与计算。判定节点用自己登记表中的内核导出匹配：候选键中恰有一个有写入，该会话即进入持有补充，规则与本机读取相同；写入在会话文件不再变化之后才出现在导出中，下一轮仍能补上。本轮本机读过的会话（含未匹配或因多个键都有写入而歧义的）只按本机结果判定，记录的候选不采用；两类 data 同样按形状校验（mode 与起点一致、键为不重复摘要；链接须有父用量、两端 scope 与至少三条证据）。`codex_root_link.v1` 以子用量为 key，data 为父用量、父子 native scope 与证据。本机读出的链接仍要求父子两条用量在本轮由同一已登记来源读到，本轮本机读过的子用量不采用记录的链接；记录的链接改为要求父、子两条用量的当前版本证据中都含记录它的来源引用（2026-11-15 大鸟定），满足才交给根请求归属。
`@anchor test ComputeCost/tests/test_holder_root_inputs.py :: RootLinkInputTests.test_link_counts_only_when_both_current_usages_carry_its_source`


Relay Pi 的 Actor 与 WI/用量终结分开：已验真的输入摘要和同节点 Host 历史 principal 映射，
可以在原生用量出现时确认 Agent，不等待 Host 汇总或 WI 授权，也不依赖可能来自旧机器的 cwd。
复用原 Host source 的 alias 历史，不复制第二份登记；按原输入时间选择映射，缺失、未来或跨节点
映射不补值。新归属只追加 actor 凭据，原用量、旧水位及任务权限不变。


Codex 子调用补充：原生工具 literal `text(await tools.exec_command({...}))` 与单一绝对路径 `cd … &&` 前缀可静态解析；不执行 JS 或 shell，不接受动态表达式、多段命令或数字进程编号。Codex 文本回执仅识别原生启动头中的唯一 session id，并与既有子用量和调用区间核对；提示词或模型正文中的同名文本不是身份。调用链本身仍不继承 Agent/WI，有限历史任务更正独立保存原发起、结果和 SCL 证据，不签发执行权。

有限历史 WI 更正针对原始整条用量，其生效时间采用已记录的 interval_start；无区间时保留原 occurred_at。原生产者误用结束时间的同一条声明仅在 WI、主体、目标和原始凭据一致时追加版本修正；不同任务或主体仍保留冲突。重放沿当前修正头幂等，旧水位与用量不变，不生成执行授权。


已审核的完整原始请求用途必须与同一用量的 canonical 弱归属推断一起读取。若清单未携带其引用，ComputeCost 仅按该用量派生的既有 SCL attribution identity 定位唯一原始、无 WI/Agent/执行授权的 ambiguous 凭据，并沿原版本链发布更正；不按当前 WI、会话或时间猜归属。迟到弱推断和旧凭据重放不得撤销该有限更正。明确分配、不同 identity、并发分支或新增请求仍不被更正覆盖。全量测试必须保留相关 SCL 凭据，不能只用 App 详情中的 actor 引用构造样本。


### 原生重试分段的计数基线

Codex paginated history 的每条边必须通过原逻辑线程、物理分段、字节截止和 ordinal 截止验证。重试分段在截止前没有计数时，沿其已验真的 history_base 继续读取原始祖先，最多 16 条边，循环、缺祖先、歧义副本与错误截止仍保留 unknown。首条累计值与截止基线完全相同是重复通知的零增量证据，即使 last_token_usage 仍描述前次回复；实际增长仍须与 native last 精确守恒。只修正计数解释，不创造新用量、业务归属或执行权，旧水位原样保留。


### 原生计量绑定不等待 Token 封存

Codex 的既存计量记录可独立证明本机原生 Host 与 WI 的计费用途，不要求它仍是主执行附件。读取端只在同一 active-lineage SQLite 只读事务中验证原生 Host、逻辑源、surface、计量摘要、开始时间和 prospective_exact_lineage_no_backfill 边界；baseline_pending 只表示数量基线未就绪，不能丢弃已记录的用途身份。有效结束、后继 epoch 与终态继续限制区间；跨任务重叠仍为歧义，不能取最后写入者。该证据不恢复执行权、不生成用量、不把计量覆盖声明为完整。


已审核的普通任务标记不是不可更正的真相。后续发现原生正式委派时，可针对原始用量和精确旧 non_wi_activity.v1 / none 凭据作有限更正：必须显式引用旧 fact_ref、原始用量区间和 native_lifecycle_request 证据，保留旧水位与原数量。不自动覆盖 cron、另一明确 WI、独立判断或未来请求；计费用途更正不修改执行权。


### 原生委派回合的准确用途关联

Codex 子回合只有在原始 task_started.root_turn_id、从当前子线程到根线程的完整原生父子链和同一次根请求用量同时存在时，才能关联该根请求已由 SCL 精确证明或由当前原生请求显式申报的用途（WI 或明确非 WI）；子区间须完全落在根请求已观测区间内。root_turn_id 不得与直接父线程拼接；根线程解析、归档版本选择和字节相同的继承前缀排除复用已有用量适配器。若原生上下文回放带入已验证祖先的启动记录、但不再字节同前缀，必须再由原生 item_completed 的 thread_id 与 turn_id 逐回合证明本地所有权，且凭据不得早于该回合开始；祖先记录本身不改变本地身份，也不能替后续回合提供所有权。未知外来启动记录、本地根身份冲突、同回合多所有者、缺少本地所有权仍保留 unknown。仅有会话当前 WI、同目录、名称、时间相近或提示词中的 WI 不足以关联。根请求缺失或歧义、父链不完整或循环、明确根身份冲突时保留 unknown；子 Agent、Token、金额、执行权不随用途关联继承，独立相反归属继续报冲突。回归覆盖两层委派、继承上下文、缺链、循环、相反根证据，以及原用量、金额、旧水位和重复处理守恒。

原生委派用途关联在 collector 完成既有凭据及有限更正合并后执行，不在单文件解析器内提前裁定。父子用量必须同时来自本轮同一登记来源；既有相反用途审核、缺失父请求及跨来源拼接不得被绕过。


## 原生计量通知的日历归期

一个 Codex 回合可跨自然日；有原始逐次 token_count 通知时，保留整轮用量 identity、原区间与数量，并附可选 native_metering.v1 精细证据。每个点使用原生计量通知时间和完整累计差分，四类数量之和须等于原聚合；不按运行时间比例拆分，也不把全部数量塞到结束日。这里的日历口径是原生已报告用量的发生时间，不推测 Token 实际计算的墙钟时间。

无时间细证、缺字段、计数下降、分区不守恒或损坏时仍保留 time_unallocated。日历窗口为半开区间，选取属于窗口的实际通知增量；全 WI / 无窗口查询仍保留原始总数。event_count 表示该窗口内参与的原观测数，不应将跨窗口的观测条数相加当作独立请求数。纯日历分组在无法保留一条观测唯一分组时仍显示跨桶不确定，不能伪造唯一桶。

所有三个实际读取端须先声明并实际支持新字段，现有 Stats 激活事务再开启该 feature；原凭据不改写，新时间证据仅修订完全同身份、同数量和同区间的原记录。并发相反时间证据仍冲突，旧水位保持不变。


### 原生直接请求的明确计费用途


#### MCP 会话 WI 登记

工具 `declare_session_wi` 只接受 `wi`，取值为一个 WI 或字面量 `none`。面向 Agent 的完整调用说明是：“登记当前 session 的归属，调用一次即可。要改文件、提 PR 或动生产时登记所属 WI；闲聊、只读查询、解释说明登记 none，不要为此新建 WI。”声明 none 的用量归为非 WI 活动（`policy: non_wi_activity.v1`、`work_kind: none`），不计入“未关联 WI”；先声明 none、后声明 WI 时按 WI 首次声明时间切分：之前的用量归 none，之后归该 WI（CC、Pi 按请求，Codex 按 session）。none 表示未声明 WI，不是反证：同一用量另有持有补充或阶段成员关系指名一个 WI 时归该 WI，指名多个 WI 时为 ambiguous；两个不同 WI 的处理不变（Pi、Codex 为 ambiguous，CC 按声明时间切换）。内核 PR 作者核查不把 none 计入会话声明的 WI。Agent 决定何时已明确，不要求首轮登记、不逐请求分类；Relay 不判断用途。开启 `sessionWiGuard` 的 Agent 另有提醒与写操作拦截（见 `Relay/specs/relay-runtime-pool.md`），它们只读取下述声明记录，不产生登记。支持的运行时为 Codex 与 CC（Relay 驱动的 Claude Code CLI）。

Codex 登记当前原生 session，即当前线程，不是根会话或子任务集合。宿主提供身份，Agent 不填写。Codex 登记可发生在会话中途；同一 session 前期及后续用量关联这一个 WI，新 session 不继承，相反声明保持冲突。持久 CC 会话可承接不同任务，现行 CC 用途按下文逐原始请求限定；旧 session 确认仍保留为原始凭据，不能按最后一个 WI 覆盖整段历史。

工具返回无状态的 `codex_session_wi/v1`，不写账本或生命周期、不读取输入副本或对话原文。collector 核对登记来源中的真实工具完成记录、当前线程、调用时轮次及结果；复用原生归档与祖先校验。只有真实原生 session 相同的用量才匹配，其他 session、祖先复制内容和单独文件不构成登记。

CC 不向 MCP 服务传递 session 身份。服务以 `--host cc` 启动，拒绝 Codex 身份元数据，返回不含 scope 的 `cc_session_wi/v1`。collector 从 CC transcript 读取 `mcp__pulse-request-purpose__declare_session_wi` 的 tool_use 与配对 tool_result：二者 sessionId、isSidechain 与 sourceToolAssistantUUID 一致，结果为非错误且与工具确认逐字匹配；原生 session 是相关范围，现行用途另核对原始请求祖先。新 CC 工具结果为 `cc_request_wi/v1`，旧 `cc_session_wi/v1` 继续只读兼容。只有主 transcript（文件名即 sessionId）中本 session 自己的记录构成登记；其他文件中的复制记录不计，尚无结果的调用不计。子代理 transcript 与父 session 共用 sessionId，其用量随父 session 登记归属；子代理自己的调用不能登记父 session，记为 `native_mcp_sidechain_unbound` 诊断。错误结果表示服务已拒绝该调用（如参数不符），不构成登记，只记 `native_mcp_call_refused` 诊断，不影响同 session 的其他登记，也不把来源降为 partial；结果不符和同一调用的不同版本使该 session 声明失效并记诊断，不猜测。请求祖先回溯时，父记录须在同一 transcript 中排在子记录之前，或其原生时间不晚于子记录；CC 会把子代理的上下文附件追加在以其为父的记录之后。

Pi 用同名工具 `declare_session_wi` 声明请求用途，由 Relay 的 Pi 工作项扩展提供：参数只有 `wi`，结果为 `{"schema":"pi_request_wi/v1","wi":…,"origin":"agent_declaration"}`，不写文件、不授予或核查 WI 权限。来源配置 `session_wi_mcp_since` 后，collector 从 Pi 会话记录读取 toolCall 与按 toolCallId 配对的 toolResult：参数只有合法 WI 或 none，结果非错误且与确认逐字一致，两条记录沿 parentId 回到同一条 user 消息，即为该请求的声明。请求内全部 assistant 用量按声明归属（`pi_native_request_purpose.v1`），同一请求声明多个 WI 记 ambiguous；未声明的请求不产生归属，保留原有证据；被拒调用只记 `pi_declaration_refused` 诊断。回溯遇到压缩或分支摘要即停止，不跨越。执行绑定给出的 WI 与声明不一致时按既有冲突规则保留争议。

来源以 `session_wi_mcp_since` 显式准入该时间之后的新登记；这是声明的准入边界，不把 session 用量截为“仅登记后”。旧逐请求工具返回不得提升成 session 声明。未启用时生产不改变；普通来源或 reader 回滚不得静默删掉既定边界。独立明确归属、PR916 历史更正、原用量、价格和旧水位保留；新 session 声明只沿自己的 attribution 版本链更新，不取得执行权。

安装时从实际 MCP 初始化响应读取同一句规则，并登记到运行时原生常驻提示。Codex 使用 `developer_instructions`；已有不同指令不得覆盖。CC 由 Relay 在启动时以 Pi manifest 中唯一的 `computeCostHost`（python 与 pythonPath，与 CC 用量测量相同）实际探测服务的 initialize 与 tools/list，再为每个 CC 进程加入 `--mcp-config` 服务、`--allowed-tools` 工具名，并把同一句规则附加到 `--append-system-prompt`；资源缺失或探测失败只告警并跳过，不影响 CC 启动。工具配置与该提示在同一次配置摘要校验和原子替换中生效；每轮提醒只对开启 `sessionWiGuard` 的 Agent 生效。MCP 元数据单独暴露与运行时常驻提示的测试结果必须分别记录，不能相互冒充。

已核验的 session 声明在查询中可补齐同一原生用量的 SCL 无 WI/执行者区间歧义；保留原歧义凭据及旧水位，声明撤回后恢复原判断。独立明确归属、相反声明和凭据自身冲突不被覆盖。原生 codex_exec/exec 入口识别为 CLI，不据此猜测执行者。

验证必须覆盖：前期未登记→中途一次登记→后续多轮仍归属；未知不补 none；重复调用与采集幂等；并发 session 隔离；相反声明与原生身份冲突；数量、价格与旧水位守恒。模型是否只根据工具说明适时调用，单独做真实运行时测试，不用固定调用脚本假冒 Agent 行为。

#### 切换前的历史回放与尚未迁移来源

以下脚本及 Hook 规则仅适用于切换前或尚未启用 MCP 的来源。复用既有 ComputeCost attribution、non_wi_activity、不可变版本链及原生计量 identity。私有 native-requests 目录只保存原生 Hook 捕获的输入身份，而非第二本费用账；collector 是费用账的唯一写者。Agent 申报命令是无文件系统读写的纯输出，声明随同一次原生工具调用及返回值持久化。collector 必须核对登记来源内实际读取的逻辑线程、原生回合、精确 call/result、输入摘要和声明字段；用户/模型正文、未配对输出和单独的 purpose 文件都不是声明证据。原生 code-mode 中，首条直接 await 并打印的明确申报可与后续操作共用外层调用；只接受原生结果序列首个已完成、exit_code=0 的 exec 结果，并继续校验同一请求、调用、输入摘要与声明字段。不执行后续程序、不搜索后续输出来替换首条失败结果，也不把动态表达式或缺少位置边界的合并文本当作凭据。多个相反声明保留为歧义，不以最近写入覆盖。此路径不需要扩大 Agent 写权限，也不增加服务或修改原生信任设置。只有本轮同一登记原生来源实际读取的用量可与输入相联。读取在解析缓存之外、历史有限更正之后进行；请求声明只能沿精确的无 WI/执行者 SCL ambiguous 推断链更正，不能抹除独立强声明。另一条新请求不继承声明；同一聚合回合中的新 steering 尚未声明时撤下此前整轮用途判断，不按最后写入获胜。原数量、Agent、授权、旧水位和历史审核清单不改变。

验真必须从新的 Hook 输入、明确声明、实际 collect_registered_sources、账本、query 贯通；覆盖连续新普通请求、同会话双 WI 的逐请求选择、重试幂等、原水位保留、未申报、不同用途 steering、跨 Host 拒绝、相反强声明及不安全输入降级。任何 fixture 成功都不等于 Hook 已实际安装或生产异常清零。安装需沿既有部署路径，显式登记本机来源与私有输入目录，先验证读取端再启用 Hook，不修改原生 Hook 信任设置、不侵入生命周期 hook、不自动创建 WI。


原生用途 Hook 的安装不得修改原有 Hook、审批策略或信任存储。Stats/request_purpose.py 仅在已选择的精确兼容 reader 下，以审查过的原配置摘要执行本地 CAS 登记；双文件安装失败只回滚本次原字节，遇并发写保留 rollback pending。配置成功仍为 configured_pending_native_trust_and_invocation；必须再读原生 hooks/list 与实际调用证据，不能用配置存在代替运行成功。


#### 共用的派生归属与数量守恒

父请求的当前用途证据失效时，已发布的子用途不能保持旧的 assigned 判断。同一次采集中仍能验真同来源父子用量与原生 root 关联时，沿本生产者原有 child/parent attribution identity 撤回派生声明；补齐同一输入声明后沿同一版本链恢复。不得修改独立子归属、数量或旧水位，不把来源读取失败或链接缺失伪装成有效撤回证据。测试须覆盖先成功再出现未申报 steering、相反父用途、明确非 WI 的撤回及恢复，并核对重复采集幂等和原数量金额守恒。


请求用途补充只处理本轮原生用量实际存在的请求。旧路径只读取已有 Hook 输入凭据的请求；MCP 路径以原生输入为凭据，缺输入时只撤回自身旧声明，不推断用途。完整用量采集和来源覆盖检查仍独立保留，不能据用途缺失删用量或补“无”。Codex SessionMeta.id 是当前线程，session_id 是根会话，子线程二者不同不构成身份冲突；用途仍严格核对当前线程、文件身份、原生回合、工具调用/结果和输入摘要。无关子线程的继承上下文不得让另一条新请求的用途来源失败；适用请求的损坏输入、相反声明和当前线程冲突仍须拒绝。回归须覆盖无输入不解析声明、不同根会话、无关继承头、适用头冲突及金额/数量/旧水位守恒。

生产启用 session WI 读取复用 Stats/deploy.py 的 `enroll-session-wi`：先选定兼容读取版本，再对唯一既有 Codex、CC 或 Pi 来源以原配置摘要核对、在现有发布锁内登记准入时间；CC 来源要求读取端支持 `cc_session_wi/v1`，Pi 来源要求支持 `pi_request_wi/v1`。Pi 节点的 Relay 扩展提供 `declare_session_wi` 后，该节点的 Pi 来源即须登记；未登记时其声明不被读取，用量只能经持有补充、复核分类或保持未归属。原来源身份及其它来源、账本、Hook 不变；重复操作幂等，已登记边界不能移动。真实 Agent 测试必须显式指定已登记执行者的既有工作区，临时目录只保存测试结果，不再作为匿名执行工作区。


### CC 请求汇总与持久累计计数

`result.usage` 与 `modelUsage` 不同：前者在流式多轮中只计本轮主 Agent，后者是含子代理和恢复前消耗的会话累计值。官方依据：https://code.claude.com/docs/en/agent-sdk/cost-tracking 。已核验 2.1.277 及之后的 2.1 引擎通过原生版本证据启用累计解释；未知或更旧引擎不推定具有相同恢复语义。原 Host `cc_sdk_result.v1/turn_aggregate` 字段和原始计数保留在账本，由唯一 ComputeCost 查询作有证据的读侧解释，不重写历史，也不使用 SDK 的美元估值替代固定价表。

累计投影按实际 native scope/model 分组，要求原生明细前缀与第一份累计值四类数量完全相等，随后累计值不回退且无不同同刻终值、未解决数量分叉、未知数量或相反 WI/操作者。不能仅凭 WI 同名、时间接近或取最大值建立关联。不同 session、模型、重置及不明恢复起点不合并。终值同时覆盖父子用量，原生请求按稳定身份保留一次；以最新累计值减去其覆盖的已观察原生明细得到差额，按相邻两份累计值分段生成显式 `cc_cumulative_residual.v1` 读侧差额：每份累计值处的已知差额取其后各份累计值处测得差额的最小值且不低于零（明细可能早于或晚于其累计值写入），相邻两份之差记入该区间，零差额不生成记录。各段非负，合计与总差额逐类守恒，其他累计快照只作凭据，不能再次收费。差额不分摊给具体子代理或猜测调用次数。

单轮差分可能因计数回报与原生时间错位出现负数，不得补零。未展开差额保留“最后一次明细前缀与累计值完全相等的观察点—最新累计观察”的粗时间区间；跨日时不能冒充今日精确用量，跨周/月同理。差额的 WI/Agent 必须是原家族一致归属；节点与入口只有在覆盖范围内所有原生明细一致且不与原累计凭据相反时继承。未来请求不污染已覆盖范围的判断。未知仍未知，异常仍单列。

未满足上述累计证据条件、也不满足下述逐次增量条件的 `cc_result/cc_sdk_result` 继续走原覆盖核对：只有真实执行、同 attempt/model 的明确 covers、已知数量与原生明细一致时才作有界去重；缺测、相反最终计数及不明覆盖关系不得借累计路径消除。保留原事实、价格凭据和全部旧水位；本机制不签发 WI/执行授权，也不增加 MCP 调用。

Host 对每个模型各记一条结果，模型是重叠判断的条件：已知模型不同的汇总与明细是不同的 provider 消耗，不互相隔离；仅模型未知时保留按会话、时间的保守重叠判断。

`cc_sdk_result.v1` 取自 `modelUsage`，永远是整场会话该模型的累计数，不是单次 attempt 用量。未被上述原生前缀累计投影处理的同一 native scope/model 结果，查询按时间排序后只计每次相对同会话上一份结果的增量：区间是“上一结果—本结果”，区间内同模型的消息明细（原生或 Host 流式快照）照常计入，增量减去这些明细后的非负余量以 `cc_cumulative_residual.v1` 记入该区间，零余量不生成记录；结果本身不计，也不再与其 covers 作单次汇总核对。余量任一类为负表示明细与计数时间错位（如被取消请求的流式快照未进入计数），不补零、不生成记录，下一份结果从最后一份已核平的结果起算。

- 只有结果、没有任何消息明细的模型（CC 自身调用的辅助小模型不写入原生 transcript）：第一份累计值起点未知，以 `session_counter` 粒度记为无法归期。
- 有消息明细的模型：至少要有两份结果；第一份结果只作起点，此前用量只按明细计，不推定不明恢复起点。只有一份结果时继续走原覆盖核对。
- 结果列出的 covers 不在同 scope/model 明细内、明细数量未知、同 scope/model 混有其他汇总类型时，继续走原覆盖核对。
- 结果缺任一类数量是 `cc_counter_fields_missing` 冲突，整组不计；同刻不同值是 `cc_counter_regressed` 冲突，整组不计。
- 累计值比上一份结果低（如 Relay 重启后续接的累计值略降）时，在该处切成新的一段：这一份结果的增量未知，只有它记 `cc_counter_regressed` 冲突；它作为后一段的起点，后续结果相对它计增量。断点前后各段照常对账，断点与上一份结果之间的消息明细照常计入。

`@anchor test ComputeCost/tests/test_cc_model_counters.py :: test_counter_regression_splits_the_family_at_the_break`
`@anchor test ComputeCost/tests/test_cc_model_counters.py :: test_itemized_counter_after_a_break_differences_from_the_resumed_baseline`

`reconciliation.cc_unitemized_counters` 列出参与计数的结果、起点（`baseline_fact_ref`）、最后核平的结果（`reconciled_fact_ref`）、已计明细合计（`itemized_tokens`）与生成的差额。

### 同一 Host attempt 的重复 CC 最终结果

同一 attempt、同一模型的两条最终结果内容一致时是同一事实。恢复会话的 CC 进程可能先为排队的回合（如后台任务通知）给出不含 provider 请求的结果：本回合用量全为零、不列消息，`modelUsage` 是本 attempt 首个请求之前的会话累计值；随后才是本 attempt 自己的结果。Relay 不记录前者（见 Relay `collaboration-capture.ts`），因此每个 attempt 只有一条最终结果。对已在账本中的双写，查询仅在以下证据之一成立时把不列消息的那条认定为前置结果、保留另一条：其计数等于更早另一 attempt 已记录的同 scope/model 结果；或另一条结果列出的消息全部晚于它出现且各类计数不小于它。其余不一致仍是版本冲突。单独成 attempt、不列消息且区间内无同模型明细的结果，若计数等于同 scope/model 最近一次更早结果，表示该 attempt 未发生该模型请求，不计费也不报冲突；计数上升按上节逐次增量计入。不满足逐次增量条件时，计数上升而区间内无明细仍报 `summary_without_detail_in_interval`。


### Native source admission and calendar continuity (WI300, 2026-10-01)

MCP-specific thread validation applies only to sessions with a native session declaration at or after the enrolled admission boundary. All copies and ancestry of a declared session remain validated; absent, withdrawn, failed, contradictory or owner-mismatched declarations never become assignments. Other native history still passes through the existing usage adapter.

An explicitly enrolled `codex_calendar_months.v1` source retains its existing `paths`, source ID, archive, actor rules, ledger and MCP boundary. `calendar_root` names an already registered native dependency; `calendar_from` names an already enrolled YYYY-MM directory. The reader includes that month and later YYYY/MM children, including new years, without scanning earlier months or another profile. Unsafe directories fail visibly. Ordinary rollback cannot silently remove the selector or choose a reader that cannot honor it. This changes source enumeration only; it grants no WI, Agent identity or complete-coverage claim.

### Collector snapshot concurrency and test isolation (WI300, 2026-10-01)

`collect_registered_sources` submits its derived batch with the exact ledger watermark it read. `UsageLedger.ingest_receipts` compares that optional expected watermark while holding the existing ledger write lock, before any receipt is appended. A stale batch fails atomically; no source cursor advances, no quantity or contrary claim is rebased, and the next normal collection reads the new snapshot. Unguarded direct receipt imports retain their existing immutable-fact behavior. This prevents new concurrent collector forks; it does not declare existing forks resolved.

Relay unit tests must not inherit `PULSE_COMPUTECOST_RECEIPT_DIR` from a resident runtime. The existing Vitest environment clears that default sink; capture tests opt into explicit temporary directories. Mocked messages are not real provider usage. Actual model-test usage remains billable evidence and must not be removed by this isolation rule. No production Relay or MCP behavior changes.


### CC 同消息分块与 session 归属边界（WI300，2026-10-03）

Codex 的 `declare_session_wi` 仍在明确 session 所属 WI 后调用一次；CC 沿同一工具名对新的原始请求明确申报。消费端按原生消息 entity 合并内容块，不能因块的时间戳不同而生成多个相反的归属头。已重新核验的 session 声明覆盖该消息的完整既有观测，包括同一原生 scope、同一消息 identity 的 Host stream 起点；不把登记误解为从末块才取得执行权。只沿自身原有 attribution identity 更正，保留独立 WI/操作者声明、相反数量及全部旧水位。

旧分块归属分叉仅在同生产者、除时间外的字段完全相同、每个时间边界有该消息实际观测、原用途证据仍可核验时收敛。非自身、相反 WI/state、无证据边界不得借此覆盖。CC 操作者凭据采用消息首个原生观测边界；同区间内再次提供已知且相同的身份不是操作者变更。无早期身份或后续值相反时继续保留歧义。此修复不改变原始用量时间、数量、价格、MCP 提示词或 Relay 逻辑。

### CC 汇总下的未定稿原生明细

CC 汇总计数（显式列出 covers 或无 covers 而由 attempt 区间界定）不与其明细相加。已完成的 CC 消息可能只留下流式快照：Host 在流式阶段观察到它，原生 transcript（尤其子代理 transcript）只写内容块条目，没有 stop reason，输出计数停在早期值。这类消息与汇总不矛盾：每条明细都在原生 transcript 中、四类数量已知、runtime/model/native scope 一致、带 Host attempt 时与汇总同 attempt 与 coverage、WI/操作者一致，且明细合计不超过汇总时，汇总不计，明细全部计入；未定稿明细是下限，以 `provisional_usage` 标出，不改为 final，不推算剩余量。真实缺口由持久累计差额补足（满足其证据条件时）。缺覆盖、仅有 Host 观察而无原生记录、未知数量、不同身份或明细超过汇总仍为冲突。该读侧选择不改写原事实、时间、价格、版本或 MCP 规则。

### CC 会话中的工作目录变化（WI300，2026-10-03）

CC 原生消息记录的是当时 cwd；在已登记 Agent 工作区内切换到子目录，不应丢失操作者。仅对 CC，除已有精确文件夹/已登记 Git worktree 等价校验外，允许实际存在、共享同一 Git common directory、位于已登记相对工作区之内的子目录。跨检出目录时 Git 必须列出实际 worktree；原登记检出目录内核对共同 toplevel，兼容子模块管理目录与工作目录分离的布局。仓库根不作为整仓 Agent 通配。越界符号链接、嵌套独立仓库、同名复制目录、缺失目录或相反登记均不认领。该规则不改变 Codex/Pi 的 startup-cwd 边界，不凭 WI owner 或目录名推断身份，不改 WI、用量或价格。

### 已核验非真实用量的排除边界（WI300，2026-10-03）

模拟事件误写到生产来源后，原始事件和账本版本仍保留；仅凭未知 Agent、零数量、入口名称或缺少 MCP，不得排除。有限处置须核对原来源字节摘要、具体行和原生对象、测试夹具及原账本凭据，并追加现有 `attribution.state=excluded`，不改数量、价格或执行权。真实模型测试仍是实际成本。

已生效且无争议的排除须在用量重叠核销前应用，避免非真实汇总继续隔离真实明细；`reconciliation.excluded_observation_refs` 保留精确对象清单。相反归属、来源版本冲突、未完成真实明细与独立数量冲突不能借此消除。旧水位继续返回原判断。有限历史证据见 `ComputeCost/uat/reviewed-test-contamination-20261003.json`，该文件是已核验案例，不是通用文件名过滤规则。


### CC 本地通知不等于 provider 用量（WI300，2026-10-03）

原生 CC 的 `<synthetic>` 本地通知沿既有有界 CC sampler 语义处理；账本保留原观察，查询仅在明确 final、原生与规范四类计数全为零时不计作 provider 请求；相反 reported total 仍由原 Receipt 守恒校验拒绝。只覆盖这些通知、同 runtime/session 的显式零汇总同样不另收费。无模型、无 covers 的失败或取消零汇总还须由原 Host attempt/coverage/区间证明，且无任何可能覆盖的真实明细。已知真实模型的零值、缺数量、未完成消息、来源或数量冲突不走这条规则。诊断 `non_provider_observation_refs` 保留精确引用；不删除原事实、不补价格、不取消执行失败事实，不伪称来源完整。

### CC 原生消息的节点与执行时间边界

同一 CC 消息的 Host 流记录与原生记录合并后，请求区间可能早于原生输出时间。直接指向该消息的 `registered_runtime_actor.v1` 节点上下文在该请求区间内（含结束点）证明消息来源节点，不按后到的执行者转移处理；两个不同节点仍为冲突。仅此直接节点字段使用消息边界，执行者、入口以及经 execution 继承的上下文仍遵循原生生效时间检查。区间结束后的上下文、外来 producer/policy 和其它 runtime 不借此补齐；原事实、用量与价格不改写。

### CC 入口沿原始输入和消息祖先恢复（WI300，2026-10-04）

`cc_input_context.py` 在现有凭据采集器内连接已登记原生 CC 的 `parentUuid`／主子任务 `promptId` 与同轮有效 Relay 输入凭据。只有原始输入正文、摘要、私有存储、操作者、节点、Host dispatch/session 及时间边界一致，才追加既有 actor 凭据中的入口字段；不赋予 WI／执行权限，不增加用量，不用 session 默认渠道或文本中的单独引用填空。未获会话级渠道确认的无凭据输入、断链、不同内容副本或不一致声明保持未知；用途仍独立消费实际 MCP 声明，持久 CC 会话按原始请求限定。原生祖先边须有同一输入文件前缀中先父后子的记录顺序，且 session／主子链／agentId 一致；附件事件的时间戳可以早于其序列化父记录，不能据此切断已证明的 parentUuid 链。原始人类输入、dispatch、主子任务输入与实际用量的时间边界仍分别验证；不能借记录顺序回填未来输入或跨文件的未证祖先。

Relay 是会话执行者、入口与输入的声明方。运行时自行发起的通知回合（后台任务完成后 CC 再起的一轮）由 Relay 记为独立执行，其 `parent_ref` 指向该会话最近一次 Relay 派发；`_continuation_routes` 仅在父执行已有唯一入口证明、运行时与执行者一致、双方 Host 用量只指向同一原生 session 且子执行不早于父执行时，沿用父执行的入口与输入凭据，否则保持未知。输入证明中的执行者以 Relay 执行为准，消息 cwd 只作反证：登记给另一 Agent 的目录拒绝，未登记目录（如任务工作树）不构成反证。查询把同一请求的 Host 与原生观测合并为一行时，绑定在其中任一组成版本上的租户与身份证明都适用于该行，不跨到其它请求。

单个 Host JSONL 无效只使该来源保持 partial 并记 `host_stream_invalid`／文件数；不能阻止其它独立文件的正常读取，无效文件不能提供原始入口锚点。恢复漏读可能显露之前缺失的用量或冲突，必须独立对账，不能宣称降低入口未知就等于费用完整。

同一原生 CC 请求及其已证明原始 prompt 的子代理共用该请求的 WI 声明，新的独立输入不继承；保留逐调用用量，不把父汇总与子明细重复相加。例外是子 WI（父 WI 下派生、由子代理完成的 WI）：子代理 transcript 首条记录是派发 prompt（`parentUuid` 为空），其首行匹配 `^\[WI: (wi_\d{8}_\d{3})\]`，且内核 `stats-facts` 中该 WI 的 `parent` 等于本请求当时声明的 WI、用量发生时该 WI 已建立且未结束（状态为 active，或已结束但 `closed_at` 晚于用量），则该子代理用量归该子 WI，仍记 `cc_native_request_purpose.v1`，凭据增加 `cc_child_work_item`；请求声明 `none`、未声明或歧义，标记指向非子 WI、已结束子 WI，以及导出缺 `parent` 字段或不可读时，一律继承声明。Codex 子线程的派发内容在 rollout 中加密，读不到标记，始终继承。用户明确确认该原会话的入口后，在现有 `reviewed-activity.json` 保存一条 `scope=cc_session_ingress`、原始用量锚点、native scope、生效边界与用户证据；正常采集为同一 scope 的 CC 消息生成既有 route-only actor 凭据，后续子代理不重复申报。渠道证据标记为 reviewed，不伪造 Host input，不改变 WI、Agent 或 execution；其它 session（即使 WI 相同）、runtime、汇总、边界前用量及原生 scope 相反证据不借此继承，独立相反渠道仍显示冲突。

### 冲突诊断使用本分组的依赖范围（WI300，2026-10-04）

`collaboration_query.report` 对被拒绝的用量保留原身份与 actor 证据，诊断不把这些用量重新计价。版本分叉、重叠核销和无法归期记录按本桶分类与各实际筛选维度判定；计数使用冲突身份并集，避免重复计算同一冲突。窗外、其他 WI／Runtime 的执行记录不使正常桶失效，直接 execution_ref 依赖例外；coverage 的既有全局边界不改变。真正相关 join_conflict 仍不提供金额，独立正常桶恢复原有 partial_machine_cash_usd，不改 App 定价规则或原始账本。摘要和每个分组的 `usage.conflict_reasons` 按原因计数本范围的冲突用量：`revision_conflict`（同一实体的不同版本）、`summary_detail_mismatch`（汇总与其列出的明细数量或身份不符）、`summary_without_detail_in_interval`（汇总区间内没有可对应的同模型明细）、`unproven_session_overlap`／`unproven_execution_overlap`（同会话或同执行内无法证明是否重叠）、`summary_without_cover_list`、`repeated_summary`、`cc_counter_fields_missing`（累计结果缺某类数量）与 `cc_counter_regressed`（累计值回退的那一份结果，或同刻不同值的整组）；`reconciliation.conflict_reasons` 给出每个被拒用量的原因。

### 同一 Codex 原生轮次的进展观察（WI300，2026-10-04）

历史采集可能缺少 supersedes 边，把同一轮的中间累计观察留下为多个并发头。查询仅在相同原生来源见证、entity/scope、开始边界、模型及业务身份全部一致，原生累计值减已分配量得到同一非负起点，且所有已知四类计数随原生时间单调时，将其解释为同一轮的进展。只保留有证据的最后观察一次；有 final 时必须唯一且同刻同值，无 final 时仍是 provisional。它不是按最大值选赢家，不更改原账本前驱、原始计数、价格或历史水位。不同 final、回退、缺数量、不同起点/来源/模型/身份/时间以及已带 metering 的记录不走此规则。

原始分叉保留在版本证据中；observation_groups 列出参与同一轮解释的所有 fact_ref。测试和生产证明须同时核对精确原生 token_count/terminal 记录、五条原始观察的守恒恢复、无关运行时不变以及真实 App 同水位消费；部分总额不代表完整来源或执行覆盖。

### 已恢复读取的历史测试污染（WI300，2026-10-04）

有限移交证据 `ComputeCost/uat/reviewed-test-contamination-20261004.json` 保存13份原始 Host 文件及46个模拟用量/执行断言。三套原有 mock 测试通过显式私有捕获重现六种完整事件图；仅对随机调用编号作保持关系的一一重命名，保留原生scope、固定启动指纹、数量、身份、状态、错误和事件顺序。`uat/reviewed_fixture_migration.py` 仅供该已评审清单的迁移验证，不注册为采集器或未来过滤器。

沿用已存在的 `reviewed_test_contamination.v1` 排除凭据；原文件、账本版本、真实模型测试成本和相反独立声明均保留。正式追加必须核对当前原始断言、既有发布锁与水位，并核验旧水位不变及重复追加为零。效果由原 WI 的正常发布/App同水位回执确定，不因文档或私有复算通过而声称已部署。

### 已记录的人审 v2 消费（WI300，2026-10-05）

同一 review adapter 接受 `work_item_review/v1` 与带显式 `outcome_waiver` 的 v2；v1 的字段与摘要不变。v2 核验原候选、验收指纹、人工确认、风险、来源和原 Outcome 状态／摘要关系，完整保留豁免见证；未知版本或破损绑定仍报来源不完整。规范真源为 `agent-harness/specs/human-outcome-waiver.md`。

单独的人审 v2 仅导入 `verification=human_waiver` 的 review，不伪造 delivery、独立 accepted、金额或新 WI 权利；真实 delivery 仍来自已有 canonical Outcome／明确 measurement。标准 WI 查询同时标明 `outcome_decision=human_waiver` 与原捕获的 Outcome 状态。注册来源、直接 review 文件和 Harness export 包络使用同一验证器；新记录重复采集幂等，原用量、历史、独立结论和覆盖分母不改变。

### 已验证历史的内存与摘要复用（WI300，2026-10-05）

同一 UsageLedger 实例内，完整校验交易顺序、原始交易摘要、每条凭据及私有文件边界后，保留每个交易前缀的 SHA-256 增量状态。当前和历史水位均由对应前缀结束数组计算原有 facts 摘要；自身成功追加仅扩展新凭据，不把全部旧 payload 解码后再次序列化。每次复用前流式核对原文件字节摘要以及身份、权限；同长度且同时间戳的覆盖也使缓存失效，重新执行完整校验。新进程不信任旧进程缓存。

反复出现的不可变字符串与 payload 字节可以在有容量上限、仅本次读取存活的内存池中共用；每条凭据依然独立校验、保留原 fact_ref、supersedes 与 evidence。池中不存身份裁决、不缓存跳过验证的结论；payload 属性继续返回独立可修改副本，缓存不写磁盘。不删除或压缩权威历史，不建立第二账本。

性能验收分别检查：同一完整水位与价格输入的摘要及三报表逐项相同；篡改、权限变化、旧水位、并发追加、失败后重试继续正确；独立进程 CPU/最大 RSS 与实际完整刷新计时。全机 swap 受其它进程影响，不能把单次总量变化当作程序自身收益。


### 已审计原生计量的重复采集持久性

关闭 `native_metering.v1` 仅限制新明细提取，不撤销已经审计写入的同一用量计量时间线。正常 collector 重读必须保留这一较强观测：仅接受相同 Codex producer、entity、occurred_at 及除 metering 外完全相同 payload；计量点须通过既有四类 Token 守恒与原区间验证。数量、模型、身份、范围、区间、时间或另一条已知时间线变化仍是独立冲突，不能继承旧明细或静默忽略新消耗。

已存在的粗粒度并发头只能在共同原生来源见证和完全相同用量下投影为已验证计量观测。`observation_groups` 保留参与的全部 fact_ref；collector 使用同一严格关系选择其重读基线。原始账本图、supersedes、事实内容及旧 as-of 水位不改写，通用 concurrent-head resolver 仍保留冲突。后续真实新回合正常采集。这一有限恢复不启用全局 feature、不补造历史数量、不删除原始账本。

`@anchor impl ComputeCost/pulse_compute_cost/native_metering.py :: metered_observation`
`@anchor test ComputeCost/tests/test_native_metering_persistence.py :: NativeMeteringPersistenceTests.test_natural_feature_off_collect_publish_repeats_preserve_reviewed_metering`
`@anchor test ComputeCost/tests/test_native_metering_persistence.py :: NativeMeteringPersistenceTests.test_existing_parallel_coarse_head_is_auditable_and_repeats_converge`

### 已退役来源

来源退役只由仓库声明 `ComputeCost/config/retired-sources.json` 给出，每项含来源 ID、类型、退役日期和人类授权摘要，可附不参与声明身份的说明。来源从登记表消失不等于退役；声明格式错误时采集失败。

collector 不读取已声明退役的来源，即使节点登记表仍列出它；退役的 Harness 根也不再提供执行授权，因此 Pi 来源不报 `authority_binding_unavailable`。`candidate_registry` 移除退役来源，其上的各登记生成随之收敛；`runtime_candidate_registry` 只刷新既有未退役的 Harness 选择器，不重新登记 Relay 仍导出的退役根。

对退役来源的每个覆盖实体，collector 追加一条 `source_retirement.v1` 覆盖版本：`status=excluded`、`reason=source_retired`，不带 `source_detail`。它的 supersedes 是 fact_ref 最小的当前头，evidence 列出全部当前头和声明引用；`current_receipts` 只把同一覆盖实体中被精确列出的事实视为已替换，退役版本因此成为并存头的唯一后继。原记录不改写；退役后才出现、未被列出的头仍是冲突，下一次采集追加包含它的新退役版本。payload、前驱和 evidence 与节点、时间无关，多节点重复采集得到同一事实。

查询不按节点标签拆分已有退役版本的覆盖实体。全局 excluded 不进入覆盖分母和来源明细，也不产生 `execution_evidence_conflict`；用量、归属和价格事实不变。旧读取端可解析该版本，单头实体同样被替换，并存头保持原冲突。

部署候选按退役声明去掉登记的来源时，ownermac 原生回退仍可用该声明之前准备的回退组：回退重新采用回退组的登记表，不认识该声明的旧 collector 会再次读取退役来源，只追加覆盖版本；候选再次运行时按上述规则为它们追加新的退役版本。

`@anchor impl Scripts/pulse_native_ownermac.py :: same_fact_sources`
`@anchor test Scripts/tests/test_native_ownermac_cohort.py :: test_declared_retirement_omission_keeps_a_prepared_fallback_compatible`

`@anchor impl ComputeCost/pulse_compute_cost/source_retirement.py :: retirement_receipts`
`@anchor impl ComputeCost/pulse_compute_cost/receipts.py :: current_receipts`
`@anchor test ComputeCost/tests/test_source_retirement.py :: SourceRetirementTests.test_retirement_is_the_only_successor_of_concurrent_heads`
`@anchor test ComputeCost/tests/test_source_retirement.py :: SourceRetirementTests.test_registered_retired_source_is_not_read_and_grants_no_authority`
`@anchor test ComputeCost/tests/test_source_retirement.py :: SourceRetirementTests.test_query_has_no_retired_source_detail_or_conflict`

### 覆盖分叉的显式恢复

覆盖实体有多个当前头时，collector 不选前驱，也不写新覆盖版本；该来源摘要带 `coverage_recovery_required` 和 `coverage_revision_conflicts`（全部头与本次快照）。恢复只由仓库声明 `ComputeCost/config/coverage-recoveries.json` 给出，每项含来源 ID、domain、wi、要放弃的头 `abandoned_heads`、恢复日期和人类授权摘要，可附不参与声明身份的说明。被放弃的头是经人类确认不属于该来源断言的版本，例如另一节点经转运收到、又被本地证据修订改基的副本。声明格式错误时采集失败。

该来源自己的 collector 下次采集时，若被声明的头中至少一个仍是当前头，且去掉它们后恰好剩一个头，就把本次快照追加为剩余头的后继：supersedes 指向剩余头，evidence 列出仍为当前头的被放弃头和声明引用。`current_receipts` 对 `registered_collector.v1` 与 `source_retirement.v1` 的覆盖版本一律把 evidence 中精确列出的同实体覆盖事实视为已替换，新版本因此成为全部头的唯一后继。原记录不改写，旧水位仍返回原冲突。声明未列出的头、剩余头不唯一或被声明的头都已不是当前头时，仍报告需要恢复。旧读取端只认 supersedes，被放弃的头对它仍是并存头。

查询的 `execution_evidence_conflict` 只计入会成为本分组 usage 或 execution 覆盖分母的分叉：至少一个头属于所选 WI，或是状态不为 idle/excluded 的全局版本。human、quality、value 覆盖分叉，以及全部头为 idle/excluded 的全局分叉，不影响成本分组。全局 usage 覆盖没有来源范围，其分叉对全部分组保持相关。

`@anchor impl ComputeCost/pulse_compute_cost/coverage_recovery.py :: recovered_coverage`
`@anchor impl ComputeCost/pulse_compute_cost/receipts.py :: named_coverage_heads`
`@anchor test ComputeCost/tests/test_coverage_recovery.py :: CoverageRecoveryTests.test_fresh_snapshot_succeeds_remaining_head_and_names_declared_head`
`@anchor test ComputeCost/tests/test_coverage_recovery.py :: CoverageRecoveryTests.test_undeclared_or_unmatched_heads_still_require_recovery`
`@anchor test ComputeCost/tests/test_coverage_recovery.py :: CoverageRecoveryTests.test_head_appended_after_recovery_conflicts_again`
`@anchor test ComputeCost/tests/test_conflict_report_scope.py :: ConflictScopeTests.test_coverage_fork_counts_only_where_a_head_is_an_applicable_denominator`

### 原件采纳不产生覆盖分叉

Ledger 收到 fact_ref 相同、evidence 更多的事实时追加证据修订，并把以原事实为前驱的后续版本改基到该修订上，所以转运副本的 fact_ref 与来源节点不同；两本独立演化的账本合并时，这些副本会成为同一实体的第二条链。因此原件采纳（把节点本地回执并入生产观察流）对覆盖版本只追加两种：观察流中尚无的覆盖实体，或当前头的后继。fact_ref 已在流中、前驱不是当前头或前驱缺失的覆盖版本留在原件文件中，采纳结果以 `coverage_versions_retained_in_originals` 计数，核验不因它们判为未完成。其它类型的事实沿用原规则，冲突照常留给查询。

`@anchor impl ComputeCost/pulse_compute_cost/observation_authority.py :: _CoverageHeads`
`@anchor test ComputeCost/tests/test_production_observation_authority.py :: test_adoption_never_forks_coverage_with_a_rebased_transport_copy`
`@anchor test ComputeCost/tests/test_production_observation_authority.py :: test_adoption_extends_current_coverage_heads_and_adds_new_entities`


反向丢弃粗观察与已有并行头投影共用非空共同来源检查；不同或为空来源不得因同量而消失。既有正向精度升级仍追加可审计的新事实和显式前驱，保留旧观察；其legacy空来源合同不被反向丢弃规则改写。两种方向及空/不相交来源须分别验证，不能把纯payload精度比较当作共同来源证明。

`@anchor test ComputeCost/tests/test_native_metering_persistence.py :: NativeMeteringPersistenceTests.test_revision_disjoint_source_coarse_observation_is_retained`
`@anchor test ComputeCost/tests/test_native_metering_persistence.py :: NativeMeteringPersistenceTests.test_revision_empty_source_coarse_observation_is_retained`
`@anchor test ComputeCost/tests/test_native_metering_persistence.py :: NativeMeteringPersistenceTests.test_registered_collector_retains_disjoint_and_empty_source_heads`


## 归属补充的数据流

规则见[声明优先的归属补充](stats-attribution-identity.md#声明优先的归属补充)。

| 环节 | 数据与约束 |
| --- | --- |
| 内核 `stats-facts` | 每个 WI 的 `session_writes`（执行者、提交时间、会话键），见上文 Outcome 段。 |
| 发布器 | 每轮先写 `wi-facts-v1.json`，再采集。 |
| 采集器（`holder_supplement.py`） | 登记表 `core_session_writes` 给出导出路径与本节点内核 `surface`，且 `required_receipt_features` 含 `holder_supplement.v1` 才启用；两者由 `Stats/deploy.py enroll-holder-supplement --surface <surface>` 在发布器锁内一次写入，选中的读取端须支持该特性。对 CC/Codex/Pi 来源的原生会话，以导出中出现过的执行者逐一重算会话键；只读写过 WI 的会话的请求边界。没有主张的用量得到以用量（CC/Pi）或会话范围（Codex）为目标的补充事实，之后随新写入修订。导出不可读时来源诊断记 `holder_supplement_unavailable`，采集照常完成。 |
| 探索（`holder_exploration.v1`） | 同一会话里既未申报、也未写入任何 WI 的请求，视作探索：其后第一条“申报或写入”的请求若新建了 WI（该 WI 创建时间落在那条请求内），这些探索请求归该 WI；若没有新建 WI，只有会话开头（任何申报或写入之前）的探索归为非 WI 工作 `none`，会话中途的未申报请求保持未关联；其后尚无这样的请求时保持未关联，已判定后不再成立的结论以 `unassigned` 撤回。同一请求的 Host 与原生双份观测按实体合并参与判断。只补无主张的用量，优先级低于持有补充，Stats 归属来源显示“探索”。仅覆盖内核记录过写入的会话。 |
| Relay | 调度运行写 `scheduled_session` Host 回执，字段见 [Relay 调度规格](../Relay/specs/relay-scheduler-jobs.md#4b-自动任务的成本归属记录)。 |
| `read_host` | 把 `scheduled_session` 映射为以会话范围为目标的 `scheduled_job.v1` 归属：有 `wi` 记该 WI，否则 `work_kind=cron` 并携带 `job`（ID、名称）。 |
| 查询与 Stats | 主张优先，补充按序择一；未配置 WI 的任务键为 `cron:<任务 ID>`，每行附 `attribution.sources` 与任务名，Stats 读取端校验来源条数与该行用量一致。 |

## WI654 逐请求用途与历史验证身份修复候选（2026-10-06）

`cc_request_purpose.py` 在采集器中核对主/子 transcript 的精确原始输入、prompt、parentUuid 顺序与成功配对工具调用；保留每次声明的时间、原始请求和工具范围。新输入须另行声明，相同 WI 在不同请求中的多次调用不合并。明确 session-task 通知只通过成功配对的原生 SendMessage 任务确认回到原始 prompt；缺结果、重复身份、错误接收者、后到的原始输入与未证明的子链不能建立入口或用途。SendMessage 可使用原生 tool_result.content 内唯一 JSON 确认；原始正文不进入 Receipt。自动压缩摘要（isCompactSummary）不是新请求：与子代理输入相同，按其 promptId 回到唯一且不晚于它的原始输入；promptId 无唯一原始输入或晚于摘要时保持未证明。

全体已准入旧 CC 声明均可核对到原始请求时，沿该生产者原有 `session_wi_declaration.v1` attribution identity 追加 `cc_native_request_purpose.v1` 更正，版本中保留原始声明/用量证据。新协议缺请求边界时不得降回整个 session。缺自己声明的请求保持 unassigned；同一请求内的后一个声明从声明时刻起切换 WI（Agent 中途新建 WI 后声明），首个声明之前的用量归首个声明，同一时刻的相反声明保持 ambiguous；独立 Agent/WI 声明与数量分叉不被覆盖。这不是“把待核验改未关联”即可交付：实际回读分别报告已证 WI、明确非 WI 及仍缺证的用量。

`codex_origin.py` 统一三个观测路径的原生入口判定：Codex Desktop 的 vscode/exec 来源归入 codex_app，codex_exec 的 exec 来源归入 cli。父线程只可传递有证原生入口，不能传递 Agent、WI 或权限；保留 exec 原生细节，不声称人类直接交互。

`verification_workspace.py` 只读取显式登记的 registration_root、单一既有 authority_root、本机 host 与 surface。每个会话自身启动目录须与正式 verification 登记和真实 Git common-directory/worktree identity 一致；不可变 Git base 中的冻结 WI 候选指纹、typed handoff、配对创建/接受 audit 及后续候选失效/撤销边界共同限制完整用量区间。过去验证者可有合法历史计费身份，当前候选已变并不恢复其当前执行权限；不修改 SCL admission，principal 使用登记 verifier，不使用 WI owner 或原生昵称。

新增 `verification_workspace.v1` 读取能力先在所有实际 Receipt 读取端验真，再通过现有 source-registration 候选路径登记；默认未启用。Actor 与用途凭据的可选 usage_fact_ref 锚定完全同一用量版本，在正式 collector 既有版本处理之后重绑定到相同原始 payload 的 canonical head。旧 partial 用量的历史证明不能带入越过失效边界的新 final；独立相反 actor/task 仍冲突。候选命令的四个显式参数为 --verification-registration-root、--verification-authority-root、--verification-host、--verification-surface；只写新私有候选配置，不激活生产。

本轮冻结的515条事件中，六个验证会话的实际私有 collector 回放已核对 Agent/入口及三个根会话 WI 更正，原始用量逐字不变、金额守恒、旧水位相同、重复采集0。455条 CC 中64条属于其原始 WI904 请求、12条属于其原始 WI340 请求，379条仍缺逐请求用途证据；不能借之后的340声明倒填。生产采用与同范围 Stats 读回必须另行核实，租户历史命名/成员有效时间缺证仍保持未知。


### Authenticated Relay product tenant context (v2 opt-in)

`pulse_stats_tenant_registry/v2` keeps the v1 exact scope/time `bindings` and adds closed `relay_users` declarations plus `definition_observed_at`. The real Relay `UserConfig.userId` is the product tenant id; its explicit public name is declared separately. The current configuration proves naming only, never past Agent membership. Each historical usage requires the original private Relay input's captured `source.humanRef`, enrolled Host source and node, event time, and the existing exact native/dispatch actor proof. Pool names, paths, WI owners and delivery destinations cannot name a tenant.

The observer extracts identity/time metadata and hashes the immutable original bytes; it does not interpret, retain or export the input body or credentials. CC sidechains must retain the exact original input and dispatch actor witness already validated by the native route reader. An agreeing Host/native request join may refine a coarse Host interval to its proven native request point using the existing quantity reconciliation; projected quantities are never written. Without that compatible witness, stale usage version, contradictory identity, unproved input or direct Desktop/automation origin stays unknown. Independent quantity and actor conflicts remain visible.

Enroll `tenant_context.v1` and `tenant_relay_input.v1` on all readers before preparing the private declaration/source candidate through `source_registration.tenant_candidate_registry`. The real named-user declaration can be prepared with `relay_tenant_context.relay_user_declaration(config_path, registry, names=explicit_public_names, observed_at=actual_naming_time)`. The opt-in publisher still requires `tenant_identity=True`; reader deployment alone does not activate membership or change the current source registry. Historical repair is append-only, anchored to the exact current usage version; old watermarks and original usage stay intact, repeat collection is zero. Missing authenticated membership is not zero cost.

### Agent 登记租户（查询时补全）

发布器读取来源登记的 `workspace_registry_path`（`ssot/registry.yaml`），得到 Agent→租户映射，交给 `CollaborationQuerySession(agent_tenants=...)`。查询完成身份投影后，`agent_tenants.apply_agent_tenants` 为已确定 Agent 的行补全租户：已有会话租户（Relay 输入或成员声明回执）保留并标 `session`，登记租户不同记入 `registry_conflicts`；没有会话租户的标 `registry`。映射摘要进入计算缓存键。此步骤不写账本、不改用量与金额；规则见 [用量身份与业务归属](stats-attribution-identity.md#全平台租户展示tenant_contextv1)。
