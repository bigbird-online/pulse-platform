"""Privacy-safe Claude Code transcript usage and bounded SCL measurement snapshots.

CC 没有 Pi 那样的进程内扩展；Relay Host 在 settled 边界调用本模块的 probe，
对 CC 原生会话的全部 transcript 取逐文件字节前缀边界：主 transcript
`<projects>/<cwd>/<session>.jsonl` 与 Task 子代理
`<projects>/<cwd>/<session>/subagents/agent-*.jsonl`。baseline 为零增量起点，
final 只计各文件 baseline 之后、final 之前的 assistant 请求；baseline 之后才出现
的子代理文件从 0 起算。位置携带逐文件清单（`sources`），聚合字节数与清单摘要
构成与 `pi_session_usage_snapshot/v2` 同形状的位置，SCL 以同一规则验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Mapping

from .runtime_samples import CC_SYNTHETIC_MODEL

from .pi_usage import (
    PiTokenUsage,
    PiUsageCollection,
    PiUsageError,
    PiUsageFact,
    _BOUNDARY_POLICY,
    _SURFACE_RE,
    _TOKEN_RE,
    _WI_RE,
    _assert_privacy,
    _canonical,
    _nonempty,
    _snapshot_digest,
    _timestamp,
    _validate_position,
    source_manifest_digest,
    validate_pi_attribution_boundary,
    validate_pi_usage_summary,
)


CC_SNAPSHOT_SCHEMA = "cc_session_usage_snapshot/v2"
CC_SUMMARY_SCHEMA = "cc_native_usage_summary/v1"
CC_RUNTIMES = frozenset({"cc", "cc_sdk"})
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_AUTHORITY_RE = re.compile(r"^session_v1_[0-9a-f]{64}$")
_PROCUREMENT = {
    "chatgpt_subscription", "openrouter_api", "claude_subscription",
    "cursor_subscription", "unknown",
}
_SNAPSHOT_FIELDS = {
    "schema", "generated_at", "lifecycle_state", "measurement_role",
    "work_item_id", "agent_id", "execution_surface", "phase", "epoch",
    "runtime", "procurement_source", "authority_ref",
    "runtime_binding_sha256", "attribution_boundary", "summary",
    "coverage", "snapshot_sha256",
}
_COVERAGE = {
    "baseline": "exact_zero_baseline",
    "progress": "exact_bounded_open",
    "final": "exact_bounded_sealed",
}


class CcUsageError(PiUsageError):
    """Raised when a CC transcript cannot provide trustworthy bounded usage."""


def cc_opaque(kind: str, raw: str) -> str:
    digest = sha256(f"pulse-computecost-v2:cc:{kind}\0{raw}".encode()).hexdigest()
    return f"{kind}_v1_{digest}"


def cc_authority_ref(session_id: str) -> str:
    """Same Host Session reference Relay uses for every runtime."""
    return "session_v1_" + sha256(("pulse-session-v1\0" + session_id).encode()).hexdigest()


@dataclass(frozen=True)
class CcUsageBinding:
    """A v3 runtime usage binding projected onto one CC native session."""

    work_item_id: str
    agent_id: str
    phase: str
    epoch: int
    runtime: str
    procurement_source: str
    authority_ref: str
    execution_surface: str
    runtime_binding_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.work_item_id, str) or _WI_RE.fullmatch(self.work_item_id) is None:
            raise CcUsageError("CC binding work item is invalid")
        for field in ("agent_id", "phase"):
            value = getattr(self, field)
            if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
                raise CcUsageError(f"CC binding {field} is invalid")
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch < 1:
            raise CcUsageError("CC binding epoch is invalid")
        if self.runtime not in CC_RUNTIMES:
            raise CcUsageError("CC binding runtime is invalid")
        if self.procurement_source not in _PROCUREMENT:
            raise CcUsageError("CC binding procurement source is invalid")
        if not isinstance(self.authority_ref, str) or _AUTHORITY_RE.fullmatch(self.authority_ref) is None:
            raise CcUsageError("CC binding authority is invalid")
        if not isinstance(self.execution_surface, str) or _SURFACE_RE.fullmatch(self.execution_surface) is None:
            raise CcUsageError("CC binding execution surface is invalid")
        if not isinstance(self.runtime_binding_sha256, str) or _SHA_RE.fullmatch(self.runtime_binding_sha256) is None:
            raise CcUsageError("CC binding digest is invalid")

    @property
    def binding_sha256(self) -> str:
        """Reuse the original host binding digest in canonical runtime samples."""
        return self.runtime_binding_sha256


def _read_source(path: str | Path) -> bytes:
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        # CC 在第一条消息前不创建 transcript；空前缀即精确零起点。
        return b""
    except OSError as exc:
        raise CcUsageError("cannot read CC transcript") from exc


def _complete_prefix(raw: bytes) -> bytes:
    """CC 追加写；只承认最后一个换行之前的完整记录。"""
    end = raw.rfind(b"\n") + 1
    return raw[:end]


MAIN_SOURCE = "main"


def cc_source_ref(session_id: str, name: str) -> str:
    """Opaque per-file identity inside one CC session (`main` or `subagents/<file>`)."""
    return cc_opaque("cc_source", f"{session_id}\0{name}")


def cc_session_sources(path: str | Path) -> list[tuple[str, Path]]:
    """Main transcript first, then Task subagent transcripts in name order.

    Claude Code 把子代理写到 `<session>/subagents/agent-*.jsonl`；其中的
    sessionId 仍是父会话。符号链接与非常规文件不被读取。
    """
    main = Path(path)
    result = [(MAIN_SOURCE, main)]
    folder = main.parent / main.stem / "subagents"
    if folder.is_dir() and not folder.is_symlink() and not folder.parent.is_symlink():
        for item in sorted(folder.glob("agent-*.jsonl")):
            if item.is_symlink() or not item.is_file():
                raise CcUsageError("CC subagent transcript must be a regular file")
            result.append((f"subagents/{item.name}", item))
    return result


@dataclass(frozen=True)
class CcSource:
    """One transcript of the session and the byte offset its new usage starts at."""

    name: str
    raw: bytes
    start_bytes: int


def _usage(row: Mapping[str, Any], binding: CcUsageBinding) -> PiTokenUsage:
    """Project the reviewed runtime_usage_sample/v2 into the bounded envelope.

    CC input_tokens is normalized only by runtime_samples. The envelope retains
    its historical uncached-input convention; no second raw-field parser lives here.
    """
    from . import runtime_samples
    message = row['message']
    message_id = message.get('id') or row.get('uuid')
    if binding.runtime == 'cc':
        payload = {**row, 'message': {**message, 'id': message_id}}
    else:
        payload = {'event_id': message_id, 'occurred_at': row.get('timestamp'),
                   'model': message.get('model'), 'usage': message.get('usage'),
                   'effort': row.get('effort')}
    try:
        sample = runtime_samples.build_cc_usage_sample(payload, binding=binding, producer=binding.runtime)
    except runtime_samples.RuntimeUsageSampleError as exc:
        raise CcUsageError(str(exc)) from exc
    tokens = sample['tokens']
    return PiTokenUsage(input=tokens['input']-tokens['cache_read']-tokens['cache_write'],
        output=tokens['output'], cache_read=tokens['cache_read'],
        cache_write=tokens['cache_write'], reasoning=0, total_tokens=tokens['total'])



def collect_cc_session_usage(
    sources: list[CcSource],
    session_id: str,
    binding: CcUsageBinding,
) -> tuple[PiUsageCollection, list[dict[str, object]]]:
    """Collect one request per native assistant message id across all session files.

    CC 每个 content block 写一行，同一 message.id 重复携带 usage；以最后一行
    为该请求的最终计数，额外行计入 deduplicated。某文件 baseline 之前出现过的
    message 整体排除，后续续写行同样排除，保证排除计数在 baseline/final 间稳定。
    去重跨文件进行（主 transcript 与子代理共用同一 message id 空间）。
    返回聚合 collection 与按 source_ref 排序的逐文件清单。
    """
    session = _nonempty(session_id, "session_id")
    seen_entries: dict[str, bytes] = {}
    excluded_ids: set[str] = set()
    requests: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    last_entry_ref: str | None = None
    source_entry_count = deduplicated = 0
    manifest: list[dict[str, object]] = []
    for source in sources:
        prefix = _complete_prefix(source.raw)
        start_bytes = source.start_bytes
        if isinstance(start_bytes, bool) or not isinstance(start_bytes, int) or not 0 <= start_bytes <= len(prefix):
            raise CcUsageError("CC baseline source position is invalid")
        if start_bytes and not prefix[:start_bytes].endswith(b"\n"):
            raise CcUsageError("CC baseline does not end at a record boundary")
        manifest.append({
            "source_ref": cc_source_ref(session, source.name),
            "source_bytes": len(prefix),
            "source_prefix_sha256": sha256(prefix).hexdigest(),
        })
        cursor = 0
        for position, raw_line in enumerate(prefix.splitlines(keepends=True), start=1):
            line_start = cursor
            cursor += len(raw_line)
            payload = raw_line.rstrip(b"\r\n")
            if not payload.strip():
                continue
            try:
                row = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise CcUsageError(f"malformed CC transcript JSON at line {position}") from exc
            if not isinstance(row, Mapping):
                raise CcUsageError(f"CC transcript line {position} must be an object")
            entry_id = row.get("uuid")
            if isinstance(entry_id, str) and entry_id:
                digest = sha256(_canonical(row)).digest()
                prior = seen_entries.get(entry_id)
                if prior is not None:
                    if prior != digest:
                        raise CcUsageError("duplicate CC transcript uuid changed immutable content")
                    deduplicated += 1
                    continue
                seen_entries[entry_id] = digest
                source_entry_count += 1
                last_entry_ref = cc_opaque("cc_entry", f"{session}\0{entry_id}")
            if row.get("type") != "assistant":
                continue
            message = row.get("message")
            if not isinstance(message, Mapping) or message.get("usage") is None:
                continue
            model = message.get("model")
            if model == CC_SYNTHETIC_MODEL:
                continue  # CC 本地合成消息，没有 provider 请求。
            identity = message.get("id") or entry_id
            if not isinstance(identity, str) or not identity:
                raise CcUsageError("CC assistant usage lacks a stable message identity")
            if identity in excluded_ids:
                deduplicated += 1
                continue
            if line_start < start_bytes:
                excluded_ids.add(identity)
                continue
            usage = _usage(row, binding)
            current = requests.get(identity)
            if current is None:
                order.append(identity)
                requests[identity] = {
                    "occurred_at": _timestamp(row.get("timestamp"), "entry.timestamp"),
                    "model": _nonempty(model, "message.model"),
                    "activity": "sidechain" if row.get("isSidechain") is True else "assistant_turn",
                    "usage": usage,
                }
                continue
            deduplicated += 1
            previous: PiTokenUsage = current["usage"]
            if any(getattr(usage, key) < getattr(previous, key)
                   for key in ("input", "output", "cache_read", "cache_write")):
                raise CcUsageError("CC message usage regressed across content blocks")
            current["usage"] = usage
    facts = []
    for identity in order:
        item = requests[identity]
        model = item["model"]
        facts.append(PiUsageFact(
            fact_id=cc_opaque("cc_usage", f"{session}\0{identity}"),
            session_ref=cc_opaque("cc_session", session),
            entry_ref=cc_opaque("cc_entry", f"{session}\0{identity}"),
            occurred_at=item["occurred_at"],
            work_item_id=binding.work_item_id, phase=binding.phase, epoch=binding.epoch,
            provider="anthropic" if model.startswith("claude") else "unknown",
            model=model, reasoning_effort="unknown",
            procurement_source=binding.procurement_source,
            activity=item["activity"], usage=item["usage"],
        ))
    manifest.sort(key=lambda item: str(item["source_ref"]))
    collection = PiUsageCollection(
        tuple(facts), last_entry_ref, source_manifest_digest(manifest),
        sum(int(item["source_bytes"]) for item in manifest),  # type: ignore[call-overload]
        source_entry_count, len(excluded_ids), deduplicated,
    )
    return collection, manifest


def _summary(collection: PiUsageCollection) -> dict[str, Any]:
    return {**collection.summary(), "schema": CC_SUMMARY_SCHEMA}


def _position(collection: PiUsageCollection, manifest: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source_bytes": collection.source_bytes,
        "source_prefix_sha256": collection.source_prefix_sha256,
        "last_entry_ref": collection.last_entry_ref,
        "sources": manifest,
    }


def validate_cc_session_snapshot(value: object) -> None:
    if not isinstance(value, Mapping) or value.get("schema") != CC_SNAPSHOT_SCHEMA:
        raise CcUsageError("unsupported CC session usage snapshot")
    if set(value) != _SNAPSHOT_FIELDS:
        raise CcUsageError("bounded CC usage snapshot fields are invalid")
    _timestamp(value.get("generated_at"), "generated_at")
    role = value.get("measurement_role")
    lifecycle = value.get("lifecycle_state")
    if role not in _COVERAGE or lifecycle not in {"open", "sealed"} or (lifecycle == "sealed") != (role == "final"):
        raise CcUsageError("bounded CC measurement role is invalid")
    CcUsageBinding(
        work_item_id=value.get("work_item_id"), agent_id=value.get("agent_id"),
        phase=value.get("phase"), epoch=value.get("epoch"), runtime=value.get("runtime"),
        procurement_source=value.get("procurement_source"), authority_ref=value.get("authority_ref"),
        execution_surface=value.get("execution_surface"),
        runtime_binding_sha256=value.get("runtime_binding_sha256"),
    )
    digest = value.get("snapshot_sha256")
    if not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None or _snapshot_digest(value) != digest:
        raise CcUsageError("CC usage snapshot digest does not match content")
    summary = validate_pi_usage_summary(value.get("summary"), family="cc")
    validate_pi_attribution_boundary(value.get("attribution_boundary"), summary=summary,
                                     measurement_role=str(role), source_manifest=True)
    if value.get("coverage") != _COVERAGE[str(role)]:
        raise CcUsageError("bounded CC coverage is invalid")
    _assert_privacy(value)


def _validate_baseline(baseline: object, binding: CcUsageBinding) -> Mapping[str, Any]:
    validate_cc_session_snapshot(baseline)
    assert isinstance(baseline, Mapping)
    if baseline.get("measurement_role") != "baseline" or any(
        baseline.get(key) != getattr(binding, key)
        for key in ("work_item_id", "agent_id", "execution_surface", "phase", "epoch", "runtime",
                    "procurement_source", "authority_ref", "runtime_binding_sha256")
    ):
        raise CcUsageError("CC baseline does not match runtime authority")
    return baseline


def build_cc_session_snapshot(
    path: str | Path,
    session_id: str,
    binding: CcUsageBinding,
    *,
    lifecycle_state: str,
    measurement_role: str,
    baseline_snapshot: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if cc_authority_ref(session_id) != binding.authority_ref:
        raise CcUsageError("CC transcript session does not match the bound Host Session")
    if measurement_role not in _COVERAGE or (lifecycle_state == "sealed") != (measurement_role == "final"):
        raise CcUsageError("CC lifecycle and measurement role disagree")
    generated = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _timestamp(generated, "generated_at")
    files = [(name, _read_source(item)) for name, item in cc_session_sources(path)]
    if measurement_role == "baseline":
        if baseline_snapshot is not None:
            raise CcUsageError("CC baseline cannot descend from another baseline")
        collection, manifest = collect_cc_session_usage(
            [CcSource(name, raw, len(_complete_prefix(raw))) for name, raw in files], session_id, binding)
        baseline_position = final_position = _position(collection, manifest)
        baseline_digest: str | None = None
        excluded = collection.excluded_usage_event_count
    else:
        baseline = _validate_baseline(baseline_snapshot, binding)
        boundary = baseline["attribution_boundary"]
        baseline_position = _validate_position(boundary["baseline"], "baseline position", source_manifest=True)
        starts = _verified_starts(baseline_position, files, session_id, "baseline")
        collection, manifest = collect_cc_session_usage(
            [CcSource(name, raw, starts.get(cc_source_ref(session_id, name), 0)) for name, raw in files],
            session_id, binding)
        final_position = _position(collection, manifest)
        baseline_digest = str(baseline["snapshot_sha256"])
        excluded = int(boundary["excluded_pre_baseline_usage_events"])
        if collection.excluded_usage_event_count != excluded:
            raise CcUsageError("CC pre-baseline usage changed after baseline capture")
    snapshot: dict[str, Any] = {
        "schema": CC_SNAPSHOT_SCHEMA,
        "generated_at": generated,
        "lifecycle_state": lifecycle_state,
        "measurement_role": measurement_role,
        "work_item_id": binding.work_item_id,
        "agent_id": binding.agent_id,
        "execution_surface": binding.execution_surface,
        "phase": binding.phase,
        "epoch": binding.epoch,
        "runtime": binding.runtime,
        "procurement_source": binding.procurement_source,
        "authority_ref": binding.authority_ref,
        "runtime_binding_sha256": binding.runtime_binding_sha256,
        "attribution_boundary": {
            "policy": _BOUNDARY_POLICY,
            "baseline_snapshot_sha256": baseline_digest,
            "baseline": baseline_position,
            "final": final_position,
            "excluded_pre_baseline_usage_events": excluded,
            "deduplicated_entries": collection.deduplicated_entry_count,
        },
        "summary": _summary(collection),
        "coverage": _COVERAGE[measurement_role],
    }
    snapshot["snapshot_sha256"] = _snapshot_digest(snapshot)
    validate_cc_session_snapshot(snapshot)
    return snapshot


def _verified_starts(
    edge: Mapping[str, Any], files: list[tuple[str, bytes]], session_id: str, position: str
) -> dict[str, int]:
    """Every file recorded at `edge` still begins with its recorded prefix; returns ref -> offset."""
    present = {cc_source_ref(session_id, name): raw for name, raw in files}
    starts: dict[str, int] = {}
    for item in edge["sources"]:
        ref, size = str(item["source_ref"]), int(item["source_bytes"])
        raw = present.get(ref)
        if raw is None:
            if size:
                raise CcUsageError(f"CC transcript recorded at the {position} position disappeared")
            raw = b""
        if size > len(raw) or sha256(raw[:size]).hexdigest() != item["source_prefix_sha256"]:
            raise CcUsageError(f"CC transcript changed before the {position} position")
        starts[ref] = size
    return starts


def verify_cc_snapshot_source_prefix(
    path: str | Path, session_id: str, snapshot: Mapping[str, Any], *, position: str
) -> None:
    validate_cc_session_snapshot(snapshot)
    edge = _validate_position(snapshot["attribution_boundary"][position], f"{position} position",
                              source_manifest=True)
    if cc_authority_ref(session_id) != snapshot["authority_ref"]:
        raise CcUsageError("CC transcript session does not match the snapshot authority")
    _verified_starts(edge, [(name, _read_source(item)) for name, item in cc_session_sources(path)],
                     session_id, position)


def _private_read(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise CcUsageError("CC capture artifact mode is invalid")
        value = json.load(stream)
    validate_cc_session_snapshot(value)
    return value


def capture_cc_session_snapshot(
    path: str | Path,
    session_id: str,
    binding: CcUsageBinding,
    *,
    output_path: str | Path,
    lifecycle_state: str,
    measurement_role: str,
    baseline_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish one immutable baseline/final under an OS lock; replay the winner."""
    target = Path(output_path)
    state = "baseline" if measurement_role == "baseline" else lifecycle_state
    if (not target.is_absolute() or target.parent.is_symlink()
            or target.name != f"{binding.work_item_id}-{binding.runtime_binding_sha256}-{state}.json"):
        raise CcUsageError("CC capture output does not match its binding and role")
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = target.with_name(target.name + ".capture-lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "rb") as lock:
            deadline = time.monotonic() + 20
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CcUsageError("CC usage capture lock did not converge")
                    time.sleep(0.05)
            previous = _private_read(target) if measurement_role != "progress" else None
            if previous is not None:
                if any(previous.get(key) != getattr(binding, key) for key in (
                        "work_item_id", "agent_id", "epoch", "runtime", "authority_ref",
                        "runtime_binding_sha256")) or previous.get("measurement_role") != measurement_role:
                    raise CcUsageError("CC capture artifact does not match runtime authority")
                verify_cc_snapshot_source_prefix(
                    path, session_id, previous, position="baseline" if measurement_role == "baseline" else "final")
                return {"schema": "cc_atomic_capture/v1", "snapshot": previous, "idempotent_replay": True}
            snapshot = build_cc_session_snapshot(
                path, session_id, binding, lifecycle_state=lifecycle_state,
                measurement_role=measurement_role, baseline_snapshot=baseline_snapshot)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                                 prefix=target.name + ".", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(snapshot, stream, ensure_ascii=False, sort_keys=True, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return {"schema": "cc_atomic_capture/v1", "snapshot": snapshot, "idempotent_replay": False}
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, PiUsageError):
            raise
        raise CcUsageError("CC capture publication or private readback failed") from exc
