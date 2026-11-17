"""Reducer for original business decisions carried by ExistingExecutionContinued.

Translation of retained original decisions is retired with the old track; this
module only replays the recorded result into native state.
"""

from __future__ import annotations

from copy import deepcopy

from .model import Json, Rejected, digest


def require(condition: object, reason: str) -> None:
    if not condition:
        raise Rejected("existing_execution_unavailable", reason)


def apply(state: Json, business: Json) -> None:
    state["profile"] = business["profile"]
    state["candidates"].update(deepcopy(business["candidates"]))
    state["active_candidate"] = business["active_candidate"]
    state["original_effects"] = deepcopy(business["original_effects"])
    # Old handoffs address authenticated actors, not invented native sessions.
    # Preserve that narrower response contract. Adoption never creates a grant.
    for original in business["original_handoffs"]:
        recipients = original.get("to_owners") or [original["to_owner"]]
        ack = original["ack_state"]
        item = {
            "contract_id": original["handoff_id"],
            "kind": "request",
            "purpose": original["input_summary"],
            "required": True,
            "generation": state["generation"],
            "source": digest(original),
            "offered_by": {
                "actor": original["from_owner"],
                "session": None,
                "surface": state["holder"]["surface"],
            },
            "recipients": [
                {
                    "principal": {
                        "actor": actor,
                        "session": None,
                        "surface": state["holder"]["surface"],
                    },
                    "resources": [],
                }
                for actor in recipients
            ],
            "withdrawn": ack == "withdrawn",
            "historical": original,
            "responses": {
                actor: {"status": ack, "history": [], "grant_id": None} for actor in recipients
            },
        }
        # Accepted full responsibility is already represented by the original
        # current owner. It is not a second work-execution delegation.
        if ack == "accepted" and original.get("responsibility_transfer") == "full":
            require(
                state["definition"]["owner"] in recipients,
                "Original responsibility acceptance disagrees with the current owner",
            )
            item["required"] = False
        if ack in {"rejected", "returned", "archived", "absorbed", "completed"}:
            item["required"] = False
        state["collaborations"][item["contract_id"]] = item


def pending(state: Json) -> bool:
    return any(
        row["disposition"] == "original_unsettled" and row["kind"] != "runtime_source_usage_seal"
        for row in state.get("original_effects", [])
    )
