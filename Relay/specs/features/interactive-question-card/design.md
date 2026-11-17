---
feature_name: "interactive-question-card"
version: "0.2.0"
created: "2026-06-10T18:00:00+0800"
modified: "2026-06-14T19:00:00+0800"
upstream_dependency: ["features/interactive-question-card/requirements.md"]
tags: [design, l2-spec, feishu, ask-user-question, path-a-pty]
changelog:
  - "v0.2.0: 多题状态机修复 — PendingAsk 加 answeredQuestions 字段；claim 时机改为只在 Submit 路径调用；统一所有卡片都加 Submit 按钮（单题不再 digit-submits）；新增文字路径 pendingAsk 短路（数字+label 双匹配）。新增 applyAskAnswer/handleSubmitAsk/tryRouteTextAsAskAnswer 三个核心函数。废弃 v0.1.0 的 A' 文字 prompt 回注方案——实际实现走 Path A（PTY 键盘注入）。修复 2026-06-14 多题卡死 bug。"
  - "v0.1.0: 初版（A' 方案：新文字 prompt 回注；未实现，被 Path A 取代）"
---

# interactive-question-card - 技术设计文档

> **设计目标**：拦截 CC 输出中的 AskUserQuestion tool_use，渲染飞书交互卡片。用户点选项按钮 → `pool.injectInput(digit)` 注入 PTY 键盘 → CC TUI 勾选；用户点 Submit 按钮 → `pool.injectInput("\r")` → CC TUI 提交。**Path A 方案**（非 v0.1.0 设想的 A' 文字 prompt 回注），见 retrospective-2026-06-10.md。
>
> **关联文档**：
> - L2 需求：`features/interactive-question-card/requirements.md`（v0.3.0）
> - L2 任务：`features/interactive-question-card/tasks.md`（v0.2.0）
> - MVS 切片：`features/interactive-question-card/MVS.md`（按 A' 方案写，已过时）
> - Path A 复盘：`features/interactive-question-card/retrospective-2026-06-10.md`
> - 探针决策：`vultr-pulse-space/memo/decision-2026-05-23-askuserquestion-feishu.md`

---

## 架构概览

```
CC PTY interactive 模式（Path A，2026-06-10 落地）
  │
  ├─ JSONL: assistant record (tool_use AskUserQuestion)
  │     ↓
  │   pool 检测 → emit 'response' → handleFeishuPoolResponse
  │     ↓
  │   渲染飞书卡片（选项按钮 + Submit 按钮）+ registerPendingAsk(answeredQuestions=[])
  │     ↓
  │   CC TUI 显示 ←☐Q1☐Q2✔Submit→ 等待键盘输入
  │
  ├─ 用户点选项按钮（card.action.trigger / relay_ask_answer）
  │     ↓ handleAskAnswer → applyAskAnswer(pending, qi, oi)
  │       → injectInput(digit)         // CC TUI 勾选当前 tab 的选项
  │       → pending.answeredQuestions.push(qi)  // 不去重 pending
  │       → StateManager 同步
  │
  ├─ 用户点 Submit 按钮（card.action.trigger / relay_ask_submit）
  │     ↓ handleSubmitAsk
  │       → 检查 answeredQuestions.length === questions.length
  │       → 未答完：提示「还有 N 题未答」
  │       → 答完：injectInput("\r") + claimPendingAsk
  │
  ├─ 用户发文字（pendingAsk 短路）
  │     ↓ tryRouteTextAsAskAnswer
  │       → 匹配数字 / 选项 N / label 子串
  │       → 命中：调 applyAskAnswer（同按钮路径）
  │       → 不命中：发选项列表提示，不进 pool.send
  │
  └─ 超时兜底（5min cleanupExpiredPendingAsks）
        → injectInput(Esc) + ASK_TIMEOUT_RESUME_PROMPT + injectInput("\r")
```

## 数据流

### Flow 1：AskUserQuestion 检测与卡片渲染

```
pool 'response' event (assistant with tool_use)
  → handleFeishuPoolResponse()
    → [新增] detectAskUserQuestionToolUse(msg)
      → 解析 input.questions[]
      → 构建 pendingAsk 记录
      → 渲染飞书卡片
      → sendCardMessage(openId, card)
      → 注册 pendingAsk 到 pendingAsks Map
      → 返回 true（拦截该消息，不处理 text）
```

### Flow 2：过渡文本抑制

```
pool 'response' event (assistant with transition text)
  → handleFeishuPoolResponse()
    → [新增] hasPendingAsk(poolKey) → true
      → 跳过该 assistant 消息（不发送卡片给用户）
      → 不清除 pendingAsk（等用户点选或超时）
```

### Flow 3：用户点选项回调（v0.2.0 状态机）

```
飞书 card.action.trigger (value.relay_ask_answer = "q0:o2")
  → handleFeishuCardAction()
    → handleAskAnswer(ctx, openId, toolUseId, "q0:o2")
      → getPendingAskByToolUseId(toolUseId)  // 非 claim 读
      → 若 pending 不存在：提示「已过期或已提交」，return
      → 解析 qi=0, oi=2
      → applyAskAnswer(ctx, pending, qi=0, oi=2):
          - injectInput(poolKey, "3")          // CC TUI 勾选
          - pending.answeredQuestions.push(0)（去重）
          - StateManager.updateSession 同步
          - **永不 claim，永不自动 submit**
      → 提示用户「✅ 已选: {label}」
```

### Flow 3b：用户点 Submit 回调（v0.2.0 新增）

```
飞书 card.action.trigger (value.relay_ask_submit = toolUseId)
  → handleFeishuCardAction()
    → handleSubmitAsk(ctx, openId, toolUseId)
      → getPendingAskByToolUseId(toolUseId)
      → 若 pending 不存在：提示「已过期或已提交」，return
      → 若 answeredQuestions.length < questions.length:
          - 提示「还有 N 题未答：Q{qi+1}: {question}」
          - return（不 claim、不 inject）
      → injectInput(poolKey, "\r")             // CC TUI 提交
      → claimPendingAsk(toolUseId)             // 原子 get+delete
      → 提示「✅ 已提交」
```

### Flow 3c：用户发文字（v0.2.0 新增，pendingAsk 短路）

```
飞书 text message（非 / 命令）
  → 文字 handler
    → pool.send(poolKey, ...) 前：
      → getPendingAskByPoolKey(poolKey) → pending
      → 若 pending 存在：
          - tryRouteTextAsAskAnswer(ctx, openId, pending, text):
              a. 找第一个 qi ∉ pending.answeredQuestions
              b. 文字匹配：
                 - 纯数字 "3" → oi = 2
                 - "选项 3" / "3." / "3、" → oi = 2
                 - label 子串 contains → oi
              c. 命中 → applyAskAnswer(ctx, pending, qi, oi) → return true
              d. 不命中 → 发提示「请点击卡片按钮，或回复 Q{qi+1} 的选项编号：...」→ return true
          - 若 handled → return（不进 pool.send）
      → 否则照常 pool.send
```

### Flow 4：超时回退（不变）

```
pendingAsk.expiresAt 到达
  → cleanupExpiredPendingAsks() (setInterval 60s)
    → injectInput(Esc) + ASK_TIMEOUT_RESUME_PROMPT + injectInput("\r")
    → 更新飞书卡片为"已超时"禁用态
    → 清除 pendingAsk
```

---

## 组件变更

### 1. `src/pool.ts` — 移除 AskUserQuestion 禁用

**当前**（L109）：
```typescript
const DISALLOWED_TOOLS = ['AskUserQuestion'];
```

**变更**：
```typescript
const DISALLOWED_TOOLS: string[] = [];
```

**影响**：所有 agent / 所有 channel 的 CC spawn 均不再禁用 AskUserQuestion。CC 会调用该工具，tool_use block 出现在 stream-json 输出中。TG 通道当前已忽略所有 tool_use block（仅取 text），不受影响。

### 2. `src/channels/feishu.ts` — AskUserQuestion 拦截

**变更点 A：handleFeishuPoolResponse 扩展**

在现有 text 提取逻辑**之前**（L1601 `if (msg.type !== 'assistant') return;` 之后），新增 AskUserQuestion 检测：

```typescript
// --- AskUserQuestion interception (A' approach) ---
if (msg.type === 'assistant') {
  const msgObj = msg.message as { content?: Array<Record<string, unknown>> } | undefined;
  if (Array.isArray(msgObj?.content)) {
    const askBlock = msgObj.content.find(
      b => b.type === 'tool_use' && b.name === 'AskUserQuestion'
    ) as { id?: string; input?: { questions?: unknown[] } } | undefined;
    if (askBlock) {
      await handleAskUserQuestion(poolKey, askBlock, poolKeyToOpenId, sendCardMessage);
      return; // 不处理该消息的 text 部分
    }
  }
}
// --- End AskUserQuestion interception ---
```

**变更点 B：过渡文本抑制**

在 text 提取后、发送卡片前（L1614 `if (!cleanedText) return;` 之后），新增：

```typescript
// 抑制 AskUserQuestion 自答后的过渡文本
if (hasPendingAsk(poolKey) && isLikelyTransitionText(cleanedText)) {
  return;
}
```

**isLikelyTransitionText 判断**：CC 自答后的过渡文本通常短小且含 "asked the question"、"waiting for your answer"、"select" 等模式。或更简单地：在 pendingAsk 存在时，抑制紧接着的第一条 assistant 文本。用计数器控制（pendingAsk.suppressNext = true → 首次命中后置 false）。

### 3. `src/channels/feishu.ts` — 卡片渲染

**handleAskUserQuestion 函数**（新增）：

```typescript
async function handleAskUserQuestion(
  poolKey: string,
  toolUseBlock: { id?: string; input?: { questions?: unknown[] } },
  poolKeyToOpenId: Map<string, string>,
  sendCardMessage: (openId: string, card: Record<string, unknown>) => void | Promise<void>,
): Promise<void> {
  const toolUseId = toolUseBlock.id ?? '';
  const questions = (toolUseBlock.input?.questions ?? []) as AskUserQuestionQuestion[];
  const openId = poolKeyToOpenId.get(poolKey);
  if (!openId || questions.length === 0) return;

  const card = buildAskUserQuestionCard(toolUseId, questions);
  await sendCardMessage(openId, card);

  registerPendingAsk(poolKey, {
    toolUseId,
    questions,
    openId,
    cardMessageId: undefined, // 飞书 API 返回后填充
    suppressNext: true,
    expiresAt: Date.now() + PENDING_ASK_TIMEOUT_MS,
  });
}
```

**buildAskUserQuestionCard**：构建飞书 Schema 2.0 交互卡片。每个 question 渲染为一个 section，每个 option 渲染为 `tag: button` + `behaviors: [{type: 'callback'}]`，callback value 格式：

```json
{
  "relay_ask_answer": "q0:o2",
  "relay_ask_id": "call_xxx"
}
```

多选题使用飞书 `multi_select` 组件 + 提交按钮，callback value 含逗号分隔的选项索引。

### 4. `src/channels/feishu.ts` — 卡片回调处理

**handleFeishuCardAction 扩展**（L812）：

```typescript
// 现有 relay_copy / relay_switch 之后新增：
const askAnswer = typeof value?.relay_ask_answer === 'string' ? value.relay_ask_answer : '';
const askId = typeof value?.relay_ask_id === 'string' ? value.relay_ask_id : '';
if (askAnswer && askId && openId) {
  await handleAskAnswer(ctx, openId, askId, askAnswer);
  return;
}
```

**handleAskAnswer 函数**（新增）：

```typescript
async function handleAskAnswer(
  ctx: MessageHandlerContext,
  openId: string,
  toolUseId: string,
  answerEncoded: string, // "q0:o2" or "q0:o1,o3"
): Promise<void> {
  const pending = claimPendingAsk(toolUseId);
  if (!pending) return; // 已过期或已回答

  // 解析答案
  const answers = parseAskAnswer(answerEncoded, pending.questions);

  // 格式化 A' prompt
  const prompt = formatAskAnswerPrompt(answers);

  // 通过 pool.send 发送新文字 prompt
  const poolKey = resolvePoolKeyByOpenId(ctx, openId);
  if (poolKey) {
    await ctx.pool.send(poolKey, { type: 'human', content: prompt });
  }

  // 更新飞书卡片为"已选择"禁用态
  await updateAskCardResolved(openId, pending, answers);
}
```

**formatAskAnswerPrompt**：

```typescript
function formatAskAnswerPrompt(
  answers: Array<{ question: string; selected: string[] }>
): string {
  const lines = ['[AskUserQuestion 用户已选择]'];
  for (const a of answers) {
    lines.push(`题目: ${a.question}`);
    lines.push(`选择: ${a.selected.join(', ')}`);
  }
  lines.push('请根据以上选择继续执行。');
  return lines.join('\n');
}
```

### 5. Pending Ask 状态管理

**内存状态**（feishu.ts 模块内）：

```typescript
interface PendingAsk {
  poolKey: string;
  toolUseId: string;
  questions: AskUserQuestionQuestion[];
  openId: string;
  cardMessageId?: string;
  suppressNext: boolean; // 是否抑制下一条 assistant 文本
  expiresAt: number;
}

const pendingAsks = new Map<string, PendingAsk>(); // key: toolUseId
```

**持久化**（StateManager）：`PersistedSession` 新增可选字段：

```typescript
interface PersistedSession {
  // ... 现有字段 ...
  pendingAsk?: {
    toolUseId: string;
    questions: unknown[];   // JSON 序列化的 questions 快照
    expiresAt: number;
  };
}
```

Relay 重启后恢复 pendingAsks Map。已过期项执行超时回退（更新卡片为"已超时"）。

### 6. Idle Timer 保护

**问题**：CC AskUserQuestion turn 结束后 pool 进入 idle。30 分钟 idle timer 开始计时。若用户接近 30 分钟才点选，进程可能已被回收。

**方案**：在 `pool.ts` 的 `cleanupIdleProcesses` 中增加判断 — 若 StateManager 中该 session 有 `pendingAsk`，跳过 idle 回收。

```typescript
function cleanupIdleProcesses(): void {
  const now = Date.now();
  for (const [tenantId, proc] of processes) {
    if (proc.state === 'idle' && now - proc.lastActivity > idleTimeoutMs) {
      // 有 pending 交互请求时跳过 idle 回收
      const session = stateManager?.getSession(tenantId);
      if (session?.pendingAsk && session.pendingAsk.expiresAt > now) continue;
      emitter.emit('session-end', tenantId, { type: 'idle-reclaimed' });
      killProcess(tenantId).catch(() => {});
    }
  }
}
```

### 7. 过渡文本过滤增强

**CC_NOISE_PATTERNS** 新增模式：

```typescript
// CC AskUserQuestion 自答后的过渡文本（A' 方案下不透传）
/I[''']ve asked (you )?(the question|about)/i,
/waiting for (your|a) (answer|response|selection)/i,
```

**注意**：过渡文本抑制主要靠 `hasPendingAsk + suppressNext` 机制（变更点 B）。噪声模式仅作为兜底，防止状态丢失时泄漏。

---

## 类型定义

```typescript
// AskUserQuestion tool_use input.questions[] 单项
interface AskUserQuestionQuestion {
  question: string;
  header?: string;
  multiSelect?: boolean;
  options: Array<{
    label: string;
    description?: string;
  }>;
}

// 解析后的用户回答
interface AskAnswer {
  question: string;
  selected: string[]; // option labels
}
```

## PendingAsk 状态机（v0.2.0 新增）

```typescript
interface PendingAsk {
  poolKey: string;
  agentId: string;
  toolUseId: string;
  questions: AskUserQuestionQuestion[];
  openId: string;
  suppressNext: boolean;
  expiresAt: number;
  answeredQuestions: number[];   // 🆕 v0.2.0：已答的 qi 列表（去重 push）
}
```

### claim 时机表（v0.2.0 核心）

| 触发 | claimPendingAsk | 注入 PTY | 状态变化 |
|------|:---:|:---:|---------|
| 点选项按钮（任何 qi） | ❌ | injectInput(digit) | `answeredQuestions.push(qi)` 去重；pendingAsk 仍在 |
| 点 Submit + 未答完 | ❌ | ❌ | 提示「还有 N 题未答」；不变化 |
| 点 Submit + 答完 | ✅ | injectInput("\r") | pendingAsk 删除 |
| 文字匹配命中选项 | ❌ | injectInput(digit) | 同「点选项按钮」 |
| 文字不匹配 | ❌ | ❌ | 提示选项列表；不变化 |
| 超时（5min） | ✅（cleanupExpiredPendingAsks） | injectInput(Esc) + resume + "\r" | pendingAsk 删除 |
| 进程 crash / `/stop` / `/new` | ✅（killProcess 路径） | — | pendingAsk 删除 |

### 核心函数（v0.2.0 新增）

```typescript
// 处理一次「点选项」事件（按钮或文字匹配共用）
async function applyAskAnswer(ctx, pending: PendingAsk, qi: number, oi: number): Promise<void>

// 处理 Submit 按钮 callback
async function handleSubmitAsk(ctx, openId, toolUseId): Promise<void>

// 文字路径匹配 + 路由
async function tryRouteTextAsAskAnswer(ctx, openId, pending: PendingAsk, text: string): Promise<boolean>

// 非 claim 读访问器（新增；claimPendingAsk 仍是原子 get+delete）
function getPendingAskByToolUseId(toolUseId: string): PendingAsk | undefined
```

---

## 飞书卡片结构（v0.2.0 统一加 Submit）

所有 AskUserQuestion 卡片（单题 + 多题）Schema 2.0，末尾**始终**追加 Submit 按钮：

```json
{
  "schema": "2.0",
  "header": { "title": { "tag": "plain_text", "content": "❓ CC 提问" }, "template": "blue" },
  "body": {
    "elements": [
      { "tag": "markdown", "content": "**Q1: {question}**" },
      {
        "tag": "button",
        "text": { "tag": "plain_text", "content": "{option.label}" },
        "type": "primary",
        "behaviors": [{ "type": "callback", "value": { "relay_ask_answer": "q0:o0", "relay_ask_id": "{toolUseId}" } }]
      },
      // ... more options for Q1
      // 多题时重复 Q2 ... 块
      { "tag": "hr" },
      {
        "tag": "button",
        "text": { "tag": "plain_text", "content": "✅ Submit" },
        "type": "primary",
        "behaviors": [{ "type": "callback", "value": { "relay_ask_submit": "{toolUseId}" } }]
      }
    ]
  }
}
```

---

## 命名空间（v0.2.0 更新）

| 回调 key | 用途 | 现有？ |
|----------|------|--------|
| `relay_copy` | 复制内容按钮 | ✅ |
| `relay_switch` | Agent 切换按钮 | ✅ |
| `relay_menu` / `relay_menu_arg` | 飞书命令菜单按钮（feishu-command-menu feature） | ✅ |
| `relay_ask_answer` | AskUserQuestion 选项（值 `q{qi}:o{oi}` 或 `q{qi}:o{oi},o{oj}` 多选） | ✅ |
| `relay_ask_id` | AskUserQuestion tool_use ID | ✅ |
| `relay_ask_submit` | AskUserQuestion Submit 按钮（v0.2.0 🆕；值 = toolUseId） | 🆕 |

`relay_ask_*` / `relay_menu*` 前缀不会与现有命名冲突。

---

## 错误处理（v0.2.0 更新）

| 场景 | 处理 |
|------|------|
| CC 进程在 pending 期间 crash | pool crash restart → pendingAsk 持久化恢复（含 answeredQuestions）→ 用户点选/Submit 时若 PTY bridge 已死则 injectInput 抛错 → catch 后走 A' fallback（pool.send 新 prompt）或提示「会话已重置」 |
| 飞书卡片更新 token 过期 | best-effort 忽略更新失败，pending 状态仍正常清理 |
| pool.injectInput 时进程已 idle-recycled | injectInput 抛错 → handleAskAnswer catch 后 fallback 到 `pool.send({type:'human', content: formatAskAnswerPrompt(answers)})`（A' 文字 prompt 兜底） |
| **用户点同一题多次（改主意）**（v0.2.0） | applyAskAnswer 重新 injectInput(digit)，answeredQuestions 去重 push（length 不增长）。CC TUI 行为依赖实测。 |
| **用户点 Submit 时未答完**（v0.2.0） | handleSubmitAsk 提示「还有 N 题未答：Q{qi+1}: {question}」；不 claim、不 inject |
| **用户点 Submit 时已答完**（v0.2.0） | injectInput("\r") + claimPendingAsk；pendingAsk 状态机终结 |
| **用户点选项时 pending 已被 claim**（v0.2.0） | getPendingAskByToolUseId 返回 null → 提示「已过期或已提交」 |
| **用户在 pending 期间发文字**（v0.2.0） | tryRouteTextAsAskAnswer 尝试匹配；命中 → applyAskAnswer；不命中 → 选项列表提示，**不进 pool.send** |
| **用户在 pending 期间发 / 命令**（v0.2.0） | `/` 命令路径优先于 pendingAsk 短路；`/stop` `/new` 等正常执行 |
| pendingAsk 超时（5min 未提交） | cleanupExpiredPendingAsks 触发：injectInput(Esc) + ASK_TIMEOUT_RESUME_PROMPT + injectInput("\r") → CC 退出 AskUserQuestion TUI、开始新 turn |
