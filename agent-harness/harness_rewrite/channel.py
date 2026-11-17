"""Daily slash vocabulary; identity and state decisions remain in Host/Kernel."""

from __future__ import annotations

import re
from .model import Json, Rejected

HELP = {
    "backend": "native_core",
    "commands": [
        "/wi help",
        "/wi list",
        "/wi status [WI]",
        "/wi watch [WI]",
        "/wi continue [WI]",
        "/wi WI",
        "/wi checkpoint [WI] SUMMARY",
        "/wi rollover [WI] [SUMMARY]",
        "/wi switch WI",
        "/wi cancel [WI] REASON",
        "/wi reinit [WI] REASON",
        "/wi label [WI] 主身|自动 [REASON]",
    ],
    "other_operations": "Use the installed native lifecycle skill and execute interface",
    "continue_semantics": (
        "Resume original effects; the same Agent's work held by another session "
        "moves here; never another Agent's authority"
    ),
}


def parse(text: str) -> Json:
    if not isinstance(text, str) or len(text) > 32768:
        raise Rejected("invalid_input", "Expected a bounded original slash command")
    match = re.fullmatch(r"/wi(?:@[A-Za-z0-9_]+)?(?:\s+([\s\S]*))?", text.strip())
    if not match:
        raise Rejected("invalid_input", "Expected original /wi command")
    parts = (match[1] or "help").split(None, 1)
    command = parts[0]
    rest = parts[1].strip() if len(parts) == 2 else ""
    if command.startswith("wi_"):
        if rest or not re.fullmatch(r"wi_[A-Za-z0-9_]+", command):
            raise Rejected("invalid_input", "Expected a single work item identity")
        return {"action": "resume", "wi": command, "view": "state"}
    if command in {"help", "list"}:
        if rest:
            raise Rejected("invalid_input", "This command accepts no arguments")
        return {"action": command}
    if command not in {
        "status", "watch", "continue", "checkpoint", "cancel", "reinit", "rollover", "label", "switch"
    }:
        raise Rejected(
            "invalid_input",
            "Unsupported daily command; use /wi help and the native lifecycle skill.",
        )
    parts = rest.split(None, 1)
    wi = None
    if parts and parts[0].startswith("wi_"):
        wi = parts[0]
        if not re.fullmatch(r"wi_[A-Za-z0-9_]+", wi):
            raise Rejected("invalid_input", "Invalid work item identity")
        rest = parts[1].strip() if len(parts) == 2 else ""
    if command in {"status", "watch", "continue"}:
        if rest:
            raise Rejected("invalid_input", "Only an optional work item is accepted")
        return {
            "action": "resume" if command == "continue" else "inspect",
            "wi": wi,
            "view": "watch" if command == "watch" else "state",
        }
    if command == "label":
        words = rest.split(None, 1)
        labels = {"主身": True, "main": True, "自动": False, "auto": False}
        if not words or words[0] not in labels:
            raise Rejected("invalid_input", "Expected /wi label [WI] 主身|自动 [REASON]")
        reason = words[1].strip() if len(words) == 2 else "Explicit human /wi label command"
        return {"action": "execute", "wi": wi, "command": "label", "text": reason,
                "main_only": labels[words[0]]}
    if command == "switch":
        if wi is None or rest:
            raise Rejected("invalid_input", "Expected /wi switch WI")
        return {"action": "switch", "wi": wi}
    if command == "rollover":
        return {"action": "rollover", "wi": wi, "text": rest or "Continue the same approved work in a fresh native session"}
    if not rest and command == "cancel":
        rest = "Explicit human /wi cancel command"
    if not rest:
        raise Rejected("invalid_input", "An explicit summary or reason is required")
    return {"action": "execute", "wi": wi, "command": command, "text": rest}


def envelope(parsed: Json, ref: str, wi: str, observation: Json) -> Json:
    command, text = parsed["command"], parsed["text"]
    if command == "checkpoint":
        intent = "checkpoint"
        data = {
            "summary": text,
            "resources": observation["result"]["current_observation"]["definition"][
                "resources"
            ],
        }
    elif command == "cancel":
        intent, data = "terminate_work", {"kind": "cancelled", "reason": text}
    elif command == "label":
        intent, data = "set_main_only", {"main_only": parsed["main_only"], "reason": text}
    else:
        intent, data = "assume_control", {"reason": text}
    return {
        "request_id": "channel." + ref,
        "intent": intent,
        "work_item_id": wi,
        "input": data,
        "human_input_ref": ref,
    }


def continue_envelope(ref: str, wi: str, observation: Json) -> Json | None:
    """`/wi continue WI` moves the same Agent's work into this session.

    The slash input is the human decision; the kernel still checks that the
    holder is the same Agent. Other callers keep the read-only continue.
    """
    work = next(
        (c for c in observation.get("capabilities") or [] if c["name"] == "local_work"), None
    )
    if work is None or work["reason"] != "held_by_same_agent_in_another_session":
        return None
    return {
        "request_id": "channel." + ref,
        "intent": "continue_here",
        "work_item_id": wi,
        "input": {"reason": "Continue the same Agent's work in this session"},
        "human_input_ref": ref,
    }


def start_envelope(ref: str, wi: str, observation: Json) -> Json | None:
    """`/wi WI` on a startable draft starts it; the slash input is the human consent.

    Standard work without an adopted plan stays read-only: the Agent drafts the plan.
    The kernel still checks the designated owner and the current plan revision.
    """
    current = (observation.get("result") or {}).get("current_observation") or {}
    if current.get("business_progress") != "draft":
        return None
    data: Json = {}
    if current.get("profile") == "standard":
        graph = (current.get("workflow") or {}).get("graph")
        if graph is None:
            return None
        data = {"graph_revision": graph["revision"]}
    return {
        "request_id": "channel." + ref,
        "intent": "start_work",
        "work_item_id": wi,
        "input": data,
        "human_input_ref": ref,
    }



def leave_envelope(ref: str, wi: str, target: str, observation: Json) -> Json:
    """`/wi switch` leaving a session: record where the held work stands."""
    envelope_ = envelope(
        {"command": "checkpoint",
         "text": f"切换到 {target} 前的自动记录；用 /wi switch {wi} 回到本会话继续"},
        ref, wi, observation,
    )
    return {**envelope_, "request_id": "channel." + ref + ".leave"}
