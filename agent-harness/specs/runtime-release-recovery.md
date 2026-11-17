# 运行时升级中断后的原事务恢复

安装器拥有制品校验、历史、外层安装指针和 canonical SCL 指针的协调。调用者只提交原事务身份与已读回的摘要；不得自行改指针、编辑事务、补造 receipt，或通过首次安装意图越过失败。

`install-runtime-release.py resume-prepared-apply` 接收 `--transaction`、`--expected-transaction-sha256`、`--state-dir` 和原 `--authorize`。这些值来自原安装记录和实际事务文件，不要求业务用户填写，也不构成新的业务授权。

本入口恢复一个已完成外层切换、尚未完成正式回执的 recorded upgrade：

1. 获取与正常安装相同的互斥锁。仍有安装器运行时拒绝；PID 文本残留不等于锁仍被持有。
2. 校验原事务摘要、授权、制品、精确源码、安装范围、已完成前驱及完整历史；只接纳这一笔未完成事务，其他未知事务仍阻塞。
3. 验证新旧 release 的既有密封清单、policy 备份及快照；实际外层指针必须仍指向这笔事务的新 release。
4. 若 canonical 仍等于事务保存的初始指针，以该快照作 CAS，调用正式 canonical activation。若已有原事务写下的 activation record，则校验实际指针与之相等，仅继续收尾。
5. 先按摘要保留原事务全部字节，再更新同一 transaction 并生成其正式 receipt。恢复过程中不准备另一份 release，也不创建新 apply intent。原回执已落盘而最终 journal 写失败时，恢复保留原回执字节。

崩溃前未落盘的 capacity、prepare 等中间输出不会重造为成功结果；恢复回执明确列出它们缺失，并包含此次实际运行验证。原失败及恢复痕迹永久保留。

## 明确拒绝的边界

- 首次安装、baseline、lineage 初始化、旧式不记录 canonical 的安装、已完成事务不走此入口。
- 外层指针、policy、源码/制品、前驱或其他历史出现漂移时，不修补它们来迎合当前请求。
- canonical 已改变，但原事务没有持久化 activation record 时，结果仍是 unknown；不能仅因新版本恰好正在运行而重建一次“历史成功”。
- 本入口不重启 Relay，不建立 WI 业务权威，不产生人类决定。SCL 运行时恢复不等于业务旅程 UAT 通过。

已完成事务可通过原 apply 的正式只读重放确认；不得再次调用恢复器模拟读回。新恢复尝试先读取更新后的同一事务，使用当前真实摘要。

正常 apply 回执不要求包含 journal 的内部阶段字段或恢复信息；恢复器必须核对其原事务路径、安装输入、前驱和 activation record，保留回执原字节。再次 journal 写失败不得使原回执失去恢复资格，也不得重复 activation。

`@anchor test agent-harness/tests/test_runtime_prepared_apply_recovery.py::test_original_apply_receipt_survives_repeated_final_journal_loss`
`@anchor test agent-harness/tests/test_runtime_prepared_apply_recovery.py::test_drift_never_activates_or_rewrites_history`

## 同一制品中的新核心

精确 wheel 同时携带 harness 与 harness_rewrite；构建、安装和回读校验两包的全部
Python 源码及新核心固定 verifier-runner.mjs。发布清单沿用原字段存储完整源摘要，
旧版不含新包的精确制品继续按其原集合验证。未知顶层包、路径逃逸、重复成员和
链接仍拒绝；不会通过复制旧安装或放宽到任意包完成迁移。新包安装不迁移旧业务事实，
不自动把旧 CLI 的工作项路由到新核心。

SSOT 的规则与 verdict 由新核心拥有；原模型测试只验证 DTO 和登记表序列化，
原适配器测试使用 check_all/check_agent/check_entry，规则及异常观察在
tests_rewrite/test_ssot.py 验证，不恢复已删除的私有判断函数。

`@anchor test agent-harness/tests/test_shared_python_runtime.py::test_installed_rewrite_and_ssot_adapter_are_source_bound`
`@anchor test agent-harness/tests/test_exact_runtime_build.py::test_exact_runtime_build_rejects_missing_rewrite_resources`

Muse 的精确 Linux 验证同时执行 tests 与 tests_rewrite 全集，不能以旧适配器测试代替新核心回归。
