"""An Agent moves its own WorkItem to a fresh session of itself.

The holder session asks with `request_rollover {summary}`. The decision writes
no event: execution authority stays where it is until the Host session
controller (`relay_session`) fences the source with `prepare_rollover` and binds
the actual successor with `accept_transfer`, exactly as for `/wi rollover`.
Authority never leaves the Agent, so no human decision is needed; instead the
WI's frontier must already be recorded and no external effect may be in doubt.
The controller checks the same readiness again when it fences the source.
"""

from __future__ import annotations

from .model import Decision, HostContext, Json, Rejected, encoded, fields
from .workflow import nonempty

INTENT = "request_rollover"
# An effect whose outcome is still being read back cannot change sessions.
DOUBTFUL = frozenset({"attempt_started", "unknown"})


def blocker(state: Json, context: HostContext, intent: str = INTENT) -> str | None:
    """Why this holder session may not hand over now (by `intent`), or None when it may."""
    records = state["records"]
    last = records[-1] if records else None
    if (
        last is None
        or last["kind"] != "CheckpointRecorded"
        or last.get("author_session") != context.session
    ):
        return (
            "The WI's latest record must be a checkpoint by this session: record the frontier "
            "(done, pending, next action) with checkpoint, then " + intent
        )
    doubtful = sorted(key for key, e in state["effects"].items() if e["status"] in DOUBTFUL)
    if doubtful:
        return (
            "Effects " + ", ".join(doubtful) + " are attempt_started or unknown; "
            "resume the WI until they settle, then checkpoint and " + intent
        )
    return None


def decide(state: Json, data: Json, context: HostContext) -> Decision:
    """The caller is already the current holder of an active WI."""
    fields(data, {"summary"})
    nonempty(data["summary"])
    if len(encoded(data)) > 32768:
        raise Rejected("invalid_input", "Use a bounded rollover summary; the checkpoint holds the frontier")
    reason = blocker(state, context)
    if reason:
        raise Rejected("domain_conflict", reason)
    return Decision(
        [],
        {
            "rollover_request": {
                "source": context.principal(),
                "generation": state["generation"],
                "summary": data["summary"],
                "next": "End this turn now and record nothing more: after it the Host fences this "
                "session, starts your successor and hands it this WI from its latest checkpoint",
            }
        },
    )
