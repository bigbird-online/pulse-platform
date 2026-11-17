# GPT-6 Astra 模型发布契约

## 发布对象与真源

唯一获准的 GPT-6 路由是 `openai-codex/gpt-6-astra`。模型事实只取自 OpenAI 官方模型页；Codex 最低版本只取自 OpenAI Codex 官方 release；CC SDK 版本只取自 Anthropic 官方 npm 包及其官方仓库。可机读断言、规范化摘要和来源摘要固定在 `Relay/config/model-release.json`。

截至 2026-09-05（UTC+8）已复核：

- 模型 ID：`gpt-6-astra`；文本与图像输入、文本输出。
- 1,050,000 tokens 上下文、128,000 max output、知识截止 2026-04-30。
- reasoning effort：`low`、`medium`、`high`、`xhigh`、`max`。
- Relay 默认保守使用 272,000 上下文；超过 272K 的请求触发官方长上下文计价，不能无门禁放大预算。
- Codex `0.153.1` 只证明可通过 API 配置 Astra；Relay `/models` 需要模型可见，因此最低版本是 `0.153.4`（官方说明修复 bundled model picker 可见性并将 Astra 设为默认）。Relay 将该版本作为 production dependency 随 immutable release 安装，Codex batch/conversation 路由优先解析 release 内 binary，不再依赖三端全局 PATH 碰巧一致。
- GPT-6 走 Codex/Pi 的 `openai-codex` provider，不走 CC。CC 路由独立升级并锁定官方 `@anthropic-ai/claude-agent-sdk` `0.3.261`，防止三端 runtime 漂移。

## 自动化发布流水线

1. **官方发现**：`npm run model-release:discover` 只访问 manifest 白名单中的 OpenAI、OpenAI Codex、Anthropic HTTPS 真源，复核精确断言并产生摘要；来源内容变化返回非零，禁止静默采用。
2. **可信变更生成**：发现结果与当前 manifest 做结构化 diff。机器人只可生成候选 PR，不可直接改默认分支；模型 ID、能力、runtime 下限、来源摘要和回滚目标必须完整。
3. **兼容性测试**：Relay typecheck、build、全量测试；特别验证默认 Codex 参数、runtime-owned catalog alias、Pi `/models` 原生发现、精确 provider/model 热切换与失败回滚。
4. **人工门禁**：owner 审查官方 diff、成本与长上下文策略，明确批准候选 PR 和逐端 activation。任何来源摘要漂移、版本过低或模型缺失均 fail closed。
5. **分阶段发布**：严格按 `ownermac → TCloud → Vultr`；每端先由容量受控 release writer 准备并读回 immutable release，再单次 activation。前一端真实对话 UAT 未通过，不进入下一端。
6. **逐端 UAT**：每端独立保存三项结果：`/models gpt-6-astra` 可见；切换读回为 `openai-codex/gpt-6-astra`；发送唯一 challenge 并收到语义正确回复。版本、时间、release SHA、catalog readback 和渠道结果必须同属该端。失败立即告警，不得用另一端成功代替。
7. **回滚**：重新激活 release writer 保护的紧前版本，读回 WorkingDirectory/ExecStart（macOS 为 launchd 等价项）、runtime 版本、模型目录与服务健康，再执行旧模型 smoke UAT。回滚失败升级为最高级告警。
8. **告警**：`source_drift`、`runtime_too_old`、`model_missing`、`conversation_uat_failed`、`rollback_failed` 均产生失败的 GitHub Actions check；Relay operations on-call owner 须在 30 分钟内确认。`rollback_failed` 为 critical：立即停止后续端发布，保留失败 check 与 release receipt，未经 owner 明确确认不得重试。不得降级成空目录、旧模型或“成功但未切换”。

## 自动化闭环

- `model-release:check` 离线校验 manifest、三端顺序、固定 runtime、人工门禁、告警 owner 与 30 分钟 SLA。
- `model-release:discover` 在线核验固定版本真源；Anthropic `latest` 只产生候选提示，不改变或否定当前固定 `0.3.261` 发布证据。
- `Scripts/model-release-site-probe.py` 从每端 release-writer 的 activated receipt 派生 immutable release，读回 Codex、Pi、CC SDK 和精确目录，并以显式 `openai-codex/gpt-6-astra` 执行 ephemeral、no-tools 真实 challenge；输出不含凭据、prompt、response 或内部会话标识。
- `model-release:control` 只接受 `ownermac → TCloud → Vultr` 顺序 receipt；每端必须同一 SHA，且 runtime、目录和真实对话均通过。首个失败端令 `next_site=null`，禁止晋级；任何 rollback failure 产生 critical blocker。
- `.github/workflows/model-release-monitor.yml` 每 15 分钟和手工运行在线发现及三端受信探针；也监听 TCloud/Vultr deployment workflow，读取 rollback step 结果。它通过 GitHub Issue 交付确定性唯一 incident，指派 owner；只接受该 owner 的精确 `/ack model-release`，30 分钟未确认自动升级。任何 open incident 或当前失败都令 Actions check 失败。
- 监控 workflow 只有 readback 与 conversation 权限，不执行 activation、restart 或 rollback。发布仍必须经过既有人工门禁和 capacity-gated release writer；监控通过不等于授权发布。

Relay 默认模型与 `platform_default`/`frontier` 动态 alias 固定为 Astra；runtime-owned catalog 不含 Astra或 reasoning 能力不足时继续 fail closed。
