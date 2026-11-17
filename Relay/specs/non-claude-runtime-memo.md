---
title: "CC Runtime 下非 Claude 模型运行备忘"
version: "0.0.2"
created: "2026-04-23T17:34:30+0800"
modified: "2026-04-23T22:22:27+0800"
tags: [memo, design, relay, claude-code, anthropic-compatible, glm]
---

# CC Runtime 下非 Claude 模型运行备忘

> 目的：总结 2026-04-23 这轮 `Relay + TG + GLM` 故障的真实根因，并给出在 Claude Code Runtime 下接入“非 Claude 模型”的工程方法。这里的“运行”包含两类：
> - **无头模式**：`claude -p` / stream-json
> - **交互模式**：`claude --bare`，由 Relay 通过 PTY 桥接成可编程接口

---

## 1. 核心结论

这次问题不是“GLM 不可用”，也不是“Relay 单纯把请求打坏了”。更准确的结论是：

1. **同一 provider、同一 token、同一模型，不同 CC 运行模式可能落在不同兼容性/限流池里。**
2. `claude -p` 对应的 **headless / sdk-cli** 路径，在 `Z.ai / GLM` 上会命中明显更严格的动态限流。
3. `claude --bare` 对应的 **interactive / cli** 路径，在相同 GLM 环境下可以工作。
4. 因此，“非 Claude 模型是否兼容 CC”不能按 provider 整体判断，必须按 **运行模式** 判断。

一句话概括：

> 非 Claude 模型接入 CC runtime 时，兼容单位不是“模型”，而是“模型 × provider × runtime mode”。

---

## 2. 这次故障的实证结果

### 2.1 同样是 GLM，原生 Anthropic 兼容请求可用

对 `https://api.z.ai/api/anthropic/v1/messages` 的原生 Anthropic 兼容请求，在同 token、同模型 `glm-5.1` 下可返回 `200`。

这说明：

- GLM 服务整体没有挂
- token 没失效
- `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 注入不是主因

### 2.2 `claude -p` 路径会命中 429 / 1302

实测中，同样请求只要带上 `sdk-cli` 特征，就会触发 `429 code=1302`。  
这与 Relay/TG 无关，脱离 Relay 直接跑 `claude -p` 也能复现。

这说明：

- 问题在 `CC headless` 这条请求形态
- 不是 session 脏、不是 TG 特有、不是 Relay 独占 bug

### 2.3 `claude --bare` 交互模式可通

实测 `claude --session-id/--resume ... --bare --permission-mode bypassPermissions`：

- session 元数据表现为 `kind: "interactive"` / `entrypoint: "cli"`
- 同一 GLM 环境下可以成功返回 assistant 回复

这说明：

- `sdk-cli` 与 `cli` 在 provider 侧被当成不同类别
- 同一模型在 CC 内部并非“永远不通”，而是 **print 模式不稳，interactive 模式可用**

---

## 3. 这次实际修了什么

这轮不是只改文案，而是把 Relay 对 CC runtime 的抽象从“只有 `-p` 一条路”升级成了“两条路”。

### 3.1 增加了 CC 模式抽象

在 [src/types.ts](/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Relay/src/types.ts) 中新增：

- `CCMode = 'print' | 'interactive'`

含义：

- `print`：传统 `claude -p --input-format stream-json --output-format stream-json`
- `interactive`：`claude --bare`，但由 Relay 通过 PTY 桥接驱动

### 3.2 为 interactive 模式增加 PTY bridge

新增：

- [scripts/claude_pty_bridge.py](/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Relay/scripts/claude_pty_bridge.py)

作用：

- 在 PTY 中启动真实交互式 `claude`
- 将 PTY stdin/stdout 转成普通 pipe
- 使 Relay 可以像管理 headless 子进程一样管理 interactive `claude`

### 3.3 Pool 侧支持按 provider 自动切换模式

在 [src/pool.ts](/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Relay/src/pool.ts) 中：

- 支持 `ccMode` 显式配置
- 支持 `RELAY_CC_MODE`
- 若检测到 `ANTHROPIC_BASE_URL` 指向 `api.z.ai/api/anthropic`，默认走 `interactive`

### 3.4 Interactive 模式不再直接读终端输出做协议

interactive 模式下，Relay 不把 PTY 输出当协议流，而是：

1. 通过 PTY 把 prompt 写进 `claude --bare`
2. 轮询对应 session 的 `~/.claude/projects/...jsonl`
3. 从 jsonl 中抽取 assistant 记录，再转成 Relay 内部 response event

原因很简单：

- `claude --bare` 的终端输出是 UI 流，不是稳定协议
- session jsonl 才是稳定的语义源

### 3.5 修掉了 “No response requested.” 假回复

interactive 模式刚落地时还有一个边角 bug：

- 恢复旧 session 时，Relay 会先读到历史 jsonl 里一条旧 assistant 记录
- 这条记录可能是 `No response requested.`
- 于是 TG 先收到一条空洞回复，随后才收到这次真正的回答

现在的修法是：

- 只接受 **本次 prompt 真正写入 PTY 之后** 产生的 assistant 记录
- 旧记录直接丢弃

关键位置：

- [src/pool.ts](/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Relay/src/pool.ts:104)
- [src/pool.ts](/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Relay/src/pool.ts:212)
- [src/pool.ts](/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Relay/src/pool.ts:249)
- [src/pool.ts](/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Relay/src/pool.ts:790)

### 3.6 新定位：interactive bridge 的“提交语义”不能等价于一次性 `stdin.write(prompt + "\r")`

这是本轮后半段真正新增的根因。

最开始我们已经确认：

- `claude -p` 在 GLM/Z.ai 上不稳
- `claude --bare` 可以工作

但切到 interactive 后，Emi / Pulse 仍然会出现：

- 长消息超时
- session 看起来活着，但 `jsonl` 里没有新的 `user turn`
- TG 一直显示 typing，直到 watchdog 超时

进一步实测后，关键发现是：

1. **直接在终端里跑** `claude --bare --resume <session>`，长消息可正常提交并落成 `user turn`
2. **通过 PTY bridge 跑**，如果把整段长消息和回车一次性写成：

```text
stdin.write(prompt + "\r")
```

则经常只会把文本灌进 TUI 输入框，**不会真正提交**
3. 同样是 PTY bridge，如果改成：
   - 先写文本
   - 再延迟单独发送一次 `Enter`

则老 session 的长消息可以稳定落成新的 `user turn`

这说明问题不在：

- GLM 本身
- `claude --bare` 本身
- session 旧不旧本身

而在：

> **交互式 TUI 的“粘贴一整段文本”和“按 Enter 提交”不是同一个语义事件。**

对 TUI 来说，`prompt + "\r"` 一次性写入，并不总等价于“用户完成输入并按下回车”。  
长消息尤其如此。

#### 这次的工程修正

Relay 现在改成：

1. 写入正文：`stdin.write(prompt)`
2. 等一个很短的提交延迟（当前默认 `300ms`）
3. 再单独写入提交键：`stdin.write("\r")`

同时把“接单成功”的定义收紧为：

- **只有 `jsonl` 中出现新的 `type: "user"` 记录，才算消息真正发出去了**

因此下面这些事件都**不能**再被误当成“消息已送达”：

- PTY write 成功
- 输入框里出现了回显
- synthetic assistant 噪音，比如 `No response requested.`

#### 这条发现的意义

这条根因和前面的 provider/mode 结论不冲突，而是进一步补全了 interactive 模式的工程约束：

- `print` 模式的边界是协议流
- `interactive` 模式的边界不是 PTY 写入成功，而是 **TUI 提交成功并落成 session turn**

换句话说：

> 对 interactive runtime，Relay 要编排的是“终端交互事件”，不是“字符串写入”。

这是后续所有非 Claude provider 走 interactive 模式时都必须记住的实现约束。

---

## 4. 两种 CC 运行模式的本质区别

| 维度 | `print` 无头模式 | `interactive` 交互模式 |
|---|---|---|
| 典型命令 | `claude -p ...` | `claude --bare ...` |
| entrypoint | `sdk-cli` | `cli` |
| Relay 协议源 | stdout NDJSON | session jsonl |
| 是否需要 PTY | 否 | 是 |
| 自动化难度 | 低 | 中 |
| 对 provider Anthropic 兼容性的要求 | 高 | 中 |
| 在 Z.ai / GLM 上的当前表现 | 易 429 | 可工作 |

关键认知：

- `print` 更像“官方无头 API 通道”
- `interactive` 更像“把真人交互 CLI 程序程序化”

两者都属于“在 CC runtime 内运行”，但 provider 侧不一定把它们当一回事。

---

## 5. 如何在 CC runtime 下使用非 Claude 模型

### 5.1 先区分三层，不要混在一起

工程上应明确区分：

1. **模型层**
   - 例如 `glm-5.1`
2. **provider 兼容层**
   - 例如 `Z.ai Anthropic-compatible API`
3. **CC runtime 运行层**
   - `print`
   - `interactive`

不要问：

> “GLM 兼容不兼容 Claude Code？”

应该问：

> “GLM 通过 Z.ai Anthropic-compatible provider，在 `claude -p` 下是否兼容？在 `claude --bare` 下是否兼容？”

### 5.2 优先做 capability probe，而不是硬编码想当然

接入一个新的非 Claude provider 时，建议最少验证四件事：

1. 原生 Anthropic 兼容请求是否正常
2. `claude -p` 是否稳定
3. `claude --bare` 是否稳定
4. session resume 是否会重放旧记录或触发额外副作用

只有四项都过，才可宣称“provider 兼容 CC runtime”。

### 5.3 Relay 层应该保留双模式，而不是强押单模式

推荐抽象：

```ts
type CCMode = 'print' | 'interactive'
```

并让 mode 的选择由以下三层共同决定：

1. 显式配置
2. provider profile
3. 运行时探测结果

### 5.4 “interactive” 仍然可以是服务端无头运行

这点很重要。

虽然 `interactive` 本质是 CLI 交互模式，但在 Relay 侧：

- 它仍然是 daemon 管理的后台子进程
- 仍然可以通过 PTY bridge 自动驱动
- 仍然可以服务 TG/cron/bot

所以它不是“人工模式”，而是：

> **交互式 CC 内核 + 无头的 Relay 编排**

---

## 6. 对后续实现的建议

### 6.1 引入 provider profile

建议后续把 provider 判断从“看 base URL”升级为显式 profile，例如：

```ts
type ProviderProfile =
  | 'native-anthropic'
  | 'anthropic-compatible-print-ok'
  | 'anthropic-compatible-interactive-only'
  | 'anthropic-compatible-unknown'
```

这样 runtime 选择不再是临时 if/else，而是可配置策略。

### 6.2 batch / scheduler 和 TG 交互要分开建模

即便都叫“CC runtime”，也不代表适合用同一 mode。

建议：

- **TG 交互**：优先可恢复、对话连续性强的模式
- **batch / scheduler**：优先可观测、结果边界清晰的模式

如果某 provider 只对 interactive 友好，就要单独考虑：

- batch 是否也走 PTY + jsonl
- 或者 batch 改成 direct API / codex / 其他 runtime

### 6.3 interactive 模式要把“会话回放污染”当一等问题

因为 interactive 模式依赖 session jsonl，所以必须显式处理：

- 旧 assistant 记录重放
- 旧 turn 与新 turn 边界
- prompt 写入时刻与 assistant 记录时刻的对应关系

否则就会再次出现类似：

- `No response requested.`
- 历史工具输出被误当本轮回复

### 6.5 interactive 模式必须显式建模“提交事件”

这次新增的关键教训是：

- `stdin.write(prompt)` 只是“文本进入终端”
- 不等于“Claude 已收到这条消息”
- `stdin.write(prompt + "\r")` 也不总等于真正提交，尤其在长消息和 `--resume` 老 session 下

因此 interactive 模式至少要显式建模三步：

1. **input staged**
   - 文本已写入 PTY
2. **submit fired**
   - Enter 已被单独发送
3. **turn accepted**
   - `jsonl` 已出现对应 `user turn`

只有第 3 步成立，TG 才应开始 typing、Pool 才应认为消息已发送。

如果没有这层建模，就会再次出现：

- 消息看似发了，其实没提交
- Relay 以为 busy，用户看到无限 typing
- 重试发生在错误边界上，只会制造假自愈

### 6.4 不要把 provider 问题误修成 retry 风暴

这次一个明确教训是：

- provider 的兼容性问题，不能只靠“多重重试”掩盖
- 外层 Relay retry + 内层 CC retry 叠加，只会把配额打爆

因此后续原则应是：

> 先确认 **模式是否对**，再讨论 **重试是否需要**

---

## 7. 当前建议的默认策略

基于本次实证，当前 Relay 的建议默认值是：

1. **原生 Claude / Anthropic**：优先 `print`
2. **Anthropic-compatible 但 headless 不稳定的 provider（如本次 GLM/Z.ai）**：优先 `interactive`
3. **未知 provider**：先做 capability probe，再决定

也就是说，未来默认策略不应是：

> “所有 provider 都先上 `claude -p`”

而应是：

> “每个 provider 先判定它在 CC runtime 下适合哪一种 mode”

---

## 8. 本 memo 的落地意义

这份文档不是单纯记录“GLM 出过一次 429”。

它真正沉淀的是一个更普适的实现原则：

> **在 Claude Code runtime 下接入非 Claude 模型时，Relay 必须把 runtime mode 当成一等兼容维度。**

没有这个抽象，后面每换一个 provider，都会重复踩：

- “原生 API 明明通，Relay 却不通”
- “CLI 交互能用，`claude -p` 不能用”
- “重试越修越坏”

有了这个抽象，后续就能把问题稳定地归到：

- provider 兼容问题
- mode 选择问题
- relay 编排问题

而不是继续混成一团。
