---
product: "Platform"
layer: "L1"
version: "0.4.0"
created: "2026-05-01T13:00:00+08:00"
modified: "2026-05-31T09:40:00+08:00"
changelog:
  - "v0.4.0: 150-spec-sync — 充值流与注册流分离（Order.source）；灰度测试仅限注册订单；国外模型暂时禁用"
  - "v0.3.0: 150-spec-sync — reload 契约更新（Relay router 热加载）；灰度测试开关；错误码 409/502 区分"
  - "v0.2.0: 150-spec-sync — 付款后与 Step 2 职责拆分（租户目录物化 vs relay.json 合并）"
  - "v0.1.0: 骨架"
related_l2: "Platform/specs/features/platform-tenant-provisioning/design.md"
tags: [l1, platform, architecture]
---

# Platform — 项目架构（L1）

## 逻辑组件

```
[注册/收银台] → [订单与支付状态] → [白名单校验]
        ↓ payment=paid（终态）, source=signup
[Tenant 空间物化] → 分配 tenantId、按 Agent 模板落盘 Tenants/{tenantId}/
        ↓ Step 2：飞书凭证校验通过
[relay.json merge] → users/agents + bindings[]（pending）→ [Relay reload] → 飞书 Bot WS
        ↓ Step 3：配对码
[binding active] → 飞书对话 → CC cwd = Tenants/{tenantId}/

[充值页 RechargePage] → [POST /api/billing/recharge] → 订单 source=recharge
        ↓ payment=paid（终态）, source=recharge
[balanceStore.createOrTopUp] → 幂等叠加余额（跳过灰度测试，保留信用倍率）
```

**充值 vs 注册**：充值订单（`source: 'recharge'`）仅影响余额，不触发 provisioning。灰度测试（¥0.01→¥10）仅限注册订单（`source: 'signup'` 或缺省）。

## 外部系统

| 系统 | 交互 |
|------|------|
| **支付渠道** | 回调 / 查询；签名校验；沙箱与生产隔离；webhook 不可靠时主动轮询 App Platform 支付状态 |
| **Relay** | 消费 `relay.json`；**reload 契约**：`POST /internal/agents/reload` 先重建 router 索引再 spawn WS client，无需重启 |
| **飞书开放平台** | 每租户独立应用；凭证由 Platform 安全存储并写入 Agent 配置 |

## 配置 SSOT

- **Gateway / binding**：以 **`Relay/specs/features/logical-gateway/design.md`** 为准；平台只负责生成符合约定的 **`bindings[]` / users / agents** 片段。
- **租户目录**：与仓库现有 **`Tenants/`** 约定对齐（路径与权限在 L2/M1 收敛）。
- **租户 Agent 模板（workspace 文件集）**：SSOT 在 L2 **`platform-tenant-provisioning`** — `design.md` §1.13；**不得**将仓库根 **`pulse-space/`**（平台管家 OA）整棵复制为会员租户模板。

## 待决（下沉到 L2）

- `relay.json` 单写者、锁、merge 算法
- 密钥存储（env / 文件 / KMS）
- reload 实现方式
