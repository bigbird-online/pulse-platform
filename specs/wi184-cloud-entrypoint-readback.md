# WI184 两云稳定入口与消费者现场核验

## 结论与范围

观察时间：**2026-10-08 09:50–09:58 UTC+8**。TCloud/Vultr业务账户`pulse`的确切旧入口 `/Users/owner/Coevolution/Bigbird/Lab/BigbirdEnvironment/agent-harness/.venv` 均已不存在；新的CLI、运行中Relay、常驻reconciler、Pi现役清单已采用标准入口/修复代码。原正式迁移的历史anchor与完成记录有效。

**不能宣布所有配置清零**：Vultr37项cron全禁用，其中19项的`execution.harnessDispatch.harnessBin`仍为旧路径的`bin/harness`。这是可执行配置字段而非说明文字；未来手动dispatch/启用可能重新暴露缺失入口。未修这19项，不重建旧链接掩盖问题。

本轮属于已获人类同意的受控只读清单核对，不是生产迁移、回退/重启实测、旧源清理或独立Outcome。没有修改生产文件、启用cron、执行任务、重启、提权或改Host/SCL身份。Mac当前现场及#922的完整原退休对象仍不在本次两云观察范围。

## 方法与证据身份

- 固定源码PULSE `b9aa1549b8255c2b8b63027ca0f60fa6bcafdfb5`、CWA `dfa3e31c25e618566844b7d1a8d6479a1972641c`；先复核账户/入口、官方迁移状态结构与manifest定位规则，再读取实际对象。
- TCloud本机账户和主机读回为`pulse`/`tcloud-node2`；Vultr使用工具链登记的`vultr-pulse`账户和Tailscale端点。SSH采用batch、严格既有主机密钥、禁止更新密钥/复用控制连接；未借用root或另选公网/身份fallback。
- 两次探针以标准Python `-I -B`运行，只读取指定链接、`stable-entrypoint/{intent,receipt}.json`及其指定anchor、systemd只读状态/加载定义、白名单进程绑定和实际Relay映射下的Pi/cron配置。没有导出Session、任务内容、秘密环境或凭据；只记录路径、计数、状态和摘要。
- 原始摘要JSON及探针在Git忽略的 `pulse-space/memory/wi184-20261008-entrypoint-confirmation/`，目录0700、数据文件0600，不作为可部署制品。公开文档不复制原业务配置或进程完整环境。

| 本地证据文件 | SHA256 |
| --- | --- |
| `read-cloud-entrypoints.py` | `d6ff6836b29dded61629958a96445ed5a67036075545e1b29755daaae9f37a6a` |
| `read-cloud-consumers.py` | `1e7a9995d55b73cb2b459670469fe9e98543474f43346a684fdd0308d57b3d56` |
| `tcloud-readback.json` | `a1a2106d4449ff97107398d3d8c465facadc867b2d1c4ff59efb60c8f3688291` |
| `vultr-readback.json` | `7b101e0c01c663e3f4ec1cc611952cd1a171fa0f9000826b664882b2cb6aa495` |
| `tcloud-consumers.json` | `afc79f5634b68161e9234bfbc109d89b25eeb3f949ba205b490ee6a372e11dfd` |
| `vultr-consumers.json` | `a74f1e81740302a932b723e10d7aae35bea39ee06690e0f9c1e451b2ae013ea1` |
| `vultr-cron-legacy-fields.json` | `2d4288464a3aef183535c0ae59360a533c9396094f9d14549ffb2f18327fbbf4` |

摘要只证明证据文件身份；原始文件留本地意味着文档本身不是可供另一执行者复跑的完整消费者验收包。

## 现场结果

| 检查 | TCloud | Vultr |
| --- | --- | --- |
| 旧`.venv`确切词法入口 | `lstat`: 不存在 | 不存在 |
| 标准代码入口 | `/home/pulse/.local/share/coevolution/tools/agent-harness` | 同左 |
| 当前代码目标 | `.../agent-harness-runtime/releases/02605a145a0c-4f8b348bf2b4593f` | 同左 |
| `~/.local/bin/harness` | 指向标准入口的`bin/harness`，目标存在 | 同左 |
| `~/.local/bin/harness-scl` | 指向原kernel的active入口，实际sealed release为`6d44c841…` | 同左 |
| 实际探针解释器/prefix | canonical Python3.14.6及唯一`python-env` prefix | canonical Python3.14.4及同名账户prefix |
| 稳定入口intent/completion | `migrated`；owner-private普通文件；确切完成对象/hash匹配 | 同左 |
| 原历史anchor | receipt/transaction摘要匹配、paired applied、scope及原release未改 | 同左 |
| 原部署来源 | `wi_20260827_282`，intent源`1d56564d…`；不是当前维护授权 | 同左 |
| 活动Relay | 原systemd user服务active/running；实际`/usr/bin/node` | 同左 |
| Relay cwd | `/home/pulse/releases/PULSE-394ac10fe05d44e3498b44b581620e1301af89a7/Relay` | 同左 |
| Relay Harness/SCL绑定 | 标准Harness、原PULSE账本、原kernel及02605a reconciler | 同左，保留Vultr账本路径 |
| 实际reconciler | 标准Python argv、02605a固定代码、原服务仓cwd | 同左，原service-worktree cwd |
| Pi现役manifest | 各profile旧入口引用0、缺失允许目录0 | 同左 |
| Pi host-native资源范围 | 14个允许目录 | 13个允许目录 |
| 指定user unit定义闭包 | 旧入口引用0 | 旧入口引用0；既有masked/failed/enabled-inactive如实保持 |
| cron | 0项 | 37项全`enabled=false`，19个exact旧harnessBin字段 |

“原迁移intent源1d56564d”与“现役代码02605a”不矛盾：intent固定原迁移锚，后续正常升级走已完成映射；不为版本变化重写旧历史。两云账本/kernel/state分别维持原路径，不把标准工具路径改成另一份治理权威。

## 不能越过的边界

- 精确入口不存在不是所有旧父目录、旧发布或Mac旧源均已退役的证明。
- pulse-owned可读argv无旧路径参数；cwd读回有TCloud3、Vultr5个EACCES。源数据原样保留，未假装全进程扫描完整、改变sandbox/ptrace或提权。常驻Relay/reconciler单独读回成功；本结论不发新的破坏性清理许可。
- unit闭包按relay/harness/reaper/patrol/ledger/heartbeat/escalation/ack-probe名称选择，没有覆盖全系统root服务、所有执行脚本、OS crontab或其它账户。
- 本轮观察Pi清单路径和资源存在性，未另发模型任务；TCloud先前的实际故障/修复/回复验证只按原记录承接。清单与服务正常不替代机器启动、备份恢复或长期SLA。
- 基础Python二进制路径不同不代表第二套环境；解释器、canonical prefix、程序代码和consumer分别取证。当前共享Python版本差异按节点契约保留。
- Vultr `ack-probe.service`和`harness-scan-report-retention.service`保留既有failed；若干timer配置enabled但当前inactive。未reset-failed或启动它们，未把本轮入口证据扩成全监控健康。

## 后续单一动作

原第2b机制设计/开发取消，承接正式交付。下一项是沿既有cron配置writer核明19个冷字段的精确替换及原事务回退：`old/.venv/bin/harness` → `/home/pulse/.local/share/coevolution/tools/agent-harness/bin/harness`，保持全部37项禁用、身份、任务内容、策略、账本及其它配置字节语义不变。未验证writer、写权限与前像/回退前不生产写入。

其它原布局、Mac旧源、独立恢复、自动发布和正式结算责任见[实施清单](pulse-platform-deployment-inventory.md)，不因这项核验自动通过。
