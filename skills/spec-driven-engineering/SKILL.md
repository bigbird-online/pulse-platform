---
name: spec-driven-engineering
description: >
  软件工程任务的最小契约驱动方法。Use this skill whenever doing AI coding work:
  analyzing an existing codebase, building a new tool or project, adding a feature,
  fixing a bug, refactoring, designing tests, doing QA/acceptance, preparing release
  or deployment, or syncing stale specs/docs. This skill prevents vague coding by
  defining a Minimum Viable Spec: goal, boundary, context, acceptance evidence, and
  risk; then routes work through Understand, Change, Verify, or Ship & Sync.
---

# spec-driven-engineering

Version: 0.2.4
Last updated: 2026-04-25

## 定位

`spec-driven-engineering` 不是“先写完整 spec 再编码”。这里的 spec 是任务级最小工程契约。

核心公式：

```text
Minimum Viable Spec = 目标 + 边界 + 上下文 + 验收 + 风险
```

目标：让每次 AI coding 改动都有最小约束、相关上下文、合适测试策略和可执行验证，避免模糊需求直接变代码，也避免局部成功冒充整体完成。

## 何时使用

使用本 skill 处理软件工程任务：

- 看懂、接手、修改历史代码库
- 开发新工具或新软件
- 增加功能
- 修复 bug
- 重构
- 补测试或设计测试策略
- QA / UAT / Agent 模拟验收
- 发布、部署、重启、上线后验证
- 代码、测试、文档、spec 不一致时的同步

不适用：纯会议、纯运营汇报、非代码知识整理。

## 工作原则

1. **最小契约先行**：先明确目标、边界、上下文、验收、风险；形式可以很轻，不必总是文档。
2. **风险决定流程重量**：tiny 不走重流程；high-risk 必须有真实验收和回滚路径；大 feature 拆切片时必须同时写 feature risk 与 slice risk。
3. **上下文优先于泛读**：先定位相关文件、接口、数据、测试和工作流，不默认全仓库考古。
4. **测试按风险选择**：TDD 是工具，不是宗教；bug 默认失败测试先行，重构默认 characterization-first。
5. **验证责任不可互替**：开发测试、真实验收、回归基线保护不同判断。
6. **只在契约漂移时同步 spec**：行为、接口、错误语义、数据、部署假设或用户可见行为改变时才更新对应 spec/docs。
7. **AI patch 不等于工程判断**：生成的 diff 必须被契约和证据约束。
8. **能力声明不得越界**：只能声明已验收的能力；小切片能力不得包装成完整可靠性。
9. **sub-agent 交付必须被主身复核**：sub-agent 的测试和报告是证据，不是最终判定。
10. **真实入口不可被近似路径替代**：权限、沙箱、生产入口、外部通道类 bug，CLI/API 探针只能证明一层；最终能力必须由触发该 bug 的真实入口验收。

## 快速流程

```text
1. 判定风险：feature risk + slice risk，均用 tiny / normal / high-risk
2. 写 Minimum Viable Spec：目标、边界、上下文、验收、风险
3. 选择模式：Understand / Change / Verify / Ship & Sync
4. 定位上下文并执行最小改动或验证
5. 运行匹配的测试 / QA / smoke / regression
6. 通过 contract drift gate 决定是否同步 spec/docs
7. 若使用 sub-agent，执行主身 review gate
8. 输出证据、能力边界和残余风险
```

## 风险分级

| 风险 | 例子 | 流程重量 |
|------|------|----------|
| tiny | 文案、局部纯函数、小配置、无外部副作用 | 不建文档；记录最小验收即可 |
| normal | 功能、bug、测试、重构、单服务内部变更 | 最小契约 + 相关上下文 + 匹配测试 |
| high-risk | 调度、发布、数据迁移、安全、权限、跨机器、生产路径、可能静默失败 | 明确 spec + 真实验收 + 回滚路径 |

任一为“是”则至少 normal：影响生产路径、数据/schema、公开 API/CLI/runtime 语义、权限/安全/账单、跨机器部署、可能静默失败。涉及不可逆或生产发布则 high-risk。

大 feature 必须拆出两层风险：

```text
Feature risk：完整功能上线后的总风险。
Slice risk：当前这次实际改动的风险。
```

如果 feature risk 是 high-risk，但当前 slice 只落 contract、测试、纯内部 façade，且不接生产入口，可以把 slice risk 判为 normal；但必须明确哪些高风险部分被排除到后续切片。

生产入口规则：只要改动会被生产入口加载、注册或路由到，例如 app/server entrypoint、bot/channel handler、scheduler、runtime registry、CLI/release path、auth/policy/escalation path，即使本次不部署、不重启，也默认 slice risk = high-risk。只有纯导出、死代码、测试夹具或未被生产入口引用的内部实现，且有证据证明不会被加载时，才可以降级。

权限/沙箱/外部入口 bug 默认 high-risk。验收必须覆盖：

```text
reproduction 或可重复失败证据
-> 自动化回归点
-> 隔离/CLI 探针
-> 生产或类生产入口 UAT
-> 日志/state/产物核验
```

如果缺少真实入口 UAT，只能说“修复在近似路径通过”，不能宣称用户入口已修复。

详细说明见 `references/minimum-viable-spec.md`。

## 四模式路由

| 模式 | 何时使用 | 重点 |
|------|----------|------|
| Understand | 看懂代码、定位上下文、逆向约束 | 足够定位，不默认完整考古 |
| Change | 新建、加功能、修 bug、重构、升级依赖、迁移、性能修复 | 最小 diff + 匹配测试策略 |
| Verify | 测试工程、QA、UAT、Agent 模拟验收、回归基线 | 区分开发测试、真实验收、回归基线 |
| Ship & Sync | 发布、部署、重启、上线后验证、spec/docs drift | contract drift gate + release evidence |

详细 playbook 见 `references/modes.md`。

## Sub-agent Prompt Contract

派 sub-agent 执行工程任务时，prompt 必须要求它输出：

```text
模式
Feature risk / Slice risk
Minimum Viable Spec
本轮明确不做什么
改动或发现
验证证据
Contract Drift
需要主身复核的点
残余风险
```

sub-agent 不应自行扩大生产范围、重启生产服务、修改真实配置或提交 git，除非任务明确授权。

## Main Review Gate

sub-agent 返回后，主身至少复核：

- diff 是否越界或混入无关改动
- 测试是否覆盖真实风险，而不只是 happy path
- spec/docs 是否只同步真实 drift
- 生产日志是否出现新 warning/error
- state/config 是否符合预期且可回滚
- secret/token 是否落盘或出现在记录中
- cleanup/resource leak 是否被覆盖

详细 review/QA 规则见 `references/verification-and-testing.md`。

Ship & Sync 高风险上线默认采用发布梯度：

```text
build/test -> dark deploy -> single canary -> canary UAT -> fallback/preference canary -> observe -> expand
```

跳级必须写明理由和回滚路径。

## Contract Drift Gate

收口时回答：

```text
行为契约变了吗？
公开接口 / CLI / API 变了吗？
错误语义变了吗？
数据 / schema / 配置变了吗？
部署或运行假设变了吗？
用户可见行为变了吗？
```

全部为否：说明无需同步 spec/docs。
任一为是：同步对应 spec/docs/release note，或明确记录暂不同步的理由。

如果实现完全匹配已有 spec/design，只需要同步任务状态、验证证据或 release note；不要为了“同步”而改写仍然正确的设计文档。

## Capability Claim Rule

收口时用最窄真实表述：

```text
已验证：哪些输入、环境、路径、用户可见行为。
未验证：哪些相邻能力、异常路径、生产范围。
不能宣称：容易被误解为更大范围的说法。
```

例如：只验证了 spawn failure fallback，就只能说“spawn 阶段失败会按 preference fallback”，不能说“Codex 不可用会自动回 CC”。

## 输出格式

默认用这个结构收口，按任务大小裁剪：

```markdown
模式：
风险：Feature risk / Slice risk
Minimum Viable Spec：
上下文：
改动 / 发现：
验证：
Contract Drift：
能力边界：
残余风险：
```

## References

- `references/minimum-viable-spec.md`：最小工程契约与风险分级
- `references/modes.md`：Understand / Change / Verify / Ship & Sync 详细 playbook
- `references/verification-and-testing.md`：测试策略、QA、回归基线
- `references/pulse-adapter.md`：PULSE 平台工程适配
- `references/source-assets.md`：旧规则与 next 思考来源
