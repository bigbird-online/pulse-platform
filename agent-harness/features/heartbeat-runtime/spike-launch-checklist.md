---
feature_name: "heartbeat-runtime"
document_type: "spike-launch-checklist"
version: "0.0.1"
created: "2026-04-19T11:15:00+0800"
modified: "2026-04-19T11:15:00+0800"
tags: [spike, checklist, heartbeat, p0, reeve]
---

# heartbeat-runtime Spike Launch Checklist

> Reeve 一周 spike 启动前的手动验证清单。对齐 requirements §AC-P0-012 / tasks §T8 阶段 B。

---

## 前置（一次性环境准备）

- [ ] `claude` CLI 可用：`which claude` 能找到；`claude --help` 含 `--allowedTools` 选项
- [ ] `harness` CLI 可用（`uv tool install` 已安装）：`harness --help` 列出 `scan` / `dispatch` / `policy` / `escalation` 四组命令
- [ ] Reeve workspace 存在：`ls /Users/owner/Coevolution/.harness/reeve-space/` 有文件或至少目录存在
- [ ] Reeve policy 就位：`harness --root /Users/owner/Coevolution/.harness policy validate reeve` 返回 `valid`
- [ ] Reeve 的 TG bot token 环境变量可被 Relay spawn 的进程读到：`RELAY_BOT_TOKEN_REEVE` 或通用 `HARNESS_ESCALATION_BOT_TOKEN`
- [ ] 大鸟 TG chat_id 确认：policy `escalation.recipient` 与实际使用的 chat_id 一致（当前：`1000000001`）

## Relay cron jobs 合入

- [ ] Pulse 把 `features/heartbeat-runtime/cron-jobs-proposed.json` 的 2 条 job **合入**（不是替换）到 `PULSE/Relay/config/cron-jobs.json`
- [ ] Pulse 验证：Relay 重启后 `systemctl status relay` 或 launchd 日志显示 `heartbeat-reeve-scan` + `heartbeat-daily-flush` 两条 job 注册
- [ ] 保持 `enabled: false` 状态，直到本 checklist "Spike 启动" 段

## Spike 启动前冒烟测试（不打开 cron）

### 冒烟 1：scan 手动跑

```bash
cd /Users/owner/Coevolution
harness scan reeve --mode manual --no-enforce-lock --json
```

- [ ] 返回 `verdict=clean` 或列出实际 Reeve 的 pending handoffs
- [ ] 退出码 0（clean）或 1（有命中）
- [ ] `.harness/events/scan_event-2026-04.jsonl` 追加一行
- [ ] 隐私边界：事件行不含 handoff input_summary 或业务原文

### 冒烟 2：dispatch --dry-run

- [ ] 构造 1 条测试 handoff 给 Reeve：`harness assign --to reeve --from ken --input-summary "spike smoke test" --expected-output "ack" --work-item <已存在的 test wi>`
- [ ] 再跑 `harness scan reeve --mode manual --no-enforce-lock --json` → 得到 scan_id
- [ ] `harness dispatch <scan_id> --dry-run` → 打印 dispositions；验证 1 条命中被分类为 `autonomous_llm`
- [ ] 没有真实 spawn 发生（CC usage 无新增）

### 冒烟 3：escalation flush-daily（零事件场景）

- [ ] 当日没有 aggregate_daily 事件的情况下 `harness escalation flush-daily` → 静默退出 0，无 TG push
- [ ] 构造 1 条 aggregate_daily 事件（手工 append 到 `.harness/events/escalation_event-2026-04.jsonl` with `aggregate_policy=aggregate_daily, push_status=pending`）
- [ ] 再跑 flush-daily → TG 收到一条日报合并消息；事件 push_status → sent

### 冒烟 4：autonomy 工具边界（AC-P0-004 核心）

> 本次必须实跑一次真 CC 分身验证 tool allow-list 硬约束，否则 AC-P0-004 不算通过。

- [ ] 准备一个测试 prompt：显式要求 Reeve 分身尝试 `Write` 一个文件到 `/tmp/test-tool-denied.txt`
- [ ] 调 `spawn_autonomous_session`（或用 `dispatch` 把一个 escalate 档改为 autonomous_llm 档跑）
- [ ] 验证：
  - CC 分身退出后，`/tmp/test-tool-denied.txt` **不存在**（Write 被拒）
  - `autonomous_execution_event` 的 `status=tool_denied` 或 stderr 含 "tool not allowed"
  - `escalation_event` 即时 push 到 TG（含 match_label=autonomous_tool_denied）

### 冒烟 5：/wrap 完整性（AC-P0-010）

- [ ] 构造 1 条合规 handoff → cron 分身完整处置 → 退出
- [ ] 验证 Reeve workspace 的 git log 在分身启动时间之后有含 `[cron autonomous]` 的 commit
- [ ] `autonomous_execution_event` status=success
- [ ] 如无 commit，status=wrap_missing 被检测到并即时升级

## Spike 启动

- [ ] 在 Relay `cron-jobs.json` 把两条 heartbeat job 改为 `enabled: true`
- [ ] 重启 Relay（launchd reload / systemctl restart）
- [ ] 记录 baseline：
  - [ ] spike 前 7 日 Reeve Token/CC usage baseline → 写入 `spike-report.md`
  - [ ] spike 前本周大鸟对 Reeve 的 nudge 次数 baseline（人工统计）→ 写入 `spike-report.md`
  - [ ] spike 开始时间戳 `D0 = <当前 ISO8601>`
- [ ] 向大鸟发一条 TG 通知"spike 开启"

## Spike 期间（D0 → D7）

每日检查：

- [ ] 查 `.harness/events/*.jsonl` 增长正常（无空白时段 > 1 小时）
- [ ] 异常监测：
  - [ ] `autonomous_execution_event.status=error` 或 `tool_denied` 数 = 0（若 >0 立即 debug）
  - [ ] `circuit_broken` 事件 = 0（若 >0 立即 debug + 修复后重启 job）
  - [ ] TG push 频率 ≤ 5 条/日（若 >5 调整 policy 阈值）
- [ ] 成本监测：
  - [ ] 每日 CC usage vs baseline，若超 +$1/日立即降频至 `*/30`

## Spike 结束（D7）

- [ ] 关闭 cron job（`enabled: false`）
- [ ] 统计 AC-P0-012 的 7 项指标（scan 触发 / clean 率 / autonomous 成功率 / tool 违规 / push 量 / CC 配额 / S1 自动化处置数）
- [ ] 出 `spike-report.md`
- [ ] 向大鸟汇报是否达标、是否进入 P1（其他 Agent 接入 + policy schema 通用化）
- [ ] 如达标：commit spike-report + 推进 P1 feature spec
- [ ] 如不达标：复盘 + 调整 policy 后再跑一周

---

## 紧急关停

任何时刻发现：
- Reeve 分身做了破坏性/越界动作 → 立即停 cron job + 回滚 Reeve workspace + TG 通知
- CC 配额告急 → 立即停 cron job
- 锁死循环 → 清理 `.harness/locks/reeve.lock` + 停 cron job

关停命令：
```bash
# 禁用 cron job（Relay 本机）
cd /Users/owner/Coevolution/Bigbird/PULSE/Relay
# 手改 config/cron-jobs.json enabled: false
# 重启 Relay 或 SIGHUP
```
