# MVS：CC 运行时记忆管理迁移 Anthropic Agent SDK——试点验证

- wi: `wi_20260702_125`
- 决策依据：2026-07-02 根因调查（`memo/2026-07/2026-07-02-pulse-root-cause-investigation.md`）大鸟拍板——**每个运行时一套官方 SDK 管记忆**（CC→Anthropic Agent SDK；Codex→OpenAI 侧，备胎最后做或不做）。方向二（故障域隔离）不单独立项，留给将来进程隔离。
- 版本：v0.1（2026-07-02）

## 目标

用官方 Anthropic Agent SDK（TypeScript，与 Relay 同栈）实现一个最小 runtime adapter，在**专用测试 agent**上验证"记忆归 SDK 管、Relay 不翻不改会话文件"的可行性，两周内产出 go/no-go 结论供大鸟拍全量迁移。

治的是根因调查五发生条件之①：CC 会话 jsonl 一身三职、多写者无锁、磁盘修复够不着进程内存（14 起当机级事故的病灶）。

## 三个未知数（= 验收）

1. **跨 Relay 重启恢复**：SDK 会话经官方 resume 接口跨进程重启恢复，真链路实测上下文连续（重启前对话内容重启后能被正确引用）；全程零次直接读写会话文件。
2. **GLM 备用路径**：SDK 下以 GLM_ 前缀 base URL 覆盖拉起会话，连通性实证（Claude→GLM fallback 语义在 SDK 形态下成立）。
3. **泄漏观测**：多轮长跑观测工具调用泄漏（文本形态 `<invoke>`/裸 fence 词）在 SDK 会话中是否复现；出现则评估兜底放哪层（SDK hooks / 消息边界），不出现则记录观测窗口与轮次。

附加量化项：**常驻 SDK 会话的内存占用 vs 现 print-mode 进程**（VPS 总内存 1.6G，可用余量 ~500M，是全量迁移的硬约束）。

## 边界（明确不做）

- 不动生产 11 bot 的任何配置与代码路径（pool.ts 现有 spawn 链零改动）
- 不做 Codex/OpenAI SDK 侧
- 不做全量迁移设计与排期（等 go 结论）
- 不拆现有防御层（退役清单属全量迁移方案）

## 上下文

- 测试载体：新建专用测试 agent（独立 poolKey / 测试通道），不复用家人 agent
- SDK：`@anthropic-ai/claude-agent-sdk` 最新版；参考 Relay 现 conversation-runtime adapter 注册形态，新 adapter 与现 `cc` runtime 并列（如 `cc-sdk`），由 relay.json 按 agent 选择
- 双机注意：试点仅 vultr；ownermac 对称性属全量迁移
- 已知底图：`memo/2026-07/2026-07-02-relay-anatomy-full.md`（11 处 jsonl 依赖面 = 全量迁移时的退役清单）

## 风险初判

- Feature risk：中——SDK 常驻会话的长稳行为（多小时/多天）未知；内存是硬约束
- Slice risk：低——独立 adapter + 独立测试 agent，不碰生产路径；回滚 = 删 adapter 注册 + 删测试 agent 配置
- 供应商风险：SDK bug 无自救手段的担忧由试点本身回答（这正是试点存在的意义）

## 执行单元

主身写 MVS（本文件）；实施切片派 Engineer（spec-driven-engineering），允许：新增 adapter 文件、测试 agent 配置、试点观测脚本；不允许：改 pool.ts 生产路径、动生产配置、重启 relay.service（试点 adapter 热加载或独立进程承载，若必须重启走 busy=0 安全窗口）。

## 验收证据形态

go/no-go memo：三未知数逐条实证记录（命令+输出+时间戳）+ 内存数据 + 全量迁移的改造面清单与风险清单。
