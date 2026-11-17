"""Fixed recorded-evidence checks. No lifecycle writer or live probe authority."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from .model import Json, Query, Rejected, digest, encoded, fields, identity

MAX_BYTES = 2 * 1024 * 1024
KINDS = {"file_exists", "command", "pattern_match", "temporal_check"}
Result = tuple[str, str, list[str]]


def _pairs(pairs: list[tuple[str, Any]]) -> Json:
    result: Json = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("Nonfinite JSON")


def _strings(value: Any, maximum: int = 1000) -> bool:
    return (
        isinstance(value, list)
        and 0 < len(value) <= maximum
        and all(isinstance(v, str) and bool(v) for v in value)
        and len(set(value)) == len(value)
    )


def _observed(row: Json) -> bool:
    return (
        row.get("evidence") == "observed"
        and isinstance(row.get("source_ref"), str)
        and bool(row["source_ref"].strip())
    )


def _find(facts: Json, family: str, key: str, value: str) -> tuple[Json, list[str]]:
    found = [
        (row, "/facts/" + family + "/" + str(i))
        for i, row in enumerate(facts.get(family, []))
        if isinstance(row, dict) and row.get(key) == value
    ]
    if len(found) != 1:
        return {}, [pointer for _, pointer in found]
    return found[0][0], [found[0][1]]


def _combine(results: list[Result]) -> Result:
    if not results:
        return "unknown", "no_observed_inputs", []
    status, reason, _ = next(
        (r for r in results if r[0] == "violated"),
        next((r for r in results if r[0] == "unknown"), results[0]),
    )
    return status, reason, sorted({p for r in results for p in r[2]})


def _files(check: Json, facts: Json) -> Result:
    fields(check, {"paths"}, {"nonempty"})
    if not _strings(check["paths"]) or type(check.get("nonempty", True)) is not bool:
        raise ValueError("Invalid file check")
    results = []
    for path in check["paths"]:
        row, pointers = _find(facts, "files", "path", path)
        status, reason = "unknown", "file_observation_unavailable"
        if _observed(row):
            if row.get("state") == "absent":
                status, reason = "violated", "recorded_file_absent"
            elif row.get("state") == "present":
                size = row.get("bytes")
                if not check.get("nonempty", True):
                    status, reason = "passed", "recorded_file_present"
                elif type(size) is int and size >= 0:
                    status, reason = (
                        ("passed", "recorded_file_nonempty")
                        if size > 0
                        else ("violated", "recorded_file_empty")
                    )
        results.append((status, reason, pointers))
    return _combine(results)


def _command(check: Json, facts: Json) -> Result:
    fields(check, {"command_id"})
    identity(check["command_id"])
    row, pointers = _find(facts, "commands", "id", check["command_id"])
    status, reason = "unknown", "command_observation_unavailable"
    if (
        _observed(row)
        and row.get("state") == "completed"
        and type(row.get("exit_code")) is int
    ):
        status, reason = (
            ("passed", "recorded_exit_zero")
            if row["exit_code"] == 0
            else ("violated", "recorded_exit_nonzero")
        )
    return status, reason, pointers


def _temporal(check: Json, facts: Json) -> Result:
    fields(check, {"objects"})
    if not _strings(check["objects"]):
        raise ValueError("Invalid temporal scope")
    events = facts.get("events", [])
    relevant = [
        (row, "/facts/events/" + str(i))
        for i, row in enumerate(events)
        if isinstance(row, dict) and row.get("object") in check["objects"]
    ]
    pointers = [p for _, p in relevant]
    if any(
        type(r.get("sequence")) is not int
        or r["sequence"] <= 0
        or not isinstance(r.get("version"), str)
        or not r["version"]
        or r.get("kind") not in {"confirmed", "written"}
        for r, _ in relevant
    ) or len({r["sequence"] for r, _ in relevant}) != len(relevant):
        return "unknown", "event_order_unavailable", pointers
    results: list[Result] = []
    for obj in check["objects"]:
        writes = [
            (r, p) for r, p in relevant if r["object"] == obj and r["kind"] == "written"
        ]
        if not writes:
            results.append(("unknown", "no_observed_writes", pointers))
        for write, pointer in writes:
            confirmations = [
                (r, p)
                for r, p in relevant
                if r["object"] == obj
                and r["version"] == write["version"]
                and r["kind"] == "confirmed"
                and r["sequence"] < write["sequence"]
            ]
            if not _observed(write):
                results.append(("unknown", "write_provenance_unavailable", [pointer]))
            elif any(_observed(r) for r, _ in confirmations):
                results.append(
                    (
                        "passed",
                        "recorded_confirmation_precedes_write",
                        [pointer] + [p for _, p in confirmations],
                    )
                )
            elif facts.get("events_complete") is True and all(
                _observed(r) for r, _ in confirmations
            ):
                results.append(
                    ("violated", "recorded_write_without_prior_confirmation", pointers)
                )
            else:
                results.append(
                    ("unknown", "confirmation_or_coverage_unavailable", pointers)
                )
    return _combine(results)


def _pattern(check: Json, facts: Json, jobs: Json, index: int) -> Result:
    fields(check, {"field", "patterns"})
    identity(check["field"])
    patterns = check["patterns"]
    if not _strings(patterns, 32) or any(len(p) > 256 for p in patterns):
        raise ValueError("Invalid patterns")
    row, pointers = _find(facts, "fields", "name", check["field"])
    values = row.get("values")
    if (
        not _observed(row)
        or not isinstance(values, list)
        or len(values) > 32
        or any(not isinstance(v, str) or len(v) > 65536 for v in values)
    ):
        return "unknown", "field_observation_unavailable", pointers
    job = {"patterns": patterns, "values": values}
    if len(encoded(jobs)) + len(encoded(job)) > MAX_BYTES:
        return "unknown", "pattern_budget_exhausted", pointers
    jobs[str(index)] = job
    return "unknown", "pattern_pending", pointers


# Fixed program, no shell or dynamic imports. Evidence text never enters argv.
_PATTERN_WORKER = """
import json,re,sys
result={}
for key,job in json.load(sys.stdin).items():
    try:
        patterns=[re.compile(p,re.IGNORECASE) for p in job['patterns']]
        result[key]='matched' if any(p.search(v) for p in patterns for v in job['values']) else 'not_matched'
    except (re.error,RecursionError):
        result[key]='invalid_pattern'
print(json.dumps(result))
"""


def _patterns(jobs: Json) -> Json:
    if not jobs:
        return {}
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", _PATTERN_WORKER],
            input=encoded(jobs),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=True,
        )
        output = json.loads(result.stdout)
        if (
            not isinstance(output, dict)
            or set(output) != set(jobs)
            or any(
                v not in {"matched", "not_matched", "invalid_pattern"}
                for v in output.values()
            )
        ):
            raise ValueError("Invalid worker output")
        return output
    except (OSError, subprocess.SubprocessError, ValueError):
        return {key: "budget_or_worker_unavailable" for key in jobs}


def assess(content: bytes, reference: Json, revision: int, query: Query) -> Json:
    """Evaluate fixed recorded claims; every rule points back to immutable bytes."""
    if len(content) > MAX_BYTES:
        raise Rejected("invalid_input", "Check evidence exceeds the report limit")
    try:
        packet = json.loads(content, object_pairs_hook=_pairs, parse_constant=_constant)
        fields(packet, {"schema", "action", "rules", "facts"})
        if packet["schema"] != "harness.check-evidence/v1":
            raise ValueError("Unsupported evidence schema")
        identity(packet["action"])
        rules, facts = packet["rules"], packet["facts"]
        if (
            not isinstance(rules, list)
            or not 1 <= len(rules) <= 100
            or not isinstance(facts, dict)
        ):
            raise ValueError("Invalid evidence envelope")
        fields(
            facts, set(), {"files", "commands", "fields", "events", "events_complete"}
        )
        if any(
            not isinstance(facts.get(k, []), list)
            or len(facts.get(k, [])) > 1000
            or any(not isinstance(row, dict) for row in facts.get(k, []))
            for k in ("files", "commands", "fields", "events")
        ) or (
            "events_complete" in facts and type(facts["events_complete"]) is not bool
        ):
            raise ValueError("Invalid evidence collection")
        ids = [
            r["id"]
            for r in rules
            if isinstance(r, dict) and isinstance(r.get("id"), str)
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate rule IDs")
    except (ValueError, TypeError, RecursionError, Rejected):
        raise Rejected(
            "invalid_input", "Invalid bounded check evidence packet"
        ) from None
    items: list[Json] = []
    jobs: Json = {}
    for index, rule in enumerate(rules):
        item: Json = {
            "id": None,
            "kind": "unsupported",
            "applicable": None,
            "status": "unknown",
            "reason": "invalid_rule",
            "rule_pointer": "/rules/" + str(index),
            "evidence_pointers": [],
            "rule_source_digest": None,
        }
        try:
            fields(rule, {"id", "kind", "actions", "source_ref", "check"})
            item["id"] = identity(rule["id"])
            if (
                not _strings(rule["actions"], 100)
                or not isinstance(rule["source_ref"], str)
                or not rule["source_ref"].strip()
            ):
                raise ValueError("Rule source or actions unavailable")
            for action in rule["actions"]:
                identity(action)
            item["rule_source_digest"] = digest(rule["source_ref"])
            item["applicable"] = packet["action"] in rule["actions"]
            kind, check = rule["kind"], rule["check"]
            if not isinstance(kind, str) or kind not in KINDS:
                item["reason"] = "unsupported_rule_kind"
            else:
                item["kind"] = kind
                if not item["applicable"]:
                    item["reason"] = "action_not_declared"
                else:
                    result = (
                        _files(check, facts)
                        if kind == "file_exists"
                        else _command(check, facts)
                        if kind == "command"
                        else _temporal(check, facts)
                        if kind == "temporal_check"
                        else _pattern(check, facts, jobs, index)
                    )
                    item["status"], item["reason"], item["evidence_pointers"] = result
        except (ValueError, TypeError, KeyError, Rejected):
            item["reason"] = "invalid_rule_or_check"
        items.append(item)
    for job_index, result in _patterns(jobs).items():
        items[int(job_index)]["status"], items[int(job_index)]["reason"] = {
            "matched": ("passed", "recorded_field_matches"),
            "not_matched": ("violated", "recorded_field_has_no_match"),
            "invalid_pattern": ("unknown", "invalid_pattern"),
            "budget_or_worker_unavailable": (
                "unknown",
                "pattern_budget_or_worker_unavailable",
            ),
        }[result]
    return {
        "schema": "harness.check-report/v1",
        "source": "recorded_artifact",
        "source_revision": revision,
        "artifact": reference,
        "action": packet["action"],
        "evidence_trust": "recorded_artifact_claims",
        "human_authority": False,
        "lifecycle_mutation": False,
        "total_count": len(items),
        "counts": {
            s: sum(i["status"] == s for i in items)
            for s in ("passed", "violated", "unknown")
        },
        "items": items[query.after : query.after + query.limit],
        "next_offset": query.after + query.limit
        if len(items) > query.after + query.limit
        else None,
    }
