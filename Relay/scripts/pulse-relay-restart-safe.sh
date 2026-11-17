#!/bin/bash
# pulse-relay-restart-safe.sh — safe wrapper around the resident Relay service manager
#
# Refuses to restart if any CC session is currently `busy` or any scheduler job
# is running. Restarting in either state kills in-flight work with SIGTERM.
#
# Usage:
#   pulse-relay-restart-safe.sh           # check + restart (refuse if busy)
#   pulse-relay-restart-safe.sh --force   # restart anyway, with audit log
#   pulse-relay-restart-safe.sh --check   # check only, exit 0 if safe / 2 if busy
#
# Exit codes:
#   0  — restart succeeded (or --check found no in-flight work)
#   2  — busy sessions or scheduler jobs present, refused (no --force)
#   3  — unified maintenance status unavailable/invalid (refuse unless --force)
#   4  — autonomous 分身（HARNESS_TRIGGER_MODE=cron）越权重启被拒（--check 除外）
#   1  — other error（含：端口真源缺失或不支持的 service manager）
#
# 注：4 曾长期未列入本表（spec pulse-global-topology.md §4 登记的「已知两义」），
# 2026-08-10 债批次 1 补齐。
set -euo pipefail

# Direct invocations on an adopted host must resolve the same resident instance
# as pulse-relay. Delegate once, before opening any log or retired env file.
# The marker only prevents recursion; the original busy/authority guards remain.
if [[ "${PULSE_RELAY_MAINTENANCE_BOUND:-}" != 1 ]] && \
   [[ -n "${PULSE_RELAY_PATHS:-}" || -e "${HOME}/.local/share/pulse/config/relay/paths.json" ]]; then
  manager="${HOME}/.local/bin/pulse-relay"
  if [[ ! -x "$manager" ]]; then
    echo "ERROR: managed Relay requires its installed pulse-relay entrypoint" >&2
    exit 1
  fi
  if [[ "$#" -eq 0 ]]; then
    exec "$manager" restart
  elif [[ "$#" -eq 1 && "$1" == --check ]]; then
    exec "$manager" check
  else
    echo "ERROR: managed Relay accepts only a normal restart or --check; no bypass flags" >&2
    exit 1
  fi
fi

AUDIT_LOG="${RELAY_DATA_DIR:-${HOME}/.relay}/restart-audit.log"

# 端口单一真源（2026-08-10 债批次 1 / wi_20260810_949）：原先此处写死 `${RELAY_HTTP_PORT:-8787}`,
# 与 relay.env 的 RELAY_HTTP_PORT 各存一份,改端口要两处齐动、漏一处 busy 探测就打错端口。
# 改后取值序：调用方已导出的 RELAY_HTTP_PORT → relay.env（systemd 给 relay.service 用的
# 同一份 EnvironmentFile,即服务实际监听端口的真源）→ 无值则**报错退出,不猜**。
#
# 为什么不留 8787 兜底：Relay 自身 resolveRelayHttpPort() 把「RELAY_HTTP_PORT 未设」
# 定义为「HTTP 面整个不启动」,没有 8787 这个隐含默认。脚本猜一个端口只会让
# curl 连不上 → 走 exit 3「HTTP 不可达」分支 → 拿 --force 一压就把 busy 会话杀了。
# 宁可在这里显式报「端口未知」,也不要把配置缺失伪装成服务不可达。
RELAY_ENV_FILE="${RELAY_ENV_FILE:-${HOME}/.relay/relay.env}"
if [[ -z "${RELAY_HTTP_PORT:-}" && -r "$RELAY_ENV_FILE" ]]; then
  # 只取这一个键,不 source 整个文件——relay.env 里有 token 等敏感值,
  # 且 set -u 下 source 一份含裸变量引用的文件可能直接把脚本带崩。
  RELAY_HTTP_PORT="$(sed -n 's/^[[:space:]]*RELAY_HTTP_PORT[[:space:]]*=[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$RELAY_ENV_FILE" | tail -n 1)"
fi

# cc-status 与 Relay 进程读同一份 token 真源。只取目标键，不 source 整份
# relay.env；token 只作为请求头传给 curl，绝不写日志或命令回显。开发环境没配置
# token 时保留空头兼容（Relay 的 checkInternalToken 同样是空配置放行）；生产若服务端
# 有 token 而调用方取不到，curl 会收到 401 并沿既有 http_unreachable 路径 fail-closed。
if [[ -z "${RELAY_INTERNAL_TOKEN:-}" && -r "$RELAY_ENV_FILE" ]]; then
  RELAY_INTERNAL_TOKEN="$(sed -n 's/^[[:space:]]*RELAY_INTERNAL_TOKEN[[:space:]]*=[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$RELAY_ENV_FILE" | tail -n 1)"
  RELAY_INTERNAL_TOKEN="${RELAY_INTERNAL_TOKEN#\"}"
  RELAY_INTERNAL_TOKEN="${RELAY_INTERNAL_TOKEN%\"}"
  RELAY_INTERNAL_TOKEN="${RELAY_INTERNAL_TOKEN#\'}"
  RELAY_INTERNAL_TOKEN="${RELAY_INTERNAL_TOKEN%\'}"
fi

PORT="${RELAY_HTTP_PORT:-}"
if [[ -z "$PORT" ]]; then
  echo "$(date -Iseconds) RESTART_WRAPPER port_unknown env_file=$RELAY_ENV_FILE by=$USER" >> "$AUDIT_LOG"
  echo "ERROR: RELAY_HTTP_PORT 未设且 ${RELAY_ENV_FILE} 里读不到——无法确定 busy 探测端口。" >&2
  echo "       不猜端口：猜错会把「配置缺失」伪装成「Relay 不可达」,再 --force 就会杀掉在跑的会话。" >&2
  echo "       处置：export RELAY_HTTP_PORT=<端口> 后重跑,或修好 relay.env。" >&2
  exit 1
fi
# 默认仍走 loopback；只绑定 Tailscale 地址的 macOS resident surface 可显式传入
# RELAY_STATUS_HOST。该值只决定只读 maintenance probe 的目的地址，不改变 Relay 监听面。
STATUS_HOST="${RELAY_STATUS_HOST:-127.0.0.1}"
STATUS_URL="http://${STATUS_HOST}:${PORT}/internal/cc-status"

FORCE=false
CHECK_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
    --check) CHECK_ONLY=true ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

mkdir -p "$(dirname "$AUDIT_LOG")"

# 兜底闸（2026-07-12 大鸟拍方案 A）：autonomous 分身（cron/dispatcher session，
# 由 harness 注入 HARNESS_TRIGGER_MODE=cron 标识）**不得直接重启 Relay**——重启杀全局
# 进程会拦腰打断正在跑的 sub-agent。分身只能 escalation 给主身，由主身挑「无活跃
# sub-agent」的时机重启。--check（只读探忙）无害，放行；restart/--force 一律拒绝。
# 第一道闸在 policy（分身 allowed_tools 已移除 restart，CC tool-dispatch 层硬拦）；
# 本闸防「绕过 policy 直接调脚本」。exit 4 = 分身越权重启被拒。
if [[ "${HARNESS_TRIGGER_MODE:-}" == "cron" && "$CHECK_ONLY" != true ]]; then
  echo "$(date -Iseconds) RESTART_WRAPPER refused_autonomous trigger=cron scan=${HARNESS_SCAN_ID:-?} by=$USER" >> "$AUDIT_LOG"
  echo "ERROR: autonomous 分身 session 不得直接重启 Relay（会打断正在跑的 sub-agent）。" >&2
  echo "       请 escalation 给主身：改动已落盘，附「配置已改，待主身在无活跃 sub-agent 时机重启生效」。" >&2
  exit 4
fi

# Fetch CC status from Relay HTTP loopback.
#
# 2026-08-10 实测捉到的 fail-open（本批次顺手修，与端口真源同属一条失败信号）：
# 原写法是 `curl ... --show-error ... 2>&1 || echo "__HTTP_ERROR__"`，--show-error 把
# curl 的错误文本也灌进 $http_response，于是取值变成 "curl: (28) ...\n__HTTP_ERROR__"，
# **不等于** "__HTTP_ERROR__" → 下面的 exit 3 闸整条跳过 → busy_count 保持 0 →
# --check 对着一个死掉/超时的 Relay 打印「OK: no busy」并退 0。
# 复现：把 STATUS_URL 指向没人监听的端口跑一次即见。
# 影响：Relay 卡死或探测超时时，安全闸判「无 busy」放行重启——正是它该拦的那一刻失灵。
# 改法：curl 的 stderr 单独丢弃，判据只认 exit code 写出的哨兵串，保持 fail-closed。
if [[ -n "${RELAY_INTERNAL_TOKEN:-}" ]]; then
  http_response=$(curl --max-time 3 --silent --fail \
    -H "x-relay-token: ${RELAY_INTERNAL_TOKEN}" "$STATUS_URL" 2>/dev/null \
    || echo "__HTTP_ERROR__")
else
  http_response=$(curl --max-time 3 --silent --fail "$STATUS_URL" 2>/dev/null \
    || echo "__HTTP_ERROR__")
fi

if [[ "$http_response" == "__HTTP_ERROR__" ]] || [[ -z "$http_response" ]]; then
  echo "$(date -Iseconds) RESTART_WRAPPER http_unreachable url=$STATUS_URL by=$USER" >> "$AUDIT_LOG"
  if [[ "$FORCE" != true ]]; then
    echo "ERROR: cannot reach Relay HTTP ($STATUS_URL) — refusing to restart without --force" >&2
    echo "       (use --force if you intend to restart a dead/hung Relay)" >&2
    exit 3
  fi
  echo "WARN: Relay HTTP unreachable, --force was given — proceeding with restart"
fi

busy_count=0
busy_list=""
scheduler_available=false
scheduler_busy=0
scheduler_list=""
scl_intents_available=true
scl_intents_busy=0
scl_intents_restart_blocking=0
scl_intents_retry_wait=0
safe_to_restart=false
status_fields=""
if [[ "$http_response" != "__HTTP_ERROR__" ]] && [[ -n "$http_response" ]]; then
  status_fields=$(printf '%s' "$http_response" | python3 -c '
import json, sys
d = json.load(sys.stdin)
if not isinstance(d, dict) or d.get("ok") is not True:
    raise ValueError("status_not_ok")
counts = d.get("counts")
busy = d.get("busy")
scheduler = d.get("scheduler")
scl_intents = d.get("sclIntents")
if not isinstance(counts, dict) or not isinstance(counts.get("busy"), int):
    raise ValueError("invalid_cc_count")
if not isinstance(busy, list) or not all(isinstance(x, str) for x in busy):
    raise ValueError("invalid_cc_list")
if not isinstance(scheduler, dict):
    scheduler = {}
running = scheduler.get("running", [])
if not isinstance(running, list) or not all(isinstance(x, str) for x in running):
    raise ValueError("invalid_scheduler_list")
scheduler_count = scheduler.get("busy", 0)
if not isinstance(scheduler_count, int) or scheduler_count != len(running):
    raise ValueError("invalid_scheduler_count")
# A target release probes the previously active Relay before cutover. Accept a
# missing field only for that one legacy adoption boundary; once present it is
# mandatory and fail-closed.
if scl_intents is None:
    scl_available = True
    scl_count = 0
    scl_blocking = 0
    scl_retry_wait = 0
elif isinstance(scl_intents, dict):
    scl_available = scl_intents.get("available") is True
    scl_count = scl_intents.get("busy")
    if not isinstance(scl_count, int) or scl_count < 0:
        raise ValueError("invalid_scl_intent_count")
    if not scl_available:
        scl_blocking = 0
        scl_retry_wait = 0
    else:
        states = scl_intents.get("states")
        if not isinstance(states, dict):
            raise ValueError("invalid_scl_intent_states")
        expected = ("recognized", "attempting", "retry_wait", "result_ready")
        if any(not isinstance(states.get(name), int) or states[name] < 0 for name in expected):
            raise ValueError("invalid_scl_intent_states")
        if sum(states[name] for name in expected) != scl_count:
            raise ValueError("invalid_scl_intent_state_count")
        scl_retry_wait = states["retry_wait"]
        scl_blocking = scl_intents.get("restartBlocking")
        if scl_blocking is None:
            # Compatibility with the previous endpoint: retry_wait is already
            # durable and recoverable after process restart; the other states may
            # still be executing or delivering a result.
            scl_blocking = scl_count - scl_retry_wait
        if not isinstance(scl_blocking, int) or scl_blocking < 0 or scl_blocking > scl_count:
            raise ValueError("invalid_scl_intent_restart_blocking")
else:
    raise ValueError("invalid_scl_intent_status")
print(counts["busy"])
print(",".join(busy))
print("true" if scheduler.get("available") is True else "false")
print(scheduler_count)
print(",".join(running))
print("true" if scl_available else "false")
print(scl_count)
print(scl_blocking)
print(scl_retry_wait)
print("true" if d.get("safeToRestart") is True else "false")
' 2>/dev/null) || status_fields="__PARSE_ERROR__"
fi

if [[ "$status_fields" == "__PARSE_ERROR__" ]]; then
  echo "$(date -Iseconds) RESTART_WRAPPER status_invalid url=$STATUS_URL by=$USER" >> "$AUDIT_LOG"
  if [[ "$FORCE" != true ]]; then
    echo "ERROR: Relay maintenance status is invalid — refusing to restart without --force" >&2
    exit 3
  fi
  echo "WARN: Relay maintenance status invalid, --force was given — proceeding with restart"
elif [[ -n "$status_fields" ]]; then
  # macOS still ships Bash 3, which has arrays but no mapfile. Keep the safety
  # wrapper runnable and testable on both launch surfaces.
  parsed_fields=()
  while IFS= read -r parsed_field || [[ -n "$parsed_field" ]]; do
    parsed_fields+=("$parsed_field")
  done <<< "$status_fields"
  busy_count="${parsed_fields[0]:-0}"
  busy_list="${parsed_fields[1]:-}"
  scheduler_available="${parsed_fields[2]:-false}"
  scheduler_busy="${parsed_fields[3]:-0}"
  scheduler_list="${parsed_fields[4]:-}"
  scl_intents_available="${parsed_fields[5]:-false}"
  scl_intents_busy="${parsed_fields[6]:-0}"
  scl_intents_restart_blocking="${parsed_fields[7]:-0}"
  scl_intents_retry_wait="${parsed_fields[8]:-0}"
  safe_to_restart="${parsed_fields[9]:-false}"
fi

if [[ "$scheduler_available" != true ]]; then
  echo "$(date -Iseconds) RESTART_WRAPPER scheduler_status_unavailable by=$USER" >> "$AUDIT_LOG"
  if [[ "$FORCE" != true ]]; then
    echo "ERROR: scheduler maintenance status unavailable — refusing to restart without --force" >&2
    exit 3
  fi
  echo "WARN: scheduler maintenance status unavailable, --force was given — proceeding with restart"
fi

if [[ "$scl_intents_available" != true ]]; then
  echo "$(date -Iseconds) RESTART_WRAPPER scl_intent_status_unavailable by=$USER" >> "$AUDIT_LOG"
  if [[ "$FORCE" != true ]]; then
    echo "ERROR: SCL intent maintenance status unavailable — refusing to restart without --force" >&2
    exit 3
  fi
  echo "WARN: SCL intent maintenance status unavailable, --force was given — proceeding with restart"
fi

if [[ "$busy_count" -gt 0 || "$scheduler_busy" -gt 0 || "$scl_intents_restart_blocking" -gt 0 ]]; then
  if [[ "$FORCE" != true ]]; then
    echo "$(date -Iseconds) RESTART_WRAPPER refused cc_busy=$busy_count tenants=$busy_list scheduler_busy=$scheduler_busy jobs=$scheduler_list scl_intents_busy=$scl_intents_busy by=$USER" >> "$AUDIT_LOG"
    echo "ERROR: in-flight Relay work detected — refusing to restart" >&2
    echo "       Busy CC sessions: $busy_count${busy_list:+ ($busy_list)}" >&2
    echo "       Running scheduler jobs: $scheduler_busy${scheduler_list:+ ($scheduler_list)}" >&2
    echo "       Pending SCL intents: $scl_intents_busy (restart-blocking: $scl_intents_restart_blocking)" >&2
    echo "       Use --force to override (will interrupt in-flight work)" >&2
    exit 2
  fi
  echo "WARN: cc_busy=$busy_count scheduler_busy=$scheduler_busy scl_intents_busy=$scl_intents_busy — restart will interrupt in-flight work"
  echo "$(date -Iseconds) RESTART_WRAPPER FORCED cc_busy=$busy_count tenants=$busy_list scheduler_busy=$scheduler_busy jobs=$scheduler_list scl_intents_busy=$scl_intents_busy by=$USER" >> "$AUDIT_LOG"
fi

derived_safe_to_restart=false
if [[ "$busy_count" -eq 0 && "$scheduler_busy" -eq 0 && "$scl_intents_restart_blocking" -eq 0 ]]; then
  derived_safe_to_restart=true
fi
if [[ "$safe_to_restart" != true && "$derived_safe_to_restart" != true && "$FORCE" != true ]]; then
  echo "$(date -Iseconds) RESTART_WRAPPER status_not_safe by=$USER" >> "$AUDIT_LOG"
  echo "ERROR: Relay did not affirm safeToRestart=true — refusing to restart" >&2
  exit 3
fi

if [[ "$scl_intents_retry_wait" -gt 0 ]]; then
  echo "Durable retry-wait SCL intents preserved: $scl_intents_retry_wait"
fi

if [[ "$CHECK_ONLY" == true ]]; then
  echo "OK: no busy CC sessions, scheduler jobs, or SCL intents (status=$http_response)"
  exit 0
fi

restart_platform="${RELAY_RESTART_PLATFORM:-$(uname -s)}"
case "$restart_platform" in
  Linux)
    echo "$(date -Iseconds) RESTART_WRAPPER reset_failed backend=systemd by=$USER cc_busy_at_check=$busy_count scheduler_busy_at_check=$scheduler_busy" >> "$AUDIT_LOG"
    echo "Resetting relay.service start-rate counter before restart ..."
    systemctl --user reset-failed relay.service

    echo "$(date -Iseconds) RESTART_WRAPPER restarting backend=systemd by=$USER cc_busy_at_check=$busy_count scheduler_busy_at_check=$scheduler_busy" >> "$AUDIT_LOG"
    echo "Restarting relay.service (cc_busy=$busy_count, scheduler_busy=$scheduler_busy, force=$FORCE) ..."
    exec systemctl --user restart relay.service
    ;;
  Darwin)
    launchd_target="${RELAY_LAUNCHD_TARGET:-gui/${UID}/com.pulse.relay}"
    if [[ -n "${RELAY_LAUNCHD_PLIST:-}" ]]; then
      if [[ ! -r "$RELAY_LAUNCHD_PLIST" ]]; then
        echo "ERROR: launchd plist is not readable: $RELAY_LAUNCHD_PLIST" >&2
        exit 1
      fi
      launchd_domain="${launchd_target%/*}"
      launchd_bootstrap_attempts="${RELAY_LAUNCHD_BOOTSTRAP_ATTEMPTS:-3}"
      launchd_bootstrap_delay_sec="${RELAY_LAUNCHD_BOOTSTRAP_RETRY_DELAY_SEC:-1}"
      launchd_unload_attempts="${RELAY_LAUNCHD_UNLOAD_ATTEMPTS:-30}"
      launchd_unload_delay_sec="${RELAY_LAUNCHD_UNLOAD_RETRY_DELAY_SEC:-1}"
      wait_for_launchd_unloaded() {
        local attempt=1
        while launchctl print "$launchd_target" >/dev/null 2>&1; do
          if [[ "$attempt" -ge "$launchd_unload_attempts" ]]; then
            return 1
          fi
          sleep "$launchd_unload_delay_sec"
          attempt=$((attempt + 1))
        done
        return 0
      }
      restore_launchd_plist_atomically() {
        local backup="$1"
        python3 - "$backup" "$RELAY_LAUNCHD_PLIST" <<'PY'
import os
import stat
import sys
import tempfile
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
mode = stat.S_IMODE(target.stat().st_mode)
fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.rollback.", dir=target.parent)
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(source.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, target)
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except Exception:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
      }
      bootstrap_launchd() {
        local plist="$1"
        local attempt=1
        local status=1
        while [[ "$attempt" -le "$launchd_bootstrap_attempts" ]]; do
          if launchctl bootstrap "$launchd_domain" "$plist"; then
            return 0
          else
            status=$?
          fi
          if [[ "$attempt" -lt "$launchd_bootstrap_attempts" ]]; then
            echo "launchd bootstrap attempt $attempt failed (status=$status); retrying ..." >&2
            sleep "$launchd_bootstrap_delay_sec"
          fi
          attempt=$((attempt + 1))
        done
        return "$status"
      }
      echo "$(date -Iseconds) RESTART_WRAPPER reloading backend=launchd target=$launchd_target plist=$RELAY_LAUNCHD_PLIST by=$USER cc_busy_at_check=$busy_count scheduler_busy_at_check=$scheduler_busy" >> "$AUDIT_LOG"
      echo "Reloading $launchd_target from $RELAY_LAUNCHD_PLIST (cc_busy=$busy_count, scheduler_busy=$scheduler_busy, force=$FORCE) ..."
      launchctl bootout "$launchd_target"
      if ! wait_for_launchd_unloaded; then
        echo "launchd service remained registered after bounded bootout wait: $launchd_target" >&2
        bootstrap_status=75
      elif bootstrap_launchd "$RELAY_LAUNCHD_PLIST"; then
        exit 0
      else
        bootstrap_status=$?
      fi
      echo "$(date -Iseconds) RESTART_WRAPPER bootstrap_failed backend=launchd target=$launchd_target status=$bootstrap_status by=$USER" >> "$AUDIT_LOG"
      if [[ -n "${RELAY_LAUNCHD_ROLLBACK_PLIST:-}" && -r "$RELAY_LAUNCHD_ROLLBACK_PLIST" ]]; then
        echo "Bootstrap failed; restoring $launchd_target from $RELAY_LAUNCHD_ROLLBACK_PLIST ..." >&2
        launchctl bootout "$launchd_target" >/dev/null 2>&1 || true
        if wait_for_launchd_unloaded \
            && restore_launchd_plist_atomically "$RELAY_LAUNCHD_ROLLBACK_PLIST" \
            && bootstrap_launchd "$RELAY_LAUNCHD_PLIST"; then
          echo "$(date -Iseconds) RESTART_WRAPPER rollback_restored backend=launchd target=$launchd_target by=$USER" >> "$AUDIT_LOG"
        else
          echo "$(date -Iseconds) RESTART_WRAPPER rollback_failed backend=launchd target=$launchd_target by=$USER" >> "$AUDIT_LOG"
          echo "ERROR: launchd bootstrap and rollback both failed for $launchd_target" >&2
        fi
      fi
      exit "$bootstrap_status"
    fi
    echo "$(date -Iseconds) RESTART_WRAPPER restarting backend=launchd target=$launchd_target by=$USER cc_busy_at_check=$busy_count scheduler_busy_at_check=$scheduler_busy" >> "$AUDIT_LOG"
    echo "Restarting $launchd_target (cc_busy=$busy_count, scheduler_busy=$scheduler_busy, force=$FORCE) ..."
    exec launchctl kickstart -k "$launchd_target"
    ;;
  *)
    echo "$(date -Iseconds) RESTART_WRAPPER unsupported_platform platform=$restart_platform by=$USER" >> "$AUDIT_LOG"
    echo "ERROR: unsupported Relay restart platform: $restart_platform" >&2
    exit 1
    ;;
esac
