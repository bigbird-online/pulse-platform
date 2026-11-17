# Relay 官方订阅配置指南

> 版本：v1.0 | 日期：2026-06-05
> 适用场景：在海外 VPS（如 Vultr）上使用 Claude 官方订阅 + Codex 官方订阅

---

## 架构前提

Relay 区分两种 runtime，由 `models.json` 中 provider 的 `protocol` 字段决定：

| protocol | runtime | 认证方式 | 默认端点 |
|----------|---------|----------|----------|
| `"anthropic"` | CC（Claude Code） | `CLAUDE_CODE_OAUTH_TOKEN` 或 provider key pool（如 `OPENROUTER_ANTHROPIC_AUTH_TOKEN`） | `api.anthropic.com` 或 provider 的 `base_url_env` |
| `"openai"` | Codex | `OPENAI_API_KEY` | `api.openai.com` |

关键代码路径：
- CC 认证：`pool.ts` → `resolveModelProviderEnv()` + OAuth token 注入
- Codex 认证：`codex-conversation-runtime.ts` → `buildCodexEnv()` → 注入 `OPENAI_BASE_URL` + `OPENAI_API_KEY`

---

## 一、Claude 官方订阅

### 方式 A：OAuth Token（推荐 — Max/Pro 订阅）

订阅模式，不限量（有 rate limit），无需 API Key。

#### 步骤 1：获取 OAuth Token

在目标服务器上以 Relay 服务用户执行：

```bash
# 以 pulse 用户登录
su - pulse

# CC 登录（会弹出浏览器链接，复制到本地浏览器完成授权）
claude login

# 登录成功后 token 存在
cat ~/.claude/.credentials.json
```

#### 步骤 2：配置 relay.service 环境变量

```ini
# OAuth Token（从 ~/.claude/.credentials.json 复制）
Environment=CLAUDE_CODE_OAUTH_TOKEN=<oauth-token>

# 以下两个不要设（OAuth 模式下 CC 自行处理端点和认证）
# Environment=ANTHROPIC_BASE_URL=       ← 不设
# Environment=ANTHROPIC_AUTH_TOKEN=      ← 不设
```

#### 步骤 3（可选）：配置 models.json

OAuth 模式下 **models.json 不是必须的**，CC 原生识别 `sonnet`、`opus`、`haiku` 等别名。不加的话基本对话正常，但 `/model` 命令看不到 Claude 模型，也没有计费数据。

如需完整体验，在 `models.json` 的 `providers` 中确认有 `anthropic` provider，`models` 中添加：

```json
{
  "providers": {
    "anthropic": {
      "protocol": "anthropic",
      "base_url_env": "ANTHROPIC_BASE_URL",
      "api_key_pool": ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN_FALLBACK"],
      "force_interactive": false
    }
  },
  "models": [
    { "id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6", "provider": "anthropic", "currency": "USD", "input_price_per_million": 3, "output_price_per_million": 15 },
    { "id": "claude-opus-4-6", "display_name": "Claude Opus 4.6", "provider": "anthropic", "currency": "USD", "input_price_per_million": 15, "output_price_per_million": 75 },
    { "id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5", "provider": "anthropic", "currency": "USD", "input_price_per_million": 0.8, "output_price_per_million": 4 }
  ]
}
```

#### 步骤 4：relay.json Agent 配置

```json
{
  "id": "pulse",
  "ccModel": "sonnet",
  ...
}
```

CC 原生识别的别名：`sonnet` → Sonnet 4.6，`opus` → Opus 4.6，`haiku` → Haiku 4.5。

#### OAuth 模式注意事项

- **Token 刷新**：CC 自动刷新 OAuth token，无需手动更新
- **登录绑定**：`claude login` 必须在目标服务器上执行，token 绑定机器
- **与 API provider 共存**：`ANTHROPIC_BASE_URL` 不设，不影响 API provider（它们有独立的 `XXX_ANTHROPIC_BASE_URL` 环境变量，如 `OPENROUTER_ANTHROPIC_BASE_URL`）

---

### 方式 B：OpenRouter API（按量付费）

在 [OpenRouter](https://openrouter.ai/) 获取 API Key，以 provider 专属变量注入，不设裸 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`：

```ini
# relay.service 环境变量
Environment=OPENROUTER_ANTHROPIC_BASE_URL=https://openrouter.ai/api
Environment=OPENROUTER_ANTHROPIC_AUTH_TOKEN=sk-or-xxxxxx
```

`models.json` 中声明 `openrouter` provider，模型 id 使用 OpenRouter 的上游前缀形式：

```json
{
  "providers": {
    "openrouter": {
      "protocol": "anthropic",
      "base_url_env": "OPENROUTER_ANTHROPIC_BASE_URL",
      "api_key_pool": ["OPENROUTER_ANTHROPIC_AUTH_TOKEN"],
      "force_interactive": false
    }
  },
  "models": [
    { "id": "deepseek/deepseek-v4-flash-0731", "display_name": "DeepSeek V4 Flash · 快省(OpenRouter)", "provider": "openrouter", "currency": "USD", "input_price_per_million": 0.09, "output_price_per_million": 0.18 }
  ]
}
```

用户在对话中用 `/model <id>` 选择这些模型；spawn 时 Relay 只为该会话注入 provider 的端点与 key。

---

## 二、Codex 官方订阅（OpenAI）

### 步骤 1：安装 Codex CLI

```bash
npm install -g @openai/codex
```

确认可用：

```bash
codex --version
```

### 步骤 2：配置环境变量

```ini
# relay.service 环境变量
Environment=OPENAI_API_KEY=sk-xxxxxx

# OPENAI_BASE_URL 不设（默认指向 api.openai.com）
```

### 步骤 3：配置 models.json

Codex 模型必须使用 `"openai"` 协议的 provider：

```json
{
  "providers": {
    "openai": {
      "protocol": "openai",
      "base_url_env": "OPENAI_BASE_URL",
      "api_key_pool": ["OPENAI_API_KEY"],
      "force_interactive": false
    }
  },
  "models": [
    { "id": "codex-mini-latest", "display_name": "Codex Mini", "provider": "openai", "currency": "USD", "input_price_per_million": 1.5, "output_price_per_million": 6 },
    { "id": "o3-pro", "display_name": "O3 Pro", "provider": "openai", "currency": "USD", "input_price_per_million": 2, "output_price_per_million": 8 }
  ]
}
```

### 步骤 4：Agent 使用 Codex 模型

在 `relay.json` 中指定 Codex 模型即可自动走 Codex runtime：

```json
{ "id": "some-agent", "ccModel": "codex-mini-latest", ... }
```

用户也可在聊天中动态切换：

```
/model codex-mini-latest
/runtime codex
```

---

## 三、完整 relay.service 环境变量示例

以下展示 Claude OAuth + Codex + OpenRouter 共存的完整配置：

```ini
[Unit]
Description=Pulse Relay Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/PULSE/Relay
ExecStart=/usr/local/bin/node /opt/PULSE/Relay/dist/cli.js
Restart=always
RestartSec=5

# ── Claude 官方（OAuth 订阅模式）──
Environment=CLAUDE_CODE_OAUTH_TOKEN=<oauth-token>

# ── Codex 官方 ──
Environment=OPENAI_API_KEY=sk-xxxxxx

# ── OpenRouter（与官方共存，不冲突）──
Environment=OPENROUTER_ANTHROPIC_BASE_URL=https://openrouter.ai/api
Environment=OPENROUTER_ANTHROPIC_AUTH_TOKEN=<openrouter-key>

# ── 通用 ──
Environment=RELAY_CONFIG_PATH=/home/pulse/.relay/relay.json
Environment=METERING_DIR=/home/pulse/.relay/metering/
Environment=CLAUDE_BIN=/usr/local/bin/claude
Environment=HOME=/home/pulse
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=RELAY_MODEL_CATALOG_PATH=/opt/PULSE/Relay/config/models.json
Environment=RELAY_HTTP_PORT=8788
Environment=PRICING_CONFIG_PATH=/opt/PULSE/data/pricing.json

[Install]
WantedBy=default.target
```

---

## 四、配置后操作

```bash
# 重启 Relay
systemctl --user restart relay.service

# 验证服务状态
systemctl --user status relay.service --no-pager

# 查看日志确认无认证错误
journalctl --user -u relay.service -n 30 --no-pager | grep -i 'error\|fail\|auth'
```

---

## 五、网络要求

| 端点 | 海外 VPS（Vultr） | 国内 VPS（阿里云） |
|------|:-:|:-:|
| `api.anthropic.com` | ✅ 直连 | ❌ 需代理 |
| `api.openai.com` | ✅ 直连 | ❌ 需代理 |

海外 VPS 的优势：Claude/Codex 官方端点直连，无需 Exit Node 或代理。

---

## 六、常见问题

### Q: OAuth 和 API Key 能同时用吗？
不能。设了 `CLAUDE_CODE_OAUTH_TOKEN` 后 CC 优先走 OAuth，`ANTHROPIC_AUTH_TOKEN` 被忽略。

### Q: API provider 会和官方冲突吗？
不会。API provider 使用独立的环境变量（如 `OPENROUTER_ANTHROPIC_BASE_URL`），由 `models.json` 中各自的 provider 定义路由，与 `ANTHROPIC_BASE_URL`（官方）互不干扰。

### Q: Codex 模型和 CC 模型能在同一个 Agent 切换吗？
能。用户通过 `/model` 或 `/runtime` 命令即可切换。`models.json` 中 provider 的 `protocol` 字段决定走哪个 runtime。

### Q: OAuth Token 过期了怎么办？
CC 自动刷新。如果刷新失败（如订阅到期），Relay 日志会出现认证错误，重新 `claude login` 即可。

### Q: apiKeyHelper 和 OAuth 的关系？
`apiKeyHelper` 是多租户场景下（`linuxUser` 隔离）的 API Key 传递机制，与 OAuth 无关。OAuth 模式下如果不需要多租户隔离，不涉及 apiKeyHelper。
