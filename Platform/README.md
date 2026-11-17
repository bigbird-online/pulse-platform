# Bigbird / Platform

PULSE **平台层**实现落点（与 `Relay` 并列）。负责 **会员准入、支付与订单闭环、租户开通编排、写入 Relay/Gateway 配置**，**不**承担消息网关、飞书 WS、CC 进程池（由 Relay 负责）。

## 边界

| 归属 | 职责 |
|------|------|
| **`Platform`** | 手机号白名单；注册/支付与回调；订单终态；创建/更新 `Tenants/` 工作区；**仅在支付成功后** merge `relay.json` / `bindings[]`；触发 Relay reload（或等价热加载）；飞书应用凭证的安全注入（**一户一应用**） |
| **`Relay`** | 读 `relay.json`；`resolveInboundRoute`；飞书/TG 通道；`commands` / `pool` |

## 规格

- **SSOT**：`Platform/specs/`（**L1**：`specs/project-specs/` · **L2**：`specs/features/` · 索引见 `specs/README.md`）
- **租户开通（L2）**：`Platform/specs/features/platform-tenant-provisioning/`
- 与飞书会员路径交接：`Relay/specs/features/tenant-onboarding-feishu/requirements.md`（**FR-S1-001**、**AC-S1-003**）

## 技术栈

| 项 | 选型 |
|----|------|
| 运行时 | Node.js >= 18，ESM |
| 语言 | TypeScript strict（与 Relay 对齐） |
| 测试 | vitest |
| HTTP | `node:http`（先无框架，probe 用） |

## 开发与运行

```bash
cd Platform
npm install
npm run build    # 输出 dist/
npm test
npm start        # 默认监听 PLATFORM_HTTP_PORT 或 PORT，缺省 8787
# curl -s http://127.0.0.1:8787/health
```

## 目录约定

```
Platform/
├── README.md
├── package.json
├── tsconfig.json
├── vitest.config.ts
├── specs/
├── src/
│   ├── index.ts       # 进程入口：HTTP listen
│   └── http-server.ts # GET /health、/ping
└── tests/
```

---

*决议：平台代码落在 PULSE monorepo 下，路径 **B** = `Platform/`（2026-05-01）。*
