"""External entry sessions bound by the owner's platform confirmation (AH-083, AH-084).

An external entry such as ChatGPT web reaches this Host through a local command
tool and has no local runtime transcript. The owner confirms on the PULSE
platform bot which Agent the session works as; the entry launcher keeps the
session log; human decisions come only from platform confirmations bound to the
exact command. The launcher trusts the local account, like a standalone Pi.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, cast

from .model import Json, Rejected, digest, encoded, identity

BINDING = "pulse.entry-binding/v1"
REQUEST = "pulse.platform-request/v1"
CONFIRMATION = "pulse.platform-confirmation/v1"
REQUEST_TTL_SECONDS = 30 * 60
SUMMARY_BYTES = 2000
ID = re.compile(r"[a-f0-9]{32}")


def settings(policy: Json) -> Json:
    value = policy.get("external_entries")
    if value is None:
        raise Rejected("authority_denied", "This Host has no external entries")
    return cast(Json, value)


def shape(value: Any) -> None:
    """Policy shape: {root, owner, entries}; checked by policy.shape."""
    if not isinstance(value, dict) or set(value) != {"root", "owner", "entries"}:
        raise ValueError("External entries require root, owner and entries")
    root = Path(value["root"]) if isinstance(value["root"], str) else None
    if root is None or not root.is_absolute() or root.resolve() != root:
        raise ValueError("External entry root redirects")
    if not isinstance(value["owner"], str) or not re.fullmatch(r"[0-9]{1,20}", value["owner"]):
        raise ValueError("External entry owner is the owner's platform account")
    entries = value["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 16 or len(set(entries)) != len(entries):
        raise ValueError("External entries require a bounded unique list")
    for entry in entries:
        identity(entry)


def _private(path: Path, limit: int = 64 * 1024) -> Json:
    if path.is_symlink():
        raise Rejected("authority_denied", "External entry record cannot redirect")
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise Rejected("source_unavailable", "External entry record is unavailable") from exc
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
        raise Rejected("authority_denied", "External entry record is not private")
    return cast(Json, json.loads(path.read_bytes()))


def _write(path: Path, value: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + os.urandom(8).hex())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def directory(policy: Json, name: str) -> Path:
    return Path(settings(policy)["root"]) / name


def profile(policy: Json, actor: str) -> Json:
    """The Agent's producer scope, joined over its runtime profiles."""
    rows = [p for p in policy["profiles"] if p["actor"] == actor and p["role"] == "producer"]
    if not rows:
        raise Rejected("authority_denied", "External entry Agent is not a registered producer")
    artifact = sorted({r for p in rows for r in p["artifact_roots"]})
    workspace = sorted({r for p in rows for r in p.get("workspace_roots", [])})
    return {
        "role": "producer",
        "actor": actor,
        "runtime": "external",
        "session_root": str(directory(policy, "sessions")),
        "artifact_roots": artifact,
        **({"workspace_roots": workspace} if workspace else {}),
    }


def _confirmation(policy: Json, ref: str, pool: str) -> tuple[bytes, Json]:
    """The owner's platform confirmation in Host custody, bound to one pool key."""
    if not re.fullmatch(r"[a-f0-9]{64}", ref):
        raise Rejected("source_invalid", "Expected a platform confirmation reference")
    path = Path(policy["human_inputs"]) / (ref + ".json")
    if path.is_symlink():
        raise Rejected("source_invalid", "Human provenance cannot redirect")
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise Rejected("human_input_unavailable", "Platform confirmation is not in Host custody") from exc
    record = json.loads(raw)
    source = record["source"]
    if (
        digest([source["channel"], source["eventId"], source["humanRef"], record["poolKey"]]) != ref
        or source["channel"] != "telegram"
        or record["poolKey"] != pool
        or source["humanRef"] != settings(policy)["owner"]
    ):
        raise Rejected("source_invalid", "Not the owner's platform confirmation for this session")
    message = json.loads(source["message"])
    if message.get("schema") != CONFIRMATION:
        raise Rejected("source_invalid", "Not a platform confirmation")
    return raw, message


def binding(policy: Json, binding_id: str) -> Json:
    if not isinstance(binding_id, str) or not ID.fullmatch(binding_id):
        raise Rejected("invalid_input", "Expected an external entry binding")
    record = _private(directory(policy, "bindings") / (binding_id + ".json"))
    if record.get("schema") != BINDING or record.get("binding_id") != binding_id:
        raise Rejected("authority_denied", "External entry binding is invalid")
    if record.get("revoked_at"):
        raise Rejected("authority_denied", "The owner revoked this external entry binding")
    _, message = _confirmation(
        policy, record["human_input_ref"], "entry-binding:" + record["request_id"]
    )
    if (
        message.get("kind") != "binding"
        or message.get("request_id") != record["request_id"]
        or message.get("entry") != record["entry"]
        or message.get("actor") != record["actor"]
        or record["entry"] not in settings(policy)["entries"]
    ):
        raise Rejected("authority_denied", "External entry binding differs from its confirmation")
    profile(policy, record["actor"])
    return record


def session_log(policy: Json, binding_id: str) -> Path:
    return directory(policy, "sessions") / (binding_id + ".jsonl")


def locate(
    policy: Json, chain: list[Json], entry: Path, *, nearer_than: int | None = None
) -> Json | None:
    """The entry launcher in this invocation's ancestry, and its confirmed binding.

    With `nearer_than`, only the first that many processes can be the launcher.
    """
    if "external_entries" not in policy:
        return None
    python = Path(policy["python"]).resolve()
    for row in chain[:nearer_than]:
        args = row["argv"]
        if row["uid"] != os.getuid():
            continue
        programs = [
            i
            for i, value in enumerate(args[:8])
            if value.startswith("/") and Path(value).resolve() == entry
        ]
        if not programs or args[programs[0] + 1 : programs[0] + 2] != ["entry"]:
            continue
        if not (
            row.get("executable") == str(python) or (args and Path(args[0]).resolve() == python)
        ):
            raise Rejected("authority_denied", "Entry launcher is not the installed Python")
        binding_id = args[programs[0] + 2] if len(args) > programs[0] + 2 else ""
        record = binding(policy, binding_id)
        log = session_log(policy, binding_id)
        if not log.is_file():
            raise Rejected("source_unavailable", "External entry session log is unavailable")
        return {
            "process": row,
            "profile": profile(policy, record["actor"]),
            "header": {"type": "session", "id": binding_id},
            "runtime": "external",
            "environment": {},
            "entry": record["entry"],
            "path": log.resolve(),
            "binding": record,
        }
    return None


def human_input(policy: Json, binding_id: str, ref: str) -> bytes:
    return _confirmation(policy, ref, "entry:" + binding_id)[0]


def confirmed_content(intent: str, wi: str | None, data: Json) -> str:
    """Digest of what the owner confirms.

    A plan names its own confirmation in `graph.confirmation_ref`, which exists
    only after the owner confirms. That one slot is excluded here and bound at
    use: it must equal the confirmation that covered everything else.
    """
    graph = data.get("graph") if isinstance(data, dict) else None
    if isinstance(graph, dict) and "confirmation_ref" in graph:
        data = {**data, "graph": {k: v for k, v in graph.items() if k != "confirmation_ref"}}
    return digest({"intent": intent, "work_item_id": wi, "input": data})


def confirmed_command(policy: Json, binding_id: str, ref: str, intent: str, wi: str | None, data: Json) -> None:
    """The confirmation covers exactly this command; any other content needs a new one."""
    _, message = _confirmation(policy, ref, "entry:" + binding_id)
    graph = data.get("graph") if isinstance(data, dict) else None
    if (
        message.get("kind") != "decision"
        or message.get("binding_id") != binding_id
        or message.get("intent") != intent
        or message.get("work_item_id") != wi
        or message.get("input_digest") != confirmed_content(intent, wi, data)
        or (isinstance(graph, dict) and "confirmation_ref" in graph and graph["confirmation_ref"] != ref)
    ):
        raise Rejected("source_invalid", "The platform confirmation covers different content")


def _summary(intent: str, wi: str | None, data: Json) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)
    body = text.encode()[:SUMMARY_BYTES].decode(errors="ignore")
    return f"{intent} {wi or ''}\n{body}".strip()


def request(policy: Json, value: Json) -> str:
    request_id = os.urandom(16).hex()
    _write(
        directory(policy, "requests") / (request_id + ".json"),
        {"schema": REQUEST, "request_id": request_id, "created_at": _now(), **value},
    )
    return request_id


def request_binding(policy: Json, entry: str, actor: str) -> str:
    if entry not in settings(policy)["entries"]:
        raise Rejected("invalid_input", "Unknown external entry")
    profile(policy, actor)
    return request(policy, {"kind": "binding", "entry": entry, "actor": actor})


def request_decision(policy: Json, binding_id: str, actor: str, entry: str, intent: str, wi: str | None, data: Json) -> str:
    return request(
        policy,
        {
            "kind": "decision",
            "binding_id": binding_id,
            "actor": actor,
            "entry": entry,
            "intent": intent,
            "work_item_id": wi,
            "input_digest": confirmed_content(intent, wi, data),
            "summary": _summary(intent, wi, data),
        },
    )


def wait(policy: Json, request_id: str, timeout: float) -> Json:
    """The platform result for one request; pending when the owner has not answered."""
    if not ID.fullmatch(request_id):
        raise Rejected("invalid_input", "Expected a platform request")
    path = directory(policy, "requests") / (request_id + ".result.json")
    deadline = time.monotonic() + max(0.0, min(timeout, REQUEST_TTL_SECONDS))
    while True:
        if path.exists():
            return _private(path)
        if time.monotonic() >= deadline:
            return {"status": "pending", "request_id": request_id}
        time.sleep(1)


def launch(entry: Path, policy: Json, argv: list[str]) -> int:
    """`entry BINDING -- ACTION ...`: log the command, then run it under this launcher."""
    if len(argv) < 3 or argv[1] != "--":
        raise Rejected("invalid_input", "Expected entry BINDING -- ACTION [ARGS]")
    binding_id, arguments = argv[0], argv[2:]
    record = binding(policy, binding_id)
    log = session_log(policy, binding_id)
    log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(encoded({"type": "session", "id": binding_id, "entry": record["entry"]}) + b"\n")
        stream.write(encoded({"type": "agent_command", "at": _now(), "argv": arguments}) + b"\n")
    return subprocess.run([sys.executable, "-I", "-B", str(entry), *arguments]).returncode


def _policy(entry: Path) -> Json:
    path = entry.parent / "host-policy.json"
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022:
        raise Rejected("authority_denied", "Installed native policy is not protected")
    policy = json.loads(path.read_bytes())
    if "production_ledger" in policy:
        from .production_install import require_active_program

        require_active_program(entry, policy)
    return cast(Json, policy)


def _option(argv: list[str], name: str, default: str | None = None) -> str | None:
    if name in argv:
        index = argv.index(name)
        if index + 1 >= len(argv):
            raise Rejected("invalid_input", "Missing value for " + name)
        return argv[index + 1]
    return default


def command(entry: Path, action: str, argv: list[str]) -> int:
    """External entry commands; results are machine JSON on stdout."""
    try:
        policy = _policy(entry)
        if action == "entry":
            return launch(entry, policy, argv)
        if action == "entry-request":
            # Anyone local may ask; only the owner's confirmation creates a binding.
            request_id = request_binding(
                policy, _option(argv, "--entry") or "", _option(argv, "--actor") or ""
            )
            result = wait(policy, request_id, float(_option(argv, "--wait", "0") or 0))
        elif action == "entry-wait":
            result = wait(policy, argv[0] if argv else "", float(_option(argv, "--timeout", "0") or 0))
        else:  # entry-confirm, only inside a bound entry session
            from .native_host import NativeHost

            host = NativeHost(entry.parent / "host-policy.json")
            if host.runtime != "external":
                raise Rejected("authority_denied", "Platform confirmation is for external entry sessions")
            path = _option(argv, "--input")
            if not path:
                raise Rejected("invalid_input", "Expected --input with intent, work_item_id and input")
            value = json.loads(Path(path).read_bytes())
            if not isinstance(value, dict) or set(value) != {"intent", "work_item_id", "input"}:
                raise Rejected("invalid_input", "Expected intent, work_item_id and input")
            request_id = request_decision(
                policy,
                host.native_header["id"],
                host.profile["actor"],
                host.entry,
                value["intent"],
                value["work_item_id"],
                value["input"],
            )
            result = wait(policy, request_id, float(_option(argv, "--timeout", "600") or 600))
    except Rejected as exc:
        print(json.dumps({"status": "rejected", "error": {"code": exc.code, "reason": exc.reason}}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
