"""Host-only session handover; business decisions remain in the same Kernel.

The direct admitted Relay parent may attest its actual native child. A private
record retains only the original request and native identities across a crash.
It never decides whether transfer committed or grants execution authority.

The original request is either the human's `/wi rollover` channel input or the
holder session's own committed `request_rollover`. For the latter the native
Host leaves a private notice naming that request (`post_agent_request`); Relay
only learns from it which conversation to change, and `prepare` reads the
committed request back from the Kernel before it fences the source.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4
from typing import cast

from . import channel
from .kernel import Kernel
from .model import (
    Command,
    HostContext,
    Json,
    Query,
    Rejected,
    SourceInput,
    digest,
    encoded,
    fields,
    writer_identity,
)
from .native_host import NativeHost, process_chain
from .policy import execution


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise Rejected("host_session_invalid", reason)


def private_json(path: Path, limit: int = 65536) -> Json:
    info = path.stat()
    require(
        path.resolve(strict=True) == path
        and path.is_file()
        and info.st_uid == os.getuid()
        and not info.st_mode & 0o077
        and info.st_size <= limit,
        "Session handover evidence must be bounded and private",
    )
    value = json.loads(path.read_bytes())
    require(isinstance(value, dict), "Invalid session handover evidence")
    return cast(Json, value)


def retain(path: Path, record: Json) -> Json:
    root = path.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    require(
        root.resolve() == root and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "Session handover directory must be private and physical",
    )
    temporary = root / (".handover-" + str(uuid4()))
    try:
        with temporary.open("xb") as out:
            os.chmod(temporary, 0o600)
            out.write(encoded(record))
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return private_json(path)
        fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return record
    finally:
        temporary.unlink(missing_ok=True)


AGENT_REQUEST = "harness.agent-rollover-request/v1"


def agent_request_ref(namespace: str, wi: str, request_id: str) -> str:
    return digest(["agent_rollover", namespace, wi, request_id])


def handover_root(policy: Json) -> Path:
    return Path(policy["human_inputs"]).parent / "native-session-handovers"


def post_agent_request(host: NativeHost, wi: str, request_id: str) -> Json:
    """Name the holder's committed request to the Host session controller.

    The notice grants nothing: `prepare` accepts it only for the same attested
    source process and only after reading the committed request back.
    """
    root = handover_root(host.policy)
    root.mkdir(mode=0o700, exist_ok=True)
    ref = agent_request_ref(host.policy["namespace"], wi, request_id)
    record = {
        "schema": AGENT_REQUEST,
        "ref": ref,
        "wi": wi,
        "request_id": request_id,
        "tenant": host.environment["PULSE_PI_TENANT_ID"],
        "source": host.base.principal(),
        "source_native_id": host.native_header["id"],
    }
    require(
        retain(root / "agent-requests" / (ref + ".json"), record) == record,
        "Another Agent rollover notice already uses this request",
    )
    return {"ref": ref}


def relay_parent(
    policy: Json, proc: Path, caller: int, *, native_accept: bool = False
) -> tuple[int, bool]:
    chain = process_chain(caller, proc)
    require(len(chain) >= 2, "No admitted Relay parent")
    for index, parent in enumerate(chain[1:], 1):
        if index > 1 and not native_accept:
            break
        argv = parent["argv"]
        if (
            parent["uid"] == os.getuid()
            and Path(str(parent.get("executable"))).resolve()
            == Path(policy["producer_node"]).resolve()
            and len(argv) >= 2
            and Path(argv[1]).resolve() == Path(policy["relay_entry"]).resolve()
        ):
            return int(parent["pid"]), index == 1
    raise Rejected(
        "host_session_invalid", "Only the installed Relay may control its native sessions"
    )


def native_child(
    policy_path: Path, pid: object, relay: int, proc: Path, *, pre_model: bool = False
) -> NativeHost:
    require(type(pid) is int and pid > 1, "A live native process is required")
    assert isinstance(pid, int)
    chain = process_chain(pid, proc)
    require(
        any(row["pid"] == relay for row in chain[1:]), "Native session is not this Relay's child"
    )
    host = NativeHost(policy_path, proc=proc, pid=pid, pre_model=pre_model)
    require(host.profile["role"] == "producer", "Session handover cannot change a verifier")
    return host


def run(
    entry: Path, request: Json, *, proc: Path = Path("/proc"), caller: int | None = None
) -> Json:
    fields(request, {"stage", "ref"}, {"pid", "native_id", "tenant"})
    require(
        request["stage"] in {"prepare", "inspect", "accept"}
        and isinstance(request["ref"], str)
        and re.fullmatch(r"[a-f0-9]{64}", request["ref"]) is not None,
        "Invalid session handover action",
    )
    policy_path = entry.parent / "host-policy.json"
    info = policy_path.stat()
    require(
        not policy_path.is_symlink() and info.st_uid == os.getuid() and not info.st_mode & 0o022,
        "Host policy is not operator owned",
    )
    policy = json.loads(policy_path.read_bytes())
    parent, direct = relay_parent(
        policy, proc, caller or os.getpid(), native_accept=request["stage"] == "accept"
    )
    require(execution(policy)["mode"] != "audit", "Installed Host is read-only")
    if "production_ledger" in policy:
        from .production_install import require_active_program

        require_active_program(entry, policy)
    root = handover_root(policy)
    path = root / (request["ref"] + ".json")
    ref = request["ref"]
    native: NativeHost | SimpleNamespace | None = None
    if request["stage"] != "inspect":
        require(direct or "pid" not in request, "A native tool cannot name another process")
        native = (
            retired_source(policy, request["native_id"], request["tenant"])
            if request["stage"] == "prepare"
            and "pid" not in request
            and direct
            and "native_id" in request
            and "tenant" in request
            else native_child(
                policy_path, request.get("pid") if direct else (caller or os.getpid()), parent, proc
            )
        )
    human: Json | None = None
    asked: Json | None = None
    if request["stage"] == "prepare":
        assert native is not None
        human_path = Path(policy["human_inputs"]) / (ref + ".json")
        if human_path.exists():
            human = private_json(human_path)
            source = human["source"]
            require(
                digest([source["channel"], source["eventId"], source["humanRef"], human["poolKey"]])
                == ref
                and human["poolKey"] == native.environment.get("PULSE_PI_TENANT_ID")
                and source["channel"] in {"telegram", "feishu"},
                "Original authenticated channel input differs",
            )
            parsed = channel.parse(source["message"])
            require(
                parsed["action"] == "rollover",
                "Host handover requires the original rollover command",
            )
            tenant = human["poolKey"]
        else:
            asked = private_json(root / "agent-requests" / (ref + ".json"), 4096)
            require(
                asked.get("schema") == AGENT_REQUEST
                and asked.get("ref") == ref
                and isinstance(asked.get("wi"), str)
                and isinstance(asked.get("request_id"), str)
                and agent_request_ref(policy["namespace"], asked["wi"], asked["request_id"]) == ref
                and asked.get("tenant") == native.environment.get("PULSE_PI_TENANT_ID")
                and asked.get("source") == native.base.principal()
                and asked.get("source_native_id") == native.native_header["id"],
                "Original Agent rollover request differs from this source session",
            )
            parsed = {"action": "rollover", "wi": asked["wi"]}
            tenant = asked["tenant"]
        current = native.base
    else:
        record = private_json(path)
        require(
            record["namespace"] == policy["namespace"]
            and record["source"]["surface"] == policy["surface"],
            "Session handover belongs to another installed surface",
        )
        source_principal = record["source"]
        # This narrow read identity comes from an originally attested process.
        # It permits no business write or arbitrary caller-supplied principal.
        current = HostContext(
            policy["namespace"],
            source_principal["actor"],
            source_principal["session"],
            source_principal["surface"],
            0,
            frozenset({record["wi"]}),
            frozenset(),
        )
    from .production_ledger import ProductionLedger

    storage = (
        ProductionLedger(policy["production_ledger"], policy["namespace"])
        if "production_ledger" in policy
        else None
    )
    handle = object()

    def authenticate(given: object) -> HostContext:
        if given is not handle:
            raise PermissionError("Unknown Host session handle")
        return current

    kernel = Kernel(
        None if storage else Path(policy["database"]),
        authenticate,
        storage=storage,
        writer_id=writer_identity(hashlib.sha256(entry.read_bytes()).hexdigest(), policy),
        pages=Path(policy["views_root"]) if policy.get("views_root") else None,
    )
    if request["stage"] == "prepare":
        assert native is not None
        from .cli import bound_work_item

        if path.exists():
            retained = private_json(path)
            wi = retained["wi"]
            require(parsed.get("wi") in (None, wi), "Original WorkItem changed")
        else:
            wi = parsed.get("wi") or bound_work_item(kernel, handle)
        if asked is not None:
            parsed["text"] = agent_summary(kernel, handle, wi, asked, native.base.principal())
        proposed = {
            "schema": "harness.native-session-handover/v1",
            "namespace": policy["namespace"],
            "source": native.base.principal(),
            "source_native_id": native.native_header["id"],
            "source_file": str(native.session_file),
            "runtime": native.runtime,
            "tenant": tenant,
            "wi": wi,
            "ref": ref,
            "summary": parsed["text"],
            "target_session_id": str(uuid4()),
            **({"agent_request": asked["request_id"]} if asked is not None else {}),
        }
        record = retain(path, proposed)
        require(
            all(record.get(k) == v for k, v in proposed.items() if k != "target_session_id"),
            "Original session handover request changed",
        )
    wi = record["wi"]
    original = kernel.inspect(Query(wi, view="request", request_id="channel." + ref), handle)
    if original["status"] == "rejected":
        return original
    prepared = original["result"]["original_request_result"]
    if request["stage"] == "prepare" and prepared is None:
        assert native is not None
        current = replace(
            native.base,
            generation=original["work_item"]["generation"],
            allowed_intents=frozenset({"prepare_rollover"}),
            session_handover={
                "source": native.base.principal(),
                **({"agent_request": record["agent_request"]} if "agent_request" in record else {}),
            },
            source=None if human is None else SourceInput(ref, "native_human", encoded(human)),
        )
        original = kernel.execute(
            Command("channel." + ref, "prepare_rollover", {"summary": record["summary"]}, wi),
            handle,
        )
        prepared = original.get("result", {}).get("original_request_result")
    if request["stage"] == "accept":
        assert native is not None
        require(
            prepared is not None and isinstance(prepared.get("transfer_id"), str),
            "Original transfer has no committed source decision",
        )
        require(
            native.base.actor == record["source"]["actor"]
            and native.base.surface == record["source"]["surface"]
            and native.environment.get("PULSE_PI_TENANT_ID") == record["tenant"]
            and native.native_header["id"] != record["source_native_id"]
            and native.runtime == record["runtime"],
            "Successor does not match the original conversation",
        )
        require(
            native.runtime == "codex" or native.native_header["id"] == record["target_session_id"],
            "Actual successor differs from the one Host allocation",
        )
        target = {"native_id": native.native_header["id"], "principal": native.base.principal()}
        require(
            retain(root / (ref + ".target.json"), target) == target,
            "Another actual successor is already retained; read back that original transfer",
        )
        current = replace(
            native.base,
            generation=original["work_item"]["generation"],
            session_handover={"source": record["source"], "transfer_id": prepared["transfer_id"]},
        )
        original = kernel.execute(
            Command(
                "channel.accept." + ref,
                "accept_transfer",
                {"transfer_id": prepared["transfer_id"]},
                wi,
            ),
            handle,
        )
    accepted = kernel.inspect(Query(wi, view="request", request_id="channel.accept." + ref), handle)
    try:
        presence = target_presence(policy, record)
    except (Rejected, OSError, ValueError) as exc:
        # A physical observation failure cannot erase a committed decision.
        presence = {
            "target_session_exists": None,
            "target_native_id": None,
            "target_presence_error": exc.code
            if isinstance(exc, Rejected)
            else "source_unavailable",
        }
    return {
        **original,
        "session_handover": {
            "ref": ref,
            "wi": wi,
            "source_native_id": record["source_native_id"],
            "target_session_id": record["target_session_id"],
            "runtime": record["runtime"],
            "prepared": bool(prepared and prepared.get("transfer_id")),
            **presence,
            "accepted": accepted.get("result", {}).get("request_observation", {}).get("state")
            == "committed",
        },
    }


def agent_summary(kernel: Kernel, handle: object, wi: str, asked: Json, source: Json) -> str:
    """The committed request of this very source session, read back from the Kernel."""
    view = kernel.inspect(Query(wi, view="request", request_id=asked["request_id"]), handle)
    committed = (view.get("result") or {}).get("original_request_result") or {}
    request = committed.get("rollover_request")
    summary = request.get("summary") if isinstance(request, dict) else None
    require(
        view.get("status") != "rejected"
        and isinstance(request, dict)
        and request.get("source") == source
        and isinstance(summary, str),
        "The Agent rollover request was not committed by this source session",
    )
    return str(summary)


def retired_source(policy: Json, native_id: str, tenant: str) -> SimpleNamespace:
    """Release-only identity from prior Relay observation and the original log.

    A CLI engine may have ended while its native conversation remains current.
    This is never accepted by NativeHost for ordinary execution or human review.
    """
    from .session_sources import source_files

    require(
        isinstance(native_id, str)
        and re.fullmatch(r"[A-Za-z0-9-]{1,128}", native_id) is not None
        and isinstance(tenant, str)
        and bool(tenant),
        "Invalid retained native identity",
    )
    matches: list[tuple[Json, Path, str]] = []
    for profile in policy["profiles"]:
        if profile.get("runtime") not in {"cc", "codex"} or not profile.get("binding_root"):
            continue
        if profile.get("relay_tenants") and tenant not in profile["relay_tenants"]:
            continue
        if not Path(profile["session_root"]).is_dir():
            continue
        roots = Path(profile["binding_root"])
        for path in roots.glob("[0-9]*.json"):
            proof = private_json(path, 4096)
            if (
                proof.get("schema") != "pulse.native-session-source/v1"
                or proof.get("runtime") != profile["runtime"]
                or proof.get("session_id") != native_id
                or proof.get("tenant") != tenant
            ):
                continue
            for source_file in source_files(profile, native_id):
                match = (profile, source_file, proof.get("entry", "relay"))
                if not any(
                    old[0]["actor"] == profile["actor"] and old[1] == source_file for old in matches
                ):
                    matches.append(match)
    require(
        len(matches) == 1,
        "No unique original native session observation; original conversation is preserved",
    )
    profile, session_file, entry = matches[0]
    require(profile["role"] == "producer", "A verifier cannot become a producer by handover")
    base = HostContext(
        policy["namespace"],
        profile["actor"],
        "session." + digest([policy["namespace"], profile["actor"], native_id, policy["surface"]]),
        policy["surface"],
        0,
        frozenset({"*"}),
        frozenset({"prepare_rollover"}),
    )
    return SimpleNamespace(
        base=base,
        profile=profile,
        runtime=profile["runtime"],
        entry=entry,
        native_header={"id": native_id},
        session_file=session_file,
        environment={"PULSE_PI_TENANT_ID": tenant},
    )


def target_presence(policy: Json, record: Json) -> Json:
    """Physical source presence determines resume vs first launch, never a grant."""
    from .session_sources import source_files

    files: dict[str, str] = {}
    for profile in policy["profiles"]:
        if (
            profile["role"] != "producer"
            or profile["actor"] != record["source"]["actor"]
            or profile.get("runtime", "pi") != record["runtime"]
        ):
            continue
        native_ids = [record["target_session_id"]]
        if record["runtime"] == "codex":
            native_ids = []
            if profile.get("binding_root"):
                for path in Path(profile["binding_root"]).glob("[0-9]*.json"):
                    proof = private_json(path, 4096)
                    if (
                        proof.get("schema") == "pulse.native-session-source/v1"
                        and proof.get("runtime") == "codex"
                        and proof.get("tenant") == record["tenant"]
                        and proof.get("allocation_id") == record["target_session_id"]
                    ):
                        native_ids.append(proof["session_id"])
        for native_id in native_ids:
            if record["runtime"] == "pi":
                root = Path(profile["session_root"])
                for path in root.rglob("*_" + native_id + ".jsonl"):
                    require(
                        path.resolve() == path and path.stat().st_uid == os.getuid(),
                        "Native successor source redirected",
                    )
                    with path.open() as stream:
                        header = json.loads(stream.readline(65537))
                    require(
                        header.get("type") == "session" and header.get("id") == native_id,
                        "Native successor source identity changed",
                    )
                    files[str(path)] = native_id
            else:
                for path in source_files(profile, native_id):
                    files[str(path)] = native_id
    require(len(files) <= 1, "Native successor source is ambiguous")
    from .session_sources import rows, user_text
    marker = "[Host continuation operation: " + record["ref"] + "]"
    bootstrap = any(
        marker in text
        for name in files
        for _, row in rows(Path(name))
        if (text := user_text(row, record["runtime"])) is not None
    )
    return {
        "target_session_exists": bool(files),
        "target_native_id": next(iter(files.values()), None),
        "target_bootstrap_observed": bootstrap,
    }
