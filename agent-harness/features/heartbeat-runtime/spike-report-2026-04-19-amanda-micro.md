---
feature_name: "heartbeat-runtime"
document_type: "spike-report"
scope: "micro-spike (single-handoff verification, not 7-day continuous)"
subject_agent: amanda
version: "1.0"
created: "2026-04-19T12:25:00+0800"
modified: "2026-04-19T12:25:00+0800"
tags: [spike, report, heartbeat, p0, amanda, s1-automation-verification]
---

# Heartbeat-Runtime Amanda Micro-Spike Report

> **类型**：单 handoff 端到端验证（非 7-day 连续 spike）。目的：证实 S1 自动化核心链路可行——scan → dispatch → spawn CC 分身 → Agent 按人格工作 → ack handoff → /wrap → commit。完整 7-day spike 是 Reeve P1 阶段工作。

---

## 执行窗口

- 触发者：Ken 主 session 手动（manual mode + dispatch）
- 时间窗：2026-04-19T11:57~12:19（~22 min，含一次 timeout 重跑）
- 对象 Agent：Amanda（doc camp，配方规则 Agent）
- 任务：审查 `Doc/amanda-space/` 旧范式文件归档完整性（真实 business task，非 dummy）

## 两次运行对比

| 维度 | Run1 | Run2 |
|------|------|------|
| scan_id | sc_20260419_115747_amanda_827d | sc_20260419_121045_amanda_3282 |
| 触发 handoff | ho_20260419_293 (wi_635) | ho_20260419_914 (wi_596) |
| policy.max_duration_seconds | 300 | 600 |
| autonomous session status | **timeout** | **success** |
| 实际 duration | 330 s | 506 s（< 600 limit 15% 余量）|
| subprocess exit_code | -1 | 0 |
| tool_denied_calls | [] | [] |
| /wrap commit | ❌（timeout kill 中断） | ✅ `bfcf3e23a [cron autonomous] docs(amanda/review): 旧范式归档审查 — 7+44 文件未归档` |
| handoff ack | accepted（分身完成后 ack） | accepted |
| review 报告落盘 | ✅ 11347 B / 171 行 | ✅ 10051 B / 184 行（结构更清晰） |
| escalation 事件 | autonomous_timeout（即时 push，TG failed 因 token 缺） | 无（success 路径） |

## AC-P0 实证（对齐 requirements v0.0.2）

| AC | 要求 | 实证 | 结果 |
|----|-----|-----|-----|
| **AC-P0-005** S1 自动化实证 | Amanda 分身无用户在场下自主完成 ≥1 个 handoff 处置 + `[cron autonomous]` commit | Run2 完整链路闭环 | ✅ 通过 |
| AC-P0-002 ScanEvent 持久化 | scan_event 写 JSONL + 白名单字段 | run1+run2 各一条 scan_event | ✅ |
| AC-P0-006 Autonomy 边界不越线 | 分身按 handoff 边界工作 | run2 仅产出 handoff 要求的 review 报告，无越界文件 | ✅ |
| AC-P0-010 /wrap 完整性 | [cron autonomous] commit by git log 检测 | run2 commit `bfcf3e23a` ✓；run1 因 timeout 未 commit（timeout 状态优先，未检测 wrap） | ✅（run2） |
| Tool allow-list 硬约束 | CC 原生 `--allowedTools` 实际生效 | run1/run2 均 `tool_denied_calls=[]`（Amanda 未尝试越界；本 spike 未诱导越界，见待办）| ⚠ 未实证（无越界尝试）|
| AC-P0-001 scan CLI 正确性 | 构造命中 + scan 返回 JSON + 退出码 1 | Amanda scan 命中 2（handoff + drift）exit 1 | ✅ |
| AC-P0-003 Dispatcher 分流 | first-match policy 正确分流 | `cross_owner_boundary → autonomous_llm` / `major drift → mechanical` 与 Amanda policy 一致 | ✅ |
| AC-P0-007 锁协议 | 人工 session 与 cron 互斥 | 本 spike 未并发测试（Ken 主 session 未锁 amanda lock）| ⚠ 未实证（已有单元/集成 test 覆盖）|
| AC-P0-011 熔断 | 连续 5 次失败 → disable | 未模拟 | ⚠ 未实证 |

## Review 输出的 business 价值（additional benefit）

Amanda 分身**真的**回答了原问题"旧范式文件是否已全部归档"——**答：否**。具体：

- 已归档：`memo/archive/` 11 项（2026-04-17 batch #1）+ `memory/2026-04-15-memory-pre-restructure.md` 快照
- **未归档**：
  - `memory/` 下 **7** 个旧范式操作文档（ROADMAP-old + phase-records）
  - `memory/` 下 **44** 个 session 日志（2026-02-21 ~ 2026-04-03）
  - **4 处**当前活跃文件中的旧范式残留（ROADMAP / TOOLS / MEMORY / IDENTITY）
- 行动建议：batch #2 归档 7 操作文档 / batch #3 归档 44 日志 / D2 后清理当前残留

报告位置：`Doc/amanda-space/memo/review-2026-04-19-paradigm-archive-audit.md`（commit bfcf3e23a）。

## 产品价值函数（proposal v0.4 §"S3 介入频次下降"）实证

| 维度 | 数据 |
|------|-----|
| Ken（S3）投入次数 | **3 次 CLI 调用**（assign + scan + dispatch）|
| Amanda 自主完成 | 审查 amanda-space 全量 + 分类 51 文件 + 产出 10KB 结构化 review + /wrap + commit + ack |
| 大鸟（S3）介入 | **0 次**（除审阅本 spike report 外）|
| 传统路径替代估算 | 大鸟口头告知 Amanda 任务 + Amanda 开 session + 审查 + 手动 commit = 30-60 min 大鸟 attention |

**产品价值函数成立**：单次 spike 就把一次 30+ min 的 S3 协调压缩为 3 次 CLI 调用。

## 改进点识别

### P0（部署前必解）

1. **Amanda TG bot token 未配置** → escalation push `status=failed`。需决：
   - 为 Amanda 配独立 TG bot（Relay 侧 `RELAY_BOT_TOKEN_AMANDA`）
   - 或 fallback 到 Ken / 大鸟主 bot（简化但消息出处混淆）

2. **timeout-kill 前 intermediate commit 未实现**：run1 完成审查但 /wrap 时被杀 → 工作 unless-手工保留就丢失。建议 dispatcher 在 timeout 前（如 duration > 80% limit）发软信号（SIGUSR1 / 写 `.harness/flush-hint`）让 Agent 提前 /wrap。属 P1 feature 补充，不阻塞首轮部署。

### P1（真实启用后迭代）

3. Amanda `max_duration_seconds=600` 实测 506s 余量 15%。对更复杂的 review 任务可能吃紧。需观察实际分布后调优——建议按任务类型分层（`format_compliant` 300s / `cross_owner_boundary` 600s / etc）。
4. 当前 scan.py 的 classifier 启发式（cross_owner_boundary 靠 `_OWNER_CAMPS` 静态映射）是 P0 最小集。随 Agent roster 变化需维护；P1 迁入 registry 或 policy 声明。

### P2（未阻塞）

5. 未诱导越界实证 tool allow-list 硬约束 → 冒烟 4（AC-P0-004）留待
6. 未演练熔断路径 → 留待连续失败场景或 spike 中途人工构造

## 判定

**P0 核心安全门 + S1 自动化 = ✅ 达标**。heartbeat-runtime 可进入**小范围真实启用**阶段（单 Agent、人工触发观察、然后 cron 自动触发）。

## 下一步（待大鸟决）

见 session 汇报正文 §部署思考。
