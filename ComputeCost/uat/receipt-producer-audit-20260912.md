# ComputeCost 三运行时凭据生产现状审计

- 时间：2026-09-12；代码基线：`b8c598fc1ddafaaded50ff5cec82eb44d32c5a89`（已 fetch origin/main）。
- 目的：为[凭据优先迭代方案](../../../specs/pulse-compute-cost-receipt-first-iteration-plan.md)提供事实基础，不作为完整生产 UAT 通过报告。
- 范围：Pi、Codex、CC（CLI 与 SDK）同等基础范围；ChatGPT 网页 draft 按用户决定排除。
- 方法：三个运行时并行只读源码审查、主审补读最新 main 接线、既有测试复跑、独立合成反例、有限现场只读探测。未启动生产模型探测、未修改/重启服务。
- 证据分级：**S** 源码实现；**T** 本轮实际测试；**H** 历史真实证据；**L** 本轮有限现场读回；**U** 未核实。S/T/H 都不自动等于当前生产覆盖。

## 1. 现场读回：不能只按 Pi 规划

旧 `pulse-space/STATS.md` 到期于 2026-09-07，未作为当前部署依据。本次直接读取以下固定来源的有限现状，不刷新或改写共享 STATS。

2026-09-12 10:36 UTC 左右读取 Relay 配置和持久 state；只输出类别统计与摘要，没有发布原始 session/tenant 身份、凭证或全文。

| 来源 | vultr | ownermac | 解释 |
|---|---|---|---|
| Relay 服务 | systemd active；进程启动路径指向 PULSE `b8c598fc…` release | launchd running；plist 指向同 revision release | L：服务/启动路径读回，不证明每个组件已产生完整凭据 |
| 配置 runtime 字段 | Pi 3、Codex 4 | Pi 6、Codex 1、CC 3、未显式指定 4 | L：配置条目计数，不是正在运行的实例数；未指定不猜默认 runtime |
| 持久 state runtime 字段 | Pi 3、Codex 5 | Pi 5、Codex 2、CC 6 | L：恢复状态记录数，不是当前活进程；更新时间分别为 10:21:12Z、07:53:17Z |
| 当日 metering 文件 | UTC 09-12 文件 22 条 | UTC 09-12 文件 12 条 | L：已写入行数，不是总请求数、全天量或覆盖率 |
| metering schema | 两机末行均含 ts/tenant/session_id/model、四类计数和 lineage | 同左 | 未见 request/attempt/runtime/WI 独立字段；lineage 需按其自身合同解读 |

配置内容 SHA-256：vultr `fc7342c2a6fd12510df96bb288b5e5e8b7e0196f45eb4bfe95e66341c73712c3`；ownermac `39df6aad3a8e14059810c636c399718687423a73df7bb8cbde54e27c6f0f4b7b`。不在仓库归档含敏感配置的原文。

额外边界检查（L）：仅枚举已知标准原生目录的 JSONL 数与最新 mtime，没有全量读取正文：

- vultr 标准 Pi 源 31 文件，Codex 标准 sessions 1163 文件（最新 mtime 09-08），CC 标准 projects 243 文件（最新 mtime 09-12）。
- ownermac Codex 标准 sessions 3099 文件（最新 mtime 09-12），CC 标准 projects 82 文件（最新 mtime 08-28）。本机 Pi 使用另有注册的 local-pulse 目录，本次标准目录枚举未覆盖它；WI309 手工审计已使用该实际来源。
- **目录计数不是消费量，更不是部署覆盖。** Codex 配置仍在用而标准目录时间较旧，可能有隔离 CODEX_HOME；必须从实际进程/Host 路径登记解析，不能据此判定没数据。CC 同理。
- tcloud-node2-pulse 只读 SSH 探测失败：hostname resolution failure；本轮该机部署与凭据覆盖 **U**，未尝试更改 DNS 或服务。
- 未完整枚举桌面入口、直接 CLI、计划任务、独立评审器的当前真实进程/环境，也未证明所有隔离 HOME 与源目录。它们是 R1 必须补齐的 inventory，不是隐含排除。

## 2. 现有链路与可靠性矩阵

缩写：`C = Bigbird/ComputeCost`，`R = Bigbird/Relay`。以下位置基于审计 SHA，可用 `git show <SHA>:<path>` 复核。

| 维度 | Pi | Codex | CC / CC-SDK |
|---|---|---|---|
| Relay 实际代码入口 S | `pi-conversation-runtime.ts`：settled 后取 session stats；绑定入口加载 Pi 扩展 | `codex-conversation-runtime.ts:buildCodexCommand`：`codex exec [resume] --json`，非 App Server 常驻订阅 | `pool.ts` print/interactive；`cc-sdk-conversation-runtime.ts:startEngineLoop` 消费 SDK query |
| 普通计量 S | 三者 result 均可进入 `index.ts → metering.record`，不是无 WI 一律没记录 | 同左，CLI 原样 usage 存在字段差异 | 同左，result-only 不等于请求级完整 |
| 规范凭据 S | `runtime/pi-usage-binding.mjs → pi_capture.py/pi_usage.py → SCL baseline/final` | `app_server_observer.py` 库/CLI；另有 `adapters.py` rollout 回溯链 | `adapters.py:_cc_events` projects 回溯链；`runtime_samples.py:build_cc_usage_sample` 接口 |
| 无 WI S | 普通日志可有；原生规范快照扩展未绑定则不启动 | rollout/普通日志可有；epoch observer 要求 WI context | projects/普通日志可有；host-bound sample 要求 binding |
| 请求/汇总粒度 S | assistant 用量 + 可见 toolResult/压缩 usage；session stats 另为汇总 | rollout 按 task 的最终累计水位差分；CLI result 为 turn 汇总 | projects 按 message；SDK result 可为多请求/模型汇总 |
| 真实当前端到端凭据 U | 有历史 seal，但本次未做当前全场景对账 | 未证明 CLI/App Server 的完整实时凭据链 | 未证明 CLI/SDK 的完整实时凭据链 |

### 2.1 Pi：封存强，不等于采集完整

S：

- `C/pulse_compute_cost/pi_capture.py:capture_pi_session_snapshot`：采样/发布进程持有 OS flock；私有文件、fsync/atomic replace、重放不可变 final、源前缀验证。不能为了简化新方案丢掉已有可靠性。
- `C/runtime/pi-usage-binding.mjs:installPiUsageLifecycle`：宿主绑定、baseline 激活、SCL 回读、progress/final。普通 shutdown 是 progress，不是 epoch seal；未恢复绑定则 `managed=false`。
- `R/src/pi-runtime-config.ts`、`work-item-session.ts`、`relay-rollover-runtime.ts`、`runtime-usage-seal.ts`：绑定注入、baseline 门禁、原 source 恢复并封存、宿主独立回读。最新 #507 的 legacy usage retirement 与版本检查已在本基线；不能按旧版本宣称完全没有恢复路径。
- `C/pulse_compute_cost/pi_usage.py:_collect_pi_source` 支持 assistant、`toolResult.usage`、compaction/branch_summary 顶层 usage；同 entry 同内容去重，冲突拒绝。

缺口/风险：

- usage 缺失时 `continue`；“采到的边界内精确”不证明边界内所有模型请求均有 usage。
- tool/compaction/branch summary 分支使用当前 provider/model 状态，未证明是子调用实际模型；缺请求模型不能默认为主模型。
- 能解析父文件里嵌套 usage，不代表自动采全独立子代理会话；需验证 summary 与独立明细是否同范围，不能简单相加。
- post-seal suffix 被排除保持旧证据稳定，但不能由此把真实迟到消费永久丢掉；需要修订/新事实协议。
- writer 锁保证捕获者互斥，不是对所有原生写入者的全局停止证明；progress best-effort 失败不能代替 gap 检测。

H：`uat/wi730-native-producer-uat-2026-08-25.md` 有开放 Pi 样本；`uat/wi568-epoch6-final-seal-uat-20260906.md` 有 cloud-linux-b 32 events / 2,216,156 Token 的真实配对封存。只证明该历史窗口，不能外推所有 Pi 入口或 Codex/CC。

### 2.2 Codex：CLI、rollout、App Server 不能混称一条已完成链

S：

- `R/src/codex-conversation-runtime.ts` 的 `turn.completed` 保存 `event.usage`；失败/timeout/crash 路径不保证发出带 usage result，interrupt 也不能证明失败消费已封存。
- `R/src/metering.ts:record` 识别 Anthropic 风格 `cache_read_input_tokens`，CLI 透传的 Codex `cached_input_tokens` 无对应映射。此为接口审查风险；本轮未做真实调用字段丢失复现。
- `C/pulse_compute_cost/adapters.py:_codex_events` 解析 rollout 累计水位、EOF incomplete task；`_copied_parent_positions`、root/parent 检查、active/archive 去重已有实现。不要重做已实现的复制前缀排除。
- `C/pulse_compute_cost/app_server_observer.py:AppServerEpochObserver` 验证累计非负、previous+last=total，complete 后 seal；ledger 加锁/fsync、重启恢复部分状态。README 明确该切片不启用生产订阅。
- `C/pulse_compute_cost/wi_discovery.py` 对混合 WI component 隔离；该正确的归属保守性，不应导致未来按天/Agent 的已知机器量也消失。

待验证风险（S，未在本轮逐一复现）：

- observer context key 仅 WI/phase/epoch/runtime，多 Agent/thread/surface 共用时分区不足；单 thread context 不自动覆盖子线程。
- 旧水位乱序重放、缺通知后的累计跳跃、seal 后新增观察、重启断线补采需要明确策略。
- rollout 每 task 聚合不能支持虚构的请求级时间/模型分区；通用 ledger 同 ID 语义冲突不等于允许未完成 task 合法增长。
- `_nearest_relay` 按时间邻近等做弱关联；无法从这种匹配升格为精确 Agent/WI 归属。

H：`uat/wi730-app-server-account-uat-2026-08-25.md` 是账户读回，非 WI 订阅；`wi730-native-producer-uat…` 明确 persistent subscription/final epoch pending。`wi568-post-0818-terminal-audit-20260826/audit.md` 记录历史多 WI 重复归属被隔离；不是原生消费全覆盖。

### 2.3 CC：回溯解析已在，SDK 接线与跨粒度核销仍缺

S：

- `C/pulse_compute_cost/adapters.py:_cc_events`：按 message.id/row.uuid 收集；Anthropic input+cache_read+cache_creation 归一。多行消息按扫描遇到的最后一份保留，不等于已验证所有副本冲突与完整度。
- `C/pulse_compute_cost/cc_lineage.py:cc_transcript_lineage`：验证主/子目录和 root；直接 parent 保持 unknown，是正确边界，不是完整多级树证明。
- `R/src/cc-sdk-conversation-runtime.ts:startEngineLoop`：assistant 保留 message，result 展开原消息；stream_event 未见独立 usage 累积器。异常只有 error/exit，首包超时 replay 没有相应独立计量 attempt 凭据。
- `C/pulse_compute_cost/runtime_samples.py:build_cc_usage_sample`：SDK 分支需要预先整理的 event_id/occurred_at/model/usage，不直接消费原生嵌套 SDK assistant。真实 SDK loop 未见调用该规范 sample 的桥接。
- `R/src/work-item-session.ts:prepareBoundEntry` 只接受 Pi/Codex 分支；不能把 schema 中有 cc/cc_sdk 枚举当成正式入口已完成同等绑定。可靠采集不应等待 CC 获得某个 WI 控制面功能。
- `R/src/pool.ts` print 先 finishTurn 的 synthetic result，再透传原 result；`index.ts` 的 response handler 调用 metering。无 usage synthetic result 可产生零行，不能将行数视为真实请求数。interactive 最终 assistant usage 也不能替代完整工具循环。

T：请求明细与 Relay 汇总重叠已复现，见 §4。现有一对一 session/time/四计数匹配无法核销多条 message 的和；未匹配 result 作为 fallback 再入账。

H：`R/poc/cc-sdk/EVIDENCE.md` 是独立 SDK 可用性 PoC，不是 ComputeCost 凭据完整 UAT。`C/tests/test_runtime_samples.py` 是合成 sample 合同测试，也不能代替生产 CC/SDK 接线。

## 3. 通用层可复用与缺口

| 模块 | 可复用 | 必须补齐/验证 |
|---|---|---|
| `events.py:UsageEvent/TokenUsage/Lineage` | 规范类型、source/quality、时间、Agent、可选 WI/血缘 | 不同粒度/attempt、数量/归属/覆盖分离，避免路径推断冒充宿主事实 |
| `ledger.py:UsageLedger` | 月分区、锁、幂等、冲突拒绝、迟到证据机制 | 多 producer 重叠不是同 ID 重放；合法源增长、归属修订与跨机确认要有协议 |
| `query.py:QueryWindow/query_events/query_ledger` | 北京时间日期、已有 Agent/model 等维度、守恒检查 | WI/组合维度、实时 watermark、unknown 桶；不能凭聚合时间制造请求精度 |
| `adapters.py:collect_usage_events` | CC/Codex/Relay collector、复制与血缘处理 | 规范 Pi/SCL、App Server facts 到统一投影尚未等价接通 |
| `runtime_binding.py` / Relay binding | 隐私安全身份、摘要、宿主资源校验 | WI 归属与采集解耦；绑定存在不等于 usage 完整 |
| `wi_report.py/wi_discovery.py/retrospective_usage.py` | 已有 WI 兼容消费者与保守归属 | 不应做普通采集入口；closed window/current route 不能限制未来日/Agent查询 |
| 定价 | OpenRouter 快照、WI 分模型 Token 算术已有资产 | 通用 `query.py` 另有单一参考模型估值；`TokenUsage.input` 包含缓存，旧 `valuation.py` 扣缓存逻辑不能直接消费新的 uncached input。须做显式映射与金额回归，现有 machine_cash 政策不可静默换成实际扣款 |

Metering 的 append 异常只记日志并继续，缺 usage 用零填充、缺原生 request identity、以 result 时间分文件。这是普通运营日志的现实边界，不能包装成已可靠的请求账本。

## 4. 本轮独立反例：CC 明细 + Relay 汇总双计

**合成测试，不是生产调用，不消耗模型 Token。** 在审计基线的真实 `collect_usage_events` 上构造：同 session、同 model、两条 CC message 各 input=100/output=10；随后一条 Relay result 为 input=200/output=20。测试作者设定其覆盖同两条请求，但当前输入没有将该覆盖关系编码成机器可核验的共同身份。

实际结果：

```json
{"expected_disjoint_tokens":220,"collected_tokens":440,
 "adapters":["cc_transcript","cc_transcript","relay_metering"],
 "flags":[["relay_attribution_unavailable"],["relay_attribution_unavailable"],
          ["runtime_unresolved","relay_fallback_usage_semantics"]]}
```

这些 flags 说明存在弱语义，却没有表达具体未决重叠；在测试作者的重叠设定下，collector 产生重复数量集合。**不能只改断言强制返回 220**：相同数量、相近时间也可能是独立消费。应补齐覆盖证据后验证 220，独立消费身份反例验证 440；原始无关联样本只支持披露未决重叠，不能推出精确 220 或精确 440。不能只让报表隐藏数量而不补齐 producer 的覆盖身份。

仓库根可复现（仅临时目录，全部 synthetic identity）：

```bash
PYTHONPATH=Bigbird/ComputeCost python3 - <<'PY'
import tempfile, json
from pathlib import Path
from pulse_compute_cost.adapters import collect_usage_events
with tempfile.TemporaryDirectory() as t:
    p = Path(t); cc = p/'cc'; relay = p/'relay'
    cc.mkdir(); relay.mkdir()
    def write(path, rows):
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    def usage(i, o):
        return dict(input_tokens=i, output_tokens=o,
                    cache_read_input_tokens=0, cache_creation_input_tokens=0)
    write(cc/'fixture.jsonl', [dict(
        type='assistant', timestamp=f'2026-09-12T00:00:0{n}Z',
        sessionId='synthetic-session', uuid=f'row-{n}',
        message=dict(id=f'msg-{n}', model='synthetic-model', usage=usage(100,10)))
        for n in (1,2)])
    write(relay/'2026-09-12.jsonl', [dict(
        ts='2026-09-12T00:00:03Z', tenant='fixture:pulse:telegram',
        session_id='synthetic-session', model='synthetic-model', **usage(200,20))])
    result = collect_usage_events(machine='fixture',
        observed_at='2026-09-12T01:00:00Z', cc_projects_dir=cc,
        relay_metering_dir=relay)
    print(sum(e.tokens.input + e.tokens.output for e in result.events))
PY
# 当前基线输出 440；仅在测试作者设定重叠的 oracle 下，互斥消费预期为 220。
```

## 5. 测试、覆盖与未决项

本轮 T，合并最新 main 到仅含文档变更的本地分支后运行：

```text
PYTHONPATH=Bigbird/ComputeCost python3 -m unittest discover -s Bigbird/ComputeCost/tests -q
Ran 246 tests — OK
node --test Bigbird/ComputeCost/tests/test_pi_usage_extension.mjs
28 tests — 28 pass, 0 fail
```

这说明现有合同回归仍通过，并不反驳 §4 的新反例。未在本轮运行完整 Relay suite、macOS 全部测试或供应商付费请求；不得声称这些通过。

仍须在审核后的 R1/R2 补齐：

1. 三机所有实际入口、隔离 HOME、App Server/CLI/SDK 版本与已加载钩子的完整 inventory；tcloud-node2 本轮不可达。
2. 各 runtime 原生失败/压缩/子执行/混合模型样本；没有样本不能用字段类型声明支持。
3. 从宿主 dispatch 到 source 再到规范凭据的有分母对账；现有测试大多从“已有 usage”开始。
4. Codex cache 字段、App Server context/重放、Pi 子模型与缺失 usage、CC partial-result 更新等逐项反例验证；本审计的代码风险不能直接写成已复现生产事故。
5. 旧 ledger、SCL facts、native rollout 与新投影的防双计迁移策略；不删除或改写历史原始证据。

本提案另经只读独立审查；已补齐覆盖身份反例、跨日/月/时区 aggregate 规则、新旧数量及估值语义映射。复审未发现阻止提交方案 PR 的实质问题。这是方案审查，不是产品独立 Outcome 验收。

**审计结论：有较多可复用计量资产，但三运行时凭据可靠性尚未得到等价证明；至少一处跨源重复已可复现。应优先修复生产、语义与对账边界，再让统一统计消费它们。**
