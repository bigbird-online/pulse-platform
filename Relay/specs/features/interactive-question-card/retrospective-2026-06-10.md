# AskUserQuestion 实践复盘与路径重评估

> Date: 2026-06-10
> Status: **路径 A 已落地，飞书 UAT 通过**（2026-06-10）
> Context: T-1.1 ~ T-1.10 全部实现完毕，build + test 通过，但飞书 UAT 时发现根本性架构问题

---

## 1. 做了什么

### 已完成的代码变更（T-1.1 ~ T-1.10）

| 任务 | 文件 | 变更 | 状态 |
|------|------|------|------|
| T-1.1 移除全局禁用 | `pool.ts` | `DISALLOWED_TOOLS` 改为 `[]`；`--disallowed-tools` 条件化（仅非空时传入） | ✅ |
| T-1.2 类型与状态 | `feishu.ts` | `PendingAsk` 接口、`pendingAsksByToolUseId/ByPoolKey` Map、register/has/claim/remove | ✅ |
| T-1.3 tool_use 拦截 | `feishu.ts` | `handleFeishuPoolResponse` 新增 AskUserQuestion tool_use 检测分支 | ✅ |
| T-1.4 飞书卡片 | `feishu.ts` | `buildAskUserQuestionCard()` Schema 2.0 交互卡片 | ✅ |
| T-1.5 卡片回调 | `feishu.ts` | `handleAskAnswer()` — claim → 解析 → `pool.send` 新文字 prompt | ✅ |
| T-1.6 文本抑制 | `feishu.ts` | `suppressNext` 机制 + 噪声正则补充 | ✅ |
| T-1.7 Idle 保护 | `pool.ts` + `pending-interactive.ts` + `index.ts` | `isPendingInteractive` callback + 共享模块避免循环依赖 | ✅ |
| T-1.8 超时回退 | `feishu.ts` | `cleanupExpiredPendingAsks()` + 60s interval（`.unref()`） | ✅ |
| T-1.9 持久化 | `types.ts` + `feishu.ts` | `PersistedSession.pendingAsk` + StateManager 同步 + 启动恢复 | ✅ |
| T-1.10 构建测试 | 全局 | `npm run build` ✅，`npm test` ✅（775/776 pass，1 个无关竞态） | ✅ |

### 实现中发现并修复的问题

1. **`require()` 在 ESM 不可用** → 改为静态 import `pending-interactive.js`
2. **`claimPendingAsk`/`removePendingAsk` 未同步 `clearPendingInteractive`** → 已修复
3. **`cleanupExpiredPendingAsks` 未同步 StateManager** → 已修复
4. **`--disallowed-tools` 空数组仍传 flag** → 改为条件化
5. **agent config `allowedTools` 覆盖默认列表缺少 `AskUserQuestion`** → 已更新 `relay.json`

---

## 2. 为什么 UAT 失败

### 根因链

```
用户发消息 → 总管 CC 进程收到
→ CC (GLM model) 说 "我没有 AskUserQuestion 工具"
→ 实际原因：--bare 禁用了 AskUserQuestion
```

### 探针事实（2026-06-10 实测）

| 测试 | 命令 | 结果 |
|------|------|------|
| `-p` + 默认模型 + `--allowed-tools AskUserQuestion` | `echo "..." \| claude -p --allowed-tools Read,AskUserQuestion` | ✅ CC 调用了 AskUserQuestion，自动回答选了 A |
| `-p` + `glm-5.1` + `--allowed-tools AskUserQuestion` | 同上 + `--model glm-5.1` | ✅ CC 调用了 AskUserQuestion |
| `-p` + `glm-5.1` + `--bare` + `--allowed-tools AskUserQuestion` | 同上 + `--bare` | ❌ GLM 说"我没有这个工具" |
| `-p` + `glm-5.1` + `--bare` + `--tools AskUserQuestion,...` | 用 `--tools` 替代 `--allowed-tools` | ❌ 同样不可用 |
| `-p` + `glm-5.1` 无 `--bare` | 去掉 `--bare` | ✅ GLM 调用了 AskUserQuestion |

**结论：`--bare` 标志是阻断点。它禁用了 CC 的交互式工具（包括 AskUserQuestion），且无法通过 `--allowed-tools` 或 `--tools` 重新启用。**

### `--bare` 为什么存在

```typescript
// pool.ts buildInteractiveArgs() 原始注释：
// bug-20260510-001 follow-up: --bare disables CLAUDE.md auto-discovery
// (per `claude --bare --help`). The official channel for re-enabling it
// is `--add-dir (CLAUDE.md dirs)`.
```

`--bare` 作用：跳过 hooks、LSP、plugin sync、auto-memory、background prefetches、keychain reads、CLAUDE.md auto-discovery。设 `CLAUDE_CODE_SIMPLE=1`。

### 去掉 `--bare` 后的第二个问题

即使去掉 `--bare` 让 CC 能用 AskUserQuestion，还有更根本的架构问题：

**PTY 交互模式下，pool 不传递 tool_use 块。**

```
CC (PTY 模式) 写 JSONL → pollInteractiveSession 读新行
→ handleInteractiveRecord 解析
→ emitInteractiveAssistant 只提取 text 块，丢弃 tool_use
→ feishu.ts 的 AskUserQuestion 拦截代码永远看不到 tool_use
```

`emitInteractiveAssistant`（pool.ts L1674-1688）：
```typescript
// 只提取 text，忽略 tool_use
const text = blocks
  .filter((block) => block?.type === 'text' && typeof block.text === 'string')
  .map((block) => block.text ?? '')
  .join('');
if (!text) return;  // tool_use only 的记录直接被跳过
```

而且 `handleInteractiveRecord` 只在 `stopReason === 'end_turn'` 时调用 `emitInteractiveAssistant`。AskUserQuestion 的 stop_reason 是 `tool_use`（模型要调用工具），不是 `end_turn`。

---

## 3. 三条可选路径

### 路径 A：PTY 键盘模拟（用户提议）

**原理**：CC 在 PTY 模式下调用 AskUserQuestion → 显示 TUI 选择 UI → 等待键盘输入。Relay 检测 → 飞书卡片 → 用户点选 → Relay 写键盘输入回 PTY。

```
CC 调用 AskUserQuestion
  → JSONL 出现 assistant record (stop_reason=tool_use, tool_use block)
  → pool 检测到，发 ask-user-question 事件
  → feishu.ts 收到事件，渲染卡片
  → 用户点选飞书卡片按钮
  → feishu.ts 调 pool.injectInput(tenantId, "0\n")
  → CC 收到键盘输入，选中第一个选项
  → CC 继续执行
```

**需要的改动**：
1. 去掉 `--bare`（让 CC 能用 AskUserQuestion）
2. `handleInteractiveRecord` 新增 AskUserQuestion tool_use 检测（stop_reason=`tool_use` 时）
3. 新增 `emitter.emit('ask-user-question', tenantId, questions)` 事件
4. 新增 `pool.injectInput(tenantId, text)` 方法（写 PTY stdin）
5. feishu.ts 监听事件 → 发卡片 → 回调时调 injectInput
6. 当前 A' 方案（pool.send 新文字 prompt）替换为 injectInput

**优点**：
- 不改 spawn 模式（保持 PTY）
- 不需要 OAuth
- GLM 模型可用
- 答案直接进入 CC 对话流（不是新 turn）
- 改动范围可控（pool.ts + feishu.ts）

**风险**：
- CC TUI 格式依赖（键盘输入格式可能随 CC 版本变化）
- 需要搞清楚 CC AskUserQuestion TUI 的键盘交互协议（数字键？方向键？Enter？）
- pool 当前没有写 PTY stdin 的机制，需新增
- 需要解决"CC 等待键盘输入时 pool 认为 busy"的超时问题

### 路径 B：CC Channels API（research 推荐 Phase 2）

**原理**：`claude --channels server:relay-feishu` → MCP channel 子进程 → 结构化 `permission_request` / `permission` 协议。

```
Relay 进程
├── 现有 pool（-p / PTY，批量/GLM 保留）
└── 新 ChannelBridge（per tenant/session）
      ├── claude --channels server:relay-feishu
      ├── MCP server 声明 claude/channel + claude/channel/permission
      ├── 出站 permission_request → 飞书卡片
      └── 入站 callback → MCP notification permission verdict
```

**优点**：
- Anthropic 官方方案，协议稳定
- 同时解决 AskUserQuestion 和工具权限审批
- 不依赖 TUI 格式

**风险/约束**：
- **需要 claude.ai OAuth**（纯 API key 不支持 Channels）
- **GLM 模型不可用**（Channels 只支持 Claude）
- **官方设计 single-user**（multi-tenant 需每租户独立 CC session + channel）
- 3-5 周 POC，6-8 周生产化
- 与现有 pool 架构并存需设计

### 路径 C：A' 方案修正（改 pool 传递 tool_use）

**原理**：保持 A' 设计（拦截 tool_use → 飞书卡片 → pool.send 新文字 prompt），但修改 pool 让 PTY 模式也传递 tool_use 块。

**需要的改动**：
1. 去掉 `--bare`
2. `emitInteractiveAssistant` 增加 tool_use 块透传
3. `handleInteractiveRecord` 处理 `stop_reason=tool_use` 的记录
4. 其余 A' 代码（T-1.2 ~ T-1.9）保持不变

**优点**：
- 改动最小（只需改 pool 的 JSONL 解析逻辑）
- 已实现的 feishu.ts 代码不用改

**风险**：
- A' 方案中 CC 在 `-p` 模式下会自动回答 AskUserQuestion（`is_error: true`），但 PTY 模式下 CC 会**挂起等待键盘输入**——pool.send 新 prompt 无法写入，因为 CC 还在等 AskUserQuestion 的回答
- 需要同时解决"CC 挂起等待"的问题（可能需要键盘模拟来跳过，或等 CC 超时）
- 本质上回到了路径 A 的键盘模拟需求

---

## 4. 关键待验证项

| # | 问题 | 验证方法 | 影响路径 |
|---|------|----------|----------|
| V1 | CC PTY 模式下 AskUserQuestion TUI 的键盘交互协议是什么？ | 手动运行 CC PTY，触发 AskUserQuestion，观察 TUI 并测试键盘输入 | A |
| V2 | 去掉 `--bare` 后，CC 在 PTY 模式下的副作用（hooks、LSP、auto-memory 等）是否可接受？ | 去掉 `--bare` 后跑完整飞书对话回归 | A、C |
| V3 | CC PTY 模式下调用 AskUserQuestion 时，JSONL 里 stop_reason 是 `tool_use` 还是其他值？ | 生成一个包含 AskUserQuestion 的 CC 会话，查看 JSONL | A、C |
| V4 | Channels API 是否支持 GLM 模型？ | 查阅 CC Channels 文档或实测 | B |
| V5 | pool 当前能否写 CC 进程的 stdin（PTY master）？ | 查看 pool.ts 中 CC 进程的 stdio 配置 | A |

---

## 5. 当前代码状态

- **所有 T-1.x 变更已提交到工作区**（未 git commit）
- `--bare` 已从 `buildInteractiveArgs` 移除（用于测试）
- `relay.json` 所有 agent 的 `allowedTools` 已添加 `AskUserQuestion`
- `DEFAULT_ALLOWED_TOOLS` 已添加 `AskUserQuestion`
- build 通过，test 通过
- Relay 已重启运行

**注意**：去掉 `--bare` 是临时测试改动。如不采用路径 A/C，需要还原。

---

## 6. 决策需要

需要大鸟拍板：

1. **走哪条路径？** A（键盘模拟）/ B（Channels API）/ C（修正 A'）/ 其他
2. **是否立即做？** 还是先记录，后续排期
3. **如果走路径 A**：先验证 V1-V5 再动手，还是边做边验证

---

## 7. 决策与验证记录（2026-06-10 晚）

### 决策（大鸟拍板）

- **路径 A**（PTY 键盘模拟），立即做，先验证 V1/V3 再动手
- 路径 C 不是独立选项：PTY 下 CC 挂起等键盘输入，最终仍需键盘模拟，C 的 pool 改动恰为 A 的子集
- 路径 B 与现状硬冲突（GLM 不可用 + 需 OAuth），仅作长期方向
- `--bare` 移除转为**永久决策**，相关测试断言已同步更新

### 待验证项结果（服务器实测，CC 2.1.161 + glm-5.1 + claude_pty_bridge.py）

| # | 问题 | 结果 |
|---|------|------|
| V1 | TUI 键盘协议 | ✅ **数字键单击即选中并提交**（单选无需 Enter）。TUI 在模型选项后追加 `Type something.` / `Chat about this` 两项，数字映射仍 1-based 对应模型选项。`Esc` 取消 |
| V3 | JSONL stop_reason | ✅ assistant record `stop_reason: "tool_use"`，tool_use 块含完整结构化 input（questions/header/options/multiSelect）。应答后出现 tool_result user record + 最终 `end_turn` |
| V5 | pool 能否写 PTY stdin | ✅ 早已具备：`claude_pty_bridge.py` 将 stdin 管道桥接到 PTY master；bypass dialog / root trust dialog 的自动应答即是先例 |
| Esc 后行为（补测） | 超时取消后 turn 是否收尾 | ⚠️ Esc 产生 tool_result(rejected) + `[Request interrupted by user for tool use]`，**无 end_turn record**——CC 回到 prompt 但 pool 仍 busy。超时回退必须跟注一条单行继续 prompt（Esc → 500ms → 文本 → 500ms → Enter）让 turn 正常收尾 |
| V2 | 去 --bare 副作用 | 观察到 CC 尝试 auto-update（无 npm 写权限而失败，无害）。可后续用 `DISABLE_AUTOUPDATER=1` 抑制。其余副作用待 UAT 回归观察 |
| V4 | Channels 支持 GLM | 未验证（路径 B 未选） |

### 路径 A 实现（同日完成，build ✅ / test 774 passed + 2 个无关环境竞态）

| 文件 | 变更 |
|------|------|
| `pool.ts` | ① `handleInteractiveRecord` 检测 AskUserQuestion tool_use（带 `interactiveAskEmitted` 去重），以 stream-json 同构形态 emit `response` 事件——feishu 现有拦截代码零改动复用；② `injectInput(tenantId, text)` 写 PTY stdin（非 interactive 模式抛错）；③ busy timer 在 `isPendingInteractive` 时跳过告警并滑动基线 |
| `types.ts` / `commands.ts` / `conversation-runtime.ts` | `Pool.injectInput?` → adapter → manager 委托链 |
| `feishu.ts` | ① `handleAskAnswer` 优先 injectInput 注入数字键（multiSelect 加 `\r`），失败回退 A' 文字 prompt（覆盖 -p 模式与 Relay 重启后恢复的 pendingAsk）；② 超时清理注入 Esc + 单行继续 prompt + Enter |
| `tests/pool.test.ts` / `tests/token-fallback.test.ts` | `--bare` 断言反转为 `not.toContain`（永久移除） |

### 已知限制（MVS 范围外）

1. **多问题（questions.length > 1）**：卡片可渲染，但一次点击只注入一个问题的数字键，TUI 顺序与点击顺序可能错位 → 依赖 30 分钟超时回退兜底
2. **multiSelect 键序未实测**（digits + Enter 为合理推测）
3. **等待应答期间用户发普通消息**：消息会排队到应答/超时后才送达（pool busy）

### UAT 问题 #1（2026-06-10 21:31，已修复）

**现象**：member-0001-pulse 收到文字「已经调用 AskUserQuestion…」但无按钮。

**根因**：该 agent 走 **print 模式**（`-p` + `deepseek-v4-flash`），`pool.ts` 为 streaming 性能会丢弃「仅 tool_use、无 text」的 assistant 记录——AskUserQuestion 恰是这类记录，feishu 拦截逻辑从未触发。

**修复**：print 模式透传 `AskUserQuestion` tool_use（与 PTY 路径对齐）；已部署重启。

### UAT 问题 #2（2026-06-10 21:41，已修复）

**现象**：第二次测试完全无回复。

**根因链**：
1. tool_use 已透传，feishu 尝试发卡片
2. 飞书 API 400：`schema V2 no longer support tag action`（`buildAskUserQuestionCard` 用了已废弃的 `{ tag: 'action', actions: [...] }` 包裹）
3. 卡片发送失败**之前**已 `registerPendingAsk(suppressNext: true)` → CC 后续过渡文字被抑制 → 用户什么都看不到

**修复**：
- 按钮改为 Schema 2.0 直入 `elements`（与 `relay_switch` / `relay_copy` 同模式）
- 仅卡片发送成功后才注册 pendingAsk

**注意**：member-0001 在 print 模式下点选后走 A' 回退（`pool.send` 文字 prompt），不是 PTY 键盘注入；pulse 总管（GLM + interactive）才走路径 A 完整链路。

### 下一步

- [x] 部署服务器 + 重启 Relay（2026-06-10 21:27 CST：`tar+scp` → `/opt/PULSE/Bigbird/Relay`，`relay.json` 为 `member-0001-pulse` 补 `AskUserQuestion`，`relay.service` active，2 个 Feishu bot WS 已连）
- [x] 飞书 UAT（2026-06-10 晚，member-0001-pulse）：AskUserQuestion 卡片 + 按钮 + 点选 + CC 继续回复，**一切符合预期** ✅
- [ ] **工具权限审批** UAT（Phase 2 范围，当前未实现）：Bash/Write/Edit 等需用户点「允许/拒绝」的场景
- [ ] UAT 通过后 git commit
