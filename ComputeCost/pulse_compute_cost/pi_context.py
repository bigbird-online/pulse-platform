"""Read Pi ancestry and existing Host provenance, never current channel defaults.

The native parentId selects context. A new unannotated input resets provenance;
quotations, assistant text and a neighbouring tenant cannot supply an ingress.
An exclusive Relay route can separately prove a channel from older Host evidence.
No raw prompt, user, tenant or native identity leaves this read-side helper.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from .events import parse_timestamp
from .receipts import ReceiptError, digest, reference

# Advisory prose is not a wire schema. The saved input hash, body, tenant and
# timestamp below remain the authority for this terminal provenance marker.
# Relay appends this fixed line after the provenance reference of an undeclared
# request (Relay/src/session-wi-guard.ts). It is a Relay instruction, not part
# of the human utterance, and must not hide the reference.
SESSION_WI_REMINDER = "[Relay] 本请求尚未调用 declare_session_wi 声明归属。"
# Recorded sessions keep the earlier wordings (before 2026-10-29, then before
# 2026-11-19); all stay recognised.
SESSION_WI_REMINDERS = (SESSION_WI_REMINDER,
                        "[Relay] 本请求尚未声明归属：要改文件、提 PR 或动生产时先调用 declare_session_wi 登记所属 WI；闲聊、只读查询声明 none，不要为此新建 WI。",
                        "[Relay] 本请求尚未声明 WI：先调用 declare_session_wi 登记本请求所属 WI，再继续工作。")
REMINDER_SUFFIX = r"(?:(?:\n\n)?(?:" + "|".join(re.escape(r) for r in SESSION_WI_REMINDERS) + r"))?$"
_INPUT = re.compile(r"\n\n\[Host human input reference: ([a-f0-9]{64})\.[^\]\r\n]*\]" + REMINDER_SUFFIX)
_RECOVERY_PREFIX = "SCL_HOST_RECOVERY_RESULT: Host control result, not a new human instruction."
_RECOVERY_REF = re.compile(r"(?:^|\n)human_input_ref=([a-f0-9]{64})(?:\n|$)")


@dataclass(frozen=True)
class PiContext:
    model: str | None = None
    provider: str | None = None
    model_ref: str | None = None
    channel: str | None = None
    input_ref: str | None = None
    input_boundary_ref: str | None = None
    human_input_claimed: bool = False
    host_input_token: str | None = None


def tenant_directory(pool_key: str) -> str:
    # Relay/src/pi-runtime-config.ts::safeTenantDirectory, not a display name.
    prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", pool_key)[:48] or "tenant"
    return prefix + "-" + hashlib.sha256(pool_key.encode()).hexdigest()[:12]


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(p, Mapping) for p in value):
        if all(p.get("type") == "text" and isinstance(p.get("text"), str) for p in value):
            return "".join(p["text"] for p in value)
    return None


def input_directories(source: Mapping[str, Any]) -> list[Path]:
    """Only explicit current/historical metadata stores; never search ancestors."""
    values = source.get("human_input_directories", [])
    if not isinstance(values, list) or len(values) > 8:
        raise ReceiptError("invalid registered Host input directories")
    values = [source.get("human_input_directory"), *values]
    result = []
    for value in values:
        if value is None:
            continue
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ReceiptError("Host input directory must be explicitly absolute")
        path = Path(value)
        if path not in result:
            result.append(path)
    return result


def input_bytes(source: Mapping[str, Any], token: str) -> bytes | None:
    """Read one exact non-secret provenance record, not any decision key."""
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ReceiptError("invalid Host input reference")
    found = []
    for folder in input_directories(source):
        if (folder.is_symlink() or not folder.is_dir()
                or folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o077):
            raise ReceiptError("registered Host input directory is unsafe")
        path = folder / (token + ".json")
        if not path.exists() and not path.is_symlink():
            continue
        st = path.lstat()
        if (path.is_symlink() or not path.is_file() or st.st_uid != os.getuid()
                or st.st_mode & 0o077 or st.st_size > 1024 * 1024):
            raise ReceiptError("registered Host input evidence is unsafe")
        found.append(path.read_bytes())
    if len({digest(json.loads(data)) for data in found}) > 1:
        raise ReceiptError("conflicting registered copies of the same Host input")
    return found[0] if found else None


def recovery_directories(source: Mapping[str, Any]) -> list[Path]:
    # This is the typed sibling store of each explicitly enrolled Host root.
    return [p.parent / "feishu-recoveries" for p in input_directories(source)
            if p.name == "human-inputs"]


def load_dispatched_recovery(source: Mapping[str, Any], request_id: str) -> Mapping[str, Any] | None:
    """Validate an original typed journal shared by Host and native readers."""
    if not re.fullmatch(r"operation_v1_feishu_recovery_[a-f0-9]{64}", request_id):
        return None
    copies = []
    for folder in recovery_directories(source):
        if not folder.exists() and not folder.is_symlink():
            continue
        st = folder.lstat()
        if folder.is_symlink() or not folder.is_dir() or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ReceiptError("registered Host recovery directory is unsafe")
        path = folder / (request_id + ".json")
        if not path.exists() and not path.is_symlink():
            continue
        st = path.lstat()
        if (path.is_symlink() or not path.is_file() or st.st_uid != os.getuid()
                or st.st_mode & 0o077 or st.st_size > 1024 * 1024):
            raise ReceiptError("registered Host recovery evidence is unsafe")
        copies.append(path.read_bytes())
    if not copies:
        return None
    records = [json.loads(data) for data in copies]
    if len({digest(record) for record in records}) != 1:
        raise ReceiptError("conflicting registered Host recovery copies")
    record = records[0]
    if not isinstance(record, Mapping):
        raise ReceiptError("invalid registered Host recovery")
    wi, pool, token, session = (record.get(k) for k in
                                ("workItemId", "poolKey", "humanInputRef", "sessionId"))
    if (not isinstance(wi, str) or re.fullmatch(r"wi_\d{8}_\d{3}", wi) is None
            or not isinstance(pool, str) or not pool or not isinstance(session, str) or not session
            or not isinstance(token, str) or re.fullmatch(r"[a-f0-9]{64}", token) is None):
        return None
    expected = "operation_v1_feishu_recovery_" + hashlib.sha256(json.dumps(
        [pool, wi, token], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if (record.get("schema") != "pulse_feishu_host_recovery/v1"
            or record.get("requestId") != request_id or request_id != expected
            or record.get("state") != "dispatched" or record.get("dispatching") is not True
            or not isinstance(record.get("result"), Mapping)):
        return None
    return record


def dispatched_recovery(source: Mapping[str, Any], content: str, token: str,
                        pool: str, session: str | None) -> str | None:
    matches = re.findall(r"(?:^|\n)SCL_HOST_RECOVERY_REQUEST=(operation_v1_feishu_recovery_[a-f0-9]{64})$", content)
    payloads = [line for line in content.splitlines() if line.startswith("{")]
    if len(matches) != 1 or len(payloads) != 1 or not session:
        return None
    try:
        payload = json.loads(payloads[0])
    except ValueError:
        return None
    record = load_dispatched_recovery(source, matches[0])
    if (record is None or record["humanInputRef"] != token or record["poolKey"] != pool
            or record["sessionId"] != session or record["result"].get("lifecycleContext") != payload):
        return None
    return reference("native_host_recovery", [matches[0], digest(record), session])


def _matches_input_body(content: str, original: str) -> bool:
    if content == original or content.endswith("\n\n" + original):
        return True
    # Relay's reply wrapper separates the quoted text from the exact new
    # utterance with one newline, not the generic two-newline prefix above.
    quoted, separator, utterance = content.rpartition("\n--- user message ---\n")
    return bool(separator and "\n--- replied message ---\n" in quoted and utterance == original)


class PiAncestry:
    """Transaction-local parsed context for one immutable native source prefix."""
    def __init__(self, path: Path, source: Mapping[str, Any] | None = None):
        self.path = path
        self.session_id: str | None = None
        self.source = source or {}
        self.contexts: dict[str, PiContext] = {}
        self.identities: dict[str, str] = {}
        self.inputs: dict[str, tuple[str, str, str] | None] = {}
        self.pool_inputs: list[tuple[datetime, str, str, str]] | None = None

    def _input(self, row: Mapping[str, Any], inherited: PiContext) -> tuple[str, str, str] | None:
        content = _text(row.get("message", {}).get("content"))
        match = _INPUT.search(content) if content else None
        mode = "human"
        if match:
            token = match[1]
        else:
            refs = _RECOVERY_REF.findall(content or "")
            if (not content or not content.startswith(_RECOVERY_PREFIX) or len(refs) != 1):
                return None
            token = refs[0]
            mode = "dispatched_recovery" if "\nSCL_HOST_RECOVERY_REQUEST=" in content else "recovery"
            if mode == "recovery" and inherited.host_input_token != token:
                return None
        # Bound cache to the exact input, including timestamp and native text.
        cache_key = digest([mode, token, row.get("timestamp"), content])
        if cache_key in self.inputs:
            return self.inputs[cache_key]
        data = input_bytes(self.source, token)
        if data is None:
            self.inputs[cache_key] = None
            return None
        record = json.loads(data)
        origin = record.get("source", {})
        channel, pool = origin.get("channel"), record.get("poolKey")
        values = [channel, origin.get("eventId"), origin.get("humanRef"), pool]
        if (channel not in {"feishu", "telegram"}
                or any(not isinstance(v, str) or not v for v in values)
                or hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() != token):
            raise ReceiptError("Host input identity does not match its native reference")
        milliseconds = record.get("observedAtMs")
        if type(milliseconds) is not int or milliseconds <= 0:
            raise ReceiptError("Host input has an invalid timestamp")
        if self.path.parent.name != tenant_directory(pool):
            return None
        when = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
        if when > parse_timestamp(row.get("timestamp"), "native input timestamp"):
            return None
        if mode == "human":
            original = origin.get("message")
            before = content[:match.start()]
            # A copied footer alone is not authenticated input provenance.
            if not isinstance(original, str) or not _matches_input_body(before, original):
                return None
        basis = [token, hashlib.sha256(data).hexdigest(), reference("native_record", digest(row))]
        if mode == "dispatched_recovery":
            proof = dispatched_recovery(self.source, content, token, pool, self.session_id)
            if proof is None or channel != "feishu":
                return None
            basis.append(proof)
        domain = "native_host_input" if mode == "human" else "native_host_recovery_input"
        result = (channel, reference(domain, basis), token)
        self.inputs[cache_key] = result
        return result

    def _exclusive_pool_input(self, row: Mapping[str, Any]) -> tuple[str, str] | None:
        """Prove the route, not human identity or task authority.

        feishuPoolKey creates user:agent:feishu for dual-channel agents. A
        digest-valid older Host record plus safeTenantDirectory proves this
        exact registered pool, independently of prompt formatting or SCL
        authority. Bare pools, current defaults and text alone prove nothing.
        """
        if not self.source.get("runtime_manifest"):
            return None
        when = parse_timestamp(row.get("timestamp"), "native input timestamp")
        if self.pool_inputs is None:
            records = []
            for directory in input_directories(self.source):
                if (directory.is_symlink() or not directory.is_dir()
                        or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077):
                    raise ReceiptError("registered Host input directory is unsafe")
                for path in sorted(directory.glob("*.json")):
                    if not re.fullmatch(r"[a-f0-9]{64}", path.stem):
                        continue
                    data = input_bytes(self.source, path.stem)
                    if data is None:
                        continue
                    record = json.loads(data)
                    if not isinstance(record, Mapping):
                        raise ReceiptError("Host pool evidence must be an object")
                    pool = record.get("poolKey")
                    if (not isinstance(pool, str) or self.path.parent.name != tenant_directory(pool)):
                        continue
                    parts = pool.rsplit(":", 2)
                    if len(parts) != 3 or not parts[0] or not parts[1] or parts[2] != "feishu":
                        continue
                    origin = record.get("source")
                    if not isinstance(origin, Mapping):
                        raise ReceiptError("Host pool evidence source must be an object")
                    values = [origin.get("channel"), origin.get("eventId"), origin.get("humanRef"), pool]
                    if (any(not isinstance(value, str) or not value for value in values)
                            or hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() != path.stem):
                        raise ReceiptError("Host pool evidence identity mismatch")
                    ms = record.get("observedAtMs")
                    if type(ms) is not int or ms <= 0:
                        raise ReceiptError("Host pool evidence timestamp is invalid")
                    records.append((datetime.fromtimestamp(ms / 1000, timezone.utc), parts[1], values[0],
                                    reference("historical_relay_pool", [path.stem, hashlib.sha256(data).hexdigest()])))
            self.pool_inputs = sorted(set(records))
        witnesses = [r for r in self.pool_inputs if r[0] <= when]
        if not witnesses:
            return None
        if {r[2] for r in witnesses} != {"feishu"}:
            raise ReceiptError("exclusive Relay pool has conflicting historical channels")
        return "feishu", reference("native_input_pool", [witnesses[0][3], reference("native_record", digest(row))])

    def consume(self, row: Mapping[str, Any]) -> PiContext:
        identity = row.get("id")
        if not isinstance(identity, str) or not identity:
            return PiContext()
        fingerprint = digest(row)
        if identity in self.identities:
            if self.identities[identity] != fingerprint:
                raise ReceiptError("Pi entry identity changed inside its native prefix")
            return self.contexts[identity]
        parent = row.get("parentId")
        context = self.contexts.get(parent, PiContext())
        kind = row.get("type")
        if kind == "session":
            if self.session_id not in (None, identity):
                raise ReceiptError("native recovery session identity changed")
            self.session_id = identity
        message = row.get("message", {})
        if kind == "model_change":
            model, provider = row.get("modelId"), row.get("provider")
            if isinstance(model, str) and model and isinstance(provider, str) and provider:
                context = replace(context, model=model, provider=provider,
                                  model_ref=reference("native_record", fingerprint))
        elif kind == "message" and isinstance(message, Mapping):
            if message.get("role") in {"user", "human"}:
                content = _text(message.get("content"))
                claimed = bool(content and ("Host human input reference:" in content
                    or (content.startswith(_RECOVERY_PREFIX) and "human_input_ref=" in content)))
                host_proof = self._input(row, context)
                route_proof = None if host_proof else self._exclusive_pool_input(row)
                proof = host_proof or route_proof
                context = replace(context, channel=proof[0] if proof else None,
                                  input_ref=proof[1] if proof else None,
                                  input_boundary_ref=reference("native_input_boundary", fingerprint),
                                  human_input_claimed=claimed,
                                  host_input_token=host_proof[2] if host_proof else None)
            elif message.get("role") == "assistant":
                model, provider = message.get("model"), message.get("provider")
                if isinstance(model, str) and model and isinstance(provider, str) and provider:
                    context = replace(context, model=model, provider=provider,
                                      model_ref=reference("native_record", fingerprint))
        self.identities[identity] = fingerprint
        self.contexts[identity] = context
        return context


def native_summary_model(row: Mapping[str, Any], context: PiContext) -> tuple[str | None, str | None, str | None]:
    """Only Pi's own summaries inherit native model state; never a hook/tool."""
    model, provider = row.get("model"), row.get("provider")
    if (row.get("type") not in {"compaction", "branch_summary"}
            or row.get("fromHook") is True or not isinstance(row.get("usage"), Mapping)):
        return model, provider, None
    if model is not None:
        return model, provider, None
    if context.model is not None and context.provider is not None and context.model_ref:
        return context.model, context.provider, context.model_ref
    return None, provider, None
