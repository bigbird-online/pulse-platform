# Stats 发布制品保留契约

## 目标与本次范围

WI825 的容量审计发现 Stats 部署会增加 runtime、App 前像及本地安装回执，却没有发布者维护的验收、替代与到期关系。`stage_runtime` 成功、`installed`、`activated_pending_business_readback` 和后续报表发布均不能自行授予删除旧载荷的权限。

原索引与只读计划阶段已经采用。本次增加原 Mac 成本发布器专用载荷池、两种正式 writer 的真实入口能力判别，以及未来安装链经业务验收后自动退休更旧专用载荷。共享 runtime、App、native、legacy、回滚前像及永久审计均不获得到期授权；不改 WI/SCL 状态或追认旧业务验收。

## 发布者证据

`deploy-refresh-local` 在原 deployment/cost-refresh 双锁、idle、current、config、loaded argv 安全门通过并正式采用后写索引；完整 `deploy` 在实际 App/peer/runtime 采用后写索引。两入口仍保留各自原始状态、前像和回滚语义。

`deploy-refresh-local --native-publisher-only` 可让现役原生发布器采用受审的 PULSE 发布包。目标须有精确
main 验证、受管封装及匹配的发布器文件，已选 Stats 读取器须通过解码探针。在原生 release writer 锁与
两把 Stats 锁下，只切换 Stats 启动路径和源版本，保留原调度、peer、读取器、App 和登记。
失败沿本地发布器事务恢复原前像。原生发布包回收保护当前 Stats 启动路径和该次采用回执确认的上一个
发布包，后者须同时匹配配置与启动项前像。切换中断或回滚尚未确认时保留同一前像引用。
前像已不存在时保留现役发布包。发布器证据暂不一致时暂停释放已安装发布包和 cohort，Relay 仍走原容量准入。
`@anchor test Stats/exporter/tests/test_stats_deploy_local.py :: test_native_publisher_only_keeps_cadence_peers_reader_and_registry`
`@anchor test Stats/exporter/tests/test_stats_deploy_local.py :: test_native_publisher_only_restores_original_after_failed_bootstrap`

每份索引位于业务账户 `~/.local/state/pulse/stats/retention-adoptions/<原回执SHA256>.json`，目录0700、文件0600。原回执字节另存于 `retention-receipts/<SHA256>.json`；同名证据不覆盖，原 `deployment.json` 后续更新不抹掉它。索引绑定组件、revision、previous、peer previous、实际 runtime 身份、清单/config/plist 摘要，以及完整部署实际 App/前像的源版本和二进制身份。配置、token、环境及凭据原文不加入索引。

采用索引始终写 `acceptance=unverified`、`supersession=unverified`、`payload_expiry=not_authorized`、`audit_retention=permanent`。它只证明原部署产生了什么，不证明 GUI UAT、Outcome、正式退役或可释放空间。索引写入失败沿原 post-activation receipt-failed 状态报告；不得杀掉已开始的发布器或为补索引回退已采用消费者。

## 正式本地发布器的加载引用

新安装的 launchd 入口为原 canonical Python 加永久保留的 `~/.local/share/pulse/stats/publishers/<源码SHA256>.py`，由同一制品内 `Stats/publisher.py` 逐字安装。内容地址文件0700父目录/0600文件、不可覆盖，放在 releases 外；原 plist 前像仍可回退到旧入口。它先取得原 `deployment.lock`，再核唯一 source_revision、原采用索引、marker、清单和脚本，最后加载版本化程序；直到程序及 finally 返回或进程退出才释放锁。现有业务 `cost-refresh.lock` 仍由程序取得，锁序为 deployment→cost-refresh。安装尚持部署锁时新启动器最多等待30秒，不启动并行采集或假报业务成功。

这是正式 launchd 消费者的 pre-import 引用保护，不是全部离线/原生/内存消费者的证明。原 direct CLI、App、native hook/MCP、legacy、回滚和审计仍保护。下述前瞻专用池退休政策不扩大到这些共享载荷，也不新增人工 skip-lock/closure 旗标。真实验收要核 loaded argv、不可变启动器、自然轮终态与业务报表；单元测试只证明隔离进程锁序/取消/回退。

`stage_runtime` 对新 Git archive 普通文件去掉组/其他写权限及特殊权限位，保留读权限、所有者写权限和执行位；真实 archive 的0664/0775分别安装为0644/0755。新制品与启动器的归属、不可组/其他写校验必须一致。重用既有制品时拒绝不安全的文件权限、归属或链接，不修改原不可变目录。回归测试用真实 Git archive 经正式安装器再进入启动器，并确认不安全权限仍在加载前被拒绝。

关键加载登记先于 bootstrap：两种 writer 在真实 config/plist/载荷选择完成、原双锁仍持有时，将 `publisher_payload_selected` 原事实及不可变镜像写入私有 `publisher-enrollments`。该 seed 只授权加载精确已选代码，不是成功业务、adoption 或 expiry。seed 写失败按原事务恢复旧消费者，不启动新入口。bootstrap 后的正式成功 adoption 索引仍独立写入；该最终索引故障不阻断 seed 已绑定的实际业务加载，退休尾段 HOLD/failed 并留独立诊断。最终原成功索引和真实业务接受两者都缺一不可，seed 永不进入可退休 `adoptions()`。同原失败启动后重试可以保留多份 seed 审计；只在当前配置/plist 字节与真实入口相符，且 policy/path/device/inode/manifest/entry 能力事实全部相同的情况下等价使用。能力冲突即 HOLD，不按时间任取最新，不删除旧 seed。回退可按原相同配置字节和原载荷身份恢复旧 seed；实际输入原 inode 变化不得伪造新的业务接受。

## 只读计划

Mac 的正式入口为：

```sh
"$HOME/.local/share/coevolution/python-env/bin/python" Stats/deploy.py plan-retention --expected <实际current的40位SHA>
```

读取账户 home，不信任重定向的 `HOME`。核对 current CAS、配置与实际 loaded argv，读取有界、归属明确、稳定的原生与发布者元数据，列出 current、publisher、完整部署及其回退、native Hook/MCP 等声明引用。索引必须绑定不可变原回执的字节摘要。末尾重新核对元数据和 loaded argv；漂移、未知 schema、符号链接、公共索引或摘要不符时拒绝输出有效计划。

计划只覆盖声明的本地发布者和原生配置引用，未完成全体进程/FD、离线消费者或全 App 前像引用闭包。已记录但未验收的制品与未知历史制品均保护；`eligible_count=0`、`apply_supported=false`。该入口不创建索引、不补写历史接受记录、不触发刷新、不统计占用为可释放量。

## 成本发布器的前瞻独占载荷

新的本地和完整安装器共同把 Mac 成本发布器安装到私有 `publisher-releases/<revision>`。`refresh.json.source_revision` 是唯一版本选择器；本地安装不再移动 App、原生消费者、自动化和远端使用的共享 `current`。完整安装仍管理共享 runtime/App/peer，同时通过同一 helper 安装独占发布载荷。永久入口取得 deployment 锁后才解析配置及载荷，不从独占目录回退加载共享代码。旧共享制品、原入口、原索引与前像仍保护。

两种新 writer 根据当前 plist/loaded 的永久入口 SHA、当前 config/plist 摘要及不可变原安装索引识别专用能力，不向 refresh.json 加池选择开关。完整 workflow 将执行的 installer 和 target archive 固定为同一 approved checkout；新 full/local writer 还在 staging 前拒绝 executing installer 与 target archive 摘要不一致。历史冻结 workflow 在同一旧 source 上仍可能沿原授权采用旧共享入口：该入口不执行专用退休，所有共享载荷保护；下一新 writer 必须重新核其实际旧入口及共享 current，再受管迁移。不能仅凭同 revision 曾有专用索引判其为当前专用入口。任意同账户手工运行旧脚本再指定新 target 的跨版本组合不属于正式调用契约；旧代码无法被新文件变成 OS 权限边界，若出现这种未知入口，验收与迁移 HOLD。原 full writer 的 shared_previous 与 publisher previous 分列，失败各自回退原对象。

只有原成功安装回执及不可变采用索引在创建时记录 `publisher-exclusive/v1` 的新载荷受此政策管理。真实 launchd 发布完成后，入口绑定原安装、实际 PID/loaded argv/周期、第一轮与后续相隔至少一个1800秒周期的两次完整业务成功，以及目的端相同 generation 字节摘要，追加永久接受证据。失败、degraded、非正式入口或缺失读回不授予接受；不补写旧索引。App UAT/Outcome 仍另验。

历史发布证明先按原采用、revision、报告摘要、时间、周期及目的端摘要格式核验完整性，再按当前输入签名判断兼容性。正式部署改变目的端或其它输入时，旧证明不能授予当前兼容接受，原载荷 HOLD；不以当前目的端身份将完整历史证明判为损坏。只有输入签名相同的证明才必须严格匹配当前目的端身份。缺失、摘要损坏或结构非法仍拒绝，兼容判断不放宽到期、双锁、CAS、引用与删除门。

当真实安装链 A→B→C 中 B、C 各自已接受、B 与当前来源格式兼容、A 不再被登记的运行/离线/回滚消费者引用时，自动发布尾段才可退休独占 A。保留当前 C 和原安装指明的 B。原 deployment→cost-refresh 双锁、current/config/plist/注册表 CAS、目录/manifest/owner/device/inode/no-follow/单链接、实际 FD 与有界消费者配置核验、同盘0700私有 staging 和永久事务回执全部成立后，才移除精确载荷。任何不确定或失败保留现场并停止；审计永不清理。

该闭包限于登记的正式发布入口与原 full/local writer、LaunchAgents/crontab、原生 Hook/MCP、来源登记、完整部署及配置声明的端点。目录私有不意味着同账户任意程序无法读取；出现其它独占目录调用、未知/不可读配置、离线引用或活引用就 HOLD。共享 legacy 不能因本政策追认到期。人工直接调用、改年龄、重部署或强制刷新不能制造接受及删除证明。

共享制品和 App 前像仍须各原组件另立业务 acceptance、supersession 和 expiry 契约，不在本次删除范围。未知 legacy 不能通过年龄、非 current、目录大小或旧只读计划获得删除授权。

## 验收与回退

本切片测试覆盖真实私有文件与原本地/完整部署事务、采用失败不产索引、不可变证据、symlink/权限/大小/摘要/漂移拒绝，以及无写入计划和原生保护角色。独立审查、精确 PR/主分支真实 CI、真实账户只读计划分别记录。正式采用新安装器后，下一次真实原部署生成索引才算生产写入分支验收；不能人工制造已接受回执。只读计划成功不等于释放空间。

回退沿现有部署器与保留的原入口/前像执行；新索引和原回执属于审计证据，保留。仍不允许绕过原 active/dirty/unknown/rollback/失败审计保护门。
