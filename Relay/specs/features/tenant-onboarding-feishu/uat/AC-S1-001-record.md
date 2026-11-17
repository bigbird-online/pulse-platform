---
uat_id: "AC-S1-001"
feature: "tenant-onboarding-feishu"
kind: "准生产（L3 自动化回归） + 真机抽检附录"
executed_at: "2026-05-02T21:28:14+08:00"
time_basis: "北京时间（Asia/Shanghai，UTC+8）"
repo_commit: "05279f096d69b55a66525b367de811e5f3f6f919"
relay_package: "@pulse/relay"
vitest: "4.1.4"
redaction: "未记录真实 open_id、appId、租户名；下文均为占位或测试别名"
---

# AC-S1-001 — UAT 记录（T0-004）

> **需求出处**：`requirements.md` — 未绑定会员 → 配对 → 管理员 `/approve` → 同用户文本 → Interactive Card；全程无 TG。  
> **清单出处**：本 feature `tasks.md` §「飞书会员 Onboarding 检查清单（定稿）」。

## 1. 本轮结论

| 项 | 结果 |
|----|------|
| **准生产** | 在提交 `05279f0` 上执行 `vitest run tests/feishu-channel.test.ts`：**77 passed**，exit **0**。 |
| **AC-S1-001 与用例映射** | 见 §2（配对、`/approve`+`bindings[]`、卡片下发）。 |
| **≤30s 响应** | 单元/组件测试不计量网络与 CC 时延；**真机**补录见 §3 附录表。 |
| **无 Telegram** | 本回归文件仅覆盖飞书渠道逻辑；交付战略与 `specs/pulse-product-strategy.md` 一致，真机抽检不启用 TG。 |

## 2. 验收映射（自动化）

| AC 要点 | 覆盖方式（vitest） |
|--------|-------------------|
| 未绑定用户收到配对码与 `/approve` 指引 | `text message from unregistered user starts pairing flow` |
| 管理员 `/approve` 写入 `bindings[]` active，含 `feishu-pairing` 审计标签 | `/approve writes an explicit active binding instead of agent feishuOpenId` |
| 助手回复以飞书卡片（Schema 2.0）下发 | `buildMarkdownCard` 系列、`sends card for assistant response` 等 |
| `/status` 等命令路径 | `handleCommand` / feishu 命令相关用例（同文件内） |

## 3. 附录 — 真机飞书端到端（可选补录）

> **更新（2026-05-02）**：**T2-002** — 运营侧已在生产形态下维护 **9** 个飞书 bot，其中 **2** 个经由 **配对码 + `/approve`** 完成绑定（与 B1–B2 一致）；未记录敏感 id。若需与 §3 表逐行对齐，可自行填入时间戳。

> 在**真实或准生产飞书应用**上按 `tasks.md` 检查清单 **B1–B4** 执行后，**追加一行**到上表或新段落；**禁止**粘贴未脱敏 open_id、密钥、聊天记录原文。

| 步骤 | 时间（本地） | 结果 | 操作者（角色） | 备注 |
|------|--------------|------|----------------|------|
| B1 配对指引 | | ☐ | | 占位 tenant：`member-***` |
| B2 `/approve` | | ☐ | | Agent id：`***` |
| B3 卡片回复（≤30s） | | ☐ | | 可记观测耗时 ms |
| B4 `/status` | | ☐ | | |

## 4. 复现命令

```bash
cd Bigbird/Relay
npx vitest run tests/feishu-channel.test.ts
```

---

*本文件用于 **T0-004** 脱敏归档；与 **150-spec-sync** 同步见根 `Doc/ops/spec-driven-developer/150-spec-sync-logs.jsonl`。*
