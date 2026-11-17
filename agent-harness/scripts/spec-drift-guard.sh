#!/bin/bash
# Spec Drift Guard —— agent-harness 主链 spec 的机器闸（Coevolution 主仓第一道 pre-commit）
#
# 为什么存在：spec 的病不是「没文档」，是文档不可信——测试跟得上、spec 跟不上，
# 差别不是自觉，是测试有机器闸、spec 没有。本闸给 specs/*.md（维护期形态显式锚点）
# 补上与测试同级的提交门。样板：PULSE 仓 Scripts/hooks/spec-drift-guard.sh（Stats 版）。
#
# 判据：本次提交动了 agent-harness 的代码 / 测试 / spec 时才跑；broken 数超过基线即拒绝。
# 基线而非硬性 0：已知债显式冻结，新增漂移拦下，存量债不阻塞日常提交。
#
# 失败方向：canonical PULSE scanner 可用时优先使用；独立 CollaborateWithAI checkout 没有
# 嵌套 PULSE 时，使用仓内 explicit-anchor portable fallback。portable fallback 只校验本 feature
# 的三件套，避免用简化扫描器假装替代 canonical scanner 对历史 Harness specs 的全仓语义。
# 只有两者都缺失/故障才 fail-open。契约失效（broken 超基线）始终 fail-closed。
#
# 回滚：rm /Users/owner/Coevolution/.git/hooks/pre-commit（hook 是本地文件，不随 git 传播）。
# 绕过：git commit --no-verify（monorepo 纪律禁止用它绕闸，出现即视为破纪律）。

set -uo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
# PULSE now keeps Harness and its consumer modules as root siblings.  Retain
# the CWA location for historical standalone checkouts, whose exact imports
# still use that repository layout.
if [ -d "$REPO_ROOT/agent-harness" ]; then
    HARNESS_REL="agent-harness"
    CANONICAL_SCAN="$REPO_ROOT/skills/spec-anchor/src/scan.py"
else
    HARNESS_REL="Bigbird/Lab/BigbirdEnvironment/agent-harness"
    CANONICAL_SCAN="$REPO_ROOT/Bigbird/PULSE/skills/spec-anchor/src/scan.py"
fi
PORTABLE_SCAN="$REPO_ROOT/$HARNESS_REL/scripts/portable-spec-anchor-scan.py"
if [ -f "$CANONICAL_SCAN" ]; then
    SCAN="$CANONICAL_SCAN"
    SCANNER_KIND="canonical"
    SPEC_GLOB="$HARNESS_REL/specs/*.md"
elif [ -f "$PORTABLE_SCAN" ]; then
    SCAN="$PORTABLE_SCAN"
    SCANNER_KIND="portable-feature-scoped"
    SPEC_GLOB="$HARNESS_REL/specs/harness-session-continuation-*.md"
else
    SCAN="$CANONICAL_SCAN"
    SCANNER_KIND="missing"
    SPEC_GLOB="$HARNESS_REL/specs/*.md"
fi
BASELINE="$REPO_ROOT/$HARNESS_REL/specs/.spec-drift-baseline.json"
OUT_DIR="${TMPDIR:-/tmp}/harness-spec-drift"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

if [ "$FORCE" -eq 0 ]; then
    CHANGED=$(git -C "$REPO_ROOT" diff --cached --name-only --diff-filter=ACMR || true)
    if ! echo "$CHANGED" | grep -q "^$HARNESS_REL/"; then
        echo "⊘ harness Spec Drift Guard：本次提交未触及 $HARNESS_REL/，跳过（未执行检查）"
        echo "  全量体检跑：$HARNESS_REL/scripts/spec-drift-guard.sh --force"
        exit 0
    fi
fi

if [ ! -f "$SCAN" ]; then
    echo "⚠ harness Spec Drift Guard：canonical/portable scan.py 均不存在，放行未检查" >&2
    exit 0
fi
if [ ! -f "$BASELINE" ]; then
    echo "⚠ harness Spec Drift Guard：找不到基线（${BASELINE}），放行未检查" >&2
    exit 0
fi

mkdir -p "$OUT_DIR"
JSON="$OUT_DIR/drift.json"
MD="$OUT_DIR/drift.md"

echo "harness Spec Drift Guard：scanner=$SCANNER_KIND spec_glob=$SPEC_GLOB"
if ! python3 "$SCAN" --repo "$REPO_ROOT" \
    --spec-glob "$SPEC_GLOB" \
    --src-glob "$HARNESS_REL/harness/**/*.py" \
    --src-glob "$HARNESS_REL/scripts/*.py" \
    --test-glob "$HARNESS_REL/tests/**/*.py" \
    --history-path "$HARNESS_REL/" \
    --json "$JSON" --md "$MD" >/dev/null 2>"$OUT_DIR/scan-err.log"; then
    echo "⚠ harness Spec Drift Guard：scanner 自身出错（见 $OUT_DIR/scan-err.log），放行未检查" >&2
    exit 0
fi

SUMMARY=$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1]))["summary"]; b=json.load(open(sys.argv[2])); print(s["spec_files"], s["drift_count"], s["verdicts"].get("error",0), b["drift_count"])' "$JSON" "$BASELINE" 2>"$OUT_DIR/parse-err.log") || {
    echo "⚠ harness Spec Drift Guard：读数解析失败（见 $OUT_DIR/parse-err.log），放行未检查" >&2
    exit 0
}
set -- $SUMMARY
SPECS=$1; DRIFT=$2; ERRORS=$3; BASE=$4

if [ "$SPECS" -eq 0 ]; then
    echo "⚠ harness Spec Drift Guard：扫到 0 份 spec（glob 或文件被动过），放行但这不是绿灯" >&2
    exit 0
fi

if [ "$ERRORS" -gt 0 ]; then
    echo "⚠ harness Spec Drift Guard：$ERRORS 条锚点判 error（校验器问题非契约失效），放行；明细 $MD" >&2
fi

if [ "$DRIFT" -gt "$BASE" ]; then
    echo "✗ harness Spec Drift Guard：spec 漂移 $DRIFT 条 > 基线 ${BASE}，拒绝提交" >&2
    echo "  明细：$MD" >&2
    echo "  处置：改 spec 或改代码使其一致；确属已知债需冻结时更新基线并在 commit 里说明" >&2
    exit 1
fi

echo "✓ harness Spec Drift Guard：drift=$DRIFT ≤ 基线 ${BASE}，通过"
exit 0
