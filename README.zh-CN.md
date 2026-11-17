# PULSE

PULSE 是一套自托管的 AI Agent（智能体）协作底座：你在飞书或 Telegram 里给 Agent 发消息，Agent 跑在你自己的服务器上、用你自己的订阅；工作项、成本与用量等模块按需接入。

[English](README.md)

**现状：** 首个公开版本。Relay（聊天渠道与 Agent 运行时）是目前支持的入口，也是唯一有完整安装说明的模块。其他模块的代码与测试已在仓内，安装说明后续逐个补齐。

## 快速开始：只部署 Relay（飞书 + Codex + `gpt-6.1-sol`）

需要：一台 Linux 服务器（已在 Ubuntu 24.04 上验证）、Node.js 22、一个 ChatGPT Plus 及以上账户（供 Codex 使用）、一个飞书自建应用。[完整说明](docs/quickstart-feishu-codex.md)（英文）逐步讲解，并附五步验收。

```bash
git clone https://github.com/bigbird-online/pulse-platform.git ~/pulse
cd ~/pulse/Relay
npm ci && npm run build

# 用 ChatGPT 账户登录 Codex（设备码方式，服务器上不需要浏览器）
./node_modules/.bin/codex login --device-auth
./node_modules/.bin/codex exec --skip-git-repo-check -m gpt-6.1-sol "用一句话自我介绍"
```

写一份最小配置 `~/.config/relay/relay.json`：

```json
{
  "users": [{
    "userId": "owner",
    "defaultAgent": "assistant",
    "agents": [{
      "id": "assistant",
      "name": "助手",
      "emoji": "🤖",
      "channel": "feishu",
      "runtime": "codex",
      "workspace": "/home/relay/workspace",
      "feishu": { "appId": "<APP_ID>", "appSecret": "<APP_SECRET>", "sessionId": "<任意 UUID>" }
    }]
  }]
}
```

启动（完整说明里给出同样内容的 systemd 用户服务）：

```bash
RELAY_CONFIG_PATH=~/.config/relay/relay.json \
RELAY_DATA_DIR=~/.local/share/relay \
RELAY_CODEX_MODEL=gpt-6.1-sol \
RELAY_HTTP_PORT=8788 RELAY_INTERNAL_TOKEN="$(openssl rand -hex 24)" \
node ~/pulse/Relay/dist/cli.js
```

在飞书里给机器人发一条消息，第一条会收到配对码；在服务器上批准后，助手开始回复。

## 特性

- **聊天渠道**：飞书（长连接，服务器不需要公网地址）与 Telegram（长轮询）。
- **Agent 运行时**：OpenAI Codex CLI（默认）、Claude Code（`cc`、`cc-sdk`）与 Pi。每个 Agent 选一种运行时，节点可设回退顺序。
- **按人放行**：只有已配对或已配置的用户能用 Agent，其他人只收到配对码或拒绝提示。
- **限定工作目录**：每个 Agent 在一个目录里工作；Codex 在 `workspace-write` 沙箱内运行。
- **流式回复**：进度卡片、可折叠的工作过程、最终答复单独投递。
- **定时任务**：按 cron 表达式跑一轮 Agent，把结果发到聊天（批处理依赖下文“运行时”一节的 Python 托管进程）。
- **本机接口**：`POST /internal/push` 等接口让本机程序借你的机器人发消息。
- **可选平台模块**：工作项内核、算力成本核算、用量看板、租户开通。

## 架构

```text
 飞书 / Telegram
        │
        ▼
┌──────────────────────── Relay（Node.js）────────────────────────┐
│ 渠道 → 路由（用户、Agent、配对）→ 会话池                        │
│ 调度器 · 本机 HTTP 接口（127.0.0.1）                            │
└───────────────┬──────────────────────────────┬──────────────────┘
                │ 拉起                         │ 可选（PULSE_RELAY_PATHS）
                ▼                              ▼
   Codex CLI / Claude Code / Pi        agent-harness（工作项内核）
   运行在 Agent 工作目录               ComputeCost → Stats
```

| 目录 | 内容 | 现状 |
| --- | --- | --- |
| `Relay/` | 聊天网关、Agent 运行时、调度器、本机接口（TypeScript） | 支持，有安装说明 |
| `agent-harness/` | 工作项（WI）内核：执行权、协作合同、PR 审核关卡（Python） | 代码与测试；说明待补 |
| `ComputeCost/` | 把模型用量与成本归到 Agent 和工作项（Python） | 代码与测试；说明待补 |
| `Stats/` | 成本与用量看板：导出器、发布器、macOS App | 代码与测试；说明待补 |
| `Platform/` | 租户开通网页流程（TypeScript） | 代码与测试；说明待补 |
| `Scripts/` | Relay 运行时调用的辅助脚本（托管进程启动器）；部署与运维工具不公开 | 运行时辅助 |
| `specs/`、`*/specs/` | 产品规范；以代码和测试为准 | 参考 |
| `skills/` | 使用本平台的 Agent 技能（如 Relay 推送） | 参考 |

## 配置

Relay 读取一个 JSON 文件（`RELAY_CONFIG_PATH`），全部字段见 [`Relay/config/relay.example.json`](Relay/config/relay.example.json)。常用环境变量：

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `RELAY_CONFIG_PATH` | `relay.json` 路径 | `<仓根>/data/relay.json` |
| `RELAY_DATA_DIR` | Relay 写入的会话等状态 | `~/.relay` |
| `CRON_JOBS_PATH` | 定时任务文件 | `<仓根>/data/cron-jobs.json` |
| `METERING_DIR` | 用量记录 | `<仓根>/data/metering/` |
| `RELAY_CODEX_MODEL` | Codex 模型 | `gpt-6-astra` |
| `RELAY_CODEX_REASONING_EFFORT` | Codex 推理强度 | `xhigh` |
| `RELAY_HTTP_PORT` | 设了才在该端口开本机接口 | 关闭 |
| `RELAY_HTTP_HOST` | 本机接口监听地址 | `127.0.0.1` |
| `RELAY_INTERNAL_TOKEN` | 本机接口要求的 `x-relay-token` 请求头 | 不设则不校验 |
| `PULSE_COEVOLUTION_ROOT` | Codex 额外可写目录（`--add-dir`） | 不设则没有 |
| `PULSE_RELAY_PATHS` | 受管模式：路径映射，开启工作项内核 | 不设即独立运行 |

`relay.json` 含渠道密钥，放在权限 `0700` 的目录里，文件权限 `0600`。

## 运行时

| 运行时 | 需要 | 说明 |
| --- | --- | --- |
| `codex`（默认） | 随包的 `@openai/codex`；ChatGPT 登录或 API key | 以 `codex exec -a never -s workspace-write -C <工作目录>` 运行 |
| `cc` | Claude Code CLI；托管进程需要 `~/.local/share/coevolution/python-env/bin/python` 处的 Python 3.11+（已知的写死路径） | 交互式进程池；各运行时的定时批处理都用这个托管进程 |
| `cc-sdk` | 可选依赖 `@anthropic-ai/claude-agent-sdk` | 进程内 SDK 会话；未安装该包时，此运行时会提示缺包 |
| `pi` | Pi 运行时清单（`RELAY_PI_RUNTIME_MANIFEST_PATH`） | 可选，见 `Relay/specs/pi-conversation-runtime.md` |

## 安全模型

- **谁能用**：Telegram Agent 只接受配置里的 chat ID；飞书 Agent 只接受已绑定的用户，陌生人只收到五分钟有效的配对码，在服务器上批准前收不到任何 Agent 回复。
- **能写哪里**：Codex 以 `-s workspace-write` 在 Agent 的 `workspace` 内运行。独立模式下，除非设了 `PULSE_COEVOLUTION_ROOT`，Relay 不再加任何可写目录。陌生人能接触到的 Agent 不要开 `codexYoloDefault`。
- **本机接口**：默认只监听 `127.0.0.1`。请设置 `RELAY_INTERNAL_TOKEN`，防止本机其他用户调用。
- **密钥**：渠道凭据只在你的 `relay.json`（或 `RELAY_SECRETS_ENV_FILE` 指向的文件）里；Codex 凭据在 `~/.codex/auth.json`。两者都按密码对待。
- 报告漏洞见 [SECURITY.md](SECURITY.md)。

## 从 Relay 扩展到完整的 PULSE

不设 `PULSE_RELAY_PATHS` 时 Relay 独立运行：工作项命令、常驻授权与会话证明都关闭，Codex 这条路径不需要 `Relay/` 以外的任何目录。设了 `PULSE_RELAY_PATHS`，Relay 进入受管模式，接入 `agent-harness/` 的工作项内核，ComputeCost 与 Stats 读取同一份记录。各模块已按原有布局放在本仓，平台在同一个仓库里原地扩展；内核、ComputeCost、Stats 的安装说明将逐个补上。

## 开发

```bash
cd Relay
npm ci
npm run typecheck
npm test          # vitest；部分测试还需要 Python 3.11+
```

Python 模块各有测试目录（`agent-harness/tests_rewrite`、`ComputeCost/tests`、`Stats/exporter/tests`）。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 参与贡献

欢迎提 issue 和 PR。请先读 [CONTRIBUTING.md](CONTRIBUTING.md)，并遵守[行为准则](CODE_OF_CONDUCT.md)。

## 许可

PULSE 以 [MIT 许可证](LICENSE)发布。

第三方许可：依赖从 npm 与 PyPI 安装，不随本仓分发。大多数为宽松许可（`@openai/codex` 为 Apache-2.0；`grammy`、`@larksuiteoapi/node-sdk`、`ws`、`@modelcontextprotocol/sdk` 为 MIT；`node-cron` 为 ISC）。`@anthropic-ai/claude-agent-sdk` 由 Anthropic 按其自有商业条款发布（该包写明“SEE LICENSE IN README.md”）。它是可选依赖，只有 `cc-sdk` 运行时用到。`npm ci` 默认会安装，保留即表示接受其条款；不想要可在安装后删除 `Relay/node_modules/@anthropic-ai/claude-agent-sdk*`。不要为此使用 `npm ci --omit=optional`，那样会连 Codex 二进制一起去掉。
