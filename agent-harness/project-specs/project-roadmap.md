---
name: project-roadmap
version: 1.0.0
status: implementation_in_progress
work_item: wi_20260922_018
modified: 2026-10-13
---

# agent-harness 重写交付顺序

AH-001–080 / A-01–35 的完整义务以[功能规格](project-requirements.md)和
[验收规格](project-acceptance.md)为准。新核心已接入同一生产 Git 账本；截至 2026-10-13
验真快照，原生事实覆盖 WI184/WI550，其余旧入口和写者尚未整体替代。
整体完成门与退出清单以[唯一事实入口与集成合同](rewrite-pulse-integration.md)补足。
下列历史证据只证明各自范围，不能将测试数或单次 Muse 旅程换算成整体通过率。
工作持续归属原 WI018；实验内工作项不替代正式 WI018。

| 顺序 | 交付物 | 完成门 |
| --- | --- | --- |
| S0 规格 | 完整功能、领域边界、验收、来源与入口映射 | 无遗漏，冲突旧约束退出 |
| S1 核心 | 事实事务、原请求、授权、查询与重放 | A-08/A-15/A-22/A-23 |
| S2 协作 | 定义/图/任务/验真/人审/返工、升级、控制与接续 | A-01–18 全循环及真实采用 |
| S3 视图与动作 | 视图、原尝试读回、工作区、发布/采用/退役 | A-19–25 及故障矩阵 |
| S4 入口及辅助能力 | CLI/Relay/Host/Hook、策略/诊断/通知/原消费者 | A-28–33，必需入口无遗漏 |
| S5 迁移与交付 | 旧事实导入、制品、Muse UAT、支持面、旧写者退出 | A-26/A-27/A-34/A-35，全部 AH 有证据 |

## 当前实现与运行位置

新 `harness_rewrite/` 的业务边界为 execute/inspect/resume。定义、图、授权、任务、
候选、控制、迁移和外部义务进入同一 SQLite 事务；日志与原始输入不可变，视图可重放。
精确重试返回原决定及当前权限，不复活历史授权。外部动作保留原意图、attempt 和未知结果。

已实现标准与轻量流程、同 WI 升级、版本化定义和计划、DAG 条件/并行/等待/汇合、
中途及最终验真/人审/交付、协作请求/范围委派/责任转移。源码存在不等于所有原生分支已验。
实际文件交付、独立 Pi 派发、Git 多仓和稀疏资源、替换/退役、固定 bundle、
批准后的 main 发布与消费检出使用同一事实来源；人工 dirt、唯一成果和旧证据保留。

安装以精确程序和不可变策略共同决定 writer 准入，writer contract 3、location contract 2。
复制数据库或整个安装目录不获得写权。新格式迁移支持源封存、私有认证包、
目标原子接纳和新代际激活；跨 Host 连续性及旧格式导入仍待完成。

Muse Relay 已部署精确候选 `984a0ba7c`，隔离新核心已更新为 `587eba1bd`。
实际 Coevolution work-item-lifecycle 为4.6.0。新核心分别对照policy.node与
policy.producer_node校验Pi与Relay，两条工具链路径与原密封policy均保持；
精确新核心Linux CI 1分45秒通过，634项及lint/strict mypy共88.658秒。
见[路径修复与验收证据](evidence/rewrite-native-node-binding-20261009.json)。

原TG help/list/status均已返回新核心结果，两WI保持gen1/rev35与gen7/rev160。
status的模型回复已生成但等待回合收尾，原生stop后送达，未重试查询。
原会话上下文291442 tokens超过272000窗口；单次compact触发Relay的15秒RPC超时，
原生压缩于14:53:02完成，未重试；14:59上下文已为81086/272000。
缺摘要checkpoint在8秒内正确拒绝且无operation/effect；普通read/bash和identity已返回，
OS exit 0、原principal/role一致，实际读取skill路径、4.6.0版本与摘要相符。
压缩刚完成时context曾回退到累计token显示，后续模型回合后恢复真实上下文；
RPC超时先于原生完成及该显示问题保留为Relay体验限制。
业务事实和原会话前28253430字节保持；M01未知原尝试及三处资源不变。

此前正式部署及失败路径保留在[日常入口证据](evidence/rewrite-relay-daily-20261009.json)。
旧兼容Harness/reconciler未升级；旧事实全量迁移、全功能UAT与正式WI018完成均未宣称。

## 已获得的有界证据

| 能力 | 实际结果与证据 | 保留边界 |
| --- | --- | --- |
| 标准和轻量流程 | 固定成果、独立 Pi、人审、交付、终态精确重试；[旅程1](evidence/rewrite-native-journey-20261008.json)、[旅程2](evidence/rewrite-native-journey-2-20261008.json)、[终态补交/轻量](evidence/rewrite-native-journey-5-20261008.json) | 完成不自动清理资源 |
| 多仓/图轮次/资源 | 双仓稀疏扩缩、中途验真交付、实际路径/分支/登记退休；[旅程3](evidence/rewrite-native-journey-3-20261008.json)、[固定源发布](evidence/rewrite-native-journey-4-20261008.json)、[同 WI 升级](evidence/rewrite-native-journey-6-20261008.json) | 当前目标的三处旧资源仍保留 |
| 负面反馈 | 真实错误候选被独立拒绝、人审要求修改、修理后新验真和批准；[旅程7](evidence/rewrite-native-journey-7-20261008.json) | 完整 HOW/WHY 反馈循环未齐 |
| 位置准入与迁移 | 原两库管理转换、原 Pi 同 WI 源封存79、目标接纳81及新成果完成；[旅程9](evidence/rewrite-native-journey-9-20261008.json) | 仅同主机两库；旧格式、跨 Host 和独立会话未齐 |
| 用量与消费者 | 原固定 Pi 字节区间恢复六类 token，精确重试不重记；[旅程8](evidence/rewrite-native-journey-8-20261008.json)。现有 ComputeCost CLI 实际读取[新查询](evidence/rewrite-computecost-consumer-20261008.json) | 缺币种费用未知；非全 WI 生涯、生产 Stats 或所有消费者采用 |
| 等待/期限/扫描 | 原 Pi 历史等待页、当前查询、collection scan 和真实图 deadline；[watch](evidence/rewrite-native-watch-20261008.json)、[deadline](evidence/rewrite-native-deadline-20261008.json) | 缺 deadline 不猜测；不撤权、不自动归档删除 |
| 证据检查 | 原生保存的文件、命令退出码、正文及确认时序正反例；[检查证据](evidence/rewrite-native-checks-20261008.json) | 评估已记录证据；不是自动动作前约束或完整 SSOT 检查 |
| 策略与执行 | [预检](evidence/rewrite-policy-preview-20261008.json)、[安装](evidence/rewrite-execution-policy-install-20261008.json)、[原生 manual/audit/automatic](evidence/rewrite-execution-policy-native-20261008.json)，批次预算、只读及原请求保持 | 任意 shell 的路径隔离及完整 A-30 未齐 |
| 独立 SDK 验真 | 原 v7 网络失败保留，修复后 v8 真实独立 ACK/三 AC accepted、确切人审、发布/采用/交付各一次，gen7/rev160 completed；[完成证据](evidence/rewrite-compute-transport-completed-20261008.json) | 旧失败不改写，三资源未退役 |
| 通知 | 原 TG 即时875、同日聚合876、真实人类注意力877、明确未发送后同 effect 恢复886；[核心](evidence/rewrite-notifications-native-core-20261009.json)、[absence恢复](evidence/rewrite-notifications-absence-recovery-20261009.json)、[渠道正文](evidence/rewrite-notifications-channel-rendered-20261009.json)、[实验完成](evidence/rewrite-notifications-completed-20261009.json) | M01 仍 unknown/唯一 attempt；预算5/5，不再发送；飞书未验 |
| SSOT检查 | 新模块接管依赖闭包、快速选择、路径/链接/字节漂移及unknown，原CLI/scan复用；本机2.3.0实表Pulse快速5项通过，scan0发现，登记表字节保持；[证据](evidence/rewrite-ssot-source-20261009.json) | 源码已随candidate28部署；SSOT原生功能验收及其余登记表维度仍待证明 |
| 当前 Pi 活性 | candidate24 原 Pi 实际8查询全 OS0/stderr0；历史33当前 holder 为 observed_present，历史147的当前原执行者 observed_present/独立 verifier unknown；[原生证据](evidence/rewrite-runtime-presence-native-20261009.json) | 明确 current_observation，不证明历史时点活性；其它执行者/运行时未知，不撤权 |

## 当前保留状态与证据限制

通知实验 `wi_4067d1ff7e5bde73699df13c3f368ae0` 已 gen1/rev35 completed。
原七 AC、29138 字节完整报告、2374 字节独立 TG 正文、2636 字节审核补充均固定捕获。
仅一个 WorkCompleted，完全相同完成输入重试不增加修订；六项通知已解决，
M01 仍 unknown/一次 attempt，F-A 保留第一次 absence 及第二次唯一成功。
AC7 的逐步授权隔离管理例外明确接受，原报告没有改成无例外通过。

旧报告 WI `wi_c79426a0028172ffc55091a8ff68b128` 保持 gen7/rev160 completed。
完成时实读 remote/consumer 为 `78a317334841a2cdddc44456cb0e0a7d26bfd47a`，
tree `998bec2439b6e5f8e9cedcfe3330033953f941a0`；源库 gen4/rev79 sealed。
三处旧资源、22 个资源文件及旧失败/交付原件保持；最老资源占用仍 unknown，
没有扫描、kill、chmod 或退休授权。未知占用不阻塞独立源码实现。

candidate24 安装后只读重放验证两 WI 的完整35/160条日志、sequence、footer及引用原件，
holder 均为 null、active grants 均为0；373个 originals 摘要正确，实际39个数据库 guards。
管理端未保存此次安装前逐表前像，不能宣称安装前后逐表相等。已证的是四目录摘要
安装前后保持，以及随后8条原生查询前后 DB、两份 Views、12份渠道原件等字节不变。
缺字段曾被误报为 holder null、进程内 sys.argv 曾被称为 OS argv，均已按实际重放/启动方式纠正；
错误历史保留在原管理记录，具体边界见安装证据。

Desktop Commander 月额度耗尽，未重试或重连；后续通过既有 Muse 管理界面与原 TG 继续，
没有把工具额度或原隔离 sender 的未知退出原因解释为 Muse VM 故障。

## 下一步与未满足的门

1. 继续AH-068静态登记/查看的完整迁移与已支持字段保真，补新SSOT模块的原生功能采用；
   不把本机只读检查当成全部登记表维度通过。随后AH-066/067的前置检查/会话提取及
   AH-072资源告警、任务裁决、cron ID和运行产物；现有evidence checks不替代这些能力。
2. 补齐真实协作请求、范围委派、责任转移及 HOW/WHY 反馈分支；
   其它执行者/运行时活性提供者和约定定时巡检仍待实现/采用。
3. 完成受支持旧格式清单、隔离试导入、真实源停写及一次性激活；
   跨 Host 迁移和外部义务连续性单独证明，不用复制库代替迁移。
4. 迁移 CLI/Relay/Hook 及现有只读/移交面，逐项验证 Codex/Pi/Claude、
   macOS/Linux、TG/飞书；完善路径策略、Stats/质量/Outcome等原消费者及实际发布。
5. 按80AH/35A建立完整断言证据，完成全阶段故障矩阵、安装回退采用、旧写者退出，
   同步 Spec/README/skill，再进入全量 UAT。正式 WI018 的 Outcome、人审、
   publication/convergence、资源退休和 closeout 不以隔离实验完成替代。

继承 `.coverage` 114688 字节、SHA
`8fb67031faad6e806ab11c96cbbd3efe3e27615e9cd4b08b799fa93cb17b3aa5` 保持、不暂存。
正式 SCL 的本地提交与 Git publication 分别读回；不手工回放已提交效果。

## 日常 CI 收敛（2026-10-09）

[CI 契约](rewrite-ci-contract.md)与[实测证据](evidence/rewrite-ci-cleanup-20261009.json)
记录本轮删除旧三档调度、重复预检和日常旧全量路径的结果。精确候选 984a0ba7c
在 Linux 的完整候选验证 8分12秒通过；完整新核心632项、lint、strict mypy共64.026秒。
旧生产兼容回归独立保留；这项结果不代替 Muse 原会话命令验收或全部 WI018 验收。
