#!/bin/bash
# ensure-dist-fresh.sh — relay.service ExecStartPre 硬门（自愈式）。
# ---------------------------------------------------------------------------
# 断根"提交≠部署"（2026-07-05 根因：src 改了没 build，relay 跑陈旧 dist，自愈代码
# 6 天没进运行时，Duke/Hestia 反复停摆）。
#
# 为什么自愈而非直接拒启动：relay.service 是 Restart=always + RestartSec=5。若
# ExecStartPre 直接 exit 非 0，systemd 会判启动失败并每 5s 无限重试 → relay 永远起不来，
# 比陈旧 dist 更糟（生产不可用）。所以这里：发现陈旧 → 自动 npm run build → 用新 dist 起。
# 只有 build 真的失败（编译错误）才 exit 非 0 拒启动——编译不过的代码本就不该上生产。
#
# 幂等、无副作用（dist 新鲜时不 build）；build 输出进 audit log 便于事后追。
set -uo pipefail

RELAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUDIT_LOG="${RELAY_DATA_DIR:-/home/pulse/.relay}/restart-audit.log"
CHECK="$RELAY_DIR/scripts/check-dist-fresh.sh"

log() { echo "$(date -Iseconds) ensure-dist-fresh: $*" >> "$AUDIT_LOG" 2>/dev/null || true; }

# 新鲜 → 直接放行（最常见路径，零开销）。
if bash "$CHECK" >/dev/null 2>&1; then
  log "OK dist fresh, start allowed"
  exit 0
fi

# 陈旧或缺失 → 自动 build。
log "STALE/missing dist detected — auto-building before start"
if ( cd "$RELAY_DIR" && npm run build >>"$AUDIT_LOG" 2>&1 ); then
  # build 后复检；正常必过，异常（时钟等）也放行避免锁死可用性，但记 WARN。
  if bash "$CHECK" >/dev/null 2>&1; then
    log "auto-build OK, dist now fresh, start allowed"
  else
    log "WARN auto-build ran but freshness check still failing — allowing start anyway (availability > strictness)"
  fi
  exit 0
fi

# build 真失败（编译错误）→ 拒启动。这种情况本就该拒：陈旧 dist 里可能是能跑的旧代码，
# 但"src 编译不过"说明有人推了坏代码，起来也是错的。让 systemd 重试期间人来修。
log "ERROR auto-build FAILED (compile error?) — refusing start until src compiles"
exit 1
