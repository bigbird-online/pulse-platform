# 跨环境 Agent 协作架构方案

> 讨论日期：2026-06-11 | 参与者：长征 + Artificer
> 状态：讨论中，待细化 spec

---

## 一、背景与问题

### 两个运行环境

| 维度 | Mac Mini（本机） | Vultr-Pulse |
|------|-----------------|-------------|
| 资源 | 充足（CPU/内存/硬盘） | 不足 |
| 模型 | 非 Claude 官方 | Claude 官方 |
| Agent | 11 个（运营官/Butler/Artificer/Gardener 等） | PULSE 平台租户 |
| 网络 | 通过 Tailscale 互通 | 同上 |

### 核心需求

1. 本地 Agent 把需要 Claude 官方模型的工作**同步阻塞委托**给 Vultr-Pulse 上的 Agent
2. 很快会有多个类似协作场景 → 架构需通用化
3. 内容只传文本+图片（不传音频视频），走 GitHub 或内联

### 第一个用例：运营官 ↔ 审查员

- 运营官（Mac Mini）生产多媒体内容（脚本/文稿/图片）
- 审查员（Vultr-Pulse，Claude 驱动）在关键环节代表长征审查
- 同步阻塞：运营官等待审查结果后才继续
- 审查内容：文稿、脚本、图片（不审查音频视频）
- 审查输出：文本（通过/修改建议/驳回）

---

## 二、架构设计

### 核心思路

**取 A2A 协议语义，做最小 HTTP JSON 实现。数据模型兼容 A2A，传输层用最轻方案。当 A2A 生态成熟时，换 transport adapter 即可迁移到标准 A2A。**

### 三层互操作参考

```
MCP  = Agent ↔ Tool（工具连接）    — 已有
A2A  = Agent ↔ Agent（协作通信）   — 本次实现
UMP  = Agent ↔ Memory（跨会话记忆）— 暂不实现，跟踪中
```

### 整体架构

```
Mac Mini（本地算力池）              Vultr-Pulse（Claude 算力池）
┌────────────────────────┐         ┌────────────────────────┐
│  本地 Agent (CC session)│         │  A2A Gateway            │
│  │                     │         │  (FastAPI, 轻量)         │
│  │  harness remote-*   │         │                         │
│  └───────┬─────────────┘         │  Agent Card registry    │
│          │                        │  ├── reviewer           │
│          │ HTTP POST (Tailscale)  │  ├── translator (未来)   │
│          └───────────────────────►│  └── ...                │
│          │ 阻塞等待                │                         │
│          │                        │  Claude API 调用         │
│          ◄───────────────────────│  （per Agent 的 prompt） │
│          审查结果（文本）          │                         │
│                                │  └────────────────────────┘
│  harness CLI 扩展               │
│  ├── remote-review             │
│  ├── remote-list               │
│  └── remote-card               │
└────────────────────────┘         └────────────────────────┘
```

---

## 三、协议设计（A2A-compatible）

### 数据模型（与 A2A 对齐）

```python
# 消息
class RemoteMessage(BaseModel):
    role: Literal["user", "agent"]
    parts: list[Part]

class Part(BaseModel):
    kind: Literal["text", "url", "data"]
    text: str | None = None       # kind=text
    url: str | None = None        # kind=url
    data: dict | None = None      # kind=data
    media_type: str | None = None

# 任务
class RemoteTask(BaseModel):
    task_id: str
    agent_id: str
    status: Literal["submitted", "working", "completed", "failed"]
    message: RemoteMessage        # 输入
    artifact: RemoteMessage | None = None  # 输出
    created_at: datetime
    completed_at: datetime | None = None

# Agent 名片
class AgentCard(BaseModel):
    agent_id: str
    name: str
    description: str
    capabilities: list[str]       # ["review_script", "review_image", ...]
    input_types: list[str]        # ["text", "image_url"]
    output_types: list[str]       # ["text"]
    endpoint: str                 # URL
```

### API 端点（A2A 兼容）

| 端点 | 方法 | 说明 |
|------|------|------|
| `GET /a2a/agent-card` | GET | 获取 Gateway 上所有 Agent 的名片 |
| `GET /a2a/agent-card/{agent_id}` | GET | 获取指定 Agent 名片 |
| `POST /a2a/message` | POST | 发送消息（同步阻塞，返回结果） |
| `GET /a2a/task/{task_id}` | GET | 查询任务状态（异步场景用） |

---

## 四、通知架构

### 决策：路径 B — Harness 统一走 Relay push 端点

**现状**：
- Harness 的 `escalation.py` 直接 curl 调 Telegram Bot API（只有 TG，没有飞书）
- Relay 有 `/internal/push` 端点，支持飞书和 TG 两种渠道（companion 伴读技能在用飞书）

**改造方向**：
- Harness 的 `push_now` 不再直调 TG curl，改为调 Relay `/internal/push`
- 由 Relay 负责路由到飞书/TG/其他渠道
- Harness 只管"什么时候通知什么内容"，不管"通过什么渠道发"

```
改造前：
  harness escalation.py → curl → Telegram Bot API

改造后：
  harness escalation.py → HTTP POST → Relay /internal/push → 飞书 / TG / ...
```

### 通知场景矩阵

| 场景 | 触发方 | 通知渠道 | 能否做到 |
|------|--------|---------|---------|
| 你通过飞书发起运营官 → 运营官汇报 | 运营官 CC session | 飞书（同一对话） | 能（现有能力） |
| Cron 定时发起运营官 → 运营官汇报 | 运营官 cron session | 飞书（通过 Relay push） | 能（需改造 push_now） |
| 审查员接到任务 → 汇报 | A2A Gateway | 飞书（通过 Relay push） | 能（Vultr-Pulse 的 Relay 实例） |
| 审查完成 → 结果通知 | A2A Gateway | 飞书（通过 Relay push） | 能 |

### 改造范围

```python
# harness/escalation.py 改造点

# 改造前
def _curl_telegram(bot_token, chat_id, text) -> bool:
    # curl -X POST https://api.telegram.org/bot{token}/sendMessage

# 改造后
def _relay_push(agent_id: str, message: str, channel: str = "feishu") -> bool:
    relay_endpoint = os.environ.get("RELAY_PUSH_ENDPOINT", "http://127.0.0.1:8787/internal/push")
    # HTTP POST relay_endpoint
    # payload: { agent_id, message, channel, ... }
```

**关键改动**：
- `push_now()` 内部调用从 `_curl_telegram` 换成 `_relay_push`
- Policy 的 `escalation.channel` 字段从硬编码 `"telegram"` 变成可配置（`telegram` / `feishu`）
- Bot token 解析逻辑从 `RELAY_BOT_TOKEN_*` 改为 `RELAY_PUSH_ENDPOINT`
- 保持向后兼容：如果 `RELAY_PUSH_ENDPOINT` 未配置，降级到原 TG curl

---

## 五、实现路径

### Phase 0：基础设施（1-2 天）

**Vultr-Pulse 上：**
```
a2a-gateway/
├── server.py          # FastAPI 服务（端口 8900）
├── agents/
│   └── reviewer.py    # 审查员 Agent（Claude API 封装）
├── agent_cards/
│   └── reviewer.json  # Agent Card（能力声明）
├── models.py          # A2A-compatible 数据模型
└── config.yaml        # API keys + Agent 注册
```

**Mac Mini 上（harness 扩展）：**
```
harness/
├── remote.py          # CLI 子命令组（新增）
├── remote_client.py   # A2A-compatible HTTP client（新增）
├── remote_models.py   # 数据模型（新增，与 gateway 侧对齐）
├── escalation.py      # 改造 push_now → 走 Relay push
└── cli.py             # 加一行 main.add_command(_remote_group)
```

### Phase 1：通知改造（1 天）

1. 改 `escalation.py` 的 `push_now` → 走 Relay `/internal/push`
2. 改 Policy 的 `escalation.channel` 为可配置
3. 降级兼容：无 `RELAY_PUSH_ENDPOINT` 时回退到 TG curl
4. 测试：harness escalation emit → 飞书收到消息

### Phase 2：审查员 Agent（3-5 天）

1. 定义 Agent Card（能力描述、输入输出类型）
2. 实现 Claude API 调用逻辑（system prompt + 审查标准 + 用户内容）
3. 实现同步审查接口
4. 实现 harness CLI 扩展（remote-review）
5. 端到端测试

### Phase 3：多 Agent 扩展

- 新增远程 Agent 只需在 gateway 加一个文件 + Agent Card
- 客户端通用，不需要改代码

### Phase 4：协议标准化

- A2A 生态成熟时，HTTP JSON → 标准 A2A SDK
- 数据模型已兼容，迁移成本低

---

## 六、完整协作流程（运营官 ↔ 审查员，含通知）

```
① 你通过飞书 bot 发起任务 → 运营官 CC session 启动

② 运营官产出脚本初稿
   → 运营官调 Relay push: "脚本初稿完成，准备提交审查"
   → 你收到飞书消息

③ 运营官调用 harness remote-review --agent reviewer ...
   → Harness 构造 A2A 请求
   → HTTP POST 到 Vultr-Pulse A2A Gateway（Tailscale）
   → 阻塞等待

④ Vultr-Pulse A2A Gateway 收到请求
   → Gateway 调 Relay push（飞书）: "审查员接到审查任务：第12集脚本"
   → 你收到飞书消息
   → 调 Claude API 执行审查
   → 审查完成

⑤ Gateway 调 Relay push（飞书）: "审查完成：通过，2条建议"
   → 你收到飞书消息

⑥ Gateway HTTP 200 返回审查结果给 Mac Mini

⑦ Harness 解析结果，记录事件
   → 运营官根据结果继续或修改

⑧ 运营官完成后续步骤
   → 运营官调 Relay push: "第12集生产完成"
   → 你收到飞书消息
```

---

## 七、关键设计决策

| # | 决策 | 选择 | 理由 |
|---|------|------|------|
| 1 | 审查员形态 | FastAPI + Claude API 直调 | 资源少，不需要 CC session |
| 2 | 通信协议 | A2A 语义 + HTTP JSON | 轻量、可迁移到标准 A2A |
| 3 | 内容传递 | 文本内联 + 图片走 GitHub URL | 带宽友好 |
| 4 | 集成方式 | harness CLI 扩展 | 复用现有工具链 |
| 5 | 记忆共享 | 暂不做，prompt 内联 | UMP 太早期 |
| 6 | 安全 | Tailscale + API key | 单租户足够 |
| 7 | 通知渠道 | Harness → Relay /internal/push → 飞书/TG | 解耦，不改 harness 通知渠道细节 |
| 8 | 通知降级 | 无 Relay 时回退 TG curl | 向后兼容 |

---

## 八、待细化

- [ ] 审查员的具体审查标准（system prompt 定义）
- [ ] 图片传递的具体方式（GitHub URL vs base64 inline）
- [ ] 超时重试策略
- [ ] Vultr-Pulse 资源评估（API 并发限制）
- [ ] Relay /internal/push 的 payload 格式确认
- [ ] harness push_now 改造的向后兼容测试

---

## 九、参考资料

- A2A 协议调研：`PARA/Resources/参考项目/Google-A2A协议-研究.md`
- UMP 协议调研：`PARA/Resources/参考项目/UMP协议-研究.md`
- agent-harness heartbeat-runtime：`Bigbird/Lab/BigbirdEnvironment/agent-harness/features/heartbeat-runtime/`
- agent-harness 通知实现：`Bigbird/Lab/BigbirdEnvironment/agent-harness/harness/escalation.py`
