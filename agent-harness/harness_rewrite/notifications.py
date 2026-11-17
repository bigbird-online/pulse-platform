"""Original notices, immutable batches and resolution; no channel I/O."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
import hashlib
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .model import Decision, HostContext, Json, NotificationRoute, Query, Rejected
from .model import digest, fields, human_decision, identity
from . import workflow, review

INTENTS = frozenset({"record_notice", "flush_notices", "resolve_notice"})
EFFECT_KIND = "notification_send"
FILTERS = {"open", "resolved", "failed", "unresolved_send", "backlog"}


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def validate_routes(routes: tuple[NotificationRoute, ...]) -> None:
    if (
        len(routes) > 128
        or any(not isinstance(r, NotificationRoute) for r in routes)
        or len({r.route_id for r in routes}) != len(routes)
    ):
        raise Rejected("authority_denied", "Notification routes are ambiguous")
    for r in routes:
        identity(r.route_id)
        if (
            r.channel not in {"telegram", "feishu"}
            or r.mode not in {"immediate", "daily"}
            or r.audience not in {"operational", "owner_attention"}
            or not isinstance(r.recipient_digest, str)
            or len(r.recipient_digest) != 64
            or any(c not in "0123456789abcdef" for c in r.recipient_digest)
            or (
                r.transport_digest is not None
                and (
                    not isinstance(r.transport_digest, str)
                    or len(r.transport_digest) != 64
                    or any(c not in "0123456789abcdef" for c in r.transport_digest)
                )
            )
            or type(r.max_items) is not int
            or not 1 <= r.max_items <= 64
            or type(r.max_bytes) is not int
            or not 128 <= r.max_bytes <= 4096
        ):
            raise Rejected("authority_denied", "Invalid installed notification route")
        try:
            ZoneInfo(r.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise Rejected("authority_denied", "Unknown aggregation timezone") from None


def route(context: HostContext, key: str) -> NotificationRoute:
    identity(key)
    found = [r for r in context.notification_routes if r.route_id == key]
    if len(found) != 1:
        raise Rejected("authority_denied", "Installed notification route unavailable")
    return found[0]


def owner(state: Json, context: HostContext, *, resolve: bool = False) -> None:
    active = state["status"] == "active" and state["holder"] == context.principal()
    terminal = (
        resolve
        and state["status"] in {"completed", "terminated", "stopped"}
        and context.actor == state["definition"]["owner"]
    )
    if context.generation != state["generation"] or not (active or terminal):
        raise Rejected("authority_denied", "Current notice owner is required")
    if context.source is None:
        raise Rejected("source_unavailable", "Notice decisions require original input")


def current(state: Json, notice: Json) -> bool:
    return bool(
        state["status"] == "active"
        and notice["principal"] == state["holder"]
        and notice["generation"] == state["generation"]
        and notice["definition_revision"] == state["definition_revision"]
        and notice["candidate_id"] == state["active_candidate"]
        and not any(
            grant["active"]
            and any(
                workflow.overlaps(a, b)
                for a in notice["resources"]
                for b in grant["resources"]
            )
            for grant in state["grants"].values()
        )
        and (
            notice["purpose"] != "candidate_review"
            or review.progress(state) == "awaiting_review"
        )
    )


def live_batches(state: Json, key: str) -> list[Json]:
    return [
        batch
        for batch in state.get("notice_batches", {}).values()
        if key in batch["notice_ids"]
        and state["effects"][batch["effect_id"]]["status"] != "cancelled"
    ]


def batch_events(
    state: Json, notices: list[Json], r: NotificationRoute, operation: str
) -> list[Json]:
    batches: list[list[Json]] = []
    for item in notices:
        if not batches or (
            len(batches[-1]) >= r.max_items
            or len("\n\n".join(n["text"] for n in batches[-1] + [item]).encode())
            > r.max_bytes
        ):
            batches.append([])
        batches[-1].append(item)
    result = []
    for index, group in enumerate(batches):
        batch_id = f"{operation}.notice.{index}"
        text = "\n\n".join(n["text"] for n in group)
        snapshot = {
            "route": asdict(r),
            "route_digest": digest(asdict(r)),
            "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "window": group[0]["window"],
            "notice_ids": [n["notice_id"] for n in group],
        }
        if r.audience == "owner_attention":
            snapshot["human_attention"] = {
                "requiresHuman": True,
                "humanDecisionKind": "owner_decision",
                "authorityFingerprint": digest(
                    [n["authority_fingerprint"] for n in group]
                ),
            }
        result.extend(
            [
                event(
                    "NoticeBatchCaptured",
                    batch_id=batch_id,
                    effect_id=batch_id,
                    **snapshot,
                ),
                event(
                    "EffectRequested",
                    effect_id=batch_id,
                    effect_kind=EFFECT_KIND,
                    candidate_id=state["active_candidate"],
                    generation=state["generation"],
                    node_id=None,
                    required=False,
                    target=r.route_id,
                    snapshot=snapshot,
                ),
            ]
        )
    return result


def decide(
    state: Json,
    intent: str,
    data: Json,
    context: HostContext,
    operation: str,
    wi: str,
    at: str,
) -> Decision:
    owner(state, context, resolve=intent == "resolve_notice")
    assert context.source is not None
    if intent == "resolve_notice":
        fields(data, {"notice_id", "reason"})
        identity(data["notice_id"])
        workflow.nonempty(data["reason"])
        item = state.get("notices", {}).get(data["notice_id"])
        if item is None:
            raise Rejected("domain_conflict", "Unknown original notice")
        if item["resolution"] is not None:
            return Decision([], {"notice_id": data["notice_id"], "resolved": True})
        return Decision(
            [
                event(
                    "NoticeResolved",
                    notice_id=data["notice_id"],
                    reason=data["reason"],
                    source=context.source.sha256,
                    actor=context.actor,
                    at=at,
                )
            ],
            {"notice_id": data["notice_id"], "resolved": True},
        )
    if intent == "record_notice":
        fields(data, {"route_id", "resources", "text", "purpose", "attention"})
    else:
        fields(data, {"route_id", "window"})
    r = route(context, data["route_id"])
    today = (
        datetime.fromisoformat(at.replace("Z", "+00:00"))
        .astimezone(ZoneInfo(r.timezone))
        .date()
    )
    if intent == "flush_notices":
        try:
            window = date.fromisoformat(data["window"])
        except (ValueError, TypeError):
            raise Rejected(
                "invalid_input", "Expected an original YYYY-MM-DD window"
            ) from None
        if str(window) != data["window"] or window > today or r.mode != "daily":
            raise Rejected("invalid_input", "Invalid daily aggregation window")
        eligible = [
            item
            for item in state.get("notices", {}).values()
            if item["route"] == asdict(r)
            and item["window"] == str(window)
            and item["resolution"] is None
            and current(state, item)
            and not live_batches(state, item["notice_id"])
        ]
        events = batch_events(state, eligible, r, operation)
        return Decision(
            events,
            {
                "batch_ids": [
                    e["payload"]["batch_id"]
                    for e in events
                    if e["kind"] == "NoticeBatchCaptured"
                ]
            },
        )
    resources = workflow.resource_paths(data["resources"])
    # Candidate freeze prevents result mutation, not requesting its required review.
    # owner() already proves the current principal; retain its exact resource boundary.
    if any(not workflow.within(r, state["resources"]) for r in resources) or any(
        grant["active"]
        and any(
            workflow.overlaps(r, other)
            for r in resources
            for other in grant["resources"]
        )
        for grant in state["grants"].values()
    ):
        raise Rejected("authority_denied", "Notice exceeds current resource authority")
    text = data["text"]
    if not isinstance(text, str) or not text.strip() or "\0" in text:
        raise Rejected("invalid_input", "Notice text must be nonempty UTF-8")
    if len(text.encode()) > r.max_bytes or len(state.get("notices", {})) >= 1024:
        raise Rejected("invalid_input", "Notice exceeds its installed bound")
    purpose = data["purpose"]
    attention = data["attention"]
    if purpose == "operational":
        if r.audience != "operational" or attention is not None:
            raise Rejected(
                "authority_denied", "Operational route cannot request owner attention"
            )
    elif purpose == "candidate_review":
        fields(attention, {"candidate_id"})
        if (
            r.audience != "owner_attention"
            or state["active_candidate"] is None
            or attention["candidate_id"] != state["active_candidate"]
            or review.progress(state) != "awaiting_review"
        ):
            raise Rejected(
                "domain_conflict", "Current unresolved candidate review required"
            )
    elif purpose == "explicit_decision":
        fields(attention, {"reason"})
        workflow.nonempty(attention["reason"])
        if r.audience != "owner_attention":
            raise Rejected("authority_denied", "Owner attention route is required")
        human_decision(context, intent, data, wi)
    else:
        raise Rejected("invalid_input", "Unknown notice purpose")
    notice = {
        "notice_id": operation,
        "route": asdict(r),
        "resources": resources,
        "text": text,
        "purpose": purpose,
        "attention": attention,
        "window": str(today),
        "at": at,
        "source": context.source.sha256,
        "source_revision": context.observed_revision,
        "principal": context.principal(),
        "generation": state["generation"],
        "definition_revision": state["definition_revision"],
        "candidate_id": state["active_candidate"],
        "resolution": None,
    }
    notice["authority_fingerprint"] = digest(
        {
            k: notice[k]
            for k in (
                "principal",
                "generation",
                "definition_revision",
                "candidate_id",
                "resources",
                "purpose",
                "attention",
                "source",
            )
        }
    )
    events = [event("NoticeRecorded", **notice)]
    if r.mode == "immediate":
        events.extend(batch_events(state, [notice], r, operation))
    return Decision(events, {"notice_id": operation})


def can_begin(state: Json, effect: Json, context: HostContext) -> bool:
    snapshot = effect["snapshot"]
    r = route(context, effect["target"])
    if snapshot["route"] != asdict(r):
        raise Rejected(
            "authority_changed", "Original notification route binding changed"
        )
    return all(
        state["notices"][key]["resolution"] is None
        and current(state, state["notices"][key])
        for key in snapshot["notice_ids"]
    )


def validate_observation(effect: Json, outcome: str, details: Json) -> None:
    if outcome != "success":
        return
    snapshot = effect["snapshot"]
    r = snapshot["route"]
    if (
        details.get("route_digest") != snapshot["route_digest"]
        or details.get("text_sha256") != snapshot["text_sha256"]
        or details.get("channel") != r["channel"]
        or details.get("recipient_digest") != r["recipient_digest"]
        or not isinstance(details.get("external_message_id"), str)
        or not 1 <= len(details["external_message_id"]) <= 256
        or not details["external_message_id"].strip()
    ):
        raise Rejected(
            "source_invalid", "Actual channel receipt does not prove this notice"
        )


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind == "NoticeRecorded":
        state.setdefault("notices", {})[data["notice_id"]] = data
    elif kind == "NoticeBatchCaptured":
        state.setdefault("notice_batches", {})[data["batch_id"]] = data
    elif kind == "NoticeResolved":
        state["notices"][data["notice_id"]]["resolution"] = data
    else:
        return False
    return True


def items(state: Json) -> list[Json]:
    result = []
    for notice in state.get("notices", {}).values():
        batches = live_batches(state, notice["notice_id"])
        effects = [state["effects"][b["effect_id"]] for b in batches]
        result.append(
            {
                k: notice[k]
                for k in (
                    "notice_id",
                    "resources",
                    "text",
                    "purpose",
                    "attention",
                    "window",
                    "at",
                    "source_revision",
                    "generation",
                    "definition_revision",
                    "candidate_id",
                    "resolution",
                    "authority_fingerprint",
                )
            }
            | {
                "route_id": notice["route"]["route_id"],
                "channel": notice["route"]["channel"],
                "current_attention": current(state, notice)
                and notice["resolution"] is None,
                "delivery_status": effects[-1]["status"]
                if effects
                else "pending_batch",
                "effect_ids": [e["effect_id"] for e in effects],
                "batch_ids": [b["batch_id"] for b in batches],
            }
        )
    return result


def matches(item: Json, status: str | None) -> bool:
    if status is None:
        return True
    if status == "resolved":
        return item["resolution"] is not None
    if status == "open":
        return item["resolution"] is None
    if status == "failed":
        return bool(item["delivery_status"] == "observed_failure")
    if status == "unresolved_send":
        return bool(item["delivery_status"] in {"attempt_started", "unknown"})
    return bool(
        item["resolution"] is None
        and item["current_attention"]
        and item["delivery_status"] == "pending_batch"
    )


def query(state: Json, query: Query, revision: int) -> Json:
    if query.status is not None and query.status not in FILTERS:
        raise Rejected("invalid_input", "Unknown notice query filter")
    values = [item for item in items(state) if matches(item, query.status)]
    page = values[query.after : query.after + query.limit]
    return {
        "source": "journal",
        "revision": revision,
        "items": page,
        "next_offset": query.after + len(page)
        if query.after + len(page) < len(values)
        else None,
    }


def public(state: Json) -> Json:
    values = items(state)
    return {
        "source": "journal",
        "count": len(values),
        **{
            status: sum(matches(v, status) for v in values)
            for status in sorted(FILTERS)
        },
    }
