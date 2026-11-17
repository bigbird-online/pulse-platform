"""Read the replacement Harness measurement query without its code or authority."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any

SCHEMA = "computecost_harness_query_report/v1"
TOKENS = (
    "input",
    "output",
    "cacheRead",
    "cacheWrite",
    "totalTokens",
    "reasoning",
    "cacheWrite1h",
)
REQUIRED_TOKENS = set(TOKENS[:5])
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")


class HarnessQueryError(ValueError):
    """The explicit query cannot be used as the requested observation."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise HarnessQueryError(message)


def object_fields(
    value: Any, required: set[str], optional: set[str] | None = None
) -> dict[str, Any]:
    require(
        isinstance(value, dict)
        and required <= set(value) <= required | (optional or set()),
        "Harness measurement fields do not match the contract",
    )
    return dict(value)


def integer(value: Any, minimum: int = 0) -> int:
    require(
        type(value) is int and minimum <= value <= 2**63 - 1,
        "Harness count or boundary is invalid",
    )
    return int(value)


def number(value: Any, maximum: float = 1e18) -> float:
    require(
        type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum,
        "Harness amount is invalid",
    )
    return float(value)


def identifier(value: Any) -> str:
    require(
        isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None,
        "Harness logical identity is invalid",
    )
    return str(value)


def checksum(value: Any) -> str:
    require(
        isinstance(value, str) and SHA.fullmatch(value) is not None,
        "Harness evidence digest is invalid",
    )
    return str(value)


def sequence(value: Any) -> list[Any]:
    require(isinstance(value, list), "Harness observation collection is invalid")
    return list(value)


def safe_measurements(value: Any) -> None:
    if isinstance(value, dict):
        require(
            not set(value)
            & {
                "principal",
                "native_session_id",
                "provider",
                "model",
                "api_key",
                "credential",
                "path",
                "prompt",
                "message",
                "body",
                "base64",
            },
            "Harness measurement contains private source fields",
        )
        for key, item in value.items():
            if key == "session":
                require(
                    isinstance(item, str)
                    and item.startswith("measurement-session.")
                    and SHA.fullmatch(item.removeprefix("measurement-session."))
                    is not None,
                    "Harness measurement contains an unscoped native identity",
                )
            safe_measurements(item)
    elif isinstance(value, list):
        for item in value:
            safe_measurements(item)


def observed_counter(
    value: Any, messages: int, *, money: bool = False
) -> dict[str, Any]:
    item = object_fields(value, {"value", "observed_messages", "coverage"})
    count = integer(item["observed_messages"])
    require(count <= messages, "Harness observation count exceeds its messages")
    coverage = (
        "unavailable" if count == 0 else "complete" if count == messages else "partial"
    )
    require(
        item["coverage"] == coverage and (item["value"] is None) == (count == 0),
        "Harness counter coverage contradicts its observations",
    )
    if count:
        number(item["value"]) if money else integer(item["value"])
    return item


def report(query: Any, work_item: str, source_digest: str) -> dict[str, Any]:
    require(
        isinstance(query, dict) and query.get("schema") == "harness.response/v1",
        "Unsupported Harness query schema",
    )
    require(
        query.get("status") == "read_only" and query.get("error") is None,
        "Harness source must be a successful read-only query",
    )
    wi = object_fields(
        query.get("work_item"),
        {"id", "generation", "local_revision", "business_progress"},
    )
    require(
        identifier(wi["id"]) == identifier(work_item),
        "Harness query belongs to another WI",
    )
    require(
        wi["business_progress"]
        in {
            "draft",
            "active",
            "transferring",
            "migration_sealed",
            "migration_prepared",
            "stopped",
            "terminated",
            "completed",
            "awaiting_verification",
            "awaiting_review",
            "needs_revision",
            "approved",
        },
        "Unknown Harness business progress",
    )
    revision, generation = integer(wi["local_revision"], 1), integer(wi["generation"])
    capabilities = sequence(query.get("capabilities"))
    require(
        any(
            isinstance(c, dict)
            and c.get("name") == "local_read"
            and c.get("state") == "available"
            and type(c.get("source_revision")) is int
            and c.get("source_revision") == revision
            for c in capabilities
        ),
        "Harness query lacks matching read capability",
    )
    result = query.get("result")
    require(isinstance(result, dict), "Harness query result is invalid")
    data = object_fields(
        result.get("items"),
        {
            "source",
            "intervals",
            "coverage",
            "usage",
            "attention",
            "quality",
            "outcomes",
            "business_progress",
            "terminal",
        },
    )
    current = result.get("current_observation")
    require(
        isinstance(current, dict)
        and current.get("source") == "journal"
        and current.get("measurements") == data
        and current.get("generation") == generation
        and current.get("business_progress") == wi["business_progress"],
        "Harness query observations disagree",
    )
    require(
        data["source"] == "journal"
        and data["coverage"] in {"unavailable", "recorded_intervals_only"},
        "Harness query coverage is invalid",
    )
    expected_progress = (
        "active"
        if wi["business_progress"]
        in {"awaiting_verification", "awaiting_review", "needs_revision", "approved"}
        else wi["business_progress"]
    )
    require(
        data["business_progress"] == expected_progress,
        "Harness measurement progress contradicts its query",
    )
    safe_measurements(data)
    intervals: dict[str, dict[str, Any]] = {}
    for value in sequence(data["intervals"]):
        item = object_fields(
            value,
            {
                "interval_id",
                "task_attempt",
                "start_byte",
                "end_byte",
                "closed",
                "end_boundary",
                "phase",
                "generation",
                "session",
            },
        )
        key = identifier(item["interval_id"])
        require(key not in intervals, "Duplicate Harness interval")
        start = integer(item["start_byte"])
        end = item["end_byte"]
        require(type(item["closed"]) is bool, "Harness interval closure is invalid")
        if end is not None:
            require(
                integer(end) >= start
                and item["closed"]
                and item["end_boundary"] == "observed",
                "Harness interval end is invalid",
            )
        else:
            require(
                item["end_boundary"] == ("unavailable" if item["closed"] else "open"),
                "Harness interval boundary is invalid",
            )
        require(
            integer(item["generation"]) <= generation,
            "Harness interval is from a future generation",
        )
        if item["task_attempt"] is not None:
            identifier(item["task_attempt"])
        intervals[key] = item
    ids: set[str] = set()
    ranges: dict[str, list[tuple[int, int]]] = {}
    usage = []
    totals: dict[str, dict[str, Any]] = {
        name: {"value": None, "observed_messages": 0} for name in TOKENS
    }
    costs: dict[str, dict[str, Any]] = {}
    messages = 0

    def record(value: Any, kind: str, fields: set[str]) -> dict[str, Any]:
        item = object_fields(
            value, fields | {"kind", "record_id", "generation", "recorded_at"}
        )
        require(item["kind"] == kind, "Harness record kind mismatch")
        key = identifier(item["record_id"])
        require(key not in ids, "Duplicate Harness observation")
        ids.add(key)
        require(
            integer(item["generation"]) <= generation,
            "Harness observation is from a future generation",
        )
        try:
            require(
                datetime.fromisoformat(item["recorded_at"]).tzinfo is not None,
                "Harness observation time needs a timezone",
            )
        except (TypeError, ValueError) as exc:
            raise HarnessQueryError("Harness observation timestamp is invalid") from exc
        return item

    for value in sequence(data["usage"]):
        item = record(
            value,
            "usage",
            {
                "schema",
                "runtime",
                "start_byte",
                "end_byte",
                "source_sha256",
                "evidence",
                "groups",
                "coverage",
                "extraction",
                "task_attempt",
                "interval_id",
                "session",
                "actor",
            },
        )
        require(
            item["schema"] == "harness.native-usage/v1" and item["runtime"] == "pi",
            "Unsupported native Harness usage",
        )
        interval = intervals.get(identifier(item["interval_id"]))
        require(interval is not None, "Harness usage has no original interval")
        assert interval is not None
        start, end = integer(item["start_byte"]), integer(item["end_byte"])
        require(
            start < end
            and end - start <= 16 * 1024 * 1024
            and start >= interval["start_byte"]
            and (interval["end_byte"] is None or end <= interval["end_byte"]),
            "Harness usage exceeds its observed native boundaries",
        )
        require(
            item["session"] == interval["session"]
            and item["task_attempt"] == interval["task_attempt"],
            "Harness usage attribution conflicts with its interval",
        )
        prior = ranges.setdefault(item["session"], [])
        require(
            not any(start < b and a < end for a, b in prior),
            "Overlapping Harness native usage",
        )
        prior.append((start, end))
        evidence = object_fields(
            item["evidence"], {"object", "version", "sha256", "size", "access"}
        )
        require(
            evidence["access"] == "private"
            and evidence["size"] == end - start
            and checksum(evidence["sha256"]) == checksum(item["source_sha256"]),
            "Harness usage evidence does not match its range",
        )
        groups = []
        configurations: set[str] = set()
        for raw_group in sequence(item["groups"]):
            group = object_fields(
                raw_group,
                {
                    "configuration",
                    "messages",
                    "observed_messages",
                    "tokens",
                    "costs",
                    "cost_coverage",
                },
            )
            config = checksum(group["configuration"])
            require(
                config not in configurations, "Duplicate Harness configuration group"
            )
            configurations.add(config)
            count = integer(group["messages"], 1)
            require(
                integer(group["observed_messages"]) <= count,
                "Harness usage messages are inconsistent",
            )
            token_values = object_fields(
                group["tokens"], REQUIRED_TOKENS, set(TOKENS) - REQUIRED_TOKENS
            )
            for name, counter in token_values.items():
                validated = observed_counter(counter, count)
                require(
                    validated["observed_messages"] <= group["observed_messages"],
                    "Token observation lacks a usage message",
                )
                aggregate = totals[name]
                if validated["value"] is not None:
                    aggregate["value"] = (aggregate["value"] or 0) + validated["value"]
                    aggregate["observed_messages"] += validated["observed_messages"]
            require(
                isinstance(group["costs"], dict),
                "Harness currency observations are invalid",
            )
            money_count = 0
            for currency, counter in group["costs"].items():
                require(
                    isinstance(currency, str)
                    and re.fullmatch("[A-Z]{3}", currency) is not None,
                    "Harness currency is invalid",
                )
                validated = observed_counter(counter, count, money=True)
                require(
                    validated["observed_messages"] > 0,
                    "Empty Harness currency observation",
                )
                money_count += validated["observed_messages"]
                aggregate = costs.setdefault(
                    currency, {"value": 0.0, "observed_messages": 0}
                )
                aggregate["value"] += validated["value"]
                aggregate["observed_messages"] += validated["observed_messages"]
            require(
                money_count <= group["observed_messages"],
                "Harness currency observations overlap",
            )
            expected = (
                "unavailable"
                if not money_count
                else "complete"
                if money_count == count
                else "partial"
            )
            require(
                group["cost_coverage"] == expected,
                "Harness cost coverage is inconsistent",
            )
            messages += count
            groups.append(group)
        detailed = any(set(g["tokens"]) - REQUIRED_TOKENS for g in groups)
        require(
            item["extraction"]
            == "deterministic-native-pi-jsonl/v" + ("2" if detailed else "1"),
            "Harness extraction version is inconsistent",
        )
        any_observed = any(
            any(c["observed_messages"] for c in g["tokens"].values())
            or bool(g["costs"])
            for g in groups
        )
        complete = all(
            all(c["coverage"] == "complete" for c in g["tokens"].values())
            and g["cost_coverage"] == "complete"
            for g in groups
        )
        require(
            item["coverage"]
            == (
                "unavailable"
                if not any_observed
                else "complete"
                if complete
                else "partial"
            ),
            "Harness usage coverage is inconsistent",
        )
        usage.append(
            {
                key: item[key]
                for key in (
                    "record_id",
                    "generation",
                    "interval_id",
                    "task_attempt",
                    "start_byte",
                    "end_byte",
                    "source_sha256",
                    "coverage",
                )
            }
            | {
                "groups": groups,
                "interval_end_boundary": interval["end_boundary"],
                "interval_end_byte": interval["end_byte"],
            }
        )
    for aggregate in [*totals.values(), *costs.values()]:
        count = aggregate["observed_messages"]
        aggregate["coverage"] = (
            "unavailable"
            if count == 0
            else "complete"
            if count == messages
            else "partial"
        )
    attention = []
    for value in sequence(data["attention"]):
        item = record(
            value,
            "attention",
            {
                "task_attempt",
                "category",
                "duration_seconds",
                "source_sha256",
                "duration_basis",
                "category_basis",
            },
        )
        checksum(item["source_sha256"])
        require(
            item["category"]
            in {"instruction", "clarification", "review", "correction", "other"}
            and item["category_basis"] == "human_confirmed",
            "Harness human category is invalid",
        )
        if item["task_attempt"] is not None:
            identifier(item["task_attempt"])
        duration = item["duration_seconds"]
        require(
            item["duration_basis"]
            == ("unavailable" if duration is None else "human_self_report"),
            "Harness duration basis is inconsistent",
        )
        if duration is not None:
            number(duration, 86400)
        attention.append(
            {
                k: item[k]
                for k in (
                    "record_id",
                    "task_attempt",
                    "category",
                    "duration_seconds",
                    "duration_basis",
                    "source_sha256",
                )
            }
        )
    quality = []
    quality_keys: set[tuple[str, str, str]] = set()
    for value in sequence(data["quality"]):
        item = record(
            value,
            "quality",
            {
                "candidate_id",
                "rubric",
                "version",
                "score",
                "evidence",
                "assignment_id",
                "source_sha256",
                "coverage",
                "verifier",
            },
        )
        quality_key = (
            identifier(item["candidate_id"]),
            identifier(item["rubric"]),
            identifier(item["version"]),
        )
        require(
            quality_key not in quality_keys, "Conflicting Harness quality observation"
        )
        quality_keys.add(quality_key)
        checksum(item["source_sha256"])
        evidence_rows = sequence(item["evidence"])
        require(bool(evidence_rows), "Harness quality lacks evidence")
        for ref in evidence_rows:
            evidence_ref = object_fields(
                ref, {"object", "version", "sha256", "size", "access"}
            )
            checksum(evidence_ref["sha256"])
            integer(evidence_ref["size"])
        if item["score"] is not None:
            number(item["score"], 1)
        require(
            item["coverage"]
            == ("unavailable" if item["score"] is None else "observed"),
            "Harness quality coverage is inconsistent",
        )
        quality.append(
            {
                k: item[k]
                for k in (
                    "record_id",
                    "candidate_id",
                    "rubric",
                    "version",
                    "score",
                    "coverage",
                    "source_sha256",
                )
            }
        )
    outcomes = []
    candidates: set[str] = set()
    for value in sequence(data["outcomes"]):
        item = object_fields(
            value,
            {
                "candidate_id",
                "current",
                "result_ids",
                "definition_revision",
                "graph_revision",
                "verification",
                "review",
                "waiver",
                "coverage",
            },
        )
        candidate = identifier(item["candidate_id"])
        require(
            candidate not in candidates and type(item["current"]) is bool,
            "Harness candidate observation is invalid",
        )
        integer(item["definition_revision"], 1)
        integer(item["graph_revision"], 1)
        for result_id in sequence(item["result_ids"]):
            identifier(result_id)
        candidates.add(candidate)
        row = {
            k: item[k]
            for k in (
                "candidate_id",
                "current",
                "definition_revision",
                "graph_revision",
                "coverage",
            )
        }
        for name, field, options in [
            ("verification", "verdict", {"accepted", "partial", "blocked", "rejected"}),
            ("review", "decision", {"approve", "changes_requested"}),
        ]:
            value = item[name]
            if value is None:
                row[name] = None
            else:
                require(
                    isinstance(value, dict)
                    and value.get("candidate_id") == candidate
                    and value.get(field) in options,
                    "Harness candidate control observation is inconsistent",
                )
                row[name] = {"id": identifier(value["id"]), field: value[field]}
        require(
            item["coverage"]
            == ("unavailable" if item["verification"] is None else "observed"),
            "Harness outcome coverage is inconsistent",
        )
        row["waiver_present"] = item["waiver"] is not None
        outcomes.append(row)
    require(
        sum(item["current"] for item in outcomes) <= 1,
        "Multiple Harness candidates claim to be current",
    )
    require(
        all(q["candidate_id"] in candidates for q in quality),
        "Harness quality references an unknown candidate",
    )
    require(
        data["coverage"] == ("recorded_intervals_only" if ids else "unavailable"),
        "Harness overall measurement coverage is inconsistent",
    )
    durations = [
        a["duration_seconds"] for a in attention if a["duration_seconds"] is not None
    ]
    return {
        "schema": SCHEMA,
        "work_item_id": work_item,
        "source": {
            "kind": "explicit_harness_query",
            "sha256": checksum(source_digest),
            "journal_revision": revision,
            "generation": generation,
            "freshness": "unknown_without_new_query",
            "authority_verification": "not_independently_attested_by_file",
        },
        "business_progress": wi["business_progress"],
        "coverage": data["coverage"],
        "whole_WI_lifetime_covered": False,
        "usage": {
            "records": usage,
            "messages": messages,
            "totals": totals,
            "costs_by_observed_currency": costs,
            "cost_basis": "native_observation_not_billing_or_pricing",
            "details_added_to_total": False,
        },
        "human_attention": {
            "events": attention,
            "event_count": len(attention),
            "self_reported_seconds": sum(durations) if durations else None,
            "duration_coverage": "unavailable"
            if not durations
            else "complete"
            if len(durations) == len(attention)
            else "partial",
        },
        "quality": quality,
        "outcomes": outcomes,
        "completion_authority": False,
    }


def load_report(path: Path, work_item: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON member in Harness snapshot")
            result[key] = value
        return result

    require(
        path.is_file() and not path.is_symlink(),
        "Harness snapshot must be a regular non-symlink file",
    )
    with path.open("rb") as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    require(len(raw) <= 32 * 1024 * 1024, "Harness snapshot exceeds its read bound")
    try:
        query = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                HarnessQueryError("Nonfinite Harness JSON value")
            ),
        )
        return report(query, work_item, sha256(raw).hexdigest())
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        if isinstance(exc, HarnessQueryError):
            raise
        raise HarnessQueryError("Malformed Harness measurement snapshot") from exc
