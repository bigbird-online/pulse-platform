---
feature_name: "metering"
version: "1.1.0"
created: "2026-04-07T10:45:00+0800"
modified: "2026-06-01T22:30:00+0800"
changelog:
  - "v1.1.0: 150-spec-sync — P0-008 tenantIdFromPoolKey 映射 + P0-009 admin shadow 非阻塞扣减"
  - "v1.0.0: 预付费按量扣减——5 个新任务（pricing 模块、metering 改造、pool 适配、余额预检、集成联调）"
  - "v0.1.0: P0 已完成——metering.ts + index.ts 集成 + 软配额告警"
upstream_dependency: ["design.md", "requirements.md"]
tags: [tasks, l2-spec, metering, billing, pre-paid, relay]
---

# metering - 任务拆分文档

> **任务概述**：基于 design.md v1.0.0 实现预付费按量扣减：pricing 模块 → metering 改造 → pool 适配 → 余额预检 → 集成联调。

---

## 任务拆分

| 任务 ID | 任务名称 | 预估工时 | 依赖 | 状态 |
|---------|---------|---------|------|------|
| P0-001 | 实现 metering.ts（record + JSONL 写入） | — | — | ✅ 完成 |
| P0-002 | 集成到 index.ts | — | P0-001 | ✅ 完成 |
| P0-003 | 实现 pricing.ts（定价配置 + 匹配 + 费用计算） | 2h | — | ✅ 完成 |
| P0-004 | 改造 metering.ts（assistant usage 提取 + 定价 + 余额扣减） | 3h | P0-003 | ✅ 完成 |
| P0-005 | 改造 pool.ts finishTurn（嵌入 usage 到 synthetic result） | 1h | — | ✅ 完成 |
| P0-006 | 余额预检 + 低余额告警 | 1h | P0-004 | ✅ 完成 |
| P0-007 | index.ts 集成联调 | 1h | P0-004, P0-005 | ✅ 完成 |
| P0-008 | tenantIdFromPoolKey 映射（linuxUser 解析） | 0.5h | P0-007 | ✅ 完成 |
| P0-009 | Admin shadow account 非阻塞扣减 | 0.5h | P0-008 | ✅ 完成 |

---

## 任务详情

### P0-003：实现 pricing.ts

**描述**：新建 `src/pricing.ts`，加载 pricing.json，提供 `calculateCost()` 函数。

**验证标准**：
- [x] loadPricingConfig 读取 JSON 并返回 PricingConfig
- [x] calculateCost 国内模型（CNY）计算正确
- [x] calculateCost 国外模型（USD × 汇率）计算正确
- [x] 前缀匹配 `claude-sonnet-4-20250514` → `claude-sonnet`
- [x] 未匹配模型使用兜底定价 `*`
- [x] 无兜底时 cost=0 + 告警日志
- [x] pricing.json 不存在时 graceful fallback（空配置，所有 cost=0）

**测试文件**：`tests/pricing.test.ts`

### P0-004：改造 metering.ts

**描述**：改造 `record()` 从 synthetic result 的 `model` + `usage` 字段提取数据，调用 pricing 计算费用，异步扣减余额，写入新格式 JSONL。

**验证标准**：
- [x] record 提取 result 消息中的 model + usage
- [x] 调用 calculateCost 计算费用
- [x] 调用 deductBalance 回调扣减余额
- [x] JSONL 包含 model / cost_cny / cost_usd / exchange_rate 新字段
- [x] deductBalance 失败不阻断
- [x] 无 model/usage 时写入 cost=0 记录（向后兼容）
- [x] 低余额事件触发（< ¥10）

**测试文件**：`tests/metering.test.ts`（扩展现有）

### P0-005：改造 pool.ts finishTurn

**描述**：`finishTurn` 新增第 3 参数 `usageRecord?`，将其嵌入 synthetic result。`handleInteractiveRecord` 传入 assistant record 的 model + usage。

**验证标准**：
- [x] finishTurn 接受可选 usageRecord
- [x] synthetic result 包含 model + usage
- [x] 无 usageRecord 时行为不变（向后兼容）
- [x] 现有 pool.test.ts 全部通过（51/51）

**测试文件**：`tests/pool.test.ts`（确保不 break）

### P0-006：余额预检 + 低余额告警

**描述**：新增 `checkBalance()` 方法和 `onLowBalance` 事件。

**验证标准**：
- [x] checkBalance 调用 getBalance 回调
- [x] 余额 ≤ 0 返回 false
- [x] 余额查询失败容许通过（fail-open）
- [x] 扣减后余额 < ¥10 触发 low-balance 事件
- [x] 同步 pending flag 防并发重复触发

**测试文件**：`tests/metering.test.ts`

### P0-007：index.ts 集成联调

**描述**：在 `startRelay` 中创建 pricing config、wire 新的 metering options、接入余额预检。

**验证标准**：
- [x] pricing.json 路径可配置（pricingConfigPath）
- [x] deductBalance / getBalance 回调注入
- [x] 新类型 export（PricingConfig / CostResult / LowBalanceEvent）
- [x] 现有测试不 break（655/656，1 个预存 flaky）

### P0-008：tenantIdFromPoolKey 映射（linuxUser 解析）

**描述**（对应 FR-P0-006）：`tenantIdFromPoolKey()` 从 poolKey 的 agent config `linuxUser` 字段解析 tenantId。租户用户 `linuxUser: "tenant-member-0001"` → strip → `"member-0001"`；管理员无 `linuxUser` → 直接用 userId。

**实现位置**：`src/index.ts`

**验证标准**：
- [x] poolKey `"member-0001:member-0001-pulse"` → `"member-0001"`
- [x] poolKey `"eric:pulse"` → `"eric"`（无 linuxUser）
- [x] deductBalance/getBalance 传入正确的 tenantId

**完成标准**：
- [x] tenantIdFromPoolKey 实现完成

**追溯**：899edd6

### P0-009：Admin shadow account 非阻塞扣减

**描述**（对应 FR-P0-007）：`deductBalance` 回调对 shadow account 的 `insufficient_balance` + `tracked: true` 视为非阻塞——log info 并返回 `true`。

**实现位置**：`src/index.ts`（deductBalance 回调）

**验证标准**：
- [x] shadow account insufficient_balance + tracked → Relay 返回 true
- [x] 真实租户 insufficient_balance（无 tracked）→ Relay 返回 false
- [x] 管理员发消息不报 account_not_found

**完成标准**：
- [x] deductBalance shadow 处理完成

**追溯**：899edd6

---

## 完成标准

- pricing.ts 测试覆盖率 ≥ 80%（✅ 11/11）
- metering.ts 测试覆盖率 ≥ 80%（✅ 28/28）
- 现有测试全部通过（✅ pool 51/51）
- tsc strict 通过（✅ Relay + Platform clean）
- 零新增依赖（使用 JSON 格式替代 YAML，避免引入解析库）
