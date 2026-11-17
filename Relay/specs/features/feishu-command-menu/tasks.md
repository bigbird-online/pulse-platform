---
feature_name: "feishu-command-menu"
version: "0.2.0"
created: "2026-06-13T23:45:00+0800"
modified: "2026-06-14T10:16:00+0800"
upstream_dependency: ["design.md", "requirements.md"]
tags: [tasks, l2-spec, feishu, menu-card, relay-m3]
changelog:
  - "v0.2.0: review 修正——移除 switch 任务、arg→relay_menu_arg、补 modelCatalog 注入测试、column_set 横排、reset danger"
  - "v0.1.0: 初始版本"
---

# feishu-command-menu - 任务拆分文档

> **任务概述**：将飞书 bot 常用文本命令封装为 `/menu` 唤起的交互卡片按钮，带参数命令通过二级选项卡片完成。复用现有 `handleXxx` 命令函数与 `CommandResult.feishuCardElements` 渲染管道，不新增命令语义。
>
> **开发规则**：遵循 `Doc/rules/spec-driven-developer/130-tdd-implementation.md`——每个任务先写测试（Red），再实现（Green），再重构（Refactor）。

---

## 1️⃣ 任务拆分

| 任务 ID | 任务名称 | 预估工时 | 依赖任务 | 状态 |
|---------|---------|---------|---------|------|
| P0-001 | buildMenuCard 主菜单构建 | 1h | — | ✅ 已完成 |
| P0-002 | 二级选项卡片构建函数 | 1.5h | — | ✅ 已完成 |
| P0-003 | dispatchMenuAction + 回调分支 | 2h | P0-001、P0-002 | ✅ 已完成 |
| P0-004 | /menu 命令注册 + help 更新 | 0.5h | P0-001 | ✅ 已完成 |
| P0-005 | 全量回归测试 | 1h | P0-001~004 | ✅ 已完成 |

---

## 2️⃣ 任务详情

### P0-001：buildMenuCard 主菜单构建

**新增于**：`src/commands.ts`

**实现内容**：
- `buildMenuCard(agent: AgentConfig): CommandResult`
- 返回 `text`（简短引导语）+ `feishuCardElements`
- 4 分组，每组 `[hr, markdown 标题, column_set(横排按钮)]`：
  - 会话：`new` `reset`（reset 用 `type:'danger'`）
  - 进程：`stop` `kill` `restart`
  - 查看：`status` `session` `usage`
  - 配置：`runtime` `model` `yolo`
- 每个按钮 value：`{ relay_menu: '<action>' }`（一级，无 `relay_menu_arg`）
- column_set 实现：每个按钮放一列（`width:'auto'`），实现组内横排

**验证标准（TDD）**：
- [ ] **Unit Test-001**：`buildMenuCard(agent)` 返回的 `feishuCardElements` 含 4 个 `column_set`（4 分组各一）
- [ ] **Unit Test-002**：每个动作按钮的 `behaviors[0].value` 为 `{ relay_menu: '<action>' }`，无 `relay_menu_arg`
- [ ] **Unit Test-003**：按钮总数 = 2+3+3+3 = 11 个
- [ ] **Unit Test-004**：`reset` 按钮的 `type` 为 `'danger'`，其余动作为 `'default'`

### P0-002：二级选项卡片构建函数

**新增于**：`src/commands.ts`

**实现内容**：
- `buildModelOptionCard(agent, userId, deps): CommandResult`
  - 从 `deps.modelCatalog.listIds()` 读模型列表
  - 每个模型生成按钮 value `{ relay_menu: 'model', relay_menu_arg: m.id }`
  - 追加 `auto`、`default` 两按钮
  - modelCatalog 为空/undefined 时仅返回 auto+default + 提示
- `buildRuntimeOptionCard(agent): CommandResult`
  - 两按钮：`{ relay_menu:'runtime', relay_menu_arg:'cc' }`、`{ relay_menu:'runtime', relay_menu_arg:'codex' }`
- `buildYoloOptionCard(agent): CommandResult`
  - 三按钮：`relay_menu_arg:'on'` / `'off'` / `'status'`
  - note 提示当前 runtime（非 Codex 时提示先切 Codex）

**验证标准（TDD）**：
- [ ] **Unit Test-001**：`buildModelOptionCard` 有 modelCatalog 时，按钮数 = catalog 数 + 2（auto/default），每个含 `relay_menu_arg`
- [ ] **Unit Test-002**：`buildModelOptionCard` modelCatalog 为 undefined 时，按钮数 = 2，text 含提示
- [ ] **Unit Test-003**：`buildRuntimeOptionCard` 返回 2 个按钮，`relay_menu_arg` 分别为 `cc`/`codex`
- [ ] **Unit Test-004**：`buildYoloOptionCard` 返回 3 个按钮（on/off/status）

### P0-003：dispatchMenuAction + 回调分支

**修改文件**：`src/channels/feishu.ts`

**实现内容**：
- 新增 `dispatchMenuAction(ctx, openId, action, arg?)`：
  - `resolveByFeishuAgent` 解析用户，失败回复提示
  - **构造 commandDeps 必须含 `modelCatalog: ctx.modelCatalog`**（现有回调 commandDeps 缺此项，是本任务关键修正点）
  - 有 `arg`：分发到 `handleModel`/`handleRuntime`/`handleYolo`（带 arg）
  - 无 `arg` + 动作/查询组：分发到对应 `handleXxx`
  - 无 `arg` + 配置组：调对应 `buildXxxOptionCard`，`sendCardMessage` 发二级卡片
- `handleFeishuCardAction` 在已有分支前插入 `relay_menu` 分支：
  ```typescript
  const relayMenu = typeof value?.relay_menu === 'string' ? value.relay_menu : '';
  const relayMenuArg = typeof value?.relay_menu_arg === 'string' ? value.relay_menu_arg : '';
  if (relayMenu && openId) {
    await dispatchMenuAction(ctx, openId, relayMenu, relayMenuArg);
    return;
  }
  ```
- 权限：复用 `resolveByFeishuAgent` + 各 `handleXxx` 内部校验

**验证标准（TDD）**：
- [ ] **Unit Test-001**：`dispatchMenuAction(ctx, openId, 'new', '')` 调用 `handleNew` 一次
- [ ] **Unit Test-002**：`dispatchMenuAction(ctx, openId, 'model', 'sonnet')` 调用 `handleModel(uid, agentId, 'sonnet', deps)`，且传入的 deps **含 modelCatalog**（断言 `deps.modelCatalog === ctx.modelCatalog`）
- [ ] **Unit Test-003**：`dispatchMenuAction(ctx, openId, 'model', '')` 调用 `buildModelOptionCard` 并 `sendCardMessage` 一次
- [ ] **Unit Test-004**：`resolveByFeishuAgent` 返回 null 时回复"无法解析用户"，不调任何 handleXxx
- [ ] **Unit Test-005**：`handleFeishuCardAction` 收到 `{relay_menu:'stop'}` 时进入 relay_menu 分支（dispatchMenuAction 被调用）；收到 `{relay_copy:true}` 时仍走原 copy 分支（回归）
- [ ] **Unit Test-006**：`handleFeishuCardAction` 收到 `{relay_switch:'agent-x'}` 时仍走原 switch 分支（回归，不被 relay_menu 拦截）

### P0-004：/menu 命令注册 + help 更新

**修改文件**：`src/channels/feishu.ts`、`src/commands.ts`

**实现内容**：
- `handleCommand` switch 新增 `case 'menu'`：
  ```typescript
  case 'menu': {
    const card = buildMenuCard(agent);
    await ctx.sendCardMessage(openId, buildMarkdownCard(card.text, {
      header: { title: `${agent.emoji} ${agent.name} 菜单` },
      trailingElements: card.feishuCardElements,
    }));
    return;
  }
  ```
- `handleHelp()` 文本补一行：`/menu — 打开命令菜单（按钮版）`

**验证标准**：
- [ ] **Unit Test-001**：`handleCommand` 收到 `['menu']` 时调用 `ctx.sendCardMessage` 一次，卡片含 `feishuCardElements`
- [ ] **Unit Test-002**：`handleHelp()` 返回文本包含 `/menu`

### P0-005：全量回归测试

**验证标准**：
- [ ] 现有测试全通过（零回归）
- [ ] 新增 ~14 个单元测试覆盖 P0-001~004
- [ ] 手动验证：飞书发 `/menu` → 收到分组卡片（横排） → 点 new 执行 → 点 model 弹二级 → 点 sonnet 切换 → 点 runtime 弹 cc/codex → 点 codex 切换

---

## 3️⃣ 完成标准

- `/menu` 返回分组主菜单卡片（column_set 横排）
- 动作/查询按钮点击后行为与对应文本命令一致
- model/runtime/yolo 按钮展开二级选项卡片，选项点击后执行切换
- 按钮权限边界与文本命令一致
- 现有测试零回归，新增单测覆盖构建函数与分发逻辑（含 modelCatalog 注入断言）

---

## 4️⃣ 风险与注意

- **飞书 column_set 兼容性**：Schema 2.0 的 `column_set` + `column`（`width:'auto'`）应支持按钮横排。若实际渲染按钮不横排或被截断，回退为每组按钮竖排（`width:'default'`），卡片略长但功能不受影响。需手动验证一次渲染效果。
- **modelCatalog 注入**：dispatchMenuAction 必须显式从 `ctx.modelCatalog` 注入到 commandDeps（现有 `handleFeishuCardAction` 回调的 commandDeps 模板缺此项，勿照抄）。
- **卡片消息不可编辑**：二级卡片发出后用户若切换了 runtime，旧卡片选项不变（如 yolo 卡片在非 Codex 下发出）。属可接受行为，note 文本提示即可。
