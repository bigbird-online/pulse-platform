# CR-02 路径盘点：历史本机观察，不是全量迁移准入

> 旧路径观察保留作取证；下文目标映射已被 [PULSE 全平台布局方案](pulse-platform-layout-plan.md) 替代，不可据此执行搬迁。旧采样工具在历史提交保留，当前提案撤回其实现；这些数字不是当前容量或三机状态。

关联：[总方案](agent-harness-unified-governance-plan.md)、[Phase A](agent-harness-migration-phase-a-readiness.md)。

## 2026-09-11 本机已观察对象

下列映射是准备阶段目标，不是已经注册/搬迁的路径。`PULSE_HOME` 必须是正式登记的稳定安装根，不能取当前 WI worktree。其他节点须独立盘点，不能套用本机结果。

| 旧对象 | 目标/处理 | 未闭合项 |
|---|---|---|
| `~/.local/state/agent-harness/scl-kernel/` | `PULSE_HOME/data/agent-harness/scl-kernel/` | 一致迁移 active lineage、journal、附件、checkpoint、effect；不能把根级历史库当活动库 |
| 同根 `scl-host/` | `PULSE_HOME/data/agent-harness/scl-host/` | Host 信任材料必须保全验证；不调用缺钥自动生成接口，不读取/提交私钥正文 |
| `~/.local/state/agent-harness-runtime/` | `PULSE_HOME/data/agent-harness/runtime/` | 实际 Python 安装环境及 LKG；重建路径相关环境，不直接移动 venv |
| `agent-harness/git-broker/` | 目标同名 namespace | Broker 事务、恢复记录、工作区 guard 均需迁移；guard 还绑定实际解释器路径 |
| `agent-harness/worktrees/` | 目标同名 namespace | 管理注册与历史退役记录；通过受审事务更新绑定 |
| `agent-harness/workspaces/` | 目标 `workspaces/` namespace，逐对象确定 | 包含真实业务文件，不是纯管理元数据；保留 dirty/staged/untracked、Git common dir 与 worktree 关系，禁止直接 mv/clean |
| `agent-harness/checkpoints/`、`scl-transfers/`、`reinit-drafts/` | 各自同名 namespace | 区分有效事实、未结对象、过期候选；默认保全，不以目录名认定可删 |
| `agent-harness/mutation-outbox/`、`publisher-dispatch-outbox/`、`outcome-recovery/` | 各自同名 namespace | 已完成不重发、unknown 不当未发生，保持原操作关联 |
| `agent-harness/capacity-admission-receipts/`、`convergence/`、`convergence-inputs/` | 各自同名 namespace | 保留历史证据，不能将旧路径准入回执冒充新路径准入 |
| `agent-harness/human-dirt-inventory/`、`human-settlement/`、`workspace-sync/`、`skill-adoption/` | 各自同名 namespace | 人类成果、同步与结算恢复对象逐项保全 |
| `agent-harness/` 根部历史 WI 文件和目录 | 带来源映射的受控归档/原 namespace | 只观察到名字，未读内容；未确认语义前不删、不静默遗漏 |
| 当前 managed service checkout 的 `.harness` | PULSE 内专用 PULSE service checkout 的 `.harness` | 完整 Git 数据及发布权切换，不能只迁 Pulse WI |
| `~/.local/bin/` 中 Harness launcher、服务/cron/Relay 显式配置 | 指向 PULSE 内实际运行物 | 薄 launcher 可留在系统入口；所有默认值/fallback 与后台消费者需读回 |

这张表补充实际发现，不覆盖 macOS、Vultr、Actions 的对象清单，也不宣称覆盖所有外部依赖。尤其实际 Python 解释器/依赖缓存可能在通用工具 namespace；要区分可重建依赖和必须迁移的 Harness 专属状态。

## 本机非原子容量采样

23:34 UTC 用 `Scripts/harness-migration-inventory.py` 对三个显式、互不重叠的根执行只读元数据扫描：

| 根类别 | 普通文件数 | logical bytes（跨根硬链接去重） | allocated bytes | 未跟随软链接 |
|---|---:|---:|---:|---:|
| 完整 `agent-harness` 状态根 | 30,050 | 261,307,238 | 336,879,616 | 4 |
| 外层 `agent-harness-runtime` | 11,043 | 313,899,435 | 338,751,488 | 36 |
| inherited ledger | 4,215 | 36,971,298 | 47,767,552 | 0 |

三根同一文件系统，可用 20,971,593,728 bytes，不能加三遍。扫描返回 needs_review/退出 2；不存在读取错误不等于闭包完整。尚未计目录元数据、备份、重建 venv 膨胀、临时导出、候选/LKG 共存、增长余量及目标文件系统；这不是峰值容量准入。

kernel 内两个软链接已单独观察为 `runtime/active` 和 `runtime/previous`，目标均位于本 kernel 的 releases 子树且存在；它们指向的 shell launcher 又调用外层 runtime，不能因链接留在 kernel 内就判定运行物闭包完成。其余链接仍待逐项分类。

## 当前实际入口发现

Relay 进程使用的 SCL launcher 为 kernel 的 `runtime/active/bin/harness-scl`，当前转至兼容 runtime。其 `HARNESS_BIN` / `PULSE_HARNESS_BIN` 环境仍记录旧 CWA `.venv` 路径。这是进程环境观察，不证明这些值当前都被调用；消费位置和升级后重启行为待验证。

该发现必须进入消费者切换矩阵：只更新 SCL wrapper 或修好当前 Git guard，不等于通用 Harness、Relay、cron 与恢复路径已经统一。
