# SPEC：Relay 主链 — 请求进入 → 路由 → 送达 → 回执

> version: `0.3.6`
> status: `reverse_engineered_from_code`
> authority: Relay 消息主链的**现行实现契约**（由代码反推）
> owner: Pulse（wi_20260804_070 · KR2a）
> modified: `2026-10-06`
> 版本史：只看 git log

**取证基础**：2026-08-09 代码普查（file:line 逐条）+ 2026-08-25 WI938
跨 runtime/channel rendering 收敛与 spec-anchor 机器验证。
**结构事实**：TG/飞书入口与出站仍在两个 channel 文件里；会话获取与恢复已由
`conversation-acquisition.ts` 承担，index 负责组装接线。「回执」体系
（delivery 模块的 receipt 工厂）目前**只覆盖 cron/推送侧，不覆盖用户对话主链**——
主链送达无回执留痕，这是本 spec 登记的头号缺口。

## 1. 组装入口

- [x] 唯一顶层入口与配置解析链：显式参数 → 环境变量 RELAY_CONFIG_PATH →
  /opt/PULSE/data/relay.json
  `@anchor symbol startRelay`
  `@anchor symbol createRouter`
  `@anchor symbol createTelegramBots`
  `@anchor symbol createFeishuBots`

PID 文件只用于本实例的恢复判断。活 PID 还须核对原生进程的 elapsed time：
若记录明确早于该进程的启动边界，属于残留 PID，可以继续启动且不向该进程发终止信号。
时间精度不够、原生观察失败或记录在核对期间变化时保留冲突；不扫描命令行误杀进程。
这不是全机孤儿清理机制。
`@anchor test Relay/tests/relay-process-owner.test.ts :: "allows recovery when a retained PID record predates the live process using that PID"`

普通 pipe 模式的 CC pool 与 CC/Codex batch 共用 `spawnOwnedProcess`：执行账户的
canonical Python 以 `-I -S` 启动短引导并 exec 原命令，保留原 PID、输入、输出、信号和退出码。
独立的存活管道与业务 stdin 分开，因此正常输入结束不会终止尚在计算的任务。
同组 guardian（仅负责回收的子进程）在 owner 管道关闭或组长退出后，向其私有组发 TERM，
两秒后 KILL；它存活期间保留该组身份，无需扫描进程名、PID 文件或旧会话。
实际 exec 失败仍作为 ChildProcess 的 error 返回，保留 ENOENT 等代码供原 fallback 分类。
`@anchor test Relay/tests/owned-process.test.ts :: "preserves stdin EOF, separate output, cwd, env, PID, and exit status"`
`@anchor test Relay/tests/owned-process.test.ts :: "reaps TERM-ignoring descendants after %s SIGKILL"`

回收范围是本次启动且同 UID、未离开私有进程组的后代。跨 UID 的 Linux 租户仍须
systemd 服务的 `KillMode=control-group`；主动 setsid 的后代、guardian 自身被单独
SIGKILL 以及 SDK 内部启动器不由这项证明覆盖。PTY 路径继续遵守上述 Harness 合同。

## 2. 路由判据

- [x] 会话键：基础形态 userId:agentId；双通道 agent 的飞书投递键追加 :feishu
  后缀，归属判定按后缀严格区分
  `@anchor symbol feishuPoolKey`
  `@anchor symbol isFeishuDeliveryPoolKey`
  `@anchor test Relay/tests/router.test.ts :: "poolKey returns userId:agentId format"`
  `@anchor test Relay/tests/feishu-router.test.ts :: "dual-channel agent is addressable through Telegram and Feishu with one pool key"`
  命令层从已知 user/Agent 配置经统一 formatter 精确匹配原键，不自行按冒号截取 Agent ID。
  基础键或飞书键若匹配多个配置身份则拒绝猜测；未知后缀也不能冒充已知 Agent。
  `@anchor test Relay/tests/commands.test.ts :: "resolves configured separator IDs without truncating or guessing a channel suffix"`
- [x] agent 定位：TG 按 chatId、飞书按 openId 各有解析函数；通道能力判据 =
  有 botToken 即支持 TG、有 feishu 凭据即支持飞书
  `@anchor symbol resolveByBotAgent`
  `@anchor symbol resolveByFeishuAgent`
  `@anchor symbol supportsTelegram`
  `@anchor symbol supportsFeishu`
  `@anchor symbol getAgentChannel`
  `@anchor test Relay/tests/router.test.ts :: "resolveByBotAgent returns undefined for wrong chatId"`
- [x] session 字段选择与重置：TG 用 sessionId、纯飞书 agent 用 feishuSessionId；
  按通道 scope 写入；重置生成新 UUID
  `@anchor symbol preferredSessionField`
  `@anchor symbol overrideSessionId`
  `@anchor symbol resetSession`
  `@anchor test Relay/tests/feishu-router.test.ts :: "dual-channel reset isolates scope %s"`
- [x] 入站逻辑 agent 重映射：单 agent 直返；入站通道明确时不重映射
  `@anchor symbol resolveInboundRoutingAgent`
  `@anchor test Relay/tests/commands.test.ts :: "resolveInboundRoutingAgent keeps physical Feishu bot when inboundChannel is feishu (multi-agent)"`
- [x] gateway 三态判定（ok / pairing_required / access_denied）与地址键
  `@anchor symbol resolveInboundRoute`
  `@anchor symbol channelAddressKey`
  `@anchor symbol conversationKeyFor`
  `@anchor test Relay/tests/gateway.test.ts :: "returns pairing_required for an unbound channel address"`
  `@anchor test Relay/tests/gateway.test.ts :: "returns access_denied when the target agent is outside the binding allowlist"`
  `@anchor test Relay/tests/gateway.test.ts :: "carries runtime: cc-sdk through the Feishu route preference instead of collapsing to cc"`

### 2.1 会话获取与恢复边界

以下描述当前源码合同；安装和真实渠道验收须绑定精确候选，不能由合同测试推断已部署。

- [x] `acquireConversation` 统一普通入站、重启后续接、Pi 命令唤醒和显式重启的
  进程复用、冷启动、原绑定采用与失败语义；TG 文本/媒体/恢复、飞书普通入站和命令
  不再各自实现这些分支。`conversationLaunch` 在该边界内统一选取 runtime、Session、
  workspace、成本绑定与已存在的 admission 证据；特殊 Host 资源恢复仍由原权威模块负责。
  `@anchor symbol acquireConversation`
  `@anchor symbol conversationLaunch`
  普通会话重置或恢复后优先采用当前渠道的持久 Session；配置只提供缺失的初始值，
  Telegram 不借用飞书 Session。适配器可以写入本次选定的启动身份；await 后只接受
  原记录或该精确初始化结果，WI/资源绑定、授权或其他 Session 的变化仍拒绝。
- [x] 同一 runtime/address 的冷启动和显式重启串行；已发布且通过校验的现有进程可被
  新输入采用，不必等待旧请求的 spawn 返回。失败后下一个请求仍可进入，不阻塞其他地址。
  显式重启先验证原身份，再停止进程；等待期间身份变化则拒绝采用旧结果。地址队列属于 runtime 本身：
  渠道交给处理器的包装（接收日志的回执 runtime）报出所包装的 runtime，经包装与不经包装的请求进同一队列。
  `@anchor test Relay/tests/conversation-acquisition.test.ts :: "serializes simultaneous cold requests and reuses the one admitted native process"`
  `@anchor symbol underlyingRuntime`
  `@anchor test Relay/tests/conversation-acquisition.test.ts :: "one address queue for the runtime and its channel wrapper: a cold start is not repeated through the wrapper"`
  `@anchor test Relay/tests/conversation-acquisition.test.ts :: "does not block another address behind a slow start"`
  `@anchor test Relay/tests/conversation-acquisition.test.ts :: "validates a restart before killing a process and preserves the original on rejection"`
- [x] 已有 Host consumer 的恢复资格、原 Session 检查及 await 后的入口边界检查在
  acquisition 内解释；飞书只传真实入口边界和已有恢复能力。新输入取代旧输入后，
  不从旧请求恢复新 Session，不签发新授权。渠道继续负责输入捕获、交付、活动记录
  与中断提示，acquisition 不宣称取得 WI 执行权或证明消息送达。
  `@anchor symbol conversationConsumerProjection`
  `@anchor test Relay/tests/conversation-acquisition.test.ts :: "does not invoke fallback after a newer input supersedes the failed start"`
  `@anchor test Relay/tests/conversation-acquisition.test.ts :: "restores only the retained WI consumer after normal admission fails"`
- [x] 飞书 Host 恢复也由 `acquireHostRecoveryConversation` 持有相同的进程队列，
  冷启动、空闲进程重建与资源恢复不能和普通获取各自产生一个进程。Host 原恢复记录
  仍负责证明已接受的 scope；调用它时释放当前队列步，返回后重新进入队列复核原
  Session、入口边界及实际消费者，避免嵌套恢复或等待已有恢复时自锁。
  `@anchor symbol acquireHostRecoveryConversation`
  `@anchor test Relay/tests/feishu-host-recovery-runtime.test.ts :: "shares cold-start ownership with ordinary acquisition during Host recovery"`
  `@anchor test Relay/tests/feishu-host-recovery-runtime.test.ts :: "allows an already running Host recovery to acquire the queue while foreground waits for it"`

### 2.2 SCL 消费边界

- [x] sealed client 保留当前 `workflow_profile`、启动历史、转换引用和 `workflow_alignment`。
  Relay 不从旧图、启动类型或 execution authority 推断当前工作方法、方案已经确认或额外写权。
  标准流程继承现有图与设计；轻量流程沿轻量收尾，需要升级时正式转换同一 WI。
  具体工作方法由已安装 `work-item-lifecycle` 负责，恢复提示不再复制另一套标准启动流程。
  `@anchor symbol sclWorkflowGuidance`
  `@anchor test Relay/tests/standard-workflow-prompt.test.ts :: "uses a promoted standard profile without losing the lightweight origin"`
  `@anchor test Relay/tests/standard-workflow-prompt.test.ts :: "does not infer an effective profile from startup history or existing artifacts"`
- [x] `readSclFrontier` 统一解释 kernel 已授予的执行方式、已提交且本地可工作的接续资格与只读观察。
  `globally_committed` 与 `local_committed` 都可承载本地接续；降级的 isolated 本地工作仍可继续，
  但不转成 fenced 外部操作。调用方继续核对精确 WI/epoch、资源范围和真实运行时采用。
  资源恢复的 normal admission、prepared rollover 释放与后继选择复用同一解释器，
  不再各自拼 execution_authority/entry_action。fenced 必须同时有严格布尔 true
  的 authority 和 external_effect_fenced。控制器中的 orient/replacement/terminal
  精确分支属于各自编排，不另包装成第二套执行授权规则。
  `@anchor symbol readSclFrontier`
  `@anchor test Relay/tests/scl-frontier.test.ts :: "requires local-work availability even when the packet carries fenced authority"`
  `@anchor test Relay/tests/scl-frontier.test.ts :: "keeps degraded local execution usable without granting fenced execution"`
- [x] Relay 给每条人类消息附 `[Host human input reference: <ref>. This records message provenance.]`：
  只说明这条消息出自哪位人类、可凭 `<ref>` 读回原文，不授予也不收回 Agent 本职以内的决定权。
  读取方（Relay 与内核）只认 `[Host human input reference: <ref>.` 前缀，旧附注同样可读。
  `@anchor symbol humanInputContext`
  `@anchor test Relay/tests/scl-human-decision.test.ts :: "states message provenance and nothing more"`
- [x] `observe_terminal` 是合法只读前沿。查看已完成 WI 保留成果与下一步，不触发重建、接续写入或启动新进程。
  `@anchor test Relay/tests/work-item-session.test.ts :: "observes a completed local WI without re-entering or restarting it"`

## 3. 对话渲染与送达

- [x] runtime 事件先经共享语义状态机归并：status/activity 是非正文覆盖层，delta
  追加、snapshot 前缀替换、commentary 分段并按包含关系去重、final 是唯一权威终态；
  event identity 为有界 opaque sequence，不持久化正文或全文哈希
  `@anchor symbol ChannelTurnRenderingState`
  `@anchor symbol activityMilestone`
  `@anchor test Relay/tests/channel-rendering.test.ts :: "normalizes Pi cumulative snapshots into one evolving message and one final state"`
- [x] TG 出站：每回合一条演进进度消息；edit 失败/429 只合并中间帧；终态先在聊天
  底部发送一次 fresh authoritative final，尽力删除旧进度，再在底部新发完成标记；超长正文只在终态分片
  `@anchor symbol createTelegramRenderingSink`
  `@anchor test Relay/tests/telegram.test.ts :: "shows an immediate busy frame and delivers one fresh authoritative final"`
- [x] 飞书出站：每回合一张 streaming card；start/update/close 任一失败后只在终态发送
  一次完整静态 fallback，同文 streaming final/result 不再追加副本
  `@anchor symbol createFeishuStreamingSink`
  `@anchor test Relay/tests/feishu-channel.test.ts :: "永久 card update 拒绝后只在终态发送一次完整 fallback，不落地递进前缀副本"`

- [x] TG transport：带重试的正文出口 + 4096 分片 + agent 前缀
  `@anchor symbol sendWithRetry`
  `@anchor symbol splitMessage`
  `@anchor symbol prefixMessage`
  `@anchor const TG_MAX_LENGTH = 4096`
- [x] 飞书出站：池响应主函数 + 卡片构造 + 4000 字分片
  `@anchor symbol handleFeishuPoolResponse`
  `@anchor symbol wirePoolEvents`
  `@anchor symbol buildMarkdownCard`
  `@anchor symbol splitCardText`
  `@anchor const FEISHU_MAX_CARD_LENGTH = 4000`
- [x] 回复上下文注入：用户可见标记行与长度上限
  `@anchor symbol REPLY_CONTEXT_USER_MARKER_LINE`
  `@anchor const REPLY_CONTEXT_MAX_CHARS = 3000`
  `@anchor test Relay/tests/reply-context.test.ts :: "returns only the user utterance after --- user message ---"`

## 4. 回执体系（覆盖面=cron/推送侧）

- [x] receipt 唯一工厂 + 五类投递函数；deliveredAt 仅 sent 时填；参数校验失败
  的回执 attempts=0 与真实失败（≥1）可区分
  `@anchor symbol receipt`
  `@anchor symbol deliverTelegramText`
  `@anchor symbol deliverTelegramDocument`
  `@anchor symbol deliverFeishuText`
  `@anchor symbol deliverFeishuCard`
  `@anchor symbol deliverFeishuImage`
  `@anchor symbol deliverFeishuFile`
  `@anchor test Relay/tests/delivery.test.ts :: "returns a sent receipt for Telegram text delivery"`
  `@anchor test Relay/tests/delivery.test.ts :: "returns a failed receipt for Telegram text delivery errors"`
- [x] 重试口径：delivery 层 TG 重试 2 次（429 按 retry-after 封顶 30 秒、瞬时
  网络错误指数退避）、飞书零重试；channel 主链另一套 TG 重试 1 次——三套并存
  互不共享（登记 §5）
  `@anchor const DEFAULT_TELEGRAM_RETRIES = 2`
  `@anchor const MAX_RETRY_DELAY_MS = 30_000`
  `@anchor const NETWORK_RETRY_BASE_DELAY_MS = 5_000`
  `@anchor test Relay/tests/delivery.test.ts :: "retries transient Telegram document network failures"`
- [x] 附件边界：图片 10MB / 文件 30MB（出站），空文件拒投
  `@anchor const FEISHU_IMAGE_MAX_BYTES = 10 * 1024 * 1024`
  `@anchor const FEISHU_FILE_MAX_BYTES = 30 * 1024 * 1024`
  `@anchor symbol checkFileForDelivery`
  `@anchor test Relay/tests/tool-file-delivery.test.ts :: "checkFileForDelivery rejects empty files"`

## 5. 接续与会话边界

普通 `/new` 不以 `/wrap` 为前置；活动或待接续 WI 仍由 SCL 绑定守卫判断是否可换会话。`/wrap` 兼容入口不能代替 WI 的验收和正式收口。

WI 入口从 `agent.workspace` 的唯一有效 Git origin 识别仓库，再由 SCL 核对登记；`HARNESS_ROOT` 只定位账本，不代替仓库路由。来源不明时拒绝，不切换到另一个仓库或建立替代 WI。

`@anchor symbol resolveWrapSkillPath`
`@anchor test Relay/tests/telegram.test.ts :: "resolves a canonical Coevolution wrap skill and refuses a missing installation"`

## 6. 覆盖声明

本 spec 给 Relay 主链的路由判据、会话键、送达分片、回执工厂与重试口径首次挂
显式锚点。通道内细节（去重闸/流式卡片/outbound cache）见 relay-channels.md；
运行时与进程池另立 spec（待补）。存量 features/ 的 logical-gateway /
telegram-channel / feishu-channel 三目录现行契约由本文与 relay-channels.md
分别接管；**bot-commands 目录只有入站重映射一条（`resolveInboundRoutingAgent`，
§2）被本文接管**——src/commands.ts 其余命令面与 src/runtime.ts 全部尚无正面
锚点，缺口已登记在 §5，补锚之前不得声称全量接管。
