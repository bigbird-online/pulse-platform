---
name: Pulse Workspace State Relocation
version: 1.0.0
modified: 2026-08-23
source_snapshot: 8d0943b72987e061b99f045751b7aa4807e80cd4
---

# Pulse Workspace State Relocation

本表逐段覆盖源快照中四个 surface workspace 的 `PROGRESS.md` 与 `STATE.md` 全部非空语义行。空行只是排版，不承载事实。迁移后共享 `pulse-space/MALIK.md` 保存 O/KR 与经营环，共享 `pulse-space/STATS.md` 只保存带 source_ref/as_of/expires 的跨 surface current facts，surface 时点事实进入 receipt，动态工作前沿只进 canonical WI/checkpoint/ResumePacket。

## Relocation map

| source | lines | original responsibility | canonical destination | disposition / loss check |
| --- | ---: | --- | --- | --- |
| `local-pulse-space/PROGRESS.md` | 1–3 | 文件身份与非流水边界 | `specs/pulse-workspace-operating-system.md` §3 | 上收状态 profile 合同；无实例事实 |
| 同上 | 5–9 | 三条月度 KR | `pulse-space/MALIK.md` | 合并为一个 Pulse 的可衡量 result/tool/count；旧环境口号不保留为第二份 O/KR |
| 同上 | 11–13 | 动态工作只认 GitHub/Harness | root `CLAUDE.md` §2、WI/checkpoint/ResumePacket | 规则上收；无具体在飞项丢失 |
| 同上 | 15–19 | 启动顺序 | 共享 `pulse-space/AGENTS.md` + surface `CLAUDE.md`、`HEARTBEAT.md` | 编译为双根 bootstrap + SCL entry |
| `local-pulse-space/STATE.md` | 1–3 | 环境投影边界 | `pulse-space/STATS.md` header、operating-system §3 | 跨 surface 事实带 source/as_of/expires；surface 时点事实进 receipt |
| 同上 | 5–7 | resident macOS surface 定义 | checkout/surface registries、surface `CLAUDE.md` | registry 为配置真源，adapter 只解释入口 |
| 同上 | 9–20 | surface/entry/control-plane/capability/unknown health | bootstrap receipt + checkout/surface/actions registries | 配置事实指 registry；live Git/health 初值 unknown，须当次取证 |
| 同上 | 22–26 | hostname、resident、Actions 与授权边界 | root `CLAUDE.md` §4–6、surface `CLAUDE.md` | 共享规则上收，surface 只留 adapter |
| `tcloud-pulse-space/PROGRESS.md` | 1–3 | 文件身份与非 GitHub 副本边界 | operating-system §3 | 上收状态 profile 合同 |
| 同上 | 5–9 | 三条月度 KR | `pulse-space/MALIK.md` | 合并为共享接活、交付、静默断链的可衡量结果 |
| 同上 | 11–13 | 动态 WI 不落本文件 | root `CLAUDE.md` §2、WI/checkpoint/ResumePacket | 无具体在飞项丢失 |
| 同上 | 15–19 | 启动与 resident entry | 共享 `pulse-space/AGENTS.md` + surface `CLAUDE.md`、`HEARTBEAT.md` | 编译为双根 + principal `pulse` 的 surface admission |
| `tcloud-pulse-space/STATE.md` | 1–3 | provider/runner/live freshness 边界 | `pulse-space/STATS.md` + receipt、operating-system §2–3 | 静态模板不再声称 current |
| 同上 | 5–7 | Linux A resident surface | checkout/surface registries、surface `CLAUDE.md` | provider 不进入 Agent identity 或 lifecycle type |
| 同上 | 9–20 | entry/control-plane/capability/health | bootstrap receipt + registries | health 初值 unknown；receipt/probe 后才可带时点更新 |
| 同上 | 22–26 | provider、entry、CI/UAT、task 状态边界 | root `CLAUDE.md` §4–6、surface `CLAUDE.md` | 规则上收，工作前沿转 SCL |
| `vultr-pulse-space/PROGRESS.md` | 1–3 | 文件身份与非快照边界 | operating-system §3 | 上收状态 profile 合同 |
| 同上 | 5–9 | 三条月度 KR | `pulse-space/MALIK.md` | 合并为共享请求终态、静默恢复、根因交付的可衡量结果 |
| 同上 | 11–13 | WI/GitHub 动态状态现场取得 | root `CLAUDE.md` §2、WI/checkpoint/ResumePacket | 无具体在飞项丢失 |
| 同上 | 15–19 | resident entry 与跨 surface 接续 | 共享 `pulse-space/AGENTS.md` + surface `CLAUDE.md`、`HEARTBEAT.md` | checkpoint/adoption 边界保留 |
| `vultr-pulse-space/STATE.md` | 1–3 | provider/runner/live freshness 边界 | `pulse-space/STATS.md` + receipt、operating-system §2–3 | 跨 surface fact 结构化，surface fact 进 receipt |
| 同上 | 5–7 | Linux B resident surface | checkout/surface registries、surface `CLAUDE.md` | 与 Linux A 同 principal、不同 surface_id |
| 同上 | 9–20 | entry/control-plane/capability/health | bootstrap receipt + registries | live health 未取证前保持 unknown |
| 同上 | 22–26 | 目录、migration/fallback、动态状态边界 | root `CLAUDE.md` §4–6、surface `CLAUDE.md` | 共享规则上收，工作前沿转 SCL |
| `chatgpt-github-pulse-space/PROGRESS.md` | 1–3 | 文件身份与现场查询边界 | operating-system §3 | 上收状态 profile 合同 |
| 同上 | 5–9 | 三条月度 KR | `pulse-space/MALIK.md` | 合并为共享 entry、交付、资源判断的可衡量结果 |
| 同上 | 11–13 | open PR/Issue 不复制 | root `CLAUDE.md` §2、WI/checkpoint/ResumePacket | 无具体在飞项丢失 |
| 同上 | 15–19 | stateless 启动、entry、resource resolver | 共享 `pulse-space/AGENTS.md`、surface `CLAUDE.md`、entry workflow | bootstrap receipt 已接入 bridge acknowledgement |
| `chatgpt-github-pulse-space/STATE.md` | 1–3 | brokered 环境非 health 真源 | `pulse-space/STATS.md` + receipt、operating-system §2–3 | 静态模板不再声称 runner 健康 |
| 同上 | 5–7 | actions_bridge 与 fallback 定义 | checkout/surface/actions registries、surface `CLAUDE.md` | principal `pulse` 与 executor pool 分离 |
| 同上 | 9–19 | registry、checkout、entry、resource blocker、health | registries + bootstrap/entry receipt | current entry/SHA/health 未取证前为 unknown |
| 同上 | 21–25 | entry gate、failure 分类、现场查询 | root `CLAUDE.md` §4–6、surface `CLAUDE.md`、workflow tests | 共享合同与 adapter 分层保留 |

## Active-consumer migration

| consumer | new behavior |
| --- | --- |
| 四个 surface 的 `AGENTS.md` / `CLAUDE.md` / `HEARTBEAT.md` | AGENTS 共享 symlink，CLAUDE/HEARTBEAT 保留入口差异；共享 fact 回 STATS，surface fact 回 receipt，任务前沿回 checkpoint |
| `Scripts/check-pulse-workspaces.py` | 拒绝活动 PROGRESS/STATE；解析 STATS 五列表，检查共享 symlink、一个 principal 与四个 surface |
| `Scripts/pulse-workspace-bootstrap.py` | 绑定 caller `pulse`、当前 surface、registered repository slug、required-path object digest 与 canonical Harness status |
| local/vultr health 与 ops playbook | 只读 STATS current facts，不再生成/更新 PROGRESS |
| `/Users/owner/Coevolution/ssot/skills/malik/agent-work-method/` | 按 workspace-focus / scl-backed 路由工作前沿 |
| `/Users/owner/Coevolution/ssot/skills/ops/wrap/` | scl-backed 把短期状态与 KR 分类写 checkpoint/outcome evidence，不重建 PROGRESS |
| `/Users/owner/Coevolution/ssot/skills/ops/agent-dashboard/` | 显式 `--state-profile`；scl-backed 检 STATS freshness，unknown 不能 PASS |
| `/Users/owner/Coevolution/ssot/skills/ops/memory-sleep-field/` | scl-backed 的 progress-minimization 输出 N/A tombstone，不创建第二账本 |
| `/Users/owner/Coevolution/ssot/skills/malik/run-mission-wheel-review/` 及管理报告/预算消费者 | 读取注册 profile 的 live state |
| `/Users/owner/Coevolution/Doc/amanda-space/skills/agent-workspace-builder/` | 默认 workspace-focus；只有六项迁移门齐备才生成 STATS 并退役 PROGRESS |

历史 memo 中的旧文件名保持原样，作为当时事实；不参与启动和回写。
