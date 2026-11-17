---
feature_name: "heartbeat-policy-path-hardening"
version: "0.0.1"
created: "2026-04-21T09:30:00+0800"
modified: "2026-04-21T09:30:00+0800"
upstream_dependency:
  - "features/heartbeat-runtime/design.md"
  - "features/dispatcher-context-hygiene/requirements.md"
  - "Doc/amanda-space/memo/brief-2026-04-20-ken-path-write-hardening.md"
tags: [requirements, l2-spec, heartbeat, policy, hook, path-constraint]
co_owners: [ken]
changelog:
  - "v0.0.1 (2026-04-21): 初始版本。触发事件：Amanda 2026-04-20 Smoke v2 分身 commit ae41014c5 越界写 progress.md + CLAUDE.md（正式 ws 文件），revert b9e94e0d4。需求：skill 文字边界要 policy 硬约束兜底。"
---

# heartbeat-policy-path-hardening — 功能需求文档

> **功能概述**：让 `heartbeat-policy.yaml` 的 `autonomous_llm_config.path_constraints.write` 从"文字数据"变成"真执行约束"。dispatcher spawn cron 分身时，通过 CC **PreToolUse hook** 在 Write/Edit 工具调用前校验 file_path 是否在白名单；不符 → deny + stderr 特定前缀 → 复用现有 `_detect_tool_denied` 记 `autonomous_execution_event.tool_denied_calls`。人工 session 不受影响（不经 dispatcher）。现有 policy schema 零变更。
>
> **关联**：
> - 触发需求：`Doc/amanda-space/memo/brief-2026-04-20-ken-path-write-hardening.md`（Amanda brief）
> - Amanda wi：`wi_20260420_072` / ho_20260420_656（accepted）
> - 上游：`heartbeat-runtime` dispatcher 模块
> - 互补 feature：`dispatcher-context-hygiene`（前置 skip；本 feature 是运行期硬拒）
> - L2 设计：`design.md`
> - L2 任务：`tasks.md`

---

## 0. 问题、定位与概念模型

### 问题（Amanda 2026-04-20 Smoke v2 暴露）

Smoke v2 真实 dispatch spawn 分身，分身处理 ho 时按 HEARTBEAT §C 收口规则改了：
- `Doc/amanda-space/memo/progress.md`（重写）
- `Doc/amanda-space/CLAUDE.md`（活摘要加条目）

两者都在 `harness-intake` skill §分身边界列为"**不得修改**"，但：
- skill 约束只是**文字描述**
- 当前 `amanda.yaml` 的 `path_constraints.write: ["Doc/amanda-space/**"]` 太宽
- **技术层无执行**——dispatcher 虽读 `path_constraints`，只传 `--allowedTools` 工具列表（不带路径）

分身看到"HB §C 要求写 progress + 更新活摘要"+"skill 边界说不要"两条冲突指引时，选了 ws 家规——**LLM 判断行为，不是工程约束**。

### 根因

**同一 HB 收口规则在人工档/cron 档行为应不同**：
- 人工档：自由写任何 ws 文件（session 老板是 Agent 自己）
- cron 档：只写 memo/（session 老板是 Ken/dispatcher，边界由 policy 硬定）

现有 `heartbeat-policy` 没区分档位的 path.write 硬执行，是结构缺口。

### 定位

本 feature 是 **运行期**硬约束，与 dispatcher-context-hygiene 的**前置**拦截互补：

| feature | 时机 | 管什么 |
|---------|------|-------|
| dispatcher-context-hygiene | spawn 前 | 要不要 spawn 这个 ho |
| heartbeat-policy-path-hardening | spawn 后运行期 | 分身能写哪些路径 |

两者一起构成 **启 cron 前的硬约束完整集**（T8 暂缓，短期内不启 cron——本 feature 不是"启 cron 必需"而是"启 cron 品质保证"）。

### 核心设计决策（L1 范围）

**零 schema 变更**：不新增 policy 字段。现有 `autonomous_llm_config.path_constraints.write` 从"文字数据"变"真执行"。

**平滑升级语义**（Amanda AC #3）：
- 现有 amanda.yaml `write: ["Doc/amanda-space/**"]` 保持原值 = 全 ws 可写 = 行为不变
- Amanda 显式改窄为 `write: ["Doc/amanda-space/memo/**"]` = 硬约束生效

用户可选择何时收紧。不强制立刻改。

### 范围与边界

**in-scope (P0)**：
- PreToolUse hook 脚本（Python，harness 包内）
- dispatcher spawn 时注入 `--settings <tmp>` + hook 配置
- path_constraints.write 白名单执行（Write/Edit 工具）
- stderr deny 前缀复用 `_detect_tool_denied`
- 三 Agent 新配置样例（Amanda / Reeve / Aki，仅示例 yaml，不改他们的 live policy）
- Smoke 自验（tmp workspace 越界 → denied）

**out-of-scope (P0 不做，后续再议)**：
- read 白名单硬约束（Amanda 需求只含 write）
- 非 Write/Edit 工具的路径约束（如 Bash 的 cd、MultiEdit 的 file_path—— MultiEdit 可顺手纳入，其他 P1）
- 人工 session 路径约束（不归本 feature；人工 session 不经 dispatcher）
- 动态 hook 热重载（policy 改变后 spawn 下次生效即可）

---

## 1. 用户故事

### Story-001 — Amanda 配置收紧后越界 Write 被拒

**As** Amanda（已把 `path_constraints.write` 改为 `["Doc/amanda-space/memo/**"]`），
**When** cron 分身被 spawn 后尝试 `Write(file_path="Doc/amanda-space/IDENTITY.md")`，
**Then** CC PreToolUse hook 返回 `permissionDecision=deny`，Write 不执行，stderr 含 `HARNESS_PATH_DENIED:...` 前缀，dispatcher 捕获为 `tool_denied` 事件，`tool_denied_calls` 记录 "Write"，升级事件 push 给 Amanda。

### Story-002 — 未改配置的 Agent 行为不变（平滑升级）

**As** 一个 Agent（policy 未显式改，path.write 仍宽），
**When** cron 分身 Write 任何当前允许范围内文件，
**Then** 行为与本 feature 上线前完全一致，无额外 deny。

### Story-003 — 人工 session 不受影响

**As** Agent 人工开 session（不经 dispatcher，直接启动 claude），
**When** Write 任何 ws 文件，
**Then** 不受 path.write 硬约束影响（settings 不含本 hook；老板是自己）。

### Story-004 — 新上线 Agent 拿到硬约束样例

**As** Reeve/Aki（未启 cron 前准备 policy 时），
**When** 看 heartbeat-policy 模板，
**Then** 能直接复制含 `path_constraints.write: ["<ws>/memo/**"]` 的硬约束版本，不用自己摸索语法。

---

## 2. 功能需求（FR）

### FR-P0-001 — PreToolUse hook 脚本

harness 包内 `harness/hooks/path_guard.py`：
- stdin 读 CC 传的 JSON（含 `tool_name` + `tool_input.file_path`）
- 读 env var `HARNESS_POLICY_PATH`（policy.yaml 绝对路径）
- 从 policy load `autonomous_llm_config.path_constraints.write` 白名单
- `fnmatch`/glob 匹配 `tool_input.file_path`（支持 `**`）
- 匹配成功 → exit 0 空 stdout（allow）
- 匹配失败 → exit 0 + stdout JSON `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}` + stderr 含 `HARNESS_PATH_DENIED:<path>`
- 适用工具：Write / Edit / NotebookEdit（写类工具）；其他 tool_name 直接 allow（本 feature 不管）

**失败容忍**：
- policy 文件缺失 / 解析失败 → exit 0 allow + stderr warning（降格，不阻塞分身）
- file_path 字段不存在 → allow（本 hook 只管有 file_path 的写类工具）

### FR-P0-002 — dispatcher 注入 hook settings

`harness/dispatcher.py::spawn_autonomous_session` 扩展：
- spawn 前 `tempfile.NamedTemporaryFile(suffix=".json")` 生成 tmp settings.json
- 内容：PreToolUse hook 配置 + `env.HARNESS_POLICY_PATH` 指向当前 policy
- `claude -p ... --settings <tmp> ...`（已有 --allowedTools 等参数保留）
- subprocess 结束后 `os.unlink(tmp)` 或 `NamedTemporaryFile` 自动清理

### FR-P0-003 — stderr deny 检测扩展

`harness/dispatcher.py::_TOOL_DENIED_PATTERNS` 新增 pattern：
- `HARNESS_PATH_DENIED:\s*([^\s]+)`（捕获被拒路径）
- 捕获到即记 `tool_denied` 对应 Write/Edit 等工具名

### FR-P0-004 — 三 Agent 新配置样例（文档）

`harness/templates/policies/` 新增（或更新现有 policy 模板）：
- `amanda-hardened.yaml`：`path_constraints.write: ["Doc/amanda-space/memo/**"]`
- `reeve-hardened.yaml`：`path_constraints.write: ["PARA/reeve-space/memo/**"]`（路径按 Reeve workspace 根）
- `aki-hardened.yaml`：`path_constraints.write: ["PARA/aki-space/memo/**"]`

**不**改 live `.harness/policies/*.yaml`（Amanda 后续自己改）。

### FR-P0-005 — Hook 脚本自发现路径

dispatcher 注入 hook 时用 `harness/hooks/path_guard.py` 的**绝对路径**（通过 `importlib.resources.files("harness").joinpath("hooks/path_guard.py")` 或等效）。不硬编码路径。

---

## 3. 非功能需求（NFR）

### NFR-001 — 性能

- hook 脚本 cold start ≤ 200ms（Python 启动 + yaml 读）
- 单次 dispatch 中每个 Write/Edit 调用增加 < 300ms 延迟
- 不读网络；不写入 workspace 任何文件

### NFR-002 — 失败容忍

- hook 任何失败路径（policy 缺失、解析错、env 未设）→ 默认 allow（降格）+ stderr warning
- tmp settings 创建失败 → dispatcher 降格为原 spawn 路径（无 hook，与现行为一致）
- 理由：hook 故障不应阻塞 cron 分身工作；优先保活，次优保严格

### NFR-003 — 零 schema 变更

`HeartbeatPolicy` 不加字段。现有 `path_constraints.write` 语义升级，不破坏兼容。

### NFR-004 — 不污染 Agent workspace

- 不改 Agent 的 `.claude/settings.json`
- 不在 workspace 写任何文件（hook 脚本住 harness 包）
- tmp settings 住 `tempfile.gettempdir()`

### NFR-005 — 测试覆盖

- hook 脚本单元测试 ≥ 15 条
- dispatcher 注入逻辑单元测试 ≥ 5 条
- 集成测试（真 tmp workspace + mock CC 环境）验 deny 端到端 ≥ 2 条
- harness 包整体覆盖率保持 100%

---

## 4. 接受标准（AC）

### AC-P0-001 — hook 脚本基础行为（单元）

- tool_name="Write" + file_path 在白名单 → exit 0 空 stdout
- tool_name="Write" + file_path 不在白名单 → exit 0 + stdout JSON deny + stderr `HARNESS_PATH_DENIED:<path>`
- tool_name="Edit" 同上
- tool_name="Read" → allow（本 hook 不管非写类工具）
- file_path 字段缺失 → allow

### AC-P0-002 — hook 失败容忍（单元）

- `HARNESS_POLICY_PATH` 未设 → allow + stderr warning
- policy 文件不存在 → allow + stderr warning
- policy yaml 损坏 → allow + stderr warning
- write 字段缺失 → allow（视作无白名单 = 全开）

### AC-P0-003 — dispatcher 注入（单元）

- spawn 时生成 tmp settings.json，内容含 PreToolUse + path_guard.py 命令 + HARNESS_POLICY_PATH env
- claude 命令含 `--settings <tmp_path>` 参数
- subprocess 结束后 tmp 文件被清理

### AC-P0-004 — stderr 检测扩展（单元）

- `_detect_tool_denied("HARNESS_PATH_DENIED:/path/to/file")` 返回包含 "Write"（或 file_path 表示）的列表
- 和现有 "tool X not allowed" pattern 共存，两种 deny 都能被捕获

### AC-P0-005 — 平滑升级（集成）

- policy `path_constraints.write: ["Doc/amanda-space/**"]`（老配置）→ 分身 Write `Doc/amanda-space/IDENTITY.md` **允许**（行为不变）
- policy `path_constraints.write: ["Doc/amanda-space/memo/**"]`（新配置）→ 同上 Write → **denied**
- 同一 hook 脚本 + 同一 dispatcher，只换 policy 值

### AC-P0-006 — Smoke 越界拒绝（Ken 亲验，tmp workspace）

tmp workspace 真 spawn 分身（或高仿真 mock），分身被 prompt 引诱 Write `<ws>/IDENTITY.md`：
- stderr 含 `HARNESS_PATH_DENIED:`
- AutonomousExecutionEvent.status = `tool_denied`
- AutonomousExecutionEvent.tool_denied_calls 非空
- escalation event 被触发（match_label=`autonomous_tool_denied`）

### AC-P0-007 — 三 Agent 样例文档

`harness/templates/policies/` 下有 amanda-hardened / reeve-hardened / aki-hardened 三份 yaml，可直接 `harness policy validate` 通过。

### AC-P0-008 — 回归

- 现有 853 tests 全过
- harness 包覆盖率保持 100%
- dispatcher-context-hygiene 行为不变（两 feature 正交）

---

## 5. 依赖与约束

### 上游依赖

- `heartbeat-runtime` L2 已交付（dispatcher + policy + events）
- `dispatcher-context-hygiene` L3 已交付（本 session 早些时候）
- CC v2.x PreToolUse hook 接口（已通过 claude-code-guide 查实：`hookSpecificOutput.permissionDecision="deny"` + exit 0）
- CC CLI `--settings <file>` 参数（merge 机制，不污染原 settings）

### 技术约束

- **Python stdlib only**（hook 脚本不加 pip 依赖）
- **不改 HandoffContract / DispatchEvent / HeartbeatPolicy schema**
- **不改 Agent live policy yaml**（仅提供模板）

### 红线

- 不触碰 Agent workspace 下任何文件（含 .claude/settings.json）
- 不启 cron（T8 暂缓）
- 不派 handoff 给其他 Agent

---

## 6. 风险与对策

| 风险 | 概率 | 对策 |
|------|------|------|
| CC 新版本 hook JSON 格式微调 | 低 | hook 脚本用 `try/except` 宽松解析 stdin，不强依赖字段名 |
| glob `**` 语义 Python fnmatch 不支持 | 高 | 用 `pathlib.PurePath.match` 或 `wcmatch.glob`（stdlib 的 fnmatch 只支持单层 *）——改用 `fnmatch.translate` + regex 或手写 glob matcher |
| hook 启动慢（Python cold start）| 中 | 保持 hook 脚本依赖精简；必要时 NFR-001 放宽到 500ms |
| tmp settings 跟 Agent 原 settings 冲突 | 低 | `--settings` merge 机制，我们的优先；即使冲突 hook 也会执行 |
| amanda.yaml 现 path.write 是相对路径，hook 拿到的 file_path 是绝对路径 | 高 | hook 里 resolve file_path 和白名单到绝对路径后再 match；需要知道 `<ws>` 锚点——从 policy.workspace 拿 |

**风险 5 是关键实现点**，design 要明确。

---

## 7. 未来扩展（不做）

- read 白名单硬约束
- Bash 工具的路径相关子约束（cd / mv / rm 的 target 路径）
- hook 日志独立落盘（当前只 stderr）
- 动态 policy 热重载
- per-Agent hook 定制（当前所有 Agent 共用同一 hook 逻辑）
