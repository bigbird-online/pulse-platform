---
name: relay-push
description: "Relay 即时推送接口（POST /internal/push）的消费方接入指引：事件驱动地把一条文本推到 owner 的 TG / 飞书，复用 Pulse Relay 现有 bot/凭证，不绑定 cron。适用场景：NewHome 151 异常告警、需要把外部事件当场送到 owner 眼前的 Agent 巡检/检测器。"
---

# relay-push — 即时推送接入

## 定位

Relay 提供一个 loopback HTTP 端点 `POST /internal/push`，**外部调用一次就立刻推一条消息**。与 cron `announce` 推送并存，不替代——cron 适合"定时任务跑完把结果推一下"，本端点适合"事件发生时当场推一条"。

## 何时用

- NewHome 纳管 Agent 检测到设备离线 / 控制失败 / 权限越界 → 当场推 owner（151 §0.4 通知红线）。
- 任何需要事件驱动、不能等定时窗口的告警 / 通知。

**不适用**：定期摘要、批量交付（用 cron `announce` 或 `feishu_document`）。

## 端点

```
POST http://127.0.0.1:8787/internal/push
```

**前提**：调用方必须与 Relay 同机。HTTP server 绑定 127.0.0.1，外网不可达。

### 鉴权

```
X-Relay-Token: <RELAY_INTERNAL_TOKEN>
```

token 在 `/home/pulse/.relay/relay.env`（仅 pulse 用户可读）。调用方需 owner 注入或 root/同用户读取。

> Token 未设置时端点不校验（dev 模式）。生产 Vultr 已注入。

### 请求体

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `channel` | `"telegram" \| "feishu" \| "both"` | ✓ | 推送通道 |
| `agentId` | string | ✓ | 决定用哪个 bot/feishu app（如 `pulse` / `aki` / `amanda`） |
| `to` | string |  | 单字段收件人（单通道场景或两通道复用）。Telegram 用 chat_id；飞书用 open_id |
| `toTelegram` | string |  | 指定 TG 收件人；优先于 `to` |
| `toFeishu` | string |  | 指定飞书收件人 open_id；优先于 `to` |
| `text` | string | ✓ | 告警正文，不会被改写；emoji/前缀由调用方决定（severity 仅入日志） |
| `severity` | `"high" \| "mid" \| "low"` |  | 仅写入日志；不影响发送 |
| `source` | string |  | 来源标识（如 `newhome-151`），仅入日志便于审计 |

**收件人解析规则**：`channel=both` 时，TG 取 `toTelegram || to`，飞书取 `toFeishu || to`。`to` 不传时两个 `toXxx` 都必须显式给。注意 TG chat_id 是数字字符串、飞书 open_id 是 `ou_xxx`——共用一个 `to` 不会同时通。

### 响应

```json
{
  "ok": true,
  "receipts": [
    { "channel": "telegram", "to": "...", "status": "sent", "attempts": 1, "externalMessageId": "..." }
  ]
}
```

- HTTP 200 + `ok: true`：全部通道成功。
- HTTP 200 + `ok: false`：`both` 模式部分失败；逐条看 `receipts[].status`、`error`。
- HTTP 400：`missing_fields` / `missing_to` / `empty_text` / `unknown_channel` / `invalid_json`
- HTTP 401：`unauthorized`（token 不匹配）
- HTTP 404：`agent_not_found`（agentId 在对应 channel 池里找不到，含 `missing: "telegram_bot" | "feishu_client"`）
- HTTP 413：`payload_too_large`（>16 KB）
- HTTP 500：`internal_error`

## 调用样例

### curl

```bash
TOKEN=$(sudo -u pulse cat /home/pulse/.relay/relay.env | grep RELAY_INTERNAL_TOKEN | cut -d= -f2)

# TG-only
curl -X POST http://127.0.0.1:8787/internal/push \
  -H 'Content-Type: application/json' \
  -H "X-Relay-Token: $TOKEN" \
  -d '{
    "channel": "telegram",
    "agentId": "pulse",
    "to": "1000000001",
    "text": "🔴 设备 device-A 离线 > 5min",
    "severity": "high",
    "source": "newhome-151"
  }'

# Both 通道并发，分别指定收件人
curl -X POST http://127.0.0.1:8787/internal/push \
  -H 'Content-Type: application/json' \
  -H "X-Relay-Token: $TOKEN" \
  -d '{
    "channel": "both",
    "agentId": "pulse",
    "toTelegram": "1000000001",
    "toFeishu": "ou_00000000000000000000000000000003",
    "text": "🔴 [HIGH] 设备 device-A 离线 > 5min",
    "severity": "high",
    "source": "newhome-151"
  }'
```

### 其它语言

同构：POST JSON + `X-Relay-Token` header，读 `data.ok`，失败逐条看 `data.receipts[].status/error`。
Python 用 `requests.post(..., timeout=15)`；Node 用 `fetch`。令牌从环境文件显式读取（TCloud 见下表；ownermac 为 `secrets/relay/relay-service.env`）：Relay 不把 `RELAY_INTERNAL_TOKEN` 传给 Agent 会话。

## 失败处理建议

1. **TG 429 / 网络瞬时错误** — `deliverTelegramText` 内部已带指数退避重试（≤2 次）；`attempts > 1` 表示触发过重试。调用方一般不必再重试。
2. **`both` 模式一路失败** — 已成功那路 receipt 含 `externalMessageId` 不会重发；失败那路按 `error` 自决（如 TG 失败 → 仅飞书已送达，可考虑短时单通道重推 TG）。
3. **`agent_not_found`** — 检查 agentId 是否在 `data/relay.json` 已注册、且该 agent 已配 botToken（TG）/ feishu（飞书）。
4. **`unauthorized`** — token 不匹配或未读到。

## 凭证与收件人（怎么取，别抄死值）

| 项 | 怎么取 |
|----|-------|
| 端点 | `http://127.0.0.1:8787/internal/push`（loopback only）；端口以 `relay.env` 的 `RELAY_HTTP_PORT` 为准，`ss -tln \| grep 8787` 核在听 |
| Token | `/home/pulse/.relay/relay.env` 内 `RELAY_INTERNAL_TOKEN=`（仅 pulse 用户可读） |
| 可用 agentId | 读 `data/relay.json` 的 `users[].agents[].id`——**清单会变，别记死**（`rg -o '"id":\s*"[a-z0-9-]+"' data/relay.json \| sort -u`） |
| owner 收件人 | 同样从 `data/relay.json` 取（TG chat_id / 飞书 open_id）；上面样例里的两个值 2026-07-16 实核仍准，但以 relay.json 为真源 |

## 边界与已知限制

- **仅文本**（`text` 字段）；飞书 card / image / file 暂未通过本端点开放——cron 路径里有 `deliverFeishuCard/Image/File`，后续可按需扩展。
- **无幂等**：相同请求重发会重复送达。事件驱动告警适用；exactly-once 语义请调用方自己加去重 key。
- **无限流**：靠 token + 127.0.0.1 + TG/飞书 API 自身配额兜底。生产消费方注意调用频率。
- **不会写 cron-jobs.json**：本端点完全旁路 scheduler，不留 jobs 痕迹；审计依赖结构化日志（`module: 'http-push'`）。
- **chat_id / open_id 错配不会预检**：format 不对会以 TG/飞书 API 错误形式回到 `receipts[].error`，HTTP 仍 200。

## 故障排查

| 现象 | 排查 |
|------|------|
| `connection refused` | `ss -tln \| grep 8787` 没监听 → relay 没装 `RELAY_HTTP_PORT` 或服务挂了 |
| `401 unauthorized` | header `X-Relay-Token` 缺失/错值；对照 `/home/pulse/.relay/relay.env` |
| `404 agent_not_found` | `relay.json` 没有这个 agentId 或对应 channel 未配置；`grep -A2 '"id":' /Users/owner/Coevolution/Bigbird/PULSE/data/relay.json` |
| receipt `failed` 含 `chat not found` | TG chat_id 错；该 bot 没和用户开过对话 |
| receipt `failed` 含 `99991663` 等飞书错码 | open_id 与 agent 的 feishu app 不在一个租户内 |
| 完全没日志 | `journalctl --user -u relay --since "5 min ago" \| grep http-push` |

## 源码与测试

- 实现：`Bigbird/Relay/src/http-server.ts` `handlePush`
- 接线：`Bigbird/Relay/src/index.ts` 把 `botByAgentId` / `feishuClientByAgentId` 注入 `RelayHttpServerDeps`
- 测试：`Bigbird/Relay/tests/relay-http-push.test.ts`（含鉴权、字段校验、单/双通道、部分失败、agent_not_found 等覆盖）
- 在役实证：端点被跨 agent 真消费（2026-07-16 扫全量 session 语料：13 次真 curl 调用，其中 11 次来自 aki / hume / hestia）。首次活体验证记录 `memo/2026-05/wi-2026-05-29-relay-instant-push-for-emi.md`
