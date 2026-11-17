# WI184 第2步：稳定入口接管支持性验证

2026-10-06；对应[实施盘点](pulse-platform-deployment-inventory.md)及[SCL 安装边界](scl-new-layout-compatibility.md)。这是本地可行性检查，不是独立 Outcome、生产迁移、三站验收或旧源退役回执。

**时效更新（2026-10-08）**：本文固定并保留10-06原版本实验，不作为当前实现缺口或待开发指令。WI219随后已正式交付精确入口迁移、历史映射/回退和适用CI；[两云现场核验](wi184-cloud-entrypoint-readback.md)已确认新入口、原历史与活动消费者，旧精确路径不存在。原第2b重复开发取消；当前具体残留是Vultr19项禁用cron的旧CLI字段，不重复迁移Python环境或恢复旧链接。

## 结论

**原安装范围的升级/重试/回退可以继承；现有已核对公开路径不能直接把新名称接管为原 recorded stable 安装入口。** 已实际复现同 release 的不同入口名称被拒，无须改动生产来发现这一边界。

部署 writer 存在显式消费者入口选择能力，因此不能笼统说“现有代码完全不支持入口变化”。但消费者入口选择不改变原安装回执的 stable 绑定，也没有证明旧安装入口可以退出。仅改 registry、软链接或传入新 `stable_venv` 不构成受管接管。

按逐步暂停要求，本轮止于这一支持性结论；**不进入第3步生产切换，不预先实现新的接管协议。**

## 固定输入与隔离方式

- 测试基线 commit `d62748325b2fa69861af758c1b4fcd584b70437a`；原已登记工作树/任务分支不变。
- 执行的 installer 与该 commit Git blob `7801a221a34f2c8c9fd899380859203737d6a991` 一致；工作树文件 SHA256 `88d6923b7db3daf464835bf53312c68eef277c818a10bcb8183111b63a331ac4`。
- 新回归文件：[test_runtime_stable_entry_binding.py](../agent-harness/tests/test_runtime_stable_entry_binding.py)。最终执行文件 SHA256 `fad33c07edf411c782a3164005f43108280b0b0f2711f8430c4e1d2265b24b5b`。
- 使用账户 canonical Python，实际 prefix `/home/pulse/.local/share/coevolution/python-env`；没有建新 Python 环境、联网装包或修改账户工具链。
- runner 使用显式环境白名单，不继承真实 Host Session、Agent/WI 身份、Relay 凭据或 Pi 环境；HOME/TMPDIR/Harness/ledger/kernel 的测试位置均在原已授予工作树的忽略目录下。测试夹具另隔离账户状态。不是 OS 级网络/文件访问 sandbox，不把 runner 的安全声明当独立外部效果 attestation。
- 测试中实际执行的是本地代码发布物与本地合成 kernel；容量和进程发现是夹具输入。没有 SSH、生产消费者探针、真实安装历史导出、部署、重启、搬迁或清理。没有额外 WI 工作树。

## 执行结果与证据层级

最终选定集合：**118 passed、1 deselected**。新增7个用例全部通过。其余为原 recorded apply/upgrade/rollback、opt-in fault、relocation core/adoption、shared Python 及两个发布消费接口回归。

| 检查 | 结果 | 实际证据层级与限制 |
| --- | --- | --- |
| 原 stable：bootstrap → recorded first → recorded second | 通过；原 receipt/journal 字节保留，stable 与真实本地 canonical 指针对应新安装结果 | 真实公共 installer、shared-code 发布与本地 kernel；各合成 revision 复用同一个 wheel/策略，不是多份真实生产代码版本升级 |
| 原请求同版重跑 | 原回执重放，不新开回退事务；历史、release、kernel、ledger 字节/类型/权限不变 | 真实安装样本；公共锁文件允许合法 pid/time 更新，仍须普通文件且 mode=0600 |
| 原事务回退 | stable 回到原 release，真实本地 canonical pointer snapshot 等于原 `initial_canonical` | 公共 rollback 与本地 kernel；不是整机恢复或生产停写证明 |
| 完成回退后的重复回退 | 原事务结果及指针/ledger 不变，不覆盖原历史；追加一份严格校验的 NOOP reclaim 回执 | 原实现会调用 terminal maintenance，因此不宣称“整个目录零写入”；本样本所有 release 模拟受保护，新增回执须无删除、无失败、release 数不变 |
| 回退后再次升级同 revision | 新 apply 回执、正确 stable 与实际 canonical 指针；先前全部回执/事务字节仍相同 | 不仅检查返回 status；没有重建首次基线 |
| paired adopter：新名称与旧名称同指向一个 release | 新名称被 `stable runtime differs` 拒绝，旧名称通过；无文件/链接/权限变化 | 合成部署回执及只读 helper；未伪造真实 site 身份或执行生产 adopter |
| recorded installer：同 release 新入口名称，同版与下一 revision | 两种请求均被 `request binding differs: stable_venv` 拒绝；历史/release/kernel/ledger 不变 | 合成 helper/spies 与真实本地 shared-code installer 两层复现；公共锁的合法 pid/time 更新不是历史改写 |
| 未创建的新入口 | 不被当成隐式采用，原历史不变 | helper 负例，不证明任何生产目标可以创建 |
| 显式消费者入口 | writer 四个入口字段按显式路径生成，authority/ledger/kernel/state/trust 字段不变；已有 nondefault reconciler 用例通过 | 字段选择与合成进程树验证；没有启动真实消费者、移除旧入口或完成 installer 接管 |
| 损坏历史、未完成回退、错误路径、CAS/补偿/完成故障 | 所选已有回归通过；已完成 relocation 可重试/向前恢复，adoption 不执行旧运行物 | 多数为纯单元、spies 或源码契约；不能把真正 kernel relocation 的中断恢复转用为本次入口更名成功证明 |

明确排除 `test_running_code_bundle_is_visible_to_existing_reclaimer`：该例会扫描账户 `/proc`，不在本轮生产消费者读取范围。排除已记录在完整命令和元证据中，不把其能力计为通过。

### 留存的测试演进

- 首轮：116通过、2失败。两处新增公共接口断言将锁的 pid/time 更新误判为持久历史改写；只修测试的锁语义，不修改 installer。
- rerun-1：118通过；随后只读审阅要求增强再次升级的实际指针/新回执、重复回退历史、锁类型/权限和证据记录。
- rerun-2：117通过、1失败。强化检查观察到完成回退重试会追加 terminal maintenance 的 NOOP reclaim 回执。静态对照确认这是原实现的明确行为，而非重复回退事务。测试限定并校验这唯一新增回执，继续严禁改旧记录/指针/数据/封存权限。
- rerun-3：最终118通过。上述失败、较弱早期版本、对应 wheel/夹具和结果均保留，不覆盖早期证据，也不把失败归为生产故障。

本地受限证据目录：`pulse-space/memory/wi184-20261006-step2-isolation/`，最终结果在 `rerun-3/`。完整命令、selectors、排除表达式、环境键、实际 installer/runner/test 摘要见 `run.json`；最终原 XML owner-private，另有脱敏 XML。原材料未随源码公开。夹具 teardown 只对自己创建的测试 release 恢复可写权限；留存 scratch 不是待上线制品，不以其事后权限代替执行时封存断言。

最终 `pytest.log` SHA256：`fa99aecdd8d66c3daa6e3268061bee15cc1b82dedeec6d28a8ff61071ec306bb`；`results.xml` 与 `results-redacted.xml` SHA256：`6a5abf5c16b81fbd98f9cf5d85f8a1a03ef8cbe61ab5a83a1e6eedcda6c6f025`。它们固定所保存材料，不独立证明真实生产历史完整。

## 最早失效点与可复用能力

1. `Scripts/adopt-paired-harness-policy.py::current_deployment_binding` 按原文比较回执与登记的 stable 路径；同一 release 不能消除此差异。
2. `install-runtime-release.py::_recorded_full_apply_replay` 的 `installation_scope` 包含 stable、policy、state、ledger、kernel，同版重放与下一 revision 都必须匹配。
3. 前驱链、SCL prehistory 和 receipt/journal/canonical 配对还分别验证精确安装范围。只放宽顶层一个比较并不足以保留后续升级/回退。
4. `adopt_relocation_receipt` 采用已经完成的真实迁移，要求原 retention hold、来源回执/完成记录/封存 inventory。底层 `relocate_recorded` 要求不同的物理 kernel/outer 根，与本次保持 ledger/kernel/state 不动不符。
5. `pulse-release-writer.py::canonical_harness_environment(..., harness_venv=...)` 是可复用的消费者入口选择原语；仍不自动证明默认登记、后续 paired 升级、旧入口退出及回退一致。

上述反例说明本次核对路径不满足目标输入，不把 fail-closed 保护本身定为故障，也不声称所有可能的未来复用组合都已排除。

## 计划复核与下一关口

第2步完成了现有能力的隔离支持性判断，**未完成新入口可升级/中断恢复/回退的成功证明**。第3步生产采用前必须补一个最小方案审查与隔离验证关口：

- 以原 installer/paired adopter 为主，审查精确旧→新 stable 绑定接管能否产生新的关联证据而不改写旧回执/事务，覆盖前驱/prehistory/升级/原操作重试/回退。复用 consumer writer；不新增通用迁移框架或放宽任意路径等价。
- 新入口必须与同一已验证 release、原 ledger/kernel/state/trust/WI 对应；冲突、别名、缺失/损坏历史、忙消费者或未决外部效果要拒绝，不能重建 baseline 求通过。
- 补新入口接管本身的真实隔离中断恢复、回退再升级、原入口退出与错误目标负例；现有 relocation 的通过结果不能替代它。
- 若只选择“新消费者 launcher + 原 installer stable 绑定”，这是保留旧管理入口的过渡，不核销原旧入口退役目标，须明说范围后再决定。
- 人类允许舍弃此前 Git 分支审计，不等于授权删除 installer receipt/journal、重建 kernel 或替换 Host 信任。

完整生产消费者/启动/恢复清单、确切维护范围和原事务回退仍是后续生产门。Platform继续排除；Stats/ComputeCost业务留在原负责人；Vultr37条cron保持禁用。源代码回归及本地读回不代替正式CI、独立Outcome、默认分支交付和生产消费者采用。
