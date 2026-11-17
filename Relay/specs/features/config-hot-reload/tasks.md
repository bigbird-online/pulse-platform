---
feature_name: "config-hot-reload"
version: "0.3.0"
created: "2026-06-12T14:00:00+0800"
modified: "2026-08-17T00:00:00+0800"
upstream_dependency: ["design.md", "requirements.md"]
tags: [tasks, l2-spec, config, hot-reload, relay-m3]
changelog:
  - "v0.3.0: node-placement exact registry reconcile replaces incremental helper names"
  - "v0.2.0: review 修正——reload 顺序、多 poolKey 遍历、mutex、验证标准补充"
  - "v0.1.0: 初始版本"
---

# config-hot-reload - 任务拆分文档

> **任务概述**：实现信号文件驱动的配置热更新，使 Relay 增减租户/Agent 不需重启。

---

## 1️⃣ 任务拆分

| 任务 ID | 任务名称 | 预估工时 | 依赖任务 | 状态 |
|---------|---------|---------|---------|------|
| P0-001 | ReloadEngine 核心模块 | 3h | — | 🔲 待开始 |
| P0-002 | 飞书 bot stopAgent | 2h | — | 🔲 待开始 |
| P0-003 | 删除 Agent 时优雅停 pool | 1h | P0-001 | 🔲 待开始 |
| P0-004 | /reload 命令 | 1h | P0-001 | 🔲 待开始 |
| P0-005 | HTTP API 改造 | 1h | P0-001 | 🔲 待开始 |
| P0-006 | index.ts 集成 | 1h | P0-001~005 | 🔲 待开始 |
| P0-007 | 全量测试 | 2h | P0-006 | 🔲 待开始 |
| P1-001 | provision-agent 自动写信号 | 1h | P0-001 | 🔲 待开始 |
| P2-001 | TG bot 热增减 | 3h | P0-001 | 🔲 延后 |

---

## 2️⃣ 任务详情

### P0-001：ReloadEngine 核心模块

**新增文件**：`src/reload-engine.ts`

**实现内容**：
- `ReloadSignal` / `ReloadResult` / `ReloadEngineDeps` 类型定义
- `createReloadEngine(deps)` 工厂函数
- `start()`：`mkdirSync(signalDir, {recursive:true})`，启动 `fs.watch`
- `stop()`：关闭 watcher
- `reload(signal)`：mutex 保护 + scope 分发，严格按顺序：②采集 poolKey → ③router.reload → ④feishu → ⑤scheduler → ⑦kill pool
- debounce 300ms 防抖
- JSON.parse 验证信号完整性
- 执行完毕后 `fs.unlink` 删除信号文件
- 每次执行输出 console.log（scope、耗时、结果）

**验证标准**：
- [ ] **Unit Test-001**：写入信号文件 → reload 被触发 → 信号文件被删除
- [ ] **Unit Test-002**：不完整的 JSON 信号 → 不触发 reload
- [ ] **Unit Test-003**：300ms 内多次写入 → 只触发一次 reload
- [ ] **Unit Test-004**：scope="router" → 只调 router.reload()，不调 feishu
- [ ] **Unit Test-005**：reload 进行中再次调 reload → 返回 `reload-in-progress`

### P0-002：飞书 bot stopAgent

**修改文件**：`src/channels/feishu.ts`

**实现内容**：
- `FeishuBotInstance` 接口新增 `stopAgent(agentId: string): Promise<void>`
- `reload()` 返回类型扩展为 `{ added, removed, total }`
- 在 `createFeishuBots` 中实现 `stopAgent`：从 `liveBots` Map 找到对应 bot → 断开 WS → 清理 listeners → 移除
- `reload()` 使用 `reconcileFeishuClientRegistry()` exact reconcile（对比 running vs config，先 stop removals 再 start additions）

**验证标准**：
- [ ] **Unit Test-001**：config 删除一个 agent → reload → 该 agent 的 WS 断开，`removed` 含 agentId
- [ ] **Unit Test-002**：config 新增一个 agent → reload → 新 bot 启动，旧 bot 不受影响
- [ ] **Unit Test-003**：config 不变 → reload → `added` 和 `removed` 都为空

### P0-003：删除 Agent 时优雅停 pool

**修改文件**：`src/reload-engine.ts`（在 P0-001 基础上）

**实现内容**：
- reload 时检查 `signal.removedAgents`
- 在 router.reload() **之前**遍历 `pool.getAll()`，筛选 poolKey 包含 agentId 的活跃进程
- 一个 agentId 可能匹配多个 poolKey（多用户、飞书/TG 分开），全部记入 `toKill`
- router.reload() 之后，对 `toKill` 中每个 poolKey 调 `pool.kill(poolKey)` 触发 `session-end`

**验证标准**：
- [ ] **Unit Test-001**：removedAgents=["x"]，x 有 2 个活跃 pool（2 个用户）→ 两个都被 kill
- [ ] **Unit Test-002**：removedAgents=["x"]，x 无活跃 pool → 无操作，不报错
- [ ] **Unit Test-003**：removedAgents=["x"]，poolKey 含 "xy" 的 agent 不被误杀

### P0-004：/reload 命令

**修改文件**：`src/channels/feishu.ts`（handleCommand switch）

**实现内容**：
- 新增 `case 'reload'`
- 解析参数（scope，默认 "all"）
- 调 `reloadEngine.reload(signal)` 直接执行（不等 watcher）
- 格式化 ReloadResult 为飞书文本/卡片回复
- 权限控制：仅管理员可用

**验证标准**：
- [ ] **Unit Test-001**：`/reload feishu` → scope 传入 feishu
- [ ] **Unit Test-002**：非管理员 `/reload` → 拒绝

### P0-005：HTTP API 改造

**修改文件**：`src/http-server.ts`

**实现内容**：
- `POST /internal/agents/reload` 接受 JSON body `{ scope?, removedAgents? }`
- 调 `reloadEngine.reload()` 替代现有直接调 `feishuBots.reload()`
- 向后兼容：body 为空时 scope 默认 "feishu"
- 返回 ReloadResult JSON

**验证标准**：
- [ ] **Unit Test-001**：POST body `{scope:"all"}` → 全量 reload
- [ ] **Unit Test-002**：POST body 为空 → scope 默认 feishu（向后兼容）

### P0-006：index.ts 集成

**修改文件**：`src/index.ts`

**实现内容**：
- 创建 `ReloadEngine` 实例，注入 router/feishuBots/scheduler/pool deps
- 注入到 feishu bot deps（/reload 命令需要）
- 注入到 HTTP server deps
- 在启动流程末尾调 `reloadEngine.start()`
- 在 shutdown 流程中调 `reloadEngine.stop()`

**验证标准**：
- [ ] 启动后 `~/.relay/signals/` 目录存在
- [ ] shutdown 后 watcher 关闭

### P0-007：全量测试

**验证标准**：
- [ ] 778 现有测试全通过（零回归）
- [ ] 新增 ~10 个单元测试覆盖 P0-001~005

---

## 3️⃣ 完成标准

- 信号文件写入后 2 秒内 reload 生效
- 新增 Agent 后飞书 bot 可用
- 删除 Agent 后 pool 进程优雅退出、飞书 bot 断开
- /reload 命令回复结果
- 现有测试零回归
