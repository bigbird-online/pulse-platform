---
uat_type: automated-user-simulation
test_environment: local-ci
real_user_participated: false
real_workload_used: true
simulation_quality: medium
feature_name: platform-tenant-provisioning
rule_basis: Doc/rules/spec-driven-developer/145-automated-user-simulation.md
ssot: Doc/ssot/spec-driven-developer/145-automated-user-simulation-ssot.md
executed_at: "2026-05-05T14:25:00+08:00"
executor: Agent（145 模拟验收）
---

# platform-tenant-provisioning — 自动化 UAT 报告（145）

> **关联需求**：[`../requirements.md`](../requirements.md)（验收标准 **AC-P0-001～AC-P0-008**）  
> **v0.4.2**：`P0-014/015/016` 全部落地。`AC-P0-008` 已由 `tests/stage-a-http.test.ts`（POST `/internal/mock-pay` → `OrderStore.onPaid` → 模板物化 → 文件存在 + 占位符替换）端到端覆盖；待真实大鸟入口 UAT（140）触发后再补本报告 §2 段。  
> **说明**：本报告按 **145-automated-user-simulation** 执行；**真实大鸟操作 + 飞书生产/准生产路由** 属于 **140-uat-acceptance**，见 §6。

## 1. 前置技术测试结果（v0.3.0）

| 项 | 命令 / 范围 | 结果 |
|----|-------------|------|
| 构建（Platform） | `cd Bigbird/Platform && npm run build` | ✅ 通过（tsc + Vite `build:web`，bundle 157.66 kB） |
| 构建（Relay） | `cd Bigbird/Relay && npm run build` | ✅ 通过 |
| 单测（Platform） | `cd Bigbird/Platform && npm test` | ✅ **16 files / 95 tests** 全部通过（Vitest v4.1.5） |
| 单测（Relay） | `cd Bigbird/Relay && npm test` | ✅ **30 files / 503 tests** 全部通过（Vitest v4.1.4） |

## 2. 验收标准 → 用例追溯（Traceability）

| UAT 用例 ID | 追溯 AC | 场景摘要 | 优先级 | 结果 |
|-------------|---------|----------|--------|------|
| UAT-AC-P0-001-1 | AC-P0-001 | 白名单外手机号无法走完「下单 / 支付侧校验」闭环 | P0 | ✅ |
| UAT-AC-P0-002-1 | AC-P0-002 | 未 `paid` 时不得产生可用 provision；`paid` 前编排拒绝 | P0 | ✅ |
| UAT-AC-P0-003-1 | AC-P0-003（子项） | `paid`（Mock）后持久化：`relay.json` bindings + Tenants 路径；本地 HTTP 全链路 | P0 | ✅ |
| UAT-AC-P0-003-2 | AC-P0-003（联合） | 飞书测试 Bot 可收到路由（**AC-S1-003 联合**） | P0 | ⏸ 未在本轮执行 |
| UAT-AC-P0-004-1 | AC-P0-004（v0.3.0） | 用户输入仅 4 项（phone、displayName、appId、appSecret、code）即可完成开通；其余字段服务端按 FR-P0-010 派生 | P0 | ✅ |
| UAT-AC-P0-005-1 | AC-P0-005（v0.3.0） | 错误 secret → UI 看到飞书原始 `code/msg`；不写 `relay.json` | P0 | ✅ |
| UAT-AC-P0-006-1 | AC-P0-006（v0.3.0） | Step 2 写盘后 Relay loopback HTTP `agents/reload` 被触发；不影响其它 bot | P0 | ✅（Relay HTTP `tests/relay-http.test.ts` + Platform `three-step-flow.test.ts`） |
| UAT-AC-P0-007-1 | AC-P0-007（v0.3.0） | Step 3 配对成功 → binding `status: pending → active`、`address.value` 写入 openId | P0 | ✅（Platform `onboarding-http.test.ts` Step 3 happy path + Relay HTTP approve 路径） |

## 3. 执行记录（可复现）

### UAT-AC-P0-001-1

- **依据**：FR-P0-001 / AC-P0-001（白名单外不得进入「可 provision」路径）。
- **自动化证据**：`tests/onboarding-http.test.ts` — `POST /internal/orders` 非白名单 **403**；`tests/payment-create-order.test.ts` 等同源校验与 allowlist；`tests/provision.test.ts` — allowlist 不匹配 **not_allowlisted**。
- **结论**：✅ 通过。

### UAT-AC-P0-002-1

- **依据**：FR-P0-002 / FR-P0-004 / AC-P0-002。
- **自动化证据**：`tests/provision.test.ts` — `rejects unpaid order` → `not_paid`；`tests/payment-create-order.test.ts` — 非 `pending` 订单拒绝扫码下单。
- **结论**：✅ 通过。

### UAT-AC-P0-003-1

- **依据**：AC-P0-003 中「paid 后持久化、可触发 reload 钩子」等**本仓可测部分**。
- **自动化证据**：`tests/onboarding-http.test.ts` — `full flow: order → mock-pay → provision`；`tests/provision.test.ts` — 写 `relay` + `markProvisioned`；`tests/relay-merge.test.ts`、`file-lock.test.ts` 支撑 merge/锁；支付适配：`tests/app-platform-payment.test.ts`、`tests/payment-provider.test.ts`。
- **结论**：✅ 通过（**本地 CI 环境**，含 Mock 支付；非微信真扣款）。

### UAT-AC-P0-003-2（联合 / 真机）

- **依据**：AC-P0-003 原文「飞书测试 Bot 可收到路由」与 `tasks.md` §半自动验收步骤 7。
- **本轮**：未启动真实 Relay + 飞书；**建议在准生产按 tasks 半自动清单或大鸟 140 UAT 补做**。
- **结论**：⏸ 未测（非失败，属**范围外**）。

### UAT-AC-P0-004-1 / 005-1（v0.3.0 三步流单测覆盖）

- **依据**：`tests/three-step-flow.test.ts` 9 条；`tests/onboarding-http.test.ts` 11 条；`tests/feishu-credentials.test.ts` 6 条；`tests/tenant-id-allocator.test.ts` 11 条。
- **关键断言**：
  - Step 1 `POST /internal/orders` 接受 `displayName` 并以 `member-NNNN` 序列分配 tenantId；
  - Step 2 仅接受 `{orderId, feishuAppId, feishuAppSecret}`，名称按 FR-P0-010 派生（`{tenantId}-pulse` / `{displayName}的pulse管理员`）；
  - 错误 secret → 400 `invalid_feishu_credentials` + `feishuCode/feishuMsg` 透传；`relay.json` 不被写入；
  - Step 3 `POST /internal/pair` 通过 `relay-pairing-client` 转发到 Relay，错误透传（`pair_code_unknown` / `pair_agent_mismatch` / `pair_relay_unreachable`）。
- **结论**：✅ 通过。

### UAT-AC-P0-006-1（Relay loopback HTTP）

- **依据**：`Bigbird/Relay/tests/relay-http.test.ts` 10 条；`feishu-pairing-http` Spec。
- **关键断言**：`/internal/health` / `/internal/agents/reload`（含异常路径）/ `/internal/pairing/approve`（404/409/200/config_missing_agent）；端口仅监听 127.0.0.1。
- **结论**：✅ 通过。

## 4. 模拟真实度（Simulation Fidelity）自评

- **真实环境**：Node 进程内 **真实** `http.Server` + **真实**操作系统临时目录中的 `relay.json` / `Tenants/`（非纯 in-memory OrderStore 投机路径；OrderStore 仍为进程内存储，与 design 一致）。
- **局限**：支付为 **Mock 或 stub fetch**，非微信真实扣款；**未接** 线上 App 管理平台 Webhook 公网回调；**未验** 飞书侧消息路由。
- **评级**：`simulation_quality: medium`（符合 YAML 声明）。

## 5. 人类视角结论（Human perspective）

- 若我是运营/大鸟：当前结果说明「先付后绑」在 **平台 HTTP + 文件落盘** 维度可用且回归可被 `npm test` 锁死；**上线信心**仍依赖 **真实支付密钥 + Webhook URL + Relay reload + 飞书** 的一次联调或 140 签字。
- **无** 已发现的阻断级逻辑失败；**已** 标注联合验收缺口。

## 6. UAT 验收决策

| 维度 | 说明 |
|------|------|
| **145 自动化范围** | ✅ **通过** — 可复现子项与测试矩阵全部绿 |
| **L2 AC-P0-003 全文（含飞书）** | ⚠️ **未闭环** — 待 **140** 或 `tasks.md` 步骤 7 真机/准生产 |

**总决策（145）：✅ 通过（带范围声明）**  
**是否等价于全量 L2 UAT 收口：否** — 须在 progress / 里程碑中保留 **AC-P0-003 飞书联调** 直至 140 证据落盘。

## 7. 附录：复现命令

```bash
# Platform
cd Bigbird/Platform
npm run build      # tsc + Vite (dist/web)
npm test           # 95 tests

# Relay
cd ../Relay
npm run build      # tsc
npm test           # 503 tests
```

---

## Gate 自检（对照 140/145 常见项）

- [x] 引用 `requirements.md`（Traceability）
- [x] ≥1 条具体测试用例与步骤（实为数条）
- [x] 明确「通过/带条件」决策
- [x] 诚实标注 `real_user_participated: false`、`uat_type: automated-user-simulation`

*（可选）可将本文件提交 ssot-judge：`Doc/ssot/spec-driven-developer/145-automated-user-simulation-ssot.md`*
