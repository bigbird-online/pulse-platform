"""Privacy-safe SCL-to-runtime usage binding contract.

The host, not the model or usage adapter, owns attribution. A managed v3 binding
carries durable WI/Agent/surface/epoch metadata, opaque Session authority, and an
explicit procurement route; provider
session, thread, message, credential, path, and content identities are forbidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


RUNTIME_USAGE_BINDING_SCHEMA_V1 = "pulse_runtime_usage_binding/v1"
RUNTIME_USAGE_BINDING_SCHEMA_V2 = "pulse_runtime_usage_binding/v2"
RUNTIME_USAGE_BINDING_SCHEMA = "pulse_runtime_usage_binding/v3"
RUNTIME_USAGE_BINDING_ENV = "PULSE_RUNTIME_USAGE_BINDING"
RUNTIME_USAGE_BINDING_FILE_ENV = "PULSE_RUNTIME_USAGE_BINDING_FILE"
_WI_RE = re.compile(r"^wi_\d{8}_\d{3}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AUTHORITY_RE = re.compile(r"^session_v1_[0-9a-f]{64}$")
_SURFACE_RE = re.compile(r"^surface_v1_[a-z0-9_]{1,96}$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_ALLOWED_RUNTIMES = {
    "pi",
    "codex",
    "chatgpt_work",
    "chatgpt_web",
    "cc",
    "cc_sdk",
}
_ALLOWED_PROCUREMENT = {
    "chatgpt_subscription",
    "openrouter_api",
    "claude_subscription",
    "cursor_subscription",
    "unknown",
}
_FIELDS_V1 = {
    "schema",
    "work_item_id",
    "agent_id",
    "phase",
    "epoch",
    "runtime",
    "procurement_source",
    "issued_at",
    "binding_sha256",
}
_FIELDS_V2 = _FIELDS_V1 | {"authority_ref"}
_FIELDS_V3 = _FIELDS_V2 | {"execution_surface"}
_FORBIDDEN_KEY_PARTS = {
    "session",
    "thread",
    "turn",
    "message",
    "prompt",
    "response",
    "credential",
    "secret",
    "token",
    "path",
    "cwd",
}


class RuntimeUsageBindingError(ValueError):
    """Raised when host attribution is absent, ambiguous, or unsafe."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _text(value: object, field: str, *, pattern: re.Pattern[str] = _TOKEN_RE) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value.strip()) is None:
        raise RuntimeUsageBindingError(f"{field} is invalid")
    return value.strip()


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise RuntimeUsageBindingError("issued_at must be canonical UTC with Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeUsageBindingError("issued_at must be canonical UTC with Z") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RuntimeUsageBindingError("issued_at must be canonical UTC with Z")
    # Preserve the producer's signed precision. JavaScript emits milliseconds
    # while Python emits microseconds; both are canonical UTC and the original
    # text is part of the binding digest.
    return value


def _reject_forbidden_keys(value: object) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                normalized = str(key).lower().replace("-", "_")
                if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                    raise RuntimeUsageBindingError(
                        f"runtime usage binding contains forbidden field: {key}"
                    )
                pending.append(item)
        elif isinstance(current, list):
            pending.extend(current)


@dataclass(frozen=True)
class RuntimeUsageBinding:
    schema: str
    work_item_id: str
    agent_id: str
    phase: str
    epoch: int
    runtime: str
    procurement_source: str
    issued_at: str
    binding_sha256: str
    authority_ref: str | None = None
    execution_surface: str | None = None

    @classmethod
    def from_mapping(cls, value: object) -> "RuntimeUsageBinding":
        if not isinstance(value, Mapping):
            raise RuntimeUsageBindingError("runtime usage binding must be an object")
        schema = value.get("schema")
        expected_fields = (
            _FIELDS_V3 if schema == RUNTIME_USAGE_BINDING_SCHEMA
            else _FIELDS_V2 if schema == RUNTIME_USAGE_BINDING_SCHEMA_V2
            else _FIELDS_V1 if schema == RUNTIME_USAGE_BINDING_SCHEMA_V1
            else None
        )
        if expected_fields is None:
            raise RuntimeUsageBindingError("unsupported runtime usage binding schema")
        if set(value) != expected_fields:
            raise RuntimeUsageBindingError("runtime usage binding fields are invalid")
        _reject_forbidden_keys({key: item for key, item in value.items() if key != "schema"})
        work_item_id = _text(value.get("work_item_id"), "work_item_id", pattern=_WI_RE)
        agent_id = _text(value.get("agent_id"), "agent_id")
        phase = _text(value.get("phase"), "phase")
        epoch = value.get("epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            raise RuntimeUsageBindingError("epoch must be a positive integer")
        runtime = _text(value.get("runtime"), "runtime")
        if runtime not in _ALLOWED_RUNTIMES:
            raise RuntimeUsageBindingError("runtime is unsupported")
        procurement = _text(value.get("procurement_source"), "procurement_source")
        if procurement not in _ALLOWED_PROCUREMENT:
            raise RuntimeUsageBindingError("procurement_source is unsupported")
        issued_at = _timestamp(value.get("issued_at"))
        authority_ref = value.get("authority_ref")
        if schema in {RUNTIME_USAGE_BINDING_SCHEMA, RUNTIME_USAGE_BINDING_SCHEMA_V2}:
            if not isinstance(authority_ref, str) or _AUTHORITY_RE.fullmatch(authority_ref) is None:
                raise RuntimeUsageBindingError("authority_ref is invalid")
        else:
            authority_ref = None
        execution_surface = value.get("execution_surface")
        if schema == RUNTIME_USAGE_BINDING_SCHEMA:
            if not isinstance(execution_surface, str) or _SURFACE_RE.fullmatch(execution_surface) is None:
                raise RuntimeUsageBindingError("execution_surface is invalid")
        else:
            execution_surface = None
        digest = value.get("binding_sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise RuntimeUsageBindingError("binding_sha256 is invalid")
        unsigned = {key: item for key, item in value.items() if key != "binding_sha256"}
        if sha256(_canonical(unsigned)).hexdigest() != digest:
            raise RuntimeUsageBindingError("runtime usage binding digest does not match")
        return cls(
            schema=str(schema),
            work_item_id=work_item_id,
            agent_id=agent_id,
            phase=phase,
            epoch=epoch,
            runtime=runtime,
            procurement_source=procurement,
            issued_at=issued_at,
            binding_sha256=digest,
            authority_ref=authority_ref,
            execution_surface=execution_surface,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "work_item_id": self.work_item_id,
            "agent_id": self.agent_id,
            "phase": self.phase,
            "epoch": self.epoch,
            "runtime": self.runtime,
            "procurement_source": self.procurement_source,
            "issued_at": self.issued_at,
            **({"authority_ref": self.authority_ref} if self.authority_ref is not None else {}),
            **({"execution_surface": self.execution_surface} if self.execution_surface is not None else {}),
            "binding_sha256": self.binding_sha256,
        }


def build_runtime_usage_binding(
    *,
    work_item_id: str,
    agent_id: str,
    phase: str,
    epoch: int,
    runtime: str,
    procurement_source: str,
    authority_ref: str | None = None,
    execution_surface: str | None = None,
    issued_at: str | None = None,
) -> RuntimeUsageBinding:
    if execution_surface is not None and authority_ref is None:
        raise RuntimeUsageBindingError("execution_surface requires runtime authority")
    schema = (
        RUNTIME_USAGE_BINDING_SCHEMA
        if execution_surface is not None
        else RUNTIME_USAGE_BINDING_SCHEMA_V2
        if authority_ref is not None
        else RUNTIME_USAGE_BINDING_SCHEMA_V1
    )
    unsigned: dict[str, Any] = {
        "schema": schema,
        "work_item_id": work_item_id,
        "agent_id": agent_id,
        "phase": phase,
        "epoch": epoch,
        "runtime": runtime,
        "procurement_source": procurement_source,
        "issued_at": issued_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **({"authority_ref": authority_ref} if authority_ref is not None else {}),
        **({"execution_surface": execution_surface} if execution_surface is not None else {}),
    }
    return RuntimeUsageBinding.from_mapping(
        {**unsigned, "binding_sha256": sha256(_canonical(unsigned)).hexdigest()}
    )


def load_runtime_usage_binding(
    env: Mapping[str, str] | None = None,
) -> RuntimeUsageBinding:
    source = os.environ if env is None else env
    inline = source.get(RUNTIME_USAGE_BINDING_ENV)
    path_text = source.get(RUNTIME_USAGE_BINDING_FILE_ENV)
    if bool(inline) == bool(path_text):
        raise RuntimeUsageBindingError(
            "host must inject exactly one runtime usage binding source"
        )
    try:
        if inline:
            value = json.loads(inline)
        else:
            path = Path(str(path_text))
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise RuntimeUsageBindingError(
                    "runtime usage binding file must be an absolute regular file"
                )
            value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeUsageBindingError("runtime usage binding is unreadable") from exc
    return RuntimeUsageBinding.from_mapping(value)


def write_runtime_usage_binding(path: str | Path, binding: RuntimeUsageBinding) -> None:
    """Atomically publish a host-owned binding without exposing private identity."""

    target = Path(path)
    if not target.is_absolute():
        raise RuntimeUsageBindingError("runtime usage binding output must be absolute")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(binding.to_dict(), handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


CANONICAL_MEASUREMENT_BASELINE_ENV = "PULSE_CANONICAL_MEASUREMENT_BASELINE_REFERENCE"

def load_measurement_baseline_reference(
    binding: RuntimeUsageBinding, environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Validate an accepted measurement origin without reconstructing its binding.

    The native adapter also verifies the canonical open fact and immutable private
    baseline. This parser does not manufacture or rewrite attribution.
    """
    raw = (os.environ if environ is None else environ).get(CANONICAL_MEASUREMENT_BASELINE_ENV)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeUsageBindingError("canonical measurement origin is invalid") from exc
    fields = {"schema", "status", "work_item_id", "agent_id", "epoch", "phase",
              "runtime", "procurement_source", "authority_ref", "execution_surface",
              "measurement_epoch_ref", "baseline_binding_sha256", "baseline_sha256",
              "active_runtime_binding_sha256"}
    if (not isinstance(value, dict) or set(value) != fields
        or value.get("schema") != "pulse_canonical_measurement_baseline_reference/v1"
        or value.get("status") != "open" or binding.schema != RUNTIME_USAGE_BINDING_SCHEMA
        or binding.runtime != "pi" or isinstance(value.get("epoch"), bool)
        or not isinstance(value.get("epoch"), (int, float))
        or any(value.get(k) != getattr(binding, k) for k in
               ("work_item_id", "agent_id", "epoch", "phase", "runtime",
                "procurement_source", "authority_ref", "execution_surface"))
        or value.get("active_runtime_binding_sha256") != binding.binding_sha256
        or re.fullmatch(r"measurement_epoch_v1_[0-9a-f]{64}", str(value.get("measurement_epoch_ref"))) is None
        or any(_SHA256_RE.fullmatch(str(value.get(k))) is None
               for k in ("baseline_binding_sha256", "baseline_sha256"))):
        raise RuntimeUsageBindingError("canonical measurement origin does not match this runtime")
    return value
