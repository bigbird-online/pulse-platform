#!/bin/bash
# check-dist-fresh.sh — 断根"提交≠部署"：启动/部署前确认 dist 是从当前 src 编出来的。
# ---------------------------------------------------------------------------
# 背景（2026-07-05 根因）：0047a48c 把 print-mode 泄漏自愈写进 src/pool.ts，但没
# `npm run build`、没 restart。dist/ 是 gitignored 编译产物，relay 跑的是 dist/cli.js，
# 于是拦截代码在 src 里躺了 6 天、从没编进运行时——Duke/Hestia 反复停摆，救命的 A+B
# 从未执行。测试全绿（测的是 src），生产全坏（跑的是 dist）。
#
# 本脚本是硬门：只要有 src/*.ts 比 dist 里最新的 .js 还新（= src 改了没重新 build），
# 就以非零码退出。挂进 relay.service 的 ExecStartPre → systemd 会拒绝用陈旧 dist 启动。
# 也可独立跑：`bash scripts/check-dist-fresh.sh` → exit 0 新鲜 / 3 陈旧 / 1 结构缺失。
#
# 判据：src 树最新 mtime <= dist 树最新 mtime。用 mtime 而非内容哈希，因为要抓的正是
# "编译产物落后于源码"这个时间关系，简单且无误报。
set -euo pipefail

RELAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$RELAY_DIR/src"
DIST_DIR="$RELAY_DIR/dist"

if [[ ! -d "$DIST_DIR" ]] || [[ -z "$(ls -A "$DIST_DIR"/*.js 2>/dev/null)" ]]; then
  echo "check-dist-fresh: FAIL — dist/ 缺失或无 .js 产物，需先 npm run build" >&2
  exit 1
fi
if [[ ! -d "$SRC_DIR" ]]; then
  echo "check-dist-fresh: FAIL — src/ 不存在，仓库结构异常" >&2
  exit 1
fi

# 最新 src .ts 的 mtime（epoch 秒）；最新 dist .js 的 mtime。
newest_src=$(find "$SRC_DIR" -name '*.ts' -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)
newest_dist=$(find "$DIST_DIR" -name '*.js' -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)

if [[ -z "$newest_src" ]]; then
  echo "check-dist-fresh: OK — src 无 .ts（无可编译源，放行）"
  exit 0
fi
if [[ -z "$newest_dist" ]]; then
  echo "check-dist-fresh: FAIL — dist 无 .js（同上，应先 build）" >&2
  exit 1
fi

if [[ "$newest_src" -gt "$newest_dist" ]]; then
  src_file=$(find "$SRC_DIR" -name '*.ts' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
  echo "check-dist-fresh: STALE — src 有改动未 build（最新源比最新产物新）" >&2
  echo "  最新源:  $src_file ($(date -d "@$newest_src" -Iseconds))" >&2
  echo "  最新产物: dist ($(date -d "@$newest_dist" -Iseconds))" >&2
  echo "  修复: cd $RELAY_DIR && npm run build，再重启" >&2
  exit 3
fi

echo "check-dist-fresh: OK — dist 是从当前 src 编出来的（src $(date -d "@$newest_src" +%H:%M:%S) <= dist $(date -d "@$newest_dist" +%H:%M:%S)）"
exit 0
