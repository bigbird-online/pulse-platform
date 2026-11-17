---
feature_name: "metering"
version: "1.1.0"
created: "2026-04-07T10:45:00+0800"
modified: "2026-06-01T22:30:00+0800"
upstream_dependency: ["project-architecture.md", "requirements.md"]
tags: [design, l2-spec, metering, billing, pre-paid, relay]
changelog:
  - "v1.1.0: 150-spec-sync — tenantIdFromPoolKey 从 linuxUser 解析（非 userId）；admin shadow account 扣减非阻塞（tracked 响应）"
  - "v1.0.0: 预付费按量扣减改造——assistant end_turn 提取、定价配置(JSON)、余额扣减、余额预检；pricing.yaml→pricing.json 纠正"
  - "v0.0.1: 初始版本——result 消息 usage 提取 + JSONL 日志"
---

# metering - 技术设计文档

> **设计概述**：从 CC `--bare` 交互模式的 `assistant` 记录中提取 token 用量，按模型定价配置计算费用，通过回调接口扣减租户余额，写入 JSONL 用量日志。
>
> **关联文档**：
> - 需求文档：`features/metering/requirements.md`
> - CC 进程池设计：`features/cc-process-pool/design.md`
> - L1 项目架构：`project-specs/project-architecture.md`

---

## 1. 核心变更

### 1.1 问题

| 问题 | 根因 | 解决 |
|------|------|------|
| metering 记录全零 | CC `--bare` 模式不产生 `type:'result'` 消息，只产生 `type:'assistant'` + `stop_reason:'end_turn'` | pool.ts 在 `finishTurn` 时将 assistant record 的 usage 嵌入 synthetic result |
| 无定价和计费 | 仅有用量记录 | 新增 pricing config + cost calculation + balance deduction |

### 1.2 数据流（改造后）

```
CC session JSONL
  → pool.ts handleInteractiveRecord()
    → stop_reason === 'end_turn'
      → finishTurn(tenantId, proc, usageRecord)   // 新增第 3 参数
        → emit 'response' { type:'result', result:'', model, usage, cost_cny }
          → index.ts wireMetering
            → metering.record()
              → 定价匹配 → 费用计算 → 余额扣减 → JSONL 写入
```

### 1.3 改动范围

| 文件 | 改动 | 影响 |
|------|------|------|
| `pool.ts` | `finishTurn` 新增第 3 参数 `usageRecord?`，嵌入 synthetic result | 内部 API 变更 |
| `metering.ts` | `record()` 从 result 提取 model+usage，定价计算，余额扣减回调 | 核心改造 |
| `types.ts` | 新增 `MeteringOptions` 字段：`resolvePricing`、`deductBalance` | 接口扩展 |
| `pricing.ts` | **新增文件**：定价配置加载 + 匹配 + 费用计算 | 新模块 |
| `index.ts` | wireMetering 传递 model+usage | 适配 |
| `tests/metering.test.ts` | 新增定价计算、余额扣减、assistant 提取测试 | 测试覆盖 |
| `tests/pricing.test.ts` | **新增文件**：定价匹配、费用计算测试 | 新测试 |

---

## 2. 模块设计

### 2.1 pricing.ts — 定价计算模块

```typescript
// src/pricing.ts

interface PricingEntry {
  match: string;          // model 名称前缀（'*' = 兜底）
  display_name: string;
  currency: 'CNY' | 'USD';
  pricing_unit: 'per_million';
  input_price: number;
  output_price: number;
  is_default?: boolean;
  is_default_domestic?: boolean;
  is_default_foreign?: boolean;
}

interface PricingConfig {
  exchange_rate: { usd_to_cny: number; updated_at: string; source: string };
  models: PricingEntry[];
}

interface CostResult {
  model: string;
  cost_cny: number;       // 人民币费用（分）
  cost_usd?: number;      // 美元费用（国外模型）
  exchange_rate?: number;  // 汇率
  matched_entry: string;  // 匹配的定价条目 display_name
  input_tokens: number;
  output_tokens: number;
}

function loadPricingConfig(configPath: string): PricingConfig
function calculateCost(config: PricingConfig, model: string, inputTokens: number, outputTokens: number): CostResult
```

**匹配算法**：
1. 遍历 `models` 列表（按顺序）
2. `match === '*'` 跳过前缀匹配，作为兜底
3. 否则：`model.toLowerCase().startsWith(match.toLowerCase())`
4. 第一个匹配成功 = 命中
5. 无匹配 → 使用 `is_default: true` 条目
6. 无兜底 → cost = 0，告警日志

**费用计算**：
```
raw = (inputTokens × input_price + outputTokens × output_price) / divisor
if currency === USD:
  cost_usd = raw
  cost_cny = raw × exchange_rate.usd_to_cny
else:
  cost_cny = raw
```

### 2.2 metering.ts — 改造后的计量模块

```typescript
interface MeteringOptions {
  meteringDir: string;
  pricingConfigPath?: string;         // pricing.json 路径
  /** 余额扣减回调。返回 true=成功，false=余额不足 */
  deductBalance?: (tenantId: string, costCents: number) => Promise<boolean>;
  /** 余额查询回调。返回余额（分），null=查询失败 */
  getBalance?: (tenantId: string) => Promise<number | null>;
  // ...existing fields...
}

interface Metering {
  record(tenantId: string, sessionId: string, msg: StreamJsonMessage): void;
  /** 余额预检。返回 true=余额充足可 spawn */
  checkBalance(tenantId: string): Promise<boolean>;
  onSoftQuotaWarning(handler: (event: SoftQuotaWarningEvent) => void): void;
  onLowBalance(handler: (event: LowBalanceEvent) => void): void;
  dispose(): void;
}
```

**record() 改造**：
```typescript
function record(tenantId: string, sessionId: string, msg: StreamJsonMessage): void {
  // 从 synthetic result 中提取 embedded usage
  const model = msg.model as string | undefined;
  const usage = msg.usage as UsageData | undefined;

  if (!model || !usage) {
    // 旧版兼容：无 usage 数据的 result 仍然写入（cost=0）
    writeZeroRecord(tenantId, sessionId);
    return;
  }

  // 定价计算
  const cost = calculateCost(pricingConfig, model, usage.input_tokens ?? 0, usage.output_tokens ?? 0);

  // 异步余额扣减（不阻塞主流程）
  if (options.deductBalance && cost.cost_cny > 0) {
    options.deductBalance(tenantId, Math.round(cost.cost_cny * 100)).catch(err => {
      console.error({ ts: new Date().toISOString(), level: 'error', msg: 'Balance deduction failed', tenant: tenantId, error: String(err) });
    });
  }

  // 写入 JSONL
  writeMeteringEntry(tenantId, sessionId, cost, usage);
}
```

**JSONL 新格式**：
```jsonl
{"ts":"...","tenant":"member-000","session_id":"...","model":"claude-sonnet-4-20250514","input_tokens":1234,"output_tokens":567,"cost_cny":15.4,"cost_usd":2.12,"exchange_rate":7.25,"cache_creation_input_tokens":0,"cache_read_input_tokens":890}
```

### 2.3 tenantIdFromPoolKey — poolKey → tenantId 映射

Relay metering 的 `tenantId` 参数实为 poolKey（格式 `userId:agentId[:scope]`），由 `index.ts` 中 `tenantIdFromPoolKey()` 解析为 Platform 余额系统的 tenantId：

```typescript
function tenantIdFromPoolKey(poolKey: string): string {
  const [userId, agentId] = poolKey.split(':');
  const agent = router.getAgents(userId).find(a => a.id === agentId);
  if (agent?.linuxUser?.startsWith('tenant-')) {
    return agent.linuxUser.slice('tenant-'.length); // "tenant-member-0001" → "member-0001"
  }
  return userId; // 管理员（如 "eric"）无 linuxUser，直接用 userId
}
```

**映射规则**：
- **租户用户**：agent config 有 `linuxUser: "tenant-member-0001"` → 去前缀得到 `member-0001`（与 Platform BalanceStore 一致）
- **管理员用户**：无 `linuxUser` 字段 → 使用 userId（如 `eric`），Platform BalanceStore 自动创建 shadow account

### 2.4 Admin shadow account — 非阻塞扣减

管理员（无充值记录）的余额扣减采用 shadow account 策略：

- **Platform `BalanceStore.deduct()`**：当 tenantId 无对应账户时，自动创建 shadow account（`balance=0, totalRecharged=0`），记录 `totalConsumed` 但不实际扣减余额
- **返回值**：`{ ok: false, error: 'insufficient_balance', balance: 0, shadow: true }`
- **Platform `handleBalanceDeduct`**：在 HTTP 响应中附加 `tracked: true`（仅 shadow account 的 insufficient_balance）
- **Relay `deductBalance` 回调**：收到 `tracked: true` 时视为非阻塞，log info 并返回 `true`（不重试、不阻断 metering）

```
管理员 eric 发消息 → poolKey "eric:pulse"
  → tenantIdFromPoolKey → "eric"（无 linuxUser）
  → deductBalance("eric", costFen)
    → Platform: 自动创建 shadow account, totalConsumed += costFen
    → Platform 响应: { ok: false, error: 'insufficient_balance', balance: 0, tracked: true }
    → Relay: 非阻塞，返回 true

租户 member-0001 发消息 → poolKey "member-0001:member-0001-pulse"
  → tenantIdFromPoolKey → agent.linuxUser "tenant-member-0001" → strip → "member-0001"
  → deductBalance("member-0001", costFen)
    → Platform: 正常扣减 balance -= costFen
    → Platform 响应: { ok: true, newBalance: xxx }
    → Relay: 正常
```

### 2.5 pool.ts — finishTurn 改造

```typescript
// 改造前
function finishTurn(tenantId: string, proc: CCProcess): void {
  // ...
  emitter.emit('response', tenantId, { type: 'result', result: '' });
}

// 改造后
interface UsageRecord {
  model?: string;
  usage?: { input_tokens?: number; output_tokens?: number; /* ... */ };
}

function finishTurn(tenantId: string, proc: CCProcess, usageRecord?: UsageRecord): void {
  // ...
  const resultMsg: StreamJsonMessage = { type: 'result', result: '' };
  if (usageRecord) {
    resultMsg.model = usageRecord.model;
    resultMsg.usage = usageRecord.usage;
  }
  emitter.emit('response', tenantId, resultMsg);
}
```

**调用点变更**：
- `handleInteractiveRecord` 第 985 行：`finishTurn(tenantId, proc, { model: record.message?.model, usage: record.message?.usage })`
- print mode 第 1294 行：`finishTurn(tenantId, proc)` — 无 usageRecord（print mode 不在此提取）
- busy timeout 第 842 行：`killProcess` — 无 usageRecord
- 其他 `finishTurn` 调用：保持无 usageRecord

---

## 3. 错误处理

| 场景 | 处理 |
|------|------|
| pricing.json 不存在或格式错误 | 启动时 graceful fallback（空配置），所有 cost=0，不阻断服务 |
| 模型未匹配定价 | 使用兜底定价或 cost=0 + 告警 |
| 余额扣减 API 失败 | 不阻断 CC 会话，下次 turn 重试 |
| 余额预检失败（API 不可达） | 容许通过（宁可多服务，不可因检查而停服务） |
| JSONL 写入失败 | console.error，不抛出 |

---

## 4. 测试策略

| 测试文件 | 覆盖范围 |
|---------|---------|
| `tests/pricing.test.ts` | 定价匹配、费用计算、汇率换算、兜底逻辑 |
| `tests/metering.test.ts` | record 从 assistant usage 提取、JSONL 新格式、余额扣减回调、余额预检 |
| 现有 `tests/pool.test.ts` | 确保 finishTurn 新参数不影响现有行为 |

---

## 5. 不做（Out of Scope）

- 余额账户实现（由 Platform 负责）
- 定价配置热更新 file watcher（v2）
- 自动汇率获取（v2）
- 充值 UI（由 Platform 负责）
