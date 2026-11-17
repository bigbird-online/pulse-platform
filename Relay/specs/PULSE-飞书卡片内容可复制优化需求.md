# PULSE Relay 飞书卡片代码复制能力调研与界面决策

> **来源**：2026-05-26 例会、2026-09-04 真实渠道 UAT
> **优先级**：P1（用户体验直接影响使用意愿）
> **最近核查**：2026-09-04

## 问题

飞书 bot 的 Interactive Card 可以用 Markdown 展示代码块，但桌面端和移动端都不提供 Telegram 那种位于代码块右上角的原生复制图标。卡片文本选择体验也不稳定，尤其是移动端。

Relay 曾尝试两种绕行方案：

1. 每张回答卡附加「发送可复制版本」按钮；
2. 每个围栏代码块附加「发送代码 N（可复制）」按钮。

两者只能触发服务端 callback，再由 bot 发送一条纯文本消息；它们不能直接写客户端剪贴板。2026-09-04 真实 UAT 判定这类独立按钮明显破坏回答卡的视觉层级，收益不足以抵偿界面噪声。

## 当前产品决策

- 新生成的普通回答卡不附加整答或代码块复制按钮。
- 围栏代码继续作为 CardKit Markdown 代码块自然渲染。
- 菜单、问答选项、确认等具有真实业务语义的交互按钮不受影响。
- 已发旧卡无法追溯修改；Relay 暂时保留 `relay_copy` 与 `relay_copy_code` callback 解析，以免旧卡点击后失效，但新卡不再生成这些 control。
- Telegram 保持客户端原生代码块复制体验，不增加 Relay 按钮。

## 官方能力核查

### CardKit 富文本组件

飞书官方[富文本（Markdown）组件](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/content-components/rich-text)支持围栏代码块及语言高亮，但公开 JSON 2.0 字段中没有：

- `copyable` 或 `selectable` 属性；
- 代码块级 action/slot；
- 可要求客户端显示复制图标的属性；
- 独立、可寻址的 `code_block` 卡片组件。

代码块只是 `tag: "markdown"` 的 `content` 内部渲染结果，bot 不能向其右上角注入原生控件。

### Button 与交互行为

飞书官方[按钮组件](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/interactive-components/button)当前公开的 `behaviors` 只有：

- `open_url`：打开链接；
- `callback`：把有界数据回传服务端。

没有 `clipboard`、`copy_text` 或等价的本地剪贴板行为。CardKit entity/element update 只能更新卡片内容，不能获得客户端剪贴板权限。

## 替代方案评估

| 方案 | 可行性 | 结论 |
|---|---|---|
| 独立 callback 按钮后再发纯文本 | 可实现，但不是真复制且制造视觉、消息噪声 | 已拒绝 |
| `column_set` + tiny/text 图标模拟右上角 | 只能放在代码块外，仍需 callback，桌面/移动布局不稳定 | 不冒充原生能力 |
| 回答后自动追加纯文本代码消息 | 可选择复制，但重复内容、打断会话 | 默认不采用 |
| 代码作为文件附件 | 适合大文件，不适合短命令；需要下载/打开 | 仅按用户明确请求使用 |
| 打开网页/小程序后由 JavaScript 复制 | 依赖额外页面、用户手势和权限，路径比手动选择更长 | 不采用 |
| 创建飞书文档并跳转 | 权限、生命周期和垃圾文档成本高 | 不采用 |
| 等待飞书客户端原生能力 | 能达到 Telegram 同级体验 | 持续关注 |

## 重新评估触发条件

仅当飞书公开并在桌面端、iOS、Android 均可验证以下任一能力时重新设计：

1. CardKit button/interaction 新增本地 `clipboard` / `copy_text` 行为；
2. Markdown 组件新增 `copyable` 或代码块 action slot；
3. 新增一等 `code_block` 组件并由客户端原生提供复制图标；
4. 飞书客户端自动为 bot 卡片的围栏代码块提供复制图标，无需 payload opt-in。

## 验收标准

- [ ] 新的普通飞书回答卡不出现「发送可复制版本」或「发送代码 N」按钮。
- [ ] 代码块、表格、列表、链接、流式更新和长答 rollover 保持正确。
- [ ] 菜单、问答和确认类交互按钮保持可用。
- [ ] 桌面端和移动端视觉验收通过。
- [ ] Telegram 渲染不受影响。
