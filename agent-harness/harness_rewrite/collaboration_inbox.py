"""Read-only collaboration inbox of one Agent across every open WI.

The kernel does not deliver an offered contract to its recipient Agent; the
recipient's sessions find it by reading. An unattended gate (a Relay cron job)
has no native session, so the protected active installation is the reader, as
for the effect service and the record receipt. The inbox names two roles. As
`recipient`, each contract the Agent still has to accept or settle; as
`requester`, each contract the Agent offered that came back answered (completed,
returned or rejected) and that it has not yet settled, with the answering Agents
and the settlement options. Each item carries only its role, WI, contract ID,
kind, status, main_only, offering Agent, offer instant and optional deadline.
Purpose, resources, sessions and every other WI fact stay behind native
authentication.

Beside the contracts, `pull_requests` lists the results of pull requests whose
latest round the Agent registered on an open WI, so the gate can tell the
holding session without a human prompt: merged by the kernel, changes
requested, checks failed, merge failed or blocked, or human confirmation
required, each for 24 hours after it happened. A row carries the WI, the
round's candidate ID, repository, number, status, head, instant, the kernel's
next step and, after a review, its score and first-line summary. Older gates
ignore the separate list.

A pull request result goes only to the session that made the round's head:
the session that registered `submit_pull_request` with that head. Until the
journal shows that session ended, the row carries `author_ended: false` and, on
the surface the session registered on, `author_key`, the same one-way digest as
`holder_key` but of the registering session; the Relay delivers it there alone,
waiting while that conversation is busy, and no other surface or session
delivers it. The journal shows the session ended when, after registering,
it handed off the WI holding the round (rollover: `TransferReserved` in an
entry it committed, on the WI or, for a child, its parent), or switched away
from it (its `request_switch` entered in the target by `continue_here` with
the Host's switch proof, or followed by another session holding the WI). A
round registered before rounds recorded the entry may have come from a
scheduler run nothing can reach, so it counts as ended: its holder hears of it.
A continuation or takeover by another session does not end it, nor does a
conversation missing on some node. Then the row says `author_ended: true`, carries no
`author_key`, and the WI holder's session (`target.holder_key`) receives it.
A round registered by a scheduler run (`author_entry: scheduler`) goes to no
session: that run is a fresh native session that never enters a conversation,
and its result is not the holder's conversation to answer. Its row says
`author_unattended: true` and `author_ended: false` and carries no
`author_key`, so no gate delivers it; the result stays in the kernel's pull
request record, this inbox and Stats.

A main-only recipient contract may name the recipient's own target WI; a
requester contract is settled on the WI it was offered from, which is therefore
its target. For either the inbox reports who holds that WI: the inbox Agent on
this surface (`recipient_here`), the inbox Agent on another surface, another
Agent, nobody, or unreadable. Only for the inbox Agent on this surface it adds
`holder_key`, a one-way digest of the holding session reference that only a
reader holding that session's native ID can recompute; the Relay uses it to
deliver the contract to that one conversation. `held` lists every active WI the
inbox Agent holds, with the same holder report, so a scheduled wake can reach
the conversation holding a named WI.

`board` is the human's view for Stats (exported with `stats-facts`): each
contract with its WI title, the offering and recipient Agents, the first line
of its purpose, its status, its timeline and who acts next. Reasons appear only
as first-line summaries; sessions, evidence and resources never appear.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from . import children, collaboration, pull_requests
from .kernel import Kernel
from .model import Json, Rejected, digest, identity

SCHEMA = "harness.collaboration_inbox/v1"
OPEN = frozenset({"draft", "active"})


def offered_at(history: list[Json], contract_id: str) -> str | None:
    """Commit instant of the journal entry that offered the contract."""
    for entry in history:
        for event in entry["events"]:
            if (
                event["kind"] == "CollaborationOffered"
                and event["payload"].get("contract_id") == contract_id
            ):
                return str(entry["at"])
    return None


SUMMARY = 80
SETTLED_WINDOW = timedelta(hours=24)
TERMINAL = frozenset({"completed", "terminated"})
DECIDED = {"accept": "accepted", "reject": "rejected", "return": "returned", "complete": "completed"}
# One status per contract; with several recipients the most pressing answer wins.
PRESSING = ("returned", "pending", "accepted", "rejected", "completed")


def summary(text: str) -> str:
    """First non-empty line, at most SUMMARY characters."""
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= SUMMARY else line[: SUMMARY - 1] + "…"


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def timelines(history: list[Json]) -> dict[str, list[Json]]:
    """Every collaboration step per contract in journal order. A reassignment
    withdraws one contract and offers its successor in the same entry."""
    steps: dict[str, list[Json]] = {}
    for entry in history:
        kinds = [(e["kind"], e["payload"].get("contract_id")) for e in entry["events"]]
        withdrawn = [c for k, c in kinds if k == "CollaborationWithdrawn"]
        offered = [c for k, c in kinds if k == "CollaborationOffered"]
        moved = len(withdrawn) == 1 and len(offered) == 1
        for event in entry["events"]:
            data = event["payload"]
            if event["kind"] == "CollaborationOffered":
                step: Json = {"action": "offered", "actor": data["offered_by"]["actor"]}
                if moved:
                    step["previous_contract_id"] = withdrawn[0]
            elif event["kind"] == "CollaborationResponded":
                step = {
                    "action": DECIDED[data["decision"]],
                    "actor": data["actor"],
                    "reason": summary(data["reason"]),
                }
            elif event["kind"] == "CollaborationWithdrawn":
                step = {
                    "action": "reassigned" if moved else "withdrawn",
                    "actor": data["actor"],
                    "reason": summary(data["reason"]),
                }
                if moved:
                    step["successor_contract_id"] = offered[0]
            else:
                continue
            steps.setdefault(data["contract_id"], []).append({"at": entry["at"], **step})
    return steps


def status(item: Json) -> str:
    if item["withdrawn"]:
        return "withdrawn"
    answers = {response["status"] for response in item["responses"].values()}
    return next(s for s in PRESSING if s in answers)


def settled(item: Json) -> bool:
    """Completed, rejected or withdrawn; an accepted transfer of full
    responsibility is settled as well."""
    return item["withdrawn"] or all(
        response["status"] in {"completed", "rejected"}
        or item["kind"] == "responsibility"
        and response["status"] == "accepted"
        for response in item["responses"].values()
    )


def next_step(state: Json, key: str, item: Json) -> Json | None:
    """Who acts next on an open WorkItem: owing recipients first, then the requester."""
    if state["status"] not in OPEN or item["withdrawn"]:
        return None
    owing = {
        actor: owed["status"]
        for actor in item["responses"]
        for owed in collaboration.awaiting(state, actor)
        if owed["contract_id"] == key
    }
    if owing:
        return {
            "role": "recipient",
            "actors": sorted(owing),
            "actions": ["accept", "reject"]
            if "pending" in owing.values()
            else ["complete", "return"],
        }
    settler = collaboration.settler(state, item)
    settle = [
        owed
        for owed in (collaboration.awaiting_settlement(state, settler) if settler else [])
        if owed["contract_id"] == key
    ]
    if settle:
        return {
            "role": "requester",
            "actors": [settler],
            "actions": settle[0]["options"],
        }
    return None


def board(wi: str, title: str, state: Json, history: list[Json], now: str) -> list[Json]:
    """Contracts still open on a non-terminal WI, plus those settled within 24 hours."""
    steps = timelines(history)
    rows = []
    for key, item in state.get("collaborations", {}).items():
        timeline = steps.get(key, [])
        done = settled(item)
        if done:
            if not timeline or instant(now) - instant(timeline[-1]["at"]) > SETTLED_WINDOW:
                continue
        elif state["status"] in TERMINAL:
            continue
        offered = item["offered_by"]
        rows.append(
            {
                "work_item_id": wi,
                "work_item_title": title,
                "contract_id": key,
                "kind": item["kind"],
                "required": item["required"],
                "main_only": collaboration.main_only(item),
                **({"deadline": item["deadline"]} if "deadline" in item else {}),
                "offered_by": {"actor": offered["actor"], "surface": offered["surface"]},
                "recipients": [
                    {
                        "actor": r["principal"]["actor"],
                        **(
                            {"surface": r["principal"]["surface"]}
                            if "surface" in r["principal"]
                            else {}
                        ),
                    }
                    for r in item["recipients"]
                ],
                "purpose": summary(item["purpose"]),
                "status": status(item),
                "settled": done,
                "timeline": timeline,
                "next": next_step(state, key, item),
            }
        )
    return rows


# Pull request results the registering Agent should hear of, each within PR_WINDOW.
PR_RESULTS = frozenset({"merged", "changes_requested", "checks_failed", "merge_failed",
                        "merge_blocked", "human_confirmation_required"})
PR_WINDOW = timedelta(hours=24)
PR_NEXT = {
    "changes_requested": ("submit_pull_request",
                          "Revise the pull request per the review and register the new head"),
    "merge_failed": ("submit_pull_request",
                     "Read the merge observation, repair its cause and register a new head"),
    "human_confirmation_required": (
        "confirm_pull_request_merge",
        "Tell the human the review outcome; only their decision in the holding session "
        "confirms the merge"),
}


# Relay's delivery accepts at most this many check names, each one line of at most CHECK_NAME characters.
CHECKS_SHOWN = 16
CHECK_NAME = 200


def shown_checks(block: Json) -> Json:
    """The failed check names a delivery can carry, and how many it leaves out."""
    names = [" ".join(str(name).split()) for name in block["failed_checks"]]
    names = [name if len(name) <= CHECK_NAME else name[: CHECK_NAME - 1] + "…" for name in names if name]
    omitted = int(block.get("failed_checks_omitted") or 0) + max(0, len(names) - CHECKS_SHOWN)
    return {"failed_checks": names[:CHECKS_SHOWN], **({"failed_checks_omitted": omitted} if omitted else {})}


def observed_at(history: list[Json], effect_id: str | None) -> str | None:
    """Commit instant of the latest read-back of an effect."""
    found = None
    for entry in history:
        for event in entry["events"]:
            if event["kind"] == "EffectObserved" and event["payload"].get("effect_id") == effect_id:
                found = str(entry["at"])
    return found


def pull_request_results(
    wi: str, state: Json, history: list[Json], actor: str, now: str
) -> list[Json]:
    """Recent results of the pull requests whose latest round `actor` registered."""
    rows = []
    for pr in pull_requests.records(state).values():
        item = pr["rounds"][-1]
        if pr.get("withdrawn") or item["author"]["actor"] != actor:
            continue
        value = pull_requests.status(state, pr)
        block = pull_requests.merge_block(state, item)
        if value == "merge_pending" and block is not None:
            value = "merge_blocked"
        if value not in PR_RESULTS:
            continue
        review = item["review"]
        if item["merge_effect"] is not None and value != "human_confirmation_required":
            at = observed_at(history, item["merge_effect"])
        else:
            at = review["at"] if review is not None else item["submitted_at"]
        if at is None or instant(now) - instant(at) > PR_WINDOW:
            continue
        if block is not None:
            intent, action = block["recovery"], block["agent_action"]
        else:
            intent, action = PR_NEXT.get(value, (None, None))
        rows.append({
            "role": "submitter",
            "work_item_id": wi,
            "candidate_id": item["candidate_id"],
            "repository": pr["repository"],
            "number": pr["number"],
            "status": value,
            "head_sha": item["head_sha"],
            "at": at,
            **({"next": {"intent": intent, "agent_action": action,
                         **(shown_checks(block) if block and block.get("failed_checks") else {})}}
               if intent else {}),
            **({"review": {"score": review["score"], "summary": summary(review["reason"])}}
               if review is not None and value != "merged" else {}),
            "target_work_item_id": wi,
            "author_session": item["author"],
            # A round registered before the kernel recorded the entry: unknown.
            "author_entry": item.get("author_entry", UNKNOWN_ENTRY),
            "submitted_at": item["submitted_at"],
        })
    return rows


HOLDER_KEY = "collab_delivery_holder"


def holder_key(session_ref: str) -> str:
    """Domain-separated from every other export of a session reference."""
    return digest([HOLDER_KEY, session_ref])


def target(work_item_id: str, actor: str, surface: str | None,
           holders: dict[str, Json | None], unreadable: set[str]) -> Json:
    if work_item_id in unreadable:
        return {"work_item_id": work_item_id, "holder": "unavailable"}
    holder = holders.get(work_item_id)
    if holder is None:
        return {"work_item_id": work_item_id, "holder": "absent"}
    if holder["actor"] != actor:
        return {"work_item_id": work_item_id, "holder": "other"}
    if surface is None or holder["surface"] != surface:
        return {"work_item_id": work_item_id, "holder": "recipient_elsewhere"}
    return {"work_item_id": work_item_id, "holder": "recipient_here",
            "holder_key": holder_key(holder["session"])}


def handoffs(history: list[Json]) -> Json:
    """The few journal facts that can end a session, kept instead of the history:
    rollovers (`TransferReserved`) and Agent switches asked (`request_switch`)
    with who committed them and when, and the attested switch proofs that a
    `continue_here` entered with (`SessionContinued.source`)."""
    found: Json = {"transfers": [], "switches": [], "entered": set()}
    for entry in history:
        kinds = {e["kind"] for e in entry["events"]}
        principal = entry["semantics"].get("principal")
        if "TransferReserved" in kinds:
            found["transfers"].append((str(entry["at"]), principal))
        asked = (entry.get("result") or {}).get("switch_request")
        if isinstance(asked, dict) and isinstance(asked.get("target"), str):
            found["switches"].append(
                (str(entry["at"]), principal, entry["request_id"], asked["target"]))
        found["entered"].update(
            e["payload"]["source"] for e in entry["events"] if e["kind"] == "SessionContinued")
    return found


# The entry of a round registered before rounds recorded it.
UNKNOWN_ENTRY = "unrecorded"


def session_ended(index: dict[str, Json], lineage: list[str], session: Json, since: str,
                  entry: Any = None, holder: Json | None = None) -> bool:
    """The kernel's own record that `session` will take no more turns on this
    round's WI. A round whose entry was never recorded may be a scheduler run
    nothing can reach, so it counts as ended (the holder may hear of it once
    more, never not at all). Otherwise the session, after registering, handed
    the WI (or a child's parent) over by rollover, or switched away from it:
    its `request_switch` was entered in the target by `continue_here` with the
    Host's switch proof, or, since some entries succeed without that event, the
    WI is now held by another session (`holder`). No conversation missing on
    some node counts. Scheduler rounds never reach here (see `inbox`)."""
    if entry == UNKNOWN_ENTRY:
        return True
    start = instant(since)
    entered = set().union(*(index[wi]["entered"] for wi in index))
    for wi in lineage:
        found = index.get(wi)
        if found is None:
            continue
        if any(who == session and instant(at) >= start for at, who in found["transfers"]):
            return True
        if any(who == session and instant(at) >= start and (
                holder is not None and holder != session
                or digest({"source": session, "source_wi": wi, "request_id": request,
                           "target": target}) in entered)
               for at, who, request, target in found["switches"]):
            return True
    return False


def parent_state(conn: Any, namespace: str, wi: str) -> Json:
    state = Kernel._replay(conn, namespace, wi)[0]
    if state is None:
        raise Rejected("journal_invalid", "Parent has no original facts")
    return state


def inbox(storage: Any, namespace: str, actor: str, surface: str | None = None,
          now: str | None = None) -> Json:
    """One consistent read: contracts awaiting `actor` on every Draft or active WI,
    as recipient and as requester, and its recent pull request results there."""
    identity(actor)
    now = now or datetime.now(timezone.utc).isoformat()
    items: list[Json] = []
    results: list[Json] = []
    holders: dict[str, Json | None] = {}
    index: dict[str, Json] = {}
    parents: dict[str, str | None] = {}
    unavailable: list[Json] = []
    with storage.connection(write=False, collection="list") as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_errors'"
        ).fetchone():
            unavailable = [
                {"wi": row["wi"], "code": row["code"]}
                for row in conn.execute("SELECT wi, code FROM collection_errors ORDER BY wi")
            ]
        heads = conn.execute(
            "SELECT wi FROM journal_heads WHERE namespace=? ORDER BY wi", (namespace,)
        ).fetchall()
        for head in heads:
            wi = head["wi"]
            try:
                state, _, _, history = Kernel._replay(conn, namespace, wi)
                if state is None:
                    raise Rejected("journal_invalid", "Collection entry has no original facts")
            except Rejected as exc:
                unavailable.append({"wi": wi, "code": exc.code})
                continue
            # A child is held by its parent's current holder; while the parent is
            # not active (stopped, transferring, ended, unreadable) nobody holds it.
            state = children.followed(state, lambda parent: parent_state(conn, namespace, parent))
            holders[wi] = state.get("holder") if state["status"] == "active" else None
            index[wi], parents[wi] = handoffs(history), state.get("parent")
            if state["status"] not in OPEN:
                continue
            results += pull_request_results(wi, state, history, actor, now)
            for owed in collaboration.awaiting(state, actor):
                item = state["collaborations"][owed["contract_id"]]
                items.append(
                    {
                        "role": "recipient",
                        "work_item_id": wi,
                        "contract_id": owed["contract_id"],
                        "kind": owed["kind"],
                        "status": owed["status"],
                        "main_only": owed["main_only"],
                        "offered_by": item["offered_by"]["actor"],
                        "offered_at": offered_at(history, owed["contract_id"]),
                        **({"deadline": owed["deadline"]} if "deadline" in owed else {}),
                        **({"target_work_item_id": owed["target_work_item_id"]}
                           if owed["main_only"] and "target_work_item_id" in owed else {}),
                    }
                )
            for owed in collaboration.awaiting_settlement(state, actor):
                item = state["collaborations"][owed["contract_id"]]
                items.append(
                    {
                        "role": "requester",
                        "work_item_id": wi,
                        "contract_id": owed["contract_id"],
                        "kind": owed["kind"],
                        "status": owed["status"],
                        "main_only": collaboration.main_only(item),
                        "offered_by": item["offered_by"]["actor"],
                        "offered_at": offered_at(history, owed["contract_id"]),
                        **({"deadline": item["deadline"]} if "deadline" in item else {}),
                        "answered_by": owed["answered_by"],
                        "options": owed["options"],
                        # Only the holder of the offering WI settles (SETTLE_ACTION).
                        "target_work_item_id": wi,
                    }
                )
    unreadable = {row["wi"] for row in unavailable}
    for item in items + results:
        if "target_work_item_id" in item:
            item["target"] = target(item.pop("target_work_item_id"), actor, surface,
                                    holders, unreadable)
    for row in results:
        # The round's registering session made its head: it alone receives the
        # result until the journal shows it ended; then the WI holder does. A
        # scheduler run's result goes to no session.
        author, since, entry = row.pop("author_session"), row.pop("submitted_at"), row.pop("author_entry")
        if entry == collaboration.UNATTENDED_ENTRY:
            row["author_unattended"], row["author_ended"] = True, False
            continue
        lineage = [row["work_item_id"], *([parents[row["work_item_id"]]]
                                         if parents.get(row["work_item_id"]) else [])]
        row["author_ended"] = session_ended(index, lineage, author, since, entry,
                                            holders.get(row["work_item_id"]))
        if not row["author_ended"] and surface is not None and author.get("surface") == surface:
            row["author_key"] = holder_key(author["session"])
    # The WIs this Agent's sessions hold, for scheduled wakes of a held session.
    held = [target(wi, actor, surface, holders, unreadable)
            for wi, holder in sorted(holders.items()) if holder is not None and holder["actor"] == actor]
    return {
        "schema": SCHEMA,
        "status": "read_only",
        "actor": actor,
        "items": items,
        "pull_requests": results,
        "held": held,
        "unavailable_items": unavailable,
        "coverage": "partial" if unavailable else "complete",
        "source": "journal",
    }


def main(entry: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="List the original collaboration contracts awaiting one Agent"
    )
    parser.add_argument("--actor", required=True)
    args = parser.parse_args(argv)
    try:
        from .effect_service import ServiceHost
        from .production_ledger import ProductionLedger

        host = ServiceHost(entry)
        ledger = ProductionLedger(host.policy["production_ledger"], host.policy["namespace"])
        response = inbox(ledger, host.policy["namespace"], args.actor, host.policy["surface"])
        # The installation that read the ledger must still be the active one.
        host.authenticate(host.handle)
        response["ledger"] = {
            "identity": host.policy["production_ledger"]["identity"],
            "ref": host.policy["production_ledger"]["ref"],
        }
        # A Relay recomputes holder_key from its own sessions' native IDs.
        response["holder_scope"] = {
            "namespace": host.policy["namespace"],
            "surface": host.policy["surface"],
        }
    except Rejected as exc:
        response = Kernel._error(exc)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        response = Kernel._error(
            Rejected("source_unavailable", "Inbox input unavailable: " + type(exc).__name__)
        )
    print(json.dumps(response, ensure_ascii=False, sort_keys=True))
    return 0 if response.get("status") == "read_only" else 2
