# PULSE 迁移实施清单与证据边界

关联 `wi_20260823_184`；目标与停止条件见[布局方案](pulse-platform-layout-plan.md)，安装边界见[SCL 同仓契约](scl-new-layout-compatibility.md)。

## 1. 范围与证据基线

本清单区分**配置事实、部署时读回、消费者时读回、待验证目标**。下列现场结果观察于 **2026-10-08 09:50–09:58 UTC+8**，不是无限有效的健康保证。未覆盖对象标为 unknown（无法判断），不据此判失败。

- 固定 PULSE main：`b9aa1549b8255c2b8b63027ca0f60fa6bcafdfb5`；共享工具链定义：Coevolution `dfa3e31c25e618566844b7d1a8d6479a1972641c` 的 `ssot/toolchain`。采用账户唯一 Python，不复制解释器或依赖进发布物。
- WI219 已交付正式入口迁移及历史回退/账户命令映射（[PR1071](https://github.com/bigbird-online/PULSE/pull/1071)、[PR1077](https://github.com/bigbird-online/PULSE/pull/1077)）、两云消费者和登记（[PR1078](https://github.com/bigbird-online/PULSE/pull/1078)）、从 enrolled CWA 源读取标准工具链（[PR1079](https://github.com/bigbird-online/PULSE/pull/1079)）。这些机制不在 WI184 重做。
- [PR1084](https://github.com/bigbird-online/PULSE/pull/1084)防止 Pi 清单重新引入已退休旧资源目录；[PR1085](https://github.com/bigbird-online/PULSE/pull/1085)记载 TCloud 清单修复、原历史保全及回复验证。PR 正文是生产者记录，不等于本轮独立现场观察。
- 原安装器修复源 `02605a145a0c4b4f2498788c63a9f6c01ec70c82` 的 [CI run37606308533](https://github.com/bigbird-online/PULSE/actions/runs/37606308533) 制品 `11477351057` 已核 SHA256 `adcda2c0fba1f163fdda85e7c357a664bb623284326e9927e667ad8c52130565`。JUnit 415 条记录、0失败/错误/跳过，其中 migration 套件29条；这是隔离机制证明，不是生产回退实测。完整 Harness tier 被跳过，未算通过；后续消费者 PR 的适用 CI 与 merged-source default-branch verification 也已取得 success。
- 本轮按人类“查清楚，然后按计划继续”，通过已登记的 TCloud 本机及 Vultr `pulse` SSH 通道进行精确只读检查；无 root 提权、未知主机密钥接受、身份替换、生产写入或重启。证据摘要见[两云入口现场核验](wi184-cloud-entrypoint-readback.md)。此前工作树资格不授权任意生产操作，这次读回也不扩展为维护/删除许可。
- 原10-05三站 adoption、WI219独立 Outcome、源导入及 reader/Relay 状态迁移等历史成果继续承接，证据索引保留在[第2步报告](wi184-stable-entry-isolation.md)与[固定历史清单](https://github.com/bigbird-online/PULSE/blob/54bb26af0a0d6e5b24e4af3c6a07660f04c328a7/specs/pulse-platform-deployment-inventory.md)。WI219整体裁决不转成WI184通过；旧版本的入口拒绝也不否定后来正式迁移能力。

Platform继续排除；Stats/ComputeCost只核迁移接缝，不接管业务修复。Vultr37项cron必须全部禁用，不靠启用制造验收。

## 2. 已登记与已观察的位置

| 对象 | ownermac / owner | TCloud / pulse | Vultr / pulse | 证据时间/限制 |
| --- | --- | --- | --- | --- |
| 稳定 Harness 代码入口 | `/Users/owner/Coevolution/Bigbird/PULSE/data/agent-harness/tools/agent-harness` | `/home/pulse/.local/share/coevolution/tools/agent-harness` | 同 TCloud，Vultr本机 | Mac：最新registry与10-05 adoption；两云：10-08 registry及现场链接 |
| 账户 CLI | 当前现场 unknown | `/home/pulse/.local/bin/harness` → 标准代码入口的 `bin/harness` | 同 TCloud | 两云10-08链接读回；Mac声明入口为`/Users/owner/.local/bin/harness`，不把声明当新现场 |
| 固定代码发布 | 10-05为`.../PULSE/data/agent-harness/runtime/releases/1d56564d73e3-4f9b27473abad4fa`，当前unknown | `/home/pulse/.local/state/agent-harness-runtime/releases/02605a145a0c-4f8b348bf2b4593f` | 同 TCloud | 两云10-08真实目标；代码与共享Python分离 |
| PULSE账本 | `/Users/owner/Coevolution/Bigbird/PULSE/data/agent-harness/service-repositories/bigbird-online/PULSE/ownermac/.harness` | `/home/pulse/.local/share/pulse/service-repositories/pulse/main/tcloud-node2/.harness` | `/home/pulse/.local/share/pulse/service-worktrees/pulse/main/vultr/.harness` | Mac登记/历史采用；两云现场Relay/reconciler及原迁移anchor一致，不重迁 |
| SCL kernel | `/Users/owner/Coevolution/Bigbird/PULSE/data/agent-harness/scl-kernel` | `/home/pulse/.local/state/agent-harness/scl-kernel` | 同 TCloud | 两云原anchor、当前消费者与登记一致；Mac不扩展为新现场 |
| 外层安装状态 | `/Users/owner/Coevolution/Bigbird/PULSE/data/agent-harness/runtime` | `/home/pulse/.local/state/agent-harness-runtime` | 同 TCloud | 原receipt/transaction保留，原摘要逐项匹配；不另起state根 |
| sealed SCL | 当前现场unknown | kernel的`runtime/active/bin/harness-scl` → `runtime/releases/6d44c8410f6e26e773f8645e570ac5aa2b9df1b56c6281f71bbfb30b74f80fd4/bin/harness-scl` | 同 TCloud | 两云CLI链接/Relay/Pi绑定现场一致；本会话handshake源02605a |
| 标准Python | 10-05独立读回3.14.3，当前unknown | `/home/pulse/.local/share/coevolution/python-env/bin/python`，3.14.6 | 同路径，3.14.4 | 两云本次实际解释器与sys.prefix；跨机补丁差异保留 |
| 旧BigbirdEnvironment `.venv` | 不以两云路径替代Mac旧源对象清单 | exact旧路径不存在 | exact旧路径不存在 | 两云10-08 `lstat`；只核这条入口，不推定所有旧父目录/发布均已清理 |

标准工具入口属于账户工具链；Harness代码与治理仍归PULSE。此前拟议的`/home/pulse/.local/share/pulse/tools/agent-harness`不再是另一个迁移目标，不创建第二套入口。

## 3. 消费者、启动与恢复清单

| 对象 | 本轮已核事实 | 剩余义务/边界 |
| --- | --- | --- |
| 两云账户CLI/正式迁移 | 新链接存在；intent/completion为migrated，原apply receipt/transaction摘要、paired applied状态、部署WI与ledger/kernel/state scope匹配 | 未在生产重复apply/rollback；原部署WI是来源，不是本轮维护授权 |
| 两云Relay→Harness/SCL | 两站live PID白名单环境均用新Harness入口、原PULSE账本、sealed SCL和02605a reconciler；装载relay.service及drop-in无旧入口引用 | 不等同全部服务配置、整机启动或全root进程证明 |
| 两云常驻reconciler | 真实子进程argv使用标准Python及02605a固定代码；cwd分别原服务仓/工作树，ledger/kernel绑定未变 | 底层exe与canonical prefix分别记录，不把基础Python二进制当另一个环境 |
| 两云Pi现役清单 | 原live Relay指定manifest逐profile旧引用0、缺失nodeResourceRoot 0；canonical Python及SCL绑定一致 | 本轮未发模型任务；承接原故障/回复验证，不重跑用户通信UAT |
| 两云user unit | 实际列出的relay/harness/reaper/patrol/ledger/heartbeat/escalation/ack-probe定义无旧词法入口引用 | 仅该名称闭包，未扫全系统unit/所有脚本；Vultr部分timer虽enabled但inactive，不冒充masked或故障已修，也不启用/重置 |
| Vultr37项cron | live Relay路径下37/37 enabled=false；**19项`execution.harnessDispatch.harnessBin`仍为旧`.venv/bin/harness`** | 是真实禁用配置残留，不是注释。未来手动dispatch/启用前必须经原writer精确迁移并保持全禁用；本轮不直接编辑、不重建旧链接 |
| TCloud cron | live路径下jobs为0 | 不是Vultr任务消失或复制许可 |
| 进程普查 | 所有可读pulse-owned argv无旧路径参数；常驻消费者独立核到新绑定 | 两云部分cwd被EACCES拒绝；不升级root、改ptrace/sandbox或把未知改pass，不据此签发新清理许可 |
| 两云Relay主程序 | 两站真实exe `/usr/bin/node`，cwd仍`/home/pulse/releases/PULSE-394ac10fe05d44e3498b44b581620e1301af89a7/Relay` | Harness升级不等于Relay升级或发布根归位 |
| Mac Relay/CLI/launchd/WI184旧源 | 新registry与原10-05/已完成迁移、用户通信确认继续承接 | 当前装载定义、#922精确12/114/7 stage/purge/retirement receipt及后验仍待核销；不重删，不把别的WI退休算此项 |
| 配置/持久状态 | 两云live映射指向`~/.local/share/pulse/{config,secrets,data}/relay`；只导出路径，不读取秘密内容 | 备份对象/写入者/恢复权限与完整性仍需按原范围验证 |
| Stats/ComputeCost接缝 | 两云Pi使用canonical Python及kernel；#977等业务成果承接 | 仅补受影响读取路径，业务归原owner |
| OS启动/独立恢复 | 当前Relay与reconciler存活、真实持久unit可读 | 不等同登录/linger、整机恢复、独立故障域的数据/附件/配置/信任与匹配程序恢复；Git clone/previous不替代 |

旧入口“路径已不存在”和“所有冷配置已清零”是不同结论。前者本轮已证；后者因19项禁用cron仍不成立。完整三端消费者/恢复清单尚未闭合。

## 4. 剩余最小动作及停止条件

1. **不再开发第2b入口接管机制**：承接WI219正式installer/paired adopter、历史映射及隔离/CI证明。现有精确失败保护继续有效，任意别名不是隐式采用。
2. **先处理明确冷配置残留**：只对Vultr19个`execution.harnessDispatch.harnessBin`旧值评审精确旧→标准值变更；原37项身份、enabled=false、任务内容、策略、账本根、其它字段及原字节备份均须保持。[原writer最小准备](wi184-disabled-cron-preparation.md)已补可选锁内前像检查并通过隔离回归，尚未生产采用。11:08 UTC+8复查仍37全禁用、19字段未改；现用writer已配置产物回执，不能绕过。当前WI写资源只有原两工作树，生产cron文件不在范围；没有受支持的确切维护授权，不借SSH/同UID写入、不改startup/reinit扩大scope。成功提交后的原producer显式回退和内存安全采用也未核完，不能把自动失败回退测试当完整生产门已过。
3. **补原剩余清单**：优先核销Mac旧源退休的既有正式材料及适用启动/恢复对象，不复制他人豁免或重复已完成迁移。剩余物理布局逐对象处置，不能把一个入口成功当整体完成。
4. **停止条件**：当前绑定漂移、原历史/备份不完整、扫描不足影响所拟动作、忙消费者、未决外部效果、无合法写权限或回退不完整，都停止相应写操作。不得重建baseline、改旧receipt/journal、手改WI身份/信任或恢复旧入口求通过。

## 5. 长期交付与最终验收

- 两云`deploy.yml`/`deploy-tcloud-relay.yml`原长期自动发布合同仍单独核验；手工dispatch及bigbird另一个实例的自动发布不替代。启用生产自动化、整机测试和不可逆清理各守原审批边界。
- Mac checkout-private Harness根、两云`.local/state`状态和`/home/pulse/releases`等过渡布局不随标准工具入口采用自动核销；原Git外平台布局、独立备份/恢复要求保留。
- Platform排除决定须沿原正式生命周期接口对齐结构化验收范围，不手改YAML/指纹。
- task branch发表、适用CI、main交付、required consumers终态采用、精确候选、独立Outcome、人审、最终冻结与正式结算分别完成；本文及本轮只读报告不是独立验收。

按人类要求一步后暂停复盘。两云精确入口及相关消费者只读盘点已完成；撤销重复接管机制开发。随后“继续”只推进原writer并发保护的最小准备与隔离验证，生产19字段尚未修，当前阻塞是合法维护执行范围及实际producer显式回退，不是再次迁移已退休Python环境或继续扩大代码方案。
