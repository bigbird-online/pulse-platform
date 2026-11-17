# SPEC：Relay 通道面 — 入站闸 / 统一回合渲染 / outbound cache

> version: `0.3.0`
> status: `reverse_engineered_from_code`
> authority: 两通道入站防线、统一回合渲染与出站文本缓存的**现行实现契约**
> owner: Pulse（wi_20260804_070 · KR2a）
> modified: `2026-09-04`
> 版本史：只看 git log

**取证基础**：2026-08-09 代码普查 + 2026-08-25 WI938 重复投递生产样本、
跨 runtime/channel rendering 回归与 spec-anchor 机器验证。

## 1. 入站防线（飞书两道，TG 零道——不对称是事实先记档）

- [x] 消息来源判不了元数据（openId 在重投与首发下取值相同，事故原文留在源码
  注释）；现行判据是**时间与 id**，不是元数据：陈旧闸先行（创建时间超窗即拒，
  判不出时间则放行——宁漏拦不误杀）、id 去重闸在后（内存表按 agent+msgId）。两道闸都在事件到达时
  判定；id 表保留时长 = 陈旧窗 + 时钟偏差余量，长于陈旧窗，所以陈旧窗内任何时刻的重投都被 id 表挡住，
  超出陈旧窗的由陈旧闸挡住，中间没有缺口（飞书对 3 秒内未确认的事件在 15 秒、5 分钟、1 小时、6 小时后重投，
  断线重连后也会重投）
  `@anchor symbol parseFeishuCreateTime`
  `@anchor symbol admitFeishuInboundMessage`
  `@anchor const FEISHU_MESSAGE_MAX_AGE_MS = 5 * 60 * 1000`
  `@anchor const FEISHU_CLOCK_SKEW_MARGIN_MS = 5 * 60 * 1000`
  `@anchor const FEISHU_MESSAGE_DEDUPE_WINDOW_MS = FEISHU_MESSAGE_MAX_AGE_MS + FEISHU_CLOCK_SKEW_MARGIN_MS`
  `@anchor test Relay/tests/feishu-channel.test.ts :: "a redelivery 2–5 minutes after arrival is still a duplicate; older ones stay stale"`
- [x] 飞书接收即确认：SDK 在事件处理器返回后才回确认帧，所以处理器过完两道闸、把消息交给发送者的队列后立即
  返回。同一发送者按到达顺序逐条处理，不同发送者互不等待；`/stop`、`/kill`、`/status`、`/help` 不进队列，
  立即处理。消息处理把回合交给运行时后即结束，不等派发；派发完成后才记会话活跃，派发失败另行告知发送者
  `@anchor symbol createFeishuIntake`
  `@anchor test Relay/tests/feishu-channel.test.ts :: "acknowledges the event at arrival while the turn waits behind a busy session; the sender keeps order"`
  `@anchor test Relay/tests/feishu-channel.test.ts :: "/stop bypasses a sender lane that is still preparing an earlier message"`
- [x] 飞书已确认的消息不丢：与 TG 同一套接收日志（键 `feishu:<agentId>`）。进发送者队列的消息在处理器返回
  （即 SDK 回确认帧）之前写入日志并落盘；写不成则处理器抛错、SDK 回错误码、飞书重投，且该 `message_id` 从去重表
  撤下，重投不被当成重复。删除时机与 TG 相同：运行时接收回执、运行中发送失败或处理结束而无回合；关停后留下的
  按原顺序重放，排在待恢复回合之后；日志里已有的 `message_id` 再到时不重复处理；热加载换代时与 TG 一样共用
  一本记录，由开始处理的那一代持有
  `@anchor symbol replayFeishuIntake`
  `@anchor test Relay/tests/feishu-channel.test.ts :: "a slow image finishes during shutdown: after restart it resumes, then the acknowledged texts, once each and in order"`
  `@anchor test Relay/tests/feishu-channel.test.ts :: "keeps an acknowledged message in the journal until the runtime admits its turn"`
  `@anchor test Relay/tests/feishu-channel.test.ts :: "a message still downloading when the bot is replaced is dispatched once, by the bot that started it"`
- [x] 入站主处理器与分支面：text（含 Minutes 嗅探与斜杠命令，compact/context
  透传 CC）、post、image、file、audio、卡片动作
  `@anchor symbol handleFeishuMessage`
  `@anchor symbol handleFeishuCardAction`
  `@anchor symbol dispatchMenuAction`
  `@anchor symbol extractPostText`
  `@anchor symbol collectPostImageKeys`
  `@anchor test Relay/tests/feishu-menu-dispatch.test.ts :: "routes {relay_menu:stop} into dispatchMenuAction (handleStop called)"`
  `@anchor test Relay/tests/feishu-channel.test.ts :: "extracts link href from a tag"`
- [x] 未绑定进配对流程：配对码字符集去混淆、五分钟过期。提示语：受管实例（设了 `PULSE_RELAY_PATHS`）
  指向 Platform 开通页 Step 3；独立部署只给配对码，请用户交给管理员
  `@anchor symbol pendingPairings`
  `@anchor symbol feishuPairingMessage`
  `@anchor test Relay/tests/feishu-pairing-message.test.ts :: 独立部署只给配对码与中性提示，不提 PULSE 开通页`
- [x] TG 接收不等回合：grammY 长轮询处理完一批更新才拉下一批，所以第一道中间件把每条更新交给
  所在聊天的队列后立即返回。同一聊天按到达顺序逐条处理，不同聊天互不等待；`/stop`、`/kill`、
  `/status`、`/help` 不进队列，立即处理。消息处理器把回合交给运行时后即结束，不等派发：维护屏障与
  运行时队列都按调用顺序排列，忙碌会话照旧排队。健康判据仍是该机器人 90 秒内有成功的 `getUpdates`
  `@anchor symbol telegramIntakeMiddleware`
  `@anchor symbol TG_POLL_FRESHNESS_MS`
  `@anchor test Relay/tests/telegram-intake.test.ts :: "keeps polling and serves other chats and /stop; health stays ok"`
  `@anchor test Relay/tests/telegram-intake.test.ts :: "keeps a second message of the same chat after the first"`
- [x] TG 已确认的更新不丢：下一次 `getUpdates` 即向 Telegram 确认此前整批更新，Telegram 不再重投。
  所以每条进队列的更新先记入该机器人的接收日志（state 的 `channelIntake`），日志在发出确认的
  `getUpdates` 之前落盘，写不成则这次拉取失败、不确认、由 grammY 重试。更新在三种情况下从日志删去：
  运行时为其回合发出接收回执（见 relay-config-http.md 运行时接收回执；发送调用返回不算）、运行中发送
  失败（已告知发送者）、处理结束且没有回合。关停时日志关闭：尚未开始的更新不再开始，回合未获回执的、
  处理中途结束而无回合的都不删，留给下一进程；重启后按原顺序重放，排在关停时记下的待恢复回合之后、
  新到更新之前；日志里已有的 `update_id` 再到时不重复处理。同一进程内同一机器人的各代（热加载移除再加入）
  共用一本记录：开始处理某条更新的那一代持有它直到处理了结，新一代只重放无人持有的记录；旧一代处理在关闭后
  中途结束而无回合的，交给最新运行的一代，没有则留给下一进程
  `@anchor symbol createChannelIntakeJournal`
  `@anchor symbol replayTelegramIntake`
  `@anchor symbol intakeAwareRuntime`
  `@anchor test Relay/tests/telegram-intake.test.ts :: "a slow photo finishes during shutdown: it resumes from state, the later texts from the journal, once each and in order"`
  `@anchor test Relay/tests/telegram-intake.test.ts :: "a slow start that never reaches the runtime: the restarted bot replays all, ignores a redelivery, keeps order"`
  `@anchor test Relay/tests/telegram-intake.test.ts :: "a turn the runtime had not admitted at shutdown stays in the journal and runs once after restart"`
  `@anchor test Relay/tests/telegram-intake.test.ts :: "an update still downloading when the bot is removed and added again is dispatched once, by the bot that started it"`
  `@anchor test Relay/tests/telegram-intake.test.ts :: "a record cut short in a replaced bot goes to the newest running bot, which alone replays it"`

### WI 控制回执的发送归属

普通斜杠入口、原生 Host 工具入口和后台恢复器共用 `SclIntentRetryCoordinator.deliverExecution`。
同一持久 intent 的结果发送前先读回并认领，互斥覆盖整个异步发送与确认；其他发送者不再追加同一回执。
发送抛错时保留原结果供恢复，不重做生命周期命令；没有持久存储时仍直接回复。
不同入站事件不按相同正文合并，自动重试后的新结果仍可送达。本约束不宣称跨进程崩溃或服务商确认丢失时的 exactly-once。
`@anchor symbol deliverExecution`
`@anchor test Relay/tests/feishu-channel.test.ts :: "WI control receipt ownership across foreground and recovery"`

### 双渠道运行配置

同一 Agent 的 TG 与飞书会话分别拥有 runtime、runtime preference、structured model/reasoning 和 CC model。
TG 使用 Agent 顶层字段；飞书使用 `feishu.runtime`、`feishu.conversationRuntimePreference`、
`feishu.conversationRuntime`、`feishu.ccModel`。`conversationRuntime: null` 和 `ccModel: null`
分别表示明确不使用 structured route 和运行时默认模型。

旧配置没有飞书字段时继承顶层配置。在任一渠道首次修改配置前，保存飞书原有效配置，避免另一渠道的后续写入
改变其 runtime/model。飞书的 `/runtime`、跨 runtime `/model`、Pi `/model` 和 `/think` 均只写飞书配置；
进程终止和 session 清理仍只作用于对应渠道 pool key。TG 的模型 override 文件保持兼容，不能覆盖飞书的明确选择。

入站路由、命令卡片、Gateway preference、Pi profile 启动检查和重新加载均使用同一渠道配置。
配置热加载与进程重启保留两个渠道的原会话；不能用新 session 或将原 session 改绑另一 runtime 消除冲突。

回归锚点：`Relay/tests/channel-conversation.test.ts`，包含真实飞书卡片处理、双向切换、model/think、
默认模型、热加载、重启和嵌套 Pi route 校验。

## 2. 共享回合语义状态机

- [x] `ChannelTurnRenderingState` 是 TG/飞书共同的语义真源：delta 只追加、Pi 累积
  snapshot 按包含关系替换、独立 commentary 以空行分隔、activity 只作覆盖层、final
  只封账一次；同 event id replay 和重复 result 是 no-op
  `@anchor symbol ChannelTurnRenderingState`
  `@anchor symbol activityMilestone`
  `@anchor test Relay/tests/channel-rendering.test.ts :: "keeps true deltas instead of treating them as repeated snapshots"`
- [x] idempotency scope 是 tenant/session/turn/channel + opaque source sequence；正文、工具名、
  参数和全文哈希都不作持久 identity。active turn 只在内存保留正文，seen event set 有界
- [x] 用户 turn 被接受后必须立即产生可见 status；typing/reaction 只作补充。activity
  保留每个实际执行步骤的计数，由 transport 合并发送时机，final 前移除 activity overlay
  `@anchor test Relay/tests/channel-rendering.test.ts :: "preserves every actual count; transport owns time coalescing"`
- [x] 每个完整 frame 由共享 RichDoc compiler 从头解析；HTML 禁用，URL 只允许
  http/https/mailto，危险链接退化为 inert label。canonical Markdown、plain fallback 与
  semantic splitter 均在共享层，渠道 adapter 不复刻 parser/sanitizer/splitter
  `@anchor symbol compileRichDoc`
  `@anchor symbol sanitizeLinkTarget`
  `@anchor symbol splitRichDoc`
  `@anchor test Relay/tests/rich-doc.test.ts :: "compiles every incomplete streaming prefix independently into a valid closed payload"`
- [x] capability profile 显式记录 payload unit/budget、element budget、rollover、throttle
  与独立 rollout；默认 off，legacy=off，shadow 只编译，canary 不使用正文或正文哈希
  `@anchor symbol resolveRichDocChannelProfiles`
  `@anchor symbol isRichDocRolloutEnabled`

## 3. 飞书流式卡片 transport 状态机

- [x] 会话类与生命周期：建卡 → 正文 PUT（单调 sequence + uuid 前缀 s_）→
  心跳 PUT（前缀 h_，不写回当前文本）→ 收卡 PATCH（前缀 c_）
  `@anchor symbol FeishuStreamingCardSession`
  `@anchor symbol getTenantAccessToken`
  `@anchor symbol updateCardContent`
  `@anchor symbol pushHeartbeat`
  `@anchor test Relay/tests/feishu-streaming-card.test.ts :: "should append delta correctly in append mode"`
- [x] 节流与增量：首帧强推 / 自然边界强推 / 显著增量强推，否则延迟合并；
  入参是累积全文、内部转 delta 追加
  `@anchor symbol shouldPushStreamingUpdate`
  `@anchor symbol hasNaturalStreamingBoundary`
  `@anchor symbol mergeStreamingText`
  `@anchor const STREAMING_UPDATE_THROTTLE_MS = 1500`
  `@anchor const STREAMING_SIGNIFICANT_DELTA_CHARS = 18`
- [x] closed 自锁三源合一：主动收卡、服务端报流已关（心跳收到该错误必须置位
  并停表——不置位会空转刷警拖垮事件循环，08-06 两次真实事故）、sink 丢弃已关
  实例重开新卡
  `@anchor test Relay/tests/feishu-streaming-card.test.ts :: "reports isClosed only after close() so stale sessions can be discarded"`
  `@anchor test Relay/tests/feishu-streaming-card.test.ts :: "should handle duplicate updates gracefully"`
- [x] 心跳节奏：30 秒 tick、空闲满 60 秒才真发
  `@anchor const STREAMING_HEARTBEAT_TICK_MS = 30_000`
  `@anchor const STREAMING_HEARTBEAT_IDLE_MS = 60_000`
- [x] sink 层：per 会话串行链防乱序；共享 rendering frame 经 RichDoc 编译后按语义
  chunk 更新 CardKit 卡片，长答 rollover 为有序多卡。streaming start/update/close 失败
  或服务端提前关流后，中间帧只在内存 coalesce，turn end 至多发送一次完整静态答案；
  成功终态在卡片 presentation 末尾显示共享 `✅ 本轮回复已生成`，但不写入 answer state
  或 reply-context cache；静态格式错误按 CardKit Markdown → post Markdown → plain text 单向退化
  `@anchor symbol createFeishuStreamingSink`
  `@anchor symbol compileFeishuPayloads`
  `@anchor symbol isFeishuStreamingCardEnvEnabled`
  `@anchor test Relay/tests/feishu-channel.test.ts :: "streams sanitized CardKit Markdown and rolls a long semantic answer across cards"`

## 4. Telegram transport 状态机

- [x] 首个 status/progress frame 通过当前选中的 Telegram envelope 建一条演进消息，
  后续帧优先 edit；edit 失败或 flood limit 不追加 copy，只保留最新内存帧；新路径还按
  profile throttle 合并更新
- [x] 新路径从共享 AST 生成 Rich Message HTML → legacy Rich HTML → conservative HTML
  → plain text 候选；Rich Message HTML 将 20 列以内表格映射为原生 `<table>`，且仅在
  独立 rollout 命中时使用。只有 definite capability/format rejection 才单向退化，
  ambiguous network write 不跨格式重发
  `@anchor symbol compileTelegramPayloads`
  `@anchor symbol createTelegramRenderingSink`
- [x] final 先在聊天底部发送一次 fresh authoritative answer，成功后尽力删除较早的演进
  消息，再在聊天底部发送不含正文且不推断业务结果的 `✅ 本轮回复已生成`；删除失败不
  阻止底部完成标记。final 投递失败则把原进度改为 `⚠️ 最终回复投递失败，请重试。`。
  开始、处理中与自动重试分别使用 `▶️`、`⏳`、`🔄`
  状态标记；处理中的 accepted-turn 步数由 Telegram 与飞书共用计数器，在第 1、5、10
  步及之后每 20 步显示相同精确计数。长正文按共享语义 chunk 发送，重复 final/result 不再发送
  `@anchor test Relay/tests/telegram.test.ts :: "uses Rich Message HTML for native tables in streaming edits and the fresh final"`
  `@anchor test Relay/tests/telegram.test.ts :: "uses Rich HTML for complete snapshots and sends one fresh semantic final before retiring progress"`
  `@anchor test Relay/tests/telegram.test.ts :: "does not retry an ambiguous final send and terminalizes the progress message as failed"`

## 5. outbound cache（飞书出站文本缓存）

存在理由：用户回复 bot 卡片时，官方 API 拿不回卡片真文本（返回占位符），
只能自存。插入序 LRU + TTL + 原子落盘。

- [x] 读写判据：record 空文本即丢、重录刷新新鲜度；lookup 过期即删；返回拷贝
  `@anchor symbol OutboundTextCache`
  `@anchor const DEFAULT_MAX_ENTRIES = 2000`
  `@anchor const SAVE_DEBOUNCE_MS = 2000`
  `@anchor test Relay/tests/feishu-outbound-cache.test.ts :: "evicts oldest entries beyond capacity (LRU by insertion)"`
  `@anchor test Relay/tests/feishu-outbound-cache.test.ts :: "refreshes recency on re-record so it is not evicted first"`
  `@anchor test Relay/tests/feishu-outbound-cache.test.ts :: "drops entries older than the TTL on load"`
  `@anchor test Relay/tests/feishu-outbound-cache.test.ts :: "survives a corrupt cache file by starting empty"`
- [x] origin 与跨 session 判据：读取时 cache 优先于 API；两个 sessionId 都在
  才判同异（防 legacy 条目误报跨 session）
  `@anchor symbol resolveFeishuReplyText`
  `@anchor symbol feishuReplyMessageId`
  `@anchor symbol buildFeishuRuntimeText`
  `@anchor test Relay/tests/feishu-outbound-cache.test.ts :: "records and returns origin (agentId/sessionId/poolKey) when provided"`
  `@anchor test Relay/tests/feishu-outbound-cache.test.ts :: "legacy entries without origin still load and serve text"`

## 6. 已知两义与债（登记待裁）

`@anchor manual TG 无入站闸 :: 飞书有陈旧闸+去重闸两道,TG 一道都没有;重投类事故在 TG 侧无防线,通道能力不对称`
`@anchor manual 静态卡 poolKey 线性反查 :: 静态卡路径写 cache 时按 openId 遍历反查 poolKey 取首个,一对多时非确定;流式路径是精确键;同一份 cache 的 origin 质量因路径而异`
`@anchor manual 心跳不入串行队列 :: 正文更新走队列,心跳发送不走,两者共享同一自增 sequence,存在竞争同一序号空间的二次风险`
`@anchor manual pending-interactive 半死模块 :: 注册函数 markPendingInteractive(src/pending-interactive.ts:9)与清除函数 clearPendingInteractive(:13)生产零调用方;唯一读者 isPoolKeyPendingInteractive 经 index.ts 注入 pool,但没人写入故永远得 false;模块注释声称的双端使用(feishu 注册/pool 检查)与事实不符,待删或接线;下面两条 unwired 锚点由机器守——一旦有人接线,锚点转红提醒把声明改回 wired`
`@anchor wire markPendingInteractive :: unwired`
`@anchor wire clearPendingInteractive :: unwired`
`@anchor manual 配对码等三个内联周期常量 :: 配对码过期五分钟、两个清扫周期六十秒均为内联字面量无符号名,改动无从锚定`

## 7. 覆盖声明

本 spec 给飞书入站两道闸、跨 runtime/channel 的统一回合渲染、TG edit/send 边界、
飞书 streaming/fallback 边界和 outbound cache 判据挂显式锚点。完整语义定义以
`channel-rendering-contract.md` 为准；语音转写与视觉代理（stt / vision-proxy）暂无
专属测试，留待接入时立锚。
# Legacy Feishu recovery eligibility

Before reading a native Pi transcript for legacy recovery, Relay excludes slash/control input
and input superseded by a durable channel boundary or a persisted successful session reset.
A legacy reset record may exclude only input observed no later than that reset, after verifying
the current pool/session identity, retired-session identity and reset/activity timestamps.
Missing or damaged reset evidence grants no replay or cancellation inference; an empty session
or recent activity alone is insufficient. The original input, retired transcript and reset
audit remain unchanged. A fresh `/new` session may have no transcript
until its first business dispatch; that absence does not justify replaying a control command or
an older message. Current ordinary inputs still require the original complete native history
and positive dispatch evidence. Missing or ambiguous business evidence remains unknown. A
control-command reply, recovery scan and process health are separate from a visible business reply.
