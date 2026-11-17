"""Host side of an Agent's own `/wi switch` (`self_switch.request_switch`).

After the holder session commits `request_switch`, the native Host leaves a
private notice `native-session-handovers/agent-switches/<ref>.json`, with
`ref = digest(["agent_switch", namespace, wi, request_id])`. Relay learns from
it only which conversation to move; it then runs the same `relay-channel`
leave and enter as for `/wi switch`, passing the notice ref instead of a human
input ref. Leave requires the attested source session itself; enter requires
the same Agent and conversation. Both read the committed request back from the
kernel, so the notice grants nothing.
"""

from __future__ import annotations

from pathlib import Path

from .kernel import Kernel
from .model import Json, Query, Rejected, digest
from .native_host import NativeHost
from .relay_session import handover_root, private_json, require, retain

SCHEMA = "harness.agent-switch-request/v1"


def notice_ref(namespace: str, wi: str, request_id: str) -> str:
    return digest(["agent_switch", namespace, wi, request_id])


def notice_path(policy: Json, ref: str) -> Path:
    return handover_root(policy) / "agent-switches" / (ref + ".json")


def post(host: NativeHost, wi: str, request_id: str, target: str) -> Json:
    """Name the holder's committed switch request to Relay's session controller."""
    root = handover_root(host.policy)
    root.mkdir(mode=0o700, exist_ok=True)
    ref = notice_ref(host.policy["namespace"], wi, request_id)
    record = {
        "schema": SCHEMA,
        "ref": ref,
        "wi": wi,
        "target": target,
        "request_id": request_id,
        "tenant": host.environment["PULSE_PI_TENANT_ID"],
        "source": host.base.principal(),
        "source_native_id": host.native_header["id"],
    }
    require(
        retain(notice_path(host.policy, ref), record) == record,
        "Another Agent switch notice already uses this request",
    )
    return {"ref": ref}


def read(policy: Json, ref: str) -> Json | None:
    """The notice for `ref`, or None when there is none."""
    path = notice_path(policy, ref)
    if not path.exists():
        return None
    return private_json(path, 4096)


def check(notice: Json, policy: Json, host: NativeHost, phase: str | None) -> None:
    """The notice belongs to this conversation; leave runs only in the source session."""
    source = notice.get("source")
    require(
        phase in ("leave", "enter", "readback")
        and notice.get("schema") == SCHEMA
        and all(isinstance(notice.get(k), str) for k in ("ref", "wi", "target", "request_id", "tenant"))
        and isinstance(source, dict)
        and notice["ref"] == notice_ref(policy["namespace"], notice["wi"], notice["request_id"])
        and notice["tenant"] == host.environment.get("PULSE_PI_TENANT_ID")
        and source.get("actor") == host.base.actor,
        "Agent switch notice differs from this conversation",
    )
    if phase == "leave":
        require(
            source == host.base.principal()
            and notice.get("source_native_id") == host.native_header["id"],
            "Only the session that asked to switch can leave",
        )


def committed(kernel: Kernel, handle: object, notice: Json) -> Json:
    """The committed request, read back; returns the proof `continue_here` accepts."""
    view = kernel.inspect(
        Query(notice["wi"], view="request", request_id=notice["request_id"]), handle
    )
    if view.get("status") == "rejected":
        raise Rejected(view["error"]["code"], view["error"]["reason"])
    asked = ((view.get("result") or {}).get("original_request_result") or {}).get("switch_request")
    require(
        isinstance(asked, dict)
        and asked.get("source") == notice["source"]
        and asked.get("target") == notice["target"],
        "The Agent switch request was not committed by this source session",
    )
    return {
        "source": notice["source"],
        "source_wi": notice["wi"],
        "request_id": notice["request_id"],
        "target": notice["target"],
    }
