"""Fixed production source contract shared by the live collector and service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Dict, Iterable, List, Mapping, Optional, Protocol, Sequence

from .reducer import IncidentEvent

PULSE_REPO = Path("/Users/owner/Coevolution/Bigbird/PULSE")
# The reader runs in the account that owns the Relay, publisher state and App.
ACCOUNT_HOME = Path.home()
HARNESS_AUTHORITY_REPOSITORY = "bigbird-online/PULSE"
# Stats consumes the inventory-managed production Harness authority repository
# registered for its own site in config/scl-surfaces.yaml. Production resolves
# it from that registry (production_paths.resolve); this default is the same
# registered ownermac repository. A human work root is not an authority source.
HARNESS_SERVICE_REPO = (
    PULSE_REPO / "data/agent-harness/service-repositories/bigbird-online/PULSE/ownermac"
)
HARNESS_ROOT = HARNESS_SERVICE_REPO / ".harness"
SURFACE_REGISTRY_PATH = PULSE_REPO / "config/scl-surfaces.yaml"
STATS_STATE_DIR = ACCOUNT_HOME / ".local/state/pulse/stats"
# The producer is SCL/agent-harness, not Stats. Stats only consumes this
# privacy-safe aggregate from a fixed service-endpoint path; absence is an
# authority gap and must never fall back to scanning one local Harness root.
GLOBAL_AUTHORITY_PATH = STATS_STATE_DIR / "global-authority-evidence-v1.json"
CANONICAL_READ_MODEL_PATH = STATS_STATE_DIR / "canonical-read-model-v2.json"
# No heartbeat registry is registered for the local reader: the remaining
# enabled sender (Hestia) reports to the TCloud Relay, which Stats does not
# read. Replays pass their own registry explicitly.
REGISTRY_PATH: Optional[Path] = None
HEARTBEAT_PATH = ACCOUNT_HOME / ".local/share/pulse/data/relay/heartbeats.json"
LEDGER_PATH = ACCOUNT_HOME / ".cache/pulse-ledger-lint-probe/incident-state.json"
ALERTS_DIR = HARNESS_ROOT / "events"
STATE_PATH = STATS_STATE_DIR / "incident-state-v1.json"

# --- v2-A: usage / quota / harness metric sources (spec pulse-stats-menubar-v2 §3) ---
WORK_ITEMS_DIR = HARNESS_ROOT / "work-items"
HANDOFFS_DIR = HARNESS_ROOT / "handoffs"
# 只为读各 agent 有无 stale_work_items 兜底扫描（spec §6.7.1 判 unowned）。
# 不读授权白名单等其余 policy 内容。
POLICIES_DIR = HARNESS_ROOT / "policies"
# Legacy defaults remain for explicit replay/test construction only. Production
# LiveSnapshotService.default resolves heartbeats and observabilityDir together
# from ~/.local/share/pulse/config/relay/paths.json. It never reads another
# process's RELAY_OBS_DIR environment, nor falls back to these retired files.
OBS_DIR = ACCOUNT_HOME / ".local/share/pulse/data/relay/obs"
SUBSCRIPTIONS_CONFIG_PATH = (
    PULSE_REPO / "Stats/exporter/config/subscriptions.json"
)
CC_PROJECTS_DIR = ACCOUNT_HOME / ".claude/projects"
CODEX_LOCAL_SESSIONS = ACCOUNT_HOME / ".codex/sessions"

RELAY_CC_STATUS_URL = "http://127.0.0.1:8787/internal/cc-status"
RELAY_HEALTH_URL = "http://127.0.0.1:8787/internal/health"
RELAY_HTTP_TIMEOUT_SECONDS = 2

# The remote Codex probe is the slowest source (measured 3.4-5.2s on 2026-07-25)
# and is intentionally given its own budget instead of the shared 4s subprocess
# timeout. It runs concurrently with local collection, so its latency overlaps
# rather than adds. Failure yields an unknown metric, never a fabricated 0.
#
# 2026-07-28: this remote route is dead and was silently serving stale numbers.
# ownermac has no `codex` binary at all and its newest session file is dated
# 2026-03-05, so the `-newermt "-3 days"` find matches nothing and the probe
# returns an empty string. Collection then fell through to the local sessions
# directory, whose newest record was 2026-07-20 -- and that 8-day-old reading
# was published as `available`/`validated`. It is kept (cheap, concurrent, and
# harmless once staleness is enforced) but is no longer the only source.
CODEX_REMOTE_HOST = "ownermac"
CODEX_REMOTE_SESSIONS = "/Users/owner/.codex/sessions"
CODEX_REMOTE_TIMEOUT_SECONDS = 7
CODEX_LOOKBACK_DAYS = 3
CODEX_SCAN_FILES = 5

# `codex_error_info` on a refused turn. Once the weekly allowance is spent the
# server declines to run the turn at all, so `token_count` arrives carrying no
# usage (`primary` and `secondary` both null, `limit_id` flipped to the empty
# `premium` bucket) and the reading looks unavailable rather than maxed out.
# The adjacent `task_complete` record is where the truth is; keying on this
# enum rather than on the human-readable message keeps the check stable across
# copy changes and locales.
CODEX_USAGE_LIMIT_ERROR = "usage_limit_exceeded"

# Codex publishes quota only as a side effect of doing work: every turn writes a
# `token_count` payload carrying `rate_limits.primary` into the session log.
# Nothing else on the box refreshes it, so when Codex goes unused the newest
# record just ages -- which is exactly how a 07-20 reading survived to 07-28.
#
# So the probe makes its own weather: a minimal headless turn purely to mint a
# fresh record. That costs real subscription quota, which is why it is rationed
# rather than run every collection. `CODEX_PROBE_MIN_INTERVAL_SECONDS` is the
# floor between two self-issued turns; below it the existing on-disk record is
# reused even if slightly old. At 6h the probe's own consumption is negligible
# against a weekly window while still guaranteeing a same-day reading.
CODEX_PROBE_BIN = "/usr/bin/codex"
CODEX_PROBE_MIN_INTERVAL_SECONDS = 6 * 60 * 60
# Measured 7.7s end to end on 2026-07-28; the ceiling is generous because the
# probe is rate-limited and a timeout only means "keep the older reading".
CODEX_PROBE_TIMEOUT_SECONDS = 45
COST_ROUTE_PATH = Path(
    os.environ.get(
        "PULSE_RUNTIME_COST_ROUTE_PATH",
        str(PULSE_REPO / "data/runtime-cost-route.json"),
    )
)
COMPUTE_PROFILES_PATH = Path(
    os.environ.get(
        "PULSE_COMPUTE_PROFILES_PATH",
        str(PULSE_REPO / "Relay/config/compute-profiles.json"),
    )
)
CODEX_MODELS_CACHE_PATH = Path(
    os.environ.get(
        "PULSE_CODEX_MODELS_CACHE_PATH",
        str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "models_cache.json"),
    )
)
MAX_CODEX_MODEL_CATALOG_AGE = timedelta(days=7)
MAX_CODEX_MODEL_CATALOG_CLOCK_SKEW = timedelta(minutes=5)
CODEX_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
ACTIVE_COST_ROUTE_NAMES = frozenset({
    "stats_quota_probe",
    "hestia_30min",
    "hestia_weekly_review",
    "harness_pending_handoffs",
    "harness_stale_work_items",
})
ACTIVE_ROUTE_WORKLOAD_SHAPES = frozenset({
    "deterministic",
    "repeatable",
    "routine_judgment",
})
ACTIVE_ROUTE_RISK_CLASSES = frozenset({"standard", "critical_ambiguous"})
ACTIVE_COST_PRESET_STATUS = {
    "current_safe": "baseline",
    "phase2_stats_canary": "canary",
    "phase23_canary": "canary",
}


class CostRouteError(ValueError):
    """The active Stats route cannot be resolved without guessing."""


def _load_route_json(path: Path, label: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CostRouteError(f"{label} unreadable at {path}: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise CostRouteError(f"{label} must be a JSON object at {path}")
    return parsed


def _validate_active_cost_route_manifest(
    active: Mapping[str, object],
    path: Path,
) -> Mapping[str, object]:
    if set(active) != {"schema_version", "preset", "status", "routes"}:
        raise CostRouteError(f"active cost route invalid root fields at {path}")
    if type(active.get("schema_version")) is not int or active.get("schema_version") != 1:
        raise CostRouteError(f"active cost route invalid schema at {path}")
    preset = active.get("preset")
    if not isinstance(preset, str) or preset not in ACTIVE_COST_PRESET_STATUS:
        raise CostRouteError(f"active cost route invalid preset at {path}")
    if active.get("status") != ACTIVE_COST_PRESET_STATUS[preset]:
        raise CostRouteError(f"active cost route preset/status mismatch at {path}")
    routes = active.get("routes")
    if not isinstance(routes, Mapping) or set(routes) != ACTIVE_COST_ROUTE_NAMES:
        raise CostRouteError(f"active cost route must contain exactly five routes at {path}")

    required = {
        "consumer_path",
        "workload_shape",
        "risk_class",
        "verifier_id",
        "base_route",
        "risk_floor",
        "effective_route",
        "rollback_profile",
    }
    for route_name, route in routes.items():
        if not isinstance(route, Mapping) or set(route) not in (
            required,
            required | {"escalation_profile"},
        ):
            raise CostRouteError(f"active cost route {route_name} has invalid fields")
        for field in ("consumer_path", "verifier_id", "rollback_profile"):
            value = route.get(field)
            if not isinstance(value, str) or not value.strip():
                raise CostRouteError(f"active cost route {route_name}.{field} is invalid")
        if route.get("workload_shape") not in ACTIVE_ROUTE_WORKLOAD_SHAPES:
            raise CostRouteError(f"active cost route {route_name}.workload_shape is invalid")
        if route.get("risk_class") not in ACTIVE_ROUTE_RISK_CLASSES:
            raise CostRouteError(f"active cost route {route_name}.risk_class is invalid")
        escalation = route.get("escalation_profile")
        if "escalation_profile" in route and (
            not isinstance(escalation, str) or not escalation.strip()
        ):
            raise CostRouteError(
                f"active cost route {route_name}.escalation_profile is invalid"
            )
        for field in ("base_route", "risk_floor", "effective_route"):
            container = route.get(field)
            if not isinstance(container, Mapping) or set(container) != {"profile"}:
                raise CostRouteError(f"active cost route {route_name}.{field} is invalid")
            profile_name = container["profile"]
            if profile_name is not None and (
                not isinstance(profile_name, str) or not profile_name.strip()
            ):
                raise CostRouteError(
                    f"active cost route {route_name}.{field}.profile is invalid"
                )

    stats = routes["stats_quota_probe"]
    assert isinstance(stats, Mapping)
    expected_stats_profile = (
        "platform_default" if preset == "current_safe" else "economy"
    )
    if (
        stats["consumer_path"] != "stats.quota_probe"
        or stats["workload_shape"] != "repeatable"
        or stats["risk_class"] != "standard"
        or stats["verifier_id"] != "stats.token_count.window_reset.v1"
        or stats["base_route"]["profile"] != expected_stats_profile
        or stats["risk_floor"]["profile"] is not None
        or stats["effective_route"]["profile"] != expected_stats_profile
        or stats["rollback_profile"] != "platform_default"
    ):
        raise CostRouteError("stats_quota_probe route semantics are invalid")
    return routes


def _resolve_codex_probe_route(
    route_path: Path = COST_ROUTE_PATH,
    profiles_path: Path = COMPUTE_PROFILES_PATH,
    cache_path: Path = CODEX_MODELS_CACHE_PATH,
    *,
    now: Optional[datetime] = None,
) -> tuple[str, str]:
    """Resolve Stats' active alias with Relay's schema-v2/cache contract."""
    active = _load_route_json(route_path, "active cost route")
    routes = _validate_active_cost_route_manifest(active, route_path)
    route = routes["stats_quota_probe"]
    assert isinstance(route, Mapping)
    effective = route.get("effective_route")
    assert isinstance(effective, Mapping)
    profile_name = effective.get("profile")
    if not isinstance(profile_name, str) or not profile_name.strip():
        raise CostRouteError("stats_quota_probe effective profile is unavailable")

    catalog = _load_route_json(profiles_path, "compute profiles")
    profiles = catalog.get("profiles")
    if catalog.get("schema_version") != 2 or not isinstance(profiles, Mapping):
        raise CostRouteError(f"compute profiles invalid schema at {profiles_path}")
    profile = profiles.get(profile_name)
    if not isinstance(profile, Mapping):
        raise CostRouteError(f"unknown compute profile: {profile_name}")
    codex = profile.get("codex")
    if not isinstance(codex, Mapping):
        raise CostRouteError(f"compute profile {profile_name}.codex is invalid")
    if "model" in codex:
        raise CostRouteError(
            f"compute profile {profile_name}.codex.model literals are forbidden"
        )
    alias = codex.get("model_alias")
    effort = codex.get("reasoning_effort")
    if not isinstance(alias, str) or not alias.strip():
        raise CostRouteError(f"compute profile {profile_name}.codex.model_alias is invalid")
    if not isinstance(effort, str) or effort not in CODEX_REASONING_EFFORTS:
        raise CostRouteError(f"compute profile {profile_name}.codex effort is unsupported")
    alias_match = re.fullmatch(r"codex-([a-z0-9-]+)-latest", alias)
    if alias_match is None:
        raise CostRouteError(f"unsupported Codex alias: {alias}")

    cache = _load_route_json(cache_path, "Codex model catalog")
    models = cache.get("models")
    fetched_raw = cache.get("fetched_at")
    if not isinstance(models, list) or not isinstance(fetched_raw, str):
        raise CostRouteError(f"Codex model catalog invalid schema at {cache_path}")
    try:
        fetched_at = datetime.fromisoformat(fetched_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CostRouteError(f"Codex model catalog stale or missing fetched_at at {cache_path}") from exc
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise CostRouteError(f"Codex model catalog stale or missing fetched_at at {cache_path}")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = current - fetched_at.astimezone(timezone.utc)
    if age > MAX_CODEX_MODEL_CATALOG_AGE or age < -MAX_CODEX_MODEL_CATALOG_CLOCK_SKEW:
        raise CostRouteError(f"Codex model catalog stale or missing fetched_at at {cache_path}")

    family = alias_match.group(1)
    pattern = re.compile(rf"^gpt-(\d+(?:\.\d+)*)-{re.escape(family)}$")
    candidates: list[tuple[tuple[int, ...], str]] = []
    for model in models:
        if not isinstance(model, Mapping) or model.get("visibility") != "list":
            continue
        slug = model.get("slug")
        match = pattern.fullmatch(slug) if isinstance(slug, str) else None
        levels = model.get("supported_reasoning_levels")
        if match is None or not isinstance(levels, list):
            continue
        if not any(isinstance(level, Mapping) and level.get("effort") == effort for level in levels):
            continue
        candidates.append((tuple(int(part) for part in match.group(1).split(".")), slug))
    if not candidates:
        raise CostRouteError(
            f"Codex alias {alias} has no visible model supporting effort {effort} in {cache_path}"
        )
    candidates.sort(reverse=True)
    return candidates[0][1], effort


try:
    CODEX_PROBE_MODEL, CODEX_PROBE_REASONING_EFFORT = _resolve_codex_probe_route()
    CODEX_PROBE_ROUTE_ERROR: Optional[str] = None
except CostRouteError as exc:
    CODEX_PROBE_MODEL = None
    CODEX_PROBE_REASONING_EFFORT = None
    CODEX_PROBE_ROUTE_ERROR = str(exc)
CODEX_PROBE_PROMPT = "reply with only: ok"
# `codex exec` refuses to start outside a trusted git repo unless told not to
# care. The probe needs no workspace writes; Codex still owns its session log.
# Pin read-only explicitly so the user's platform-wide YOLO default cannot leak
# into this fixed Stats collection task.
CODEX_PROBE_ARGV = (
    (
        CODEX_PROBE_BIN,
        "--ask-for-approval",
        "never",
        "exec",
        "--sandbox",
        "read-only",
        "-m",
        CODEX_PROBE_MODEL,
        "-c",
        f'model_reasoning_effort="{CODEX_PROBE_REASONING_EFFORT}"',
        "--skip-git-repo-check",
        CODEX_PROBE_PROMPT,
    )
    if CODEX_PROBE_MODEL is not None and CODEX_PROBE_REASONING_EFFORT is not None
    else None
)

# How old a quota reading may be before the snapshot stops calling it a fact.
#
# Set to twice the probe interval so one failed probe is survivable -- the menu
# bar keeps showing the last good figure -- while two consecutive failures blank
# it. Blanking is the honest outcome: the App renders an unknown quota as "--"
# with a grey lamp, which reads as "I don't know" rather than as a number the
# owner might act on.
CODEX_QUOTA_MAX_AGE_SECONDS = 2 * CODEX_PROBE_MIN_INTERVAL_SECONDS

# --- CC (Claude Code) subscription quota ---
# Unlike Codex, CC publishes no quota file on disk: the only source is the
# `anthropic-ratelimit-unified-*` response headers on a real API call. The
# request is therefore made as small as it can possibly be (cheapest model,
# max_tokens=1) because the body is discarded -- we are paying solely for the
# headers. Exactly one such request is issued per exporter run.
CC_CREDENTIALS_PATH = ACCOUNT_HOME / ".claude/.credentials.json"
# Relay 给每个 CC 进程注入的 OAuth token —— **生产上真正在用的那份**。
#
# 2026-07-30 查明:`.credentials.json` 是 CC TUI 交互登录留下的副产物,vultr 上
# 没人靠它跑。Relay 从这里读 `CLAUDE_CODE_OAUTH_TOKEN` 注进每个 CC 进程的
# 环境,那份长期有效、由大鸟维护。exporter 却只读 credentials.json,于是
# 「CC 未知」发作三次:三次都是 credentials.json 里那个 8 小时期的 access token
# 过期了,而生产明明一直好着。修的是读错源,不是刷新机制。
CC_RELAY_ENV_PATH = ACCOUNT_HOME / ".relay/relay.env"
# 最近一次成功读到的 CC 额度。令牌每 8 小时过期一次，且**过期后要等下一次
# CC 真正发请求才会被刷新**（2026-07-29 实测：05:55 过期，09:37 才被刷回），
# 空窗期不确定多长。此前空窗期内这一格直接变「未知」——把一个 8 分钟前还
# 好好的读数整个丢掉。改为落盘保留，由 CC_QUOTA_MAX_AGE_SECONDS 判老。
CC_QUOTA_CACHE_PATH = STATS_STATE_DIR / "cc-quota-last.json"
# 比令牌有效期（8h）略长，好让空窗期整个被兜住；超过就该说"太老了"而不是
# 继续拿旧数当现值。
CC_QUOTA_MAX_AGE_SECONDS = 9 * 3600
CC_QUOTA_URL = "https://api.anthropic.com/v1/messages"
CC_QUOTA_MODEL = "claude-haiku-4-5-20251001"
CC_QUOTA_MAX_TOKENS = 1
CC_QUOTA_ANTHROPIC_VERSION = "2023-06-01"
CC_QUOTA_OAUTH_BETA = "oauth-2025-04-20"
# Own budget, kept under the remote Codex probe's 7s. It runs concurrently with
# local collection so its latency overlaps rather than adds to the end-to-end
# time. Any failure yields unknown metrics, never a fabricated 0.
CC_QUOTA_TIMEOUT_SECONDS = 5
CC_QUOTA_HEADER_PREFIX = "anthropic-ratelimit-unified"

# Only files touched inside this window are opened when looking for CC 429s.
# The window is then re-applied to each line's own timestamp, because a file
# modified today routinely still contains months-old error records.
CC_RATE_LIMIT_WINDOW_HOURS = 24
CC_RATE_LIMIT_MAX_FILES = 40
WI_STALLED_DAYS = 7
WI_STALLED_MAX_ITEMS = 20
# 「在办」看板：草案超过这么多天没推进算卡住；其它在办 WI 沿用 WI_STALLED_DAYS。
WORK_BOARD_DRAFT_DAYS = 3
# 在飞项分档阈值（spec stats-exporter-collection §6.7.1）。
# **刻意不复用 WI_STALLED_DAYS(7)**：那个是「停滞列表」另一个功能的判据，改它会波及
# collect_harness。这里对齐 harness 侧 STALE_WI_DEFAULT_AGE_DAYS=3 —— 判据必须与
# 兜底扫描真正动手的那个时点一致，用 7 天描述 3 天触发的机制会让「已超期」失真。
IN_FLIGHT_SWEEP_DAYS = 3
# executor 是否「真在场」：昨今有过真开工才算。
IN_FLIGHT_ACTIVE_DAYS = 1
USAGE_TOP_AGENTS = 10
USAGE_TOP_SESSIONS = 20
SESSION_ID_PREFIX_CHARS = 8
# ack-probe 已退役（PULSE#603），仓内不再有可执行的探针；生产以 retire_legacy_probes 跳过 ack 采集。
# 非生产路径（测试、历史回放）要采 ack，须由调用方注入完整命令（ack_argv）；
# 默认为空：不执行任何命令，s2.ack_overdue 判 unknown（source 记 not_configured）。
ACK_ARGV: tuple[str, ...] = ()
# ack-probe 要遍历全部 handoff yaml,实测 7.0s(2026-07-27),远超 runner 默认的 4s。
# 超时的后果不是少一个数,而是 s2.ack_overdue 整条能力降级成 unknown、
# 进而把 overall.runtime_health 也拖成 unknown——覆盖度看着有、实则一直没兑现。
# 给足余量而非贴着实测值,避免账本变大后再次静默降级。
ACK_TIMEOUT_SECONDS = 20
SYSTEMD_UNITS = (
    "relay.service",
    "degen-probe.timer",
    "ack-probe.timer",
    "harness-ledger-lint.timer",
    "executor-reaper.timer",
    "dual-heartbeat-watcher.timer",
    "harness-daily-flush.timer",
)
SYSTEMD_ARGV = (
    "/usr/bin/systemctl",
    "--user",
    "show",
    *SYSTEMD_UNITS,
    "--property=Id,ActiveState,UnitFileState,Result",
    "--no-pager",
)
# On a macOS reader the Relay is the per-user launchd job, not a systemd unit.
RELAY_LAUNCHD_LABEL = "com.pulse.relay"
LAUNCHCTL = "/bin/launchctl"
GIT_ARGV = ("/usr/bin/git", "-C", str(PULSE_REPO), "rev-parse", "--short", "HEAD")
MAX_PAYLOAD_BYTES = 1024 * 1024

# Escalation events are collapsed per "thing" -- one (match_label, subject)
# pair -- instead of one alert per raw line. Measured on the 2026-07 stream:
# 913 raw events inside the 7-day window collapse to 147 groups, so the cap
# below is a defensive backstop against a runaway emitter, not a routine
# truncation. When it does bite, the count is stated in the capability reason;
# alerts are never dropped silently.
ALERT_WINDOW_DAYS = 7
# 往回读几个月的分片。事件按月分文件，只读当月就是又一道把陈年待决挡在门外的
# 闸：一件等人拍板的事，等了多久都还在等，读不到就等于不存在。实测本月最久一条
# 待决 1122 小时（47 天，C-2 adjudicator 接入路径等大鸟+Ken 拍方向），只读当月
# 分片就够不着它。13 覆盖一整年多一个月。
#
# 注意这只放宽了「读哪些文件」，没放宽「文件里取多久」——每一行仍受上面
# `ALERT_WINDOW_DAYS = 7` 的 cutoff。两者不一致意味着 12 个月的分片被读进内存
# 后基本全部丢弃，是纯开销；在 act 真源迁到 `main_only` handoff 之后
# （决策请求不再走这条路）不再造成漏报。曾有一个 `DECISION_REQUEST_WINDOW_DAYS
# = 400` 常量是为对齐这两个环节而设的，从未被接上，2026-08-01 删除。
DECISION_REQUEST_SHARD_MONTHS = 13
MAX_ALERT_GROUPS = 200
# Longest slice of the source message carried into the alert reason.
ALERT_MESSAGE_CHARS = 220

# Everything is stored and compared in UTC; this is the clock the owner reads.
# It is a fixed offset on purpose rather than the machine's local zone: the
# production host runs on UTC and the peer on CST, so anything local-dependent
# would render the same event differently on the two machines.
DISPLAY_TZ = timezone(timedelta(hours=8))

# --- Alert tiers (owner-decided 2026-07-26, act semantics re-set 2026-07-28) ---
# Every collapsed alert carries exactly one tier. Labels still only select
# ``done`` or ``watch``.  A structured Harness routing receipt may select
# ``act`` when ``requires_human=true`` and its decision kind is one of the
# three reviewed facts below; no label or severity can manufacture that fact.
#
# `act` -- 已经从 S2 升到 S3 的协作：**在等大鸟或 Duke 裁决的决策请求**。
# 它的唯一真源是 `main_only: true` 的未闭环 handoff（见 `usage_sources
# .collect_main_only_pending`），由 `_collect_alerts` 直接产出，不经 `tier()`。
# 那个字段是**发起时就声明「必须人来判断」**，分身按设计不能代答。大鸟定的
# 判据一句话——「main_only 为 true，必须大鸟参与，无论 wi 属于谁」。
#
# 判据只有一条：**这条要不要人拍板？** 不是"严不严重"，不是"修没修好"。
# 一个 P0 故障如果修法明确、归 Pulse 执行，它再严重也不进这一档——那是我的活，
# 不是决策请求。反过来一件小事只要卡在"没人有权决定"，它就该在这。
#
# 判错信号：大鸟看到某条的反应是「这不是你自己该处理的吗」——命中即说明
# 内部事务混了进来。
#
# 为什么 act 不再从标签里选（留档以免下次又按"严重"选回来）：07-28 之前有一张
# `TIER_ACT_LABELS` 表是按"严重程度"选的，11 个标签里 7 个是内部事务——探针挂了
# （我重启就行）、分身收口证据缺失（台账没记全，活早干完了）、退化重手处置
# （系统当场自愈了）。大鸟原话点破：「wi 关闭只是流程，我并不关心」。我按
# 「票据卡住」「系统失能」「S3 决策请求」各改了一版，都还是从标签里选；第四轮
# 大鸟看了选出来的 6 条，原话「**都不是我说的真需要大鸟关注的 wi，都是你该做
# 的**」。逐条对下来他是对的：ho_555 分叉处置修法早定好了、duke 的 push 撞分叉
# 同型、hestia 文件回退是要查的活不是要拍的板、C-2 三选一是我和 Duke 该定的工程
# 取舍。**标签说的是「分身不敢往下走」，正确出路是我接过来做或走 Duke，不是堆
# 到大鸟面前。** 那张表遂清成空集，2026-08-01 连同空集一起删除。
#
# `done` -- the system already handled it. Kept for the record, never pushed.
TIER_DONE_LABELS = frozenset({
    "autonomous_success_with_output",
    "autonomous_assigned_work_completed",
    "autonomous_success_with_output_invalidated",
    "executor_reaped",
    "autonomous_rate_limited",
    "autonomous_deferred",
})

# Must match agent-harness ``human_attention.HUMAN_DECISION_KINDS``.  This is
# intentionally a tiny enum, not a label allowlist.
HUMAN_DECISION_KINDS = frozenset({
    "main_only",
    "owner_decision",
    "irreversible_authorization",
})
# Anything not named above defaults to `watch` (the tier names themselves live
# in reducer.py). The burden of proof sits with the alert: if it cannot show it
# deserves to interrupt the owner, it stays down there.

# Silence is a weak proxy for recovery: it only says nothing new was *said*.
# For one family of alerts there is a direct positive signal instead. A
# "probe X is down" alert is a claim about a process that, when healthy,
# posts a heartbeat to Relay on every successful run. A heartbeat stamped
# after the last complaint is therefore proof from the probe's own mouth that
# it ran again and succeeded.
#
# Measured 2026-07-26 (back when an act-listed label still aged out after 48h):
# ack-probe failed once at 02:15 (Relay restarted, dist was stale, the 28s
# rebuild refused its 8787 heartbeat), then succeeded on every run from 02:47
# to 06:31. It nonetheless sat in `act` for hours, because the age window had
# not expired. That is the "查下去无错" failure -- the owner is sent to look at
# something already fixed. That aging rule is gone (see `tier()`); this
# positive signal outlived it.
#
# What recovery evidence buys today: `tier()` no longer consults it -- nothing
# it could demote *to* remains, since escalation labels already top out at
# `watch`. `_collect_alerts` still calls `recovered_at` to count how many
# groups have healed ("已自愈降档" in the capability reason), and the
# `subject_closures` route is the exit for an explicitly disposed escalation.
#
# Only labels with a real heartbeat source appear here. Verified against the
# Relay heartbeats.json and the probe-heartbeats.json registry on 2026-07-26. Deliberately absent:
#   * `heartbeat_watcher_down` -- the watcher *reads* heartbeats.json; it
#     posts none of its own, so a fresh file proves the probes ran, not the
#     watcher. Mapping it would infer recovery from another process's output.
#   * `degen_probe_down` -- degen-probe posts no heartbeat at all; there is
#     no `vultr-degen-probe` key in the file.
# Both are instead covered by RECOVERY_UNIT_NAMES below; absent that, they
# would simply contribute no recovery evidence.
RECOVERY_HEARTBEAT_SOURCES = {
    "ack_probe_down": "vultr-ack-probe",
    "ledger_lint_probe_down": "vultr-ledger-lint-probe",
    "executor_reaper_down": "vultr-executor-reaper",
}

# The second recovery signal, for probes that post no heartbeat of their own.
# A heartbeat is the probe's *self-report*; systemd's exit record is an
# *outside* observation, and a dead probe cannot file a self-report while
# systemd still knows exactly how it exited. So this is the stronger evidence
# of the two, and it is the only evidence available for these units:
#   * dual-heartbeat-watcher reads heartbeats.json, it never posts to it
#   * degen-probe posts no heartbeat at all
# Measured 2026-07-26: the watcher last complained 07-25 17:45 and last exited
# 07-26 06:50 with status 0 -- thirteen hours of proof it recovered.
#
# Units already covered by a heartbeat are deliberately absent: the heartbeat
# is checked first and only falls through to here when it shows no recovery.
RECOVERY_UNIT_NAMES = {
    "heartbeat_watcher_down": "dual-heartbeat-watcher.service",
    "degen_probe_down": "degen-probe.service",
}
# The third recovery signal, for autonomous-run failures. These alerts are not
# about a probe but about an agent's own session: it finished without leaving
# closeout evidence, errored, timed out, or tripped its breaker. None of that
# is visible to systemd (the agent runs inside Relay, not as a unit) and agents
# post no heartbeat, so the proof of recovery is the agent's *next* run
# succeeding, recorded in the autonomous execution stream.
#
# Measured 2026-07-26: pulse last reported wrap_missing 07-25 13:02Z and
# succeeded 17:02Z; amanda last reported it 07-25 14:02Z and has succeeded
# twice since, most recently 07-26 03:06Z. Both nonetheless sat in `act`.
RECOVERY_AGENT_LABELS = frozenset({
    "autonomous_wrap_missing",
    "autonomous_error",
    "autonomous_timeout",
    "circuit_broken",
})
# Only a completed, successful run counts. `started` says the session was
# launched, not that it got anywhere -- a probe that keeps starting and keeps
# failing must not read as healthy.
RECOVERY_AGENT_SUCCESS_STATUS = "success"
AUTONOMOUS_EVENTS_PREFIX = "autonomous_execution_event"
RECOVERY_UNIT_ARGV = (
    "/usr/bin/systemctl",
    "--user",
    "show",
    *sorted(set(RECOVERY_UNIT_NAMES.values())),
    "--property=Id,ExecMainExitTimestamp,ExecMainStatus",
    "--no-pager",
)
# Own budget, well under the shared 4s: this is a local, non-blocking query and
# a slow one must never hold up the snapshot. Timeout means "no evidence", so
# the group is simply not counted as healed -- never the reverse.
RECOVERY_UNIT_TIMEOUT_SECONDS = 3
# systemd renders timestamps as "Sun 2026-07-26 06:50:11 UTC" -- a weekday, a
# naive datetime, and a zone abbreviation -- so it needs its own parser rather
# than `_parse_date`.
RECOVERY_UNIT_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

BASELINE_VERSION = "0.2.2"

# Only exact, reviewed labels may cross from the append-only escalation stream
# into the user-visible snapshot. Unknown labels stay in the source ledger but
# are not copied into the App cache.
ALERT_TEMPLATES = {
    "ack_overdue": ("s2.ack_overdue", "Handoff acknowledgement is overdue"),
    "ledger_lint_breach": ("s2.ledger_invariants", "Harness ledger invariant finding"),
    "executor_stale_unknown": ("s2.executor_reaper", "Executor state needs review"),
    "executor_reaped": ("s2.executor_reaper", "Stale executor was reaped"),
    "degen_critical": ("s1.reply_quality_degen", "Critical reply degeneration detected"),
    "degen_burst": ("s1.reply_quality_degen", "Reply degeneration burst detected"),
    "degen_detector_blind": ("s1.reply_quality_degen", "Reply degeneration detector is blind"),
    "degen_probe_down": ("s1.reply_quality_degen", "Reply degeneration probe is down"),
    "heartbeat_watcher_down": ("meta.probe_heartbeat", "Heartbeat watcher is down"),
    "heartbeat_escalation": ("meta.probe_heartbeat", "Heartbeat source is stale"),
    "ack_probe_down": ("s2.ack_overdue", "Acknowledgement probe is down"),
    "ledger_lint_probe_down": ("s2.ledger_invariants", "Ledger lint probe is down"),
    "executor_reaper_down": ("s2.executor_reaper", "Executor reaper is down"),
    "reply_probe_down": ("s1.request_reply_reconciliation", "Retired reply probe emitted an event"),
    "reply_overdue": ("s1.request_reply_reconciliation", "Historical reply overdue event"),
    "circuit_broken": ("notify.action_package", "Monitoring circuit is broken"),
    "red_line_flag": ("notify.action_package", "Monitoring red line was raised"),
    # 日巡当晚清不掉的残余,以「我搞不定,请裁决」的形式升到大鸟面前。
    # 2026-07-28 补登记：此前该 label 不在本表,而 `_collect_alerts` 用
    # `ALERT_TEMPLATES[match_label]` 取模板、KeyError 直接 continue——
    # **日巡推给大鸟的每一条都会被 exporter 静默丢弃,菜单栏上一个字都看不到。**
    # 又一次「机制只做了一半」：推送通道通了,展示侧没登记。
    "daily_patrol_action_required": ("notify.action_package", "Daily patrol needs a decision"),
    "autonomous_success_with_output": ("notify.action_package", "Autonomous work completed with output"),
    "autonomous_success_with_output_invalidated": ("notify.action_package", "Autonomous output was invalidated"),
    "autonomous_assigned_work_completed": ("notify.action_package", "Assigned autonomous work completed"),
    "autonomous_rate_limited": ("notify.action_package", "Autonomous work was rate limited"),
    "autonomous_error": ("notify.action_package", "Autonomous work reported an error"),
    "autonomous_deferred": ("notify.action_package", "Autonomous work was deferred"),
    "autonomous_tool_denied": ("notify.action_package", "Autonomous tool access was denied"),
    "autonomous_wrap_missing": ("notify.action_package", "Autonomous closeout evidence is missing"),
    "autonomous_timeout": ("notify.action_package", "Autonomous work timed out"),
    "autonomous_path_constraint_gap": ("notify.action_package", "Autonomous path constraint needs review"),
    "autonomous_blocked_needs_main": ("notify.action_package", "Autonomous work needs the main owner"),
    "autonomous_permission_gap": ("notify.action_package", "Autonomous permission gap needs review"),
    "autonomous_blocked": ("notify.action_package", "Autonomous work is blocked"),
    "label_unregistered": ("notify.action_package", "Escalation label is not registered"),
    "whitelist_boundary_patch_pending": ("notify.action_package", "Whitelist boundary patch is pending"),
    "cross_machine_action_required": ("notify.action_package", "Cross-machine action is required"),
    "policy_write_deny_blocked": ("notify.action_package", "Policy denied a write action"),
    "workspace_regression_detected": ("notify.action_package", "Workspace regression was detected"),
}


class LiveSnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], timeout: int = 4) -> CommandResult:
        ...


class SubprocessCommandRunner:
    def run(self, argv: Sequence[str], timeout: int = 4) -> CommandResult:
        completed = subprocess.run(
            list(argv),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            shell=False,
        )
        return CommandResult(completed.returncode, completed.stdout)


def codex_remote_argv() -> Sequence[str]:
    """Fixed read-only remote command for the peer machine's Codex quota."""
    # Two greps per file, not one: the quota reading lives in `token_count`
    # while an exhausted allowance is only knowable from `task_complete`
    # (see CODEX_USAGE_LIMIT_ERROR). Taking just the former would leave the
    # peer machine reporting "unknown" for the one state worth knowing.
    remote = (
        f'find {CODEX_REMOTE_SESSIONS} -name "*.jsonl" '
        f'-newermt "-{CODEX_LOOKBACK_DAYS} days" 2>/dev/null '
        f"| sort -r | head -{CODEX_SCAN_FILES} "
        '| while read -r f; do '
        'grep -h \'"type":"token_count"\' "$f" 2>/dev/null | tail -1; '
        'grep -h \'"type":"task_complete"\' "$f" 2>/dev/null | tail -1; '
        "done"
    )
    return (
        "/usr/bin/ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=accept-new",
        CODEX_REMOTE_HOST,
        remote,
    )


def _metric(
    metric_id: str,
    capability_id: str,
    *,
    value: object,
    unit: str,
    observed_at: Optional[datetime],
    window: str = "current",
    quality: str = "validated",
    scope: str = "fixed production source",
    sample_count: Optional[int] = None,
    reason: Optional[str] = None,
    max_age_seconds: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Mapping[str, object]:
    """Build one snapshot metric.

    A ``value`` of ``None`` is reported as ``unknown`` rather than being coerced
    to zero, per spec §1 ("拿不到的维度显式缺席或标 unknown").

    A value that is too *old* is also reported as ``unknown``. Until 2026-07-28
    ``status`` was decided solely by ``value is None``, so a reading that had
    been collected once and never again stayed ``available``/``validated``
    forever: an 8-day-old Codex figure was indistinguishable in the snapshot
    from one taken a second ago, and the menu bar duly showed it as fact. Age is
    only checked when a caller supplies ``max_age_seconds`` -- most metrics are
    stamped with the collection time and cannot go stale by construction, so
    demanding a policy from every call site would be noise.
    """
    stale_reason: Optional[str] = None
    if (
        value is not None
        and max_age_seconds is not None
        and observed_at is not None
        and now is not None
    ):
        age = (now - observed_at).total_seconds()
        if age >= max_age_seconds:
            stale_reason = (
                f"Last observed {_format_age(age)} ago, older than the "
                f"{_format_age(max_age_seconds)} freshness limit for this metric"
            )

    unknown = value is None or stale_reason is not None
    return {
        "id": metric_id,
        "capability_id": capability_id,
        "status": "unknown" if unknown else "available",
        # A stale reading is withheld rather than shipped alongside its own
        # disclaimer: every consumer that reads `value` would otherwise have to
        # remember to check `status` first, and the menu bar demonstrably did
        # not. Absence is the one signal that cannot be misread.
        "value": None if stale_reason is not None else value,
        "unit": unit,
        "window": window,
        "sample_count": sample_count,
        "quality": quality,
        "scope": scope,
        "observed_at": observed_at.isoformat() if observed_at else None,
        "reason": stale_reason or reason,
    }


def _format_age(seconds: float) -> str:
    """Coarse human duration for staleness reasons ("3天" / "5小时" / "20分钟")."""
    seconds = max(0.0, float(seconds))
    if seconds >= 86400:
        return f"{int(seconds // 86400)}天"
    if seconds >= 3600:
        return f"{int(seconds // 3600)}小时"
    if seconds >= 60:
        return f"{int(seconds // 60)}分钟"
    return f"{int(seconds)}秒"


@dataclass(frozen=True)
class LivePaths:
    retire_legacy_probes: bool = False  # Historical replay default; production retires the old probes.
    # "systemd" replays the retired Linux reader; production on ownermac is "launchd".
    relay_supervisor: str = "systemd"
    emit_legacy_cost_metrics: bool = True  # Historical replay only; production uses the ComputeCost carrier.
    repo: Path = PULSE_REPO
    harness_repo: Path = HARNESS_SERVICE_REPO
    surface_registry: Path = SURFACE_REGISTRY_PATH
    global_authority: Path = GLOBAL_AUTHORITY_PATH
    canonical_read_model: Path = CANONICAL_READ_MODEL_PATH
    # None: no heartbeat registry is registered for this reader.
    registry: Optional[Path] = REGISTRY_PATH
    heartbeats: Path = HEARTBEAT_PATH
    ledger: Path = LEDGER_PATH
    alerts_dir: Path = ALERTS_DIR
    state: Path = STATE_PATH
    work_items: Path = WORK_ITEMS_DIR
    handoffs: Path = HANDOFFS_DIR
    policies: Path = POLICIES_DIR
    obs: Path = OBS_DIR
    subscriptions: Path = SUBSCRIPTIONS_CONFIG_PATH
    cc_projects: Path = CC_PROJECTS_DIR
    codex_sessions: Path = CODEX_LOCAL_SESSIONS
    codex_quota: Optional[Path] = None
    # Relay-observed CC quota publication. When set, the legacy header probe is not used.
    cc_quota: Optional[Path] = None
    # Grok account weekly quota fact from the cost publisher (grok_quota).
    grok_quota: Optional[Path] = None
    # agy account quota groups from the cost publisher (agy_quota).
    agy_quota: Optional[Path] = None
    # Native core WorkItem facts (harness.stats_facts/v1). When set, WI metrics,
    # human requests and the authority lamp come only from these facts.
    wi_facts: Optional[Path] = None
    # S2 opt-in only. `LivePaths()` is deliberately off even when a candidate
    # file happens to exist; production wiring must pass the exact flag.
    compute_cost_shadow_enabled: bool = False
    compute_cost_shadow: Optional[Path] = None


@dataclass
class Collection:
    capabilities: List[Mapping[str, object]]
    metrics: List[Mapping[str, object]]
    events: List[IncidentEvent]
    provenance_sources: List[Mapping[str, object]]
    source_revisions: Dict[str, str]
    source_watermarks: Dict[str, str]
    source_cursors: Dict[str, str]
    incident_action_overrides: Dict[str, str]
    core_successes: int
    projection_complete: bool


def _parse_date(value: object) -> datetime:
    """Parse a source timestamp and normalise it to UTC.

    The escalation stream carries a genuine mix of offsets -- measured on the
    2026-07 file, 916 records end in ``Z`` and 552 in ``+08:00`` -- because
    emitters inherit whichever clock their machine runs on. Comparisons across
    aware datetimes would still be correct, but formatting reads the *fields*,
    so an un-normalised value renders on its own clock and lands in a list
    beside UTC ones with nothing marking the difference. Normalising here means
    every downstream comparison and every format string works in one timezone.
    """
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _parse_systemd_timestamp(value: object) -> Optional[datetime]:
    """Parse systemd's ``Sun 2026-07-26 06:50:11 UTC`` into an aware UTC datetime.

    Returns ``None`` for anything unparseable -- including the empty string
    systemd prints for a unit that has never run, and any timezone this cannot
    prove the offset of. Guessing an offset would silently shift the comparison
    by hours and could declare a still-broken probe recovered, so an unknown
    zone is treated as no evidence at all.

    Only UTC is accepted because the production host runs on UTC; the peer
    machine's units are never queried here.
    """
    text = str(value).strip()
    if not text:
        return None
    parts = text.split()
    # Drop the leading weekday ("Sun"), which is decoration, not data.
    if parts and not parts[0][:1].isdigit():
        parts = parts[1:]
    if len(parts) != 3 or parts[2] not in ("UTC", "GMT", "Z"):
        return None
    try:
        naive = datetime.strptime(f"{parts[0]} {parts[1]}", RECOVERY_UNIT_TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return naive.replace(tzinfo=timezone.utc)


def _short_hash(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:20]


def _source(
    source_id: str,
    freshness: str,
    observed_at: Optional[datetime] = None,
    error_code: Optional[str] = None,
) -> Mapping[str, object]:
    return {
        "id": source_id,
        "freshness": freshness,
        "observed_at": observed_at.isoformat() if observed_at else None,
        "error_code": error_code,
    }


def _capability(
    capability_id: str,
    *,
    health: str,
    freshness: str,
    observed_at: Optional[datetime],
    max_age_seconds: Optional[int],
    reason: str,
    implementation_status: str = "active_validated",
    aggregation_role: str = "runtime",
    evidence: Iterable[str] = (),
) -> Mapping[str, object]:
    # Implementation maturity and current observation are separate axes, but a
    # capability cannot claim "validated" while either structured observation
    # axis is explicitly unknown. Measured failures (warning/critical/stale/
    # error) remain validated: they prove the integration is observing a bad
    # result rather than proving the integration is absent.
    if implementation_status == "active_validated" and (
        health == "unknown" or freshness == "unknown"
    ):
        implementation_status = "active_partial"
    return {
        "id": capability_id,
        "implementation_status": implementation_status,
        "health": health,
        "aggregation_role": aggregation_role,
        "scope": "fixed production source",
        "reason": reason,
        "freshness": {
            "state": freshness,
            "observed_at": observed_at.isoformat() if observed_at else None,
            "max_age_seconds": max_age_seconds,
        },
        "evidence": list(evidence),
    }
