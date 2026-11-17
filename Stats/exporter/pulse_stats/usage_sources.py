"""Read-only metric collectors for Stats v2-A (spec pulse-stats-menubar-v2 §3).

Every collector here is strictly read-only and independently failure-isolated:
an unavailable source yields ``None`` (rendered as an ``unknown`` metric) and
never aborts the surrounding snapshot, and never substitutes ``0`` for a value
that was not actually observed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import statistics
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import AbstractSet, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

try:  # libyaml bindings cut full harness parsing from ~2.5s to ~0.4s
    from yaml import CSafeLoader as _YamlLoader
except ImportError:  # pragma: no cover - exercised only without libyaml
    from yaml import SafeLoader as _YamlLoader

from .live_contract import (
    CC_CREDENTIALS_PATH,
    CC_RELAY_ENV_PATH,
    CC_QUOTA_ANTHROPIC_VERSION,
    CC_QUOTA_HEADER_PREFIX,
    CC_QUOTA_MAX_TOKENS,
    CC_QUOTA_MODEL,
    CC_QUOTA_OAUTH_BETA,
    CC_QUOTA_TIMEOUT_SECONDS,
    CC_QUOTA_URL,
    CC_QUOTA_CACHE_PATH,
    CC_RATE_LIMIT_MAX_FILES,
    CC_RATE_LIMIT_WINDOW_HOURS,
    CODEX_PROBE_ARGV,
    CODEX_PROBE_MIN_INTERVAL_SECONDS,
    CODEX_PROBE_TIMEOUT_SECONDS,
    CODEX_SCAN_FILES,
    CODEX_USAGE_LIMIT_ERROR,
    DISPLAY_TZ,
    IN_FLIGHT_ACTIVE_DAYS,
    IN_FLIGHT_SWEEP_DAYS,
    RELAY_HTTP_TIMEOUT_SECONDS,
    SESSION_ID_PREFIX_CHARS,
    USAGE_TOP_AGENTS,
    USAGE_TOP_SESSIONS,
    WI_STALLED_DAYS,
    WI_STALLED_MAX_ITEMS,
)

WI_OPEN_STATUSES = ("in_progress", "ready", "draft")
# 「在飞」比「开放」宽：blocked 与 awaiting_verification 也是摊在台面上的活。
# 2026-08-07 大鸟定：这 6 件此前被 WI_OPEN_STATUSES 挡在面板外，
# 「在飞 11 件」这个数一直少算——hestia 那件「卡真人换电池」从未出现在大鸟眼前。
# **刻意不改 WI_OPEN_STATUSES**：它同时服务停滞列表与闭环判据，动它会波及两处。
WI_IN_FLIGHT_STATUSES = (
    "in_progress",
    "ready",
    "draft",
    "blocked",
    "awaiting_verification",
)
CANONICAL_HARNESS_CLI = Path.home() / ".local/bin/harness"
# 终态 = 这张票据不再等人动手。`accepted` 不在内——已接不等于已办完,
# 把它算作恢复会在活还在做的时候把告警灭掉。
HANDOFF_TERMINAL_STATES = ("archived", "rejected", "timed_out")
HANDOFF_ACTION_STATES = ("pending", "accepted")


def main_only_still_open(
    item: Mapping, wi_status: Optional[str]
) -> bool:
    """一张 `main_only` handoff 是否仍等大鸟出手。「要你出手」的唯一判据。

    **闭环与否看它挂的 wi 是否终态,不看 `ack_state`**(2026-07-29 大鸟
    「按事实处理」)。原判据用 `ack_state in HANDOFF_TERMINAL_STATES`,
    与 `main_only` 的语义直接矛盾:

    * `main_only: true` 的意思是「分身按设计不能代答,必须人来判断」;
    * 而 `ack_state` 恰恰是**分身能改的字段**——分身 ack 一下就变终态。

    于是「分身不该碰的单」因为「分身碰了一下」而静默移出大鸟视野。实测
    ho_20260726_251(ken→pulse,spec-anchor 装备邀请)07-27 05:45 被日巡
    分身 ack,此后三天对大鸟不可见,直到 07-29 04:18 wi 才真关票——大鸟
    全程没看见过,却以为自己看见并放过了。

    wi 终态才是「事情办完了」的事实;ack 只是「有人接了手」。改判后实测
    23 条非跨租户 main_only 中仍只有 1 条在飞(ho_20260724_903,wi
    in_progress),数字不涨——因为已闭环的单本来就 wi 也终态了。真正的
    差别在时间轴:单子从 ack 那刻起可见,一直可见到真办完。

    `wi_status` 为 ``None``(wi 文件缺失/坏 YAML/无 work_item_id)时判为
    **仍开着**——宁可多报一条让大鸟看见,不可因读不到而静默隐藏。
    """
    if item.get("main_only") is not True:
        return False
    # 跨租户默认就带 main_only——对面是外部真人(Eric / 长征),必须人应答、
    # 分身不能代。但**那个「人」是 Ken,不是大鸟**。不排掉的话,Ken 跟 Eric
    # 谈部署进度会出现在大鸟菜单栏上(2026-07-28 实测 8 条里 4 条是这一族,
    # 全库 34 条 main_only 里 ken↔cz-pulse 一对就占 11 条)。
    #
    # 靠 cross_tenant 反推是**短期方案,大鸟明确认可**:根因是 main_only
    # 一个布尔值承载了两种意思——「必须大鸟参与」(大鸟要求才打)与
    # 「跨租户默认」。真正的修法是把后者分出独立字段,属全队字段语义变更,
    # 不由 Pulse 单方拍。
    if item.get("cross_tenant") is True:
        return False
    if wi_status is None:
        return True
    # blocked / awaiting_verification 都不是终态：前者仍在等解阻，后者仍在等
    # 验收。用较窄的 WI_OPEN_STATUSES 会把这两类人工单误当成已经办完。
    return wi_status in WI_IN_FLIGHT_STATUSES


def _parse_ts(value: object) -> Optional[datetime]:
    """Parse an ISO timestamp, normalising to UTC. Returns None if unusable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_relay_internal_token(path: Path = CC_RELAY_ENV_PATH) -> Optional[str]:
    """Read Relay's HTTP token without sourcing or exposing the env file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("RELAY_INTERNAL_TOKEN="):
            continue
        token = line.split("=", 1)[1].strip().strip('"').strip("'")
        return token or None
    return None


def fetch_json(
    url: str,
    *,
    timeout: int = RELAY_HTTP_TIMEOUT_SECONDS,
    relay_env_path: Path = CC_RELAY_ENV_PATH,
) -> Optional[Mapping[str, object]]:
    """GET a token-authenticated loopback Relay endpoint; failures yield None."""
    token = read_relay_internal_token(relay_env_path)
    headers = {"x-relay-token": token} if token else {}
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed loopback URL
            if getattr(response, "status", 200) != 200:
                return None
            raw = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return raw if isinstance(raw, Mapping) else None


# --------------------------------------------------------------------------
# §3.1 Relay
# --------------------------------------------------------------------------


def collect_cc_status(payload: Optional[Mapping[str, object]]) -> Dict[str, Optional[int]]:
    """Normalise /internal/cc-status.

    The live endpoint reports counts as
    ``{starting, ready, busy, idle, dead}``; the spec table names only
    ``busy``/``idle``. Anything absent stays None rather than becoming 0.
    """
    result: Dict[str, Optional[int]] = {"processes": None, "busy": None, "idle": None}
    if not isinstance(payload, Mapping):
        return result
    counts = payload.get("counts")
    if isinstance(counts, Mapping):
        for key in ("busy", "idle"):
            value = counts.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                result[key] = value
    total = payload.get("total")
    if isinstance(total, int) and not isinstance(total, bool):
        result["processes"] = total
    elif isinstance(counts, Mapping):
        numeric = [v for v in counts.values() if isinstance(v, int) and not isinstance(v, bool)]
        if numeric and len(numeric) == len(counts):
            result["processes"] = sum(numeric)
    return result


def collect_health(payload: Optional[Mapping[str, object]]) -> Dict[str, Optional[int]]:
    result: Dict[str, Optional[int]] = {"bots_online": None, "uptime_seconds": None}
    if not isinstance(payload, Mapping):
        return result
    bots = payload.get("feishuBots")
    if isinstance(bots, int) and not isinstance(bots, bool):
        result["bots_online"] = bots
    uptime = payload.get("uptime")
    if isinstance(uptime, (int, float)) and not isinstance(uptime, bool):
        result["uptime_seconds"] = int(uptime)
    return result


def detect_cc_rate_limited(
    projects_dir: Path,
    now: datetime,
    *,
    window_hours: int = CC_RATE_LIMIT_WINDOW_HOURS,
    max_files: int = CC_RATE_LIMIT_MAX_FILES,
) -> Optional[bool]:
    """Whether a genuine 429 occurred within the trailing window.

    File mtime is only a cheap prefilter for *which* files to open. The window
    is re-applied to each record's own ``timestamp`` because transcripts that
    were appended to today routinely still hold error records from weeks ago
    (verified 2026-07-25: the only two 429 records on disk were from 06-11 and
    06-18, so an mtime-only rule would have falsely reported a red light).
    """
    if not projects_dir.is_dir():
        return None
    cutoff = now - timedelta(hours=window_hours)
    try:
        candidates: List[Tuple[float, Path]] = []
        for path in projects_dir.glob("*/*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff.timestamp():
                candidates.append((mtime, path))
    except OSError:
        return None
    if not candidates:
        return False
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _, path in candidates[:max_files]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "429" not in line or "rate_limit" not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(record, Mapping):
                        continue
                    if not _record_is_rate_limit_429(record):
                        continue
                    occurred = _parse_ts(record.get("timestamp"))
                    if occurred is not None and occurred >= cutoff:
                        return True
        except OSError:
            continue
    return False


def _record_is_rate_limit_429(record: Mapping[str, object]) -> bool:
    if record.get("apiErrorStatus") != 429:
        return False
    blob = json.dumps(record, ensure_ascii=False)
    return '"error":"rate_limit' in blob.replace(" ", "")


def _extract_quota(line: str) -> Optional[Dict[str, object]]:
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    payload = record.get("payload")
    if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, Mapping):
        return None
    primary = limits.get("primary")
    if not isinstance(primary, Mapping):
        return None
    used = primary.get("used_percent")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    observed = _parse_ts(record.get("timestamp"))
    if observed is None:
        return None
    resets_at = primary.get("resets_at")
    plan = limits.get("plan_type")
    return {
        "used_percent": float(used),
        "resets_at": int(resets_at) if isinstance(resets_at, int) and not isinstance(resets_at, bool) else None,
        "plan_type": str(plan) if isinstance(plan, str) else None,
        "observed_at": observed,
    }


def _extract_exhaustion(line: str) -> Optional[Dict[str, object]]:
    """A refused turn as a quota reading of 100%.

    When the weekly allowance is spent the server stops executing the turn, so
    there is no usage to report: `token_count` still arrives but with
    `primary`/`secondary` both null, and `limit_id` flips to the empty
    `premium` bucket. Read on its own that looks like "the quota is unknown" --
    yet the very next line of the same session says exactly what happened:

        "error": {"message": "You've hit your usage limit. ... try again at
                   Aug 5th, 2026 4:10 AM.",
                  "codex_error_info": "usage_limit_exceeded"}

    So exhaustion is not missing data, it is data in a different place. This
    reads that place and reports the fact `token_count` cannot: 100% used.

    Deliberately keyed on `codex_error_info`, a stable enum, rather than on the
    English prose of `message` -- and the reset time is taken from the record's
    own `resets_at` when present rather than parsed out of that prose, which is
    localised and reworded at the vendor's convenience.
    """
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    payload = record.get("payload")
    if not isinstance(payload, Mapping) or payload.get("type") != "task_complete":
        return None
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    if error.get("codex_error_info") != CODEX_USAGE_LIMIT_ERROR:
        return None
    observed = _parse_ts(record.get("timestamp"))
    if observed is None:
        return None
    return {
        "used_percent": 100.0,
        "resets_at": None,
        "plan_type": None,
        "observed_at": observed,
        # Distinguishes "measured at 100%" from "refused because spent" for
        # callers that want to say so; the percentage is identical either way.
        "exhausted": True,
    }


def _scan_codex_line(line: str) -> Optional[Dict[str, object]]:
    """One session-log line as a quota reading, whichever form it takes."""
    if '"token_count"' in line:
        quota = _extract_quota(line)
        if quota is not None:
            return quota
    if '"task_complete"' in line:
        return _extract_exhaustion(line)
    return None


def _merge_exhaustion_resets(records: List[Dict[str, object]]) -> None:
    """Backfill an exhaustion record's reset time from its session's own quota.

    A refused turn carries no `resets_at`, but the last successful reading in
    the same window does, and the weekly window is the same one. Without this
    the panel would show "100%" with no indication of when it lifts, which is
    the half of the fact the owner actually acts on.
    """
    known = [r for r in records if not r.get("exhausted") and r.get("resets_at")]
    if not known:
        return
    newest = max(known, key=lambda r: r["observed_at"])
    for record in records:
        if record.get("exhausted") and record.get("resets_at") is None:
            record["resets_at"] = newest["resets_at"]
            if record.get("plan_type") is None:
                record["plan_type"] = newest.get("plan_type")


def collect_codex_local(
    sessions_dir: Path, *, scan_files: int = CODEX_SCAN_FILES
) -> Optional[Dict[str, object]]:
    """Newest Codex quota record on this machine, or None if unavailable."""
    if not sessions_dir.is_dir():
        return None
    try:
        files: List[Tuple[float, Path]] = []
        for path in sessions_dir.rglob("*.jsonl"):
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return None
    if not files:
        return None
    files.sort(key=lambda item: item[0], reverse=True)
    found: List[Dict[str, object]] = []
    for _, path in files[:scan_files]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    record = _scan_codex_line(line)
                    if record is not None:
                        found.append(record)
        except OSError:
            continue
    if not found:
        return None
    _merge_exhaustion_resets(found)
    return max(found, key=lambda item: item["observed_at"])


def parse_codex_remote(stdout: str) -> Optional[Dict[str, object]]:
    found: List[Dict[str, object]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        record = _scan_codex_line(line)
        if record is not None:
            found.append(record)
    if not found:
        return None
    _merge_exhaustion_resets(found)
    return max(found, key=lambda item: item["observed_at"])


def merge_codex(
    local: Optional[Mapping[str, object]], remote: Optional[Mapping[str, object]]
) -> Optional[Dict[str, object]]:
    """Codex quota is account-level and shared, so the newest record wins."""
    candidates = [item for item in (local, remote) if item]
    if not candidates:
        return None
    winner = dict(max(candidates, key=lambda item: item["observed_at"]))
    # The allowance is one account-wide weekly window, so a reset time seen on
    # either machine describes the winner too. Worth crossing over: the newest
    # record is typically this box's exhaustion notice, which carries no
    # `resets_at`, while the figure sits in the peer's last successful reading.
    if winner.get("resets_at") is None:
        donors = [item for item in candidates if item.get("resets_at")]
        if donors:
            donor = max(donors, key=lambda item: item["observed_at"])
            winner["resets_at"] = donor["resets_at"]
            if winner.get("plan_type") is None:
                winner["plan_type"] = donor.get("plan_type")
    return winner


def codex_probe_is_due(
    quota: Optional[Mapping[str, object]],
    now: datetime,
    *,
    min_interval_seconds: int = CODEX_PROBE_MIN_INTERVAL_SECONDS,
) -> bool:
    """Whether to spend a Codex turn minting a fresh quota reading.

    Due when there is no reading at all, or when the newest one is older than
    the rationing floor. Anything fresher is good enough: the probe costs real
    subscription quota, so it must not run once per collection.

    A reading stamped in the future (clock skew between the two machines) is
    treated as due rather than trusted -- the alternative is suppressing the
    probe indefinitely on the strength of a timestamp we already know is wrong.
    """
    if not quota:
        return True
    observed = quota.get("observed_at")
    if not isinstance(observed, datetime):
        return True
    age = (now - observed).total_seconds()
    if age < 0:
        return True
    return age >= min_interval_seconds


def run_codex_probe(
    argv: Optional[Sequence[str]] = CODEX_PROBE_ARGV,
    timeout: int = CODEX_PROBE_TIMEOUT_SECONDS,
) -> bool:
    """Run one minimal headless Codex turn so it writes a fresh quota record.

    The turn's *output* is discarded -- the point is the `token_count` payload
    it appends to the session log on the way past. Returns whether the turn
    exited cleanly; callers re-read the sessions directory either way, since a
    non-zero exit does not prove nothing was written.

    stdin is closed explicitly: `codex exec` otherwise blocks reading additional
    input from the terminal, which in a timer-driven exporter is a hang, not a
    prompt.
    """
    if argv is None:
        return False
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def collect_codex(
    sessions_dir: Path,
    now: datetime,
    *,
    remote_stdout: Optional[str] = None,
    probe: Optional[Callable[[], bool]] = None,
    min_interval_seconds: int = CODEX_PROBE_MIN_INTERVAL_SECONDS,
) -> Optional[Dict[str, object]]:
    """Newest Codex quota reading, minting one first if the on-disk copy is old.

    Order matters: read what is already there, decide whether that is fresh
    enough, and only then spend a turn. After probing, re-read -- the new record
    lands in a brand new session file, so the pre-probe scan cannot have seen it.
    """
    local = collect_codex_local(sessions_dir)
    remote = parse_codex_remote(remote_stdout) if remote_stdout else None
    quota = merge_codex(local, remote)

    if not codex_probe_is_due(quota, now, min_interval_seconds=min_interval_seconds):
        return quota

    runner = probe if probe is not None else run_codex_probe
    try:
        runner()
    except Exception:  # noqa: BLE001 - a quota probe must never break the snapshot
        return quota

    refreshed = merge_codex(collect_codex_local(sessions_dir), remote)
    if refreshed is None:
        return quota
    if quota is None:
        return refreshed
    # Keep whichever is actually newer: a failed probe leaves the old record as
    # the best available answer rather than regressing to None.
    return refreshed if refreshed["observed_at"] >= quota["observed_at"] else quota


# --------------------------------------------------------------------------
# CC (Claude Code) subscription quota -- response headers only
# --------------------------------------------------------------------------


def read_relay_env_token(path: Path = CC_RELAY_ENV_PATH) -> Optional[str]:
    """读 Relay 注给每个 CC 进程的 `CLAUDE_CODE_OAUTH_TOKEN`。

    **这是生产上真正在用的那份凭证**，长期有效、由大鸟维护。返回值是活凭证，
    只能当请求头用，绝不许进 metric / 日志 / 快照。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
            continue
        # 形如 KEY=value / KEY="value"，两种都要吃。
        token = line.split("=", 1)[1].strip().strip('"').strip("'")
        return token or None
    return None


def read_cc_oauth_token(
    path: Path = CC_CREDENTIALS_PATH,
    relay_env_path: Path = CC_RELAY_ENV_PATH,
) -> Optional[str]:
    """CC OAuth access token，取不到返回 None。

    **先读 relay.env，后退回 credentials.json**（2026-07-30 定序）。

    「CC 额度未知」发作三次，三次都栽在读错源上：
    `~/.claude/.credentials.json` 是 **CC TUI 交互登录留下的副产物**，里面的
    access token 只活 8 小时，且**过期后要等下一次交互登录才被刷回**——vultr
    是生产机，没人天天去 TUI 里登录，所以那个字段几乎总是过期的。而生产上
    的 CC 进程根本不读它：Relay 从 `relay.env` 取 `CLAUDE_CODE_OAUTH_TOKEN`
    注进每个进程的环境，那份长期有效。

    于是三次「未知」的真相是：**探针拿一份没人用的过期凭证去问，而生产一直
    好着**。前两次我改的是呈现（说清原因、加缓存兜底），都没碰这个根。

    退回 credentials.json 是为了本机（ownermac）等没有 relay.env 的场景，
    那里交互登录反而是常态。
    """
    token = read_relay_env_token(relay_env_path)
    if token:
        return token
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, Mapping):
        return None
    oauth = raw.get("claudeAiOauth")
    if not isinstance(oauth, Mapping):
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        return None
    return token


def read_cc_quota_cache(path: Path = CC_QUOTA_CACHE_PATH) -> Optional[Dict[str, object]]:
    """最近一次成功读到的 CC 额度，含它是什么时候读到的。

    只存额度数字与时间戳，**不含任何凭证**。读不出就是 None，从不抛。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, Mapping):
        return None
    observed = _parse_ts(raw.get("observed_at"))
    if observed is None:
        return None
    used = raw.get("used_percent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    return {
        "used_percent": float(used),
        "resets_at": raw.get("resets_at"),
        "five_hour_used_percent": raw.get("five_hour_used_percent"),
        "observed_at": observed,
    }


def write_cc_quota_cache(
    quota: Mapping[str, object], now: datetime, path: Path = CC_QUOTA_CACHE_PATH
) -> None:
    """把刚读到的额度落盘。写失败不许影响快照——这是锦上添花，不是核心源。"""
    payload = {
        "used_percent": quota.get("used_percent"),
        "resets_at": quota.get("resets_at"),
        "five_hour_used_percent": quota.get("five_hour_used_percent"),
        "observed_at": now.isoformat(),
    }
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return


def cc_token_expiry(
    path: Path = CC_CREDENTIALS_PATH,
    relay_env_path: Path = CC_RELAY_ENV_PATH,
) -> Optional[datetime]:
    """When the CC access token expires, from the credential file's own stamp.

    **2026-07-30：生产用的是 relay.env 那份，它没有过期戳，所以这里返回
    None（＝「说不上」）而不是 credentials.json 里那个与生产无关的时间。**
    拿一个没人用的凭证的过期时间去解释生产状态，正是「CC 未知」被误诊三次的
    帮凶——面板上写「令牌 17 小时前过期」，听起来像根因，其实指着一份闲置文件。
    None 的语义是「查不出」，调用方不得当作「没问题」。

    Why this exists (2026-07-28): the menu bar read ``CC 未知`` and nobody --
    including me -- could tell from the panel whether production was broken,
    the network was down, or a token had simply aged out. The answer was in
    ``expiresAt``, sitting in the same file two lines from the token we do
    read, and no layer looked at it. The access token lives ~8 hours, so this
    cell goes blind on a schedule; without the stamp every occurrence costs a
    fresh investigation.

    Reads only the timestamp, never the credential. Returns None when the file
    or the field is unusable -- absence of a stamp is not evidence of validity,
    so callers must treat None as "cannot tell", not "fine".
    """
    # 生产走 relay.env 那份长期凭证；它没有过期戳，也不该拿隔壁文件的戳冒充。
    if read_relay_env_token(relay_env_path):
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, Mapping):
        return None
    oauth = raw.get("claudeAiOauth")
    if not isinstance(oauth, Mapping):
        return None
    expires_at = oauth.get("expiresAt")
    # The file stores epoch milliseconds; bool is excluded because it is an int.
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(expires_at / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _utilization_percent(headers: Mapping[str, str], key: str) -> Optional[float]:
    """Convert a ``*-utilization`` header (a 0-1 fraction) to a 0-100 percent.

    Anthropic reports utilization as a fraction (0.42), whereas the Codex
    source already reports 0-100. The scaling lives here so both quota metrics
    reach the snapshot on the same scale. A value outside 0-1 is treated as
    unusable rather than silently clamped.
    """
    raw = headers.get(key)
    if raw is None:
        return None
    try:
        fraction = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if fraction != fraction or fraction < 0.0 or fraction > 1.0:  # NaN or out of contract
        return None
    return round(fraction * 100.0, 2)


def _unix_seconds(headers: Mapping[str, str], key: str) -> Optional[int]:
    raw = headers.get(key)
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse_cc_quota_headers(headers: Optional[Mapping[str, str]]) -> Optional[Dict[str, object]]:
    """Extract weekly/5h quota from ``anthropic-ratelimit-unified-*`` headers.

    Returns None when the 7-day utilization -- the metric the menu bar exists
    to show -- is absent or unparseable, so a malformed response degrades to
    ``unknown`` rather than to a fabricated 0.
    """
    if not isinstance(headers, Mapping):
        return None
    # Header names are case-insensitive per RFC 9110; normalise before lookup.
    normalized = {
        str(key).lower(): value
        for key, value in headers.items()
        if str(key).lower().startswith(CC_QUOTA_HEADER_PREFIX)
    }
    used_percent = _utilization_percent(normalized, f"{CC_QUOTA_HEADER_PREFIX}-7d-utilization")
    if used_percent is None:
        return None
    return {
        "used_percent": used_percent,
        "resets_at": _unix_seconds(normalized, f"{CC_QUOTA_HEADER_PREFIX}-7d-reset"),
        "five_hour_used_percent": _utilization_percent(
            normalized, f"{CC_QUOTA_HEADER_PREFIX}-5h-utilization"
        ),
        "five_hour_resets_at": _unix_seconds(normalized, f"{CC_QUOTA_HEADER_PREFIX}-5h-reset"),
    }


def fetch_cc_quota_headers(
    *,
    token: Optional[str] = None,
    timeout: int = CC_QUOTA_TIMEOUT_SECONDS,
) -> Optional[Dict[str, str]]:
    """Issue the single minimal API call and return only its rate-limit headers.

    The response body is never read. The token is confined to this function's
    request headers; only ``anthropic-ratelimit-unified-*`` values are returned,
    so no credential can reach the caller. Every failure mode -- missing
    credentials, 401, timeout, DNS -- yields None.
    """
    if token is None:
        token = read_cc_oauth_token()
    if not token:
        return None
    payload = json.dumps(
        {
            "model": CC_QUOTA_MODEL,
            "max_tokens": CC_QUOTA_MAX_TOKENS,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed https endpoint
        CC_QUOTA_URL,
        data=payload,
        headers={
            "authorization": f"Bearer {token}",
            "anthropic-version": CC_QUOTA_ANTHROPIC_VERSION,
            "anthropic-beta": CC_QUOTA_OAUTH_BETA,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _rate_limit_headers(response.headers)
    except urllib.error.HTTPError as error:
        # A 429 still carries the quota headers, which is exactly the state the
        # menu bar most needs to show; 401/403 carry none and degrade to None.
        try:
            return _rate_limit_headers(error.headers) or None
        except Exception:  # noqa: BLE001 - never propagate from a quota probe
            return None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _rate_limit_headers(headers: object) -> Dict[str, str]:
    """Copy out only the rate-limit headers, dropping everything else."""
    items = getattr(headers, "items", None)
    if not callable(items):
        return {}
    return {
        str(key).lower(): str(value)
        for key, value in items()
        if str(key).lower().startswith(CC_QUOTA_HEADER_PREFIX)
    }


def collect_cc_quota(fetch=None) -> Optional[Dict[str, object]]:
    """CC weekly quota, or None when it cannot be observed."""
    fetcher = fetch if fetch is not None else fetch_cc_quota_headers
    try:
        headers = fetcher()
    except Exception:  # noqa: BLE001 - a quota probe must never break the snapshot
        return None
    return parse_cc_quota_headers(headers)


# --------------------------------------------------------------------------
# §3.2 usage (~/.relay/obs/*.jsonl)
# --------------------------------------------------------------------------


# 固定订阅月费，用于「这个月的钱花在谁身上」的分摊呈现。
#
# 2026-07-30 这块被一版更严格的 billing 实现替换掉了：新判据要求 turn.end 显式
# 携带 billing_component_id，obs 里没有这个字段，于是归因永远停在「证据不足」，
# 而旧的、能跑出数的实现被一并删除（0ef3260）。大鸟 07-31 指出：「原版本呈现的
# 是每个 Agent 耗费了多少 Token，新版本完全丢弃了之前的功能，而且没有新东西
# 替代」——完全属实，我复核那批提交时没发现，因为新实现的「证据不足」在它自己
# 的测试里就是预期行为，全绿。
#
# 两套并存，各答各的问题：billing.mtd.* 说「$270 由哪些组件构成」并精确守恒到
# 分；usage.mtd.* 说「输出 token 花在谁身上」。后者按输出占比分摊，是估算不是
# 账单归因——文案上必须说清，不能让读者当成后者。
MONTHLY_SUBSCRIPTION_USD = 270.0


def collect_month_to_date(obs_dir: Path, now: datetime) -> Optional[Dict[str, object]]:
    """本月累计用量，按 agent 分摊订阅费(大鸟 2026-07-29)。

    **摊销的对象是 agent，不是天。** 面板原先报「今天摊销 $8.71」——那是
    $270 除以当月天数，一个每天都一样的常数，大鸟原话「你这个数字没用」。
    它不随任何事情变化，看一百天都是同一个数，构不成任何判断。

    真正要回答的是「这笔钱花在谁身上」。分摊基准取 **输出 token**：

    * 输入侧绝大部分是缓存复读(实测占 99%+)，按它分摊等于按「谁的上下文长」
      分摊，而上下文长度更多反映会话形态不反映工作量；
    * 输出是模型真正生成的内容，也是各家 API 定价里最贵的一档，最接近
      「这个 agent 消耗了多少算力」。

    月累计跨多天，所以按**北京时区的自然月**扫 obs 目录下所有相关日期文件。
    `obs_dir` 里每天一个 `*-YYYY-MM-DD.jsonl`，只读文件名落在本月的。

    返回 ``None`` 表示目录读不到。``days_covered`` 如实报覆盖天数——obs 文件
    可能缺失(轮转/清理/机器换过)，报「本月累计」却只覆盖 15 天而不说，读者
    会以为那是整月的数。
    """
    if not obs_dir.is_dir():
        return None

    local_now = now.astimezone(DISPLAY_TZ)
    month_prefix = local_now.strftime("%Y-%m")

    by_agent: Dict[str, int] = {}
    total = 0
    days: set = set()

    for path in sorted(obs_dir.glob(f"*{month_prefix}-*.jsonl")):
        # 文件名尾部就是日期，用它当天数计数的键（比逐行解析 ts 快得多，
        # 且这里只需要「覆盖了几天」这个粗粒度事实）。
        stem_date = path.stem[-10:]
        seen_row = False
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                if event.get("evt") != "turn.end":
                    continue
                out = event.get("outputTokens")
                if not isinstance(out, int) or isinstance(out, bool):
                    continue
                agent = str(event.get("agent") or "unknown")
                by_agent[agent] = by_agent.get(agent, 0) + out
                total += out
                seen_row = True
        if seen_row:
            days.add(stem_date)

    # 分摊：每个 agent 占总输出的比例 × 月订阅额。总量为 0 时不分摊——
    # 除零会得 NaN，而「这个月还没人干活」的正确呈现是零而不是「未知」。
    rows: List[Dict[str, object]] = []
    for agent, tokens in by_agent.items():
        share = (tokens / total) if total else 0.0
        rows.append({
            "agent": agent,
            "output_tokens": tokens,
            "share": share,
            "amortized_usd": round(MONTHLY_SUBSCRIPTION_USD * share, 2),
        })
    rows.sort(key=lambda r: (-int(r["output_tokens"]), str(r["agent"])))

    return {
        "month": month_prefix,
        "days_covered": len(days),
        "total_output_tokens": total,
        "subscription_usd": MONTHLY_SUBSCRIPTION_USD,
        "by_agent": rows,
    }


#: `relay-request-trace.ts` 的三类埋点。事件名与函数名**不同**（函数叫
#: `traceTurnEnd`，事件名是 `turn.end`），2026-08-02 我按函数名 grep 成
#: `req.turn_end` 一无所获，差点判「只有一类在跑」。以真实事件名为准。
REQUEST_TRACE_EVENTS = ("req.inbound", "turn.end", "delivery.receipt")


def collect_request_trace(obs_dir: Path, now: datetime) -> Optional[Dict[str, object]]:
    """`s1.request_trace_capture` 的活性证据——三类埋点当天各落了多少条。

    2026-08-02 查出的 spec-code drift：baseline SPEC §143 判这项
    `active_validated`（取证「07-15 canary；relay-request-trace.ts；07-16~22
    live 事件计数」），exporter 却把它写死在 `_add_baseline_gaps` 里，值是
    `active_partial` + freshness `unknown`、理由 "Trace source is not yet
    adapted"。那句话在 07-15 之后就不再成立，但没有任何闸会红，于是它一直
    留在那儿——**单这一项就把 overall coverage 从 partial 短路成 unknown**
    （`producer.reduce_coverage` 第 51 行：任一项 freshness unknown 即整体
    unknown）。

    实测（08-02）：近 7 天 req.inbound 884 / turn.end 886 / delivery.receipt
    1783，当天仍在写。所以 SPEC 是对的、代码是陈的，修法是**真去读这个源**，
    而不是把硬编码字符串改成好看的值——后者只是把 drift 换个方向。

    返回 ``None`` 表示 obs 目录读不到（未知 ≠ 没有）。读得到但当天零事件时
    返回计数全 0 的 dict，由调用方判 stale——「读得到且确实没有」和「读不到」
    必须分开，这是 SPEC §322「缺值不得 fallback 为 0 或 ok」的要求。
    """
    if not obs_dir.is_dir():
        return None
    local_day = now.astimezone(DISPLAY_TZ).date()
    local_start = datetime(
        local_day.year, local_day.month, local_day.day, tzinfo=DISPLAY_TZ
    )
    utc_dates = {
        local_start.astimezone(timezone.utc).date(),
        (local_start + timedelta(days=1) - timedelta(microseconds=1))
        .astimezone(timezone.utc)
        .date(),
    }
    paths: List[Path] = []
    for utc_date in sorted(utc_dates):
        paths.extend(sorted(obs_dir.glob(f"*{utc_date.isoformat()}.jsonl")))
    if not paths:
        return None

    counts: Dict[str, int] = {name: 0 for name in REQUEST_TRACE_EVENTS}
    latest: Optional[datetime] = None
    for path in paths:
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                name = event.get("evt")
                if name not in counts:
                    continue
                stamp = _parse_ts(event.get("ts"))
                if stamp is None or stamp.astimezone(DISPLAY_TZ).date() != local_day:
                    continue
                counts[str(name)] += 1
                if latest is None or stamp > latest:
                    latest = stamp
    return {
        "counts": counts,
        "total": sum(counts.values()),
        # 三类齐全才算这条链路完整——只有入站没有投递回执,说明链路断在中间。
        "kinds_present": sum(1 for value in counts.values() if value > 0),
        "kinds_expected": len(REQUEST_TRACE_EVENTS),
        "latest": latest,
    }


def collect_usage(obs_dir: Path, now: datetime) -> Optional[Dict[str, object]]:
    """Aggregate the current Beijing calendar day's obs events.

    A Beijing day spans two UTC dates, so both date-suffixed source-file sets
    are candidates. Event timestamps remain authoritative: each parsed event
    is converted to ``DISPLAY_TZ`` and filtered to the requested local day.
    Unparseable rows have no event-day attribution and are therefore counted as
    source-quality evidence for the candidate files, not as dated events.
    Returns None when neither candidate source is readable, which is distinct
    from a readable-but-empty day (all-zero counters).
    """
    if not obs_dir.is_dir():
        return None
    local_day = now.astimezone(DISPLAY_TZ).date()
    local_start = datetime(
        local_day.year, local_day.month, local_day.day, tzinfo=DISPLAY_TZ
    )
    utc_dates = {
        local_start.astimezone(timezone.utc).date(),
        (local_start + timedelta(days=1) - timedelta(microseconds=1))
        .astimezone(timezone.utc)
        .date(),
    }
    paths: List[Path] = []
    seen_paths: set[Path] = set()
    for utc_date in sorted(utc_dates):
        for path in sorted(obs_dir.glob(f"*{utc_date.isoformat()}.jsonl")):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            paths.append(path)
    if not paths:
        return None

    input_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    output_tokens = 0
    turns = 0
    failures = 0
    inbound = 0
    delivery_sent = 0
    delivery_total = 0
    attempts_hist: Dict[str, int] = {}
    inbound_types: Dict[str, int] = {}
    degen = 0
    malformed = 0
    durations: List[float] = []
    hourly = [0] * 24
    by_agent: Dict[str, Dict[str, object]] = {}
    by_session: Dict[Tuple[str, str], Dict[str, object]] = {}
    read_any = False

    for path in paths:
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        read_any = True
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    malformed += 1
                    continue
                if not isinstance(event, Mapping):
                    malformed += 1
                    continue
                occurred = None
                for timestamp_key in ("ts", "occurred_at", "timestamp"):
                    occurred = _parse_ts(event.get(timestamp_key))
                    if occurred is not None:
                        break
                if occurred is None:
                    continue
                local_occurred = occurred.astimezone(DISPLAY_TZ)
                if local_occurred.date() != local_day:
                    continue
                kind = event.get("evt")

                if kind == "turn.end":
                    turns += 1
                    agent = str(event.get("agent") or "unknown")
                    session = str(event.get("session") or "unknown")
                    def _count(key: str) -> int:
                        raw = event.get(key)
                        return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0

                    # 真实输入 = 裸 input + 缓存读 + 缓存写。缓存两项是 2026-07-27 才补的埋点,
                    # 之前的历史事件没有这两个字段 → 缺失记 0,老数据仍只反映裸 input(会偏低),
                    # 这是已知的历史断层,不是本次口径错误。
                    tokens_in_raw = _count("inputTokens")
                    tokens_cache_read = _count("cacheReadTokens")
                    tokens_cache_creation = _count("cacheCreationTokens")
                    tokens_in = tokens_in_raw + tokens_cache_read + tokens_cache_creation
                    tokens_out = _count("outputTokens")
                    input_tokens += tokens_in_raw
                    cache_read_tokens += tokens_cache_read
                    cache_creation_tokens += tokens_cache_creation
                    output_tokens += tokens_out
                    hourly[local_occurred.hour] += tokens_out
                    if event.get("isError") is True:
                        failures += 1
                    duration = event.get("durationMs")
                    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                        durations.append(float(duration))

                    agent_row = by_agent.setdefault(
                        agent, {"agent": agent, "output_tokens": 0, "input_tokens": 0, "turns": 0, "sessions": set()}
                    )
                    agent_row["output_tokens"] += tokens_out
                    agent_row["input_tokens"] += tokens_in
                    agent_row["turns"] += 1
                    agent_row["sessions"].add(session)

                    session_row = by_session.setdefault(
                        (agent, session),
                        {
                            "agent": agent,
                            "session_prefix": session[:SESSION_ID_PREFIX_CHARS],
                            "output_tokens": 0,
                            "turns": 0,
                            "last_activity": occurred,
                        },
                    )
                    session_row["output_tokens"] += tokens_out
                    session_row["turns"] += 1
                    if occurred > session_row["last_activity"]:
                        session_row["last_activity"] = occurred

                elif kind == "req.inbound":
                    inbound += 1
                    # Only the message type is counted; `who` (a Feishu open_id)
                    # is deliberately never carried into the snapshot (spec §6).
                    msg_type = str(event.get("msgType") or "unknown")
                    inbound_types[msg_type] = inbound_types.get(msg_type, 0) + 1

                elif kind == "delivery.receipt":
                    delivery_total += 1
                    if event.get("status") == "sent":
                        delivery_sent += 1
                    attempts = event.get("attempts")
                    if isinstance(attempts, int) and not isinstance(attempts, bool):
                        key = str(attempts)
                        attempts_hist[key] = attempts_hist.get(key, 0) + 1

                elif kind == "degen.observed":
                    degen += 1

    if not read_any:
        return None

    agents = sorted(
        (
            {
                "agent": row["agent"],
                "output_tokens": row["output_tokens"],
                "input_tokens": row["input_tokens"],
                "turns": row["turns"],
                "session_count": len(row["sessions"]),
            }
            for row in by_agent.values()
        ),
        key=lambda row: (-row["output_tokens"], row["agent"]),
    )[:USAGE_TOP_AGENTS]

    sessions = sorted(
        (
            {
                "agent": row["agent"],
                "session_prefix": row["session_prefix"],
                "output_tokens": row["output_tokens"],
                "turns": row["turns"],
                "last_activity": row["last_activity"].isoformat(),
            }
            for row in by_session.values()
        ),
        key=lambda row: (-row["output_tokens"], row["agent"], row["session_prefix"]),
    )[:USAGE_TOP_SESSIONS]

    return {
        # input_tokens 是**总输入**（裸 + 缓存读 + 缓存写）——面板要回答的是"吃进去多少",
        # 裸值单独留在 input_tokens_uncached 供排查缓存命中率。
        "input_tokens": input_tokens + cache_read_tokens + cache_creation_tokens,
        "input_tokens_uncached": input_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "output_tokens": output_tokens,
        "turns": turns,
        "failures": failures,
        "inbound": inbound,
        "degen_observed": degen,
        "duration_p50_ms": int(statistics.median(durations)) if durations else None,
        "hourly_output_tokens": hourly,
        "by_agent": agents,
        "by_session": sessions,
        "delivery_sent": delivery_sent,
        "delivery_total": delivery_total,
        "delivery_attempts_histogram": dict(sorted(attempts_hist.items(), key=lambda kv: int(kv[0]))),
        "inbound_types": dict(sorted(inbound_types.items())),
        "malformed_lines": malformed,
    }


# --------------------------------------------------------------------------
# §3.3 harness
# --------------------------------------------------------------------------


def _load_yaml(path: Path) -> Optional[Mapping[str, object]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            loaded = yaml.load(handle, Loader=_YamlLoader)
    except (OSError, yaml.YAMLError, UnicodeDecodeError, ValueError):
        return None
    return loaded if isinstance(loaded, Mapping) else None


def collect_subject_closures(work_items_dir: Path, handoffs_dir: Path) -> Optional[Dict[str, datetime]]:
    """When each wi/handoff reached a terminal state — recovery evidence by id.

    An alert whose subject is a wi/ho keeps firing until something proves the
    thing is no longer stuck. The other recovery routes (probe heartbeat,
    systemd exit, the agent's next success) cannot speak for a *specific*
    ticket, so a wi that was simply closed out had no way to clear its own
    alert and kept demanding action after the work was done.

    Returns ``None`` when neither directory is readable, so the caller can tell
    "no evidence available" (fall back to the age rule) from "read it, nothing
    is closed" — the same unknown-vs-zero distinction the rest of the exporter
    keeps.
    """
    if not work_items_dir.is_dir() and not handoffs_dir.is_dir():
        return None

    closures: Dict[str, datetime] = {}

    if work_items_dir.is_dir():
        for path in sorted(work_items_dir.glob("*.yaml")):
            item = _load_yaml(path)
            if item is None:
                continue
            if item.get("status") in WI_OPEN_STATUSES:
                continue
            closed = _parse_ts(item.get("updated_at"))
            if closed is not None:
                closures[str(item.get("work_item_id") or path.stem)] = closed

    if handoffs_dir.is_dir():
        for path in sorted(handoffs_dir.glob("*.yaml")):
            item = _load_yaml(path)
            if item is None:
                continue
            if item.get("ack_state") not in HANDOFF_TERMINAL_STATES:
                continue
            # Handoffs carry no updated_at; created_at is the only stamp, and it
            # is necessarily <= the moment it was closed. Using it can only make
            # recovery look *older* than it was — i.e. it may fail to clear an
            # alert, never clear one prematurely. That asymmetry is the safe one.
            closed = _parse_ts(item.get("created_at"))
            if closed is not None:
                closures[str(item.get("handoff_id") or path.stem)] = closed

    return closures


def collect_main_only_pending(
    handoffs_dir: Path, now: datetime, work_items_dir: Optional[Path] = None,
    *, coverage_errors: Optional[List[str]] = None
) -> Optional[List[Dict[str, object]]]:
    """未闭环的 `main_only` handoff —— 「要你出手」的真源。

    大鸟 2026-07-28 定：**`main_only: true` 就必须大鸟参与，无论 wi 属于谁。**

    在此之前这个数字取自 escalation 事件的标签（`red_line_flag` /
    `circuit_broken` 那些），报出来的全是「某个 Agent 卡住了」——大鸟原话
    「都不是我说的真需要大鸟关注的 wi，都是你该做的」。两套东西：标签说的是
    分身不敢往下走（正确出路多半是 Pulse 接过来做或走 Duke 裁），`main_only`
    说的是**这单发起时就声明了「必须人来判断」**，分身按设计不能代答。

    所以判据不看标签、不看严重程度、不看票据挂在谁名下——只看这一个字段。
    典型：`ho_20260724_903`（aki→hestia 主身，问 Oikos 默认浏览器基准该不该
    定 Firefox）挂了 3 天，正是大鸟点名「这条是真的」的那件。

    返回 ``None`` 表示目录读不到（未知 ≠ 没有）；空列表表示读到了且确实没有。

    ## 「大意」取自 wi title，不取 expected_output（2026-07-28 大鸟定）

    面板原先直接铺 `expected_output`，大鸟原话「实在是不容易理解，且没有用」。
    根因不在排版：`expected_output` 是**发起方写给收方 agent 的验收规格**，
    典型如 ho_20260727_374 的「①逐条认或驳（驳要附证据，不接受「已知悉」式
    回复）；②自诊根因——按存活性三分…」——四段式、面向执行，压成一行只会
    被截断成省略号，读者仍不知道这是件什么事。

    每张 handoff 都带 `work_item_id`，而 wi 的 `title` 恰恰是人手写的一句话
    主旨（「S1 成果符合性裁决：duke 7月 pass_with_findings/major——请自诊并出
    整改方向」）。那正是大鸟要的「大意」列，已经存在，不必新增字段、不必让
    机器去总结长文（机器摘要会替他做判断，摘错他看不出来）。

    读不到 wi（文件缺失/坏 YAML/无 work_item_id）时 `gist` 为空串，由渲染侧
    退回 `expected_output`——宁可显示难读的真话，不显示编出来的好话。
    """
    errors = coverage_errors if coverage_errors is not None else []
    def entries(directory: Path, label: str) -> Optional[List[Path]]:
        try:
            return sorted(path for path in directory.iterdir() if path.suffix == ".yaml" and path.is_file())
        except OSError:
            errors.append(label + "_unreadable")
            return None
    handoff_paths = entries(handoffs_dir, "handoffs")
    if handoff_paths is None:
        return None

    # wi 既供「大意」(title),又供闭环判据(status);一次性读进来,避免每条
    # handoff 再扫一遍目录。`items` 另留整份 payload——裸 main_only wi
    # (无 handoff)要靠它自己成行,见下方 _bare_main_only_rows。
    titles: Dict[str, str] = {}
    statuses: Dict[str, str] = {}
    items: Dict[str, Mapping] = {}
    if work_items_dir is not None:
        for path in entries(work_items_dir, "work_items") or []:
            item = _load_yaml(path)
            if not isinstance(item, Mapping):
                errors.append("work_item_unreadable")
                continue
            wi_key = str(item.get("work_item_id") or path.stem)
            items[wi_key] = item
            title = item.get("title")
            if isinstance(title, str) and title.strip():
                titles[wi_key] = title.strip()
            status = item.get("status")
            if isinstance(status, str) and status.strip():
                statuses[wi_key] = status.strip()

    hits: List[Dict[str, object]] = []
    # 哪些 wi 已经有 handoff 载着——**不论那张单是否仍开着**。已闭环的
    # wi 不该因为「它没有在飞的 handoff」而被裸 wi 通道二次捞出来。
    covered: set = set()
    for path in handoff_paths:
        item = _load_yaml(path)
        if not isinstance(item, Mapping):
            errors.append("handoff_unreadable")
            continue
        wi_id = item.get("work_item_id")
        if wi_id:
            covered.add(str(wi_id))
        if not main_only_still_open(item, statuses.get(str(wi_id))):
            continue
        created = _parse_ts(item.get("created_at"))
        hits.append({
            "id": str(item.get("handoff_id") or path.stem),
            "from_owner": str(item.get("from_owner") or "?"),
            "to_owner": str(item.get("to_owner") or "?"),
            "ack_state": str(item.get("ack_state") or "?"),
            # 大意：wi title 优先，取不到才退回 expected_output（见 docstring）。
            "gist": first_line(titles.get(str(wi_id), "")),
            "asks": first_line(item.get("expected_output")),
            "days": (now - created).days if created else None,
            "_wi": str(wi_id) if wi_id else "",
            "_created": created,
        })
    hits.extend(_bare_main_only_rows(items, covered, now))
    rows = _fold_by_work_item(hits)
    # 久的在前：等最久的最该先看。
    rows.sort(key=lambda r: -(r["days"] or 0))
    return rows


def _bare_main_only_rows(
    items: Mapping[str, Mapping], covered: AbstractSet[str], now: datetime
) -> List[Dict[str, object]]:
    """`main_only: true` 却一张 handoff 都没有的 wi —— 第二类真源。

    2026-08-05 大鸟报「wi_20260804_851 定了主身接活,却没出现在要你出手」。
    查下来判据没错、`draft` 也在 `WI_OPEN_STATUSES` 里——**是取数范围够不着**：
    采集器只扫 `handoffs/`,而这张 wi 的 `main_only` 写在 wi 文件上,名下零
    handoff,于是对面板永不可见。当日同型两张(另一张 wi_20260804_827),
    都是 08-04 新建、都隐身。

    这是同一族的第三个形态。前两次修的是**判据**(07-29 `ack_state` 被分身
    改→改看 wi 终态;08-02 每轮往返都成行→按 wi 折叠),这次修的是**载体**:
    `main_only` 的真源有两处,而采集器只认其中一处。判据本身可直接复用——
    `main_only_still_open` 传 wi 自己的 status 即可,语义完全一致。

    与 handoff 行去重靠 `covered`(**任何** handoff 提到过这个 wi 就算已覆盖,
    不论那张单开着还是终态),避免同一件事出两张卡。

    没有 handoff 也就没有 `from_owner`/`ack_state`/`expected_output`:
    * 两端 owner 都取 wi 的 `owner`——事实是「这单挂在他名下、还没发出去」,
      渲染成 `ken→ken` 不如直说,故 `from_owner` 留 `owner`、`to_owner` 同值,
      由渲染侧识别同值时改口径;
    * `asks` 留空——**不编**。wi 的 `acceptance_criteria` 是给执行方的验收
      规格,压成一行正是 07-28 大鸟说「不容易理解且没有用」的那种;
    * `id` 用 wi 号本身——要动手就去开这张 wi,没有 handoff 可开。
    """
    rows: List[Dict[str, object]] = []
    for wi_key, item in items.items():
        if wi_key in covered:
            continue
        status = item.get("status")
        status = status.strip() if isinstance(status, str) and status.strip() else None
        if not main_only_still_open(item, status):
            continue
        created = _parse_ts(item.get("created_at"))
        owner = str(item.get("owner") or "?")
        rows.append({
            "id": wi_key,
            "from_owner": owner,
            "to_owner": owner,
            "ack_state": "no_handoff",
            "gist": first_line(item.get("title")),
            "asks": "",
            "days": (now - created).days if created else None,
            "_wi": wi_key,
            "_created": created,
        })
    return rows


def _fold_by_work_item(hits: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """把同一件事的多张 handoff 收成一行。「一行 = 一件要大鸟出手的事」。

    2026-08-02 大鸟：「都是些重复 wi……重复且无用」。实测当日 8 行里 7 行
    `gist` 一字不差，全挂 `wi_20260729_419`（aki↔ken 同一件事，光当天就往返
    5 轮，每轮都是 `accepted`）。

    根因不在语义而在**基数**：`main_only` 判得对（分身不能代答＝要大鸟在场），
    07-29 定的「闭环看 wi 终态、不看 ack_state」也对（分身 ack 一下就把卡片
    藏了，那是真缺陷）。但两条对的规则叠起来＝**wi 开着期间，它名下每张单
    都常亮且只增不减**，于是面板数的是对话轮次，不是待办件数。

    折叠取值：
    * `days` 取**最早**一张——「这件事挂了多久」，不因为刚回了一句就归零；
    * `asks` / `id` / 两端 owner 取**最新**一张——要动手就从最新那张进；
    * `rounds` = 这件事名下的单数，`1` 表示没往返过。

    催单（`pulse→ken` 那种「给个去向」）不单独成行：它催的就是同一件事，
    并进去只多算一轮（2026-08-02 大鸟未特别指定，按默认处理）。

    `_wi` 为空（handoff 无 `work_item_id`）时**不折叠**，各自成行——判不出
    是不是同一件事就别合，宁可多一行也不合错。
    """
    folded: Dict[str, Dict[str, object]] = {}
    out: List[Dict[str, object]] = []
    for hit in hits:
        wi_key = str(hit.get("_wi") or "")
        if not wi_key:
            out.append(_strip_internals(hit, rounds=1))
            continue
        group = folded.get(wi_key)
        if group is None:
            folded[wi_key] = dict(hit, rounds=1)
            continue
        group["rounds"] = int(group["rounds"]) + 1
        # 最早的那张决定「挂了多久」。created 读不出时保守留住已有值。
        earliest, current = group.get("_created"), hit.get("_created")
        if earliest is None or (current is not None and current < earliest):
            group["days"] = hit["days"]
            group["_created"] = current
        # 最新的那张决定「现在要动什么」。
        latest = group.get("_latest") or group.get("_created")
        if current is not None and (latest is None or current >= latest):
            group["_latest"] = current
            for field in ("id", "from_owner", "to_owner", "ack_state", "asks"):
                group[field] = hit[field]
        # gist 同源于 wi title，组内本就一致；空串时用后来非空的补上。
        if not group.get("gist") and hit.get("gist"):
            group["gist"] = hit["gist"]
    out.extend(_strip_internals(g, rounds=int(g["rounds"])) for g in folded.values())
    return out


def _strip_internals(row: Mapping, rounds: int) -> Dict[str, object]:
    """去掉 `_` 前缀的折叠中间量，只留渲染侧读的字段。"""
    projected = {
        key: value for key, value in row.items() if not key.startswith("_")
    } | {"rounds": rounds}
    projected["business_subject"] = str(row.get("_wi") or row.get("id") or "")
    return projected


def first_line(value: object, limit: int = 110) -> str:
    """把 expected_output 压成一行，供面板直接显示。"""
    if not isinstance(value, str):
        return ""
    collapsed = " ".join(value.split())
    return collapsed[: limit - 1] + "…" if len(collapsed) > limit else collapsed


def _handoff_receivers(item: Mapping) -> Optional[Tuple[str, ...]]:
    """Return every schema-valid receiver, preserving fan-out send order.

    Harness serialises fan-out as ``to_owners`` plus a legacy ``to_owner``
    projection of the first receiver.  A reader that takes only ``to_owner``
    silently drops the other action holders.  Conversely, conflicting,
    duplicated or non-string receiver fields are not safe enough to display as
    current truth, so the whole handoff is ignored and the caller falls back to
    stronger/provable evidence.
    """
    multi = item.get("to_owners")
    if multi is not None:
        if not isinstance(multi, list) or not multi:
            return None
        receivers: List[str] = []
        seen: set = set()
        for value in multi:
            if not isinstance(value, str) or not value.strip():
                return None
            receiver = value.strip()
            if receiver in seen:
                return None
            receivers.append(receiver)
            seen.add(receiver)
        primary = item.get("to_owner")
        if primary is not None:
            if not isinstance(primary, str) or primary.strip() != receivers[0]:
                return None
        return tuple(receivers)

    single = item.get("to_owner")
    if not isinstance(single, str) or not single.strip():
        return None
    return (single.strip(),)


def _current_handoff_receivers(
    handoffs_dir: Path,
) -> Tuple[set, Dict[str, Tuple[str, ...]]]:
    """Read escalation ids and the provable current action receivers in one pass.

    Only ``pending`` (waiting for the receiver to accept) and ``accepted``
    (receiver accepted and is executing) carry live action rights.  Rejected,
    archived and timed-out attempts do not erase an earlier live handoff.

    ``created_at`` is the only handoff timestamp stored in the current YAML
    schema.  The newest valid timestamp wins.  An undated/invalid-time live
    handoff cannot prove that it supersedes that group, but its action right
    also cannot be disproved, so it remains an explicit parallel candidate.
    If multiple live handoffs share the newest timestamp, none is provably
    later: merge all their receivers in deterministic handoff-id order instead
    of guessing or dropping a fan-out member.
    """
    escalated: set = set()
    latest: Dict[
        str,
        Tuple[datetime, List[Tuple[str, Tuple[str, ...]]]],
    ] = {}
    undated: Dict[str, List[Tuple[str, Tuple[str, ...]]]] = {}
    if not handoffs_dir.is_dir():
        return escalated, {}

    for path in sorted(handoffs_dir.glob("*.yaml")):
        item = _load_yaml(path)
        if not isinstance(item, Mapping):
            continue
        wi_id = str(item.get("work_item_id") or "")
        if main_only_still_open(item, None):
            escalated.add(wi_id)
        if not wi_id or item.get("ack_state") not in HANDOFF_ACTION_STATES:
            continue
        created = _parse_ts(item.get("created_at"))
        receivers = _handoff_receivers(item)
        if receivers is None:
            continue
        handoff_id = str(item.get("handoff_id") or path.stem)
        candidate = (handoff_id, receivers)
        if created is None:
            undated.setdefault(wi_id, []).append(candidate)
            continue
        current = latest.get(wi_id)
        if current is None or created > current[0]:
            latest[wi_id] = (created, [candidate])
        elif created == current[0]:
            current[1].append(candidate)

    resolved: Dict[str, Tuple[str, ...]] = {}
    for wi_id in set(latest) | set(undated):
        timed = latest.get(wi_id)
        candidates = list(timed[1]) if timed is not None else []
        candidates.extend(undated.get(wi_id, []))
        receivers: List[str] = []
        seen: set = set()
        for _, names in sorted(candidates, key=lambda pair: pair[0]):
            for name in names:
                if name not in seen:
                    receivers.append(name)
                    seen.add(name)
        if receivers:
            resolved[wi_id] = tuple(receivers)
    return escalated, resolved


def _agents_with_stale_sweep(policies_dir: Path) -> Optional[AbstractSet[str]]:
    """哪些 agent 开了 `stale_work_items` 兜底扫描（spec §6.7.1）。

    这是判 `unowned` 的唯一依据。harness 的 dispatcher 是 **handoff 驱动**的
    （`scan.py::list_stale_work_items_for` docstring: "it only ever wakes on a
    PENDING handoff"）——一件 wi 的 handoff 全终态后，唯一还会主动碰它的就是
    这个兜底扫描，而扫描按 agent opt-in：policy 里没声明这个 scan_item，
    `_resolve_stale_wi_age_days` 返回 None，整个采集器不跑。

    返回 ``None`` 表示目录读不到——调用方必须据此**降级为观察中而非误报
    「没人管」**（读不到 ≠ 没开）。
    """
    try:
        if not policies_dir.is_dir():
            return None
        # glob can hide scandir permission errors as an empty result. An
        # unreadable policy directory is unknown coverage, not no sweep owners.
        paths = sorted(path for path in policies_dir.iterdir()
                       if path.suffix == ".yaml" and path.is_file())
    except OSError:
        return None
    opted_in: set[str] = set()
    for path in paths:
        policy = _load_yaml(path)
        if not isinstance(policy, Mapping):
            return None
        scan_items = policy.get("scan_items")
        if not isinstance(scan_items, Sequence):
            continue
        for item in scan_items:
            if isinstance(item, Mapping) and item.get("type") == "stale_work_items":
                opted_in.add(path.stem)
                break
    return opted_in


def _real_activity_from_events(
    events_dir: Path, handoff_to_wi: Mapping[str, str]
) -> Tuple[Dict[str, datetime], Dict[str, datetime], AbstractSet[str]]:
    """从事件流读三样**机制产生**的信号（spec §6.7.1 第一原则）。

    返回 ``(每个 agent 最近真开工, 每件 wi 最近真动静, 熔断的 wi 集合)``。

    为什么不能用 `active_executor` 代替：那是 ack 时写入、干完不清的字段，
    记的是「谁曾经接过」。2026-08-07 实测 6 件非空的里 5 件持有者已数日未开工。

    为什么必须排除 ``skipped_*``：dispatcher 每小时扫一次，扫到不处置也记一条
    `dispatch_event`。实测 `wi_20260725_586` 连续 6 小时每小时一条
    `skipped_autonomous_covered`，而该 wi 实际六天无人推进——**不过滤就会让被
    反复跳过的 wi 看起来最活跃**。

    wi 的归属只认 disposition 的 ``ref_id`` 正式字段，**不正则扫全文**：全文里
    出现一个 wi 号可能只是「提到」，据此判活跃会系统性偏乐观。``ref_id`` 多数
    形如 ``ho_*``，经 ``handoff_to_wi`` 映射回 wi。
    """
    last_work: Dict[str, datetime] = {}
    last_touch: Dict[str, datetime] = {}
    circuit_broken: set[str] = set()
    if not events_dir.is_dir():
        return last_work, last_touch, circuit_broken

    def _touch(wi_id: str, moment: datetime) -> None:
        if wi_id not in last_touch or moment > last_touch[wi_id]:
            last_touch[wi_id] = moment

    for path in sorted(events_dir.glob("*.jsonl")):
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                moment = _parse_ts(event.get("occurred_at"))
                if moment is None:
                    continue
                event_type = event.get("event_type")
                agent_id = event.get("agent_id")

                # 真开工：只认 autonomous_execution_event（它不带 wi 归属，
                # 只回答「这个 agent 还在不在干活」）。
                if event_type == "autonomous_execution_event" and isinstance(agent_id, str):
                    if agent_id not in last_work or moment > last_work[agent_id]:
                        last_work[agent_id] = moment

                raw_dispositions = event.get("dispositions")
                dispositions = [
                    d for d in (raw_dispositions if isinstance(raw_dispositions, Sequence)
                                and not isinstance(raw_dispositions, (str, bytes)) else ())
                    if isinstance(d, Mapping)
                ]
                for disposition in dispositions:
                    ref_id = disposition.get("ref_id")
                    if not isinstance(ref_id, str):
                        continue
                    wi_id = ref_id if ref_id.startswith("wi_") else handoff_to_wi.get(ref_id)
                    if not wi_id:
                        continue
                    outcome = str(disposition.get("outcome", ""))
                    if not outcome.startswith("skipped"):
                        _touch(wi_id, moment)
                    if outcome == "circuit_broken":
                        circuit_broken.add(wi_id)
    return last_work, last_touch, circuit_broken


def _pending_handoffs_by_wi(
    handoffs_dir: Path,
) -> Tuple[Dict[str, int], Dict[str, str]]:
    """``({wi: pending 张数}, {handoff_id: wi})``。

    第二张表让事件流的 ``ref_id``（多为 ``ho_*``）能映射回 wi。
    """
    pending: Dict[str, int] = {}
    ho_to_wi: Dict[str, str] = {}
    if not handoffs_dir.is_dir():
        return pending, ho_to_wi
    for path in sorted(handoffs_dir.glob("*.yaml")):
        record = _load_yaml(path)
        if not isinstance(record, Mapping):
            continue
        work_item_id = record.get("work_item_id")
        if not isinstance(work_item_id, str) or not work_item_id:
            continue
        ho_to_wi[str(record.get("handoff_id") or path.stem)] = work_item_id
        if record.get("ack_state") == "pending":
            pending[work_item_id] = pending.get(work_item_id, 0) + 1
    return pending, ho_to_wi


def collect_in_flight(
    work_items_dir: Path,
    handoffs_dir: Path,
    now: datetime,
    policies_dir: Optional[Path] = None,
    events_dir: Optional[Path] = None,
    workflow_profiles: Optional[Mapping[str, str]] = None,
) -> Optional[List[Dict[str, object]]]:
    """全部非终态 WI —— Stats 人工/自动双层在飞项的唯一真源。

    2026-08-16 重定边界：exporter 必须保留 ``WI_IN_FLIGHT_STATUSES`` 的全集，
    不得用展示过滤冒充巡检完成。App 再将其切成两个互斥层：非跨租户
    ``main_only`` 或 ``blocked`` 属于人工层；其余保留调度推进态，属于默认
    折叠的自动层。

    ``needs_owner`` 仍只表达前一个信号，不能因 WI 是 ``blocked`` 就改成 true；
    ``disposition=blocked_needs_read`` 仍只表达后一个信号。两者可同时成立。不得从
    ``blocked_reason`` 猜理由真假，状态本身就是唯一机械准入判据。

    每件带三样东西：
    * ``blocked_by`` —— 下一行动人，不是结果责任 owner。WI 有
      ``active_executor`` 时它是最强真值；否则取按可证明时刻排序后最新的
      pending/accepted handoff 全部 receiver，并保留无法排序的 undated
      活跃候选；都没有才回退 owner。
    * ``days`` —— 距上次动过多少天。区分「正常在做」与「烂在那儿」的唯一机械
      判据；实测 11 件里 0-8 天连续分布，没有天然断层，所以不硬分两类，
      交给渲染侧按 ``WI_STALLED_DAYS`` 标记。
    * ``needs_owner`` —— 非跨租户 WI / handoff 的 ``main_only`` 投影；blocked
      非 main_only 行保持 false。
    * ``disposition`` —— blocked 恒为 ``blocked_needs_read``；非 blocked 的
      main_only 恒为 ``manual_required``；其余才按机制信号分入
      ``waiting_peer`` / ``sweep_*`` / ``unowned``。

    返回 ``None`` 表示目录读不到（未知 ≠ 没有）。
    """
    if not work_items_dir.is_dir():
        return None

    # 哪些 wi 已由 main_only handoff 明确声明要主身参与。裸 main_only WI 在
    # 循环内另判；两条通道与 collect_main_only_pending 完全同源。
    escalated, handoff_receivers = _current_handoff_receivers(handoffs_dir)
    pending_by_wi, handoff_to_wi = _pending_handoffs_by_wi(handoffs_dir)
    swept = _agents_with_stale_sweep(policies_dir) if policies_dir is not None else None
    last_work, last_touch, circuit_broken = _real_activity_from_events(
        events_dir, handoff_to_wi
    ) if events_dir is not None else ({}, {}, frozenset())

    def _days_since(moment: Optional[datetime]) -> Optional[int]:
        return (now - moment).days if moment is not None else None

    rows: List[Dict[str, object]] = []
    for path in sorted(work_items_dir.glob("*.yaml")):
        item = _load_yaml(path)
        if not isinstance(item, Mapping):
            continue
        status = item.get("status")
        if status not in WI_IN_FLIGHT_STATUSES:
            continue
        touched = _parse_ts(item.get("updated_at")) or _parse_ts(item.get("created_at"))
        work_item_id = str(item.get("work_item_id") or path.stem)
        needs_owner = (
            main_only_still_open(item, str(status)) or work_item_id in escalated
        )
        owner = str(item.get("owner") or "?")
        active_executor = item.get("active_executor")
        executor = (
            active_executor.strip()
            if isinstance(active_executor, str) and active_executor.strip()
            else None
        )
        if status == "in_progress" and executor:
            next_actors = (executor,)
        else:
            next_actors = handoff_receivers.get(work_item_id)
        blocked_by = "、".join(next_actors) if next_actors else owner

        created = _parse_ts(item.get("created_at"))
        age_days = _days_since(touched)
        quiet_days = _days_since(last_touch.get(work_item_id))
        executor_idle = _days_since(last_work.get(executor)) if executor else None

        # 人工信号优先，否则 main_only 会因 pending handoff 被错分为
        # waiting_peer；blocked 则恒须进人工层，不受其他机制信号覆盖。
        if status == "blocked":
            disposition = "blocked_needs_read"
        elif needs_owner:
            disposition = "manual_required"
        elif pending_by_wi.get(work_item_id):
            disposition = "waiting_peer"
        elif (
            executor
            and executor_idle is not None
            and executor_idle <= IN_FLIGHT_ACTIVE_DAYS
            and quiet_days is not None
            and quiet_days <= IN_FLIGHT_ACTIVE_DAYS
        ):
            disposition = "waiting_peer"
        elif swept is None or owner in swept:
            disposition = (
                "sweep_overdue"
                if age_days is not None and age_days >= IN_FLIGHT_SWEEP_DAYS
                else "sweep_watching"
            )
        else:
            disposition = "unowned"

        row: Dict[str, object] = {
            "work_item_id": work_item_id,
            "title": first_line(item.get("title"), limit=64),
            "blocked_by": blocked_by,
            "status": str(status or "?"),
            "days": age_days,
            "open_days": _days_since(created) if created is not None and created <= now else None,
            "created_at": created.isoformat() if created is not None and created <= now else None,
            "needs_owner": needs_owner,
            "disposition": disposition,
            # None = 从无真动静；整数 = 上次非 skipped 事件距今 N 天。
            "quiet_days": quiet_days,
            "circuit_broken": work_item_id in circuit_broken,
            # 原文透出，exporter 不判理由真假（那只会制造第二种假绿）。
            "blocked_reason": first_line(item.get("blocked_reason"), limit=96)
            if status == "blocked"
            else None,
            "owner": owner,
        }
        profile = (workflow_profiles or {}).get(work_item_id)
        if profile in {"lightweight", "standard"}:
            row["workflow_profile"] = profile
        rows.append(row)
    # Created age, never last-update age. Unknown creation stays last.
    rows.sort(key=lambda row: (row["open_days"] is None, -(row["open_days"] or 0),
                               row["created_at"] or "", str(row["work_item_id"])))
    return rows


def read_canonical_work_item(
    work_item_id: str,
    *,
    harness_cli: Path = CANONICAL_HARNESS_CLI,
    timeout_seconds: int = 90,
) -> Optional[Mapping[str, object]]:
    """Read the platform WI authority without pulling a repository checkout."""

    try:
        result = subprocess.run(
            [
                str(harness_cli),
                "converge",
                "read-work-item",
                work_item_id,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        payload = json.loads(result.stdout)
    except (
        OSError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        ValueError,
        TypeError,
    ):
        return None
    if (
        result.returncode != 0
        or not isinstance(payload, Mapping)
        or payload.get("ok") is not True
    ):
        return None
    read = payload.get("read")
    return read if isinstance(read, Mapping) else None


def probe_terminal_work_item_projection(
    work_items_dir: Path,
    handoffs_dir: Path,
    work_item_id: str,
    now: datetime,
    *,
    authority_read: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """Probe the real Stats in-flight read model for one closeout candidate.

    This is a privacy-safe downstream-effect primitive: it reads the same
    ``collect_in_flight`` path as production, reports no WorkItem body, and
    fails closed when the ledger item or source directory is unavailable.
    Git revision readback remains a separate Harness consumer probe.
    """

    canonical = authority_read or read_canonical_work_item(work_item_id)
    canonical_fields = {
        "canonical_as_of": canonical.get("canonical_as_of") if canonical else None,
        "surface_coverage": canonical.get("coverage") if canonical else "unknown",
        "authority_status": (
            canonical.get("authority_status") if canonical else "unavailable"
        ),
        "source_ref": canonical.get("source_ref") if canonical else None,
    }
    if (
        not isinstance(canonical, Mapping)
        or canonical.get("authority_status") != "verified"
        or canonical.get("record_status") != "found"
        or not isinstance(canonical.get("work_item"), Mapping)
    ):
        return {
            "schema_version": "pulse_stats_terminal_projection_probe/v1",
            "consumer_id": "pulse-stats-in-flight",
            "work_item_id": work_item_id,
            "passed": False,
            "reason": "canonical_authority_unavailable",
            "ledger_status": None,
            "in_flight_present": None,
            **canonical_fields,
        }
    canonical_item = canonical["work_item"]
    assert isinstance(canonical_item, Mapping)
    ledger_status = canonical_item.get("status")
    if not isinstance(ledger_status, str):
        return {
            "schema_version": "pulse_stats_terminal_projection_probe/v1",
            "consumer_id": "pulse-stats-in-flight",
            "work_item_id": work_item_id,
            "passed": False,
            "reason": "work_item_status_unavailable",
            "ledger_status": None,
            "in_flight_present": None,
            **canonical_fields,
        }
    item_path = work_items_dir / f"{work_item_id}.yaml"
    item = _load_yaml(item_path)
    if not isinstance(item, Mapping) or item.get("status") != ledger_status:
        return {
            "schema_version": "pulse_stats_terminal_projection_probe/v1",
            "consumer_id": "pulse-stats-in-flight",
            "work_item_id": work_item_id,
            "passed": False,
            "reason": "local_projection_stale",
            "ledger_status": ledger_status,
            "in_flight_present": None,
            **canonical_fields,
        }
    rows = collect_in_flight(work_items_dir, handoffs_dir, now)
    if rows is None:
        return {
            "schema_version": "pulse_stats_terminal_projection_probe/v1",
            "consumer_id": "pulse-stats-in-flight",
            "work_item_id": work_item_id,
            "passed": False,
            "reason": "stats_source_unavailable",
            "ledger_status": ledger_status,
            "in_flight_present": None,
            **canonical_fields,
        }
    matches = [row for row in rows if row["work_item_id"] == work_item_id]
    expected_present = ledger_status in WI_IN_FLIGHT_STATUSES
    passed = (
        canonical.get("coverage") == "complete"
        and len(matches) == (1 if expected_present else 0)
    )
    return {
        "schema_version": "pulse_stats_terminal_projection_probe/v1",
        "consumer_id": "pulse-stats-in-flight",
        "work_item_id": work_item_id,
        "passed": passed,
        "reason": (
            None
            if passed
            else (
                "surface_coverage_incomplete"
                if canonical.get("coverage") != "complete"
                else "projection_status_mismatch"
            )
        ),
        "ledger_status": ledger_status,
        "in_flight_present": bool(matches),
        **canonical_fields,
    }


def collect_harness(work_items_dir: Path, handoffs_dir: Path, now: datetime) -> Dict[str, object]:
    """wi counters, stalled list and pending handoffs.

    Bad YAML is skipped and counted, never fatal (spec §3.3). The wi identifier
    field is ``work_item_id``, not ``id``.
    """
    # 「今日」按大鸟看表的时区算，不按 UTC（2026-07-28）。原先取 UTC 日期，
    # 北京时间 00:00-08:00 这八个小时里「今日」指的是昨天——每天有三分之一
    # 时间这三个数说的不是它标的那天。
    today = now.astimezone(DISPLAY_TZ).date()
    week_cutoff = now - timedelta(days=7)
    # 本周 = 北京时区的自然周(周一 00:00 起),与「近 7 天」是两回事:
    # 前者周一清零、可与上周比,后者是滑动窗口、天天在变。大鸟 2026-07-29
    # 要的是前者(「当周(不是最近7天)」),但滚动窗口对看趋势仍有用,两个都留,
    # 各自标清楚数的是哪个窗口。见 [[counter-label-must-name-unit-and-window]]。
    iso_week_start = today - timedelta(days=today.weekday())
    result: Dict[str, object] = {
        "week_created": None,
        "day_created": None,
        "day_completed": None,
        "iso_week_created": None,
        "iso_week_completed": None,
        "iso_week_start": iso_week_start.isoformat(),
        "stalled": None,
        "handoff_pending": None,
        "unreadable_work_items": 0,
        "unreadable_handoffs": 0,
    }

    if work_items_dir.is_dir():
        week = day = completed = 0
        iso_week = iso_week_done = 0
        unreadable = 0
        stalled: List[Dict[str, object]] = []
        for path in sorted(work_items_dir.glob("*.yaml")):
            item = _load_yaml(path)
            if item is None:
                unreadable += 1
                continue
            created = _parse_ts(item.get("created_at"))
            status = item.get("status")
            if created is not None:
                if created >= week_cutoff:
                    week += 1
                created_local = created.astimezone(DISPLAY_TZ).date()
                if created_local == today:
                    day += 1
                if created_local >= iso_week_start:
                    iso_week += 1
            # 收口与新建各算各的：`completed` 原先嵌在「今天新建」里面，数的是
            # 「今天建、今天完」，挂了八天今天才收口的一件都不认（2026-07-28
            # 实测面板报 5，真值 6，漏的是 amanda 昨天建今天完的那件）。
            # 判据用 updated_at：全库 463 条都有，completed_at 只有 6 条。
            if status == "completed":
                closed = _parse_ts(item.get("updated_at"))
                if closed is not None:
                    closed_local = closed.astimezone(DISPLAY_TZ).date()
                    if closed_local == today:
                        completed += 1
                    if closed_local >= iso_week_start:
                        iso_week_done += 1
            if status in WI_OPEN_STATUSES:
                touched = _parse_ts(item.get("updated_at")) or created
                if touched is not None:
                    age_days = (now - touched).days
                    if age_days > WI_STALLED_DAYS:
                        stalled.append(
                            {
                                "work_item_id": str(item.get("work_item_id") or path.stem),
                                "title": str(item.get("title") or ""),
                                "days": age_days,
                                "status": str(status),
                            }
                        )
        stalled.sort(key=lambda row: (-int(row["days"]), str(row["work_item_id"])))
        result.update(
            week_created=week,
            day_created=day,
            day_completed=completed,
            iso_week_created=iso_week,
            iso_week_completed=iso_week_done,
            stalled=stalled[:WI_STALLED_MAX_ITEMS],
            unreadable_work_items=unreadable,
        )

    if handoffs_dir.is_dir():
        pending = 0
        unreadable = 0
        for path in sorted(handoffs_dir.glob("*.yaml")):
            item = _load_yaml(path)
            if item is None:
                unreadable += 1
                continue
            if item.get("ack_state") == "pending":
                pending += 1
        result.update(handoff_pending=pending, unreadable_handoffs=unreadable)

    return result


def run_codex_remote(argv: Sequence[str], timeout: int) -> Optional[str]:
    """Run the fixed remote Codex probe. Any failure yields None."""
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout
