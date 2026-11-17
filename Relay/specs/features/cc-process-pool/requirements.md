---
feature_name: "cc-process-pool"
version: "0.10.0"
created: "2026-04-07T10:00:00+0800"
modified: "2026-06-03T15:40:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md"]
tags: [requirements, l2-spec, process-management, relay-core]
changelog:
  - "v0.10.0: 150-spec-sync — FR-P0-023 更新（lastStdoutSignal 替代 lastStdoutRaw；interactive stdout 'already in use' 检测）；新增 FR-P0-024（key pool sticky/skipped + 429 interactive stdout 检测 + crash restart sticky 清除 + maxRateLimitRetries=2）"
  - "v0.9.0: 150-spec-sync — outbox ACL 继承（FR-P0-022）；session-in-use 检测增强 + crash restart 孤儿清理 + restartTracking resume 保护（FR-P0-023）"
  - "v0.8.0: 150-spec-sync — S1 --preserve-env token 隔离；restartTracking 重置防崩溃循环；accept timer 通知节流"
  - "v0.7.0: 150-spec-sync — /root trust gate + claude.json writable + PTY sniffer；修正 FR-P0-017/011 interactive jsonl 路径（workspace 优先）；新增 FR-P0-021"
  - "v0.6.0: 120-feature-planning — 租户平台隔离加固（Phase A 权限 + Phase B bubblewrap）；Outbox 迁入 `{workspace}/.relay/outbox/`；新增 FR-P0-018/019、AC-P0-018/019、故事10、CONSTRAINT-003"
  - "v0.5.0: 150-spec-sync — 模型绑定 CC 模式；interactive watcher -root/；FR-P0-016/017"
---

# cc-process-pool - 功能需求文档

> **功能概述**：管理 CC 子进程的完整生命周期（spawn / send / kill / crash restart / idle cleanup），为 Relay 网关提供稳定可靠的 Claude Code 进程池。支持 pipe 模式（`claude -p`）和 PTY interactive 模式（`claude --bare`），内置 token fallback、crash replay、rate limit abort、busy progress 通知等生产级弹性机制。CC 任务可持续运行任意时长，通过定期 `busy-progress` 事件通知用户当前状态，用户可随时通过 `/stop` 命令终止。
>
> **关联文档**：
> - L1 项目愿景：`project-specs/project-vision.md`
> - L1 项目架构：`project-specs/project-architecture.md`
> - L2 技术设计：`features/cc-process-pool/design.md`
> - L2 任务拆分：`features/cc-process-pool/tasks.md`

---

## 1. 用户故事（User Stories）

### 主要用户故事

**故事1：按需启动 CC 进程**
- **作为**：Relay 网关（代表租户）
- **我希望**：当租户发来第一条消息时，自动为该租户 spawn 一个 CC 子进程
- **以便**：租户无需等待预启动，按需分配计算资源
- **优先级**：P0

**故事2：双模式运行（Pipe / Interactive）**
- **作为**：Relay 运维者
- **我希望**：进程池支持 pipe 模式（`claude -p`）和 PTY interactive 模式（`claude --bare`），可按模型目录的 provider 配置自动切换
- **以便**：不同模型后端使用合适的 CC 驱动方式：缺省用 `-p`（stream-json 双向流）；provider 声明 `force_interactive: true` 时用 `--bare`
- **优先级**：P0

**故事3：进程崩溃自动恢复 + 消息重放**
- **作为**：Relay 运维者
- **我希望**：CC 子进程崩溃后自动重启，保留会话上下文，并 replay 崩溃时正在处理的消息
- **以便**：租户对话不因进程异常而中断，无需人工干预，且消息零丢失
- **优先级**：P0

**故事4：空闲进程自动回收**
- **作为**：Relay 运维者
- **我希望**：超过 30 分钟无活动的 CC 进程自动关闭
- **以便**：释放服务器内存资源（每进程约 250MB），避免资源耗尽
- **优先级**：P0

**故事5：Token 自动降级**
- **作为**：Relay 运维者
- **我希望**：当 primary OAuth token 触发限流/配额不足时，自动切换到 fallback token 并重启进程
- **以便**：避免因单个 token 限流导致服务中断
- **优先级**：P0

**故事6：优雅关闭**
- **作为**：Relay 运维者
- **我希望**：Relay 关闭时能优雅终止所有 CC 子进程
- **以便**：避免孤儿进程残留，确保系统干净退出
- **优先级**：P1

**故事7：进程状态可观测**
- **作为**：Relay 运维者
- **我希望**：能查询当前所有 CC 进程的状态（running/idle/dead），并收到 session-end 事件通知
- **以便**：快速排查问题，了解系统负载
- **优先级**：P2

**故事8：租户文件隔离**
- **作为**：Relay 运维者
- **我希望**：每个租户的 CC 进程以独立 Linux 用户身份运行，OS 内核强制阻止跨租户文件访问
- **以便**：即使 CC 的 Bash 工具执行任意命令，也无法读写其他租户的工作区目录
- **优先级**：P0

**故事9：开通自动创建租户用户**
- **作为**：Platform 自动化流程
- **我希望**：租户开通时自动创建对应的 Linux 系统用户，并设置正确的目录权限
- **以便**：Relay spawn CC 时能立即以该用户身份运行，无需人工配置
- **优先级**：P0

**故事10：租户平台边界加固**
- **作为**：Relay 运维者 / 平台安全负责人
- **我希望**：租户 CC 进程（含 Bash 工具）无法读取 `/opt/pulse/` 平台树中的配置与文档，无法枚举其他租户，且文件访问由内核路径白名单强制约束（不仅靠 prompt）
- **以便**：租户不能通过 home 之外的路径获取不该获取的信息，也不能对平台造成破坏
- **优先级**：P0

---

## 2. 功能需求（Functional Requirements）

### P0 功能（MVP 必需）

**FR-P0-001：按需 Spawn CC 子进程**
- **描述**：收到租户消息时，检查该租户是否有活跃 CC 进程。若无，则使用 `child_process.spawn` 启动一个新的 CC 子进程，工作目录指向租户 workspace。支持两种模式：
  - **pipe 模式**：`claude -p --input-format stream-json --output-format stream-json`
  - **interactive 模式**：`python3 claude_pty_bridge.py claude --bare --permission-mode bypassPermissions`，通过 PTY bridge 与 CC 交互
- **来源**：故事1、故事2
- **示例**：租户 member-000 发来消息 -> 检测无活跃进程 -> spawn CC 进程（cwd=/opt/PULSE/Tenants/member-000/）-> 进程就绪后接收消息

**FR-P0-002：双模式通信**
- **描述**：
  - **pipe 模式**：通过 stdin 向 CC 子进程发送 stream-json 格式消息，通过 stdout 读取 CC 的 stream-json 响应。支持 NDJSON 逐行解析
  - **interactive 模式**：通过 PTY stdin 写入 prompt 文本（512B 分块），通过轮询 session jsonl 文件读取 assistant 响应。支持 warmup delay、submit delay、slash command settle 等生命周期控制
- **来源**：故事2
- **示例**：pipe 模式发送 `{"type":"user","message":{"role":"user","content":"hello"}}` -> 收到 `{"type":"assistant",...}` + `{"type":"result",...}`；interactive 模式写入 prompt -> 等 warmup 5s -> submit delay 500ms -> 轮询 jsonl 获取响应

**FR-P0-003：进程崩溃自动重启 + Crash Replay**
- **描述**：监听 CC 子进程的 `exit` 事件。非正常退出时自动 respawn（同一租户同一小时内最多重启 3 次，超过则标记为 dead 并触发 session-end）。重启时携带 `--resume <session-id>` 恢复会话。如果崩溃时有正在处理的 in-flight 消息，重启后自动 replay 该消息
- **来源**：故事3
- **示例**：CC 进程因 OOM 被 kill -> 检测到 exit code 非 0 -> 自动 respawn + resume -> replay 崩溃时的消息 -> 租户无感知

**FR-P0-004：空闲超时回收**
- **描述**：维护每个进程的 `lastActivity` 时间戳。定时检查（每 60 秒），idle 超过 30 分钟的进程发送 SIGTERM 关闭。下次租户发消息时重新 spawn + resume。回收前触发 `session-end` 事件（type: idle-reclaimed）
- **来源**：故事4
- **示例**：租户 30 分钟无消息 -> 触发 session-end(idle-reclaimed) -> 进程被回收 -> 1 小时后租户发消息 -> 重新 spawn + resume -> 对话上下文恢复

**FR-P0-005：Token 自动降级（Fallback）**
- **描述**：维护 primary/fallback 双 OAuth token。当 CC stderr 输出包含 rate limit / 429 / 401 / insufficient_quota 等信号时，自动从 primary 切换到 fallback token 并 respawn 进程。token 切换不消耗重启配额。通过 `CLAUDE_CODE_OAUTH_TOKEN` 和 `CLAUDE_CODE_OAUTH_TOKEN_FALLBACK` 环境变量或 PoolOptions 配置
- **来源**：故事5
- **示例**：primary token 限流 -> stderr 出现 429 -> 自动切换 fallback token -> respawn + resume -> 服务继续

**FR-P0-006：Rate Limit Abort**
- **描述**：当 CC 内部检测到 429 并开始内部重试时，允许一个恢复窗口（默认 90 秒）。若窗口耗尽仍 429，强制 SIGKILL 中止进程，然后走 crash restart 路径。支持按消息配置最大重试次数（`maxRateLimitRetriesPerMessage`）
- **来源**：故事3、故事5
- **示例**：CC 内部 429 重试 -> 等待 90s -> 仍未恢复 -> SIGKILL -> crash restart -> replay in-flight message

**FR-P0-007：进程 Kill**
- **描述**：提供 `kill(tenantId)` 方法，先发 SIGTERM，等待 5 秒后若未退出则发 SIGKILL。更新进程状态为 dead，清理所有关联资源（timers、watchers、pending queue）
- **来源**：故事1（反向操作）
- **示例**：调用 `pool.kill("member-000")` -> SIGTERM -> 5s -> SIGKILL（如需） -> 进程状态变为 dead

**FR-P0-008：Busy Progress 通知（替代自动 Kill）**
- **描述**：当 CC 进程进入 busy 状态时，启动周期性检查定时器（默认每 10 分钟，由 `busyTimeoutMs` / `PoolOptions.busyTimeoutMs` 控制）。每次检查时 emit `busy-progress` 事件（含 `elapsedMs`、`checkCount`、`maxChecks`、`isFinalCheck`），由通道层（TG/飞书）向用户发送状态通知（告知已运行时长 + `/stop` 提示）。**不再自动终止进程**。通知最多发送 `maxBusyChecks`（默认 10）次，达到上限后停止检查，进程继续运行直到自然完成或用户发送 `/stop`。CC 进程通常执行超过 10 分钟（深度分析、大量代码生成等），自动 kill 会中断合法任务。
- **来源**：故事3、用户反馈（CC 任务经常超过 10 分钟，自动 kill 不合理）
- **配置**：`PoolOptions.busyTimeoutMs`（默认 600,000ms = 10 分钟）为检查间隔；`PoolOptions.maxBusyChecks`（默认 10）为最大通知次数
- **示例**：CC busy 10 分钟 -> emit busy-progress(checkCount=1) -> 飞书发送「⏳ 正在执行任务（已运行 10 分钟），请耐心等待。如需终止请发送 /stop。」-> CC 继续执行 -> 20 分钟 -> 第 2 次通知 -> ... -> 100 分钟 -> 第 10 次通知（isFinalCheck=true）-> 不再检查 -> CC 自然完成或用户 /stop

**FR-P0-009：Session-end 事件分类**
- **描述**：在 session 结束时发出结构化 `session-end` 事件，区分以下原因：
  - `idle-reclaimed`：空闲回收
  - `turns-exhausted`：轮次耗尽
  - `crash-unrecoverable`：不可恢复的崩溃（超过重启限制）
  - `rate-limited`：限流中断
  - `timeout`：超时（interactive accept timeout；busy timeout 不再触发此类型，改为 busy-progress 通知）
  - `relay-shutdown`：Relay 关闭
- **来源**：故事7
- **示例**：CC 进程崩溃超过 3 次/小时 -> session-end({type: 'crash-unrecoverable', exitCode: 1})

**FR-P0-010：CC Onboarding 自动补全**
- **描述**：在每次 spawn CC 之前，幂等地检查并补齐 `~/.claude.json` 中 onboarding 关键字段（`hasCompletedOnboarding`、`theme`、`tipsHistory`、`hasIDEOnboardingBeenShown`、`customApiKeyResponses`、`shiftEnterKeyBindingInstalled`）。若字段已存在有效值则不覆盖；若文件损坏/缺失则容错处理且不阻断 spawn。
- **来源**：bug-20260509-001（CC 2.x 在以上字段为 null 时启动会进入 first-run theme picker，PTY-bridge 模式下用户输入会被 wizard 消化，导致永远不进入对话循环）
- **示例**：新租户 member-0002 首次 spawn -> ensureClaudeOnboardingComplete 写入 `theme:'dark'` + `hasCompletedOnboarding:true` -> CC 跳过 wizard 直接进入对话循环

**FR-P0-011：interactive 认证 env 策略（禁止 API_KEY 注入）**
- **描述**：API provider 认证走 `ANTHROPIC_AUTH_TOKEN`。CC interactive（`--bare`）模式下，若子进程 env 含 `ANTHROPIC_API_KEY`，会弹出 "Detected a custom API key" TUI；PTY-bridge 无人应答则阻塞。Relay spawn 前调用 `mirrorAnthropicAuthTokenToApiKey(env)` **删除**子进程 env 中的 `ANTHROPIC_API_KEY`，保留 `ANTHROPIC_AUTH_TOKEN`；不污染主进程 env。print 模式（Anthropic OAuth）仍走 `CLAUDE_CODE_OAUTH_TOKEN`。
- **来源**：bug-20260509-001 / bug-20260531 interactive 阻塞
- **示例**：provider env 注入 `ANTHROPIC_AUTH_TOKEN=<provider-token>` → spawn 前 strip `ANTHROPIC_API_KEY` → CC `--bare` 用 AUTH_TOKEN 认证，无 custom API key TUI

**FR-P0-012：thinking-only end_turn 延迟 finishTurn**
- **描述**：interactive 模式下，当一条 assistant 记录的 blocks 非空且**全部为 `thinking` 类型**且 `stop_reason: end_turn` 时，pool **不**调用 `finishTurn`，等待下一条带 `text` / `tool_use` 等消费类 block 的 end_turn 记录到达后才结束本轮。其他情形（含 text 的 record / tool-only / 空 blocks）保持原 finishTurn 行为不变。
- **来源**：bug-20260509-002（部分 Anthropic 兼容后端把 thinking 与最终 text 拆为两条独立 end_turn 记录，每条都带 `stop_reason: "end_turn"`；Anthropic 原生 API 把二者放在同一条记录中）
- **示例**：用户问「你是谁」-> CC 写入 [assistant thinking + end_turn] -> pool 不 finishTurn、状态保持 busy -> CC 继续写入 [assistant text "我是 Claude Code..."] + end_turn] -> pool emit response（含 text）+ finishTurn -> 飞书用户收到回复

**FR-P0-013：CC `--bare` 模式 CLAUDE.md 显式加载（双前置）**

`--bare` 模式同时禁用了两个机制，必须**双前置**才能加载租户 CLAUDE.md：

- **前置 A：项目级 trust 状态**：CC 2.x 在 cwd 进行 tool 读写 / 注入 settings 之前会检查 `~/.claude.json#projects[<cwd>]` 中的 `hasTrustDialogAccepted`。常规交互模式下首次进入 cwd 会弹 trust dialog 让用户接受；`--bare` 模式（PTY-bridge 无 TTY）下该 dialog 被静默跳过，cwd 永远没有 entry，CC 视其为「未受信项目」。Relay 调用 `ensureClaudeProjectRegistered(home, realpath(cwd))` 幂等写入 `projects[realpath(cwd)]` entry（注意：必须用 `realpath` 解析 symlink，因为 Linux `chdir(2)` 在子进程内 `getcwd()` 返回的是 realpath）。已有 entry 只补缺失字段不覆盖（包括用户主动设置的 `hasTrustDialogAccepted: false` 拒绝意图）。
- **前置 B：CLAUDE.md 显式声明**：CC `--bare --help` 原文「Minimal mode: skip ... auto-memory, ... and **CLAUDE.md auto-discovery**. Explicitly provide context via: --system-prompt[-file], --append-system-prompt[-file], **`--add-dir (CLAUDE.md dirs)`**, --mcp-config, --settings, --agents, --plugin-dir.」——`--bare` 硬编码禁用 CLAUDE.md 自动发现，仅靠 trust 不会让 CC 加载 cwd/CLAUDE.md。Relay 必须在 spawn args 里追加 `--add-dir <realpath(workspace)>`，明确把 workspace 声明为 CLAUDE.md 目录。

只补 trust 不加 `--add-dir`：CC 能进 cwd 读 git 状态、用 Bash 工具，但 CLAUDE.md 不被注入到 system prompt（实测：用户问「你是谁」回复中含 cwd 与 git 信息，但仍是通用 Claude Code 身份）。

- **来源**：bug-20260510-001（租户首次会话 Agent 身份失效）
- **示例**：spawn → realpath(workspace)='/opt/pulse/Tenants/member-0003' → 写入 `projects['/opt/pulse/Tenants/member-0003']` entry + spawn args 追加 `--add-dir /opt/pulse/Tenants/member-0003` → CC 加载 `<workspace>/CLAUDE.md` 到 system prompt → session jsonl 头部出现 system 消息含「pulse 管理员」身份

**FR-P0-014：OS 级租户文件隔离（sudo -u spawn）**
- **描述**：每个租户的 CC 进程以独立 Linux 系统用户（`tenant-{tenantId}`）运行。Relay 在 spawn CC 子进程时，通过 `sudo -u tenant-{tenantId}` 切换用户身份。租户工作区目录（`/opt/PULSE/Tenants/{tenantId}/`）owner 为对应租户用户，权限 `700`，内核强制阻止其他租户用户访问。
- **来源**：故事8
- **SpawnConfig 扩展**：新增 `linuxUser?: string` 字段，由 relay.json agent 配置提供
- **spawn 行为变更**：
  - 当 `config.linuxUser` 存在时，spawn 命令前缀 `sudo -u {linuxUser} --preserve-env=list`（interactive 模式：`sudo -u {linuxUser} --preserve-env=ANTHROPIC_AUTH_TOKEN,... python3 bridge.py claude ...`；print 模式同理）
  - `--preserve-env=list` 从调用者环境传递指定变量（ANTHROPIC_AUTH_TOKEN、CLAUDE_CODE_OAUTH_TOKEN、HOME 等），**不嵌入命令行**，避免敏感 token 出现在 `ps aux` 输出中（S1 安全修复）
  - 当 `config.linuxUser` 不存在时，保持原行为（直接 spawn），兼容非隔离场景
- **跨租户访问处理**：
  - Relay 主进程（`pulse` 用户）需要读写租户目录（投递消息、读取 session jsonl、扫描 outbox）→ 通过 ACL `u:pulse:rwx` + `m::rwx`
  - **Outbox** 位于租户 workspace 内 `{workspace}/.relay/outbox/`（见 FR-P0-019），与 workspace 同 owner；CC 以 tenant UID 读写；Relay 经 ACL 扫描投递
- **示例**：租户 member-0001 的 CC 进程以 `tenant-member-0001` 用户运行 → 执行 `cat /opt/PULSE/Tenants/member-0002/CLAUDE.md` → 内核返回 `Permission denied`

**FR-P0-015：租户开通自动创建 Linux 用户**
- **描述**：Platform 在 provisioning 时自动创建租户对应的 Linux 系统用户。用户名格式 `tenant-{tenantId}`（如 `tenant-member-0001`），home 目录指向租户 workspace（`/opt/PULSE/Tenants/{tenantId}/`）。
- **来源**：故事9
- **创建流程**（由 Platform `provision.ts` 执行）：
  1. `useradd -r -m -d /opt/PULSE/Tenants/{tenantId} -s /bin/bash tenant-{tenantId}`
  2. `chown -R tenant-{tenantId}:tenant-{tenantId} /opt/PULSE/Tenants/{tenantId}`
  3. `chmod 700 /opt/PULSE/Tenants/{tenantId}`
  4. 设置 ACL：`setfacl -m u:pulse:rwx /opt/PULSE/Tenants/{tenantId}`（允许 Relay 访问）
- **幂等性**：用户已存在时跳过创建，仅检查/修复目录权限
- **relay.json 同步**：agent 配置中新增 `"linuxUser": "tenant-{tenantId}"` 字段
- **示例**：Platform 开通 member-0002 → 创建 `tenant-member-0002` 用户 → relay.json 中 agent 配置 `"linuxUser": "tenant-member-0002"` → Relay spawn 时 `sudo -u tenant-member-0002`

**FR-P0-016：模型绑定 CC 模式自动选择**
- **描述**：`resolveCCMode()` 按模型目录选择 `print` 或 `interactive`。优先级（高→低）：`RELAY_CC_MODE` / `PoolOptions.ccMode` → `AgentConfig.ccMode` / `SpawnConfig.ccMode` → 模型目录中该 `ccModel` 所属 provider 的 `force_interactive: true` → 默认 `print`（`claude -p --input-format stream-json`）。
- **覆盖**：Agent 显式 `"ccMode": "print"` 或 `"ccMode": "interactive"` 覆盖目录判定
- **示例**：agent `ccModel` 所属 provider 声明 `force_interactive: true` 且无 ccMode 覆盖 → spawn interactive PTY bridge；agent `ccModel: "claude-sonnet-4-0"` → spawn print

**FR-P0-017：Interactive session jsonl 路径解析（workspace 优先 + `-root/` fallback）**
- **描述**：interactive 模式 jsonl watcher 与 accept 检测通过 `resolveInteractiveSessionFilePath()` 解析路径：**优先** workspace 编码目录 `$HOME/.claude/projects/{cwd-encoded}/{sessionId}.jsonl`（CC `--bare` 在 `projects['/root']` trust 登记后通常写此处，`entrypoint: cli`）；若磁盘上仅存在 legacy 路径则 fallback `$HOME/.claude/projects/-root/{sessionId}.jsonl`（print/sdk-cli 遗留会话）。spawn 时 watcher 初始化到 workspace 路径；poll 期间 `maybeRelocateInteractiveWatcherPath()` 若发现 jsonl 仅存在于 `-root/` 则自动 relocate watcher。`findExistingSessionFile()` 仍供 `--resume`/repair（先 workspace，再 `-root/`）。
- **来源**：bug-20260531 session path mismatch；2026-06-01 生产实测修正（/root trust 登记后 interactive 写 workspace 路径，非始终 `-root/`）
- **示例**：pulse 管理员 spawn interactive → watcher 初始化 `-opt-pulse-local-pulse-space/{uuid}.jsonl` → CC 写入同路径 → turn-accepted 正常；legacy 会话 jsonl 仅在 `-root/` 时 watcher 自动 relocate

**FR-P0-021：CC `--bare` interactive `/root` trust gate + claude.json 可写**
- **描述**：CC 2.x `--bare` interactive 启动 TUI 前检查 `~/.claude.json#projects['/root'].hasTrustDialogAccepted`（与真实 workspace cwd 无关）。未登记时 CC 弹出 "Quick safety check" /root trust dialog；PTY-bridge 无人应答则永不进入对话、不写 session jsonl。Relay 必须：
  1. interactive spawn 前调用 `ensureClaudeBareRootTrust(home)` 幂等登记 `projects['/root']`（`CLAUDE_BARE_REGISTRY_CWD = '/root'`），与 workspace trust（FR-P0-013）并列
  2. 所有写 `~/.claude.json` 的路径前调用 `ensureClaudeJsonWritable(home)` 清除 `chattr +i`（harden 脚本 `ensure_claude_json_writable` 部署时同样清理）
  3. 若 CC 仍弹出 /root trust TUI，PTY stdout sniffer 检测 `isRootTrustDialog()` 并 `tryAutoAcceptRootTrustDialog()` 发送 `\r` 确认默认选项
- **来源**：bug-20260531 interactive 无回复（workspace trust 已登记但 /root 未登记 + claude.json immutable）
- **示例**：member-0001 首次 interactive spawn → ensureClaudeBareRootTrust 写入 `projects['/root']` → CC 跳过 Quick safety check → jsonl 写入 workspace 编码路径

**FR-P0-018：租户平台隔离 — Phase A（文件系统权限加固）**
- **描述**：在 FR-P0-014 UID 隔离之上，对 **平台域** 与 **租户域** 做 DAC 加固，使 `tenant-*` 用户无法读取 `/opt/pulse/` 平台树、无法列目录侦察、无法 traverse 枚举其他租户名。Prompt 沙箱仅为辅助说明，**不以 prompt 作为安全边界**。
- **来源**：故事10
- **平台树**（`/opt/pulse/vultr-pulse-space/`、`local-pulse-space/`、`Bigbird/`、`data/`、`Doc/` 等）：`chmod -R o-rwx`（建议 `750 pulse:pulse`；敏感文件 `640`）
- **平台根**：`/opt/pulse` 建议 `711 pulse:pulse`（other 可 traverse 到已知 `Tenants/{id}/`，不可 `ls` 列根目录）
- **Tenants 父目录**：`/opt/pulse/Tenants` 设为 `711`（other 可 `cd Tenants/member-0001`，不可 `ls Tenants` 见 sibling 目录名）
- **租户 workspace**：维持 `700` + `chown tenant-*` + ACL `u:pulse:rwx` + `m::rwx`
- **实现入口**：`Scripts/harden-tenant-platform-isolation.sh`（幂等运维脚本，含 `ensure_claude_json_writable` 清除 pulse/租户 HOME 下 `~/.claude.json` 的 `chattr +i`）；Platform provision 对新租户仅修复 workspace ACL，平台树 chmod 为一次性/定期运维
- **示例**：`tenant-member-0001` 执行 `cat /opt/pulse/vultr-pulse-space/MEMORY.md` → Permission denied

**FR-P0-019：租户 workspace 内 Outbox（`.relay/outbox/`）**
- **描述**：文件投递 Outbox **迁入租户 workspace（HOME）内**，路径固定为 `{workspace}/.relay/outbox/`。租户 CC 自行读写自己的 Outbox；Relay 在 `finishTurn` 扫描并经 ACL 读取后投递、删除。**废弃**生产环境共享 `/tmp/relay-outbox/{tenantId}/` 方案（other 可读、可列目录，不符合隔离目标）。
- **来源**：故事10、Flow-005
- **路径规则**：
  - 有 `linuxUser`：`{resolveWorkspacePath(workspace)}/.relay/outbox/`
  - 无 `linuxUser`（PULSE 管理员等）：同上，相对于该 agent 的 workspace（如 `/opt/pulse/local-pulse-space/.relay/outbox/`）
- **权限**：目录随 workspace `chown`/`chmod 700`；`ensureOutboxDir` 在 spawn 前创建；`.relay/` 可同时承载 `prompts/` 等中转文件
- **Relay 行为**：`scanOutbox(tenantId)` 读 workspace 内 outbox；频道投递成功后 `rmSync` 单文件；`kill` 时 `cleanupOutbox` 可清空该目录
- **兼容**：`RELAY_OUTBOX_DIR` 仅保留 macOS 本地开发或非隔离调试 override；**生产租户不得依赖全局 outbox root**
- **示例**：member-0001 CC 将 PDF 写入 `/opt/pulse/Tenants/member-0001/.relay/outbox/report.pdf` → Relay emit `tool-files` → 飞书发送后删除

**FR-P0-022：Outbox 目录 ACL 继承（pulse 删除权）**
- **描述**：`ensureRelaySubdir()` 在创建 `{workspace}/.relay/` 及 `outbox/` 子目录时，除 `chmod(0o755)` 外必须调用 `setfacl -m u:pulse:rwx` 设置 ACL。文件删除需要父目录的写权限；outbox 文件由 tenant CC 创建（owner 为 `tenant-*`），Relay（`pulse` 用户）在投递后需删除这些文件。仅靠 `chmod 755` 不够——`pulse` 不是 owner 也不在 group 内，需 ACL 显式授予写权限。`setAclForPulse()` 为幂等操作，对已有 ACL 条目不重复添加；对不支持 ACL 的文件系统（如 macOS dev）静默跳过。
- **来源**：bug-20260602（pulse 无法删除 tenant 创建的 outbox 文件，`rmSync` 抛 EACCES）
- **示例**：member-0001 CC 写入 `outbox/report.pdf`（owner: tenant-member-0001）→ Relay 扫描投递 → `rmSync` 删除 → 成功（因 `u:pulse:rwx` ACL 授予 outbox/ 目录写权限）

**FR-P0-023：Session-in-use 检测增强 + crash restart 孤儿清理 + restartTracking resume 保护**
- **描述**：三个相互关联的修复，共同解决 session-id 冲突导致的无限崩溃循环：
  1. **Session-in-use stdout 检测**：CC 的 "Session ID already in use" 错误输出到 **stdout**（经 PTY bridge），而非 stderr。旧 `sessionInUse` 检查仅查 stderr，导致该错误被误判为普通崩溃。`sessionInUse` 判定同时检查 `lastStderr` 和 `lastStdoutSignal`（缓存 interactive CC 最近 stdout 信号文本，包括 429 和 session-in-use 错误）。interactive stdout 中 `"already in use"` 匹配即触发 session-in-use 处理
  2. **Crash restart 孤儿清理**：`handleCrashRestart` 在重试前先 `pkill -f 'claude.*--session-id {sessionId}'` 杀死占用同一 session-id 的孤儿进程，再 `killProcess(tenantId)` 清理已追踪的子进程。防止旧进程仍存活时新 spawn 因 session-id 冲突立即失败
  3. **restartTracking resume 保护**：`restartTracking.delete(config.tenantId)` 仅在 **fresh spawn**（`!config.resume`）时执行。crash restart 走 `resume: true`，若每次 spawn 都清计数器，则 `maxRestartsPerHour` 永远无法达到，进程无限循环。fresh spawn（notifyResumed 恢复 / 首次 spawn）需要清零以避免旧计数误判
- **来源**：bug-20260602（member-0001 18 分钟内崩溃 871 次，session-id 冲突 → 无限循环）
- **示例**：CC 进程 A 持有 session-id X → 崩溃 → handleCrashRestart pkill A → restartTracking 保留计数 → spawn B(resume: true, session-id X) → B 成功 → restartTracking.delete 仅在 fresh spawn 时清零

**FR-P0-024：Key Pool 粘性/跳过 + 429 interactive stdout 检测 + crash restart sticky 清除**
- **描述**：当 model catalog 配置了 provider `api_key_pool`（多 key 池化）时，429/rate-limit 触发 key 轮换，确保单 key 配额耗尽后自动 fallback 到池中下一个 key：
  1. **Sticky key**（`stickyKeyMap`）：`resolveApiKey()` 为每个 poolKey 建立粘性映射（poolKey → envVarName），同一 Agent 尽量使用同一 key（最大化 Anthropic prompt cache 命中率）
  2. **Skipped keys**（`skippedKeysMap`）：key 触发 429 后标记为 skipped（poolKey → Set\<envVarName\>），`resolveApiKey()` 跳过已 429'd 的 key。`finishTurn` 成功时清除 skipped（配额可能已恢复）
  3. **429 interactive stdout 检测**：CC 在 interactive 模式下将 429 错误显示在 PTY stdout（ANSI 文本），而非 stderr 或结构化 JSON。新增 regex `/429|rate.limit/i` 检测 interactive stdout preview，匹配后清除 sticky key + `rateLimitAbortInProgress.add()` + SIGKILL 进程 → exit handler 走 `handleRateLimitRestart` → 用下一个 key respawn
  4. **handleCrashRestart sticky 清除**：crash restart 时也清除 sticky key（crash 可能由 429'd key 引起但未被检测为 rate-limit，如 "Session ID already in use" 场景下 root cause 实为配额错误）
  5. **maxRateLimitRetriesPerMessage**：从 0 提升到 2，允许 `handleRateLimitRestart` 在 429 后最多重试 2 次（即最多尝试 3 个 key），而非立即 session-end
- **来源**：429 fallback 测试（2026-06-03）；interactive 429 仅出现在 stdout；handleCrashRestart 不清 sticky 导致无限重试用同一 429'd key
- **示例**：key pool = [ANTHROPIC_AUTH_TOKEN, ANTHROPIC_AUTH_TOKEN_FALLBACK] → ANTHROPIC_AUTH_TOKEN 429 → skippedKeysMap 标记 → resolveApiKey 跳过 → 选中 ANTHROPIC_AUTH_TOKEN_FALLBACK → spawn 成功 → finishTurn 清除 skippedKeysMap
- **描述**：当 `SpawnConfig.linuxUser` 存在时，CC 进程（含 `claude` / `claude_pty_bridge.py`）经 **bubblewrap** 包装 spawn，内核级 **路径 allowlist**：仅 bind 租户 workspace（含 `.relay/outbox/`）、受限 `/tmp`、以及运行所需的 `/usr`/`/lib*`（只读）。未 bind 的路径对 CC **不可见**。
- **来源**：故事10
- **Allowlist**：
  - `$WORKSPACE/**` — rw（含 `.relay/outbox/`、`.relay/prompts/`、session jsonl 所在 `$HOME/.claude/`）
  - 受限 `/tmp` — rw（`--tmpfs /tmp` 或 bind workspace 内 tmp）
  - `/usr`, `/lib`, `/lib64` — ro
- **Deny（默认）**：整个 `/opt/pulse` 平台树（除 workspace bind）、`/home/pulse`、其他租户 workspace
- **Env 最小化**：bubblewrap 内仅注入 CC 必需变量（`HOME`, `PATH`, `ANTHROPIC_*`, 模型别名等）；禁止透传 `RELAY_*`、bot token、平台 OAuth
- **资源**：spawn 前可选 `ulimit`（进程数/内存）防资源耗尽
- **兼容**：macOS / 无 `linuxUser` 跳过 bwrap；Linux 生产租户强制
- **示例**：租户 Bash `cat /opt/pulse/data/pricing.json` → 路径不存在或 Permission denied（命名空间内不可见）

### P1 功能（增强功能）

**FR-P1-001：优雅关闭（Graceful Shutdown）**
- **描述**：监听 Relay 主进程的 `SIGTERM`/`SIGINT` 信号，依次关闭所有 CC 子进程（SIGTERM -> 等待 -> SIGKILL），确保无孤儿进程。关闭前对所有活跃进程发出 `session-end(type: relay-shutdown)` 事件
- **来源**：故事6
- **示例**：`systemctl stop relay` -> Relay 收到 SIGTERM -> emitShutdown() -> 逐个关闭 CC 进程 -> 所有进程退出后 Relay 退出

**FR-P1-002：Session JSONL 修复**
- **描述**：在 resume 时自动修复 session jsonl 文件，截断未闭合的 tool_use 链（找到没有对应 tool_result 的 tool_use，回溯到触发它的 user message，截断之后的所有内容）
- **来源**：故事3
- **示例**：CC 在 tool 执行中间崩溃 -> resume 时修复 jsonl -> CC 不再卡在失败的 tool_use 上

**FR-P1-003：Turn 计数与预警**
- **描述**：跟踪每个进程的 turn 计数。当达到 maxTurns 的 80% 时发出 `turn-warning` 事件。当 exit code=0 且 turnCount >= maxTurns 时发出 `session-end(type: turns-exhausted)`
- **来源**：故事7
- **示例**：maxTurns=20，CC 已用 16 turn -> turn-warning(16, 20) -> CC 正常退出 -> session-end({type: 'turns-exhausted', turnCount: 20, maxTurns: 20})

### P2 功能（锦上添花）

**FR-P2-001：进程状态查询**
- **描述**：提供 `getStatus()` 方法，返回所有进程的当前状态（tenantId、state、pid、lastActivity、spawnedAt、restartCount）
- **来源**：故事7
- **示例**：调用 `pool.getStatus()` -> 返回 `[{tenantId:"member-000", state:"idle", pid:12345, ...}]`

**FR-P2-002：Interactive Accept 超时重试**
- **描述**：interactive 模式下，如果 prompt 未被 CC session 接受（超时），自动重启进程并重试。支持配置最大重试次数（`maxInteractiveAcceptRetriesPerMessage`）
- **来源**：故事3
- **示例**：interactive prompt 发出 -> 15s 内未被接受 -> 重启 + replay -> 最多重试 1 次

**FR-P2-003：消息队列**
- **描述**：当 CC 进程处于 busy 或 starting 状态时，新消息自动入队而非抛出错误。当进程回到 idle 状态时自动排空队列
- **来源**：故事1
- **示例**：CC busy 时收到第二条消息 -> 入队 -> CC 完成当前 turn -> 自动发送队列中的下一条

---

## 3. 验收标准（Acceptance Criteria）

### P0 功能验收标准

**AC-P0-001：进程 Spawn 成功**
- **条件**：调用 `pool.spawn(tenantConfig)` 且系统资源充足
- **预期结果**：CC 子进程在 <= 5 秒内启动并进入 ready 状态；stdin/stdout 流可用；进程 pid > 0
- **验证方式**：Unit Test（mock child_process）+ Integration Test（真实 CC 进程）

**AC-P0-002：双模式消息往返**
- **条件**：CC 进程处于 ready 状态，发送一条消息
- **预期结果**：
  - pipe 模式：<= 30 秒内收到至少 1 条 assistant 类型响应 + 1 条 result 类型响应
  - interactive 模式：prompt 被 CC 接受（turn-accepted 事件），jsonl 轮询到 assistant 输出
- **验证方式**：Integration Test

**AC-P0-003：崩溃自动重启 + Replay**
- **条件**：CC 进程非正常退出（exit code !== 0）
- **预期结果**：<= 10 秒内自动 respawn 新进程；若有 in-flight 消息则自动 replay；同一租户同一小时内重启次数 <= 3 次
- **验证方式**：Unit Test（模拟进程退出事件）

**AC-P0-004：空闲回收**
- **条件**：CC 进程最后活动时间距今 > 30 分钟
- **预期结果**：进程在下一个检查周期（<= 60 秒）内被 SIGTERM 关闭；触发 session-end(type: idle-reclaimed)
- **验证方式**：Unit Test

**AC-P0-005：Token Fallback**
- **条件**：CC stderr 输出包含 rate limit/429/401 信号，且配置了 fallback token
- **预期结果**：进程被终止，使用 fallback token respawn；token 切换不消耗 restart 配额
- **验证方式**：Unit Test

**AC-P0-006：Rate Limit Abort**
- **条件**：CC stdout 出现 429 内部重试信号
- **预期结果**：允许 90 秒恢复窗口；超时后 SIGKILL；走 crash restart + replay 路径
- **验证方式**：Unit Test

**AC-P0-007：Kill 操作**
- **条件**：调用 `pool.kill(tenantId)`，目标进程存在
- **预期结果**：进程在 <= 5 秒内终止（SIGTERM 优先，超时后 SIGKILL）；所有关联资源被清理
- **验证方式**：Unit Test

**AC-P0-008：Busy Progress 通知**
- **条件**：CC 进程处于 busy 状态，经过 `busyTimeoutMs`（默认 600s）
- **预期结果**：emit `busy-progress` 事件（含 `checkCount=1`、`maxChecks=10`、`isFinalCheck=false`）；进程**不被**终止；不 emit `session-end(timeout)`。第 10 次通知后（`checkCount >= maxChecks`）停止检查，进程继续运行
- **验证方式**：Unit Test（tests/pool.test.ts）

**AC-P0-009：Session-end 事件**
- **条件**：各种 session 结束场景
- **预期结果**：每种场景触发对应类型的 session-end 事件，上层可据此做差异化处理
- **验证方式**：Unit Test

**AC-P0-010：Onboarding 自动补全**
- **条件**：spawn 前 `~/.claude.json` 中 `hasCompletedOnboarding`/`theme` 等字段为 null 或缺失
- **预期结果**：spawn 之前文件被补全为合法 onboarding 状态，CC 启动后不出现 first-run wizard 输出（"Welcome to Claude Code … Choose the text style …" 字符串次数 = 0）；若字段已是有效值则文件 mtime 不变
- **验证方式**：Unit Test（tests/claude-onboarding.test.ts）+ Integration Test（PTY 复刻）

**AC-P0-011：`--bare` 模式 API_KEY 镜像**
- **条件**：spawn 时 `process.env` 仅含 `ANTHROPIC_AUTH_TOKEN`，不含 `ANTHROPIC_API_KEY`
- **预期结果**：CC 子进程 env 中 `ANTHROPIC_API_KEY === ANTHROPIC_AUTH_TOKEN`；主进程 env 不被修改；若用户已配置 `ANTHROPIC_API_KEY`，其值不被覆盖
- **验证方式**：Unit Test（tests/claude-onboarding.test.ts）

**AC-P0-012：thinking-only end_turn 延迟 finishTurn**
- **条件**：先后向 session jsonl 投递两条 assistant 记录，第一条 `content=[{type:'thinking',...}]` + `stop_reason=end_turn`，第二条 `content=[{type:'text', text:'final'}]` + `stop_reason=end_turn`
- **预期结果**：
  - 第一条投递后 pool 状态保持 `busy`，未 emit `'response'`
  - 第二条投递后 pool 状态变 `idle`，emit `'response'` 且包含 `final` 文本
  - 回归保护：thinking+text 同条 record 一次完结；tool-only 一次完结；空 content 一次完结
- **验证方式**：Unit Test（tests/pool.test.ts × 3 用例：defers thinking-only / combined thinking-text / empty content）

**AC-P0-013：CC `--bare` 模式 CLAUDE.md 显式加载（双前置）**
- **条件 A**（trust 登记）：spawn 前 `~/.claude.json#projects[<realpath_cwd>]` 不存在 / 存在但缺关键字段 / 存在且完整 / `hasTrustDialogAccepted: false` 明确拒绝
- **条件 B**（spawn args）：interactive 模式 spawn args 包含 `--add-dir <realpath_workspace>`
- **预期结果**：
  - trust 缺失：写入完整 entry，含 `hasTrustDialogAccepted: true` 等默认值
  - trust 部分：补缺失字段，已有值（如 `allowedTools: ['Bash']`）保持不变
  - trust 完整：mtime 不变、`changed=false`
  - trust 已显式拒绝：保持 `hasTrustDialogAccepted: false` 不覆盖
  - 异常容错：HOME/cwd 缺失或 JSON 损坏不抛错、不阻断 spawn
  - **大小写**：登记 key 必须与 CC 子进程 `getcwd()` 一致（即 realpath 解析后），否则 trust gate 仍 miss
  - **CLAUDE.md 注入**：`--add-dir <workspace>` 出现在 spawn args；session jsonl 第 1-3 条记录中应当包含 `{"type":"system",...}` 含 CLAUDE.md 关键内容
- **验证方式**：
  - Unit Test：`tests/claude-onboarding.test.ts` × 7 用例（trust 登记各分支）+ `tests/pool.test.ts` × 1 用例（interactive args 含 `--add-dir <workspace>`）
  - Integration（服务端复测）：member-0002 强制 new session 后 session jsonl 头部出现 system 消息、用户问「你是谁」时回复含「长征的pulse管理员」身份与「租户 member-0002」字样

**AC-P0-014：OS 级租户文件隔离（sudo -u spawn）**
- **条件 A**（spawn 行为）：`SpawnConfig.linuxUser` 为 `tenant-member-0001`，spawn CC 子进程
- **预期结果 A**：
  - interactive 模式 spawn 命令以 `sudo -u tenant-member-0001` 前缀执行
  - print 模式同理
  - CC 子进程的 effective UID 为 `tenant-member-0001`（`ps -o euid` 确认）
- **条件 B**（跨租户拒绝）：`tenant-member-0001` 的 CC 进程执行 `cat /opt/PULSE/Tenants/member-0002/CLAUDE.md`
- **预期结果 B**：内核返回 `Permission denied`，文件内容不可读
- **条件 C**（relay 访问）：`pulse` 用户执行 `cat /opt/PULSE/Tenants/member-0001/CLAUDE.md`
- **预期结果 C**：通过 ACL 权限成功读取
- **条件 D**（无 linuxUser 兼容）：`SpawnConfig.linuxUser` 为 `undefined`
- **预期结果 D**：spawn 行为与 v0.3.0 完全一致（直接 spawn，无 sudo 前缀）
- **验证方式**：Integration Test（真实 Linux 环境 + 多租户用户）

**AC-P0-015：租户开通自动创建 Linux 用户**
- **条件 A**（新租户）：Platform provisioning member-0003，系统中不存在 `tenant-member-0003` 用户
- **预期结果 A**：
  - 创建用户 `tenant-member-0003`（`id tenant-member-0003` 返回 UID）
  - home 目录为 `/opt/PULSE/Tenants/member-0003/`
  - 目录 owner 为 `tenant-member-0003:tenant-member-0003`，权限 `700`
  - ACL 条目 `user:pulse:rwx` 生效（`getfacl` 确认）
  - `relay.json` 中 agent 配置含 `"linuxUser": "tenant-member-0003"`
- **条件 B**（幂等性）：用户 `tenant-member-0003` 已存在，重复执行创建流程
- **预期结果 B**：跳过 useradd，仅检查/修复目录权限和 ACL；无错误抛出
- **条件 C**（目录已存在但权限不对）：目录存在但 owner 为 root 或权限为 755
- **预期结果 C**：chown + chmod + setfacl 修复到正确状态
- **验证方式**：Integration Test（Platform provision 端到端）

**AC-P0-016：模型绑定 CC 模式**
- **条件 A**（目录强制 interactive）：`ccModel` 所属 provider 声明 `force_interactive: true`，未设置 ccMode 覆盖
- **预期结果 A**：spawn 命令为 `python3 ... claude_pty_bridge.py ... --bare`；日志 `mode: 'interactive'`
- **条件 B**（默认 print）：`ccModel=claude-sonnet-4-0`，所属 provider 未声明 `force_interactive`
- **预期结果 B**：spawn 命令为 `claude -p --input-format stream-json ...`；日志 `mode: 'print'`
- **条件 C**（Agent 覆盖）：条件 A 的 agent 配置 `"ccMode": "print"`
- **预期结果 C**：强制 print，忽略目录判定
- **验证方式**：Unit Test（`tests/pool.test.ts`：force_interactive、claude print、agent ccMode 覆盖）

**AC-P0-017：Interactive jsonl watcher 路径解析（workspace 优先）**
- **条件**：interactive 模式 spawn 新 session（jsonl 尚未存在）
- **预期结果**：
  - watcher 初始化路径为 workspace 编码目录 `$HOME/.claude/projects/{cwd-encoded}/{sessionId}.jsonl`
  - CC 写入 workspace 路径后 poll 能检测到 user/assistant 记录；emit `turn-accepted` 与 assistant 响应
  - legacy 会话 jsonl 仅存在于 `-root/` 时，`maybeRelocateInteractiveWatcherPath()` 自动 relocate watcher
  - 不因 watcher 盯错路径而持续报「CC has not accepted prompt」
- **验证方式**：Unit Test（`tests/pool.test.ts`：`resolveInteractiveSessionFilePath`、watcher relocate 用例）+ Integration Test（bbpulse 2026-06-01）

**AC-P0-021：`/root` trust gate + claude.json 可写**
- **条件**：interactive 模式 spawn；`~/.claude.json` 可能被 `chattr +i`
- **预期结果**：
  - spawn 前 `projects['/root'].hasTrustDialogAccepted === true`
  - `ensureClaudeJsonWritable` 成功写入 trust 字段
  - 若 CC 仍弹出 /root Quick safety check TUI，PTY sniffer 自动 `\r` 确认；CC 进入对话并写入 session jsonl
- **验证方式**：Unit Test（`tests/claude-onboarding.test.ts`：`ensureClaudeBareRootTrust`）+ Integration Test（bbpulse feishu 往返）

**AC-P0-018：Phase A 平台权限加固**
- **条件**：已执行 `Scripts/harden-tenant-platform-isolation.sh`；以 `tenant-member-0001` 身份探测
- **预期结果**：
  - `cat /opt/pulse/vultr-pulse-space/MEMORY.md` → Permission denied
  - `cat /opt/pulse/CLAUDE.md` → Permission denied
  - `ls /opt/pulse/Tenants/` → Permission denied
  - `cat /home/pulse/.relay/relay.json` → Permission denied
  - `cat /opt/pulse/Tenants/member-0002/CLAUDE.md`（member-0002 存在且 700）→ Permission denied
  - `pwd` / 正常 Write 于 workspace → 允许
- **验证方式**：Integration Test（`Scripts/tenant-isolation-probe.sh` 或等价 probe）

**AC-P0-019：Workspace 内 Outbox**
- **条件**：租户 CC spawn 并完成一轮含文件投递的 turn
- **预期结果**：
  - `tenantOutboxDir()` 解析为 `{workspace}/.relay/outbox/`
  - CC 写入 outbox 的文件 owner 为 `tenant-*`；其他租户不可读
  - Relay（pulse）可 scan 并 emit `tool-files`；投递后文件删除
  - 不存在对 `/tmp/relay-outbox/` 的生产依赖
- **验证方式**：Unit Test（pool outbox 路径）+ Integration Test（飞书/TG 文件投递）

**AC-P0-022：Outbox 目录 ACL 继承**
- **条件**：租户 CC spawn 并写入文件到 `{workspace}/.relay/outbox/`；Relay 扫描投递后尝试删除
- **预期结果**：
  - `ensureRelaySubdir` 创建的 `.relay/` 和 `outbox/` 目录均含 `u:pulse:rwx` ACL 条目
  - `pulse` 用户可删除 tenant 创建的 outbox 文件（`rmSync` 不抛 EACCES）
  - 不支持 ACL 的文件系统（macOS dev）静默跳过，chmod 仍生效
- **验证方式**：Integration Test（Linux + ACL）+ Unit Test（setAclForPulse 调用）

**AC-P0-023：Session-in-use 检测增强 + crash restart 孤儿清理 + restartTracking resume 保护**
- **条件 A**（stdout 检测）：CC 进程因 session-id 冲突退出，"already in use" 出现在 stdout（经 PTY bridge）
- **预期结果 A**：`sessionInUse` 判定为 true；进程标记为 dead，不触发 crash restart
- **条件 B**（孤儿清理）：handleCrashRestart 被调用，存在使用同一 session-id 的孤儿 CC 进程
- **预期结果 B**：pkill 杀死孤儿进程后 spawn 新进程；新进程不因 session-id 冲突立即退出
- **条件 C**（resume 保护）：crash restart（`config.resume = true`）spawn 成功
- **预期结果 C**：`restartTracking` 计数不被清零；连续崩溃 `maxRestartsPerHour` 次后标记 `crash-unrecoverable`；fresh spawn（`config.resume = false`）时计数清零
- **验证方式**：Unit Test（sessionInUse stdout 检测、restartTracking resume 条件）+ Integration Test（bbpulse 崩溃恢复）

**AC-P0-020：Phase B bubblewrap 路径沙箱**
- **条件**：Linux + `linuxUser` 租户 spawn；bwrap 已安装
- **预期结果**：
  - spawn argv 含 `bwrap`（或等价 wrapper）；allowlist 仅 workspace + ro system lib
  - 租户 Bash 无法读 Phase A 中已 chmod 的平台路径，且 **在 bwrap 内路径不可见**（`ENOENT` 或 denied）
  - 正常对话、Read/Write/Bash 于 workspace、outbox 投递仍成功
- **验证方式**：Integration Test + pool spawn argv 单元测试

### P1 功能验收标准

**AC-P1-001：优雅关闭**
- **条件**：Relay 主进程收到 SIGTERM
- **预期结果**：所有活跃进程收到 session-end(relay-shutdown)；所有 CC 子进程在 <= 15 秒内全部终止；无孤儿进程残留
- **验证方式**：Integration Test

**AC-P1-002：Session JSONL 修复**
- **条件**：session jsonl 中存在未闭合的 tool_use 链
- **预期结果**：resume 时自动截断到触发 user message 处；CC 能正常继续
- **验证方式**：Unit Test

### P2 功能验收标准

**AC-P2-001：进程状态查询**
- **条件**：调用 `pool.getStatus()`
- **预期结果**：返回所有进程的摘要信息数组，包含 tenantId、state、pid、lastActivity、spawnedAt、restartCount
- **验证方式**：Unit Test

---

## 4. 非功能需求（Non-Functional Requirements）

### 性能需求

- **Spawn 延迟**：CC 子进程从 spawn 到 ready 状态 <= 5 秒
- **并发进程数**：支持同时管理 <= 12 个 CC 子进程（受 GCP e2-medium 4GB RAM 限制）
- **内存占用**：进程池管理器自身内存 <= 50MB（不含 CC 子进程）
- **Interactive 轮询间隔**：250ms（session jsonl 文件轮询）

### 安全需求

- **进程隔离**：每租户独立进程 + 独立工作目录，无共享状态
- **文件隔离**：每租户独立 Linux 用户 + `sudo -u` spawn + workspace `700`；Phase A 平台树 `o-rx`；Phase B bubblewrap 路径 allowlist（参见 FR-P0-018/020、CONSTRAINT-003）
- **工具白名单**：通过 `--allowed-tools` 限制租户可用工具
- **权限跳过**：pipe 模式使用 `--dangerously-skip-permissions`；interactive 模式使用 `--permission-mode bypassPermissions`
- **环境变量清理**：spawn 时自动剥离 `TELEGRAM_BOT_TOKEN` 和 `BOT_TOKEN_*`，防止泄露
- **Settings 注入**：自动从 `~/.claude/settings.json` 读取 `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN` 等环境变量（若缺失）
- **Settings 继承**：interactive 模式使用 `--setting-sources user,project,local` 确保用户/项目/本地配置生效

### 可用性需求

- **崩溃恢复**：进程崩溃后 <= 10 秒自动重启 + replay in-flight 消息
- **会话连续性**：重启后通过 `--resume <session-id>` 恢复对话上下文
- **Session 修复**：resume 时自动修复 session jsonl 中的损坏数据
- **Token 弹性**：primary token 失败时自动降级到 fallback token
- **Rate limit 弹性**：429 时允许恢复窗口 + 自动 restart + replay

### 可维护性需求

- **代码规范**：遵循 project-standards.md（TypeScript strict，kebab-case 文件名）
- **测试覆盖率**：核心模块 >= 80%
- **零外部依赖**：pool.ts 仅使用 Node.js 内置模块（child_process、fs、path、events）

---

## 5. 约束条件（Constraints）

### 技术约束

- **技术栈**：基于 project-architecture.md -- Node.js >= 18 + TypeScript 5.x
- **CC 通信协议**：
  - pipe 模式：`child_process.spawn` + `--input-format stream-json --output-format stream-json`（NDJSON 双向流）
  - interactive 模式：`python3 claude_pty_bridge.py` + `--bare` + PTY stdin + session jsonl 轮询
- **Interactive PTY Bridge**：`scripts/claude_pty_bridge.py`，用于将 stdin 转发到 CC PTY

### 后端兼容性约束（硬约束）

**CONSTRAINT-002：CC 2.x 启动前置条件（Onboarding + Auth + 项目级 Trust + `/root` Trust + 可写 claude.json）**

CC 2.x 在 `--bare` 模式下会**静默跳过**部分交互式 dialog，但启动逻辑仍以这些 dialog 的产物作为 gate；interactive TUI 仍可能弹出 `/root` trust 与 custom API key 提示。Relay 必须在每次 spawn 之前主动补齐前置：

- **用户级 Onboarding**：若 `~/.claude.json` 中 `hasCompletedOnboarding`/`theme` 等字段为 null 或缺失，会进入 first-run theme picker。PTY-bridge 模式下，Relay 写入的用户 prompt 会被 wizard 当作选项消化，CC 永不进入对话循环。Relay 调用 `ensureClaudeOnboardingComplete()` 幂等补齐（参见 FR-P0-010）。
- **claude.json 可写**：若 `~/.claude.json` 被 `chattr +i`，上述登记与 onboarding 写入均失败。Relay 调用 `ensureClaudeJsonWritable(home)` 在每次写前 best-effort 清除 immutable（参见 FR-P0-021）；harden 脚本部署时同步清理。
- **认证（interactive）**：API provider 走 `ANTHROPIC_AUTH_TOKEN`；**禁止**向子进程 env 注入 `ANTHROPIC_API_KEY`（会触发 blocking custom API key TUI）。Relay 调用 `mirrorAnthropicAuthTokenToApiKey()` strip `ANTHROPIC_API_KEY`（参见 FR-P0-011）。
- **项目级 Trust + CLAUDE.md 显式加载**（双前置，参见 FR-P0-013）：
  - **Workspace trust gate**：CC 2.x 在 cwd 进行 tool 读写 / 注入 settings 之前会检查 `~/.claude.json#projects[<realpath_cwd>].hasTrustDialogAccepted`。`--bare` 模式下 workspace trust dialog 被静默跳过。Relay 调用 `ensureClaudeProjectRegistered(home, realpath(cwd))` 幂等登记 entry。**关键**：必须用 `realpath` 解析 symlink。
  - **CLAUDE.md auto-discovery**：`--bare` 硬编码禁用 CLAUDE.md 自动加载。Relay 必须在 spawn args 中追加 `--add-dir <realpath(workspace)>`。
  - **实测信号**：仅做 trust 不加 `--add-dir` 时，身份仍是通用「Claude Code」。
- **`/root` trust gate（interactive 专用）**：CC `--bare` interactive 额外检查 `projects['/root']`（registry cwd 恒为 `/root`，与真实 chdir 无关）。未登记时弹出 Quick safety check TUI，不写 session jsonl。Relay 调用 `ensureClaudeBareRootTrust(home)` + PTY sniffer 兜底（参见 FR-P0-021）。

前置缺一不可：缺 onboarding → CC 卡 wizard；缺 claude.json 可写 → trust 登记失败；缺 AUTH_TOKEN → 401；缺 workspace trust+`--add-dir` → 身份失效；缺 `/root` trust → interactive 无 jsonl、无回复。

**CONSTRAINT-003：租户 CC 不得依赖 home 外可读路径（平台边界）**

- **目标**：租户 CC（含 Bash）不得通过读取 `/opt/pulse/` 平台树或其他租户 workspace 获取平台配置、定价、MEMORY、源码等信息；不得写平台文件。
- **执行分层**：
  1. **Phase A（必需）**：平台树 DAC `o-rx`；Tenants 父目录 `711`；Outbox 在 `{workspace}/.relay/outbox/`（FR-P0-019）
  2. **Phase B（Linux 生产租户必需）**：bubblewrap allowlist（FR-P0-020）
  3. **Prompt（辅助）**：`buildFileDeliverySystemPrompt` 声明允许路径；Phase B 落地后文案改为「已由系统强制执行」
- **Outbox 禁止**：生产环境不得使用共享 `/tmp/relay-outbox/`（global 755 与租户自管 Outbox 目标冲突）
- **PULSE 管理员**：无 `linuxUser` 的 agent 不适用 tenant bwrap，但仍建议使用 workspace 内 outbox

### 资源约束

- **运行环境**：GCP pulse-server（e2-medium, 4GB RAM）
- **测试环境**：本机开发测试（需 CC CLI 可用）

---

## 6. 排除范围（Out of Scope）

- 租户路由（chat_id -> tenant 映射）-- 属于 `telegram-channel` feature
- 消息渠道适配（Telegram/飞书）-- 属于 `telegram-channel` / 后续 feature
- 用量计量（usage 提取、计费）-- 属于 `metering` feature
- 多进程并发控制（信号量/队列上限）-- v0.2 多租户阶段考虑
- 进程健康检查 heartbeat -- v0.2 运维工具阶段考虑
- Context window hard-wrap 自动恢复（`recoveryRequiredAt` 标记已在 types.ts 定义，但 pool.ts 未实现自动恢复逻辑）
