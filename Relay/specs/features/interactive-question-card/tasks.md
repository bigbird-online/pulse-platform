---
feature_name: "interactive-question-card"
version: "0.2.0"
created: "2026-06-10T18:00:00+0800"
modified: "2026-06-15T00:00:00+0800"
upstream_dependency: ["features/interactive-question-card/design.md"]
tags: [tasks, l2-spec, implementation]
changelog:
  - "v0.2.0: 多题状态机修复（T-2.0~T-2.7）。T-1.x 标 ✅ 已完成（Path A PTY 键盘注入方案，2026-06-10 落地）。新增 T-2.x：PendingAsk 加 answeredQuestions、统一 Submit 按钮、applyAskAnswer 重构、handleSubmitAsk、tryRouteTextAsAskAnswer 文字路径短路。修复 2026-06-14 运营官多题卡死 bug。"
  - "v0.1.0: 初版 T-1.1~T-1.10 任务拆分（A' 方案）"
---

# interactive-question-card - 任务拆分

> **关联文档**：
> - L2 需求：`features/interactive-question-card/requirements.md`（v0.3.0）
> - L2 设计：`features/interactive-question-card/design.md`（v0.2.0）
> - Path A 复盘：`features/interactive-question-card/retrospective-2026-06-10.md`
>
> **开发规则**：遵循 `Doc/rules/spec-driven-developer/130-tdd-implementation.md`——每个任务先写测试（Red），再实现（Green），再重构（Refactor）。

---

## Phase 1：AskUserQuestion Path A（已完成 2026-06-10）

| 任务 ID | 任务名称 | 状态 |
|---------|---------|:----:|
| T-1.1 | 移除 AskUserQuestion 全局禁用（`DISALLOWED_TOOLS=[]`） | ✅ |
| T-1.2 | PendingAsk 类型 + 内存状态 Map | ✅ |
| T-1.3 | AskUserQuestion tool_use 拦截（handleFeishuPoolResponse） | ✅ |
| T-1.4 | 飞书卡片渲染（buildAskUserQuestionCard） | ✅ |
| T-1.5 | 卡片回调处理（handleAskAnswer + Path A injectInput） | ✅ |
| T-1.6 | 过渡文本抑制（suppressNext） | ✅ |
| T-1.7 | Idle Timer 保护（pending-interactive 模块） | ✅ |
| T-1.8 | 超时回退（cleanupExpiredPendingAsks） | ✅ |
| T-1.9 | PersistedSession 持久化 + 重启恢复 | ✅ |
| T-1.10 | 端到端集成测试 + 飞书 UAT | ✅ |

---

## Phase 2：多题状态机修复（v0.2.0，2026-06-14）

> **背景**：2026-06-14 运营官多题 AskUserQuestion 卡死。根因：`claimPendingAsk` 在第一次点选就清状态，多题第二题点选收到「已过期」错误；文字路径无 pendingAsk 短路导致 typed answer 入队永不消费。详见 requirements.md FR-P0-007/008、design.md §PendingAsk 状态机。

| 任务 ID | 任务名称 | 预估工时 | 依赖任务 | 状态 |
|---------|---------|---------|---------|:----:|
| T-2.0 | Spec 升级（requirements→v0.3.0 / design→v0.2.0 / tasks→v0.2.0） | 0.5h | — | ✅ |
| T-2.1 | PendingAsk 加 `answeredQuestions` 字段 + schema 持久化 + restore + `getPendingAskByToolUseId` 非 claim 读 | 1h | T-2.0 | ✅ |
| T-2.2 | 抽 `applyAskAnswer` 核心（统一单/多题：digit select，永不 claim/submit） | 1.5h | T-2.1 | ✅ |
| T-2.3 | `handleSubmitAsk` + 卡片统一加 Submit 按钮 + `relay_ask_submit` callback 分支 | 1.5h | T-2.2 | ✅ |
| T-2.4 | `tryRouteTextAsAskAnswer` + 文字 handler pendingAsk 短路 | 1.5h | T-2.2 | ✅ |
| T-2.5 | 多题单测 M1~M10（Red→Green）+ 现有单题测试断言更新 | 1.5h | T-2.1~T-2.4 | ✅ |
| T-2.6 | 全量回归 `npm run build && npm test`（基线 806 + 新增 ~10） | 0.5h | T-2.5 | ✅ |
| T-2.7 | 飞书 UAT：单题新流程 + 多题按钮/Submit/文字路径 + 解锁运营官卡死会话 | 1h | T-2.6 | ✅ |

---

## T-2.x 任务详情

### T-2.0：Spec 升级

**文件**：`specs/features/interactive-question-card/{requirements,design,tasks}.md`

**实现内容**：
- requirements.md：v0.2.0 → v0.3.0；加 FR-P0-007（统一 Submit + 状态机）+ FR-P0-008（文字路径识别）+ AC-P0-008/009
- design.md：v0.1.0 → v0.2.0；架构图改为 Path A；新增 §PendingAsk 状态机 + claim 时机表 + Flow 3a/3b/3c；错误处理表重写；命名空间加 `relay_ask_submit`
- tasks.md：v0.1.0 → v0.2.0；T-1.x 标 ✅；新增 T-2.0~T-2.7

**验证标准**：
- [ ] 三个 spec 文件 frontmatter version 已 bump
- [ ] changelog 仅保留最近三个版本
- [ ] 引用 retrospective-2026-06-10.md 标注 Path A

### T-2.1：PendingAsk 加 answeredQuestions + 持久化

**文件**：`src/channels/feishu.ts`、`src/types.ts`

**实现内容**：
- `PendingAsk` interface 加 `answeredQuestions: number[]`（feishu.ts:349-357）
- `PersistedSession.pendingAsk` 加可选 `answeredQuestions?: number[]`（types.ts:471-477）
- `registerPendingAsk` 初始化 `answeredQuestions: []`（feishu.ts:376-397）
- `registerPendingAsk` 持久化时写 `answeredQuestions` 到 StateManager
- `restorePendingAsksFromState` 恢复时读 `answeredQuestions`，缺省为 `[]`（feishu.ts:1243-1280）
- 新增 `getPendingAskByToolUseId(toolUseId)`：非 claim 读访问器（feishu.ts 现有 `pendingAsksByToolUseId` Map）

**验证标准（TDD）**：
- [ ] **Unit Test**：`registerPendingAsk` 后 `pending.answeredQuestions === []`
- [ ] **Unit Test**：`StateManager.updateSession` 调用参数含 `answeredQuestions: []`
- [ ] **Unit Test**：`restorePendingAsksFromState` 后 `pending.answeredQuestions` 从持久化数据恢复
- [ ] **Unit Test**：旧持久化数据无 `answeredQuestions` 字段时，restore 后默认 `[]`（向后兼容）

### T-2.2：抽 applyAskAnswer 核心

**文件**：`src/channels/feishu.ts`

**实现内容**：
- 新增 `applyAskAnswer(ctx, pending, qi, oi): Promise<void>`（导出供测试）
  - injectInput(digit)（CC TUI 勾选当前 tab）
  - pending.answeredQuestions 去重 push qi
  - StateManager 同步
  - **永不 claim，永不自动 submit**
- 重构 `handleAskAnswer`（feishu.ts:1126-1202）：
  - 第一行从 `claimPendingAsk` 改为 `getPendingAskByToolUseId`（非 claim 读）
  - 解析 answerEncoded → 对每个 oi 调 applyAskAnswer
  - 末尾提示用户「✅ 已选: ...」
  - A' fallback（pool.send 文字 prompt）保留：仅当 injectInput 抛错时使用

**验证标准（TDD）**：
- [ ] **Unit Test M1**：多题点 q0 选项 → injectInput 调 1 次，`answeredQuestions=[0]`，`hasPendingAsk===true`
- [ ] **Unit Test M2**：多题再点 q1 选项 → injectInput 累计调 2 次（digit、digit），`answeredQuestions=[0,1]`，**pendingAsk 仍在**
- [ ] **Unit Test M9**：多题 q0 已答再点 q0 → injectInput 调 1 次，`answeredQuestions` 仍 `[0]`（去重）

### T-2.3：handleSubmitAsk + Submit 按钮 + callback 分支

**文件**：`src/channels/feishu.ts`

**实现内容**：
- `buildAskUserQuestionCard`（feishu.ts:448-478）：末尾**始终**追加 Submit 按钮
  - `{ tag:'button', text:'✅ Submit', type:'primary', behaviors:[{type:'callback', value:{relay_ask_submit: toolUseId}}] }`
- `handleFeishuCardAction`（feishu.ts:1062-1123）：在 `relay_ask_answer` 分支前/旁加 `relay_ask_submit` 分支
  - 调 `handleSubmitAsk(ctx, openId, toolUseId)`
- 新增 `handleSubmitAsk(ctx, openId, toolUseId): Promise<void>`
  - `getPendingAskByToolUseId(toolUseId)` → null 时提示「已过期或已提交」
  - `answeredQuestions.length < questions.length` → 提示「还有 N 题未答：Q{qi+1}: {question}」
  - 否则 `injectInput(poolKey, "\r")` + `claimPendingAsk(toolUseId)` + 提示「✅ 已提交」

**验证标准（TDD）**：
- [ ] **Unit Test M3**：多题 Submit 未答完 → 提示，不 claim、不 inject；答完 → injectInput `"\r"` + claim
- [ ] **Unit Test M4**：单题点选项 → injectInput digit，pendingAsk 仍在；点 Submit → injectInput `"\r"` + claim
- [ ] **Unit Test M10**：buildAskUserQuestionCard 输出末尾含 `{relay_ask_submit}` button（含单题场景）

### T-2.4：tryRouteTextAsAskAnswer + 文字 handler 短路

**文件**：`src/channels/feishu.ts`

**实现内容**：
- 新增 `tryRouteTextAsAskAnswer(ctx, openId, pending, text): Promise<boolean>`
  - 找第一个 qi ∉ pending.answeredQuestions
  - 匹配规则（按优先级）：
    1. 纯数字 `^\d+$/` → oi = num - 1（若 ≤ options.length）
    2. `^选项?\s*(\d+)[.、]?$` → oi = num - 1
    3. `option.label.includes(text)` 或 `text.includes(option.label)` → oi
  - 命中 → `applyAskAnswer(ctx, pending, qi, oi)` → return true
  - 不命中 → 发提示「请点击卡片按钮，或回复 Q{qi+1} 的选项编号：1 xxx / 2 yyy」→ return true
- 修改文字消息 handler（feishu.ts:1422-1473）：
  - 在 `text.startsWith('/')` 检查后、`pool.send` 前插入
  - `const pending = getPendingAskByPoolKey(poolKey)`
  - `if (pending && await tryRouteTextAsAskAnswer(...)) return;`

**验证标准（TDD）**：
- [ ] **Unit Test M5**：label 子串「底座+可替换」→ 命中 → applyAskAnswer 调用 → injectInput digit
- [ ] **Unit Test M6**：纯数字「3」→ 命中当前未答题的 o2 → digit "3"
- [ ] **Unit Test M7**：不匹配「瞎打的」→ 提示含「Q1」「1 xxx」，**不调 pool.send**

### T-2.5：多题集成测试 M8 + 现有测试更新

**文件**：`tests/feishu-ask-multi.test.ts`（新增）、`tests/feishu-channel.test.ts`（更新）

**实现内容**：
- `feishu-ask-multi.test.ts`：完整覆盖 M1~M10（在 T-2.1~T-2.4 中逐步 Red→Green）
- `feishu-channel.test.ts:1005-1035` 单题测试更新：
  - 原期待「无 Submit 按钮，2 个按钮」→ 新增 Submit 按钮后变成 3 个按钮（2 选项 + 1 Submit）
- M8 持久化测试：
  - mock stateManager.updateSession 调用参数含 answeredQuestions
  - restore 后 pending.answeredQuestions 恢复
- M9 改主意测试：q0 已答再点 q0 → answeredQuestions 去重 length 不增长

**验证标准**：
- [ ] **Unit Test M8**：StateManager 持久化 + restore 保留 answeredQuestions
- [ ] 现有 `feishu-channel.test.ts` 单题测试断言更新后通过
- [ ] 所有 M1~M10 在 `feishu-ask-multi.test.ts` 中通过

### T-2.6：全量回归

**命令**：`cd Relay && npm run build && npm test`

**验证标准**：
- [ ] tsc --noEmit 零 error
- [ ] 全部测试通过（基线 806 + 新增 ~10 = ~816）

### T-2.7：飞书 UAT + 解锁卡死会话

**前置**：先 `/stop` 运营官卡死 session（CC PID 90009，已挂 4h+）。

**手动验证**：
- [ ] 单题新流程：触发单题 AskUserQuestion → 卡片含 Submit → 点选项 → ✅ 已选 → 点 Submit → CC 继续
- [ ] 单题 digit 不再自动 submit：点选项后 pendingAsk 仍在
- [ ] 多题按钮路径：触发 2 题 → 点 q0 → 点 q1 → 点 Submit → CC 继续
- [ ] 多题 Submit 未答完：触发 2 题 → 点 q0 → 点 Submit → 提示「还有 1 题未答」
- [ ] 多题文字路径：触发 2 题 → 发文字「3」→ ✅ 已选 → 发文字「1」→ ✅ 已选 → 点 Submit → CC 继续
- [ ] 文字不匹配：发文字「瞎打的」→ 收到选项列表提示，CC 不被打扰
- [ ] 超时兜底：触发 2 题 → 不点 Submit → 5min 后 Esc + resume prompt

---

## 任务依赖

```
T-2.0 (spec)
  ↓
T-2.1 (PendingAsk schema)
  ↓
T-2.2 (applyAskAnswer)
  ↓
T-2.3 (Submit) ──┐
T-2.4 (文字路径) ─┤
                  ↓
              T-2.5 (集成测试 M8/M9 + 现有更新)
                  ↓
              T-2.6 (全量回归)
                  ↓
              T-2.7 (UAT + 解锁)
```

建议实施顺序：T-2.0 → T-2.1 → T-2.2 → T-2.3 → T-2.4 → T-2.5 → T-2.6 → T-2.7

---

## 不在本期范围（v0.2.0）

- TG 通道 AskUserQuestion 卡片（Phase 2b）
- CC 工具权限审批透传（Phase 2，需 Channels API）
- Codex 审批（Phase 2b）
- 卡片富样式美化
- 多题 label 子串误匹配的二次确认（UAT 验证后决定是否加）
