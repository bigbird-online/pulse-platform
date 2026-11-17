# agent-harness

> 源码及过滤历史已归入 PULSE，来源见 [导入证据](../specs/evidence/wi184-source-import/README.md)。PULSE 是 canonical ledger；旧 CWA 根在受支持写入口中拒写，历史可只读访问。源码归属不代表所有运行节点已采用；安装、数据布局和上线门见 [全平台方案](../specs/pulse-platform-layout-plan.md) 与 [运行时契约](specs/harness-platform-epoch.md)，实际采用以各节点的受管回执及运行读回为准。

开发、pytest、持久 CI 和发布构建使用实际运行账户主目录下唯一的 `.local/share/coevolution/python-env`；依赖按 Coevolution `ssot/toolchain/` 统一配置与锁维护。主目录由操作系统账户记录确定，不能通过覆盖 `HOME` 选择另一环境。不在项目内 `uv sync`、安装 editable 包、`uv tool install` 或创建长期私有环境；缺失或不兼容依赖须先修订统一锁。

Agent 团队的操作系统 — 协作状态追踪、行为约束验证、SSOT 漂移检测。

> WI018 替换式重写以[项目规格](project-specs/project-vision.md)为准，业务通过 execute / inspect / resume 进入唯一事实内核；新测试独立于旧 Harness 回归。
> Linux Pi 新核心可直接使用唯一生产 Git 账本；操作入口、授权来源和保留边界见[原生入口指南](project-specs/rewrite-native-operations.md)。TCloud 通过[正式 CD 合同](project-specs/rewrite-tcloud-deployment.md)与 Relay 成对发布。Muse CD 同时采用 main 原生核心；多端共享账本的读取兼容及故障边界见[读取器合同](project-specs/rewrite-reader-compatibility.md)。累计日志采用按对象校验的文件后备读取；单 WI 查询只加载其自身事实，成功发布不受累计容器的固定读取上限影响。
> 标准/轻量工作、独立验真、人审返工、发布采用、迁移和部分资源退休已有原生证据；[通知实验](project-specs/evidence/rewrite-notifications-completed-20261009.json)已完成，即时、聚合、明确未发送后的恢复和防重均有真实渠道记录。解决通知不抹去未知发送，也不授权重发，见[通知合同](project-specs/rewrite-notifications-contract.md)。
> [运行观察](project-specs/rewrite-watch-contract.md)区分原记录、当前进程观察、逾期和执行权限；未观测到保持 unknown。源码检查、精确安装和真实消费者采用分别记录在[当前路线图](project-specs/project-roadmap.md)，不能由其中一项推导整体验收。
> 新核心生产模式直接读写原生产账本，不使用另一个持久业务数据库；历史草稿沿原编号接入，业务归属决定在原 Agent 的本轮工具结果中返回。接入合同见[唯一生产账本](project-specs/rewrite-legacy-admission.md)。安装采用和用户渠道 UAT 须另行读回。
> 全量 80 项需求、35 组验收、旧数据与消费者迁移及支持矩阵仍未完成。生产替换和正式 WI018 收尾须分别满足对应验收门。

## 日常验证

新核心只使用一个入口：从 PULSE 根运行标准 Python 的
`-I -B Scripts/ci/harness-check.py --report-dir <新的报告目录>`。
它隔离 HOME、状态和临时文件，完整运行新核心测试、lint 与 strict mypy，
总预算 180 秒，输出 JUnit 与逐阶段耗时。自动 Harness CI 和 Muse 精确候选
验证共用该入口，不再重复构建旧 wheel 或运行旧 Harness 全量回归。

旧内核已退役，不再有 CI。范围、预算和实测证据见
[CI 契约](project-specs/rewrite-ci-contract.md)。

## 定位

```
宪法层 (ssot/agent-constitution/)    ← 定义标准
    │
agent-harness（操作系统层）           ← 代码强制 + 机械检测
    │
CC + Relay（基础设施层）              ← 运行引擎 + 通信管道
```

模型是 CPU，CC 是指令集，Relay 是总线，harness 是 OS。

## 旧内核归档

PULSE 单轨运行原生核心 `harness_rewrite`。旧 `harness` 包、其 CLI（`harness`、`harness-scl`、`harness-codex-hook`、`harness-scl-reconciler`）、测试、wheel 构建器与运行安装器已从源码删除；旧账本数据保留为只读归档，由原生核心按历史事件重放读取。

只读归档的位置（不再写入）：

- 旧 SCL 内核状态：`PULSE/data/agent-harness/scl-kernel`（host-policy 的 `historical_kernel_root`），原生核心只读取历史执行。
- 旧 CWA 账本根：`Coevolution/.harness`，受支持写入口拒写。
- 生产账本 `.harness/` 中旧轨记录：原生核心按历史事件重放读取，退役轨道的意图一律以 `track_retired` 拒绝。
- 旧内核规范：[`specs/`](specs/README.md)。

## 开发与调用

Spec 测试锚点以 PULSE 仓库根为基准，使用现有测试文件路径和准确用例名。定位存在只证明用例可找到；测试执行结果和实际 UAT 证据分别记录在对应工作项中。

从受管 PULSE 工作树运行源码检查和测试：

```bash
coevo_python="$HOME/.local/share/coevolution/python-env/bin/python"
"$coevo_python" -I Scripts/ci/check-canonical-python.py --dev
cd agent-harness
"$coevo_python" -B -m pytest -q -p no:cacheprovider -c pytest-rewrite.ini tests_rewrite
"$coevo_python" -m ruff check --no-cache harness_rewrite
"$coevo_python" -m mypy --strict harness_rewrite
```

测试按主机容量规则使用受管临时目录；测试结束须移除自己的临时目录。原生核心随 Relay 不可变发布包构建与激活（`harness_rewrite.release`）；不将开发源码安装进统一环境。架构见 [`project-specs/project-architecture.md`](project-specs/project-architecture.md)。
