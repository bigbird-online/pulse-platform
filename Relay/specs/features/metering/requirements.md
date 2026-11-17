---
feature_name: "metering"
version: "1.1.0"
created: "2026-04-07T10:45:00+0800"
modified: "2026-06-01T22:30:00+0800"
upstream_dependency: ["cc-process-pool", "session-resilience", "feishu-channel"]
tags: [requirements, l2-spec, metering, billing, pre-paid, relay]
changelog:
  - "v1.1.0: 150-spec-sync — FR-P0-006/007: tenantIdFromPoolKey 从 linuxUser 解析；admin shadow account 非阻塞扣减（tracked 响应）"
  - "v1.0.0: 预付费按量扣减改造：(1) 提取源从 result 改为 assistant end_turn 记录（解决 --bare 模式下全零问题）；(2) 新增模型定价配置、余额扣减、汇率换算；(3) 新增余额预检、低余额告警、余额耗尽停服；(4) 与 platform-tenant-provisioning v0.6.0 联动"
  - "v0.0.1: 初始版本 — 从 CC result 消息提取 usage 写入 JSONL 日志"
---

# metering - 功能需求文档

> **功能概述**：从 CC `--bare` 模式的 `assistant` 记录中提取 token 用量，按模型定价计算费用，从租户余额账户实时扣减，写入 JSONL 用量日志。为预付费按量计费模型提供完整的数据采集和扣费闭环。
>
> **关联文档**：
> - CC 进程池：`features/cc-process-pool/requirements.md`
> - 平台租户开通：`Platform/specs/features/platform-tenant-provisioning/requirements.md`
> - L2 技术设计：`features/metering/design.md`
> - L2 任务拆分：`features/metering/tasks.md`

---

## 1. 背景与约束

### 1.1 核心问题

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| Metering 记录全零 | CC `--bare` 交互模式不产生 `type: 'result'` 消息，现有代码只监听 `result` 类型 | 改为监听 `assistant` 记录中 `stop_reason === 'end_turn'` 的消息 |
| 无计费能力 | 只有用量记录，无定价、无扣减 | 新增模型定价配置 + 余额扣减 |
| 无余额管控 | 无余额检查，无法实现预付费 | 新增余额预检 + 耗尽停服 |

### 1.2 CC `--bare` 模式 token 数据来源

CC 在 `--bare` 交互模式下，token 使用量报告在 `assistant` 类型记录中：

```jsonl
{"type":"assistant","message":{"model":"claude-sonnet-4-20250514","usage":{"input_tokens":1234,"output_tokens":567,"cache_creation_input_tokens":0,"cache_read_input_tokens":890}},"stop_reason":"end_turn"}
```

**关键事实**：
- `stop_reason === 'end_turn'` 标识一个完整 turn 的结束（而非 `tool_use` 中断）
- `message.model` 字段包含实际使用的模型标识
- `message.usage` 包含完整的 token 使用明细
- 经 API provider 接入的模型（如 OpenRouter 的 `deepseek/deepseek-v4-flash-0731`）同样提供 `usage` 字段

### 1.3 技术约束

| 编号 | 约束 | 说明 |
|------|------|------|
| TC-1 | Relay 通过 JSONL Watcher 读取 CC 输出 | 不能直接拦截 CC 内部状态，只能从 JSONL 文件解析 |
| TC-2 | 模型标识以 CC 输出为准 | `message.model` 可能是别名（如 `claude-sonnet-4-20250514`），定价配置需覆盖 |
| TC-3 | 汇率需定期更新 | 国外模型 USD 定价需转换为 CNY 扣减 |

---

## 2. 用户故事

**故事1：准确记录每条消息的 token 用量**
- **作为**：PULSE 平台管理员
- **我希望**：每条 CC 回复的 token 使用量被准确记录
- **以便**：可以核算成本和收费
- **优先级**：P0

**故事2：按消息实时扣减余额**
- **作为**：PULSE 租户
- **我希望**：我的余额按实际使用量扣减，每条消息扣一次
- **以便**：用量透明，不用担心月底账单
- **优先级**：P0

**故事3：余额不足时收到提醒**
- **作为**：PULSE 租户
- **我希望**：余额快用完时收到飞书提醒
- **以便**：及时充值，服务不中断
- **优先级**：P1

**故事4：不同模型不同定价**
- **作为**：PULSE 平台管理员
- **我希望**：国内模型和国外模型有独立的定价配置
- **以便**：灵活定价，覆盖成本
- **优先级**：P0

---

## 3. 功能需求

### P0 功能

**FR-P0-001：从 assistant end_turn 记录提取 usage**
- **描述**：监听 CC session JSONL 文件，当解析到 `type === 'assistant'` 且 `stop_reason === 'end_turn'` 的记录时，提取 `message.model`、`message.usage.input_tokens`、`message.usage.output_tokens`、`message.usage.cache_creation_input_tokens`、`message.usage.cache_read_input_tokens`。
- **来源**：故事1
- **示例**：`{type:"assistant", message:{model:"claude-sonnet-4-20250514", usage:{input_tokens:1234, output_tokens:567}}, stop_reason:"end_turn"}` → 提取 model + usage

**FR-P0-002：模型定价计算**
- **描述**：根据提取到的 `model` 字段，在定价配置（见 §6 定价配置格式）中查找匹配的定价条目。计算公式：
  - 国内模型：`cost_cny = input_tokens × input_price_per_1k / 1000 + output_tokens × output_price_per_1k / 1000`
  - 国外模型：`cost_usd = input_tokens × input_price_per_1k / 1000000 + output_tokens × output_price_per_1k / 1000000`，`cost_cny = cost_usd × exchange_rate`
- **来源**：故事4
- **兜底**：若 `model` 未在定价配置中匹配，使用默认定价（配置中标记为 `default` 的条目）。若无默认定价，记录告警日志，cost 记为 0，不阻断服务。

**FR-P0-003：余额实时扣减**
- **描述**：每次成功提取 usage 并计算费用后，从 Platform 余额 API 扣减租户余额。扣减为原子操作：`new_balance = old_balance - cost_cny`。扣减失败（如 API 不可达）时记录告警日志，不阻断 CC 会话，下次 turn 重试累加。
- **来源**：故事2
- **示例**：租户余额 ¥85.30，本次 cost_cny = ¥0.15 → 新余额 ¥85.15

**FR-P0-004：写入 JSONL 日志**
- **描述**：每条用量记录追加写入 `{meteringDir}/{YYYY-MM-DD}.jsonl`，格式为单行 JSON。在 v0.0.1 基础上新增 `model`、`cost_cny`、`cost_usd`（国外模型）、`exchange_rate`（国外模型）字段。
- **来源**：故事1
- **示例**：`{"ts":"2026-05-27T12:00:00+08:00","tenant":"member-000","model":"claude-sonnet-4-20250514","input_tokens":1234,"output_tokens":567,"cost_cny":0.15,"cost_usd":0.021,"exchange_rate":7.25,"session_id":"..."}`

**FR-P0-005：余额预检**
- **描述**：Relay 在 spawn CC 进程前，检查租户余额。余额 ≤ ¥0 时拒绝 spawn，返回固定消息。余额检查为轻量级操作，不增加可感知延迟（< 50ms）。
- **来源**：故事2
- **关联**：`platform-tenant-provisioning` FR-P0-024

**FR-P0-006：tenantIdFromPoolKey 映射**
- **描述**：Relay metering 的 poolKey（格式 `userId:agentId[:scope]`）需正确解析为 Platform BalanceStore 使用的 tenantId。映射规则：若 agent config 有 `linuxUser` 字段（如 `"tenant-member-0001"`），去掉 `"tenant-"` 前缀得到 tenantId（如 `"member-0001"`）；无 `linuxUser` 的管理员用户直接使用 userId（如 `"eric"`），Platform 自动创建 shadow account。
- **来源**：bug-20260601 account_not_found（旧版 `poolKey.split(':')[0]` 返回 userId 与 BalanceStore tenantId 不匹配）
- **示例**：poolKey `"member-0001:member-0001-pulse"` → agent.linuxUser `"tenant-member-0001"` → tenantId `"member-0001"`；poolKey `"eric:pulse"` → 无 linuxUser → tenantId `"eric"`

**FR-P0-007：Admin shadow account 非阻塞扣减**
- **描述**：管理员（无充值记录）的余额扣减由 Platform 自动创建 shadow account（`balance=0, totalRecharged=0`）。shadow account 的 `insufficient_balance` 响应附加 `tracked: true`，表示消费已记录但无实际扣减。Relay 收到 `tracked: true` 时视为非阻塞——log info 并返回 `true`，不重试、不阻断 metering。真实租户的 `insufficient_balance`（有充值记录但余额耗尽）仍按原策略返回 `false`。
- **来源**：管理员不需要扣费但需记录消费金额
- **示例**：eric 发消息 → deductBalance("eric", 15分) → Platform shadow account `totalConsumed += 15` → 响应 `{ok: false, error: 'insufficient_balance', balance: 0, tracked: true}` → Relay 返回 true

### P1 功能

**FR-P1-001：低余额告警**
- **描述**：扣减后余额 < ¥10 时，通过飞书 Bot 主动推送通知。每次充值后重置告警状态（避免重复推送）。余额 ¥10~50 时在 Agent 回复末尾追加轻量提示。
- **来源**：故事3
- **关联**：`platform-tenant-provisioning` FR-P0-023

**FR-P1-002：月度用量汇总**
- **描述**：`summarizeMonthlyUsage()` 从 JSONL 日志聚合每月用量，输出：总 token 数、总费用（CNY）、按模型分类明细。供管理员查看和租户对账。
- **来源**：运维需求

**FR-P1-003：汇率更新机制**
- **描述**：汇率配置支持手动更新（配置文件）和自动更新（可选，定时从外部 API 获取）。默认使用手动更新。汇率变更记录到日志。
- **来源**：运维需求

---

## 4. 验收标准

### P0 验收标准

**AC-P0-001：assistant end_turn 触发用量记录**
- **条件**：CC 产生 `type: 'assistant'` + `stop_reason: 'end_turn'` 的 JSONL 记录
- **预期结果**：对应日期的 JSONL 文件新增 1 行，包含 ts、tenant、model、input_tokens、output_tokens、cost_cny 字段
- **验证方式**：Unit Test

**AC-P0-002：定价计算准确**
- **条件**：已知 model 和 token 数
- **预期结果**：`cost_cny` 按定价配置计算，误差 < 0.01 分。国外模型正确应用汇率
- **验证方式**：Unit Test（多组 model + token 数据）

**AC-P0-003：余额扣减原子性**
- **条件**：租户余额 ¥100，CC turn 产生 ¥0.15 费用
- **预期结果**：扣减后余额 = ¥99.85。并发扣减不丢失不重复
- **验证方式**：Unit Test（并发场景）

**AC-P0-004：余额预检拦截**
- **条件**：租户余额 ≤ ¥0，用户发送新消息
- **预期结果**：CC 不启动，用户收到 "⚠️ 余额已用完" 固定回复
- **验证方式**：Unit Test

**AC-P0-005：未知模型兜底**
- **条件**：CC 返回的 model 不在定价配置中
- **预期结果**：使用默认定价或记录 0 费用（不阻断服务），告警日志记录未匹配的 model
- **验证方式**：Unit Test

**AC-P0-006：按日分文件**
- **条件**：不同日期的 CC turn
- **预期结果**：写入不同日期的 JSONL 文件
- **验证方式**：Unit Test

**AC-P0-007：JSONL 格式合法**
- **条件**：读取 metering JSONL 文件
- **预期结果**：每行都是合法 JSON，包含所有必需字段
- **验证方式**：Unit Test

### P1 验收标准

**AC-P1-001：低余额通知**
- **条件**：扣减后余额从 ¥12 降至 ¥8
- **预期结果**：飞书 Bot 推送 "💡 余额不足 ¥8，请及时充值" 通知
- **验证方式**：Unit Test

**AC-P1-002：月度汇总**
- **条件**：指定租户 + 月份
- **预期结果**：返回总 token、总费用、按模型明细
- **验证方式**：Unit Test

---

## 5. 非功能需求

### 性能需求
- **用量记录延迟**：JSONL 追加 ≤ 10ms（本地文件 appendFile）
- **余额扣减延迟**：单次扣减 ≤ 100ms（含网络往返）
- **余额预检延迟**：CC spawn 前检查 ≤ 50ms

### 安全需求
- **余额操作原子性**：扣减必须为原子操作，防止并发超扣
- **日志不可篡改**：metering JSONL 为追加模式，写入后不修改

### 可用性需求
- **扣减失败容错**：余额 API 不可达时，用量仍记录到 JSONL，不阻断 CC 会话
- **定价配置缺失容错**：未知模型使用默认定价，不阻断服务

### 可维护性需求
- **定价配置热更新**：修改定价配置不需要重启 Relay
- **汇率可追溯**：每条国外模型记录包含当时的汇率

---

## 6. 定价配置格式

### 6.1 配置文件

路径：`/opt/PULSE/config/pricing.json`（git 管理，支持热更新；JSON 格式避免引入 YAML 解析依赖）

```json
{
  "exchange_rate": {
    "usd_to_cny": 7.25,
    "updated_at": "2026-05-27T10:00:00+08:00",
    "source": "manual"
  },
  "models": [
    {
      "match": "deepseek/",
      "display_name": "DeepSeek（OpenRouter）",
      "currency": "USD",
      "pricing_unit": "per_million",
      "input_price": 0.09,
      "output_price": 0.18
    },
    {
      "match": "claude-sonnet",
      "display_name": "Claude Sonnet",
      "currency": "USD",
      "pricing_unit": "per_million",
      "input_price": 3.0,
      "output_price": 15.0,
      "is_default_foreign": true
    },
    {
      "match": "claude-opus",
      "display_name": "Claude Opus",
      "currency": "USD",
      "pricing_unit": "per_million",
      "input_price": 15.0,
      "output_price": 75.0
    },
    {
      "match": "claude-haiku",
      "display_name": "Claude Haiku",
      "currency": "USD",
      "pricing_unit": "per_million",
      "input_price": 0.80,
      "output_price": 4.0
    },
    {
      "match": "*",
      "display_name": "Unknown Model",
      "currency": "CNY",
      "pricing_unit": "per_million",
      "input_price": 5.0,
      "output_price": 15.0,
      "is_default": true
    }
  ],
  "model_groups": [
    {
      "id": "foreign",
      "display_name": "国外模型",
      "description": "Claude Sonnet / Opus / Haiku",
      "default_match": "claude-sonnet"
    }
  ]
}
```

### 6.2 匹配规则

1. 遍历 `models` 列表（按顺序），对 `match` 做前缀匹配（忽略大小写）
2. 第一个匹配成功 = 命中定价
3. 无匹配 → 使用 `is_default: true` 的兜底条目
4. 无兜底 → cost = 0，告警日志

| CC 输出的 model | 命中 | 说明 |
|----------------|------|------|
| `claude-sonnet-4-20250514` | `claude-sonnet` | 前缀匹配 |
| `deepseek/deepseek-v4-flash-0731` | `deepseek/` | 前缀匹配 |
| `some-new-model-v1` | `*`（兜底） | 无前缀匹配 |

### 6.3 费用计算

- **国内模型**：`cost_cny = (input_tokens × input_price + output_tokens × output_price) / divisor`
- **国外模型**：`cost_usd` 同上，`cost_cny = cost_usd × exchange_rate.usd_to_cny`
- `divisor`：`per_million` → 1,000,000

### 6.4 热更新

Relay 通过 file watcher 监听 `pricing.json`：变更 → 解析 → 校验 → 通过则切换（下一条消息生效）。校验失败保留旧配置并记录错误日志。

---

## 7. 约束条件

- **技术栈**：TypeScript 5.x, Node.js ≥ 18, vitest
- **存储路径**：`/opt/PULSE/data/metering/`（可配置）
- **余额 API**：由 Platform 提供（见 `platform-tenant-provisioning` FR-P0-020）

---

## 8. 排除范围

- ❌ 余额充值 UI（由 Platform 负责）
- ❌ 支付对接（由 Platform 负责）
- ❌ 用量可视化仪表盘（后续版本）
- ❌ 自动汇率获取 API（v1 手动配置，v2 考虑自动）
- ❌ 套餐/折扣/优惠券（后续版本）
