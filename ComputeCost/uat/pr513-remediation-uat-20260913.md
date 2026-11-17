# PR513 统一修复与新 UAT：2026-09-13

## 状态与范围

- 原审查候选：`a682da89ede202929ec0ae463be639b32c5d911b`。
- 固定修复代码：`637cb121e00ebe4fa38a466c876a073cd725f239`。
- 实施仍在 PR #513，不新增 WI，不修改 WI568/WI779 生命周期或原验收门。
- **新 UAT 已开始：第一阶段是私有、只读的三运行时原生文件回放。不是生产 Host 全量启用，也不是最终 UAT 通过。**
- 本轮没有部署/重启服务、修改路由/价格、开启生产 `PULSE_COMPUTECOST_RECEIPT_DIR`、新增供应商模型请求或自动合并。
- ChatGPT 网页 WI draft 继续明确排除，不记作零成本。

## 1. 原阻断项的修复落点

| 原审查项 | 修复及实测验收 |
|---|---|
| R1 CC 重复观察导入冲突 | Host 同一内容版本复用 event_ref/at，不同 partial/final 版本共享 usage_ref；真实 Node writer → Python registry 的 prefix100 → full110 → replay110，旧水位100 |
| R2 Host/native 双计 | 新可选 native_scope_ref 关联原生 session 范围；同scope的request/aggregate及跨producer aggregate/aggregate无covers则未决，不再输出双倍且reconciled。Pi tool summary未有child覆盖身份不相加，独立compaction保留 |
| R3 Codex 增长丢失 | 按原生与归一化计数共同判断revision；保留进行中增长与仅final变化；ledger evidence refinement不再分叉/复活已替代断言，同批多步revision衔接正确 |
| R4 缺失变零 | native字段presence在整数兼容投影前保留；缺失/null保持unknown。新增可选reported_total_tokens保留可证的inclusive input+output总量，不给未知缓存分区定价、不重复加到分类Token |
| R5 取消/失败后结算丢失 | Codex各终结出口保留已知usage；CC/SDK明确Host attempt元数据；SDK旧generation迟到事实只写旧attempt、不驱动新turn/channel。kill/replay/fallback保留有界旧ref，活跃turn不受缓存过期影响 |
| R6 A coverage替B证明 | 每个所选WI都有独立domain分母；相关global缺源继续阻断，idle/excluded不冒充缺失 |
| R7 分组删缺测WI | 以执行预期及各域事实建立cohort，保留execution-only及未知model桶；agent/runtime组合不再只靠usage选择WI，按日期约束该桶 |
| R8 registry永远unknown | 成功receipt transport交给原凭据coverage，不自造完整分母；故障恢复以追加修订撤回旧transport gap；再次missing重新降级；可选registry work_items约束可信影响范围 |
| R9 人力冲突后仍算ROI | human/human_summary冲突进入所选成本基准；不完整计时、分母、费率或冲突时机器+人力ROI不可得 |

另修复：print A完成同步drain B后误删B context；Codex失败A的exit不得提前结束B；SDK自动重试独立attempt；首次目录ENOENT不假装恢复失败；spool file/dir fsync；无WI执行者由Host实际router注入；review activity日期、评审冲突、跨日execution关联、pricing=null隔离、CLI先验证参数再采集。

迟到路由限制：已结束attempt最多保留128条/5分钟；窗口从终结或替换刷新。活跃attempt仍由活动队列查找，不因运行超过5分钟而丢失。回收后无法定位的迟到帧显式gap，不赋给下一任务。历史gap不因重启自动变成零；shadow记录不改变既有业务幂等授权。

## 2. 回归与独立复审

| 检查 | 本地结果 | 证据边界 |
|---|---|---|
| ComputeCost Python全量 | **328 passed** | 固定637cb121；含真实TypeScript writer跨语言测试，消息仍为合成输入 |
| Pi extension | **28 passed** | Relay/Pi封存既有合同，未进行生产模型调用 |
| Relay typecheck/build | **passed** | 已独立npm ci安装本工作树依赖，未修改生产依赖 |
| Relay全量 | **1977 passed / 7 skipped**；112文件通过、1文件跳过 | 在2c64569b执行；之后637cb121只改Python，Relay内容相同，不将skip写成pass |
| runtime-manager | **33 passed** | 原pytest未安装，隔离venv补齐；sparse缺失的原版runtime-cost-presets.json按固定HEAD物化后通过 |
| spec drift guard | spec=4，drift=2 ≤ 原基线5，**通过** | 不宣称零漂移 |
| patch检查 | **通过** | 不代表生产完整性 |

首次最小代码快照遗漏config导致的测试失败、依赖未就绪/typecheck中途错误均已修正后重跑；不能引用这些中间结果声称最终回归失败，也不能把旧候选37a340ad的测试报告当新SHA证据。

独立复审在b2e6d822允许进入有界只读回放；随后刷新旧attempt保留窗口的告警被补入2c64569b。真实回放发现的Codex counter问题在637cb121修复，并单独复审未发现阻断本轮回放的问题。复审为源码/反例审查，不是人类最终验收或生产Outcome认证。

## 3. 第一阶段：既有原生日志回放

机器可读结果：[pr513-native-replay-20260913.json](pr513-native-replay-20260913.json)。

选择方法：从ownermac已知原生目录中，为Pi、Codex、CC各取一份有usage、已稳定至少10分钟、大小小于2MB、以完整换行结束的文件；Codex选择无parent/subagent声明的root日志，避免仅复制child却丢parent前缀证据。**这是有界样本选择，不是平台来源完整inventory。**

原文件没有被修改。原字节只进入vultr自有工作区内0700目录、0600私有副本；SHA-256与读取时原字节一致，collector使用独立ledger，不污染正式usage ledger。发布物只含hash、数量、结果和限制，不含原文、原始session/request ID、私有绝对路径或凭证。

| 样本 | 原生行数 | 导入源事实 / 去重后usage事件 | 独立复算 = collector Token | 再次回放新增 | 数量是否改变 |
|---|---:|---:|---:|---:|---|
| Pi | 8 | 2 / 2 | **7,077** | 0 | 否 |
| Codex | 144 | 3 / 3 | **1,502,000** | 0 | 否 |
| CC | 14 | 5 / 3 | **77,740** | 0 | 否 |

独立算术没有调用ComputeCost聚合：
- Pi：逐assistant原生usage的input+cacheRead+cacheWrite+output求和。
- CC：按native message ID去掉重复观察，再求互斥四类计数。5条source事实折为3个usage事件不是漏掉两次执行。
- Codex：逐原生token_count的last_token_usage独立求和，并验证同计数代次内total差分与last一致、边界重置的首total等于last；不是把每个累计快照相加。

这些样本是此前存在的原生日志，不是本轮生成的合成fixture。mtime分别为Pi 2026-09-13、Codex 2026-09-12、CC 2026-08-28；不把CC旧样本冒充当前SDK或当前部署验证。

### 3.1 UAT中发现并修复的Codex计数代次问题

首次回放在2c64569b得到1,085,638 Token；独立核对发现漏计一个回合：

- A末累计385,293；B首total=last=75,549，证明新计数起点；
- B末累计416,362；C末累计1,116,707；
- 独立last逐调用合计为 **1,502,000**。

旧逻辑直接跨代次减累计值，将B当成负增量异常而排除。637cb121只在**新turn首更新下降，且native total与native last逐已知字段一致**时重新建立零起点。没有该证据的下降保持quantity unknown、source partial，不能猜零基线；当前不宣称无证据的mid-turn reset能精确重建。

新增last-proven/last-absent与显式null回归。对同一原字节副本重跑后，collector与独立值一致，重复回放0新增；这次真实UAT失败与修复没有被抹掉。

### 3.2 不可外推的边界

- 三个样本都缺独立execution inventory，coverage仍为 **unknown**，不是三运行时“全部消耗无遗漏”。
- 没有为这些来源伪造Agent/WI归属，不能当作WI568/其他WI完整成本。样本不相加冒充平台总量。
- 本轮没有加载价格目录，machine_cash_usd均为null；不把未知分区或未知价格填零。
- Pi和Codex原文件mode为0644，CC为0600。私有回放副本均为0600，但**没有因此修复或隐藏原来源权限缺陷**；生产privacy门仍待处理。
- 没有验证当前CC-SDK/App Server等每个入口、每台主机的新请求连续生成、断电补采、跨机重传、完整WI/父子归集、自然收益或正式ROI。

## 4. 后续 UAT 前沿

| 阶段 | 当前状态 | 退出条件 |
|---|---|---|
| U0 固定代码/反例/复审 | 已完成本轮门禁 | 固定SHA回归与独立审查，不冒充部署 |
| U1 私有只读原生文件回放 | **已开启；上述三样本首轮通过数量/幂等检查** | 扩大真实版本/失败/子执行样本；每份都要独立算术和明确来源范围 |
| U2 prospective Host shadow | 未开启生产开关 | 三runtime×entry×host inventory、私有registry、写入/延迟/保留/回退预算与真实安装加载读回；不能用U1历史副本代替 |
| U3 自然WI消费者验收 | 未完成 | 既有方案自然WI门、质量/人工投入、完整归属与覆盖；未知保留，不制造WI或付费请求凑数 |
| 正式替换/合并/全量发布 | 未执行 | 完整批准与消费者证据，保留原WI正式验收义务 |

因此，本轮交付为：统一修复已发布到原实施PR的候选代码，并实际开始了新UAT第一阶段；**不宣称整个ComputeCost已经最终验收通过。**
