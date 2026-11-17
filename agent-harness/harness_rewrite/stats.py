"""Read-only WorkItem facts for the Stats read model.

Stats counts and ages WorkItems from these journal facts instead of scanning
legacy ledger files, and shows collaboration contracts from the same read
(`collaboration_inbox.board`). ComputeCost reads each item's `completions` as
accepted WI Outcomes and its `merged_pull_requests` as the WI's output: pull
requests the core merged itself and read back. `parent` names a child's parent
WorkItem (null otherwise). A completion's `verification` is `independent`,
`human`, or `agent_review` (a child accepted by its parent holder's review). The export carries no Host
identity, grant or lifecycle authority and never writes the ledger.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from . import children, collaboration_inbox, pull_requests
from .domain import advance, public_state
from .kernel import Kernel
from .model import Json, Rejected, digest

SCHEMA = "harness.stats_facts/v1"
CLOSING = {"completed": "WorkCompleted", "terminated": "WorkTerminated"}


def main_only(state: Json, created_by_human: bool | None) -> bool:
    """The owner's main-body label, recorded at creation or carried from history.

    WorkItems created before the label was recorded keep their original label;
    otherwise standard ones belong to the main body, and lightweight ones do
    when their creation source was the owner's own input.
    """
    label = state.get("main_only")
    if label is None:
        record = (state.get("production") or {}).get("record") or {}
        original = (state.get("legacy_import") or {}).get("original_metadata") or {}
        label = record.get("main_only", original.get("main_only"))
    if label is None:
        label = state["profile"] == "standard" or created_by_human
    return label is True


def created_by_human(conn: Any, namespace: str, history: list[Json]) -> bool | None:
    """Whether the creation source bytes are an owner's native input."""
    created = [e for e in history[0]["events"] if e["kind"] == "WorkItemCreated"]
    if not created:
        return None
    row = conn.execute(
        "SELECT content FROM originals WHERE namespace=? AND digest=?",
        (namespace, created[0]["payload"]["source"]),
    ).fetchone()
    if row is None:
        return None
    try:
        source = json.loads(row[0])
    except ValueError:
        return None
    return isinstance(source, dict) and "native_header" not in source and "source" in source


def completions(history: list[Json]) -> list[Json]:
    """Each accepted completion as of its own revision, as opaque digests.

    The accepted records or candidate, their artifacts and the acceptance
    decision are hashed; paths, prose and sessions do not leave the core.
    """
    if not any(e["kind"] == "WorkCompleted" for entry in history for e in entry["events"]):
        return []
    result: list[Json] = []
    state: Json | None = None
    for entry in history:
        # The replay owns its state; each decision's events are applied as a copy.
        state = advance(state, deepcopy(entry["events"]))
        for event in entry["events"]:
            if event["kind"] != "WorkCompleted" or state is None:
                continue
            data = event["payload"]
            if data.get("profile") == "lightweight":
                records = {r.get("record_id"): r for r in state["records"] if r["kind"] == "WorkRecorded"}
                ids = sorted(data["record_ids"])
                artifacts = [a for i in ids for a in records[i]["record"]["artifacts"]]
                candidate = digest(["records", ids])
                if data.get("review_id"):
                    # A child accepted by its parent holder's review_child, not a human.
                    acceptance = digest(["child_review", data["review_id"], sorted(data["acceptance_refs"])])
                    verification = "agent_review"
                else:
                    acceptance = digest([data["source_digest"], sorted(data["acceptance_refs"])])
                    verification = "human"
            else:
                item = state["candidates"][data["candidate_id"]]
                artifacts = item["content"]["artifacts"]
                candidate = item["content_digest"]
                acceptance = digest([data["review_id"], data.get("verification_id"), data.get("waiver_id")])
                verification = "independent" if data.get("verification_id") else "human"
            principal = (entry.get("semantics") or {}).get("principal") or {}
            result.append({
                "revision": entry["revision"],
                "at": entry["at"],
                "actor": principal.get("actor"),
                "profile": state["profile"],
                "title": public_state(state)["title"],
                "candidate": candidate,
                "artifacts": digest(artifacts),
                "acceptance": acceptance,
                "verification": verification,
            })
    return result


def merged_pull_requests(state: Json, history: list[Json]) -> list[Json]:
    """Each pull request registered on this WI that the core merged and read back.

    Only a merge effect observed as success counts; a PR merged on GitHub
    without the core's own attempt is a failure there and never appears.
    ``score`` is the review of the merged head (None when a human confirmed
    it without one); ``reviewed_rounds`` counts every reviewed round of the PR;
    ``observed_at`` is when the core committed the read-back.
    """
    observed: dict[Any, str] = {}
    for entry in history:
        for event in entry["events"]:
            data = event.get("payload") or {}
            if (event["kind"] == "EffectObserved"
                    and (data.get("observation") or {}).get("outcome") == "success"):
                observed.setdefault(data.get("effect_id"), entry["at"])
    rows: list[Json] = []
    for pr in pull_requests.records(state).values():
        if pull_requests.status(state, pr) != "merged":
            continue
        item = pr["rounds"][-1]
        details = pull_requests.last_observation(state, item) or {}
        review = item["review"]
        rows.append({
            "repository": pr["repository"],
            "number": pr["number"],
            "head_sha": item["head_sha"],
            "merge_commit": details.get("merge_commit"),
            "merged_at": details.get("merged_at"),
            "observed_at": observed.get(item["merge_effect"]),
            "score": review["score"] if review is not None else None,
            "reviewed_rounds": pull_requests.reviewed_rounds(pr),
            "decision": "human" if item["confirmation"] is not None else "review",
        })
    return sorted(rows, key=lambda r: (r["repository"], r["number"]))


def session_writes(history: list[Json]) -> list[Json]:
    """Each committed change a native session made to this WorkItem, keyed opaquely.

    ``session`` is ``digest(["stats_session_write", principal.session])``: a
    one-way, domain-separated key over the existing ``native_session_ref``
    digest. A reader holding the native session ID can recompute it to learn
    which WorkItems that session wrote and when; the export itself carries no
    native ID, surface, path, prose or the core's own principal reference.
    Delivery-service and other non-session commits are not session writes.
    """
    result: list[Json] = []
    for entry in history:
        principal = (entry.get("semantics") or {}).get("principal") or {}
        session = principal.get("session")
        if not entry["events"] or not isinstance(session, str) or not session.startswith("session."):
            continue
        result.append({
            "session": digest(["stats_session_write", session]),
            "actor": principal.get("actor"),
            "at": entry["at"],
        })
    return result


def row(
    wi: str, state: Json, revision: int, history: list[Json], human: bool | None = None,
    pages: Path | None = None,
) -> Json:
    view = public_state(state)
    closing = CLOSING.get(state["status"])
    closed = [
        entry["at"]
        for entry in history
        if closing in {event["kind"] for event in entry["events"]}
    ]
    holder = state.get("holder")
    return {
        "id": wi,
        "title": view["title"],
        "summary": view.get("summary"),
        "owner": state["definition"]["owner"],
        "status": state["status"],
        "business_progress": view["business_progress"],
        "profile": state["profile"],
        "holder": holder["actor"] if isinstance(holder, dict) else None,
        "created_at": history[0]["at"],
        "updated_at": history[-1]["at"],
        "closed_at": closed[-1] if closed else None,
        "main_only": main_only(state, human),
        # The parent WorkItem of a child (create_child), else null. ComputeCost
        # sums a parent's row with its children's.
        "parent": state.get("parent"),
        "local_revision": revision,
        "completions": completions(history),
        "merged_pull_requests": merged_pull_requests(state, history),
        "session_writes": session_writes(history),
        "project_page": page_path(pages, wi),
    }


def page_path(pages: Path | None, wi: str) -> str | None:
    """Where the kernel's rendered project page is; None when absent or unreadable."""
    if pages is None:
        return None
    try:
        from .project_page import located

        found = located(pages, wi)
    except Exception:  # noqa: BLE001 - a derived view never breaks the facts export.
        return None
    return None if found is None else str(found["path"])


def facts(storage: Any, namespace: str, clock: str, pages: Path | None = None) -> Json:
    """One consistent read of every WorkItem journal in the namespace."""
    items: list[Json] = []
    contracts: list[Json] = []
    unavailable: list[Json] = []
    with storage.connection(write=False, collection="list") as conn:
        heads = conn.execute(
            "SELECT wi FROM journal_heads WHERE namespace=? ORDER BY wi", (namespace,)
        ).fetchall()
        for head in heads:
            wi = head["wi"]
            try:
                state, revision, _, history = Kernel._replay(conn, namespace, wi)
                if state is None or not history:
                    raise Rejected("journal_invalid", "Collection entry has no original facts")
                # A child's holder is its parent's current holder.
                state = children.followed(state, lambda other: Kernel._replay(conn, namespace, other)[0])
                human = None
                if state.get("main_only") is None and state["profile"] != "standard":
                    human = created_by_human(conn, namespace, history)
                items.append(row(wi, state, revision, history, human, pages))
                contracts.extend(
                    collaboration_inbox.board(wi, items[-1]["title"], state, history, clock)
                )
            except Rejected as exc:
                unavailable.append({"wi": wi, "code": exc.code})
    return {
        "schema": SCHEMA,
        "generated_at": clock,
        "namespace": namespace,
        "items": items,
        "collaborations": contracts,
        "unavailable_items": unavailable,
        "coverage": "partial" if unavailable else "complete",
        "source": "journal",
        "read_only": True,
    }


def main(entry: Path) -> int:
    from datetime import datetime, timezone

    from .policy import load
    from .production_install import require_active_program
    from .production_ledger import ProductionLedger

    try:
        policy = load(entry.parent / "host-policy.json")
        if "production_ledger" not in policy:
            raise Rejected("authority_denied", "Stats facts require the production ledger")
        require_active_program(entry, policy)
        ledger = ProductionLedger(policy["production_ledger"], policy["namespace"])
        pages = Path(policy["views_root"]) if policy.get("views_root") else None
        response = facts(ledger, policy["namespace"], datetime.now(timezone.utc).isoformat(), pages)
    except Rejected as exc:
        print(json.dumps({"schema": SCHEMA, "status": "rejected", "error": {"code": exc.code}}))
        return 2
    print(json.dumps(response, ensure_ascii=False, sort_keys=True))
    return 0
