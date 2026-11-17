# MVS：cc-sdk 双路适配器——SDK 运行时并列接入 Relay（旧路不拆）

- 决策依据：2026-07-02 大鸟拍板（根因调查 → SDK 试点 go → 政策卡点以"双路可逆"架构消解）。前置试点证据：`Bigbird/Relay/poc/cc-sdk/EVIDENCE.md`（wi_20260702_125）。
- 版本：v0.1（2026-07-02）

## 目标形态（四条铁律）

1. **旧路不拆**：现有 `cc` 运行时（claude -p 直驱 + 全部 jsonl 防御层）原样保留，无限期作为一等回退路径。
2. **双路并列**：新增 `cc-sdk` 运行时适配器，与 `cc` 在 runtime 注册表并列；每个 agent 在 relay.json 用 `runtime` 字段选路。
3. **可逆=配置级**：任一 agent 切回 = `runtime: "cc-sdk"` 改回 `"cc"` + 重启，分钟级；政策/故障两类风险共用这一个逃生门。
4. **认证与端点纯配置**：订阅 OAuth（默认）/ API key，按 agent 配置注入 SDK 会话 env，换路不改代码。

## 切片规划

- **切片 1（本 wi）：适配器核心**。`cc-sdk` adapter：SDK streaming-input 常驻会话、官方 resume（session id 存 state.json，同现状）、**懒驻留**（闲置 N 分钟引擎进程退出、来消息按需 resume——内存硬约束的解法，默认 15min 可配）、per-agent provider 配置注入、runtime 注册与 validateConfig 接线。**适配器内零接触 `~/.claude/projects/`**（清污/陈旧检查/marker 均不移植——那是旧路的绷带）。单测 + 真链路 smoke 脚本。
- **切片 2：试点接线**。专用测试 agent 走 `cc-sdk` 真通道跑数日；泄漏 nudge（A 方案）按需移植到消息层。
- **切片 3：灰度迁移**。家人 agent 逐个切，间隔数日，观测三指标：失忆/停摆/串位=0、泄漏计数、内存水位。pulse 本尊（TG）最后切。
- **切片 4：退役评估**。全量稳定数周 + 大鸟点头后，才议旧路防御层退役；在此之前一行不删。

## 边界（切片 1 明确不做）

- 不改 pool.ts 现有 cc spawn 逻辑（只新增文件 + 注册点最小接线）
- 不动任何生产 agent 的 relay.json 条目、不重启 relay.service
- 不做泄漏 nudge 移植（切片 2）、不做 Codex/OpenAI 侧
- SDK 包由 devDependency 转正为 dependency（构建/运行验证）

## 风险初判

- Feature risk：中——SDK 长稳行为（多日常驻+反复驱逐/唤醒）未经生产验证，靠切片 2/3 灰度覆盖
- Slice risk：低——纯新增路径，现有 11 bot 行为零变化；回滚=不注册该 runtime
- 内存：懒驻留是硬要求非优化项（1.6G 机器 400M 余量）

## 验收（切片 1）

- 单测：会话生命周期/驱逐-唤醒/resume/配置注入，全绿；现有全量测试无回归
- 真链路 smoke：脚本驱动 adapter 完成"多轮→闲置驱逐→resume 召回事实"端到端，证据落 EVIDENCE 格式
- `npm run build` 通过，dist 行为与生产一致（不切任何 agent 前提下）
- 主身逐行 review diff 后方可 push

## 迁移验收指标（G1 挂钩，切片 3 起算）

已迁移 agent 30 天：用户侧失忆/停摆/串位事故 = 0；大鸟人工报障 = 0。
