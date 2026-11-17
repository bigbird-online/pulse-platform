---
name: Pulse Workspace Surfaces
version: 2.0.7
modified: 2026-11-10
# history: git log -- specs/pulse-workspace-surfaces.md
authority: one Pulse Agent and registered execution surfaces
---

# Pulse Workspace Surfaces

## 1. 一个 Agent、四个 surface

| surface workspace | canonical agent / principal | surface | entry compute |
| --- | --- | --- | --- |
| `local-pulse-space/` | `pulse` | `surface_v1_local_macos` / `resident_harness` | 当前 resident macOS + Harness |
| `tcloud-pulse-space/` | `pulse` | `surface_v1_cloud_linux_a` / `resident_harness` | 当前 resident Linux A + Harness |
| `muse-pulse-space/` | `pulse` | `surface_v1_muse_linux` / `resident_harness` | Muse Relay + 新 SCL 核心、原生产账本 |
| `oracle-pulse-space/` | `pulse` | `surface_v1_cloud_linux_c` / `resident_harness` | Oracle Phoenix ARM64 Relay + 原生 Harness、原生产账本 |

`pulse-space/` 持有共享 identity、MALIK、memory 和 Harness principal。每个 surface 定义自己的入口、能力和取证方式。一个 WI 在 surface 之间接续时仍是同一个 `pulse`，active authority 通过 checkpoint/dormant/adoption/recovery 迁移。

`chatgpt-github-pulse-space/` 已删除，`surface_v1_chatgpt_github` 与 Vultr 的 `surface_v1_cloud_linux_b` 只在 `retired_surfaces` 中留作历史计量归属。`vultr-pulse-space/` 不再是执行面，只存放仍在使用的脚本、systemd 单元、skill、Specs 与历史 memo。

## 2. 文件拓扑

`pulse-space/` 中的共享真源：

`AGENTS.md` · `IDENTITY.md` · `MALIK.md` · `MEMORY.md` · `SOUL.md`

每个已注册 surface 的 `*-pulse-space/` 必须：

- 以普通文件保存自己的 `CLAUDE.md` 与 `HEARTBEAT.md`；
- 以上述五个文件名建立 `../pulse-space/<file>` 相对符号链接；
- 不保留活动 `PROGRESS.md`、`STATE.md` 或第二份核心文件。

完整 Git checkout 才是支持形态；不保留 symlink 的下载/复制方式不能声称得到可运行 workspace。

## 3. 真源

- 双根继承、冲突裁决、状态分层：`specs/pulse-workspace-operating-system.md`。
- PULSE 公共项目合同：根 `CLAUDE.md` + 相关 Specs/code/tests。
- Agent 共享内核：`pulse-space/`。
- surface：`config/scl-surfaces.yaml`。
- 双根 checkout：`config/pulse-workspace-checkouts.yaml`。
- brokered Actions 资源：`config/actions-resource-pools.yaml`。
- surface 时点事实：bootstrap/entry receipt。
- 动态工作前沿：canonical WI / execution checkpoint / ResumePacket。

`scl-surfaces.yaml` 顶层唯一声明 `agent_id=pulse`、`harness_principal=pulse` 与 `state_profile=scl-backed`，surface 行只投影差异。checkout registry 的 workspace 行只保存 `surface_id`；repository 段拥有 slug、remote、default-ref policy 与 required paths。actions bridge 只声明 entry/rollover 两个 atomic profile，profile 再唯一解析到兼容 pool 与 workflow `runs-on`。

## 4. 普通启动与专项 UAT

普通讨论、诊断、审查和一般开发不以双根 bootstrap、身份背诵或 WI 恢复为启动前提。正式 WI 的 entry、continuation、adoption、recovery，以及确需证明 surface 或 execution authority 的操作，才运行对应 bootstrap 或读取 binding/ResumePacket。

workspace topology、surface registry 或迁移合同变化后，执行专项 UAT：

1. 在当前 surface 运行双根 bootstrap，取得 exact-SHA、`surface_id` 与 Harness binding receipt；
2. 读 Coevolution 根 `CLAUDE.md` 与任务所需 `ssot/*`；
3. 读 PULSE 根 `CLAUDE.md` 与当前 surface `CLAUDE.md`；
4. 通过 symlink 读同一份 `IDENTITY.md` 与 `MALIK.md`；
5. 用 Harness ResumePacket 恢复工作前沿，按需读 `SOUL/MEMORY`。

专项 UAT 要求各 surface 的 session 共享同一 Pulse 责任与工作状态，并各自证明 surface/compute/entry、双根 SHA、当前 WI 第一动作和仍 unknown 的 runtime 事实。

## 5. 变更纪律

- surface `CLAUDE.md` / `HEARTBEAT.md` 只写入口与环境 adapter；公共判断回 `pulse-space/` 或根合同。
- 共享 `MALIK.md` 回答如何经营 PULSE，不写 hostname、shell 命令或 runner 操作手册。
- 平台现状只从 Stats 同源查询读；surface-specific live facts 只进带 freshness 的 receipt。
- 所有大写文件 ≤100 行；共享文件 symlink 必须可解析且 target 在 `pulse-space/`。
- 本合同由 workspace checker（`uv run --with pyyaml python Scripts/check-pulse-workspaces.py`）、
  bootstrap/convergence tests 与 control-plane CI 机检。
