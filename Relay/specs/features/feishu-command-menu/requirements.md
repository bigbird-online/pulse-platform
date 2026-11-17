---
feature_name: "feishu-command-menu"
version: "0.2.0"
created: "2026-06-13T23:45:00+0800"
modified: "2026-06-14T10:16:00+0800"
upstream_dependency: ["bot-commands/design.md", "feishu-channel/design.md", "project-architecture.md"]
tags: [requirements, l2-spec, feishu, menu-card, relay-m3]
changelog:
  - "v0.2.0: review 修正——移除 switch 按钮（飞书固定 bot 模式无意义）、补卡片生命周期约束"
  - "v0.1.0: 初始版本——/menu 唤起分组菜单卡片 + 二级选项卡片"
---

# feishu-command-menu - 功能需求文档

> **功能概述**：将飞书 bot 的常用文本命令（`/new` `/model` `/runtime` 等）封装为可点击的交互卡片按钮。用户发送 `/menu` 唤起主菜单卡片，按分组展示命令按钮；带参数的命令（model/runtime/yolo）通过二级选项卡片完成，无需手打参数。
>
> **设计原则**：菜单卡片是 bot-commands 的 **UI 触达层**，不新增命令语义，复用现有 `handleXxx` 处理函数。
>
> **关联文档**：
> - 上游依赖：`features/bot-commands/`（命令语义 SSOT）、`features/feishu-channel/`（卡片渲染管道）
> - 复用：`CommandResult.feishuCardElements`、`handleFeishuCardAction`

---

## 1️⃣ 用户故事

**故事1：不记命令也能操作**
- **作为**：飞书用户（含非技术用户）
- **我希望**：不用记忆 `/new` `/stop` `/model` 等命令文本，点按钮就能完成会话/进程控制
- **以便**：降低使用门槛，减少因命令拼写错误导致的挫败
- **优先级**：P0

**故事2：带参数命令纯按钮完成**
- **作为**：需要切换模型/runtime 的用户
- **我希望**：点"模型"按钮后看到可选模型列表（sonnet/opus/haiku/auto），再点一个完成切换，不用手打 `/model sonnet`
- **以便**：切换操作两步点击完成，且不会输错模型名
- **优先级**：P0

**故事3：runtime / yolo 配置一键切换**
- **作为**：需要在 CC 与 Codex 之间切换、或开关 Codex YOLO 模式的用户
- **我希望**：点"runtime"看到 cc/codex 按钮、点"yolo"看到 on/off 按钮，一键完成
- **以便**：配置类操作全部按钮化，与文本命令等价但更直观
- **优先级**：P0

---

## 2️⃣ 功能需求

### P0 功能

**FR-P0-001：`/menu` 唤起主菜单卡片**
- **描述**：用户在飞书发送 `/menu`，bot 回复一张交互卡片，按四个分组展示命令按钮（每组按钮横排紧凑排列）：
  - **会话**：`new`、`reset`
  - **进程**：`stop`、`kill`、`restart`
  - **查看**：`status`、`session`、`usage`
  - **配置**：`runtime`、`model`、`yolo`
- **来源**：故事1

**FR-P0-002：动作类按钮直接执行**
- **描述**：点击会话/进程分组的按钮（new/reset/stop/kill/restart），等同于执行对应文本命令，回复执行结果。`reset` 按钮以红色（danger）样式与 `new` 区分，降低误点
- **来源**：故事1

**FR-P0-003：查询类按钮直接回复**
- **描述**：点击查看分组的按钮（status/session/usage），等同于执行对应查询命令，回复查询结果
- **来源**：故事1

**FR-P0-004：配置类按钮展开二级选项卡片**
- **描述**：点击配置分组的按钮（runtime/model/yolo），bot 回复一张二级卡片，列出可选项按钮：
  - `model` → 从 `modelCatalog.listIds()` 生成选项按钮 + `auto` + `default`
  - `runtime` → `cc` / `codex` 两按钮
  - `yolo` → `on` / `off` / `status` 三按钮（仅 Codex runtime 下 on/off 有效）
- **来源**：故事2、3

**FR-P0-005：二级卡片选项按钮执行切换**
- **描述**：点击二级卡片的选项按钮（如 model 卡片的 `sonnet`），等同于执行带参数命令（如 `/model sonnet`），回复切换结果
- **来源**：故事2、3

**FR-P0-006：按钮回调与命令权限一致**
- **描述**：按钮点击回调走 `handleFeishuCardAction`，通过 openId 解析到用户，权限边界（admin/tenant、Agent 归属）与现有文本命令完全一致，不因按钮触达而绕过权限
- **来源**：故事1、2、3

### P1 功能（延后）

**FR-P1-001：当前状态回显在二级卡片**
- **描述**：二级选项卡片在选项按钮旁标注当前值（如 model 卡片标注"当前: opus"，对应按钮标记为当前选中）。延后实现，首版仅展示选项
- **来源**：故事2

---

## 3️⃣ 验收标准

**AC-P0-001：/menu 唤起主菜单**
- **条件**：用户在飞书发送 `/menu`
- **预期结果**：2 秒内收到一张交互卡片，包含会话/进程/查看/配置四个分组的按钮，每组按钮横排
- **验证方式**：手动验证

**AC-P0-002：动作按钮执行**
- **条件**：点击主菜单"会话"分组的 `new` 按钮
- **预期结果**：bot 执行新会话逻辑，回复与 `/new` 文本命令一致的结果
- **验证方式**：手动验证

**AC-P0-003：model 二级卡片切换**
- **条件**：点击主菜单"配置"分组的 `model` 按钮 → 收到二级卡片 → 点击 `sonnet` 按钮
- **预期结果**：bot 执行模型切换，回复与 `/model sonnet` 一致的结果
- **验证方式**：手动验证 + Unit Test（回调 value 解析）

**AC-P0-004：runtime 二级卡片切换**
- **条件**：点击 `runtime` 按钮 → 收到 cc/codex 二级卡片 → 点击 `codex`
- **预期结果**：bot 切换 runtime，回复与 `/runtime codex` 一致的结果
- **验证方式**：手动验证

**AC-P0-005：权限边界**
- **条件**：非管理员用户点击 `kill` 按钮
- **预期结果**：与 `/kill` 文本命令的权限校验结果一致（被拒绝或受限）
- **验证方式**：Unit Test

---

## 4️⃣ 非功能需求

- **响应延迟**：点击按钮到收到回复 ≤2 秒（与文本命令一致）
- **幂等**：按钮回调复用现有命令处理路径，天然继承其幂等性（如 `/new` 重复执行不会崩溃）
- **复用**：菜单卡片走 `CommandResult.feishuCardElements` + `buildMarkdownCard` 现有渲染管道，不新建卡片协议
- **可观测**：按钮点击在 console.log 记录（openId、action、arg），与命令执行日志对齐

---

## 5️⃣ 约束条件

- **仅飞书**：telegram 已有 `inlineKeyboard` 机制，本 feature 不涉及 TG
- **不改命令语义**：菜单按钮触发的逻辑与文本命令完全等价，复用 `handleNew`/`handleStop`/`handleModel` 等函数
- **按钮 value 约定**：新增 `relay_menu`（action）+ `relay_menu_arg`（可选参数）两个字段（详见 design.md），与已有 `relay_switch`/`relay_copy`/`relay_ask_answer` 并列，统一 `relay_` 前缀
- **按需唤起**：主菜单不常驻、不在每条回复后附带，仅 `/menu` 触发
- **卡片生命周期**：菜单卡片与二级卡片发出后不主动清理/更新（飞书机器人不 patch 已发卡片）；用户点选后结果以新消息发出，旧卡片留在聊天记录中可手动撤回。属已知行为
- **`/help` 保持文本**：help 仍为纯文本命令（菜单卡片本身就是快捷操作入口，无需再把 help 按钮化）

---

## 6️⃣ 排除范围

- ❌ telegram 菜单卡片（已有 inlineKeyboard）
- ❌ 常驻菜单卡片（bot 启动时自动发送）
- ❌ 每条回复附带快捷按钮栏
- ❌ 新增命令语义（菜单只是现有命令的 UI 封装）
- ❌ switch / list 按钮（飞书为固定 bot 模式，`handleCommand` 已对 `/list` `/switch` 直接拒绝；菜单不引入该能力）
- ❌ `/start` `/help` `/approve` `/jobs` 系列按钮化（非常用/高级操作，保持文本）
- ❌ 按钮点击动画/加载态定制（飞书原生处理）
- ❌ 已发卡片的主动更新/删除（不实现 card patch）
