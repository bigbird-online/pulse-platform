# 验证与测试

## 验证责任模型

| 层级 | 保护什么 | 常见证据 |
|------|----------|----------|
| 开发测试 | 局部正确、快速反馈 | unit / component / integration / contract |
| 真实验收 | 用户或平台工作流真实有效 | UAT、Agent 模拟真实工作流、真实探针 |
| 回归基线 | 未来版本不退化 | regression suite、characterization tests、smoke baseline |

规则：

- 单元 / 集成 / E2E 测试不能替代 QA 验收。
- QA 验收不能替代回归基线。
- 发布冒烟测试不能证明完整功能正确。
- mock / API 调通不能冒充真实工作流证据，除非明确限定范围。
- CLI 探针不能冒充用户入口证据。尤其是权限、沙箱、auth、路由、bot、cron、scheduler、release path 类 bug，必须跑触发问题的真实入口或明确标记 partial。

## 测试策略模型

| 测试类型 | 适用场景 |
|----------|----------|
| unit | 纯逻辑、边界条件、快速反馈 |
| component | 模块内部协作 |
| integration | 跨模块 / DB / 文件系统 / 外部接口 |
| contract | API / CLI / runtime 语义 |
| e2e | 少量关键用户旅程 |
| smoke | 发布后核心路径存活 |
| characterization | 遗留代码和重构前行为冻结 |
| property / fuzz | 解析器、协议、输入空间复杂场景 |

## TDD 使用规则

- bug 修复：默认先复现，再写失败测试；无法自动化时保留可重复手工复现证据。
- 新功能：优先 acceptance-first，再按风险拆 unit / integration / contract。
- 重构：默认 characterization-first，先冻结行为再改结构。
- spike / exploration：允许不 test-first，但 spike 不可直接交付；交付前必须回填契约和验证。
- UI / 外部系统重依赖任务：可用 agent-simulated QA 或手工验收作为真实验收，但仍需尽量沉淀可自动化回归点。

## QA 判定

QA 关注真实可用性，不是重复开发测试。

QA 记录至少包含：

- acceptance criteria
- 环境与数据
- 真实路径和异常路径
- 证据
- accepted / rejected / blocked
- defect severity 或解除 blocked 的条件

## 真实入口 UAT

当 bug 来自特定入口，验收必须覆盖该入口：

| bug 来源 | 真实入口证据 |
|----------|--------------|
| Telegram / Feishu bot | 用户消息、bot 回复、通道日志、目标产物或 state |
| cron / scheduler | 真实或临时轻量 job、scheduler 日志、artifact/delivery |
| CLI / release path | 实际 CLI 命令、构建产物、部署入口日志 |
| permission / sandbox | 跨边界读写探针 + 原始入口触发同样能力 |
| auth / token / external API | 脱敏配置摘要 + 真实 API 响应或可观测失败 |

近似路径有价值，但要写清楚：

```text
CLI probe passed: proves Codex --add-dir works.
TG UAT passed: proves Telegram-spawned Codex receives that permission.
```

只有两者都成立，才能声明 TG bot 权限 bug 已修复。

## Main Review Gate

sub-agent 的输出必须经过主身复核，尤其是 high-risk slice。

复核清单：

- diff：是否只包含 MVS 范围内的文件和行为。
- tests：是否覆盖主要风险、异常路径、回归基线；mock 是否被误当真实验收。
- runtime evidence：生产或类生产日志是否出现新 warning/error。
- state/config：是否符合预期，是否存在持久化残留，是否可回滚。
- cleanup：临时文件、队列、子进程、listener、lock 是否释放。
- security：token/auth/key/secret 是否进入源码、测试、spec、memo、日志摘要。
- claim：最终说法是否只覆盖已验证能力。

复核可以发现 sub-agent 未覆盖的工程风险；这不是 skill 失败，而是高风险工程的必要责任分层。

## 验证环境约束

验证命令如果依赖特殊环境，必须记录原因，而不只记录命令结果。

常见例子：

- `HOME=/tmp`：隔离本机用户配置、token、cache 或真实凭据副作用。
- 临时 `CRON_JOBS_PATH`：避免碰生产 cron 配置。
- fake CLI / fake token：验证 runtime 语义，不能冒充真实 provider UAT。
- 不重启生产服务：当前 slice 未接生产入口，或重启需要单独对齐。

记录格式建议：

```text
验证：HOME=/tmp npm test ...，用于隔离本机配置；这是自动化回归，不是生产 UAT。
```

## 能力声明边界

验收结论必须拆成：

- 已验证：具体路径、环境、输入、用户可见行为。
- 未验证：相邻能力和异常路径。
- 不可宣称：比证据更大的能力。

示例：

```text
已验证：single canary 的文本/resume/图片路径。
未验证：所有 agent workspace。
不可宣称：全平台 Codex runtime 已稳定。
```
