# 四模式 Playbook

## Understand：理解 / 定位 / 逆向约束

适用：

- 分析历史代码
- 接手旧项目
- 为一次修改定位相关上下文
- 逆向提炼项目或模块约束

流程：

```text
确认问题/目标
-> 定位入口和相关文件
-> 跑现有 build/test 或最小样例
-> 提炼足够约束
-> 标出不确定项
```

输出：

- 相关上下文清单
- 入口点 / 调用链 / 数据流
- 现有测试和运行方式
- 推断契约与证据来源
- 不确定项

注意：大多数修改不需要完整代码地图，只需要“足够定位”。大规模 onboarding 才需要完整 repo map。

只读理解任务的额外要求：

- 区分事实、推断和未验证假设。
- 不顺手改代码，除非用户明确要求进入 Change。
- 输出“下一步最小验证”而不是把理解报告包装成已解决。

## Change：新建 / 加功能 / 修 bug / 重构

适用：

- 新软件 / 新工具
- 新功能
- bug 修复
- 重构
- spike / exploration
- code review 后修正
- dependency upgrade
- performance / reliability 修复
- data / migration 变更

通用流程：

```text
Minimum Viable Spec
-> 选择子路径
-> 定位上下文
-> 选择测试策略
-> 最小 diff
-> 验证
```

子路径默认策略：

| 子路径 | 默认策略 |
|--------|----------|
| 新软件 / 新工具 | 最小项目契约 + 第一条 vertical slice |
| 新功能 | acceptance-first，再拆 unit / integration |
| bug 修复 | reproduction-first，默认 failing test 先行 |
| 重构 | behavior contract + characterization-first |
| spike / exploration | 允许快速试验，但产物不可直接交付；必须回填契约与验证 |
| dependency upgrade | changelog / breaking change / compatibility / rollback |
| performance / reliability | baseline metric -> change -> compare metric |
| data / migration | migration contract + backup/rollback + compatibility check |

场景模板：

- bugfix：复现路径 -> 失败测试或可重复手工证据 -> 最小修复 -> 回归。
- permission/sandbox/entrypoint bugfix：触发 bug 的真实入口 -> 最小可重复失败证据 -> 自动化回归点 -> 近似探针 -> 真实入口 UAT -> 日志/state/产物核验。
- historical analysis：入口/调用链/数据流 -> 证据来源 -> 不确定项 -> 可执行下一步。
- QA/release：验收矩阵 -> 环境/数据 -> 正常/异常路径 -> accepted/rejected/blocked。
- refactor：行为契约 -> characterization tests -> 小步结构调整 -> 行为不变证明。
- test strategy：风险到测试层级映射；避免只为了覆盖率写无断言或弱断言测试。

bugfix 证据阶梯：

```text
1. 复现：最好是 failing test；无法自动化时保留可重复手工证据。
2. 修复：最小 diff，避免把旁路重构混进 bugfix。
3. 回归：补能防止同类问题回来的自动化测试。
4. 近似探针：CLI/API/fake runtime 可以证明局部机制有效，但必须注明覆盖边界。
5. 真实入口：用户、bot、cron、scheduler、release path 等原始触发入口必须通过。
6. 旁证：日志、state、artifact、cleanup、错误通道没有新异常。
```

权限、沙箱、认证、路由、生产入口 bug 不能停在第 4 步；否则只能判为 blocked 或 partial。

退出门禁：

- 契约中的边界未被破坏。
- 验收证据与风险等级匹配。
- 若行为、接口、错误语义、数据、配置或部署假设改变，进入 Ship & Sync。

## Verify：测试工程 / QA / 回归 / 真实验收

适用：

- 补测试
- 设计测试策略
- QA 验收
- UAT / Agent 模拟验收
- 建立 regression baseline
- 判断某项工作是否真的完成

流程：

```text
读取 MVS / requirements
-> 选择验证责任层
-> 选择测试或 QA 方法
-> 执行并记录证据
-> accepted / rejected / blocked
```

QA 输出必须是：

```text
accepted / rejected / blocked
```

blocked 必须写解除条件。

## Ship & Sync：发布 / 部署 / spec drift

适用：

- 发布准备
- 部署 / 重启 / 上线后验证
- spec/docs drift
- 运行假设改变
- contract drift 检查

Contract drift gate：

```text
行为契约变了吗？
公开接口 / CLI / API 变了吗？
错误语义变了吗？
数据 / schema / 配置变了吗？
部署或运行假设变了吗？
用户可见行为变了吗？
```

全部为否：记录无需同步。
任一为是：同步对应 spec/docs/release note，或明确记录暂不同步理由。

发布证据按风险裁剪：

- tiny：通常无需发布记录。
- normal：build/test 证据 + 简短变更说明。
- high-risk：发布边界 + 构建产物 + 部署/重启证据 + smoke test + rollback。

高风险发布默认采用梯度：

```text
build/test
-> dark deploy
-> single canary
-> canary UAT
-> fallback/preference canary
-> observe
-> expand
```

每一级必须记录：

- 配置/代码实际改变了什么
- 是否重启、reload 或迁移
- 验收命令、用户可见证据、日志/state 证据
- 回滚命令或回滚配置
- 是否允许进入下一级

不要把 canary 结果扩大成全量上线结论。
