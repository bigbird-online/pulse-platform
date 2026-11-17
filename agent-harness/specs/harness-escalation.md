# SPEC：harness 告警与通知面 — escalation

> version: `0.1.1`
> status: `reverse_engineered_from_code`
> authority: 告警分档、推送通道与决策请求生命周期的**现行实现契约**
> owner: Pulse（wi_20260804_070 全面 spec 化）
> modified: `2026-08-10`
> 版本史：只看 git log

**取证基础**：2026-08-09 代码普查 + spec-anchor 机器验证全绿。

## 0. 结构事实

对外导出面只有五个符号（记账 / 单发 / 日聚合刷送 / 积压刷送 / CLI group），但
dispatcher 直接 import 了两个下划线私有符号——封装承诺已破，本文按事实立约。
严重度词表有三套并存（policy 层 critical/major/minor、Relay 层 high/mid/low、
渲染层 A/B/C），映射藏在函数体内。

## 1. 对外入口与 CLI

- [x] 五个导出符号 + 异常类
  `@anchor symbol record_escalation`
  `@anchor symbol push_now`
  `@anchor symbol flush_daily_aggregate`
  `@anchor symbol flush_aggregate_backlog`
  `@anchor symbol escalation_group`
  `@anchor symbol DailyFlushDeliveryError`
- [x] 六个子命令：flush-daily / emit / list-failed / resolve / list-open /
  retry-failed。emit 退出码契约：0=已推送或已入日聚合，1=推送失败或缺 token，
  2=policy 缺失非法（含 site-policy 拒绝）
  `@anchor symbol flush_daily`
  `@anchor symbol list_failed`
  `@anchor symbol retry_failed`
- [x] dispatcher 直接使用的两个私有符号（受支持的既成事实接口）
  `@anchor symbol _curl_relay_internal_push`
  `@anchor symbol _resolve_relay_internal_endpoint`

## 2. 三档分档

- [x] 档位常量与大字头
  `@anchor const _TIER_A = "A"`
  `@anchor const _TIER_B = "B"`
  `@anchor const _TIER_C = "C"`
  `@anchor symbol _TIER_HEADLINE`
- [x] label→档位查表，**未登记 label 一律 fail-closed 落 C 档**并追加 meta 事件
  （不落 A——内部事务再响也不该冒充「要大鸟出手」）
  `@anchor symbol _LABEL_TIER`
  `@anchor symbol _tier_for_label`
  `@anchor test agent-harness/tests/test_escalation.py :: test_unregistered_label_tier_is_c_not_a`
  `@anchor test agent-harness/tests/test_escalation.py :: test_unregistered_label_emit_appends_meta_event`
  `@anchor test agent-harness/tests/test_escalation.py :: test_meta_label_itself_registered_no_infinite_recursion`
- [x] 决策请求 label 白名单（A 档中「等大鸟拍板」的子集，不老化）
  `@anchor test agent-harness/tests/test_escalation.py :: test_decision_labels_are_a_tier`
  `@anchor test agent-harness/tests/test_escalation.py :: test_probe_labels_not_a_tier`
- [x] label 登记完备性的机器闸：源码里 emit 的 label、scan hit 的 label、policy
  声明的 label 都必须在档位表登记
  `@anchor test agent-harness/tests/test_escalation.py :: test_all_emitted_labels_are_registered_in_tier`
  `@anchor test agent-harness/tests/test_escalation.py :: test_scan_hit_labels_are_registered`
  `@anchor test agent-harness/tests/test_escalation.py :: test_all_emitted_labels_have_plain_language`
  `@anchor test agent-harness/tests/test_escalation.py :: test_all_policy_declared_labels_are_registered`
- [x] 渲染层：人话对照表与按 label 覆盖大字头
  `@anchor symbol _PLAIN_LANGUAGE`
  `@anchor symbol _LABEL_HEADLINE_OVERRIDE`

## 3. 通道与推送

- [x] 阈值常量
  `@anchor const _CURL_TIMEOUT_SECONDS = 10`
  `@anchor const _PUSH_RETRY_ATTEMPTS = 2`
  `@anchor const _RELAY_LAUNCHD_LABEL = "com.pulse.relay"`
- [x] token 解析顺序：按 agent 环境变量 → 通用环境变量 → launchctl 两级 →
  Relay 配置文件精确匹配
  `@anchor symbol _resolve_bot_token`
- [x] TG 成功判定 = curl 退出 0 且响应含 ok:true 字面量，承载函数 _curl_telegram
  （超时/异常一律吞掉返回 False 不外抛）；飞书走 Relay 已声明的本机绑定地址内部推送端点
  `@anchor symbol _curl_telegram`
  `@anchor test agent-harness/tests/test_escalation.py :: test_success_ok_true`
  `@anchor test agent-harness/tests/test_escalation.py :: test_nonzero_returncode_fails`
  `@anchor test agent-harness/tests/test_escalation.py :: test_ok_false_response_fails`
  `@anchor test agent-harness/tests/test_escalation.py :: test_checked_flush_feishu_failure_marks_failed_and_raises`

RELAY_HTTP_HOST 与 RELAY_HTTP_PORT 从现有环境或 launchd 同名变量取得；缺 host 时沿用 127.0.0.1。支持合法 IP 与 localhost，通配绑定转换为对应回环地址；IPv6 使用 URL 方括号。端口须为 1–65535。非法地址、缺端口或缺内部认证拒绝解析，不猜另一个端点，不改令牌或日志输出令牌。

## 4. 记账与日聚合

- [x] 唯一写路径是 JSONL 事件流按月分片；同一事件后写胜出（reduce-latest by
  event_id）
  `@anchor symbol _reduce_latest_state`
  `@anchor test agent-harness/tests/test_escalation.py :: test_flush_reduces_by_event_id_latest_state`
- [x] 日聚合与积压：backlog 从最老月份开始刷，可重试 failed
  `@anchor symbol _build_daily_digest`
  `@anchor test agent-harness/tests/test_escalation.py :: test_backlog_flushes_pending_and_failed_days_oldest_first`
  `@anchor test agent-harness/tests/test_escalation.py :: test_checked_flush_can_retry_failed_daily_event`

## 5. 已知两义与债（登记待裁）

`@anchor manual 主流量绕站点闸 :: flush/emit/retry 三入口有 site-policy 检查，record_escalation 本身没有，而 dispatcher 的 12 处调用全走后者`
`@anchor manual probe-down-fresh 漏登记 :: 该 label 只在大字覆盖表、不在档位表，触发即走 fail-closed C 档并拖一条 label_unregistered 噪音；同型缺口注释自述已中六次`
`@anchor manual scan 动态 label 不在表 :: stale_ 前缀四个、scheduled、schema_invalid 由 scan 拼接产生，档位表无登记，完备性闸只扫字面量赋值扫不到它们`
`@anchor manual 冻结副本落后于真表 :: 档位不变性测试内联的期望表缺后加的十余个 label，新 label 改档不会被它拦`
`@anchor manual 跨仓口头契约无锚点 :: 2026-08-10(wi_20260810_631) 查实:注释声称与 Stats exporter 的 TIER_ACT_LABELS「保持同一份判断」,而该表 2026-08-01 已被 Stats 清空删除——声称有对齐、实则零闸且注释在误导人去对齐一个不存在的东西。2026-08-11(ho_20260811_764)才真正改到源码:此前 9242ed4785 的 commit message 与本条都写了「已改正」,而 escalation.py 零改动——声明跑在动作前面,验收人 ken 复跑 Stats 全量时被那条机器闸抓住。2026-08-13(ho_20260811_445)又查出探针豁免缝隙:活声明行只要顺手写「曾」也会被当历史留档放行,已把历史豁免收窄为「曾声称/原注释/已被删除」等明确更正语义,并加负控。现注释已说清两边**不是也不该是**同一份判断(此表管 TG 推送档位渲染,Stats 那边管菜单栏要不要打扰大鸟,判据不同)。现由 Stats 侧机器闸钉住:Stats/exporter/tests/test_cross_repo_enum_contract.py::test_harness_does_not_cite_a_dead_stats_symbol——本文件再写下指向 Stats 不存在符号的「活的对齐声明」即红(历史留档措辞不误判)`
`@anchor manual 严重度三套词表 :: critical/major/minor 与 high/mid/low 与 A/B/C 并存，映射是函数内联字面量非模块常量`
`@anchor manual resolve 改写发生时间 :: 处置事件复用同一 event_id 但把 occurred_at 改成当下，待决时长读法（取最早）与 reduce-latest 语义相反，同一数据两套读法`
`@anchor manual 导入期改表 :: 档位表与人话表在模块导入时被运行时注入 label_unregistered 条目，静态读字面量会漏`

## 6. 覆盖声明

本 spec 给告警三档、fail-closed 分档、label 完备性闸、通道解析、日聚合与决策请求
生命周期首次挂机器锚点。resolve / list-open 两个子命令无专属测试类，登记为测试
缺口；补齐前它们的行为以代码为准。
