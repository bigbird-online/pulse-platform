"""Relay 启动记录抽取：execution、scheduled_session、usage（合同第 2 节启动记录）。

human、attribution、gap 不入库。入口渠道按 execution.human_ref 到登记的 human-inputs 目录里找。

用量记录的语义（Relay/src/collaboration-capture.ts，11 月实测核过）：
- cc_message.v1（request）：单条消息的用量快照，不是累计；99% 在 message_start 时记下，输出数偏小。
- codex_cli.v1、relay_pi.v1、cc_result.v1（turn）：Relay 已按本回合切分的增量，照取。
- cc_sdk_result.v1（turn）：CC 的 modelUsage，是整个根会话按模型的累计计数，跨 Relay 进程延续
  （11 月 145 族里跨文件边界 1,138 处上升、3 处下降）。按 (会话, 模型) 取差：本值减同族时间上
  前一个计数；首个计数从 0 起算；任一类下降即该次不出事件（旧链路 cc_session_counters 同法）。
  每族的计数序列存在节点库 meta 的 host_counter:<会话>:<模型> 键下，重读同一记录得到同一差值。
  迟到的计数插在序列中间时，后继计数的增量随之重算：event_id 带增量（usage_ref:四类增量），旧行撤销、
  新行写入，保证增量追尾与干净重建逐行相同。
- usage 为空对象 {} 且不覆盖任何请求明细（covers 为空）：结果里没有报任何用量字段，也没有请求记录，
  不入库（诊断计 host_usage_empty）。实测 11-08 两条 CC 的这类记录，所在执行 7–14 毫秒内结束、
  一条以 failed 收尾、另一条记了 missing_usage 缺口，不可能发出过模型请求；记 0 会把"没有观测"
  写成"观测到 0"，记 NULL 会把它算成未计价，两者都不如不出事件诚实。键为 null 的用量仍是未知，照常入库。
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from pulse_compute_cost.receipts import reference

from .store import utc_text

SCHEMA = "pulse_collaboration_host_event/v1"
GRANULARITY = {"request": "request", "turn_aggregate": "turn", "turn": "turn"}
CUMULATIVE = "cc_sdk_result.v1"
RUNTIME = {"cc_sdk": "cc"}
TOKENS = ("input_uncached", "cache_read", "cache_write", "output")
COUNTER_KEY = "host_counter:"
PATH_FIELD = "__path__"      # 行所属的来源文件（迟到计数重算的后继行归后继自己的文件）
NULL_TOKENS = {"input_uncached": None, "cache_read": None, "cache_write": None, "output": None}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def tokens(runtime: str, usage: object, mapping: object, model: object = None) -> dict[str, int | None]:
    """带上模型名：agy 协议没有缓存写一类，Gemini 模型据此记 0（normalize_usage 的规则），其余模型仍为未知。"""
    from pulse_compute_cost.receipt_sources import normalize_usage
    try:
        got = normalize_usage(runtime, usage, mapping=mapping if isinstance(mapping, str) else None,
                              model=model if isinstance(model, str) else None)
    except Exception:  # 原始值冲突或不合法：记未知，事件仍入库
        return dict(NULL_TOKENS)
    return {"input_uncached": got.get("uncached_input"), "cache_read": got.get("cache_read"),
            "cache_write": got.get("cache_write"), "output": got.get("output")}


def load_job_owners(path: str | Path | None) -> dict[str, str | None]:
    """cron-jobs.json → {任务号: owner}。同一任务号出现多次且 owner 不一致即歧义，记 None（不猜）；
    Relay 启动时取第一个、热重载拒收整份重复配置，两种行为下 owner 都不确定，所以不补。"""
    try:
        document = json.loads(Path(path).read_bytes()) if path else {}
    except (OSError, ValueError):
        document = {}
    jobs = document.get("jobs") if isinstance(document, dict) else document
    owners: dict[str, str | None] = {}
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict) or not isinstance(job.get("id"), str):
            continue
        owner = _text(job.get("owner"))
        if job["id"] in owners and owners[job["id"]] != owner:
            owner = None
        owners[job["id"]] = owner
    return owners


def owners_digest(owners: dict[str, str | None]) -> int:
    """任务号→owner 映射的摘要，进 host 文件的解析版本：映射一变，host 文件整份重读，启动记录重新判定。"""
    import zlib
    return zlib.crc32(json.dumps(sorted(owners.items()), ensure_ascii=False).encode("utf-8"))


def job_owner(ctx, job_id: object) -> str | None:
    """定时任务 ID → owner（路径由 tail.cron_jobs_path 解析后注入）；任务已删、歧义或未注入路径为 None。
    None 不会覆盖库中已记录的 agent（store 的 launch_receipts.agent 合并规则），历史归属保留。"""
    if not isinstance(job_id, str):
        return None
    if "job_owners" not in ctx.shared:
        ctx.shared["job_owners"] = load_job_owners(ctx.shared.get("cron_jobs_path"))
    return ctx.shared["job_owners"].get(job_id)


def human_channels(ctx) -> dict[str, Path]:
    """human_ref → 输入元数据文件；每轮建一次，只哈希文件名，不读内容。"""
    cache = ctx.shared.setdefault("human_inputs", {})
    source_id = ctx.source.get("id") or str(ctx.path.parent)
    if source_id not in cache:
        dirs = [ctx.source.get("human_input_directory"), *(ctx.source.get("human_input_directories") or [])]
        index: dict[str, Path] = {}
        for value in dirs:
            if not isinstance(value, str) or not Path(value).is_dir() or Path(value).is_symlink():
                continue
            for path in Path(value).glob("*.json"):
                if re.fullmatch(r"[a-f0-9]{64}", path.stem) and not path.is_symlink():
                    index.setdefault(reference("human", path.stem), path)
        cache[source_id] = index
    return cache[source_id]


def entrypoint(ctx, human_ref: object) -> str | None:
    if not isinstance(human_ref, str):
        return None
    path = human_channels(ctx).get(human_ref)
    if path is None:
        ctx.note("host_human_input_missing")
        return None
    try:
        channel = json.loads(path.read_bytes()).get("source", {}).get("channel")
    except (OSError, ValueError, AttributeError):
        return None
    return channel if channel in ("feishu", "telegram") else None


def _counters(ctx, family: str) -> list:
    """一族累计计数 [[at, [四类]], …]，按时间升序；首次用到时从节点库 meta 读。"""
    cache = ctx.shared.setdefault("host_counters", {})
    if family not in cache:
        row = ctx.conn.execute("SELECT value FROM meta WHERE key=?", (COUNTER_KEY + family,)).fetchone() \
            if ctx.conn is not None else None
        cache[family] = json.loads(row[0]) if row else []
    return cache[family]


def _delta(series: list, index: int) -> list[int | None]:
    values = series[index][1]
    previous = series[index - 1][1] if index else [0, 0, 0, 0]
    return [v - p if v is not None and p is not None else None for v, p in zip(values, previous)]


def _counter_row(series: list, index: int) -> dict[str, Any] | None:
    """序列第 index 个计数相对前一个计数的增量行；下降或全零为 None。event_id 带增量，增量变了即是另一行。"""
    template = series[index][2]
    delta = _delta(series, index)
    if any(d is not None and d < 0 for d in delta) or not any(delta):
        return None
    suffix = "-".join("x" if d is None else str(d) for d in delta)
    return {**template, "event_id": f"{template['event_id']}:{suffix}", **dict(zip(TOKENS, delta))}


def counter_rows(ctx, family: str, at: str, total: dict[str, int | None], template: dict[str, Any]
                 ) -> list[tuple[str, dict[str, Any]]]:
    """累计计数并入同族序列（按时间），返回要写的行：本计数的增量行；迟到插在中间时，
    后继计数的增量随之重算——旧增量行撤销、新增量行写入（归后继自己的来源文件）。"""
    import bisect
    values = [total[k] for k in TOKENS]
    series = _counters(ctx, family)
    times = [item[0] for item in series]
    index = bisect.bisect_left(times, at)
    entry = [at, values, template]
    out: list[tuple[str, dict[str, Any]]] = []
    if index < len(series) and series[index][0] == at:
        if series[index][1] != values:
            ctx.note("host_counter_conflict")
        if series[index][2:] != [template]:
            series[index] = [series[index][0], series[index][1], template]
            ctx.shared.setdefault("host_dirty", set()).add(family)
    else:
        follower = index < len(series) and len(series[index]) > 2
        before = _counter_row(series, index) if follower else None   # 后继在插入前的增量行
        series.insert(index, entry)
        ctx.shared.setdefault("host_dirty", set()).add(family)
        if index + 1 < len(series):
            ctx.note("host_counter_out_of_order")
            after = _counter_row(series, index + 1) if follower else None
            if before != after:
                if before is not None:
                    out.append(("retract:usage_events", before))
                if after is not None:
                    out.append(("usage_events", after))
    row = _counter_row(series, index)
    if row is None and any(d is not None and d < 0 for d in _delta(series, index)):
        ctx.note("host_counter_decrease")
    return ([("usage_events", row)] if row is not None else []) + out


def flush_counters(ctx, conn) -> None:
    dirty = ctx.shared.get("host_dirty") or set()
    for family in sorted(dirty):
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (COUNTER_KEY + family, json.dumps(ctx.shared["host_counters"][family], separators=(",", ":"))))
    dirty.clear()


class Extractor:
    PARSER_VERSION = 5

    @classmethod
    def version(cls, shared: dict[str, Any]) -> int:
        """游标里记的解析版本＝规则版本与 owner 映射摘要的组合；任一变化都触发整份重读（幂等，带撤销）。"""
        if "job_owners" not in shared:
            shared["job_owners"] = load_job_owners(shared.get("cron_jobs_path"))
        return (cls.PARSER_VERSION << 32) | owners_digest(shared["job_owners"])

    def __init__(self, ctx, state: dict[str, Any]):
        self.ctx = ctx

    def state(self) -> dict[str, Any]:
        return {}

    def flush(self, conn) -> None:
        flush_counters(self.ctx, conn)

    def wants(self, line: bytes) -> bool:
        return b'"execution"' in line or b'"scheduled_session"' in line or b'"usage"' in line

    def feed(self, line: bytes) -> list[tuple[str, dict[str, Any]]]:
        try:
            raw = json.loads(line)
        except ValueError:
            self.ctx.note("host_invalid_json")
            return []
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not _text(raw.get("event_ref")):
            return []
        kind, node = raw.get("kind"), self.ctx.node
        started = utc_text(raw.get("started_at")) or utc_text(raw.get("at"))
        if started is None:
            self.ctx.note("host_without_time")
            return []
        base = {"node": node, "occurred_at": started, "runtime": _text(raw.get("runtime")),
                "agent": _text(raw.get("executor")), "wi": _text(raw.get("wi")),
                "attempt_ref": _text(raw.get("attempt_ref")), "session_ref": None, "job_id": None,
                "job_name": None, "entrypoint": None, "state": _text(raw.get("state"))}
        if kind == "execution":
            return [("launch_receipts", {**base, "receipt_id": raw["event_ref"], "kind": "execution",
                                         "entrypoint": entrypoint(self.ctx, raw.get("human_ref"))})]
        if kind == "scheduled_session":
            # 定时任务记录不带 executor：按本机 Relay 定时任务登记的 owner 补 Agent
            base["agent"] = base["agent"] or job_owner(self.ctx, raw.get("job_id"))
            return [("launch_receipts", {**base, "receipt_id": raw["event_ref"], "kind": "scheduled_session",
                                         "session_ref": _text(raw.get("native_scope_ref")),
                                         "job_id": _text(raw.get("job_id")), "job_name": _text(raw.get("job_name")),
                                         "entrypoint": "automation"})]
        if kind != "usage" or raw.get("model") == "<synthetic>":
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        scope, attempt = _text(raw.get("native_scope_ref")), _text(raw.get("attempt_ref"))
        if scope and attempt:
            out.append(("launch_receipts", {**base, "receipt_id": f"bind:{attempt}:{scope}", "kind": "session_binding",
                                            "session_ref": scope, "state": None}))
        runtime = _text(raw.get("runtime")) or "unknown"
        runtime = RUNTIME.get(runtime, runtime)
        event = _text(raw.get("usage_ref")) or raw["event_ref"]
        if not scope:
            self.ctx.note("host_usage_without_session")
        occurred = utc_text(raw.get("at")) or started
        if raw.get("usage") == {} and not raw.get("covers"):
            self.ctx.note("host_usage_empty")
            return out
        amounts = tokens(runtime, raw.get("usage"), raw.get("mapping"), raw.get("model"))
        row = {"node": node, "runtime": runtime, "event_id": event, "request_id": "", "source": "host",
               "granularity": GRANULARITY.get(raw.get("granularity"), "request"), "session_id": "",
               # 没有会话号的用量以执行号兜底，保留定位线索
               "session_ref": scope or attempt or event, "agent_id": "", "turn_id": "",
               "occurred_at": occurred, "model": _text(raw.get("model")), "cwd": None,
               "final": 1 if raw.get("final") else 0, PATH_FIELD: str(self.ctx.path)}
        if raw.get("mapping") == CUMULATIVE:
            if not scope:
                return out          # 没有会话就无从取差，不出事件
            return out + counter_rows(self.ctx, f"{scope}:{raw.get('model')}", occurred, amounts, row)
        out.append(("usage_events", {**row, **amounts}))
        return out
