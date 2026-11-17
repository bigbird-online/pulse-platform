# Platform — 规格（Spec）

本目录为 **Platform** 的文档根：**L1** 与 **L2** 均在此处，与 **`Relay/specs`**（网关）**分栈**。

## L1 — 项目层（四件套）

| 文档 | 路径 |
|------|------|
| 愿景 | [`project-specs/project-vision.md`](project-specs/project-vision.md) |
| 架构 | [`project-specs/project-architecture.md`](project-specs/project-architecture.md) |
| 规范 | [`project-specs/project-standards.md`](project-specs/project-standards.md) |
| 路线图 | [`project-specs/project-roadmap.md`](project-specs/project-roadmap.md) |

索引说明：[`project-specs/README.md`](project-specs/README.md)

## L2 — 功能层

| Feature | 路径 |
|---------|------|
| 租户开通与支付编排（先付后绑） | [`features/platform-tenant-provisioning/`](features/platform-tenant-provisioning/requirements.md) |

## 跨栈引用（Relay）

Platform 依赖 Relay 配置与通道语义时，使用 **仓库根** 路径，例如：

- `Relay/specs/project-specs/project-vision.md`
- `Relay/specs/features/tenant-onboarding-feishu/requirements.md`
- `Relay/specs/features/logical-gateway/design.md`

---

*原则：Platform **自有 L1**；Relay L1 **不迁移**，二者 **并列 + 互链**。*
