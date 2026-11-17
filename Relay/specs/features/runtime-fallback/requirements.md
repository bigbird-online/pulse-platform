---
feature_name: "runtime-fallback"
version: "0.0.2"
created: "2026-04-22T07:29:49+0800"
modified: "2026-08-15T00:00:00+0800"
upstream_dependency:
  - "specs/project-specs/project-architecture.md"
  - "specs/project-specs/project-standards.md"
  - "specs/project-specs/project-roadmap.md"
tags: ["relay", "runtime", "scheduler", "fallback", "codex", "cc"]
---

# runtime-fallback - Requirements

## 1. 用户故事

- P0: 作为 Relay 运维者，我希望当 CC 在 batch 场景下不可用时，scheduler 能自动切到 Codex，避免定时任务静默失败。
- P0: 作为 Relay 运维者，我希望只有“运行时不可用”才触发 fallback，而不是把正常任务失败误判成 runtime 故障。
- P1: 作为 Relay 开发者，我希望 runtime 选择逻辑从 scheduler/CC 细节中抽离，后续可复用到其他入口。

## 2. 功能需求

### FR-P0-001: batch runtime adapter 抽象

- 描述：为 batch 执行引入 runtime adapter 抽象，支持至少 `cc` 与 `codex` 两个实现。
- 来源：用户故事 1、3。
- 示例：scheduler 执行 job 时，不直接调用 `claude -p`，而是调用 runtime orchestrator。

### FR-P0-002: 显式 CC 场景不可用时可 fallback 到 Codex

- 描述：本文件保留 2026-04 的历史 feature 场景。当前平台默认主路已改为
  runtime=`codex`，默认 fallback 为空；只有 job 或测试显式配置 primary runtime=`cc`
  且显式给出 fallback runtime=`codex` 时，当前 job 才尝试 fallback。
- 运行时级失败包含：
  - 二进制不存在或无执行权限
  - 启动参数/协议不兼容
  - 认证、配额、rate limit、overloaded
  - 启动后超时无结果
- 示例：历史/显式 CC job 中 `claude` 缺失 → scheduler 同轮切到 `codex exec` 完成任务。

### FR-P0-003: 普通任务失败不得触发 fallback

- 描述：若 primary runtime 能正常启动并执行，但 job 本身非零退出，Relay 返回 job error，不切换 runtime。
- 示例：prompt 导致执行失败，结果应为 `status=error`、`runtime=cc`，而不是改用 Codex 重跑。

### FR-P0-004: cooldown + sticky-until-healthy

- 描述：某 runtime 被判定不可用后，在 cooldown 窗口内优先使用其他可用 runtime，避免每次 job 都先撞坏 runtime。
- 示例：CC 刚报 `ENOENT`，未来 5 分钟内 scheduler 优先使用 Codex。

### FR-P0-005: 本轮仅覆盖 scheduler batch 路径

- 描述：本 feature 只改 agent scheduler 与一次性 batch 执行；Telegram 长会话池、`pool.ts`、stream-json session 语义不在本轮范围内。
- 示例：用户通过 TG 与 OA 对话时，仍保持 CC-only 行为。

## 3. 验收标准

- AC-P0-001: 当 CC spawn 报 `ENOENT` 时，同一 job 在 1 次调度内自动回退到 Codex 并成功返回结果。
- AC-P0-002: 当 CC 返回普通非零退出且未出现 runtime-level 信号时，不触发 fallback，spawn 次数保持 1 次。
- AC-P0-003: cooldown 生效后，后续 job 在 cooldown 期内优先选择 fallback runtime，不重复先撞 CC。
- AC-P0-004: scheduler 的 job result 能记录 `runtime` 与 `attemptedRuntimes`，便于审计与排障。
- AC-P0-005: 相关单元/集成测试通过，`vitest` 中新增 runtime fallback 覆盖。

## 4. 非功能需求

- 性能：fallback 只在 primary runtime 失败时触发，不引入正常路径额外重试。
- 可维护性：runtime 选择逻辑集中在独立模块，scheduler 不再内嵌多套 CLI 细节。
- 可观测性：日志需明确记录 fallback 是否触发、候选 runtime、最终选中 runtime。

## 5. 约束条件

- 基于 `project-architecture.md` 的现有 Node.js + TypeScript + child_process 技术栈实现。
- 不新增生产依赖。
- 保持现有 `spawnCC()` 对测试和历史调用的兼容。

## 6. 排除范围

- 不为 `pool.ts` 引入 Codex 长会话支持。
- 不修改 Telegram channel 协议、stream-json reader、session 持久化语义。
- 不实现 runtime 健康探针守护进程，仅在 batch 调用路径内维护 cooldown 状态。
