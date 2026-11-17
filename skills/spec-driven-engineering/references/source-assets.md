# 来源资产与保留判断

本文件用于维护 `spec-driven-engineering` 时回看旧规则和 next 重构思考。不要把旧规则整套复制进 skill。

## 原始规则集

路径：`/Users/owner/Coevolution/Doc/rules/spec-driven-developer/`

保留意图：

- `110-project-startup.md`：项目级约束先于实现。
- `111-project-spec-extraction.md`：从历史代码逆向提炼约束。
- `120-feature-planning.md`：需求、设计、任务、验收必须分离。
- `130-tdd-implementation.md`：实现必须可测试、可追溯；但 TDD 不是所有场景的硬规则。
- `140-uat-acceptance.md` / `145-automated-user-simulation.md`：真实工作流验收，不用 mock/API 调通冒充完成。
- `150-spec-sync.md`：实现变化后处理 spec drift。
- `160-next-feature-selection.md`：下一轮工作受验收反馈影响。
- `170-regression-test-management.md`：真实验收后的核心路径沉淀为回归基线。
- `180-bug-fix-workflow.md`：缺陷修复要复现、根因、修复、回归、范围检查。
- `190-194`：发布准备、文档、检查、部署、上线后验证是独立能力。

## next 重构思考

路径：`/Users/owner/Coevolution/Doc/rules/spec-driven-developer-next/`

文件：

- `000-preserved-strengths.md`
- `001-generation-constraints.md`
- `domain-overview.md`
- `rule-plan.md`

保留判断：

1. 不复刻旧目录表面结构，要按真实使用形态组织。
2. `115` 是横切状态 / handoff 层，不是线性生命周期步骤。
3. `190-194` 是低频高后果 release 扩展，不应伪装成高频主链。
4. 核心矛盾不是测试少，而是开发测试、真实验收、回归基线被互相替代。
5. `140` 和 `145` 属于同一真实验收域，只是 human-led / agent-simulated / hybrid 的不同执行模式。
6. 发布是独立能力，不是开发完成后的自然尾声。

## 两次挑战后的 v0.2 调整

传统软件工程挑战结论：

- spec 必须变成工程约束，而非文档纪律。
- 测试金字塔和验证责任要拆开。
- TDD 不能宗教化：bug 默认失败测试先行，重构 characterization-first，探索允许 spike。
- QA 与测试工程要分工。

AI coding 研究挑战结论：

- 不把 spec-driven 宣称为 AI coding 主流最优实践。
- 使用 Minimum Viable Spec，避免重文档。
- 按风险分级决定流程重量。
- 强调上下文定位、最小 diff、验证和 review 成本。
- spec sync 是 contract drift gate，不是每次都做的行政动作。
