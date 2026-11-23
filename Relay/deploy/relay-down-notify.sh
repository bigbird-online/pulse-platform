#!/usr/bin/env bash
# relay-down-notify.sh — Relay 反复起不来时，绕开 Relay 直接告知大鸟。
#
# 为什么不走 harness escalation（2026-08-07 Duke b2-relay-selfref-alert-20260807）：
#   harness escalation 的飞书出口是 Relay 的 /internal/push，而本脚本存在的前提
#   就是 Relay 挂了 —— 走它等于让病人自己打 120。故直连 Telegram Bot API。
#
# 为什么不硬编码收件人：
#   probe-heartbeat.py 曾把收件人写死在源码里（B4），结果 policy 白名单对它完全
#   无效，「收敛噪声」在那条路上是空的。这里从 policy 读，改 policy 就能改收件人。
#
# relay.service 以 RestartMode=direct 跳过自动重启中间态的 OnFailure。
# OnFailure 本身只证明 unit 进入 failed，不能被解读为「已失败 3 次」；
# 下方实查 ActiveState/SubState/Result 后才决定是否发送以及发什么文案。
set -euo pipefail

POLICY="/Users/owner/Coevolution/.harness/policies/pulse.yaml"
AUDIT_LOG="/home/pulse/.relay/restart-audit.log"

show_relay_property() {
  systemctl --user show relay.service --property="$1" --value 2>/dev/null | head -n 1 || true
}

ACTIVE_STATE="$(show_relay_property ActiveState)"
SUB_STATE="$(show_relay_property SubState)"
RESULT="$(show_relay_property Result)"

# OnFailure 入队后可能已被人工恢复；当前已不是 failed 就不发陈旧故障。
# 查询失败时 ACTIVE_STATE 为空，不 fail-open 吞掉真告警，继续走下方通用文案。
if [[ -n "$ACTIVE_STATE" && "$ACTIVE_STATE" != "failed" ]]; then
  echo "[relay-down-notify] suppressed stale trigger active=${ACTIVE_STATE} sub=${SUB_STATE:-unknown} result=${RESULT:-unknown}"
  exit 0
fi

if [[ "$RESULT" == "start-limit-hit" ]]; then
  WHAT_HAPPENED="relay.service 的自动恢复链已触发 systemd 启动限额，当前仍为 failed"
  TITLE="🚨 Relay 反复起不来·要你出手"
else
  WHAT_HAPPENED="relay.service 当前仍为 failed（Result=${RESULT:-unknown}）；这不等于已证明耗尽 3 次/10 分钟启动限额"
  TITLE="🚨 Relay 终态失败·要你出手"
fi

# 告警写明出事的节点：本脚本随 relay.service 装在哪台机器，就由哪台机器发出。
NODE="$(hostname 2>/dev/null || echo unknown-host)"
TITLE="${TITLE}（${NODE}）"

TOKEN="${HARNESS_ESCALATION_BOT_TOKEN:-${RELAY_BOT_TOKEN_PULSE:-}}"
if [[ -z "$TOKEN" ]]; then
  echo "[relay-down-notify] no bot token in env; cannot notify" >&2
  exit 1
fi

# 收件人取自 policy 的 escalation.recipient（TG chat_id），不写死在本脚本里。
CHAT_ID="$(python3 -c "
import sys, yaml
try:
    d = yaml.safe_load(open('$POLICY', encoding='utf-8'))
    print((d.get('escalation') or {}).get('recipient', ''))
except Exception:
    print('')
" 2>/dev/null || echo '')"

if [[ -z "$CHAT_ID" ]]; then
  echo "[relay-down-notify] no recipient in $POLICY; cannot notify" >&2
  exit 1
fi

# 带上最近几行重启审计，让大鸟一眼看出是崩了几次还是起不来。
TAIL="$(tail -n 6 "$AUDIT_LOG" 2>/dev/null || echo '(no audit log)')"

TEXT="$(cat <<TXT
$TITLE

发生什么：${WHAT_HAPPENED}。
当前状态：ActiveState=${ACTIVE_STATE:-unknown} SubState=${SUB_STATE:-unknown} Result=${RESULT:-unknown}。

影响：飞书/TG 入口全断，所有 agent 的对话与 cron 都停了。这条消息是绕开 Relay
直连 Telegram 发的——所以你能看到它，但飞书那边多半是死的。

行动：ssh 上 ${NODE}（pulse 账户）后
  systemctl --user status relay.service
  journalctl --user -u relay.service -n 80
  systemctl --user reset-failed relay.service && systemctl --user start relay.service

最近重启审计：
$TAIL
TXT
)"

if ! TELEGRAM_RESPONSE="$(curl -sS --max-time 20 \
  "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${TEXT}")"; then
  echo "[relay-down-notify] Telegram transport failed" >&2
  exit 1
fi

# Telegram 的业务失败也经常返回 HTTP 200（例如 ok=false）。只有 API 明确接受请求
# 并分配 message_id，才允许写 notified；既不打印响应正文，也不泄露 token/chat_id。
if ! MESSAGE_ID="$(printf '%s' "$TELEGRAM_RESPONSE" | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
    message_id = (payload.get("result") or {}).get("message_id")
except (AttributeError, json.JSONDecodeError, TypeError):
    raise SystemExit(1)

if payload.get("ok") is not True or type(message_id) is not int:
    raise SystemExit(1)
print(message_id)
')"; then
  echo "[relay-down-notify] Telegram rejected request or returned an invalid receipt" >&2
  exit 1
fi

echo "[relay-down-notify] notified message_id=${MESSAGE_ID}"
