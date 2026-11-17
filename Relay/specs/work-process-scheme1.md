# 工作过程头部：方案一

2026-09-19 用户确认的展示修订，属于 Issue #535 / PR #636 的同一消费者结果。
补充 [通道渲染契约](channel-rendering-contract.md)；仅替代原来“▶ 展开＋▶ 已收到”的视觉排版，不改变计数、回合、权限、归属或最终投递规则。

## 两端共同结果

左侧为有明确点击区域的强调色按钮：`⌄ 展开过程` / `⌃ 收起过程`。
右侧状态与准确的执行步骤数、用时在同一文本流显示，以 ` · ` 分隔，不强制另起一行；不重复聊天客户端自己的时钟。窄屏允许自然换行，不裁切按钮或隐藏时长。
外层头部不使用彩色播放 emoji，也不把展开箭头与工作状态图标混为一谈。

- 收到：`◌ 已收到，开始处理…`。
- 工作：`◌ 仍在工作… · 已推进 N 个执行步骤 · 已用 X 分 Y 秒`。
- 完成：`✓ 本轮回复已生成 · 用时 X 分 Y 秒`，终态用时固定。
- 未完成：`! 本轮回复未完成`；不能以颜色或成功符号假报完成。

`◌` 只是静态工作状态标记，不宣称是连续动画或新的存活证明。用时随实际渲染采样，不增加刷新定时器或合成步骤；终态按已记录 finishedAt 冻结，旧记录无结束时间则不补造时长。
原 activityMilestone、WorkProcessRecord.summary 及最终答复保持不变；仅渲染时将状态与计数分层。

## 分别使用原生组件

TG 使用 Rich Message 的原生 callback button（primary）和同行状态文本；正文继续使用段落，不使用不受支持的 CSS、圆角值或任意 div 布局。普通消息回退使用已有 inline keyboard；这个回退保留功能，不冒充与 Rich Message 像素一致。
飞书使用 JSON 2.0 button、column_set、markdown：按钮列 auto，状态列 weighted，用时通过同一 markdown 中的 grey font 弱化；细分隔符与留白明确层次，不使用曾裁掉图标的固定窄列，不把 TG HTML 塞进飞书卡片。
两端都沿用原 callback_data / relay_work_process，进入同一 WorkProcessView 的开合状态机。原生按钮颜色、圆角和字体由客户端主题决定，设计图是视觉目标，不承诺任意 CSS 或逐像素相同。

## 展开与收起

正文为实际输出、有序的公开工作说明；原生公开摘要保留原文与顺序，但明确标注“原生推理摘要”，不当作自然工作说明或原始内部思考。工具仍为紧凑条目，输入与结果默认隐藏；单独点击才展开。计数刷新不能自动收起用户已打开的过程；唯一最终答复确认送达后收起一次，之后可以重开。
Feishu 的 schema 2.0 event/operator/context 封装和 TG 的 callback query 地址分别解析，不因展示一致就复用错误的消息 ID 或收件人字段。

## 验证与边界

自动化应检查两端同一状态的文案/计数/时长、原生组件层次、完整回调路径、错误收件人拒绝、连续开合、刷新保持、工具正文隐藏和最终答复隔离。

`@anchor test Relay/tests/work-process-header.test.ts :: "removes the duplicate play emoji without changing accepted state"`
`@anchor test Relay/tests/work-process-header.test.ts :: "keeps final duration fixed and old unknown duration honest"`

自动化 sink 测试使用模拟服务端回执，不等于真实接口接受，不等于手机/桌面布局或真实用户点击通过。正式验收必须另外记录 TG 与飞书各自的真实创建/更新、上游点击回调、桌面和手机展开效果；不能从 TG 通过外推飞书通过。
逐次执行证据与未完成验收项记录在 Issue #535／PR／Actions，不将一次性运行状态当作永久产品约定。

官方格式来源：[Telegram RichMessageButton](https://core.telegram.org/bots/api#richmessagebutton)、[Telegram Rich HTML](https://core.telegram.org/bots/api#rich-html-style)、[飞书 JSON 2.0 按钮](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/interactive-components/button)。

## 自然公开沟通与来源

Pi 使用现有可重复的 `--append-system-prompt` 追加简短沟通约定，不替换 Agent 身份、已审工作区上下文或 WI 约束。对多步骤调查和实质性分析，在有用时说明当前做法、已有证据和不确定性；简单问答直接答复。没有固定阶段台词、强制字数、计时伪进展或为展示而添加的工具调用。仅保留实际发出的公开说明，不从摘要补写一段所谓原始思考。

`WorkProcessEvent.source=reasoning_summary` 标识原生公开摘要；旧归档只识别既有适配器生成的 `pi:summary:`／`codex:summary:` ID，不从英文标题或内容猜来源。来源随合并与归档保留，并在两端和完整只读文件中标注。两种内容不拼入最终答案。

验收使用普通问题（短问、实质分析、有工具调查），不在用户测试问题中指定“阶段A/B/C”或要说的过程句子。检查真实公开输出、自然语言、同排终态时长、原文一致、工具默认隐藏和独立最终答复；模型没有发出公开说明时，不伪造补齐。

`@anchor test Relay/tests/work-process-header.test.ts :: "keeps 41-second duration after completion in the same native text row (expanded=%s)"`
