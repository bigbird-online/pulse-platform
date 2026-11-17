# Minimum Viable Spec

Minimum Viable Spec（MVS）是任务级最小工程契约，不等于完整文档。

## 五字段

| 字段 | 要回答的问题 |
|------|--------------|
| 目标 | 这次要改变什么？ |
| 边界 | 不能破坏什么？哪些范围不做？ |
| 上下文 | 相关代码、接口、数据、工作流、测试在哪里？ |
| 验收 | 用什么证据判断完成？ |
| 风险 | 是否涉及生产、数据、权限、安全、发布、跨机器、静默失败？ |

风险必须分两层：

- Feature risk：完整功能或最终上线形态的风险。
- Slice risk：当前这次实际改动的风险。

两者可以不同。比如一个生产 runtime 新功能整体是 high-risk，但当前只新增类型、contract 和测试，且不接生产入口，则当前切片可以是 normal-risk。此时 MVS 必须说明高风险部分被排除在哪里。

生产入口规则：

- 改 app/server entrypoint、bot/channel handler、scheduler、runtime registry、CLI/release path、auth/policy/escalation path，默认 slice risk = high-risk。
- “没有部署/没有重启”不能自动降低风险；它只说明 release 尚未发生。
- 只有纯导出、测试、死代码、或未被生产入口引用的内部实现，且能说明不会被加载时，slice risk 才可降级。

示例：

```text
目标：为 scheduler 增加 dry-run 模式。
边界：不得注册 cron，不得投递 Telegram，不改变真实 cron 语义。
上下文：scheduler.ts、relay-jobs.ts、scheduler.test.ts。
验收：dry-run 测试覆盖；真实 runJob 语义不变。
风险：Feature risk normal；Slice risk normal；需要更新 scheduler design spec。
```

生产入口示例：

```text
目标：在 startRelay() 注册新的 runtime adapter。
边界：默认 runtime=cc 不变；不重启生产 Relay。
验收：入口回归测试、默认路径 regression、真实轻量 UAT。
风险：Feature risk high；Slice risk high。原因：代码已接入生产入口，未部署只代表 release 尚未发生。
```

## 风险分级

| 风险 | 使用场景 | 验证要求 |
|------|----------|----------|
| tiny | 文案、局部纯函数、小配置、无外部副作用 | 相关单测 / typecheck / 手工检查即可 |
| normal | 功能、bug、测试、重构、单服务内部变更 | MVS + 相关上下文 + 匹配测试 |
| high-risk | 调度、发布、数据迁移、安全、权限、跨机器、生产路径、可能静默失败 | 明确 spec + 真实验收 + 回滚路径 |

升高风险的问题：

- 会影响生产路径吗？
- 会改数据或 schema 吗？
- 会改变公开 API / CLI / runtime 语义吗？
- 会影响权限、安全、账单、跨机器部署吗？
- 失败是否会静默？

## MVS 如何驱动工程动作

| MVS 元素 | 驱动动作 |
|----------|----------|
| requirement | acceptance criteria / QA 场景 |
| design decision | contract test / integration test / architecture note |
| task | implementation diff / unit test / commit scope |
| bug spec | reproduction / failing test / regression baseline |
| behavior contract | characterization test / refactor boundary |
| risk | verification weight / release gate / rollback path |

如果一个 spec 没有驱动边界、测试、验收、发布或同步动作，它只是文档，不是工程契约。

## 轻重裁剪

- tiny：MVS 可以只在最终回答里一行说明，不必建文件。
- normal：MVS 应在计划、issue、任务说明、测试名或短 memo 中可见。
- high-risk：MVS 应落成可继承记录，并包含验收证据和回滚路径。
- feature risk 为 high-risk 时，即使 slice risk 只是 normal，也必须显式列出未进入本切片的高风险部分和后续验收门槛。
