---
feature_name: "agent-self-authored-policy"
version: "0.0.1"
created: "2026-04-20T10:05:00+0800"
modified: "2026-04-20T10:05:00+0800"
upstream_dependency:
  - "project-specs/project-vision.md"
  - "project-specs/project-architecture.md"
  - "project-specs/project-standards.md"
  - "project-specs/project-roadmap.md"
  - "features/heartbeat-runtime/requirements.md"
  - "features/heartbeat-runtime/design.md"
tags: [requirements, l2-spec, policy, cli, governance, ownership-boundary]
co_owners: [ken]
changelog:
  - "v0.0.1 (2026-04-20): 初始版本。因越权修正（commit 193d75091 → bdd14b9c7）暴露 policy.yaml 所有权歧义；本 feature 用 CLI 作为接口，把 policy 内容层所有权从 Ken 迁到 Agent，Ken 仅保留 schema / 执行契约层。"
---

# agent-self-authored-policy - 功能需求文档

> **功能概述**：把 `.harness/policies/<agent>.yaml` 的内容层所有权从"Ken 起草"迁移为"Agent 自撰"。通过 `harness policy write` CLI 把**写入动作**作为唯一合法接口；Ken 保留 schema 定义、dispatcher 执行契约、事件留痕，不再审查内容。
>
> **关联文档**：
> - L1 项目愿景：`project-specs/project-vision.md`（harness = Agent 团队 OS，平台而非代笔者）
> - L1 项目架构：`project-specs/project-architecture.md`（Python 3.11 + Click + Pydantic v2 + PyYAML 技术栈 / 三域 CLI 入口）
> - L1 项目规范：`project-specs/project-standards.md`（100% 测试覆盖率）
> - 前置 feature：`features/heartbeat-runtime/`（policy.yaml schema 和 dispatcher 已落地）
> - L2 技术设计：`features/agent-self-authored-policy/design.md`（后续产出）
> - L2 任务拆分：`features/agent-self-authored-policy/tasks.md`（后续产出）

---

## 0. 问题、定位与概念模型

### 问题（2026-04-19 越权事件揭示）

heartbeat-runtime 上线时，Ken 按默认"谁管 `.harness/` 谁起草"思路为 Amanda/Reeve 起草了 `.harness/policies/*.yaml`。2026-04-19 大鸟先后两次裁定：

1. Ken 派 sub-agent 改 Agent workspace 的 HEARTBEAT/CLAUDE → 越权，revert（commit 193d75091）
2. Ken 过度回退把 `.harness/policies/*.yaml` 也一起删了 → 再纠正，policy 属 agent-harness runtime 配置归 Ken（commit bdd14b9c7 restore）

修正后 Ken 起草了 amanda.yaml/reeve.yaml 并派 handoff 请两人协商。Amanda 反向 handoff（ho_20260419_264）给出 10 条意见——其中多条涉及"哪档开 autonomous_llm / 给哪些工具 / max_duration 多少"——**本质上是运行参数的内容决策，不该由 Ken 代笔**。

### 定位：policy 所有权再分层

本 feature 把 policy 拆成**两层**：

| 层 | 归属 | 内容 | 本 feature 影响 |
|----|------|------|-----------------|
| **Schema / 执行契约层** | Ken（agent-harness） | `PolicySchema` Pydantic 模型 / dispatcher 如何读 / allowed_tools 如何下发给 CC / push 如何触发 | 保留，无修改 |
| **内容层** | **Agent（本 feature 迁移）** | 各档 autonomy 取值 / allowed_tools 列表 / path_constraints / max_duration / push 列表 | 从"Ken 代笔 + Agent 协商" → "Agent 自撰 + CLI 投递" |

### 与 heartbeat-runtime 的关系

heartbeat-runtime 定义了 policy.yaml 字段语义和 dispatcher 消费逻辑；本 feature 只加**写入接口**。physical 位置 `.harness/policies/<agent>.yaml` 不变，现有 `policy validate` / dispatcher / scan / event 代码零改动。

### 三角所有权清晰化

```
┌────────────────────────┐   提供 schema + write CLI
│  Ken (agent-harness)   │ ───────────────────────────┐
└────────────────────────┘                            │
          │ 不审批内容 / 只保障契约                     ▼
          │                               ┌──────────────────────┐
          │                               │  .harness/policies/  │
          │          dispatcher 读        │    <agent>.yaml      │
          │ ◄──────────────────────────── │                      │
          │                               └──────────┬───────────┘
                                                     │
                                                     │ Agent 通过 CLI 写入
                                                     │
                                  ┌──────────────────┴────────────┐
                                  │  Agent (amanda / reeve / aki) │
                                  └───────────────────────────────┘
```

---

## 1. 用户故事（User Stories）

### 主要用户故事

**故事 1：Agent 自撰 policy 并投递**
- **作为**：Amanda / Reeve / Aki（三档 autonomy 的实际业主）
- **我希望**：在自己 workspace 编辑 policy 草稿文件 → 调 `harness policy write <agent> --from <path>` 投递 → 得到 schema 校验结果（通过 / 具体失败字段）→ 通过则自动写入 `.harness/policies/<agent>.yaml`
- **以便**：自己决定 autonomy 档位、工具列表、频率、push 规则，不再依赖 Ken 代笔或协商
- **优先级**：P0

**故事 2：Ken 保障执行契约稳定**
- **作为**：Ken（agent-harness 平台维护者）
- **我希望**：提供 schema + 写入 CLI + 事件留痕，不用读/审/改 Agent 写的内容
- **以便**：Ken 职责收敛到 "schema + runtime 契约 + 可观测性"，不越界介入 Agent 内政
- **优先级**：P0

**故事 3：审计者追溯 policy 变更**
- **作为**：Reeve（审计者）/ 大鸟（事后追责）
- **我希望**：每次 `policy write` 成功都留一条结构化事件（谁 / 何时 / 写的哪个 agent / diff 摘要）
- **以便**：policy 内容变化有追溯链，Reeve 日审可把"policy 突变"与"后续行为变化"对齐
- **优先级**：P0

**故事 4：Agent 先看后写**
- **作为**：Agent（尤其首次接入）
- **我希望**：`harness policy show <agent>` 看当前落地版本；`harness policy diff <agent> --from <path>` 看我的草稿与现状差异
- **以便**：改之前先对齐，避免盲改
- **优先级**：P1

**故事 5：Agent 草稿反复试错**
- **作为**：Agent
- **我希望**：对草稿路径调 `harness policy validate --from <path>` 预校验（不写盘）
- **以便**：迭代草稿时无需每次写文件再调 validate（当前 `policy validate <agent>` 只支持读已落地文件）
- **优先级**：P1

---

## 2. 功能需求（Functional Requirements）

### P0 功能（MVP 必需）

**FR-P0-001：`policy write` 命令**
- **描述**：`harness policy write <agent> --from <path> --as-agent <caller>` 从 `<path>` 读 YAML → 反序列化为 `PolicySchema` → 校验成功则写入 `.harness/policies/<agent>.yaml`；校验失败非零退出 + stderr 指向出错字段；文件原子替换（不出现半写状态）
- **来源**：故事 1、故事 2
- **示例**：Amanda 编辑 `Doc/amanda-space/drafts/heartbeat-policy.yaml` → `harness policy write amanda --from Doc/amanda-space/drafts/heartbeat-policy.yaml --as-agent amanda` → 校验通过 → `.harness/policies/amanda.yaml` 被替换 → stdout 打印 "written to .harness/policies/amanda.yaml (2.1KB)"

**FR-P0-002：身份声明（Accountability，非 Authorization）**
- **描述**：`--as-agent <caller>` 为必填参数，声明"谁发起了这次写入"。**不做密码学验证**（CLI 层面无从验证，留给 Reeve 事后审计 + Git commit author）；`<caller>` 与 `<agent>` 可以不一致（例如 Ken 首次为 Aki 建立基础 policy，`--as-agent ken`），事件中两者都会留痕
- **来源**：故事 2、故事 3
- **示例**：`harness policy write aki --from /tmp/aki-draft.yaml --as-agent ken` → 事件 `caller=ken target=aki`，便于 Reeve 后期识别"Ken 为非自身 agent 写 policy"

**FR-P0-003：policy_change_event 留痕**
- **描述**：每次 `write` 成功后追加一条 JSONL 事件到 `.harness/events/policy_change_event-YYYY-MM.jsonl`；字段含 `timestamp / target_agent / caller / source_path / bytes_written / change_type（created|updated）/ previous_sha256 / new_sha256`
- **来源**：故事 3
- **示例**：见 design.md 事件 schema

**FR-P0-004：`policy show` 命令**
- **描述**：`harness policy show <agent>` 读 `.harness/policies/<agent>.yaml` → YAML 原样输出到 stdout；文件不存在则非零退出 + stderr 明示
- **来源**：故事 4
- **示例**：`harness policy show amanda` → stdout 输出当前 amanda.yaml 内容

**FR-P0-005：schema 失败原子拒绝**
- **描述**：`write` 的 schema 校验失败时，`.harness/policies/<agent>.yaml` **字节不变**（包括 mtime 保持原值）；stderr 输出 Pydantic 验证错误（字段路径 + 具体原因）；非零退出码（约定 `2`）
- **来源**：故事 1、故事 2
- **示例**：草稿里 `frequency_hint` 写成 `not-a-cron` → stderr `frequency_hint: Value must be a valid cron expression`，exit=2，现有 amanda.yaml 内容不变

### P1 功能（增强功能）

**FR-P1-001：`policy validate --from <path>` 草稿预校验**
- **描述**：扩展现有 `policy validate` 命令增 `--from <path>` 参数；带此参数时校验草稿文件而非已落地文件，不写盘
- **来源**：故事 5

**FR-P1-002：`policy diff` 草稿 vs 现状**
- **描述**：`harness policy diff <agent> --from <path>` 以 YAML / unified diff 输出两者差异；纯比较不写盘；任一不存在则给明确提示
- **来源**：故事 4

### P2 功能（暂不做）

- **不做** 基于密码学的身份鉴权（超出平台范围，走 Git commit + 事件留痕 + Reeve 抽查即可）
- **不做** 回滚命令（Git 提供 `git checkout <prev-commit> -- .harness/policies/<agent>.yaml`）
- **不做** policy 文件迁移到 agent workspace（物理位置稳定，CLI 是所有权边界而非路径）
- **不做** schema 字段扩展（Amanda memo 里 #5 per-label duration / #6 Write 路径审计 / #9 session lock CLI 归独立 feature）

---

## 3. 验收标准（Acceptance Criteria）

所有标准均可量化验证（自动化测试 / 命令行复现）。

### P0 验收标准

**AC-P0-001：write 合法路径写入成功**
- **标准**：对 fixture 中一个 schema 合法的 policy YAML 执行 `harness policy write test-agent --from <path> --as-agent test-agent` → exit=0；`.harness/policies/test-agent.yaml` 存在且内容与 source 做 YAML 语义等价（`yaml.safe_load` 后 Python dict 相等）；stdout 含 "written" 关键词
- **验证方式**：pytest 自动化（tmp_path fixture）

**AC-P0-002：write schema 失败拒绝 + 原文件零改动**
- **标准**：对已落地的 amanda.yaml，事先记下其 `stat().st_mtime_ns` 与 SHA-256；对一个 schema 非法的草稿执行 `harness policy write amanda --from <非法草稿> --as-agent amanda` → exit=2；现有 amanda.yaml 的 mtime_ns 与 SHA-256 **完全不变**；stderr 含 Pydantic 验证错误（至少一个字段名）
- **验证方式**：pytest 自动化

**AC-P0-003：policy_change_event 事件字段完整**
- **标准**：每次 `write` 成功后，`.harness/events/policy_change_event-YYYY-MM.jsonl` 追加一行；解析该行 JSON，包含且仅包含字段 `{timestamp, target_agent, caller, source_path, bytes_written, change_type, previous_sha256, new_sha256}`（previous_sha256 在 `change_type=created` 时为 null）；timestamp ISO8601 带 +0800
- **验证方式**：pytest 自动化

**AC-P0-004：policy show 输出等价于源文件**
- **标准**：`harness policy show <agent>` 的 stdout 解析为 YAML（`yaml.safe_load`）与 `.harness/policies/<agent>.yaml` 解析结果 dict 相等
- **验证方式**：pytest 自动化

**AC-P0-005：write 性能**
- **标准**：单次 `harness policy write` 端到端耗时 <500ms（wall clock）；测试 fixture 为典型 ~2KB policy
- **验证方式**：pytest 自动化 + `time.perf_counter` 计时

**AC-P0-006：现有 dispatcher/validate 代码零改动**
- **标准**：`git diff HEAD~N -- harness/policy.py harness/dispatcher.py harness/scan.py`（N 为本 feature 合入点）对**现有函数签名和逻辑**无变更；仅允许在 cli.py / 新增模块中加新代码
- **验证方式**：人工审 diff + 现有 711 tests 全通过（regression）

**AC-P0-007：端到端串联（write → dispatch）**
- **标准**：写入一个合法 amanda policy（mechanical 档）→ 立刻 `harness dispatch amanda --dry-run`（或真 dispatch 空命中）→ exit=0，dispatcher 按新 policy 运行
- **验证方式**：integration test

**AC-P0-008：caller ≠ target 被事件捕获**
- **标准**：`harness policy write aki --from <...> --as-agent ken` 成功后，事件中 `caller=ken target_agent=aki`，两字段都存在
- **验证方式**：pytest 自动化

### P1 验收标准

**AC-P1-001：validate --from 不写盘**
- **标准**：`harness policy validate test-agent --from <path>` 前后 `.harness/policies/test-agent.yaml` 不存在（或保持原内容），exit 按校验结果（0 or 2）
- **验证方式**：pytest 自动化

**AC-P1-002：diff 输出格式**
- **标准**：`harness policy diff amanda --from <path>` stdout 为合法 unified diff（`---` / `+++` / `@@` 行存在）；相同内容时 stdout 为空 + exit=0
- **验证方式**：pytest 自动化

---

## 4. 非功能需求（Non-Functional Requirements）

### 性能
- **NFR-PERF-001**：`policy write` 端到端 <500ms（已在 AC-P0-005 量化）
- **NFR-PERF-002**：`policy show` <200ms（YAML 解析 + 打印）
- **NFR-PERF-003**：事件 JSONL 追加 <50ms（avoid holding lock long）

### 可靠性
- **NFR-REL-001**：`policy write` 写入为**原子替换**——先写临时文件 `<target>.yaml.tmp`，fsync 后 `os.replace` 到目标路径；异常退出不产生半写文件
- **NFR-REL-002**：事件 JSONL 追加在文件写入**成功后**执行；事件失败不回滚文件写入（事件属于可恢复损失——Git log 也是二次证据）

### 安全 / 边界
- **NFR-SEC-001**：不做密码学鉴权；accountability 靠 `--as-agent` + 事件 + Git commit 三层留痕
- **NFR-SEC-002**：`--from <path>` 必须是规范化后的仓库内路径；禁止以 `/` 开头的绝对路径或 `..` 逃逸到仓库外（简单校验）
- **NFR-SEC-003**：`<agent>` 参数限定 `[a-z][a-z0-9-]{1,30}` 正则；防止写入 `../etc/passwd` 之类

### 可维护性
- **NFR-MAINT-001**：遵循 project-standards.md 的 **100% 测试覆盖率**（引用 `feedback_100_coverage.md`，不可达行用 `# pragma: no cover`）
- **NFR-MAINT-002**：新增代码不超过 ~200 行（保持小 feature 范围）；CLI 子命令集成到现有 `harness/cli.py` 的 `policy` group

### 可观测性
- **NFR-OBS-001**：事件 schema 稳定版本化；若未来扩字段用 additive-only 原则

---

## 5. 约束条件（Constraints）

### 技术约束（引用 project-architecture.md）
- **TECH-001**：Python 3.11+，Click CLI，Pydantic v2，PyYAML（复用现有栈，不引入新依赖）
- **TECH-002**：文件物理位置 `.harness/policies/<agent>.yaml` **不变**
- **TECH-003**：事件位置 `.harness/events/policy_change_event-YYYY-MM.jsonl`，与现有事件文件命名一致（参考 autonomous_execution_event / dispatch_event）
- **TECH-004**：PolicySchema 与 dispatcher 现有代码零改动（AC-P0-006）

### 时间约束
- **TIME-001**：目标 1 个 session 内完成 L3 实现（预估 4-6h 编码 + 测试）

### 资源约束
- **RES-001**：单人（Ken）实现；无需跨 Agent 协作

---

## 6. 排除范围（Out of Scope）

明确不做，避免范围蔓延：

1. **不做密码学鉴权**：`--as-agent` 是声明，不是验证。未来若需强鉴权，走独立 feature 与 Relay token 体系整合
2. **不做 rollback 命令**：Git 已经提供版本管理（`git log -- .harness/policies/<agent>.yaml` + `git checkout`）
3. **不做 schema 字段扩展**：Amanda memo 里 #5/#6/#9 归独立 feature（policy-schema-extensions / session-lock-cli）
4. **不做 policy 内容审批流**：Ken 不审内容；若未来需要，走 handoff sign-off 模式（Amanda 元治理意见 #10）
5. **不做物理位置迁移**：policy 仍在 `.harness/policies/`；所有权通过 CLI 接口边界体现，不靠目录位置
6. **不修改现有 policy validate / dispatcher / scan 代码**：只加新能力；regression 靠现有 711 tests 保证
7. **不为 Reeve / Aki 自动生成默认 policy**：首次 write 由各 Agent 自己发起；Aki 当前没有 policy 由 Aki 自己起草（Ken 不代笔）

---

## 附录 A：Amanda memo 协商意见分流

Amanda ho_20260419_264 附带的 `review-2026-04-19-amanda-policy-negotiation.md` 10 条意见，按新分层分流如下：

| # | 意见摘要 | 分类 | 去向 |
|---|---------|------|------|
| 1 | cross_owner_boundary 细分 label | 基础设施（schema + scan） | 未来独立 feature `scan-label-refinement`；backlog |
| 2 | path_constraints.write 收紧 | **内容**（Agent 自决） | Amanda 自写时决定；本 feature 无关 |
| 3 | push_immediately 加 self_reported_breach | **内容 + 微契约** | 名字 Amanda 定；harness 侧无需改（list[str] 已自由） |
| 4 | ssot_drift.major 升 autonomous_llm | **内容**（Agent 自决） | Amanda 自写时决定 |
| 5 | max_duration per-label 分层 | 基础设施（schema 扩展） | 未来独立 feature `policy-schema-extensions` |
| 6 | Write 路径事后审计字段 | 基础设施（event schema + dispatcher） | 未来独立 feature `dispatcher-write-audit` |
| 7 | frequency_hint */30 保留 | **内容**（Agent 自决） | 无需 Ken |
| 8 | workspace 相对路径解析 | 基础设施（CLI 增强） | 未来独立 feature `workspace-path-normalization` |
| 9 | harness session lock 原子 CLI | 基础设施（独立 feature） | 未来独立 feature `session-lock-cli` |
| 10 | policy 变更协商流程入宪法 | 元治理 | Amanda 领域，本 feature 无关 |

**本 feature 作为"分流枢纽"**：打通 Agent 自撰 policy 接口后，#2/#3/#4/#7 自动被解决（Amanda 自己决定），#1/#5/#6/#8/#9 沉淀为 harness 后续 feature backlog。

---

## 附录 B：追溯链

- **触发事件**：Ken 越权修正（2026-04-19 commits 193d75091 / bdd14b9c7）
- **直接上游**：ho_20260419_264（Amanda 反向 handoff + policy 协商意见 memo）
- **大鸟方向**：2026-04-20 "policy 也应该由 Agent 自我编写 / 你需要提供的是写入 policy 的接口 / 用 CLI 这才是平台做法"
- **相关 wi**：
  - wi_20260419_378（amanda heartbeat-runtime 接入，本 feature 间接收口此 wi 的 policy 段）
  - wi_20260419_576（Amanda 交付物审核 wi，由本 feature 改变审核方式——Ken 不审内容）

