# 旧内核规范（只读归档）

本目录描述已退役的旧 `harness` 包及其 CLI（`harness`、`harness-scl`、`harness worktree` 等）。
2026-10-18 起 PULSE 单轨运行原生核心 `harness_rewrite`，这些命令和流程不再存在，本目录不再修订。

现行合同在 [`../project-specs/`](../project-specs/)：

- 业务协议与资源退休：[project-protocol.md](../project-specs/project-protocol.md)
- 原生入口与运维：[rewrite-native-operations.md](../project-specs/rewrite-native-operations.md)
- 架构：[project-architecture.md](../project-specs/project-architecture.md)

工作树放置的现行规则：新 WI 工作树默认分配在账户内置盘
`~/.local/state/agent-harness/workspaces/<org>/<repo>/`；终结 WI 的工作树由效果服务自动回收。
