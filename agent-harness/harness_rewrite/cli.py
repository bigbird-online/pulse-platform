"""Native Host CLI for the isolated replacement candidate; no legacy controller."""

from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from . import guide
from .kernel import Kernel
from . import agent_switch, measurements, self_rollover, self_switch
from .model import (
    ArtifactInput,
    Command,
    Json,
    Query,
    Rejected,
    digest,
    writer_identity,
)
from .native_host import NativeHost
from .effects import Effects
from .pi_dispatch import PiDispatch
from .views import Views
from .repository_identity import public_response
from .workspaces import writable


def substitute(value: Any, artifacts: Json) -> Any:
    if isinstance(value, dict):
        if set(value) == {"file"}:
            return artifacts[value["file"]]
        return {k: substitute(v, artifacts) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, artifacts) for v in value]
    return value


def effect_progress(results: list[Json]) -> list[Json]:
    # Worker observations carry dispatch capabilities; the business interface
    # reports operation progress and reads the current view as the real caller.
    return [
        {
            key: result.get(key)
            for key in ("status", "request_id", "operation_id", "work_item", "error")
        }
        for result in results
    ]


def binding_family(result: Json, conflict: str) -> list[str]:
    """The WI this session holds, then the children it holds through it; [] for none.

    A child's execution authority follows its parent, so the session holding a
    parent holds its children too: together they are one binding. Two unrelated
    WIs, or a partial page, remain a conflict.
    """
    items = result["items"]
    roots = [str(item["id"]) for item in items if item.get("parent") is None]
    if (
        result.get("next_offset") is not None
        or len(roots) != (1 if items else 0)
        or any(item.get("parent") not in (None, *roots) for item in items)
    ):
        raise Rejected("domain_conflict", conflict)
    return roots + [str(item["id"]) for item in items if item.get("parent") is not None]


def bound_family(kernel: Kernel, handle: object, response: Json | None = None) -> list[str]:
    """The one WI this session is bound to, then its children; `response` reuses a bindings read."""
    response = response or kernel.inspect(Query(view="bindings"), handle)
    if response["status"] == "rejected":
        raise Rejected(response["error"]["code"], response["error"]["reason"])
    if response["result"].get("coverage") != "complete":
        raise Rejected(
            "binding_coverage_incomplete",
            "Binding discovery is partial; specify the original work item explicitly. "
            "Inspect bindings for unavailable records; no unique binding was inferred.",
        )
    conflict = (
        "Omitted work item requires exactly one current native binding; name the WI: "
        "work_item_id inside the execute envelope (execute takes no --wi), --wi for "
        "inspect or resume, or /wi <command> <WI>"
    )
    family = binding_family(response["result"], conflict)
    if not family:
        raise Rejected("domain_conflict", conflict)
    return family


def bound_work_item(kernel: Kernel, handle: object, response: Json | None = None) -> str:
    """The one WI this session is bound to; a parent stands for its children."""
    return bound_family(kernel, handle, response)[0]


def held_work_item(kernel: Kernel, handle: object) -> str | None:
    """The one WI this session holds, None only when provably none; else refuse."""
    family = held_family(kernel, handle)
    return family[0] if family else None


def held_family(kernel: Kernel, handle: object) -> list[str]:
    """The one WI this session holds, then its children; [] only when provably none.

    A partial or unreadable binding read must stop `/wi switch`: leaving a session
    whose work could not be recorded would lose where it stands. A parent stands
    for its children, which follow it.
    """
    response = kernel.inspect(Query(view="bindings"), handle)
    if response["status"] == "rejected":
        raise Rejected(response["error"]["code"], response["error"]["reason"])
    result = response["result"]
    if result.get("coverage") != "complete":
        raise Rejected("binding_coverage_incomplete", "Bindings are partial; nothing is switched")
    # A draft this session created is listed among its bindings but holds no work yet.
    held = {**result, "items": [i for i in result["items"] if i["business_progress"] != "draft"]}
    return binding_family(held, "This session holds several WIs; switch one explicitly")


def observe_current(kernel: Kernel, host: NativeHost, wi: str) -> Json:
    """Observe the journal's generation without changing its holder or grants.

    Native identity is still the actual process/session. A different or revoked
    principal remains unauthorized even after seeing the current generation.
    """
    host.current = host.base
    current = kernel.inspect(Query(wi), host.handle)
    if current["status"] != "rejected":
        host.current = replace(host.base, generation=current["work_item"]["generation"])
        current = kernel.inspect(Query(wi), host.handle)
    return current


HANDOVER_FIELDS = (
    "schema", "status", "request_id", "operation_id", "next_action", "error", "session_handover",
)


def switch_answer(response: dict[str, Any]) -> dict[str, Any]:
    """Relay's switch reads status, error and where the WI stands, never a whole WI view."""
    answer = handover_answer(response)
    work_item = response.get("work_item")
    if isinstance(work_item, dict) and "business_progress" in work_item:
        answer.setdefault("work_item", {})["business_progress"] = work_item["business_progress"]
    return answer


def handover_answer(response: dict[str, Any]) -> dict[str, Any]:
    """Relay needs the handover facts only; a whole large WI view overflows its pipe budget."""
    answer = {key: response[key] for key in HANDOVER_FIELDS if key in response}
    work_item = response.get("work_item")
    if isinstance(work_item, dict):
        answer["work_item"] = {
            key: work_item[key] for key in ("id", "generation", "local_revision") if key in work_item
        }
    return answer


def main(argv: list[str] | None = None, *, _host: NativeHost | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    if arguments[:1] == ["effect-service"]:
        from .effect_service import main as service_main

        return service_main(Path(sys.argv[0]).resolve(), arguments[1:])
    if arguments[:1] == ["record-receipt"]:
        from .record_receipt import main as receipt_main

        return receipt_main(Path(sys.argv[0]).resolve(), arguments[1:])
    if arguments[:1] == ["pr-review-dispatch"]:
        from .review_dispatch import main as review_dispatch_main

        return review_dispatch_main(Path(sys.argv[0]).resolve(), arguments[1:])
    if arguments[:1] == ["pr-review-gate-audit"]:
        from .review_dispatch import audit_main

        return audit_main(Path(sys.argv[0]).resolve(), arguments[1:])
    if arguments[:1] == ["collaboration-inbox"]:
        from .collaboration_inbox import main as inbox_main

        return inbox_main(Path(sys.argv[0]).resolve(), arguments[1:])
    if arguments[:1] and arguments[0] in {"entry", "entry-request", "entry-wait", "entry-confirm"}:
        from .external_entry import command as entry_command

        return entry_command(Path(sys.argv[0]).resolve(), arguments[0], arguments[1:])
    if arguments == ["stats-facts"]:
        from .stats import main as stats_main

        return stats_main(Path(sys.argv[0]).resolve())
    if arguments[:1] == ["native-run"]:
        from .standalone import run as run_standalone

        try:
            return run_standalone(Path(sys.argv[0]).resolve(), arguments[1:])
        except Rejected as exc:
            print(
                json.dumps(
                    {"status": "rejected", "error": {"code": exc.code, "reason": exc.reason}}
                )
            )
            return 2
    parser = argparse.ArgumentParser(
        description="Native Host interface to the production WorkItem core"
    )
    parser.add_argument(
        "action",
        choices=[
            "execute",
            "relay-session",
            "relay-channel",
            "relay-access",
            "relay-standing-access",
            "access",
            "channel",
            "inspect",
            "resume",
            "drain",
            "identity",
            "list",
            "usage-range",
        ],
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Business command JSON; raw Host contexts are not accepted",
    )
    parser.add_argument("--human-input-ref")
    parser.add_argument("--request-id")
    parser.add_argument("--wi")
    parser.add_argument("--status")
    parser.add_argument("--owner")
    parser.add_argument("--text")
    parser.add_argument("--view", default="state")
    parser.add_argument("--intent")
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--start-byte", type=int, default=0)
    parser.add_argument("--age-threshold-seconds", type=int)
    parser.add_argument("--revision", type=int)
    parser.add_argument("--artifact-ref", type=Path)
    args = parser.parse_args(argv)
    one_read = ExitStack()
    # Set once an execute envelope is read: a shape refusal then names its input guide.
    executing, guide_intent = False, None
    try:
        if args.request_id is not None and (args.action != "inspect" or args.view != "request"):
            raise Rejected("invalid_input", "Request ID requires inspect --view request")
        if args.revision is not None and (args.action != "inspect" or args.revision <= 0):
            raise Rejected("invalid_input", "Revision requires a positive inspect revision")
        if args.age_threshold_seconds is not None and (
            args.action != "inspect" or args.view not in {"watch", "scan"}
        ):
            raise Rejected(
                "invalid_input",
                "Age threshold only applies to inspect --view watch or scan",
            )
        if args.view == "scan" and args.action != "inspect":
            raise Rejected("invalid_input", "Scan view requires inspect")
        if args.artifact_ref is not None and (
            args.action != "inspect" or args.view not in {"artifact", "checks"}
        ):
            raise Rejected(
                "invalid_input",
                "Artifact reference requires inspect artifact or checks",
            )
        if args.view in {"artifact", "checks"} and (
            args.action != "inspect" or args.artifact_ref is None
        ):
            raise Rejected("invalid_input", "Artifact and checks views require an exact reference")
        if args.view == "policy" and (
            args.action != "inspect"
            or args.input is not None
            or any(
                a.split("=", 1)[0] in {"--after", "--limit", "--start-byte"}
                for a in (argv if argv is not None else sys.argv[1:])
            )
            or args.wi is not None
            or args.revision is not None
            or args.after != 0
            or args.limit != 100
            or args.start_byte != 0
            or args.status is not None
            or args.owner is not None
            or args.text is not None
            or args.age_threshold_seconds is not None
            or args.artifact_ref is not None
        ):
            raise Rejected("invalid_input", "Policy view accepts only inspect --view policy")
        if args.intent is not None and (args.action != "inspect" or args.view != "guide"):
            raise Rejected("invalid_input", "--intent requires inspect --view guide")
        if args.view == "guide":
            if args.action != "inspect" or args.wi is not None or args.input is not None:
                raise Rejected("invalid_input", "Guide view accepts only inspect --view guide [--intent NAME]")
            # Static, read-only and identical on every node: no policy, ledger or WI is read.
            print(json.dumps(guide.read(args.intent), ensure_ascii=False, sort_keys=True))
            return 0
        reference = None
        if args.artifact_ref is not None:
            if (
                args.artifact_ref.is_symlink()
                or not args.artifact_ref.is_file()
                or args.artifact_ref.stat().st_size > 4096
            ):
                raise Rejected("invalid_input", "Expected a bounded regular artifact reference")
            reference = json.loads(args.artifact_ref.read_bytes())
        if args.action == "relay-channel":
            from .relay_channel import command_host

            if args.input is None or str(args.input) != "-":
                raise Rejected("invalid_input", "Relay command requires its bounded stdin request")
            request = json.loads(sys.stdin.read(65537))
            channel_host = command_host(Path(sys.argv[0]).resolve(), request)
            return main(["channel", "--human-input-ref", request["ref"]], _host=channel_host)
        if args.action == "relay-access":
            from .relay_channel import access_host

            if args.input is None or str(args.input) != "-":
                raise Rejected("invalid_input", "Relay access requires its bounded stdin request")
            return main(
                ["access"], _host=access_host(Path(sys.argv[0]).resolve(), json.loads(sys.stdin.read(4097)))
            )
        if args.action == "relay-standing-access":
            from .relay_channel import standing_access

            if args.input is None or str(args.input) != "-":
                raise Rejected("invalid_input", "Relay standing access requires its bounded stdin request")
            print(json.dumps(
                standing_access(Path(sys.argv[0]).resolve(), json.loads(sys.stdin.read(4097))),
                ensure_ascii=False, sort_keys=True,
            ))
            return 0
        if args.action == "relay-session":
            from .relay_session import run

            if args.input is None or str(args.input) != "-":
                raise Rejected(
                    "invalid_input", "Relay session action requires its bounded stdin request"
                )
            try:
                handover_response = run(
                    Path(sys.argv[0]).resolve(), json.loads(sys.stdin.read(65537))
                )
            except (Rejected, OSError, ValueError, KeyError, TypeError):
                raise
            except Exception as exc:
                # The exception class is enough to diagnose the failure in Relay.
                handover_response = Kernel._error(
                    Rejected("source_unavailable", "Relay session action failed: " + type(exc).__name__)
                )
                handover_response["error"]["exception_type"] = type(exc).__name__
            print(json.dumps(public_response(handover_answer(handover_response)), ensure_ascii=False))
            return 2 if handover_response.get("status") == "rejected" else 0
        # The operator-installed bundle fixes this policy location; business
        # command arguments cannot choose an actor/profile/authorization file.
        host = _host or NativeHost(Path(sys.argv[0]).resolve().parent / "host-policy.json")
        if args.action == "inspect" and args.view == "policy":
            from .policy import describe

            print(json.dumps(describe(host.policy), sort_keys=True))
            return 0
        from .policy import execution

        execution_policy = execution(host.policy)
        if args.action == "execute" and execution_policy["mode"] == "audit":
            raise Rejected("policy_read_only", "Installed audit policy permits observations only")
        program_sha256 = hashlib.sha256(Path(sys.argv[0]).read_bytes()).hexdigest()
        admission = writer_identity(program_sha256, host.policy)
        from .production_ledger import ProductionLedger

        if "production_ledger" in host.policy:
            from .production_install import require_active_program

            require_active_program(Path(sys.argv[0]).resolve(), host.policy)
        storage = (
            ProductionLedger(host.policy["production_ledger"], host.policy["namespace"])
            if "production_ledger" in host.policy
            else None
        )
        if storage is not None:
            # Reads before the command's first write or effect drain are one
            # observation of the production ref; see production storage "Read reuse".
            one_read.enter_context(storage.read_snapshot())
        kernel = Kernel(
            Path(host.policy["database"]) if storage is None else None,
            host.authenticate,
            writer_id=admission,
            storage=storage,
            pages=Path(host.policy["views_root"]) if host.policy.get("views_root") else None,
        )
        envelope = None
        verifier_continuations = []
        channel_ref = args.human_input_ref
        # A /wi switch phase: one bounded read, no effect drain, a bounded answer.
        switching: str | None = None
        switch_current: Json | None = None
        switch_proof: Json | None = None
        if args.action == "channel":
            from . import channel

            if not channel_ref:
                raise Rejected("invalid_input", "Original human input reference is required")
            if any(
                a.split("=", 1)[0] not in {"channel", "--human-input-ref", channel_ref}
                for a in (argv if argv is not None else sys.argv[1:])
            ):
                raise Rejected("invalid_input", "Channel accepts only its original input reference")
            asked_switch = getattr(host, "relay_agent_switch", None)
            if asked_switch is not None:
                # An Agent's own committed request_switch; relay_channel checked its notice.
                parsed = {"action": "switch", "wi": asked_switch["target"]}
            else:
                source = json.loads(host.human_input(channel_ref, latest=True))["source"]
                parsed = channel.parse(source["message"])
            if parsed["action"] == "help":
                print(json.dumps(channel.HELP, ensure_ascii=False))
                return 0
            if parsed["action"] == "rollover":
                raise Rejected(
                    "host_session_controller_required",
                    "Relay must complete this original command through its native session controller",
                )
            if parsed["action"] == "switch":
                # Relay leaves the current session, then enters the target's (Relay owns
                # which native session runs); the kernel records and moves authority only.
                phase = getattr(host, "relay_switch_phase", None)
                if phase not in ("leave", "enter", "readback"):
                    raise Rejected(
                        "host_session_controller_required",
                        "Relay must complete /wi switch through its session controller",
                    )
                if execution_policy["mode"] == "audit":
                    raise Rejected("policy_read_only", "Installed audit policy permits observations only")
                switching = phase
                if phase == "readback":
                    # Whether enter (start_work or continue_here, request channel.<ref>) committed;
                    # Relay reads it from any session of the conversation after a restart or an
                    # unknown enter, before it settles on the target session or returns.
                    asked = kernel.inspect(
                        Query(parsed["wi"], view="request", request_id="channel." + channel_ref),
                        host.handle,
                    )
                    if asked["status"] == "rejected":
                        print(json.dumps(switch_answer(asked), ensure_ascii=False))
                        return 2
                    print(json.dumps({
                        "schema": "pulse.wi_switch/v1", "phase": "readback", "wi": parsed["wi"],
                        "committed": asked["result"]["request_observation"]["state"] == "committed",
                    }))
                    return 0
                proof = (
                    None if asked_switch is None
                    else agent_switch.committed(kernel, host.handle, asked_switch)
                )
                if phase == "leave":
                    family = held_family(kernel, host.handle)
                    held = family[0] if family else None
                    leave = {"schema": "pulse.wi_switch/v1", "phase": "leave", "left": held,
                             "recorded": False}
                    if asked_switch is not None:
                        # The Agent's own checkpoint, required by request_switch, stays the latest record.
                        if held != asked_switch["wi"]:
                            raise Rejected("domain_conflict",
                                           "This session no longer holds the WI that asked to switch")
                        print(json.dumps({**leave, "agent_request": True}))
                        return 0
                    if held is None or held == parsed["wi"]:
                        print(json.dumps(leave))
                        return 0
                    if parsed["wi"] in family[1:]:
                        print(json.dumps({**leave, "no_switch": parsed["wi"] + " 是当前 WI " + held
                                          + " 的子 WI，执行权已在本会话，无需切换"}, ensure_ascii=False))
                        return 0
                    # One read: the checkpoint needs only the generation and declared resources.
                    host.current = host.base
                    switch_current = kernel.inspect(Query(held), host.handle)
                    if switch_current["status"] == "rejected":
                        print(json.dumps(switch_answer(switch_current), ensure_ascii=False))
                        return 2
                    envelope = channel.leave_envelope(channel_ref, held, parsed["wi"], switch_current)
                    parsed = {"action": "execute"}
                else:
                    target = parsed["wi"]
                    switch_current = observe_current(kernel, host, target)
                    if switch_current["status"] == "rejected":
                        print(json.dumps(switch_answer(switch_current), ensure_ascii=False))
                        return 2
                    envelope = channel.start_envelope(
                        channel_ref, target, switch_current
                    ) or channel.continue_envelope(channel_ref, target, switch_current)
                    if envelope is None:
                        # Already held here, or read-only: the Agent resumes it in its first turn.
                        print(json.dumps(switch_answer(switch_current), ensure_ascii=False))
                        return 0
                    if proof is not None:
                        # No human input: the attested Agent request stands in for it.
                        envelope = {k: v for k, v in envelope.items() if k != "human_input_ref"}
                        switch_proof = proof if envelope["intent"] == "continue_here" else None
                    parsed = {"action": "execute"}
            args.action = parsed["action"]
            args.wi = parsed.get("wi")
            args.view = parsed.get("view", "state")
            if args.action == "execute" and envelope is None:
                if execution_policy["mode"] == "audit":
                    raise Rejected(
                        "policy_read_only",
                        "Installed audit policy permits observations only",
                    )
                wi = args.wi or bound_work_item(kernel, host.handle)
                channel_current = observe_current(kernel, host, wi)
                if channel_current["status"] == "rejected":
                    print(json.dumps(public_response(channel_current), ensure_ascii=False))
                    return 2
                envelope = channel.envelope(parsed, channel_ref, wi, channel_current)
            elif args.action == "resume" and args.wi and execution_policy["mode"] != "audit":
                observed = observe_current(kernel, host, args.wi)
                start = channel.start_envelope(
                    channel_ref, args.wi, observed
                ) or channel.continue_envelope(channel_ref, args.wi, observed)
                if start is not None:
                    args.action, envelope = "execute", start
        elif channel_ref is not None:
            raise Rejected("invalid_input", "Human input reference requires channel action")
        if args.action == "usage-range":
            if host.runtime != "pi":
                raise Rejected(
                    "measurement_source_unavailable",
                    "This runtime uses its registered observation source",
                )
            from .native_usage import boundaries

            response: Json = {
                "schema": "harness.native-usage-range/v1",
                "range": boundaries(host.native_log(), args.start_byte),
                "source": "actual_native_session",
                "read_only": True,
            }
        elif args.action == "inspect" and args.view == "scan":
            response = kernel.inspect(
                Query(
                    work_item_id=args.wi,
                    revision=args.revision,
                    view="scan",
                    status=args.status,
                    owner=args.owner,
                    text=args.text,
                    after=args.after,
                    limit=args.limit,
                    age_threshold_seconds=args.age_threshold_seconds,
                ),
                host.handle,
            )
        elif args.action == "list":
            response = kernel.inspect(
                Query(
                    view="list",
                    status=args.status,
                    owner=args.owner,
                    text=args.text,
                    after=args.after,
                    limit=args.limit,
                ),
                host.handle,
            )
        elif args.action == "access":
            # Human-approved paths for this session on its bound WI and the
            # children it holds through it, the standing grants of its Agent
            # and the proven workspaces of every WI it holds; Relay enforces all three.
            bindings = kernel.inspect(Query(view="bindings"), host.handle)
            standing = (
                [] if bindings["status"] == "rejected" else bindings["result"]["standing_access"]
            )
            held_workspaces = writable(
                [] if bindings["status"] == "rejected" else bindings["result"]["workspace_access"]
            )
            try:
                family = [args.wi] if args.wi else bound_family(kernel, host.handle, bindings)
            except Rejected as exc:
                if exc.code != "domain_conflict":
                    raise
                family = []
            access_wi: str | None = family[0] if family else None
            grants: list[Json] = []
            for wi in family:
                current_view = kernel.inspect(Query(wi), host.handle)
                if current_view["status"] != "rejected":
                    grants += [
                        row
                        for row in current_view["result"]["current_observation"].get(
                            "access_grants", []
                        )
                        if row.get("session") == host.base.session
                    ]
            response = {
                "schema": "harness.session_access/v1",
                "work_item_id": access_wi,
                "grants": grants,
                "standing": standing,
                "workspaces": held_workspaces,
                "read_only": True,
            }
        elif args.action == "identity":
            # This reference is an actual native identity, never an execution grant.
            from .session_sources import original_user

            human_ref = None
            if (
                host.runtime in {"cc", "codex", "agy"} or host.profile.get("standalone")
            ) and not host.environment.get("PULSE_PI_TENANT_ID"):
                try:
                    human_ref = original_user(
                        host.native_log(), host.runtime, host.native_header["id"], None
                    )[0]
                except Rejected:
                    pass  # Native identity is independent of a human decision.
            response = {
                "schema": "harness.native_identity/v1",
                "runtime": host.runtime,
                "entry": host.entry,
                "latest_human_input_ref": human_ref,
                "principal": host.base.principal(),
                "role": host.profile["role"],
                "installation": {
                    "bundle_sha256": program_sha256,
                    "policy_sha256": digest(host.policy),
                    "writer_admission": admission,
                },
            }
        elif args.action == "execute":
            if envelope is None:
                if args.input is None:
                    raise Rejected("invalid_input", "A business command is required")
                try:
                    text = (
                        sys.stdin.read(4 * 1024 * 1024 + 1)
                        if str(args.input) == "-"
                        else args.input.read_text()
                    )
                except OSError:
                    raise Rejected(
                        "invalid_input",
                        "--input names a readable JSON file, or - for standard input; inline JSON "
                        "is not accepted: send the envelope {\"intent\", \"work_item_id\", "
                        "\"input\"} with --input - <<'JSON' ... JSON",
                    ) from None
                try:
                    envelope = json.loads(text)
                except ValueError:
                    raise Rejected("invalid_input", "--input is not valid JSON") from None
            executing = True
            guide_intent = envelope.get("intent") if isinstance(envelope, dict) else None
            if not isinstance(envelope, dict) or set(envelope) - {
                "request_id",
                "intent",
                "work_item_id",
                "input",
                "files",
                "sources",
                "human_input_ref",
            }:
                raise Rejected(
                    "invalid_input",
                    "Unsupported command envelope field: the envelope takes only request_id, "
                    "intent, work_item_id, input, files, sources and human_input_ref; business "
                    "fields such as candidate_id go inside input",
                )
            for field, kind in (("intent", str), ("work_item_id", str), ("input", dict),
                                ("files", dict), ("sources", dict), ("human_input_ref", str)):
                if field in envelope and envelope[field] is not None and not isinstance(envelope[field], kind):
                    shape = {
                        "files": 'an object {name: {path, logical, version[, access]}}, '
                                 'referenced in input as {"file": name}',
                        "sources": "an object {name: captured snapshot_id}",
                        "input": "an object",
                    }.get(field, "a string")
                    raise Rejected("invalid_input", f"{field} must be {shape}, not {type(envelope[field]).__name__}")
            for name, row in (envelope.get("files") or {}).items():
                if not (
                    isinstance(row, dict)
                    and {"path", "logical", "version"} <= set(row) <= {"path", "logical", "version", "access"}
                    and all(isinstance(v, str) and v for v in row.values())
                ):
                    raise Rejected(
                        "invalid_input",
                        f"files.{name} must be {{path, logical, version[, access]}} strings; "
                        "reference it in input as {\"file\": \"" + str(name) + "\"}",
                    )
            intent = envelope["intent"]
            wi = envelope.get("work_item_id")
            if wi is None and intent not in {"create_draft", "create_lightweight", "create_child"}:
                wi = bound_work_item(kernel, host.handle)
            current: Json = {}
            if wi and switch_current is not None:
                current = switch_current
            elif wi:
                current = observe_current(kernel, host, wi)
                if current["status"] == "rejected":
                    print(json.dumps(current, ensure_ascii=False))
                    return 2
            # Extra artifact scope comes only from replayed, currently authorized
            # allocations. Physical roots are never accepted in the envelope.
            from .source_capture import artifact_roots

            roots = (
                artifact_roots(
                    kernel,
                    wi,
                    host.handle,
                    intent,
                    tuple(row["path"] for row in envelope["files"].values()),
                )
                if wi and envelope.get("files")
                else ()
            )
            artifacts = {
                name: host.artifact(**row, workspace_roots=roots)
                for name, row in envelope.get("files", {}).items()
            }
            for name, snapshot_id in envelope.get("sources", {}).items():
                if name in artifacts:
                    raise Rejected("invalid_input", "Duplicate artifact name")
                snapshots = current["result"]["current_observation"]["source_publication"][
                    "snapshots"
                ]
                snapshot = next((s for s in snapshots if s["snapshot_id"] == snapshot_id), None)
                if snapshot is None:
                    raise Rejected("source_invalid", "Unknown captured source")
                ref = snapshot["artifact"]
                fixed = kernel.inspect(Query(wi, view="artifact", artifact_ref=ref), host.handle)
                if fixed["status"] == "rejected":
                    raise Rejected("source_invalid", "Captured source bytes unavailable")
                artifacts[name] = ArtifactInput(
                    ref["object"],
                    ref["version"],
                    base64.b64decode(fixed["result"]["artifact"]["base64"], validate=True),
                    ref["access"],
                )
            data = substitute(
                envelope.get("input", {}),
                {key: item.reference() for key, item in artifacts.items()},
            )
            if intent in {"create_draft", "create_lightweight"}:
                data.setdefault("owner", host.base.actor)
            request = envelope.get("request_id") or "native." + digest(
                {
                    "session": host.base.session,
                    "intent": intent,
                    "wi": wi,
                    "input": data,
                }
            )
            generation = 0
            git_source = None
            if intent == "create_child" and isinstance(data.get("parent"), str):
                # The parent's holder creates a child under the parent's current generation.
                parent_view = observe_current(kernel, host, data["parent"])
                if parent_view["status"] != "rejected":
                    generation = parent_view["work_item"]["generation"]
            if wi and current:
                generation = current["work_item"]["generation"]
                if intent == "capture_source":
                    from .source_capture import capture, retained

                    resources = current["result"]["current_observation"]["workspace_settlement"][
                        "resources"
                    ]
                    resource = next(
                        (r for r in resources if r["resource_id"] == data.get("resource_id")),
                        None,
                    )
                    if resource is None:
                        raise Rejected("source_invalid", "Unknown managed source workspace")
                    captured = retained(
                        kernel, wi, host.handle, data["resource_id"], data["commit"]
                    )
                    git_source, bundle_artifact = (
                        captured
                        if captured is not None
                        else capture(resource, host.base, data["commit"])
                    )
                    artifacts["captured-source"] = bundle_artifact
                if intent == "record_task_result":
                    data.setdefault("observation_ref", "observed." + request)
                    # A late original executor reports its original generation.
                    attempts = current["result"]["current_observation"]["workflow"]["attempts"]
                    attempt = next(
                        (a for a in attempts if a["attempt_id"] == data.get("attempt_id")),
                        None,
                    )
                    if attempt:
                        generation = attempt["generation"]
            if intent in (self_rollover.INTENT, self_switch.INTENT) and not host.environment.get(
                "PULSE_PI_TENANT_ID"
            ):
                raise Rejected(
                    "host_session_controller_required",
                    intent + " needs a Relay conversation to change sessions; "
                    "outside Relay the human uses /new and /wi continue",
                )
            handle = host.prepare(
                intent,
                data,
                wi,
                generation,
                tuple(artifacts.values()),
                envelope.get("human_input_ref"),
                git_source,
            )
            if switch_proof is not None:
                host.current = replace(host.current, session_switch=switch_proof)
            one_read.close()
            response = kernel.execute(Command(request, intent, data, wi), handle)
            asked = ((response.get("result") or {}).get("original_request_result") or {})
            if (
                intent == self_switch.INTENT
                and response.get("status") == "local_committed"
                and isinstance(asked.get("switch_request"), dict)
            ):
                response["session_switch_request"] = agent_switch.post(
                    host, response["work_item"]["id"], request, asked["switch_request"]["target"]
                )
            if intent == self_rollover.INTENT and response.get("status") == "local_committed":
                from .relay_session import post_agent_request

                response["session_handover_request"] = post_agent_request(
                    host, response["work_item"]["id"], request
                )
            # External intentions are already durable. A regular successful
            # producer command advances a bounded batch; no recovery recipe.
            # A rollover request leaves its checked effects as they were.
            if (
                response.get("status") == "local_committed"
                and host.profile["role"] == "producer"
                and intent not in measurements.INTENTS
                and intent not in (self_rollover.INTENT, self_switch.INTENT)
                and switching is None
                and execution_policy["mode"] == "automatic"
            ):
                target_wi = response["work_item"]["id"]
                results = Effects(
                    kernel,
                    PiDispatch(
                        kernel,
                        host.policy,
                        host.worker,
                        target_wi,
                        Path(sys.argv[0]).resolve(),
                    ),
                    host.worker,
                ).drain(target_wi, budget=execution_policy["batch_budget"])
                response["effect_progress"] = effect_progress(results)
            if (
                response.get("status") == "local_committed"
                and host.profile["role"] == "producer"
                and switching is None
            ):
                if storage is not None and not storage.snapshot_open:
                    one_read.enter_context(storage.read_snapshot())  # After every effect.
                response["latest_observation"] = observe_current(
                    kernel, host, response["work_item"]["id"]
                )
        else:
            if not args.wi:
                args.wi = bound_work_item(kernel, host.handle)
            current = observe_current(kernel, host, args.wi)
            if current["status"] == "rejected":
                print(json.dumps(current, ensure_ascii=False))
                return 2
            if (
                args.action in {"drain", "resume"}
                and host.profile["role"] == "producer"
                and execution_policy["mode"] != "audit"
                and (current.get("next_action") or {}).get("intent")
                != "resolve_business_affiliation"
            ):
                one_read.close()
                results = Effects(
                    kernel,
                    PiDispatch(
                        kernel,
                        host.policy,
                        host.worker,
                        args.wi,
                        Path(sys.argv[0]).resolve(),
                    ),
                    host.worker,
                ).drain(args.wi, budget=execution_policy["batch_budget"])
                response = observe_current(kernel, host, args.wi)
                response["effect_progress"] = effect_progress(results)
            else:
                response = (
                    Views(kernel, Path(host.policy["views_root"])).read(args.wi, host.handle)
                    if args.view == "state"
                    and args.revision is None
                    and execution_policy["mode"] != "audit"
                    else kernel.inspect(
                        Query(
                            args.wi,
                            request_id=args.request_id,
                            view=args.view,
                            revision=args.revision,
                            status=args.status,
                            artifact_ref=reference,
                            after=args.after,
                            limit=args.limit,
                            age_threshold_seconds=args.age_threshold_seconds,
                        ),
                        host.handle,
                    )
                )
        if args.action == "resume":
            if host.profile["role"] == "producer" and execution_policy["mode"] != "audit":
                from .verifier_recovery import recover

                one_read.close()
                verifier_continuations = recover(
                    PiDispatch(
                        kernel, host.policy, host.worker, args.wi, Path(sys.argv[0]).resolve()
                    )
                )
            progress = response.get("effect_progress")
            if storage is not None and not storage.snapshot_open:
                one_read.enter_context(storage.read_snapshot())  # After every effect.
            observe_current(
                kernel, host, args.wi
            )  # Restore the real caller after worker observations.
            response = kernel.resume(args.wi, host.handle)
            if progress is not None:
                response["effect_progress"] = progress
        if verifier_continuations:
            response["verification_runtime"] = verifier_continuations
        if storage is not None:
            response["ledger"] = {
                "identity": host.policy["production_ledger"]["identity"],
                "ref": host.policy["production_ledger"]["ref"],
                "backend": "production_git",
                "independent_database": False,
            }
        if args.action in {"list", "inspect", "resume"} and host.profile["role"] == "producer":
            from .legacy import catalog_observation

            response = catalog_observation(
                host.policy,
                response,
                wi=args.wi,
                listing=args.action == "list",
                after=args.after,
                limit=args.limit,
            )
        if channel_ref:
            response["channel_command"] = {
                "backend": "native_core",
                "input_ref": channel_ref,
            }
        if executing:
            response = guide.annotate(response, guide_intent)
        response["dispatch_policy"] = {
            **execution_policy,
            "automatic_drain_enabled": execution_policy["mode"] == "automatic",
        }
        if switching is not None:
            response = switch_answer(response)
        print(json.dumps(public_response(response), ensure_ascii=False, sort_keys=True))
        return 2 if response.get("status") == "rejected" else 0
    except Rejected as exc:
        error = Kernel._error(exc)
        print(json.dumps(guide.annotate(error, guide_intent) if executing else error, ensure_ascii=False))
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Never echo native text, absolute session paths or credential content.
        print(
            json.dumps(
                Kernel._error(
                    Rejected(
                        "source_unavailable",
                        "Native Host input unavailable: " + type(exc).__name__,
                    )
                )
            )
        )
        return 2
    finally:
        one_read.close()


if __name__ == "__main__":
    sys.exit(main())
