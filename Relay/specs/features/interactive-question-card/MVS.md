# MVS — AskUserQuestion Feishu Card (A' 方案)

> Owner: Pulse 主身 | Engineer 切片 | Created: 2026-05-23
> Modified: 2026-06-10 — 按 A' 方案改写（tool_result 回注已证实不可行）
> Status: spec updated, not yet implemented

## 目标

CC 在 `-p`/stream-json 模式下调用内置工具 `AskUserQuestion` 时，CC 会同步自答 `is_error: true` tool_result 并结束 turn，输出无意义的过渡文本。本功能拦截该 tool_use block，渲染为**飞书交互卡片**（可点选），用户点选后把选择以**新文字 prompt** 发回 CC（A' 方案），让 CC 在新一轮 turn 中基于用户选择继续。

**为什么是 A' 而非 tool_result 回注**：CC 2.1.104 + 2.1.143 探针均证实，CC `-p` 模式下 AskUserQuestion 被调用后**同步自答** tool_result 并 `end_turn`，无外部注入窗口。stdin 回注的 tool_result 被当成新一轮 prompt，CC 已丢失原 tool_use 上下文。详见 `vultr-pulse-space/memo/decision-2026-05-23-askuserquestion-feishu.md`。

## 边界（本切片做什么 / 不做什么）

做：
- 拦截 stream-json 输出中 `type==='tool_use' && name==='AskUserQuestion'` 的 content block（飞书通道）。
- 把 `input.questions[]`（question/header/multiSelect/options[label+description]）渲染成飞书交互卡片，每个 option 一个可点按钮；`multiSelect` 题型支持多选 + 一个「提交」按钮。
- 用户点选 → 通过飞书 `card.action.trigger` 回调收集选择 → 组装为结构化文字 prompt → `pool.send` 发回 CC（新 turn，非 tool_result）。
- pending 状态持久化到 StateManager，承载 `pendingToolUseId` / 卡片 messageId / 题目快照 / 截止时间。
- 挂起期延长 BUSY_TIMEOUT：检测到 pending AskUserQuestion 时，该 tenant 的 busy timer 延长到 30 分钟窗口（可配置）。
- 超时回退：30 分钟窗口内无完整点选，清理 pending 状态，更新卡片为"已超时"禁用态。CC 进程不受影响（turn 已正常结束）。
- 过滤 CC 自答后的过渡文本（如 "I've asked the question..."），不透传给飞书用户。

不做：
- 不做 TG（下一切片）。
- 不回注 tool_result 续接同一 turn（已证实不可行）。
- 不做其他 tool_use 的拦截（Bash/Write 等仍自动执行，见 CONSTRAINT-002）。
- 不改 CC 的 permission-mode（工具审批透传属于 Phase 2 / Channels API）。
- 不做卡片的富样式美化，先功能正确。

## 时序（A' 方案）

```
1. CC 输出 tool_use: AskUserQuestion
   ↓ (stream-json)
2. CC 同步自答 is_error tool_result + assistant 过渡文本 + result
   ↓ (CC turn 正常结束，进程 idle)
3. Relay 拦截 step 1 的 tool_use block → 渲染飞书卡片 → 发送给用户
4. Relay 过滤 step 2 的过渡文本（不发给用户）
5. 用户在飞书点选 → card.action.trigger 回调
6. Relay 组装文字 prompt → pool.send（新 human 消息）
   ↓
7. CC 开始新 turn，基于文字 prompt 中的选择继续工作
8. CC 后续回复正常透传给飞书用户
```

**关键差异**：原 A 方案试图在 step 2 和 step 3 之间注入 tool_result，但 step 2 是同步的，无注入窗口。A' 方案放弃回注，改为 step 6 发起新 turn。

## A' prompt 格式

```text
[AskUserQuestion 用户已选择]
题目: {question}
选择: {selected_option_labels}
请根据以上选择继续执行。
```

多题目时按序拼接。CC 在新 turn 中通过文字理解用户选择，不需要特殊 schema。

## 相关路径 / 上下文

- **tool_use 拦截**：`src/channels/feishu.ts:1601-1612`，当前 `handleFeishuPoolResponse` 只取 text block、跳过 tool_use。新增 AskUserQuestion 拦截分支在此函数。tool_use block 的 `message.content` 数组中 `type==='tool_use'` 的元素在 text 之前或之后均可出现（CC 同一 assistant message 可含多个 content block）。
- **飞书卡片回调**：已有 `handleFeishuCardAction`（`src/channels/feishu.ts:725-752`）处理 `card.action.trigger`，`/switch` 的 agent picker（`withAgentPickerUi` / `feishuCardElements`）是现成先例。复用其 action.value 约定。
- **卡片发送**：`deliverFeishuCard` / `client.im.message.create`（`feishu.ts:1782`）。卡片需可更新（点选后把卡片改成「已选 X」禁用态），用 `client.im.message.patch` 或 update card。
- **pending state**：`PersistedSession`（`src/types.ts:380-395`）新增字段；先例是 feishu pairing 的 `pendingPairings` map（`feishu.ts:116-123`）+ `claimPendingPairing`。
- **busy timer**：`armBusyTimer` / `resetBusyTimer` / `busyTimeoutMs`（`src/pool.ts:798-834`）。pool 暴露 `setPendingInteractive(tenantId, expiresAt)` / `clearPendingInteractive(tenantId)` 接口，由 feishu channel 调用。
- **AskUserQuestion 移除 disallow**：`src/pool.ts:109` `DISALLOWED_TOOLS = ['AskUserQuestion']` — 移除该条目。注意：`--disallowed-tools` 是全局的（所有 agent / 所有 channel），放开后需确认 TG 通道也有对应的拦截逻辑或同样放开。
- **过渡文本过滤**：`src/channels/feishu.ts` `CC_NOISE_PATTERNS`（`stripCCNoise`）新增模式匹配 CC 自答后的过渡文本（如 "I've asked the question"、"[Request interrupted]"）。

## Slice risk / Feature risk 初判

- **中风险**：A' 方案的"文字 prompt"让 CC 通过自然语言理解用户选择，而非结构化 tool_result。CC 可能不完美理解选择（尤其是多题目场景），但比 `[Request interrupted]` 好得多。
- **中风险**：Relay 重启丢失内存 pending state → 卡片已发出但回调无人接。需 StateManager 持久化 + 重启恢复（或重启后给已 pending 的卡片回注「会话已重启，请重新提问」并清状态）。
- **低风险**：飞书卡片渲染/多选 UI。
- **低风险**：busy timer 延长。与原 A 方案不同，A' 下 CC turn 已正常结束（idle），busy timer 延长仅在"等用户点选"期间生效。即使 clear 路径漏掉，busy timer 到期后也只是正常发 `busy-progress` 通知，不会卡死进程。

## 验收证据（Engineer 必须给）

1. AskUserQuestion tool_use 拦截单测：识别 AskUserQuestion block、卡片元素生成、card action → 文字 prompt 组装、超时回退清理各路径。
2. `npm run build` + 全量 `npm test` 通过，给出 files/tests 数。
3. busy timer 回归：证明非 pending 场景的 10min busy timeout 行为不变。
4. **真实飞书入口 UAT**（不是 CLI 探针冒充）：在飞书让 Agent 触发 AskUserQuestion → 收到可点卡片 → 点选 → Agent 收到选择并在后续回复中体现选择 → 飞书不再出现 `[Request interrupted]`。给出 journal 证据（card action、pool.send 新 prompt、后续 assistant）。
5. 超时路径 UAT 或自动化：pending 超过窗口后卡片更新为"已超时"、pending 状态被清理、进程未被杀。
6. 回归：非 AskUserQuestion 的 tool_use block 仍被忽略，行为不变。

## Contract Drift 检查

- `pool.send` 入参不变（仍是 string content 的 human 消息），A' 方案不改变 pool 接口。
- `DISALLOWED_TOOLS` 移除 `AskUserQuestion` 后，CC 会开始调用该工具。需确认所有通道（TG、飞书、scheduler、commands）对 AskUserQuestion tool_use block 的处理要么有拦截逻辑，要么能安全忽略（当前已忽略所有 tool_use block，仅取 text）。
- PersistedSession 新增字段需向后兼容旧 state 文件（缺字段按未挂起处理）。
- 飞书 card.action.trigger 现有 `/switch` 回调与新 AskUserQuestion 回调的 action.value 命名空间不能冲突。
