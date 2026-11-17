# ComputeCost 迭代方案：先建立可信 WI 协作凭据，再统一报告成本、质量与 ROI

- 状态：**2026-09-13 的原始迭代方案；协作用量→Stats 子路径已随后实施并部署，五类凭据的整体退出门未全部完成**。
- 方案日期：2026-09-13；采用状态更新：2026-09-17。原方案修订基于 PR #512 的 `42b2ad4689c24f5badc9997297b693fc919e0e54`，保留该版已吸收的独立审查意见。
- 承载：`wi_20260820_568`；不修改 WI lifecycle、既有验收记录或其他 WI 前沿。
- 审计代码基线：PULSE `b8c598fc1ddafaaded50ff5cec82eb44d32c5a89`。
- 现状实证：[三运行时审计](../ComputeCost/uat/receipt-producer-audit-20260912.md)；历史样本：[WI309 手工对账](../ComputeCost/uat/wi309-manual-evidence-audit-20260912.md)。两份历史证据保持原样，不升级为本次生产验证。
- 既有合同：[requirements](pulse-compute-cost-requirements.md)、[design](pulse-compute-cost-design.md)、[tasks](pulse-compute-cost-tasks.md)。[Issue #492](https://github.com/bigbird-online/PULSE/issues/492) 的修复与 WI568 总体验收分开；本方案不关闭该 Issue，不以 R3 的五个自然 WI 替代既有 S5 七天／十个合格 Outcome 等验收门。

## 采用状态与阅读边界（2026-09-17）

本方案保留当时的目标、反例和五域验收要求，不继续以“仅提案”描述已经运行的成本路径。现行用量/归属/价表设计在[requirements](pulse-compute-cost-requirements.md)、[design](pulse-compute-cost-design.md)及[身份设计](stats-attribution-identity.md)；采用状态与剩余任务见[tasks](pulse-compute-cost-tasks.md)。
已完成的范围是登记原生/Host/SCL 证据到正式七栏 Stats、结果复用与增量传输、两个自然发布周期和 App 重启，见[生产基线](stats-query-dashboard.md#已证基线与边界)。这不是全量历史、全 Runtime/入口、完整 WI 质量/ROI 或原 S5/S6 的通过声明。
用户后续明确：业务去向可以是 WI、cron 或普通非 WI；证据不足仍未关联，不把所有空 WI 改成无。四类 Token 明确为零可计零标准成本，未知不能补零。默认复用已计算结果，强制重算与按真实依赖失效并存；旧价表、旧水位、来源错误及历史捕获 gap 保留。
以下“本 PR/仅方案/尚未接线”等表述均指 2026-09-13 的原审计或计划阶段。它们不撤销后续明确授权、已记录部署，也不授权文档修改者擅自关闭原 WI；未完成的质量/收益/库存目标不能用“已上线 Stats”替代。

## 1. 用户结果与范围

**用户给出 WI、时段或 Agent，ComputeCost 从已登记来源自动汇合协作凭据，返回可追溯的成本、质量、ROI，以及尚不能确定的部分；用户无需寻找日志、拼接 snapshot 或重新描述归属。**

本轮只有两个产品目标：

1. **准确记录所有基于 WI 的协作凭据。** 至少覆盖执行与归属、机器用量、人类参与、产出与验收、收益观察五类事实。准确包括不重复、不补造、遗漏可发现，并能解释每项事实属于谁、哪个 WI、哪个范围。
2. **同一凭据底座支持 WI、时段（日／周／月／任意区间）、Agent 及其组合的成本、质量和 ROI 查询。** 返回明确的事实集合、时间及统计口径、as-of、水位、政策版本与各项证据覆盖。

先把机器用量做可靠，但不能把其他四类凭据的来源和关联留到最后再设计。Pi、Codex、CC 是同等 usage 基础范围；CC CLI print/interactive 与 SDK、Codex Relay CLI 与实际使用的 App Server 入口分别登记、接线和验收。runtime、entry、host、采购来源是不同维度；库里有 adapter 不等于真实入口已启用。

已确认排除仅为 **ChatGPT 网页中的 WI draft 暂不计入统计**。排除不等于零成本，也不扩张为所有网页协作排除；其他 WI 执行／审查入口逐项登记 supported/excluded/unknown。网页不可取得的原生用量保持不可观测，不以 Session 自估 Prompt、DOM 估算或月费分摊伪造精确凭据。受支持入口的无 WI 用量仍记录，归属可空或后补。

不建设复杂仪表盘、通用工作流平台、新审批链或自动质量评分器；不强迫每个 WI 都有货币收益、每个人类交互都有实测时长。`partial/unavailable` 是有原因的真实结果，**不是缺少应交付 producer 接线时的免责完成状态**。默认输出机器标准成本与人类投入向量，不把缺少 ROI 依据变成整份报告不可用。

本方案被接受后，R1 将新增语义映射至既有 requirements/design/tasks，逐项标明保持、扩展和需另获范围批准的变化；新合同未经同步确认不得用来改写旧凭据或绕过既有门禁。本 PR 不宣告旧验收门完成或失效。

## 2. 2026-09-13 审计现状与复用边界

可复用资产包括原生解析器、`UsageEvent/TokenUsage`、追加账本、Pi 锁内捕获与 baseline/final、App Server 水位 observer、CC/Codex 血缘解析、`query.py`、`wi_report.py`、Harness measurement/review、attention、business-value receipt 和定价组件。不另造平行账本，不把重构规模当成果。

当前 usage 至少存在 Relay `result → metering`、CC/Codex 回溯 collector、WI-bound Pi → SCL、App Server/CC-SDK observer/sample 四条未等价贯通的链。已有质量／价值 adapter 或构造函数，也不证明下游正在持续生成真实凭据。

| 已知问题或待验证风险 | 方案必须提供的修复 |
|---|---|
| CC 明细与 Relay 汇总的合成重叠样本采成 440 Token，作者设定互斥消费为 220 | 补机器可核验覆盖身份；有重叠证据才核销，独立消费保留，无关系保持未决 |
| result-only、缺 usage 归零、失败/取消/重试无完整回执 | 执行预期独立于 usage；失败消费保留，缺失不转零，恢复只补采不重执业务 |
| Codex cached 字段、CC inclusive/uncached input、Pi 子调用/压缩模型语义不同 | 按 producer/version 映射，保留原生数量及本请求模型依据，未知不继承主模型 |
| sealed/bounded 或已找到的日志被当作完整覆盖 | 分离数量有效性、入口覆盖、WI 归属与终结状态；分母来自独立预期 |
| 总机器量可留在 unassigned，但已知 WI 的准入、恢复、委派、收尾仍漏账 | 保存宿主业务 assignment 的生效边界，建立 WI 预期清单；不靠 session 全量或终态窗口猜归属 |
| usage、评审、产出与收益粒度不同 | 自然键去重后分别汇总，再连接；禁止多对多 join 放大成本、成果或收益 |
| 现有 query 单一参考模型估值不等于 WI 实际模型标准成本 | 显式估值名称、映射与价格快照，独立验证数量和金额，不静默替换口径 |

上述代码依据、合成反例、历史样本与有限生产读回各自的证明范围见审计文档；不把源码风险外推为全部生产账单错误，不用过期 STATS 推断三机当前状态。

## 3. 数据流与责任

```text
Host / SCL / HO 的任务与执行事实 ─┐
Pi / Codex / CC 原生 usage ───────┤
宿主人类交互 / Harness measurement ┤
产出 / review / Outcome ──────────┤→ 按域持久化与核销 → 可重建查询投影
真实 consumer 的收益观察 ─────────┘                       ↓
                        WI / 日周月 / Agent 的成本、质量、ROI + 证据与缺口
```

Host/运行时负责稳定 dispatch/attempt、执行者及原生来源；Harness/SCL/HO 继续负责授权、WI 和生命周期，并暴露已有 assignment、委派、评审事实。ComputeCost 负责语义校验、核销、归属修订与投影；价格独立于数量；Stats 只呈现结果。计量归属不是执行授权，记录进入失败不赋予执行权，缺计量也不能擅自新增业务审批阻塞。

五类凭据是五类事实，不是要求五套存储服务或一个万能 event。优先复用已有宿主钩子、ledger、SCL artifacts 和 Harness 投影；快照是来源引用或物化视图，不再增加一笔消费。跨机索引可重建，本地耐久记录、源材料与确认游标不因索引短暂不可用而删除。

## 4. 最低凭据合同

以下冻结业务语义；字段名是语义名称，不声称新 schema 已上线。R1 对现有闭合 schema 做最小版本化映射，不能直接加字段使旧 validator 失效。

### 4.1 来源、触发与稳定关联

| 凭据域 | 生成／导入时机与权威来源 | 最小关联和处理 |
|---|---|---|
| 执行与归属 | Host 接受明确任务、尝试 dispatch、spawn/HO、恢复、结束时记录；复用 SCL/HO 事实 | 稳定 assignment/execution/attempt、WI、生效边界、executor、责任人、父/root、disposition；同一次重放不新增执行，真实 retry 是新 attempt |
| 机器用量 | 真实请求 usage、原生日志、水位更新及故障补采；复用 Pi/CC/Codex adapter 与 observer | 原生消费身份、execution、粒度、覆盖范围、模型、Token 类别、源位置；result/session 汇总不能与其明细同时计费 |
| 人类参与 | 宿主接收到真实人类输入或既有人类审核动作时留事实，分类/HAU 可后补；消费 measurement/attention | 稳定原始交互引用、actor、时间、assignment/WI；原始消息计数与标签/权重/方法版本分离；专注计时另有来源 |
| 产出与质量 | 正式交付候选、评审、接受/驳回、复审、验收后修正发生时导入 Harness review/measurement/Outcome | 稳定交付对象、candidate/artifact/acceptance fingerprint、repo revisions、评审事实和角色；旧候选审批不转移到新候选 |
| 收益观察 | 真实下游 consumer 在明确观察范围产生效果回执时导入；复用 `delivery_value.py` | outcome、同一已审产物、consumer、独立 verifier、指标/单位、基准/当前/差值、收益期间、证据；构造函数不是权威来源 |

共同保留稳定 `fact_ref`、producer/schema/adapter 版本、隐私安全证据引用、来源内容摘要、发生／观察时间。事实身份跨重放、归档和跨机复制稳定；新消费或新业务动作不能因时间、数量相近被去重。没有原生 request ID 时使用宿主 dispatch 加源位置/序号并标明身份强度；直接 parent 不可得时留 unknown。

人类交互先保存可证明的发生事实，不等待 Agent 分类或 WI 终结。渠道重送、引用旧消息、恢复包、Host 合成提示不能重复算人类输入；新的人类发言即使文本相同也不是重送。一次人类验收可同时支撑交互计数和评审事实，但不因此成为两次人类投入。只发布安全引用与派生标签，不复制正文。标签允许多选；互斥类别统计必须另有唯一主类别政策，不能把多标签之和当消息总数。

### 4.2 原生用量与定价兼容

- 保存原生数量、归一化数量、单位和映射版本；uncached input、cache read、cache write、output 互斥。reasoning 已包含于 output 时不再加一次；缓存写入类别等保留自身语义。
- **不复用旧字段名改变含义。** 旧 `TokenUsage.input` 包含缓存；新数量投影到旧接口须重建 inclusive input，不能把 uncached input 直接喂给旧扣缓存逻辑。`valuation.py`、`query.py` 与 WI 按实际模型分区估值各有兼容测试。
- 模型证据属于本请求，包括子执行、压缩和分支总结。配置默认模型仅是推断，不能冒充事实；缺模型也保留已知 Token。字段缺失为 null/unknown，只有明确零才记零。
- 累计水位声明作用域、起点、代次、reset 与时间范围；差分守恒，负跳变不截零，乱序重放不产生新消费。由分项构造 total 只证明算术，不证明源完整。
- 保留既有 `machine_cash_usd` 政策：以 digest-bound 公共 OpenRouter catalog 的实际模型平坦标准价，对互斥 Token 类别计价一次；不选 overrides，不按订阅分摊，不由 native usage.cost 覆盖。它是政策标准成本，不是实际账单结算。
- 估值同时绑定事实集合摘要、模型分区、映射版本、catalog/政策版本；同 WI 的旧估值不能用于新事实集。缺模型/缺价保留未计价量；只有无重叠且可证明的已知子集才可报告 partial 金额，冲突不能包装成完整金额或可靠下界。

### 4.3 WI 全程归属与 Agent 角色

usage 发生与它属于哪个 WI 分开。WI 状态至少有 assigned、unassigned、ambiguous、excluded；排除必须引用批准政策，无法归属不是排除。宿主已有明确任务时，保存稳定 assignment、来源、版本及其生效边界，不以成功 `/wi`、存在 draft 或进入 in_progress 为记账前提。

归属覆盖已知的进入尝试、准入失败、恢复、HO/子执行、独立审查、验收、rollover、收口与终态后清理。Host 在 dispatch 时绑定当时有效 assignment；并行执行各自保存边界，不能靠一个全局“当前 WI”覆盖所有并行任务。WI 中途切换只影响相应新范围；不可拆分的跨 WI 汇总留 ambiguous，而不是按时间均摊。

`execution parent`、业务 assignment/责任、`cost roll-up parent` 是三种关系。WI309 样本中的 WI145 父账归集、WI154 canary 排除与终态后清理依据必须保留；不从 root 推造完整调用树或成本树。人工裁定可以作为带裁定者、范围和来源的追加归属证据，不冒充原生 telemetry。

Agent 保存稳定身份及角色：executor（实际执行者）、responsible/owner（责任主体）、delegator、reviewer；别名改名或 owner 转移不能重写历史执行者。按 executor 的机器成本与按 responsible 的交付质量是不同视图，报告明确标注；ROI 必须使用同一角色与对象集合的成本/收益归属，不能混用两列默认值。多参与者视图不自动可加；共享事实保留独立共享桶。

父账和子账 drill-down 是同一事实集的不同视图，总量按事实集合求并；显式份额分摊须带政策和证据，份额加未分配部分守恒。多 WI/Agent 归属不清只降低相应维度，不能把已知机器数量从日期、模型或主机统计里丢掉。工作阶段／用途标签可作为版本化注释，不当作新生命周期门禁，也不把手工语义划段冒充 SCL 原生 phase。

### 4.4 产出、评审与质量

区分**稳定交付对象**与其 candidate/artifact 版本。对同一交付的三次修订、三轮评审仍只计一个交付，评审工作量另计；不能因每次修复生成新 artifact hash 而增加成果数。优先复用 WI/Outcome 的交付引用；无法证明不同候选属于同一交付时保留关系缺口，不能按标题相似合并。独立的新交付必须有业务依据。

每次评审保持 candidate fingerprint、acceptance/rubric 版本、评审者及其权威角色、decision、时间和证据。ComputeCost 复核精确候选与来源；模型自评、CI 通过、人类接受、独立验证、豁免分别展示。两个冲突评审不能简单选时间较新者；仅同一评审链的合法 supersedes 或既有权威裁决可消解。

默认使用所选 as-of 下目标候选的有效评审，不把旧候选 approved 搬到新候选。复审历史保留首次通过、实质返工及验收后修正。没有历史不能从最终 approved 推断 first-pass；新交付/新验收目标与原交付返工的边界由业务引用与验收版本确定，不由 ComputeCost 猜。

| 最低质量指标 | 统计合同 |
|---|---|
| 交付对象数／accepted 数 | 对选定交付 cohort 去重并取 as-of 下有效状态；accepted 比例为 accepted/(accepted+partial+rejected)；submitted/pending、blocked、unknown 另列并披露决定覆盖，不算通过或失败 |
| approved 比例 | 每对象的目标候选取有效 approved/changes_requested 决定；分子为 approved 数、分母为有有效决定数，同时给出该分母占全部应评审对象的覆盖；不与 accepted 合并 |
| first-pass 比例 | 在同一交付 cohort 中，以首轮结论及前史可证明的对象为分母；首轮通过为分子；缺历史、waived、not_executed 单列，不算通过或自动归零 |
| 返工／验收后修正 | 按唯一业务事件和评审链统计；回执重放、重复 webhook 不增加轮数；来源仅有快照总数时保持快照粒度，不生成虚构事件 |
| 已有评分 | 仅在同 metric、量纲、rubric/版本和明确权重下聚合；返回样本数、覆盖、分布/均值及关键失败项；跨 rubric 转换必须显式选政策 |

所有比例返回 numerator、denominator、eligible/known/pending/unknown/waived 数及规则。分母为零返回 `unavailable/no_observations`，不是 0% 或 100%。分母只覆盖已知样本时，比例标为已知子集，不冒称全体通过率。关键未执行项、critical failure 与 waiver 不被均值覆盖；不新造一个强制“总质量分”。

### 4.5 收益、人类投入与 ROI

收益仍需 accepted outcome、同一 reviewed artifact set、真实 consumer 和独立 verifier 的可追溯证据；维持既有 `delivery_value.py`／Harness 的权威要求，不以自算 digest 冒充独立验证。量化 baseline/current/delta 守恒，定性陈述可以报告但不进入货币 ROI 分子。

每项收益明确：指标/单位、受益对象、实际测量期间、观察时间、baseline 方法及政策、增量/累计/快照属性、唯一 observation 与修订关系。重复观察不相加；同范围累计 40→110 只表示该范围最新累计 110，不是 150；只有作用域、起点一致且可证无重叠时才能差分。重叠收益窗口、同一收益的“节省时间”和其美元换算不能双加；forecast/assumed 与已观察收益分开。负向效果保留负值，不静默删掉。

人类投入最低交付是有覆盖的人类交互数量、已有分类及版本化 HAU 向量，沿用 G0 权重；实测 focused_seconds、日历经过时间和等待暴露分别报告。没有 timer/trusted telemetry 不造工时，不将消息数、HAU 或经过时间直接换美元。源已有实测时间与批准费率时，本轮查询可支持显式 `machine_plus_measured_human` 成本口径，不把它留作含糊的未来功能；没有则该口径保持 partial/unavailable，默认机器标准成本仍可用。显式批准的 HAU→时间/费率估计只能作为 scenario，不冒充 measured。

基础 ROI 为 `(V-C)/C`，其中 V 为同一对象集合与指定收益期间内的可比货币收益，C 为选定范围的成本。默认 C 使用政策标准 `machine_cash_usd`，输出注明 `cost_basis` 和排除的人力／其他资源项，不把“机器口径 ROI”叫完整协作或现金投资回报。

- 多 WI/Agent 先分别累计同一 cohort 的 ΣV 与 ΣC，再计算 `(ΣV-ΣC)/ΣC`，不能平均各 WI 的百分比。父子成本及共享收益先核销再求和。
- Agent ROI 需要成本和收益都可按该角色归属；有多个执行者但无收益份额依据时，允许总体 WI ROI 可用而 Agent ROI unavailable。不能按 Token 比例默认分收益。
- 同一集合缺少所选成本、价值、分配、单位转换或时间依据时，完整 ROI 的 value 为 null，并给出 `incomplete_cost/incomplete_value/allocation_unknown/basis_mismatch` 等原因。只可另列明确命名的 matched-known-subset/scenario，不能静默删掉失败、取消、未测收益的 WI 来美化结果。
- C=0 时 `ROI=null, reason=zero_denominator`；已测得 V=0 且 C>0 时 ROI=-100%，不同于收益未知。V<0 允许合法负 ROI。币种不一致且无批准汇率/快照时不能相加；整数数量与 Decimal 金额计算后再按显示政策舍入。
- 输出 V、C、V-C、ratio、对象数、成本与价值范围、完整度、收益期间、as-of、价格/估值/分配政策版本与证据摘要。不得只输出一个百分比。

### 4.6 修订、历史查询与可信度

消费事实不可变；更正、归属、合法增长、评审与收益修订追加，并引用原对象、来源版本和被替代 revision。同一身份的内容冲突不能静默覆盖；被撤销的断言保留历史。final 后的新消费是新事实/范围，不修改旧 sealed 内容。

区分发生时间、来源 observed_at、进入权威持久账本的 recorded_at/提交水位，以及归属/修订的 effective_at。`as_of` 默认表示“当时账本已知”：先以账本提交水位选择当时可见事实/修订，再应用有效范围；不能按旧 source 时间把后来收到的归属或评分灌回旧报告。跨机保存各来源确认水位；报告固定事实集合摘要、查询参数、adapter/指标/归属政策及价格快照。仅凭一个 observed_at 或当前 owner 表不能保证历史可复现。

独立披露数量有效性、source completeness、WI/Agent 归属、运行状态、质量、人类参与、收益和价格覆盖；一域缺失不抹去其他域的已知结果。sealed 只证明声明边界不变，不证明 provider 隐藏重试、全部入口或全 WI 已覆盖。

SHA-256 只证明内容完整性；来源权威仍由可信 Host、已有审核和消费者证据决定。不发布正文、raw session/request ID、绝对私有源路径或密钥。必要的可复算数量、映射与政策内容应随安全凭据保留；源清理服从补采期限，未确认入账不得先删源。

## 5. 可靠性与覆盖

### 5.1 分母必须独立，且按凭据域计算

第一层是注册入口/实际任务与 WI 预期清单：所有已知主执行、委派、子执行、审查、恢复和收尾都应有 disposition。第二层对照原生事实，第三层对照规范凭据与投影的源位置、水位和关系。入口离线、隔离 HOME 未核、child 未回交不能被平均覆盖率掩盖。

**不要求每次模型请求产生五种凭据。** usage 预期来自 dispatch/attempt；人类参与预期来自宿主原始 ingress；评审预期来自真实交付/评审请求；收益预期来自已声明的下游观察对象与窗口。没有收益发生，不是一次模型请求漏记收益；已有 consumer 回执没导入，则是接线/采集缺口。

分开记录 expected、observed、missing、in_flight、conflict、explicitly_excluded、not_applicable 与 visibility_unknown；具体原因、来源水位及观察窗可查。没有独立分母时不报 100%；零样本为 idle/not_exercised。失败有 disposition 但 usage 未知，只能说明执行已解释，不能说明用量完整。仅见快照总数的人类参与可按 WI 报告，不能伪拆为逐日事件。

### 5.2 核销与迟到

请求明细只在有同范围覆盖证据时核销汇总，汇总保留作对账。部分明细与全量汇总之间仅能对数学可证的互斥剩余范围计 residual；无法关联的独立消费不得误删。相同数量/时间只能生成诊断候选，不是精确核销依据。

重复通知、日志副本和修订不增加消费；streaming 同 message 更新、未完成 turn 合法增长、累计 reset 与乱序有各自协议。并发 revision 冲突隔离；迟到 suffix、终态后清理与迟到收益追加可追溯事实，不因 WI terminal 永久丢弃，也不复写旧 sealed 记录。

### 5.3 持久化与降级

尽量在 dispatch 前落盘 durable expectation；失败时不得声称可靠计量。区分未 dispatch、dispatch 结果未知、provider 返回、源已写、凭据已写、索引已确认等中断位置。磁盘满/权限失效可能连本地 gap 都写不了，需独立宿主健康信号，不承诺不可能的本地无损。

复用 OS-owned lock、fsync、原子发布、私有权限和持久游标；重启补采不重新发出业务请求。已有计量/安全门禁不降级，普通入口是否继续遵循宿主政策并明确告警；不得额外创建全局 SCL/人审阻断。运行中报告可 provisional；不能因等待超时自动升级为完整。

## 6. 三运行时并列落地

| 工作面 | Pi | Codex | CC（含 SDK） |
|---|---|---|---|
| 原生源 | assistant、nested-tool、独立子执行、压缩/分支总结 | CLI turn usage/rollout；实际 App Server 通知 | projects、CLI print/interactive、SDK assistant/result/stream |
| 首要修复 | 缺 usage 检测、子/压缩模型、独立子源、无 WI 捕获 | cached 字段、失败中断、水位恢复、aggregate 粒度 | request/result 核销、缺失不记零、模型拆分、消息与 attempt 身份 |
| 接线 | 复用 capture/扩展与 baseline/final；采集和归属解耦 | 不强迫 CLI 改 App Server；各实际入口各自接线 | sample builder 接真实 SDK loop；CLI 与 SDK 分别验证 |
| 归属/血缘 | Host assignment、恢复/rollover、子请求不双计、迟到 suffix | assignment、fork 复制前缀/archive、子线程、旧水位、多 WI | assignment、子目录/root、多行同 message、SDK retry |
| 完成要求 | 正常、故障、恢复、无 WI、真实消费者读回 | 同左；CLI 通过不能代 App Server | 同左；CLI 通过不能代 SDK |

人类参与、质量和收益不按 runtime 另造三套 producer；复用已有 Harness/consumer 事实，以稳定关系关联。每轮保留三列进度、已启用入口和缺口；未启用入口可 not_exercised，但启用前必须验收。不能用 Pi 的通过宣布三个运行时全完成。

## 7. 迭代切片与退出条件

### R0：原方案 PR——修订方案，不实施产品

保留原现状审计和历史证据，补齐本方案及反例。用户本次授权是修改方案；修改者不将自己的改稿标为独立验收通过。不部署、不改变价格或 WI 状态、不自动合并。

### R1：最小合同、来源映射和固定反例

登记 runtime × entry × host 的实际支持/启用/排除/未知状态、owner、源版本和真实路径来源，补齐未核入口而不臆断默认值。按 §4 的五类表逐项确定真实 producer、读取/触发点、稳定键、历史兼容和缺口恢复；仅有 builder 的项目明确列待接线。

冻结最小 schema 差异、指标政策和 §8 固定样本；同步 requirements/design/tasks 中的对应条款。取得各 runtime 的真实脱敏原生样本，合成故障样本单列。对保留期、对账频率、故障发现上限、性能/延迟预算和回退阈值取得基线并冻结数值，未冻结不推广生产。

退出：五类事实来源及查询映射明确，三 runtime 数量兼容可独立复算，WI/Agent/质量/ROI/时间政策可执行；§8 预期不依赖实现猜测。不得把任务扩为全新通用协议或新授权系统。

### R2：生产、关联、恢复与同路径只读查询

按 §6 分小 PR 修复 usage，并把 Host assignment/预期清单、人类参与、已有 review/measurement/attention/value 来源接入。复用既有 WI/SCL consumer；旧凭据原样可读，历史 unknown 不升级 exact，新旧路径 shadow 时不重复入账。

每个 producer 接线同时提供到 `wi-report` 或同一统一查询投影的读回，不等 R4 才第一次端到端 join。确认来源存在却未接通，是未完成；某自然 WI 没有价值/专注时间事实，则结果可以 unavailable，但接入、校验、持久化、历史视图和正反查询路径必须实现。

退出：三 runtime 各通过正常/故障/恢复、守恒与重放；至少一条自然 WI 自动关联执行→用量/人类参与→产出/评审；有安装版本/实际加载/输出读回。完整已知 value 样本能经真实 adapter→存储→查询路径算出 ROI，而不只是直接测公式；不冒充真实收益。

### R3：真实自然 WI 灰度

按已批准 runtime × entry × host 灰度，复用自然任务，不创建行政/测试 WI 凑样本。同窗口留独立原生读回、assignment/执行清单和查询证据。WI309 用作历史回归，不替代新接线。

至少五个自然 WI 合计覆盖多 Agent、跨机/跨 runtime、委派、跨会话/恢复、中途切换、真实审查/验收及终态后清理等主要边界；各已启用基础 runtime 有独立样本。未自然出现的进入失败、磁盘故障等在隔离环境验证并标明，不能编造生产事故。无 WI 用量也有实测，但不伪装成 WI 成本。

推广要求：观察原生数量与规范数量差异为零、重复计入为零、预期协作有 disposition、已存在的人类参与/review/value 来源正确导入；未知消费、归属或缺域分别保持 partial。没有货币收益的真实 WI 应展示成本/质量及 ROI unavailable；有真实 value 才做 consumer 证据链验证。人工 UAT 是最终价值验收，不让用户首次发现接线、路径和版本问题。

退出：各已启用入口有真实读回、独立对账，无业务回归且满足 R1 预算；逐域说明已验证/未验证/不适用，不用五个样本冒充覆盖所有入口或替代旧 WI 总体验收。回退只关闭新投影/collector 开关，保留源、事实、游标及修订，不清库、不把旧路能力包装成新覆盖。

### R4：最小统一统计服务

基于 R2/R3 同一投影扩展筛选、分组和解释，不新造独立报表账；至少交付机器可读结果和可读摘要，不要求 GUI。现有注册来源自动发现，兼容的显式 snapshot 参数只作诊断，不成为用户日常拼接步骤。

#### 7.4.1 查询与维度

支持 WI、任意半开区间 `[from,to)`、IANA 时区、日/周/月、Agent role、runtime、model 及组合；兼容默认 Asia/Shanghai，周默认 ISO 周一开始，月为当地自然月，返回实际边界。时区转换和夏令时不能假定每天固定 24 小时；物理月分区不能限制其他时区边界检索。

每项指标声明自己的 cohort/metric_scope。runtime/model 等仅直接属于 usage 的过滤不能暗中把整 WI 质量/收益分给某模型；可显式选择“涉及该 runtime 的 WI cohort 的整体质量”，但标明它是整体而非模型贡献。维度无直接证据时，该指标返回 unsupported_dimension/allocation_unknown，其他合法指标照常输出；ROI 的分子分母必须同范围。

先分别去重/汇总 usage、交付/评审、收益，再按稳定关系关联。每次查询保存参数、政策版本、as-of/来源确认水位、事实集合摘要；按需 drill-down 到安全来源、归属解释和缺口。不能由用户多选分组改变去重集合。

#### 7.4.2 时间与对账

默认期间成本按消费 occurred_at；评审活动按 reviewed_at，交付 cohort 按首次正式提交或接受时间（查询明确选择）；收益金额按其测量/实现期间，observed_at 仅说明何时看见证据。“本月发生成本”“本月验收 WI 的生命周期成本”“本月测得收益”分别提供，不能因都在本月就直接相除。

跨边界且不可拆分的 aggregate 放 `time_unallocated`，返回相交范围但不计精确日桶；合并窗口按唯一事实集合重算一次，不直接加各日的 unresolved 行。没有区间依据则 time unknown，不均摊或全塞结束日。时区/跨月重算和迟到修订前后的 as-of 均能复现。

数量/金额的互斥桶含 unassigned、ambiguous、shared、time_unallocated，并在各自可证明范围守恒；非互斥父子、参与者和跨窗口诊断视图明确不可直接相加。质量比例和 ROI 不强求可加，合并时由其分子/分母和自然对象重算。

#### 7.4.3 成本、质量与 ROI 输出

成本输出互斥 Token、当前政策机器标准成本或 partial 子集、未计价量及原因，并列人类参与/HAU/实测时间，不藏入 Token。参考模型估值、实际模型标准估值、原生 provider cash 和订阅付款分别命名；既有 `machine_cash_usd` 的完整度规则不变。

质量按 §4.4 输出交付/评审、first-pass、返工/修正、可比评分和各项缺口；不从 usage event 数推成果。Agent 默认成本视图标 executor，质量视图标 responsible；用户可显式选角色，reviewer 用于评审活动，不自动成为产出 owner。

ROI 按 §4.5 选择同一对象集合、成本 basis、收益期间和分配政策，可分别查询单 WI、WI cohort、Agent；期间投入流与期间验收 cohort 的生命周期回报不混算。收益未知时 ROI=null，已知成本和质量继续显示。每项结果均有自己的 status/coverage/reason，不设一个“全局绿色”覆盖局部未知。

退出：§8 各反例和三 runtime 守恒通过；同一固定事实集按 WI、日/周/月、Agent 及组合可复算，修改价格不改用量，重建索引不改结果，迟到/修订不重计。自然无收益得到正确 null，完整已知样本得到正确非空 ROI；不能只实现永远返回 unavailable。

## 8. 固定验收反例与真实证据矩阵

以下数值均为**合成验收预期，不是生产测量**。R1 将它们落为自动回归并保留原始输入及独立 oracle；R2/R4 必须经过真实 adapter、持久化和查询路径。每项另列实现提交、测试/Actions、适用 producer、真实证据或未验证边界。

| ID | 输入/场景 | 必须观察到的结果 |
|---|---|---|
| A01 | 两请求各 110 Token + 覆盖二者的 220 汇总；对照样本汇总是独立消费；第三样本无覆盖身份 | 分别为 220、440、未决重叠而非精确总量；部分重叠不全加/全删 |
| A02 | 缓存、reasoning、子调用/压缩模型缺失；旧 inclusive input 与新互斥字段 | 四类数量和估值独立守恒；不二次扣缓存/加 reasoning；未知模型保留 Token、不套主模型 |
| A03 | 同通知重放、乱序、水位 reset、同 message 合法增长、fork/archive/跨机副本 | 重放不增，reset 有代次，增长不被不可变 ID 吞掉，冲突显式；复制前缀不再计 |
| A04 | 失败/取消/429/retry/断流；进程死、并发 writer、磁盘满；凭据已写索引未确认 | 每已知 attempt 有 disposition；用量未知不记零；恢复不重执业务、live writer 不被抢锁；重传幂等 |
| A05 | 已知 WI 进入失败→恢复→HO/子执行→跨机 rollover→终态后清理；中途 canary 切到另一 WI | 各任务按自己的 assignment 边界归属；父子/执行/成本关系分离；不截 terminal，不扩入无关任务 |
| A06 | 一笔 100 Token 在 t1 未归属；t2 才追加生效于过去的 WI 归属 | t1 仍是 unassigned=100，t2 为目标 WI=100；总量都为 100，旧 as-of 不被回填改写 |
| A07 | 两条 usage 合计成本 100，一个交付三个候选/评审，两条互斥收益增量 40、70 | 成本 100、交付 1、评审活动 3、收益 110、ROI 10%；连接后不乘以候选/评审/收益行数 |
| A08 | 三个应评审交付：两个有决定（一 approved、一 changes_requested），一个 pending；已知首轮中一通过 | approved=1/2，首轮历史完整时 first-pass=1/2；评审覆盖=2/3、pending=1；不报全体通过率或把 pending 当失败 |
| A09 | 旧候选 approved 后产生新候选；多 reviewer 冲突；跨 rubric；waiver/关键未执行 | 旧 approved 不迁移，冲突不靠最后写入获胜；不同 rubric 不默认平均；waiver 与关键失败单列 |
| A10 | WI-A 成本10收益20，WI-B成本90收益90；另选集合包含一个收益未知的 WI | 前两项集合 ROI=10% 而非50%；扩大集合后完整 ROI=null，不静默丢掉未知 WI |
| A11 | 收益分别为未知、已测零、负值；成本为零或人力未测 | 未知收益 ROI=null；C>0且V=0 为-100%；负值保留；零分母 null；选总协作口径但人力缺失不得冒充完整 |
| A12 | 多 Agent/父子 WI 共享一个收益；另有明确份额及未分配余额 | 无政策时总体可算、Agent ROI=null；有政策时份额与余额守恒，不每人复制全额，不默认按 Token 分配 |
| A13 | 同范围收益累计40→110，重复读取110；其时间节省和美元换算并存 | 最新累计110不变成150/260；只对可证范围差分；同效益不同表达不双计，定性/预测不混入实际货币分子 |
| A14 | 8月投入40、9月投入60，9月验收，指定收益期已测120；同对象完整合成事实集 | 9月发生额60，9月验收 cohort 生命周期成本100，ROI20%；不能拿120与60直接算成100% |
| A15 | 跨日/月 aggregate、不同查询时区/夏令时、窗口合并 | 精确桶与 time_unallocated 分开；合并按事实去重重算，不全归结束日、不假设24小时；未知时间不消失 |
| A16 | 一个人类输入同时留在渠道、Relay、原生日志；另有重送/恢复包/新的相同文本发言 | 同一输入计一次，恢复包不计；新发言另计；HAU分类修订不增消息，快照不伪拆日事件，工时未知不变0 |
| A17 | runtime/model 过滤 usage，但质量/value 只绑定整体 WI；切换 owner/别名 | 整体质量显式标 WI cohort；无分配不生成模型/执行者 ROI；历史 executor 不被当前 owner 改写 |
| A18 | 更换价格/归属/评审/收益 revision；源离线后补采；重建索引 | 数量不因改价而变，估值绑定新事实摘要；旧 as-of/快照可复现；各域未确认水位保持 partial；摘要不冒充来源权威 |

usage 行分别给 Pi、Codex、CC 结果；协作/人类/质量/收益行给对应源及最终查询结果。正常多轮工具循环、无 WI、独立子执行的自然样本与隔离故障注入分开记录；没有样本的入口为 not_exercised，不适用必须给合同依据。

## 9. 本次修订落点与停止点

| 独立审查项 | 本方案落点 | 最终验收 |
|---|---|---|
| P1-1：协作凭据被收窄为 usage | §1、§4.1/4.4/4.5，R2 接真实来源而非只留 builder | 五类有权威来源/触发/关联；人类参与与已有质量自动入账，收益正反路径均能查询 |
| P1-2：总机器量完整但 WI 不完整 | §4.3、§5.1 | A05/A06/A12/A16/A17；已知 WI 全程任务、角色、缺口和修订可解释 |
| P1-3：质量/ROI 跨维度语义缺失 | §4.4–4.6、R4 | A07–A18；自然粒度、分母、时间、价格/归属政策与非空/空 ROI 均正确 |

原方案 PR 仅提交方案和既有证据，不授权实施或自动合并；后续实施与部署的独立授权及记录见页首采用状态。此历史说明不等于今天 collector/查询仍未启用，也不以本次文档维护补签独立验收、全量覆盖或 WI closeout。
