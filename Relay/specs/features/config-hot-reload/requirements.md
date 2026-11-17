---
feature_name: "config-hot-reload"
version: "0.3.0"
created: "2026-06-12T14:00:00+0800"
modified: "2026-08-17T00:00:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md"]
tags: [requirements, l2-spec, config, hot-reload, relay-m3]
changelog:
  - "v0.3.0: node-placement 模式 exact reconcile、running-job fence 与失败事务语义"
  - "v0.2.0: review 修正——reload 顺序约束、多 poolKey、mutex、信号目录创建、删自身 bot 边界"
  - "v0.1.0: 初始版本"
---

# config-hot-reload - 功能需求文档

> **2026-08-17 node-placement addendum**：placement 模式覆盖本文旧的增量/P2 语义。`/internal/agents/reload` 必须同时 exact reconcile Telegram long-poll 与 Feishu WebSocket；任一 scheduler job running 时在所有 mutation 前拒绝。removed client 真 stop，stop failure 零 additions，start failure 回退本轮 additions；旧 shared listener 必须 inactive。legacy 非 placement 模式维持原有渠道增量语义，并在渠道重载之前重读 `cron-jobs.json`（`scheduler.reload()`）：先同步落盘当前运行状态，再完整读取并校验候选文件（文件缺失、不可解析、缺 `jobs` 数组、任一条目无效（含 id 非字符串）、id 重复、cron 表达式或时区非法都整次拒绝、保留原任务并返回 500；只有可读且 `jobs` 为空数组才表示没有任务），再预建全部候选 cron 注册（任一注册抛错就停掉已建的、原注册不动），通过后在同一同步段取消旧存储的全部延迟写入、停旧注册、采用候选；不停调度器、不拒绝 running job，已认领的任务按旧定义跑完本轮且仍阻止同一任务重复启动。

> **功能概述**：Relay 运行时热更新配置（增减租户/Agent/渠道绑定），不重启进程，不影响在线租户。
>
> **关联文档**：
> - 上游依赖：`features/feishu-channel/`、`features/session-management/`
> - 触发方：`provision-agent` 脚本、管理员手动编辑、Platform API

---

## 1️⃣ 用户故事

**故事1：管理员增减租户不中断服务**
- **作为**：平台管理员
- **我希望**：添加新租户或删除过期租户时，不用重启 Relay
- **以便**：现有租户的对话不受影响，服务持续可用
- **优先级**：P0

**故事2：provision-agent 脚本自动生效**
- **作为**：开发者
- **我希望**：`provision-agent` 脚本执行完毕后，新 Agent 自动上线，无需人工干预
- **以便**：租户注册流程全自动
- **优先级**：P0

**故事3：Agent 配置变更即时生效**
- **作为**：管理员
- **我希望**：修改 Agent 的模型/渠道绑定后，下次对话即使用新配置
- **以便**：运维调整不需要等待重启窗口
- **优先级**：P1

---

## 2️⃣ 功能需求

### P0 功能

**FR-P0-001：信号文件触发 reload**
- **描述**：Relay 监听 `~/.relay/signals/reload.json` 信号文件。外部系统写入信号后，Relay 检测到并执行定向 reload，完成后删除信号文件
- **来源**：故事1、2

**FR-P0-002：reload scope 分级**
- **描述**：信号文件支持 `scope` 字段，控制 reload 范围：
  - `router`：仅重建路由索引
  - `feishu`：router + 飞书 bot 增减
  - `scheduler`：重载 cron jobs
  - `all`：以上全部
  - `telegram`：router + TG bot 增减（P2 延后）
- **来源**：故事1、3

**FR-P0-003：删除 Agent 时优雅停 pool 进程**
- **描述**：信号文件的 `removedAgents` 字段列出被删除的 Agent ID。Relay 遍历 `pool.getAll()` 找到所有 poolKey 包含该 agentId 的活跃进程，执行 `session-end`（idle-reclaimed）。一个 agent 可能有多个用户的 pool 进程（poolKey = `userId:agentId` 或 `userId:agentId:feishu`），全部需要停掉
- **来源**：故事1

**FR-P0-004：飞书 bot 热增减**
- **描述**：reload 时对比新旧配置，启动新增的飞书 bot，停止已删除的飞书 bot（断开 WebSocket）。已有 bot 的配置变更（如 feishuOpenId）在下一次消息时自然生效
- **来源**：故事1、2

**FR-P0-005：/reload 命令**
- **描述**：管理员在飞书聊天中发送 `/reload [scope]`，等同于写入信号文件并等待结果。回复 reload 结果卡片（新增/删除了哪些 bot）
- **来源**：故事1、3

### P1 功能

**FR-P1-001：Agent 配置变更生效**
- **描述**：修改已有 Agent 的 ccModel/workspace 等字段后，reload 时更新 router 索引，下次 spawn 自然使用新配置。不需要停掉正在运行的 pool 进程
- **来源**：故事3

### P2 功能（延后）

**FR-P2-001：Telegram bot 热增减**
- **描述**：与飞书 bot 同理，reload 时启动/停止 TG bot。延后实现
- **来源**：故事1

---

## 3️⃣ 验收标准

**AC-P0-001：信号文件触发 reload**
- **条件**：写入 `~/.relay/signals/reload.json`，内容为 `{"scope":"feishu"}`
- **预期结果**：Relay 在 2 秒内执行 reload，信号文件被删除
- **验证方式**：Unit Test + 手动验证

**AC-P0-002：新增 Agent 自动上线**
- **条件**：provision-agent 添加新 Agent + 写信号文件
- **预期结果**：飞书 bot 启动，用户可以立即通过飞书与 Agent 对话
- **验证方式**：Integration Test

**AC-P0-003：删除 Agent 优雅停进程**
- **条件**：信号文件 `removedAgents: ["agent-x"]`，agent-x 有活跃 pool 进程
- **预期结果**：pool 进程收到 session-end，优雅退出，不丢失正在进行的回复
- **验证方式**：Unit Test

**AC-P0-004：/reload 命令**
- **条件**：管理员在飞书发 `/reload all`
- **预期结果**：Relay 执行全量 reload，回复结果卡片
- **验证方式**：手动验证

---

## 4️⃣ 非功能需求

- **延迟**：从信号文件写入到 reload 完成≤2秒（不含优雅停进程时间）
- **原子性**：信号文件是完整 JSON 才触发 reload，不完整则忽略
- **并发安全**：多次快速写入信号文件，只执行最后一次（debounce 300ms）
- **互斥**：reload 进行中收到新信号时跳过（mutex 锁），写信号方可重试
- **可观测**：每次 reload 记录 console.log（scope、新增/删除的 bot、耗时）

---

## 5️⃣ 约束条件

- 信号文件路径固定为 `~/.relay/signals/reload.json`
- 信号目录 `~/.relay/signals/` 由 Relay 启动时创建；外部写入方（provision-agent）也需确保目录存在
- 不监听 relay.json 本身（避免半写问题）
- 不强制约束外部系统的写文件方式（可以原子写也可以直接写）
- Telegram bot 热增减排 P2
- 已有 pool 进程不会被 reload 杀掉（除非 Agent 被删除）
- reload 必须先采集被删 Agent 的 poolKey（旧索引），再 `router.reload()` 重建索引

---

## 6️⃣ 排除范围

- ❌ relay.json 文件监听（只监听信号文件）
- ❌ 分布式配置中心（etcd/consul）
- ❌ 运行时 API 修改配置（改的还是文件，只是触发改成热更新）
- ❌ TG bot 热增减（P2）
