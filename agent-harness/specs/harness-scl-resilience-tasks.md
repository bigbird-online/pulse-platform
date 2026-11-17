---
feature_name: "harness-scl-resilience"
version: "1.4.0"
status: "authorization_closure_redesign_pending"
work_item: "wi_20260827_282"
owner: "Pulse"
created: "2026-08-29T00:00:00+08:00"
tags: [tasks, scl, resilience, migration, uat]
---

# SCL Local-First Resilience — Tasks

## 1. Delivery rule

Requirements and design MUST be reviewed, committed and pushed as a Specs-only boundary before
implementation continues. Source experiments that predate this boundary are not evidence of design
completion and MUST NOT enter the Specs commit.

Each implementation slice ends with an installed-runtime readback and fault test appropriate to
that slice. Unit green, source-tree execution, Git publication or a generated draft alone is not
product acceptance.

`@anchor manual specs-before-code-boundary :: staged diff for the first commit contains only normative specs and its pushed SHA precedes every implementation commit`

## Current business-first implementation boundary

### Executable reinit recovery

The current delivery targets are the TCloud Pulse Feishu conversation blocked after successful reinit
and the ownermac Hestia conversation whose reinit stops at conflicting registration metadata.
Feature and slice risk are high: live process admission, authorization and channel continuation change.
The accepted human scope permits implementation, controlled deployment and real original-conversation
verification; preserving all prior work is mandatory. The original WI is preferred, with legal
successor reconstruction permitted only when necessary to recover the work.

- Reproduce successful reinit followed by failed service-ledger routing using the real launcher.
- Remove legacy ledger-copy routing from managed local execution while preserving canonical writes.
- Verify reinit through an installed launcher child, including negative identity and scope cases.
- Realize automatic local escape from an unchanged original human decision; preserve dirty data,
  reject broader scope and keep unknown external effects intact.
- Resume a persisted request after restart through the original Feishu bot and actual Agent process.
- Route native Host tools and slash commands through that same persisted recovery. Discover old
  rejected requests only from their original signed input, complete native result and unchanged
  channel decision. Preserve Stop and newer-input boundaries across recovery.
- Recover text known to have failed before dispatch; reconcile uncertain dispatch from native
  acceptance evidence without resending. A damaged recovery record must not stop another bot.
- Reconcile stale metadata only for the same sealed, physically verified worktrees. Preserve the
  old registration bytes in the authority transaction and archive before replacing its projection;
  resource changes and concurrent projection changes remain protected.
- Escalate dual-path recovery failure into a durable independent-resource transaction for the same
  legal WI. Preserve original resources and all business provenance; use a fresh private workspace
  and lock domain rather than weakening primary/resource locks or requiring a healthy old kernel.
- Verify installed independent recovery with an initially broken lineage pointer, primary-only
  workspace, damaged Git metadata and occupied source lock, plus dirty-data preservation,
  idempotent restart and exact Host decision/session/input/Stop fences. Real child read/write/test
  and actual channel continuation are required; a prepared copy alone is insufficient.
- Resume canonical synchronization automatically after original registered resources become usable.
  Reconcile the recovery copy's changed, new and deleted files with the immutable inheritance
  manifest and the current original workspace. Preserve both sides on conflict or unknown evidence
  intersecting the planned changes; continue authorized local work while retrying mechanical failures.
  Control references and a non-atomic snapshot marker alone must not block reliable per-file plans.
  A partial manifest never implies deletion of an unlisted path.
- Complete original canonical reinit/enter only through its formal, idempotent operation; preserve
  the same human authorization, WI and native Session. Do not allocate another WI or replacement
  workspace, invent an epoch, or replay already dispatched business work.
- Use both real resource lock domains, durable per-path receipts and manifest/identity CAS. Recover
  interrupted copies and lost responses without overwriting new work. Verify the actual normal
  consumer, native history, usage origin and public launcher before retiring the active derived
  projection. Compensate failed adoption through the still-authorized local consumer.
- Prove restored commit/publish capability through installed guards and a disposable local bare
  remote in tests. Production reattachment sends no automatic business commit/push and never
  repeats uncertain external effects. Report unavailable publication separately from local work.
- Publish verified code, deploy through the existing TCloud release/installer transaction and retain
  its rollback receipts. Activate only when the existing restart guard confirms no active work.
- Pass only when the original bot performs the authorized next business action through its normal
  tools and returns the result to the original conversation. CLI-only success remains insufficient.

原登记资源自动接回的当前切片验收：

| 用例 | 必须观察到的结果 | 状态 |
|---|---|---|
| 已安装双层失败后副本读/改/测；原控制与原登记资源恢复 | 无第二条人类授权/新 WI/旧业务重发；新增、修改和明确删除保留；同 native normal consumer 及 public launcher 真正执行成功 | pending |
| 原决定已 canonical committed / 尚未 committed | 前者不重复 reinit/epoch；后者消费原真实决定的正式结果，不编 binding/baseline；副本原历史与业务结果保留 | pending |
| 原树独有变更；同文件双方冲突；partial 继承和未知缺失 | 独有成果不丢；冲突/相交未知不覆盖、不当删除；具体 pending 原因可读，本地读写测试仍成功 | pending |
| partial 清单含正常 `.git`/控制引用、non-atomic 标记或计划外未知项 | 可靠逐文件 B/R/C 计划仍自动接回；相交不可判定项才阻断相应计划；不推断缺项为删除 | pending |
| source/target 锁忙、计划后文件变化、路径替换与 symlink | 不偷锁、不越界、不覆盖变更；释放锁或重新对账后原事务自动推进 | pending |
| 每个持久边界崩溃、逐文件写入后响应丢失、重启 | 原计划幂等续办，已完成文件不重复破坏；原件、回执、历史和副本成果保留 | pending |
| normal admission/spawn/baseline/最终 flush 失败 | ready 不提前；正式补偿后副本仍能真实受管读写测试，后续自动再次尝试 | pending |
| Stop/new input/new session/current grant 竞争 | 旧尝试不写/不切/不派发；新消息可正常使用当前合法 consumer，旧边界不覆盖新边界 | pending |
| 正常 Git guard 与发布能力 | 测试经真实正常 launcher 完成 commit，并向隔离 bare remote push；撤权、错误资源/分支、未决发送负例拒绝；不以空 hook probe 冒充实际动作成功 | pending |

Rollback uses the existing release-writer and runtime-installer receipts; it must not reset business
state or delete recovery evidence. Verification of additional trapped bots follows their own actual
failure cases and must not be inferred from this first consumer.

Accepted design: PR377 at 4c60763c6a55f42279bd79f4812194393448454c. These tasks govern
current WI282 work; historical completion labels below do not prove these new gates.

| task | replacement | state | proof |
|---|---|---|---|
| C1 | StartJournal writer and content-derived request identity -> kernel request/step revisions | pending | same request corrects missing class; create once; crash/migration replay |
| C2 | global error exit -> preserved step receipts and versioned recovery result | pending | post-commit failure preserves work; invalid input returns fields; unknown is not retried |
| C3 | receipt-only delegation -> usable scoped execution and workspace rights | pending | B accepts A's rights and edits original dirty artifact; A cannot write transferred scope |
| C4 | old-state-gated reinit -> reachable request and minimum authorized correction | pending | no grant before consent; one real decision yields actual execution despite retired state |
| C5 | all-control-ready admission -> operation/resource-specific availability | pending | audit/measurement failure leaves unrelated edits and checkpoint working |
| C6 | parallel legacy writers -> per-WI migration and generation enforcement | pending | partial migration resumes, old binary cannot write, no old snapshot after new writes |
| C7 | source-only acceptance -> installed B01 then B02/B03 | pending | actual artifacts, identities, restart, fault, cancellation and migration evidence |

The existing three-document requirements/design/tasks set is the normative source. No new recovery
service, global lock or parallel authority database is introduced. The current human-authorized
escape contract permits a legal successor only when recovery requires it. Preserved 097
collaboration drafts are not this implementation and are excluded from delivery candidates.

### 第 0 节的不可拆分交付门

C1/C2 的局部通过不得替代 C3/C4/C5/C6 的权限闭环；不能以声明“当前只支持独占交接”来交付缺少
范围内转授、人类重新授权或登记落实的产品。实施可分提交，发布门不可拆开。顺序如下：

1. 固定失败回归：旧 YAML 的 executor 为空/过期、缺登记、退休调用者、接收者未获权、内核打不开。
   保留失败初态；禁止测试先改 YAML executor、预授接收方权限或替换权限检查来制造成功。
2. 在既有内核和交接记录实现统一授权来源、范围、版本与落实进度。先跑请求/决定/实际权限的正反例，
   再迁移所有消费者；不是只把一处 YAML 检查换成全局 kernel executor 检查。
3. 贯通普通 HO 与 reinit：范围内交接、再转授、独占接管、不重叠协作；缺权者经正常对话取得一项明确
   新授权后最小范围继续。原成果不变，旧方无需再批准，人类拒绝不产生新权限。
4. 一次性迁移登记、目录扩展、Host 绑定、存档/接续、Git guards 与外部动作发送。逐个登记调用点的
   原判据、授权来源、范围和测试；每个剩余 owner/executor 比较必须说明它检查的业务责任或旧路径边界。
5. 在已安装产物上执行下面的 B01 附加矩阵及已有未知效果/旧 writer 隔离用例。渠道入口、普通命令和
   恢复入口都走实际消费者，不靠内部函数成功推断其他路径成功。
6. 所有强制项通过后才更新技能的已支持行为并提出新的发布候选。首次独立真实场景验收不得承担发现
   已知授权闭环缺口的职责；用户验收仍单独记录，不由自动测试代替。

| B01 必测路径 | 必须观察到的业务结果 | 必须同时保留的反例 |
|---|---|---|
| UAT1：旧 YAML executor=null；先前 reinit/存档已成功，再执行 reinit | 一次调用完成必要登记修复；原 WI/目录内登记、编辑、测试、存档成功，原未提交成果不丢；原样重放不重复已完成步骤 | 不手改 YAML；无新授权者不能借“修登记”扩大范围 |
| 没有任何旧登记，或投影损坏/只读 | 已授权目录由产品登记/重建；只读失败明确为机器落实未完成，恢复后续办原决定 | 不先要求登记权限已经写进登记；不覆盖另一工作的成果 |
| A 将部分范围交 B，B 原先非 owner/executor，再在范围内交 C | 接受后直接执行受托动作；原责任不暗改；拒绝/未接受时原权限不被无故撤销 | 超出父授权范围或已撤回的委托被拒 |
| 独占接管与并行协作 | 独占旧 worker 不能继续相同资源写入；不重叠范围能并行修改并存档 | 接管与发送竞争用确定性暂停点验证，不能只检查状态字段 |
| 退休/无权者、旧 owner 消失、内核或帮助入口失败 | 普通对话可请求；一个真实决定或明确标识的测试决定即可落实新范围 | 无决定不提权；伪造 human flag、旧摘要、晚到旧决定不能覆盖新授权 |
| 授权已成立，投影/进程停止/存档/响应阶段故障 | 分别读回授权与完成步骤，重启后只补未完成动作；不重复人审、不重建无关前沿 | 外部效果未知时不重发；真实文件权限拒绝不得伪装为人类未授权 |

验收证据必须记录授权来源、实际调用者、资源范围、前后成果摘要、旧写入隔离、安装包摘要以及消费者
调用结果。`reinit_succeeded`、`execute_fenced`、内核 executor、checkpoint 计数和测试总数都不能单独过门。

## 2. A01–A26：行为、恢复与安全验收

每项均检查原 WI/业务请求、有效授权及其交接/重新授权来源、接受的输入修订、原成果和已完成步骤可追溯。缺权可以限制尚未获准的业务动作，不能限制向人类请示；新授权成立后验证权限实际落实，而非再校验被替换的旧许可。机器未落实与人类未授权必须分开。子场景属于原用例，不增加一套产品流程。

| 编号 | 故障与合法修复 | 必须证明的结果 |
|---|---|---|
| A01 | 既有启动草稿缺运营类别，Agent 根据 schema 补齐 | 同一 WI/请求完成修订，并在授权目录实际写入成果及 checkpoint；不重新确认已定归属，不重复 create；纳入 B01 |
| A02 | 类别拼写/枚举错误，按合法值修正 | 先校验再产生效果；全部可识别问题一次返回；不留永久冲突或原样无限重试 |
| A03 | 尚未消费的仓库选择错误 | 只分配已授权正确目录；已消费/超授权范围给精确差异和请示出口，不要求先取得所缺权限 |
| A04 | 标题修正、新会话或受托 Agent 继续原请求 | 身份不随文字/执行者变化；已创建字段用后续修订；同 WI 多个真实请求不误合并，交接来源保留 |
| A05 | 缺少 WI/worktree 权限、需要变更归属或资源范围 | 原请求中提出人类授权，权限/完整草稿缺失不挡请示；真实明确同意后一次落实，不重复索权；不同意则不增加权限；布尔值/历史摘要不冒充新同意 |
| A06 | checkpoint 字段/类型错误 | 修正后真实保存新内容；失败提交不污染原进度；源文件与存档引用一致 |
| A07 | 显式复用标识但改变内容、WI 或调用者 | 不静默返回旧成功、不串用凭据；受托者按交接范围读回旧成果，不必冒充原提交者；追加修订保留旧记录 |
| A08 | rollover 预留后改下一步；目标同时认领 | 未消费则原子修订，目标读取一致版本；已消费则后续存档；无新授权不复活旧凭据，仍保留人类请求权 |
| A09 | checkpoint 成功、prepare 失败 | 公共响应/查询找回第一步凭据，只恢复第二步；目标按业务下一步工作，不要求重建 WI |
| A10 | 主操作成功，读回/响应组装随后失败 | 包括提交后崩溃；重试不重复写，保留提交和授权事实，失败只限制剩余落实动作 |
| A11 | 跨机器导入成功后响应丢失 | 相同包返回原结果，后续进展不被覆盖；不要求旧缺陷重现；目录映射与权限落实分别验证 |
| A12 | 有效 HO 交接；接收者原无 owner/executor/worktree 权限 | A 可交自己所持范围；B 接受后实际读写/测试/存档原 WI 与目录，无第二次人审、另建 WI 或清空脏文件；不是只 ACK。超范围不自动授予但可请示；拒绝/未接受不假报交接完成 |
| A13 | 外部调用明确在执行前拒绝输入 | 可靠拒绝凭据证明零效果后修订；不只凭错误名；旧尝试保留，不永久卡 conflicting |
| A14 | 超时、暂时读不到、晚到成功、无查询/幂等 | 原身份观察，不当零效果、不自动重发；可以交接未决工作并向人类请示，具体未知动作保留，不冻结无关业务 |
| A15 | 分别注入网络、审计投影、复制、计量初始化故障 | 实际编辑/测试/保存仍可做；内核可写时 checkpoint 成功；交接与人类决定不因同步故障被否决；故障状态如实记录，恢复后只收敛一次 |
| A16 | 基线前进、旧记录迁移、迁移中崩溃 | 重验依赖，不静默换基线；映射可重复，完成凭据和交接/授权不丢；无双写；新写入后禁止旧快照回滚；迁移中请示仍可达 |
| A17 | 退休者、非 owner、新接收者；Host/日志/映射丢失 | 无新授权不自行夺权，但各身份都能请示；人类新授权或有效委托后按新权限实际执行，不被 retired_source_session 再否决；旧凭据不复活，有 create 凭据不重发 |
| A18 | 投影漂移、资源冲突、合法 worktree 交接 | 按现有授权/新决定修复或隔离；原未提交成果保留，无关他人文件不变；同机可复用原目录，交接不能强制全清理；人类已裁决不再要旧 owner 批准 |
| A19 | 目录扩展/交接已提交，投影失败 | 凭据可查；只补投影/实际权限，不重复转授或覆盖后来登记；明确决定生效与机器落实状态，不能让投影错误抹掉授权 |
| A20 | 未授权的 reinit 请求、已授权草稿缺字段、内核不可读/写 | 请示先于完整草稿/旧准入/内核初始化；读取器也坏仍可正常会话请求。人类同意后最小范围落实，不默认全量重建；缺旧历史不阻止新授权，也不造历史/假报保存 |
| A21 | 计量输入错误或初始化抛错 | 主业务、进展、交接与请示不丢；计量 unavailable；重取原证据，不伪造用量，不先修计量才继续 |
| A22 | 帮助/schema 不可用，或权限错误只有代码 | 无业务参数/旧绑定/可写内核仍能请示；帮助可读时给完整条件；无读取权不先泄露私有材料，错误响应不能只剩 permission_denied |
| A23 | 停止/收窄权限；worker 领取后或发送准入后暂停 | 新权限/取消先赢则旧 worker 不发送；准入先赢则保留 in_flight/unknown；人类请求不被未决效果堵住；晚到结果归原尝试，不报全撤销 |
| A24 | 两个修订者、交接与旧 worker 竞争、重新授权与退休绑定 | 独占范围只按新权利执行，旧写入被停止/隔离；并行委托按资源范围运行而不冻 WI；新授权无需旧方许可；租约过期不重发远端未知请求 |
| A25 | 已发布候选/终态丢响应，资源清理失败 | 采用原凭据不重发/回退；人类新决定追加，不篡改旧验收结果；清理按授权及归属，未知残留保留；终态也不禁止提出新授权请求 |
| A26 | CLI、TG、飞书、Hook 新旧版本与解析/展示失败 | 支持消费者保留交接、纠错、凭据及请示；不识别 reinit/Hook 出错时原消息进正常会话；新旧协议不双写。不得先升级或先获得业务权限才向人类请示 |

A14/A23/A24 用可重复暂停屏障测试，不靠随机 sleep。A16 覆盖 StartJournal、operation/checkpoint、outbox/effect 及其交接/授权关联，检查原始摘要、完成/未知步骤和最低兼容版本；至少一份从原失败形态构造的存量请求。A05/A17/A20 同时测试“未同意”“明确同意”“拒绝”：只有请求不增加权限，明确同意后不能再被旧 SCL 状态否决。自动测试的合成授权必须标明测试来源，不能写成真实人类已授权生产。

### Current implementation evidence (2026-09-09, candidate verification)

The existing kernel now stores action/resource grants with parent lineage,
redelegation limits, revocation and resource readiness. Ordinary HO offer/ack,
checkpoint contributions, managed child launch, Git commit/push guards, workspace
extension and scoped Session replacement consume these grants. Legacy owner and
executor fields remain compatibility projections, not recipient admission gates.

Authenticated Feishu/Telegram command inputs and Codex UserPromptSubmit capture
human decision receipts before lifecycle admission. Minimal reinit preserves the
business definition, responsibility and checkpoint history. Full reconstruction
requires missing semantic input. Existing dirty WI allocations can reconstruct a
missing registration; saved registration evidence repairs missing/corrupt projections.
Accepted authorization waits for managed writers without requesting human approval
again. Unknown external sends preserve their original request and block new sends
while unrelated local work continues.

Source evidence includes the same-artifact ordinary HO/revocation/human recovery
business test, actual managed child writes, scoped continuation, duplicate/late human
events, recursive revocation, Git path checks and a paused actual-send/readback race.
Relay regression: 1759 passed, 2 skipped; typecheck and build passed. Harness final
full-suite and isolated wheel verification are release gates and are recorded in the
release receipt. Synthetic authenticated inputs are test evidence, not production UAT.

B01 requires wheel-installed execution of these paths before adoption. B02/B03 and
actual channel UAT retain their separate evidence levels. A partial grant targeting
another surface stays pending until exact physical resource mapping exists; a source
path is never treated as a target-host path. No source test proves runtime adoption.

## 3. B01–B03：业务闭环，不用控制状态代替结果

### B01 · 第一个实现切片的交付门

同一受支持安装态 Host/渠道、同一测试 WI 和工作目录，用两名测试 Agent 处理一份实际业务文本；校验内容、未提交修改和摘要，从 checkpoint 引用读回。初始范围由测试授权给 A，B 起初没有该 WI/worktree 权限；不得用 monkeypatch 恒真权限检查代替真实交接。

主路径：原启动请求漏分类 → A 按返回信息补齐 → 实际修订文本并存档 → A 通过 HO 将所持工作权限交 B → B 接受，读取原未提交成果、继续修订并存档；不另建 WI、不换空目录、不要求人类再批准。独占交接后 A 不再写所交范围。

同一路径故障分支：分别使审计同步、计量不可用，B 仍能做无关本地修订；再构造 B 缺少下一动作所需权限或旧绑定失效，B 通过 reinit/正常对话请求一项明确人类授权 → 人类明确决定 → 产品最小范围落实，B 在原 WI/成果上完成下一动作，不先修旧绑定，不重复确认，不默认清空重建。真实人工决定只计这一次必要授权；自动测试采用测试决定时单列证据等级。

依赖恢复后原效果只收敛一次；重启查回同一 WI 的原请求、交接/授权和最新成果。启动、每次存档、交接及替换保留各自请求身份与关联，不把整个 WI 压成一项去重请求。

证据来自已安装产物、实际 Host 身份、真实目录和持久状态；可在隔离生产等价实例注入故障，不能模拟全部发布成功代替组合调用。真实生产采用另记，不将隔离结果写成 WI843 已恢复。本轮设计没有发起实际 HO 或 reinit；后续生产动作按当时有效授权办理。上述最高逃生、发送/取消和迁移保护在首个切片采用前即须通过，不能延期到 B02/B03。

### B02 · 接续、最后恢复与外部效果交付门

沿 B01 同一 WI/业务完成会话替换，新会话读取正确产物并执行下一动作；分别注入丢响应、HO 记录未同步、Host/启动日志损坏、活动内核打不开、最小恢复读取器失败、旧会话退休。每种情况下都测试：普通人类请求仍能到达；没有新授权不擅自写，有明确新授权不因旧状态再次被拒。

有可信历史时恢复原事实；无可信历史时保留缺失状态并落实人类明确指定的新权限，不伪造旧历史。物理权限失败时仅报告具体未落实动作及办理路径，不标成“人类授权无效”。

外部效果使用受控端点/仓库计数，不发送真实业务通知；结合 A14/A23/A24 暂停点证明不重复旧效果，未决效果保持 unknown，新执行者继承未决事实。无法证明旧请求停止时不报全部取消，也不禁止向人类报告和请示。

### B03 · 存量、支持范围与收口交付门

存量请求执行 dry-run、切换、重复切换、切换中崩溃、新写入后回滚拒绝及兼容包恢复。旧 worker/二进制不能再写迁移单元，已有交接与人类新授权不丢，迁移暂停也不能封锁普通会话。结束测试业务后按原验收/终态凭据和授权处置目录、临时分支，归档摘要不变、未知资源不删除。

扩展面逐一记录 Harness/PULSE SHA、包摘要、实际加载版本及正常/恢复用例；不外推其他机器/渠道。后续已变化的导入与计量路径纳入回归，不覆盖已合并修复。各支持渠道都验证解析失败或不识别 reinit 时的会话回退，不以客户端版本排除人类请求权。

## 4. 时间、调用与人类注意力预算

以下为验收预算，不是现有测量结果。实现前固定环境和边界，不在失败后仅放宽预算宣称通过。

| 项目 | 首个切片要求 |
|---|---|
| 普通字段错误 | 一次响应列全可确定问题；补齐后最多一次修订提交，无重复授权、手改库或源码救火 |
| 权限交接 | A 明确委托、B 接受即沿原范围办理；新增人工批准为 0；不让 B 先为同一权限另走一次首次准入 |
| reinit 请示与授权 | 缺权即能在当前响应提出一个明确决定，不先跑完修复；人类一次明确同意后无第二次同义确认，能局部更权就不全量重建；人类思考时间单列 |
| 本地入口与 checkpoint | 暖启动连续 20 次客户端 p95 ≤ 2 秒，控制依赖不可用仍适用；冷启动单列且 ≤ 10 秒 |
| 组合请求有外部等待 | 客户端 ≤ 5 秒返回事实/实际能力/下一动作；不等远端恢复才展示人类请示，模型生成和业务执行时间单列 |
| 后台恢复 | 确定输入错误零原样重试；暂时故障每触发最多 3 次有界尝试，之后等待依赖变化/低频观察；未知结果不自动转零效果；不反复向人类索取已拒绝权限 |
| 人工介入 | 普通纠错、丢响应、投影修复及范围内委托为 0；确需新权限时一个最小决定；不让用户复制 session/fence、搬状态或替产品排障 |
| 进展与收敛 | 授权已成立与机器尚未落实分别可查；预算用尽显示具体未决步骤及负责者，不假绿、不否决有效决定、不阻塞无关工作 |

B 类验收留存输入与安全化响应、接受的请求/权限/步骤版本、HO 与人类决定来源、原新凭据、业务文件摘要、外部效果计数、调用时间、人类介入及重启读回。代码、命令与结果绑定精确提交，通过 GitHub Actions 留存证据。测试脚本生成的授权和模拟对话不得标为真实生产人审。


## 2. Work breakdown

| id | task | state | exit gate |
|---|---|---|---|
| S0-001 | Audit current runtime, old Specs and real UAT counterexamples | complete | Every claimed current failure has source or isolated runtime evidence; incorrect legacy requirements are named |
| S0-002 | Complete requirements/design/tasks trio | complete | Cross-document terms, states, operation protocol and acceptance gates agree |
| S0-003 | Correct conflicting Golden Path/convergence guidance | complete | No normative text says an external effect or hook failure must stop ordinary local work |
| S0-004 | Non-overfit and feasibility review | complete | No WI/Agent/provider/host/repository special case in target logic; physical limits remain truthful |
| S0-005 | Commit and push Specs-only baseline | complete (`73f66b9b5e7`; correction `1b199d0e2d8`) | Exact staged diff is docs-only; remote branch readback equals local commit |
| S0-006 | Specify lineage cutover, `/wi reinit` and real continue readiness | in progress | Requirements/design/tasks plus PULSE ingress Specs agree; Specs-only commit is pushed before implementation |
| S1-001 | Define `scl_response/v1`, capability and stable error schemas | complete | Parser/preflight/runtime failures all emit one conforming envelope |
| S1-007 | Recover semantic input and resident Host first-entry gaps | source candidate | Invalid draft gives safe field diagnostics; new/existing unbound WI starts with Relay Host context; interrupted startup resumes same request; authorized reinit needs no old executor and inherits omitted Host fields; installed/channel readback remains required |
| S1-002 | Implement local kernel storage and migrations | partial | WAL, integrity snapshot and owned reserve pass; versioned migration/rollback fixtures remain |
| S1-003 | Implement operation journal and idempotent transaction API | partial | Exact retry, payload drift and stale precondition pass; generated concurrency/crash properties remain |
| S1-004 | Implement exact registration authority and fencing | partial | Bound exact registration and digest fencing pass; the full historical/ambiguous fixture matrix remains |
| S1-005 | Implement capability projector and truthful read model | partial | Local, pending, expired-fence and corrupt-read-only projections pass; full global conflict matrix remains |
| S1-006 | Project source identity and liveness from durable attachment evidence | pending | Ref mismatch, missing join and unknown state cannot project another active Session; positive distinct-source readback can |
| S2-001 | Route entry and checkpoint through the kernel | partial (installed checkpoint/rollover still have a legacy writer) | Offline local entry and new-decision checkpoint work without Git/GitHub/replication through one sealed frontier |
| S2-002 | Implement candidate withdrawal and explicit frozen-source adoption | complete | Human-facing adapter or contextual Session Agent submits the decision and rationale; kernel validates only machine preconditions, idempotency and fencing |
| S2-003 | Implement target-neutral staged Session replacement | complete | One public entry privately claims, captures the exact canonical first result, reads back and applies; same-entry retry recovers a durable pre-apply claim without Agent-supplied refs/digests |
| S2-004 | Make CLI and Hook fail-degraded | complete | Sealed discovery works, bounded matching inline-code presentation is losslessly unwrapped, prose/mismatches remain ordinary prompts and adapter failure never blocks conversation |
| S2-005 | Preserve privacy and user dirty provenance | partial | Session identity is opaque and lifecycle state is external; explicit installed dirty-byte manifest remains |
| S2-006 | Route Relay Feishu and Telegram through one sealed SCL ingress | complete (`c68d44a1da0` + PULSE `992dee498a6`) | Both channel handlers consume one channel-neutral result; no active path imports the legacy resume adapter; typed safe diagnostics survive end to end |
| S2-007 | Enforce sealed plan before dormant host-runtime activation | complete in PULSE `253c3aa4aac` | No-authority plan results survive stale/missing host runtimes; only authorized execution or replacement starts a process and requires exact readback |
| S2-008 | Route every `/wi` lifecycle verb through one sealed writer | pending | Entry, reopen, checkpoint and rollover read one frontier; checkpoint/rollover use Session-Agent semantic drafts and no legacy controller writes |
| S2-009 | Implement human-authorized `/wi reinit` root recovery | source candidate; installed legacy-adoption readback pending | Canonical WI + missing/retired executor, missing active kernel row, stale or absent registration and missing managed allocation preserve the business definition and accept one current human decision before resource realization; no model full draft is generated. Only typed `business_definition_unavailable` enters complete-draft rebuild. Realization failure retains the accepted decision and reports pending capability rather than requesting another reinit. |
| S2-010 | Make fresh-session `/wi continue` prove real readiness | pending | One composite operation restores the checkpoint and passes installed worktree plus Git-guard readback before returning success |
| S3-001 | Define common effect adapter and durable effect queue | complete | Provider-neutral contract tests cover apply/readback/unknown/conflict |
| S3-002 | Add resident/opportunistic reconciler | partial | Lease/retry/readback reconciler passes; resident startup/connectivity/periodic scheduling remains |
| S3-003 | Convert Git audit publication to an effect | partial | Rollover queues Git audit publication; concrete fenced Git adapter and recovery readback remain |
| S3-004 | Convert surface replication/readback to effects | pending | Partial endpoints retry independently; coverage remains truthful |
| S3-005 | Convert handoff/review/terminal external gates | pending | Valid scoped grants persist independently of network receipts; exclusive writes require realized fencing, and review/Outcome/terminal completion requires its own receipts |
| S3-006 | Bootstrap missing authority and settle attempt-zero effects | pending | Resident scheduling hydrates exact current registration, allocates/adopts the valid fence and settles effects without a user retry |
| S3-007 | Resolve Git authority and capability per durable effect registration | source complete; installed recovery pending | An unrelated process root cannot pin all effects; two-repository true-shape recovery settles the original effect and entry projects a concrete dependency result |
| S3-008 | Make Git audit write-path evidence and runtime push durable | source complete; installed replay pending | Receive-pack dry-run, exact stderr, adapter-owned fence/CAS pre-push, known rejection versus unknown outcome and historical-registration replay pass in source and installed runtime |
| S4-001 | Add verified LKG runtime activation and fallback | complete | Sealed installed LKG verifies and is selected independently of the current runtime pointer |
| S4-002 | Add emergency reserve, compaction and pressure policy | partial | ENOSPC reserve/recovery passes; compaction and sustained pressure policy remain |
| S4-003 | Implement legacy import and compatibility materializers | partial | Per-WI idempotent frontier import passes; compatibility materializers and real-fixture parity remain |
| S4-004 | Canary migration and rollback | pending | One WI can move/rollback without dual writer or effect loss |
| S4-005 | Hydrate an existing WI on bounded local-kernel miss | pending | Exact configured source imports WI/binding/checkpoint/continuation/replacement/writer parity atomically; conflict and true absence remain distinct |
| S4-006 | Perform release-level SCL lineage cutover | pending | Prior kernel state is checksum-archived and cannot schedule; new lineage starts empty and old WIs return only `reinit_required` |
| S4-007 | Atomically adopt and protect the canonical SCL runtime | source complete; installed adoption pending | Formal install repairs a dangling shim, direct readback proves the loaded revision and reclaim preserves every canonical active/valid-previous outer release |
| S5-001 | Model/property test authority and operation invariants | partial | Deterministic invariant tests pass; generated partition/concurrency campaigns remain |
| S5-002 | Run full crash/fault matrix | partial | Core local, ingress, runtime and storage faults pass; complete matrix remains |
| S5-003 | Build and seal runtime; verify exact installed bytes | complete | Final merged SHA is installed from an exact wheel; formal sibling entrypoints, source digests, shared-prefix separation and wrapped public entry pass outside the source tree |
| S5-004 | Isolated true-shape UAT | partial (remote-effect recovery missing) | Wrapped one-call public entry, exact first-result ownership, durable-claim recovery, source fencing and corrupt-store fallback pass; concrete remote-effect recovery remains |
| S5-005 | Re-run real UAT on the original WI | pending | Same WI continues under the new plan with decisions/artifacts intact; user judges result |
| S5-006 | Update lifecycle skill from shipped source contract | complete | Installed Skill 2.3 forbids Agent-level claim/confirm/digest emulation, consumes lifecycle orientation separately and preserves the checkpoint business frontier |
| S5-007 | Install and UAT Relay plus SCL on all resident nodes | partial (three-node install verified; visible UAT1/UAT2 failed) | ownermac, cloud-linux-a and cloud-linux-b read back exact compatible manifests and rollback releases; every actual resident Feishu/Telegram route passes visible positive and typed-negative probes |
| S5-008 | Regress UAT2 source identity, cold-miss and writer-split shapes | pending | Synthetic/data-injected WI568/WI282-shaped fixtures pass source attachment, positive liveness, hydrate parity and single-writer assertions before another human UAT |
| S5-009 | Regress reinit and continue with copied split-frontier shapes | pending | WI568/WI282/WI479-shaped data passes reinit, same-session rollover, fresh-session continue, worktree validation and disposable push without production-WI logic |
| S5-010 | Update and forward-test lifecycle skill after product gates | pending | Skill describes reopen/reinit/rollover/continue truthfully and an independent realistic evaluation does not infer authority from stale state |
| S5-011 | Migrate ordinary Linux workflows to registered trusted runners | source complete; Actions readback pending | Adjudicate and ComputeCost lineage contracts run on `pulse-linux-trusted + pulse-control-plane`; no ordinary CI requests GitHub-hosted Linux or a redundant macOS job, while the explicit manual GitHub UAT path remains separate |
| S5-012 | Make private cross-repository adoption credential-safe | source complete; Actions readback pending | PULSE publishes one exact digest-bound minimal snapshot on the trusted pool; Harness sparsely materializes only its package, verifies schema/repository/revision/archive/member digests and never assumes its repository token or an undeclared SSH key can read PULSE |

`@anchor manual resilience-delivery-plan :: each task keeps exact test commands, receipts, installed revision and outstanding unknowns; state changes require the named exit gate`

## 3. Slice boundaries

### Slice A — local kernel, no external mutation conversion

S1 introduces storage, operation semantics, exact registration selection and capability responses
behind an opt-in seam. Existing production writes remain unchanged until parity and migration tests
pass. This slice must already prove that the kernel itself does not depend on Git worktree health.

### Slice B — local continuity path

S2 moves `/wi`, checkpoint, explicit candidate withdrawal and replacement to the kernel. The first
user-visible gain is offline/degraded local continuity. It does not yet claim automatic global
convergence until Slice C effects ship.

### Slice C — external effects and reconciliation

S3 moves publication, replication and authority-crossing gates behind the durable effect queue. No
adapter is accepted without timeout-after-commit readback and idempotent retry tests.

### Slice D — survival and migration

S4 makes the path operationally durable under runtime and storage failures, seals the unprovable
mixed lineage as read-only evidence, and activates one clean live lineage. No prior-lineage effect or
authority survives that cutover. Individual WIs re-enter only through human-authorized reinit.

### Slice E — proof and real UAT

S5 validates the generic model first, then the installed runtime, then an isolated true-shape
workflow. Only after those pass is the original real WI used to repeat UAT. The UAT remains
human-directed; implementation green cannot substitute for the user's result.

`@anchor manual slice-gate-review :: do not start a later slice when an earlier slice's safety gate is red; availability improvements may ship disabled but cannot weaken old global-authority safety`

## 4. Required test matrix

| dimension | required cases |
|---|---|
| process boundary | before local begin, during WAL write, after commit/before response, during each effect, during materialization |
| concurrency | duplicate same intent, changed payload same key, two local Sessions, two sites under partition, stale publisher |
| remote | DNS/network down, permission denied, CAS moved, completed exit 1 with stderr, timeout before send, timeout after commit, partial replication, receive-pack dry-run |
| registration | exact active, retired first in filename order, duplicate historical, digest drift, generation mismatch |
| reinit | canonical WI with missing active kernel row, legacy `active_executor=null`, stale registration executor, no registration/allocation, resource realization failure after decision commit, truly absent business definition, stale binding, applied replacement, absent authority, dependency-free unbound revision-zero placeholder, non-empty current row, incomplete full-rebuild draft, exact retry, process kill, retired-lineage resurrection attempt |
| continue | warm/cold target, claim crash, attachment crash, authority unavailable/restored, worktree validation failure, guard failure, old-source retry |
| ingress | Hook absent, Hook timeout, import error, stderr-only dependency, escaped underscore, matched inline-code wrapper, mismatched/prose negative cases, ordinary non-command text, sealed sibling discovery without override, venv/base-prefix separation, same-intent retry |
| runtime | mixed modules, corrupt candidate, failed handshake, restart after activation, LKG fallback |
| installed adoption | dangling canonical shim, Harness no-op reinstall, staged handshake failure, active readback mismatch, outer-release reclaim protection |
| storage | low watermark, ENOSPC with reserve, ENOSPC after reserve, corrupt projection, corrupt kernel/read-only fallback |
| workspace | tracked dirt, untracked drafts, symlink, nested repository, missing remote-only artifact |
| lifecycle | one-call public entry, exact private first-result capture, checkpoint business frontier, lifecycle orientation consumption, candidate withdrawal, target-neutral replacement at every stage, handoff, terminal pending and completion |
| source identity | current-format attachment, provable legacy join, unresolved legacy join, contextual same-source attachment, positive distinct-source liveness, stale/missing Host observation |
| cold kernel | existing exact WI hydrate, true absence, changed import bytes, continuation/replacement disagreement, duplicate writer, exact retry |
| command ownership | entry/reopen/checkpoint/rollover share one writer; Session Agent supplies checkpoint/rollover semantics; channel adapter cannot synthesize a draft |
| presentation | mutation/no-mutation first, durable frontier safety, one next action, optional diagnostics; no positive human claim from mismatch/absence/unknown |
| truth model | local/global revisions diverge, unknown, pending, partial, conflict, recovery, exact completed evidence |
| effect routing | unrelated process root, two registered repositories, zero/one/multiple exact WI-local roots, attempt-zero and attempt-N dependency restoration |

Tests MUST assert unaffected capabilities remain available and user bytes remain unchanged, not only
that an error code was returned.

`@anchor manual resilience-fault-matrix :: test inventory maps every cell to an executable test or a documented physical impossibility; no case is waived because a provider is unavailable`

## 5. Implementation guardrails

- No direct Git-resident lifecycle write may be introduced as a shortcut around the journal.
- No effect adapter may run inside the local transaction.
- No retry loop may require a user to retype the operation or copy a recovery reference.
- No capability may be disabled only because an unrelated dependency health bit is red.
- No recovery path may reset, clean, stash, overwrite, rebase or absorb user work.
- No test may hardcode a production WI, owner, provider, host or absolute repository path as product
  logic; true-shape fixtures inject these as data.
- No `completed` projection may be derived from object presence alone.
- No source-tree test may be reported as installed-runtime acceptance.
- No installed Hook test may inject a client-path override absent from the production command and
  then claim the production discovery path passed.
- No skill text may be used to compensate for a missing kernel invariant.
- No ref mismatch, missing source attachment or unknown Host observation may be rendered as proof
  that another Session is active.
- No installed lifecycle command may read or advance a second legacy frontier after kernel-writer
  cutover.

`@anchor manual implementation-guardrails :: structural tests and review checklist enforce all guardrails before each slice is enabled`

## 6. Evidence and release decision

Each slice records:

- exact Specs revision and implementation revision;
- test command, environment and exit result;
- fault schedule and operation/effect ids;
- local/global revisions before and after;
- installed artifact digest and live readback;
- preserved-dirt manifest where applicable;
- known unknowns and disabled capabilities.

Release is reversible by runtime pointer, but data migration is forward-preserving. Rollback cannot
erase operations or pending effects. A slice may be declared `partial`; it may not be called
complete while its installed or user-visible gate is missing.

### 6.1 Evidence snapshot — 2026-08-29

- Specs boundary: `73f66b9b5e7`; target-neutral correction: `1b199d0e2d8`.
- Installed source revision: `4b0a8d18bec039f926222d2d6902ec0db05fa576`.
- Installed wheel SHA-256:
  `838277133c04f6616ed02a11cbf3cf55235baca28b46665cf1054a0d0f0e3f92`.
- Active outer release:
  `/Users/owner/.local/state/agent-harness-runtime/releases/4b0a8d18bec0-838277133c04f661`;
  the previous release remains retained and the installation receipt is
  `/Users/owner/.local/state/agent-harness-runtime/receipts/apply.20260829T084933090101Z.4b0a8d18bec0-838277133c04f661.json`.
- Verified SCL LKG digest:
  `e095b43fe6be78dba0963e229e56a50cc53a55dc90460730313e7e815e415124`;
  its read-only handshake returned `scl_response/v1`.
- Installed black-box receipt: target epoch `2`, source fenced, two decisions retained, corrupt
  kernel fell back to verified read-only state, and Hook did not block the prompt. The fixture WI was
  `wi_20260829_990` in an isolated temporary root; no production WI was read or changed.
- Full suite command: `.venv/bin/python -m pytest -q --no-cov`; result:
  `2581 passed, 1 skipped, 4 subtests passed` in `235.51s`.
- Installed lifecycle skill SHA-256:
  `bb043c59565c5f88630d56b189eda8a0ba9b269b29e3a54216967494c580ece3`;
  prior installed skill SHA-256 `5e0d29dc4e93c33d8770d3a63c8a3bd69e4673a0ecb9d1524637016209878b41`
  remains under `.codex/skills/.backups`.
- Outstanding boundaries: concrete Git/surface/terminal effect adapters, resident scheduling,
  frozen-source adoption exposed by real UAT, migration canary, the complete generated fault matrix,
  and human-directed UAT on the original WI.

### 6.2 Corrective evidence — frozen-source adoption and integrated runtime

This subsection supersedes only the installed/runtime/source-adoption facts in 6.1; the earlier
snapshot remains historical evidence rather than being rewritten after the fact.

- Specs correction: `a83ca4a9c7800f85e83813f86bc5a36b1672e96f`; implementation:
  `d930ee1633ea2d2511f7f50578582b2e55404c49`; integration with the current
  `feature/doc-para` lineage: `51023e3b3cf994e45fc94fe97de7e055736444a1`.
- The first `d930ee1633ea` install discovered a newer WI479 runtime already active. It was immediately
  reverted by the official receipt
  `/Users/owner/.local/state/agent-harness-runtime/receipts/rollback.20260829T094326235263Z.json`;
  after source integration, the combined runtime was installed once. No concurrent delivery was
  silently overwritten.
- Active formal release:
  `/Users/owner/.local/state/agent-harness-runtime/releases/51023e3b3cf9-3583fc2c4171bf25`.
  Wheel SHA-256 is `3583fc2c4171bf251c3b2f3cac479a3137329864d45b6fcebd8815fda8b3a704`;
  artifact-manifest SHA-256 is
  `eab24d43a2850133fdf22130c5dd5a1d3cc98167f1974c5971d88b1fdecd44fe`;
  apply receipt is
  `/Users/owner/.local/state/agent-harness-runtime/receipts/apply.20260829T094945220974Z.51023e3b3cf9-3583fc2c4171bf25.json`.
- Active verified SCL LKG digest is
  `8a33103afb5e54bf69f7ea071dc72b201d51465f2c580960e1780c119e49f226`;
  previous `e095b43fe6be78dba0963e229e56a50cc53a55dc90460730313e7e815e415124`
  remains retained. Formal `harness-scl handshake` returns `scl_response/v1` from the sealed release.
- Exact installed black-box command uses the formal `python`, `harness-scl` and
  `harness-codex-hook` against `tests/installed_scl_uat.py`. Its synthetic receipt is:
  `source_adoption_status=local_committed`, `adoption_epoch=2`,
  `rollover_status=local_committed`, `target_epoch=3`,
  `target_entry=execute_local_isolated`, `source_fenced=true`,
  `checkpoint_decisions=2`, `corrupt_kernel_fallback=degraded`, `hook_blocked=false`.
  The isolated fixture remains `wi_20260829_990`; it did not mutate the real WI825.
- Integrated full suite: `.venv/bin/python -m pytest -q --no-cov` returned
  `2602 passed, 1 skipped, 63 warnings, 4 subtests passed` in `224.20s`. Targeted Ruff over the
  SCL adoption slice and integrated `workspace_sync` files returns `All checks passed!`. Full-repo
  Ruff still reports ten pre-existing mainline test errors outside this slice; they were not hidden
  or rewritten as SCL failures.
- Shipped Skill revision: `37a682858573fac48d87ba8f78dc5dae07b60c70`; installed
  `work-item-lifecycle` version `2.1.0` `SKILL.md` SHA-256 is
  `1a7830c57e5cf173c21f7a245b8306a65ac47cb584705b3447951443f15d94a1`.
  The previous installed version remains at
  `/Users/owner/.codex/skills/.backups/work-item-lifecycle.pre-source-adoption.sha-bb043c59`.
- Still not accepted: the human-directed rerun on `wi_20260826_825`, concrete remote-effect
  recovery, resident scheduling, migration canary and the complete generated fault matrix.

### 6.3 Real Hook regression — 2026-08-29 18:07+08:00

- The real `/wi reopen wi_20260826_825` reached Codex but returned
  `SCL_HOST_ADAPTER_DEGRADED: installed local SCL client is unavailable`; no lifecycle operation was
  committed and WI825 remained `awaiting_verification`, epoch 2.
- Root cause: `_local_client_command()` resolved the venv `sys.executable` into the shared CPython
  prefix before looking for `harness-scl`. The matching client existed in the active sealed venv.
- The installed synthetic UAT set `SCL_LOCAL_CLIENT_EXECUTABLE`, masking this production-only path;
  therefore the prior installed-UAT acceptance claim is withdrawn.
- The recovery Session then created a soft link inside the shared CPython prefix. That link is not a
  product fix and was moved intact to
  `/Users/owner/.local/state/agent-harness-runtime/recovery-artifacts/harness-scl.shared-python-symlink.20260829T1814`;
  the shared prefix is clean again.
- `human_confirmed` and `human_intent_ref` attempted to turn semantic judgment into a mechanical
  gate. The program cannot know human intent; those fields are removed from the authority decision.
  A human-facing adapter or contextual Session Agent submits the explicit lifecycle decision and
  rationale. The kernel records provenance but validates machine state, idempotency and fencing;
  it neither certifies intent nor asks a user to copy/repeat an internal reference.

### 6.4 Contextual-intent correction and formal installed evidence

This subsection supersedes the invalidated consumer-path and intent-gate claims above; it does not
claim the pending human-directed WI825 rerun.

- Requirements/design correction: `d6f3feb6775`; implementation: `274e962cfc4`; Skill 2.2 source:
  `e1f0561e384`; integration with the installed WI479 lineage and latest `feature/doc-para`:
  `e741be2c7a9bffc266d3a2479b02a1caf3970226`.
- `CandidateWithdrawalDecision` records the asserted decision kind, `decided_by=human|session_agent`
  and rationale. Those fields are audit provenance, not intent attestation. Kernel admission checks
  WI/candidate, owner, binding, replacement, runtime/surface, epoch, fence and idempotency only.
- Full integrated suite: `.venv/bin/python -m pytest -q --no-cov` returned
  `2605 passed, 1 skipped, 63 warnings, 4 subtests passed` in `205.72s`; targeted SCL Ruff returned
  `All checks passed!`.
- Exact wheel SHA-256 is
  `bd8ed360719e2c02a697b411680c9c70e01eca9232298dfd245555279273bbd0`; artifact-manifest SHA-256
  is `c89aaf88976c50e7e10c3b2e3b417dfead0efdca23782ec47a9a06a3d0918a99`.
- Formal sealed release is
  `/Users/owner/.local/state/agent-harness-runtime/releases/e741be2c7a9b-bd8ed360719e2c02`; apply
  receipt is
  `/Users/owner/.local/state/agent-harness-runtime/receipts/apply.20260829T104600681263Z.e741be2c7a9b-bd8ed360719e2c02.json`.
  Prior WI479 release `19295750fec0-a5d1e0cbb97b5c46` remains retained and rollbackable.
- Formal installed UAT removed `SCL_LOCAL_CLIENT_EXECUTABLE`, invoked the real sealed
  `harness-codex-hook`, and returned `source_adoption_status=local_committed`, `adoption_epoch=2`,
  `rollover_status=local_committed`, `target_epoch=3`, `source_fenced=true`, two retained decisions,
  degraded read-only corrupt-store fallback and `hook_blocked=false`. The shared CPython prefix was
  clean before and after. Only isolated fixture `wi_20260829_990` was mutated; WI825 was untouched.
- Installed `work-item-lifecycle` version `2.2.0` `SKILL.md` SHA-256 is
  `cf744d4545da0becb4b2400e22dfab899b19197bf8cb93ab107cf3af8269d144`; version 2.1.0 is preserved at
  `/Users/owner/.codex/skills/.backups/work-item-lifecycle.pre-contextual-intent.sha-1a7830c5`.
- Still not accepted: the human-directed rerun on `wi_20260826_825`, concrete remote-effect
  recovery, resident scheduling, migration canary and the complete generated fault matrix.

### 6.5 Real successor-entry regression — Session `01a04d33-edd1-7ee0-82e0-fdd87d334bb6`

- Reopen and rollover succeeded before this Session. The new Session's raw user message was the
  exact command wrapped in inline-code presentation: `` `/wi wi_20260826_825` ``. The installed
  Hook grammar rejected the backticks and injected no machine response.
- The Session Agent then located the internal module and manually ran `enter-local`. The first call
  durably claimed the reserved target. A later retry produced a different response and the Agent
  supplied that retry's digest plus private replacement/frontier refs to
  `confirm-replacement-local`. Kernel state reached epoch 4 with `stage=applied` and the old source
  retired, but this does not prove the required first settled result and is not an accepted public
  entry path.
- The final ResumePacket correctly projected `execute_local_isolated`, local execution authority
  and pending external effects, and it preserved checkpoint decisions/pending/risks. However its
  human-authored `next_action` still began by telling the already-active successor to open a fresh
  successor Session. The Agent ignored that stale sentence and continued read-only business probes;
  product correctness cannot depend on that interpretation.
- This correction keeps the successful WI825 local state intact and does not request another
  reopen/rollover. Implementation and sealed verification use synthetic WIs; the user will select a
  different real object for the next human UAT.

### 6.6 Product-owned successor entry and formal installed evidence

This subsection resolves the three product regressions recorded in 6.5. It does not claim that the
next human-directed UAT on a different real object has passed.

- Specs-only ownership boundary: `a40fb3f811e`; implementation: `95e2b8721be`; Skill 2.3 source:
  `d44c64da78f`; integration with the latest `feature/doc-para` lineage:
  `9114f1874138f5b35ed53a1b8451e743aa6d87fc`. All four commits are pushed.
- Public `enter`/`enter-local` is now a single client-owned operation. The client privately performs
  claim, exact canonical first-result capture, runtime readback and apply, then returns one final
  envelope. Public `confirm-replacement[-local]` and Agent-supplied private refs/digests are removed.
  A regression starts from a durable `target_claimed` frontier and proves that repeating the same
  public entry finishes apply without human recovery material.
- Hook parsing accepts only the exact `/wi` grammar or one bounded, matching, single-line inline-code
  wrapper around it. It still normalizes escaped underscores only inside the proven WI token; prose,
  trailing text, mismatched fences and multiline code remain ordinary prompts.
- Checkpoints persist `business_next_action` as post-entry work. ResumePacket independently projects
  `lifecycle_next_action`; after successor activation it is null and compatibility `next_action`
  resolves to the preserved business frontier rather than another replacement instruction.
- Final integrated source suite: `.venv/bin/python -m pytest -q --no-cov` returned
  `2614 passed, 1 skipped, 63 warnings, 4 subtests passed` in `205.82s`. The merged targeted slice
  returned `68 passed`; Ruff and format checks passed; Skill validation and Golden Path contract
  returned `PASS`.
- Exact wheel SHA-256 is
  `29980ee0c275f5c47b0c7ee5def45eeccba84b73c539275e941257914df0fd92`; artifact-manifest SHA-256
  is `5949c36d86499d7dbdb0e66e36814927ff8c5b53d43d602308a7f10cb3ea7889`.
  Formal sealed release is
  `/Users/owner/.local/state/agent-harness-runtime/releases/9114f1874138-29980ee0c275f5c4`;
  apply receipt is
  `/Users/owner/.local/state/agent-harness-runtime/receipts/apply.20260829T113945330757Z.9114f1874138-29980ee0c275f5c4.json`.
  Prior release `e741be2c7a9b-bd8ed360719e2c02` remains retained and rollbackable.
- The candidate and formal installed UAT both ran outside the source tree without
  `SCL_LOCAL_CLIENT_EXECUTABLE`. The raw target prompt was the real presentation form
  `` `/wi wi_20260829_990` ``. Both receipts returned target epoch `3`,
  `target_entry=execute_local_isolated`, source fenced, two decisions retained, corrupt-kernel
  fallback `degraded` and `hook_blocked=false`. The shared CPython prefix had no `harness-scl`
  shim before or after.
- Installed `work-item-lifecycle` version `2.3.0` `SKILL.md` SHA-256 is
  `2c8eb83e72784c3194265ee7bce84d5648f944eb715a665c4def27f193ad5c71`.
  Version 2.2.0 remains at
  `/Users/owner/.codex/skills/.backups/work-item-lifecycle.pre-product-owned-entry.sha-a1ecacdd`.
- This iteration did not read or mutate WI825 lifecycle state. Still pending are the user's UAT on a
  different real object, concrete remote-effect recovery, resident scheduling, migration canary and
  the complete generated fault matrix.

`@anchor manual resilience-release-evidence :: independent review can recompute every claimed revision, digest, state transition and preserved-byte assertion without reading a chat transcript`

### 6.7 Three-node channel-ingress regression — 2026-08-29

- A real Vultr Pulse Feishu command `/wi wi_20260820_568` returned only
  `WI continue 失败 [resume_failed]。` while the downstream Harness reason was the deterministic
  `identity_mismatch` caused by a stale registered PULSE branch identity. The command did not mutate
  the WI binding or create a durable Relay entry plan.
- Source inspection proves Feishu and Telegram already converge on `handleWorkItemCommand`, but the
  shared `ExecFileSclResumeClient` still invokes the release-bundled `Scripts/scl-resume.py`, which
  delegates to legacy `harness continue`. `formatFailure` then discards the downstream safe reason.
- The active ownermac Relay release contains the legacy script even though a separate sealed
  `harness-scl` release exists on that host. The active cloud-linux-a and cloud-linux-b Relay nodes
  use independent Harness virtual environments and neither exposes `harness-scl` at the configured
  runtime path.
- The source correction is Harness `c68d44a1da00a8f8614242a65696f19f81358b81` plus PULSE
  `992dee498a642791cfe99279be3ab8885cd77c0b`. `plan-entry-local` is read-only, a replacement entry
  requires a real host-runtime observation before its sole mutating call, and a legacy
  `transitioning` frontier imports as a target-neutral reservation without claiming it.
- Harness full-suite source evidence is `2615 passed, 1 skipped, 63 warnings, 4 subtests passed`;
  targeted Ruff and format checks pass. PULSE typecheck passes, its SCL/channel slice is
  `181 passed`, and its second full-suite run is `1512 passed, 1 skipped`.
- This establishes S2-006 at source level. The paired immutable installs now read back on all three
  nodes, but S5-007 remains partial until every actual resident Feishu/Telegram route has the user's
  visible result. Runtime discovery found `2/12` Feishu/Telegram bots on ownermac, `10/1` on
  cloud-linux-a and `7/0` on cloud-linux-b. The earlier six-cell demand is withdrawn because it
  would require inventing a duplicate Telegram poller rather than testing production truth.

### 6.8 UAT2 truth-split and source-identity regression — 2026-08-30

This subsection supersedes only the prior claim that active-route UAT was still pending. UAT2 ran
and failed; the installed three-node bytes remain historical deployment evidence, not product
acceptance.

- On the real WI568 Feishu route, sealed entry returned read-only `source_session_active`, epoch 8
  and checkpoint r30. The user reported that work had remained in the same channel Session, so the
  human explanation that another Session was executing the WI was false.
- Read-only inspection found two frontiers for that WI: the sealed kernel projected epoch 8/r30,
  while the configured legacy status path projected epoch 7/r19. The logical bound source did not
  equal the current Host Session candidates, neither side had a durable runtime binding for this
  WI, and no positive readback proved a distinct live source.
- The installed entry classifier treated `bound_session_ref != current_session_ref` as sufficient
  for `source_session_active`. A ready legacy continuation also coexisted with sealed
  `replacement_stage=none`. Both are control-plane truth defects, not user workflow mistakes.
- The real WI282 `/wi rollover` command reached the sealed adapter but returned
  `work_item_not_found` because its local kernel lacked the WI. Read-only canonical status still
  showed an active epoch 1/checkpoint r5 frontier. The rejection created no operation, effect,
  checkpoint, reservation, epoch or binding change.
- Source inspection found entry routed through sealed local planning while installed checkpoint
  and rollover still called a legacy controller. Therefore one WI can be classified from one
  frontier and mutated through another writer.
- The channel response exposed raw enums and fields without first stating that nothing changed,
  the durable frontier was preserved and what single safe action came next. Presentation amplified
  the contract defects and must be fixed from machine evidence rather than Skill wording.
- No production WI is a test fixture. Engineering gates use synthetic/data-injected copies of
  these shapes, pass source and exact installed-runtime tests, then pass every actual resident
  Feishu/Telegram route before the user chooses the next human UAT object and order.

### 6.9 WI282 post-apply attachment self-lock — 2026-08-30

- Real WI282 replacement claim and apply committed once and advanced the canonical successor to
  epoch 2, but the fresh Agent later received `source_identity_unresolved` and reported
  `read_only/no mutation`. That report contradicted the operation journal and hid a completed
  lifecycle mutation.
- The applied successor retained one durable Host attachment, while the subsequent Host identity
  did not match it. `attach_current_host_to_source` then rejected mechanically because it accepted
  only `replacement_stage=none`; an applied successor therefore had no bounded recovery path.
- Add an applied-successor attachment-rebind operation with exact owner/executor, runtime/surface,
  frontier and old-source fencing checks. A current Host identity found on any older epoch is
  rejected. Rebind changes no WI frontier field and is followed by installed worktree/Git readback.
- Make Codex lifecycle parsing retain explicit `continue` and `reinit` verbs, and make response
  tests prove a committed claim/apply can never be summarized as `read_only/no mutation` because a
  later attachment or consumer readback degraded.
- Run source tests, sealed installed-runtime tests and ownermac exact installation/readback before
  retrying the real fresh-session consumer. Vultr/TCloud and Relay production actions remain
  frozen; no production WI storage or legacy ledger is edited.

### 6.10 WI479 runtime-adoption and effect-routing regression — 2026-08-30

- A stable Harness executable at revision `f88dc45...` was callable, while the canonical SCL shim
  still targeted a reclaimed outer release and failed with `No such file or directory`. Therefore
  stable installation and canonical runtime adoption are separate gates.
- The same installed client projected `external_publish=pending / external_dependency_unprobed`
  because entry supplied no concrete dependency observation.
- The resident reconciler was launched with a process-global development Harness root. The durable
  WI registration contained two repositories and only one exact WI-local Harness root, yet the Git
  adapter rejected the effect as `authority_repository_unresolved` for repeated attempts.
- Corrective acceptance requires one bounded per-registration Git probe, one repository decision
  per effect, formal canonical-shim activation/readback and reclaim protection. The existing effect
  must then settle automatically and be observed read-only; no reinit, database edit, legacy-ledger
  edit or replacement product commit is permitted.
- Source implementation now resolves the exact registered repository containing the WI-local
  Harness root, performs a bounded non-interactive dependency probe for capability projection, and
  lets the resident reconciler route every effect independently instead of accepting a process-wide
  authority root. A true-shape two-repository test restores the dependency after attempt 1 and
  reads the same effect back as `succeeded` on attempt 2 while preserving unrelated dirty bytes.
- The formal Harness release installer now stages and atomically activates a sealed canonical SCL
  wrapper, repairs a deliberately dangling active pointer, reads back the exact source revision and
  protects the outer Harness releases referenced by canonical active/valid-previous wrappers from
  reclaim. Source gates: targeted SCL 42 passed, runtime installer 11 passed, full Harness 2652
  passed / 1 skipped / 4 subtests passed, targeted Ruff clean and compileall clean. Installed
  ownermac/three-node adoption and existing-effect readback remain deliberately open.

### 6.11 Post-adoption effect, empty-reinit and runner regression — 2026-08-31

- The resident reconciler leases Git audit effects, but a push from its adapter-owned detached
  worktree exits 1. The same registered worktree can run a direct no-op push dry-run successfully;
  the detached path reproduces `HARNESS_BRANCH_DENIED:git_probe_failed` because Git exports the
  detached `GIT_DIR` to a worktree-scoped pre-push guard. Existing attempt evidence retained only
  `push_exit_code=1`, losing the decisive stderr and misclassifying a completed rejection plus
  absent readback as unknown.
- The external capability probe currently proves only `ls-remote`. It must also run a bounded
  receive-pack dry-run through the adapter-owned fence/CAS gate. Apply must publish only the
  deterministic audit path, without force or human-worktree hooks, and retain bounded stdout,
  stderr, byte counts, digests and truncation state. The original pending WI282 effects are replayed
  by the resident scheduler after installation; no database edit or replacement operation is used.
- Real WI282 pre-install readback additionally shows its registered local ref ahead of the remote
  branch while the remote remains a linear descendant of the fenced authority base. This is safe
  audit recovery, not product-history conflict: apply starts at the exact remote tip, adds only the
  receipt and uses a fresh no-force CAS. A remote not descended from the authority base still
  conflicts, and no human local ref is moved to make the effect green.
- A copied real state shape contains an `in_progress` WI row at local revision zero with
  `execution_state=unbound` and no epoch, Session, checkpoint, operation or authority. This is an
  imported placeholder rather than a frontier. Reinit may replace exactly that dependency-free
  shape while binding its state digest as the atomic precondition; every non-empty row remains
  protected from overwrite.
- `adjudicate-spawn-ci.yml` still requests `ubuntu-latest` and `macos-latest`. The tested script is a
  retired Bash entrypoint with no platform-specific contract, so the redundant matrix is removed
  and its single job explicitly uses `pulse-linux-trusted` with `pulse-control-plane`. A zero-step
  hosted-runner billing failure is not product-test evidence.
- The first formal Linux adoption run resolved the exact Harness candidate but failed before
  validation because `PULSE_READ_TOKEN` was absent and the claimed credential-helper fallback had
  no credential (`could not read Username`). The trusted Linux fallback now uses the same bounded
  BatchMode/strict-host-key SSH read path as the registered Mac runner. The first SSH read then
  failed closed because that runner had no GitHub ED25519 known-host entry. The workflow now writes
  GitHub's officially published ED25519 public key to a task-local mode-0600 file, verifies its
  published SHA-256 fingerprint with `ssh-keygen`, and passes that exact file to SSH. It still
  fetches only the requested exact PULSE SHA and never prints credential material.
- Source implementation now passes the combined SCL/effect/reinit/workflow slice (35 tests plus
  4 subtests), exact local Git receive-pack rejection and credential-redaction tests, Ruff and the
  full Harness suite (2656 passed, 1 skipped, 4 subtests). Immutable installed-runtime, Actions
  runner assignment, resident WI282 replay and three-node adoption remain open and are not inferred
  from these source results.
