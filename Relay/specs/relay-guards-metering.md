# SPEC：Relay 防护/观测/计量面

> version: `0.1.0`
> status: `reverse_engineered_from_code`
> authority: 双向泄漏防线、观测事件流与计量计价的**现行实现契约**
> owner: Pulse（wi_20260804_070 · KR2a）
> modified: `2026-08-09`
> 版本史：只看 git log

**取证基础**：2026-08-09 代码普查 + spec-anchor 机器验证全绿。

## 1. tool-call 泄漏防线（入站拦、出站截）

- [x] 判据合取：文本标记 ∧ 结构完整（400 字符窗口内有参数或闭合）；孤立提及
  不算。入站命中**整条替换**为安全提示（fail-closed）；出站命中截断保留泄漏
  点之前正文，两个截断正则都不中时**原文放行仅标记**（fail-open，注释自承
  理论不该发生）
  `@anchor symbol LEAK_TEXT_RE`
  `@anchor symbol LEAK_STRUCT_RE`
  `@anchor symbol detectInboundToolcallLeak`
  `@anchor symbol stripOutboundToolcallLeak`
  `@anchor test Relay/tests/inbound-leak-guard.test.ts :: "does NOT trip on a bare mention of the marker"`
  `@anchor test Relay/tests/inbound-leak-guard.test.ts :: "handles the namespaced invoke variant"`
  `@anchor test Relay/tests/outbound-leak-guard.test.ts :: "strips the real cross-session re-injection payload (89373f33 leak)"`

## 2. 逐行降解防线（好词豁免版）

- [x] 判据族：好词表（必须含 good——第一版漏 good 致误伤有测试钉着）、文件名
  豁免、异文种独占行、CJK 课件族与单汉字独占行、call 提纲行（复读满二才
  开火）、裸 ASCII 短 token 洪水（连续满三）
  `@anchor symbol scrubDegenerateLines`
  `@anchor symbol DEGEN_GOOD_WORDS`
  `@anchor symbol DEGEN_FILENAME_RE`
  `@anchor symbol DEGEN_CJK_SOLO_CHAR_RE`
  `@anchor symbol DEGEN_CALL_LEAK_RE`
  `@anchor test Relay/tests/outbound-leak-guard.test.ts :: "删旧判据漏掉的新 fence 变体 call/count/care（打地鼠→好词豁免的核心收益）"`
  `@anchor test Relay/tests/outbound-leak-guard.test.ts :: "好词豁免表：Summary/Note/TODO/Context/Bug/Good 独占整行不删"`
  `@anchor test Relay/tests/outbound-leak-guard.test.ts :: "后接正文的 Word: 标题不删（防 ICT: / Resolved: 类 prose 误伤，strict）"`
  `@anchor test Relay/tests/outbound-leak-guard.test.ts :: "call: 复读 ≥2 删提纲行"`
  `@anchor test Relay/tests/outbound-leak-guard.test.ts :: "call: 单次不删"`
- [x] 两档 strictness 同函数：实时出站严档（仅高置信形态开火）、spawn 前扫盘
  宽档；最高置信形态（围栏词悬在 invoke 上方）两档一致开火
  `@anchor test Relay/tests/outbound-leak-guard.test.ts :: "两档对最高置信 fence-above-<invoke> 一致开火"`

## 3. 观测事件流

- [x] obs 写入器：默认开（显式 false 才关）、按日分桶单行 JSON、写失败限频
  告警永不抛
  `@anchor symbol emitObsEvent`
  `@anchor symbol emitDegenObserved`
  `@anchor symbol resolveObsDir`
  `@anchor symbol isObsEnabled`
  `@anchor test Relay/tests/relay-observability.test.ts :: "never throws even when the dir is unwritable (fail-safe)"`
  `@anchor test Relay/tests/relay-observability.test.ts :: "infers critical severity for restarted disposition"`
- [x] 请求踪迹三事件（req.inbound / turn.end / delivery.receipt）：默认关
  （dark deploy），turn.end 去重滤空合成 result，缓存两 token 字段必须带
  （漏记会少报上万倍）
  `@anchor symbol traceInboundRequest`
  `@anchor symbol traceTurnEnd`
  `@anchor symbol traceDeliveryReceipt`
  `@anchor symbol shouldTraceTurnEnd`
  `@anchor symbol isRequestTraceEnabled`
  `@anchor test Relay/tests/relay-request-trace.test.ts :: "defaults to disabled (dark deploy)"`
  `@anchor test Relay/tests/relay-request-trace.test.ts :: "去重效果：一个 print-mode turn 的两条 result 恰好一条通过"`
  `@anchor test Relay/tests/relay-request-trace.test.ts :: "每行都是单行合法 JSON（探针逐行 parse 前提）"`
  `@anchor test Relay/tests/relay-request-trace.test.ts :: "req.inbound / turn.end / delivery 落同一 poolKey 字符串 → 探针可 group-by 配对"`
- [x] fatal 日志保栈（修旧版丢栈）
  `@anchor symbol formatFatalErrorLog`
  `@anchor test Relay/tests/cli.test.ts :: "regression: does NOT collapse to String(err) which drops the stack"`

## 4. 计量与计价

- [x] 计量落盘：按日 JSONL，仅 result 消息触发；恒有字段（ts/tenant/session_id/
  四 token 数）与条件字段（model/cost 两币种/汇率）；月度聚合坏行计数不抛
  `@anchor symbol createMetering`
  `@anchor symbol summarizeMonthlyUsage`
  `@anchor symbol DAILY_FILE_PATTERN`
  `@anchor test Relay/tests/metering.test.ts :: "records result message to daily JSONL file"`
  `@anchor test Relay/tests/metering.test.ts :: "counts malformed lines but does not throw"`
  `@anchor test Relay/tests/metering.test.ts :: "seeds from disk on first record so cross-restart cost survives"`
- [x] 余额链：低于阈值出低额事件、月预算 80% 出软额告警；余额检查三重
  fail-open（无查询函数/查询空/查询抛错一律放行）——余额为零只挡新 spawn
  不挡在飞 turn
  `@anchor const LOW_BALANCE_THRESHOLD_CENTS = 1000`
  `@anchor symbol checkBalance`
  `@anchor test Relay/tests/metering.test.ts :: "calls deductBalance callback with cost in cents"`
  `@anchor test Relay/tests/metering.test.ts :: "returns true (fail-open) when getBalance throws"`
  `@anchor test Relay/tests/metering.test.ts :: "emits low-balance when balance drops below threshold after deduction"`
- [x] 计价：匹配序精确→前缀→通配；无价目模型走通配兜底；坏条目跳过不崩
  （历史 crash-loop 有测试钉着）；汇率兜底 7.25
  `@anchor symbol loadPricingConfig`
  `@anchor symbol calculateCost`
  `@anchor symbol normalizeModelsJson`
  `@anchor test Relay/tests/pricing.test.ts :: "skips entries with undefined match instead of crashing"`
  `@anchor test Relay/tests/pricing.test.ts :: "falls back to wildcard for unknown models"`
  `@anchor test Relay/tests/pricing.test.ts :: "recognizes models.json format when first entry is a price-less alias [bug-20260616]"`

## 5. 已知两义与债（登记待裁）

`@anchor manual billing-component-id 全域零写入 :: 计费组件标识在 turn.end 与计量两条链都缺,Stats 侧按缺字段判 provenance_gap 致四项计费指标永久 unknown;Stats 测试夹具恒带该字段掩盖生产缺口;这是 KR3 候选真账`
`@anchor manual 出站防线单通道 :: 泄漏截断与降解清洗只挂飞书出站,TG 回程只有噪声清理;同一泄漏在 TG 侧可穿透`
`@anchor manual 入站拦截无观测 :: 入站防线命中只写控制台不打 obs 事件,观测面上完全不可见;文档列的六种处置里两种全仓无发射点`
`@anchor manual 同核心正则三份字面量 :: invoke 标记正则在出站防线/入站防线/进程池三处独立维护,声称单一真相源只在前两者成立;池内另有十余个近名判据族与防线侧宽严不一`
`@anchor manual session 字段三义 :: 观测事件里同名字段在三类事件分别是 CC 会话号/池键/关联号,群组配对的主要歧义源`
`@anchor manual tenant 字段装的是池键 :: 计量落盘的 tenant 字段实际值是 poolKey,真租户号只在回调里换算;字段名与语义错位`
`@anchor manual 分与 cents 命名错位 :: 余额事件字段名用 cents 语义是人民币分,跨文件一致但命名系统性误导`
`@anchor manual obs 文件名名实不符 :: 请求踪迹三类事件与降解事件同写 degen 前缀文件,消费方靠 evt 字段区分;Stats 硬编码读死目录与 Relay 可配目录不对称,改目录即失明。2026-08-10(wi_20260810_631) 部分收口:不对称仍在(读侧刻意不读 RELAY_OBS_DIR,理由与代价见 Stats live_contract.py OBS_DIR 上方注释),但已由 Stats/exporter/tests/test_external_input_contract.py 钉住「写侧默认路径==读侧常量」+「写侧 env 名须在读侧被点名」,写侧默认一改即红;残余=env 真被设上时静态闸看不见`

## 6. 覆盖声明

本 spec 给双向泄漏判据、两档降解清洗、观测三事件、计量计价与余额链首次挂显式
锚点。billing_component_id 缺口有 Stats 侧 spec 的 wire::unwired 锚点呼应
（stats-billing-usage.md），两侧合看即缝合层雏形。
