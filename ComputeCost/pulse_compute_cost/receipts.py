"""Versioned, privacy-safe collaboration facts; no workflow or authority engine.

The envelope only identifies evidence. Each domain has its own closed payload.
Records are stored by UsageLedger, never priced or attributed by the model.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from sys import getsizeof
from typing import Any, Mapping, Iterable

from .events import parse_timestamp, utc_text
from .stage_facts import STAGE_FIELDS, validate_stage_payload

SCHEMA = "computecost_collaboration_fact/v1"
POLICY = "wi_collaboration/1"
SUPPORTED_RECEIPT_FEATURES = frozenset({"actor_context.v1", "pi_parent_history.v1", "non_wi_activity.v1", "wi_outcome_context.v1", "host_native_input.v1", "runtime_subcall.v1", "native_metering.v1", "tenant_context.v1", "verification_workspace.v1", "tenant_relay_input.v1", "stage_semantics.v1", "production_observations.v1", "holder_supplement.v1", "native_input.v1"})
TOKEN_FIELDS = ("uncached_input", "cache_read", "cache_write", "output")
NATIVE_TOKEN_FIELDS = frozenset({"input", "output", "cacheRead", "cacheWrite", "totalTokens", "reasoning",
    "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "total_tokens", "inputTokens", "outputTokens",
    "cachedInputTokens", "cacheWriteInputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"})
ROLES = ("executor", "responsible", "delegator", "reviewer")
REF = re.compile(r"^ccf1_[a-f0-9]{64}$")
WI = re.compile(r"^wi_\d{8}_\d{3}$")
FORBIDDEN = frozenset({"prompt", "content", "text", "response", "credential", "session_id",
    "thread_id", "request_id", "message_id", "token", "api_key", "path", "raw_id"})

# All fields must be present, including explicitly unknown (null) ones.
COMMON = {"wi", "executor", "responsible", "delegator"}
FIELDS = {
    **STAGE_FIELDS,
    "output_assessment": {"wi", "candidate_ref", "version_refs", "scope", "gate_refs", "criterion_refs",
        "assessment", "result", "rubric_ref", "score", "checks", "source_ref", "native_revision"},
    # An epoch expectation is a control fact, never another usage row/charge.
    "execution_expectation": {"wi", "executor", "runtime", "scope", "source_ref", "assignment_ref",
        "usage_ref", "started_at", "ended_at", "status"},
    "execution": COMMON | {"runtime", "assignment_ref", "parent_ref", "state", "expected_usage"},
    # native_scope_ref is deliberately a session-range correlation, not an attempt/WI claim.
    "usage": COMMON | {"assignment_ref", "execution_ref", "native_scope_ref", "reported_total_tokens", "runtime", "model", "provider", "tokens", "native_tokens", "mapping",
        "granularity", "coverage_ref", "covers", "interval_start", "interval_end", "final", "metering"},
    "attribution": {"target_ref", "wi", "executor", "responsible", "delegator", "state", "policy", "effective_at", "execution_ref", "assignment_ref", "scope", "context", "work_kind", "usage_fact_ref", "job"},
    "human": COMMON | {"execution_ref", "origin", "category", "band", "focused_seconds", "method"},
    "delivery": {"wi", "responsible", "candidate_ref", "artifact_ref", "acceptance_ref", "status", "accepted_at", "outcome_context"},
    "review": {"wi", "delivery_ref", "candidate_ref", "reviewer", "decision", "first_pass", "rework_rounds",
        "post_approval_corrections", "rubric", "score", "verification", "critical"},
    "value": {"wi", "delivery_ref", "artifact_ref", "effect_ref", "consumer", "verifier", "amount",
        "unit", "mode", "period_start", "period_end", "realization", "complete", "allocations"},
    "human_summary": {"wi", "count", "bands", "focused_seconds", "method"},
    "coverage": {"domain", "wi", "source_ref", "status", "expected", "observed", "watermark", "reason", "source_detail"},
    # What a node read from its own native files, for a stream reader to judge.
    "native_input": {"input_class", "source_ref", "value"},
}

class ReceiptError(ValueError):
    """Invalid or unsafe producer evidence."""


# A Relay scheduler run: its configured job, by ID and public name.
SCHEDULED_JOB_POLICY = "scheduled_job.v1"
# Attribution sources applied only where no declaration or other claim
# exists, by (producer, policy); see collaboration_query.select_supplement.
# Undeclared exploration before a request that creates a WI belongs to it;
# exploration that created none is ordinary non-WI work.
EXPLORATION_POLICY = "holder_exploration.v1"
SUPPLEMENTS = {("holder_supplement.v1", "holder_supplement.v1"): "holder_supplement",
               ("holder_supplement.v1", EXPLORATION_POLICY): "exploration",
               ("relay_host.v1", SCHEDULED_JOB_POLICY): "scheduled_job"}
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_job(job: object) -> None:
    if (not isinstance(job, Mapping) or set(job) != {"id", "name"}
            or not isinstance(job["id"], str) or not JOB_ID.fullmatch(job["id"])
            or not isinstance(job["name"], str) or not 0 < len(job["name"]) <= 120
            or job["name"] != job["name"].strip() or any(c in job["name"] for c in "\r\n\x00")):
        raise ReceiptError("scheduled job identity is invalid")


def valid_node_identifier(value: object) -> bool:
    """Execution provenance label; this never grants execution authority."""
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is not None


def canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise ReceiptError("receipt is not finite JSON") from exc


def digest(value: object) -> str:
    return sha256(canonical(value)).hexdigest()


def digest_sequence(values: Iterable[object]) -> str:
    """The same canonical array digest, retaining only one encoded value."""
    result = sha256(b"[")
    for index, value in enumerate(values):
        if index:
            result.update(b",")
        result.update(canonical(value))
    result.update(b"]")
    return result.hexdigest()


def digest_hex(chunks: Iterable[bytes | memoryview]) -> str:
    """Preserve digest(data.hex()) without materializing its doubled string."""
    result = sha256(b'"')
    for chunk in chunks:
        result.update(chunk.hex().encode("ascii"))
    result.update(b'"')
    return result.hexdigest()


def reference(domain: str, identity: object) -> str:
    """Hash native identity before crossing the producer privacy boundary."""
    return "ccf1_" + digest([domain, identity])



# A Relay dispatch without a WI only falls back to ordinary work: a written,
# created or reviewed WI of the same request decides first. Its non-WI
# classifications carry this evidence; their source label stays "classified".
HOST_FALLBACK = reference("host_non_wi", "fallback.v1")


def supplement_source(row: "Receipt") -> str | None:
    """The source label of an attribution applied only without a primary claim."""
    p = row.payload
    label = SUPPLEMENTS.get((row.producer, p.get("policy")))
    if label is None and row.producer == "non_wi_activity.v1" and p.get("work_kind") == "none" \
            and HOST_FALLBACK in row.evidence:
        return "classified"
    return label

def decimal(value: object, field: str, *, nonnegative: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise ReceiptError(f"{field} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ReceiptError(f"{field} must be a decimal string") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise ReceiptError(f"{field} is not a valid finite amount")
    return result


def decimal_text(value: Decimal) -> str:
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _count(value: object, field: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ReceiptError(f"{field} must be a nonnegative integer or null")


def _ref(value: object, field: str, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not REF.fullmatch(value):
        raise ReceiptError(f"{field} must be a private reference")


def _safe(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN:
                raise ReceiptError("raw identity or content is not permitted")
            _safe(child)
    elif isinstance(value, list):
        for child in value:
            _safe(child)
    elif isinstance(value, str) and (value.startswith(("/", "file:", "sk-")) or "\x00" in value):
        raise ReceiptError("unsafe private text in receipt")


def validate_payload(kind: str, payload: Mapping[str, Any]) -> None:
    # v1 receipts predate native_scope_ref. Accept that exact older usage shape
    # so historical as-of snapshots remain readable; new writers always include it.
    if not isinstance(kind, str) or kind not in FIELDS:
        raise ReceiptError(f"{kind} fields do not match the domain contract")
    optional = {"native_scope_ref", "reported_total_tokens", "assignment_ref", "metering"} if kind == "usage" else {"source_detail"} if kind == "coverage" else {"execution_ref", "assignment_ref", "scope", "context", "work_kind", "usage_fact_ref", "job"} if kind == "attribution" else {"outcome_context"} if kind == "delivery" else set()
    valid_fields = FIELDS[kind] - optional <= set(payload) <= FIELDS[kind]
    if not valid_fields:
        raise ReceiptError(f"{kind} fields do not match the domain contract")
    _safe(payload)
    for field in ("policy", "method", "rubric", "verification", "watermark", "reason", "category"):
        val = payload.get(field)
        if val is not None and (not isinstance(val, str) or not val or len(val) > 512):
            raise ReceiptError(f"{field} must be a bounded string or null")
    wi = payload.get("wi")
    if wi is not None and (not isinstance(wi, str) or not WI.fullmatch(wi)):
        raise ReceiptError("wi must be a canonical WI or null")
    for role in ROLES:
        val = payload.get(role)
        if val is not None and (not isinstance(val, str) or not val.strip() or len(val) > 128):
            raise ReceiptError(f"{role} must be a stable agent identity or null")
    for key, val in payload.items():
        if key.endswith("_ref"):
            _ref(val, key)
        if key.endswith("_at") and val is not None:
            parse_timestamp(val, key)
    if kind in STAGE_FIELDS:
        validate_stage_payload(kind, payload)
    if kind == "native_input":
        from .native_inputs import validate_input
        validate_input(payload)
    if kind == "output_assessment":
        from .native_output_assessment import validate_payload as validate_assessment
        validate_assessment(payload)
    if kind == "execution":
        if payload["state"] not in {"accepted", "running", "completed", "failed", "cancelled", "unknown"}:
            raise ReceiptError("invalid execution state")
        if not isinstance(payload["expected_usage"], bool):
            raise ReceiptError("execution usage expectation is required")
    if kind == "execution_expectation":
        if payload["scope"] != "scl_measurement_epoch" or payload["status"] not in {"missing", "sealed"}:
            raise ReceiptError("invalid execution expectation scope or disposition")
        if wi is None or payload["source_ref"] is None or payload["started_at"] is None:
            raise ReceiptError("SCL expectation requires WI, source and start boundary")
        if (payload["status"] == "sealed") != (payload["usage_ref"] is not None):
            raise ReceiptError("sealed expectation requires its exact source usage evidence")
        if payload["ended_at"] and parse_timestamp(payload["started_at"], "start") > parse_timestamp(payload["ended_at"], "end"):
            raise ReceiptError("execution expectation boundary is reversed")
    if kind in {"execution", "usage", "execution_expectation"} and payload["runtime"] not in {"pi", "codex", "cc", "cc_sdk", "unknown"}:
        raise ReceiptError("runtime must be explicit")
    if kind == "usage":
        _ref(payload.get("native_scope_ref"), "native_scope_ref")
        reported_total = payload.get("reported_total_tokens")
        _count(reported_total, "reported_total_tokens")
        if reported_total is not None and isinstance(payload.get("tokens"), Mapping):
            values = [payload["tokens"].get(k) for k in TOKEN_FIELDS]
            if all(isinstance(v, int) and not isinstance(v, bool) for v in values) and sum(values) != reported_total:
                raise ReceiptError("reported total does not conserve normalized classes")
        for key in ("model", "provider"):
            if payload[key] is not None and (not isinstance(payload[key], str) or not payload[key]):
                raise ReceiptError(f"{key} must be a source string or null")
        native = payload["native_tokens"]
        if not isinstance(native, Mapping) or not set(native) <= NATIVE_TOKEN_FIELDS:
            raise ReceiptError("native token fields are not allowlisted")
        for field, val in native.items():
            _count(val, field)
        tokens = payload["tokens"]
        if not isinstance(tokens, Mapping) or set(tokens) != set(TOKEN_FIELDS):
            raise ReceiptError("usage requires four mutually exclusive token fields")
        for field, val in tokens.items():
            _count(val, field)
        if payload["granularity"] not in {"request", "turn_aggregate", "session_counter"}:
            raise ReceiptError("invalid usage granularity")
        if not isinstance(payload["final"], bool):
            raise ReceiptError("usage final must be explicit")
        if not isinstance(payload["covers"], list) or any(not isinstance(v, str) for v in payload["covers"]) or len(set(payload["covers"])) != len(payload["covers"]):
            raise ReceiptError("coverage identities must be unique")
        for ref in payload["covers"]:
            _ref(ref, "covers", False)
        if not isinstance(payload["mapping"], str) or not payload["mapping"]:
            raise ReceiptError("usage mapping version is required")
    if kind == "usage" and payload.get("metering") is not None:
        from .native_metering import validate_metering
        validate_metering(payload)
    if kind in {"usage", "value"}:
        first, last = ("interval_start", "interval_end") if kind == "usage" else ("period_start", "period_end")
        if (payload[first] is None) != (payload[last] is None):
            raise ReceiptError("interval requires both boundaries or neither")
        if payload[first] is not None and parse_timestamp(payload[first], first) > parse_timestamp(payload[last], last):
            raise ReceiptError("interval is reversed")
    if kind == "attribution":
        _ref(payload["target_ref"], "target_ref", False)
        if payload["state"] not in {"assigned", "unassigned", "ambiguous", "excluded"}:
            raise ReceiptError("invalid attribution state")
        if "usage_fact_ref" in payload:
            _ref(payload['usage_fact_ref'], 'usage_fact_ref', False)
            if (payload['policy'] not in {'registered_runtime_actor.v1','verification_workspace.v1'}
                    or (payload['policy'] == 'verification_workspace.v1' and any(
                        payload.get(k) is not None for k in ('executor','responsible','delegator','execution_ref','assignment_ref')))):
                raise ReceiptError('unsupported usage-bound attribution')
        actor = payload.get("scope") == "actor"
        if payload.get("scope") not in {None, "actor"}:
            raise ReceiptError("unsupported attribution scope")
        if actor:
            if "work_kind" in payload:
                raise ReceiptError("actor context cannot classify work")
            if (wi is not None or any(payload.get(k) is not None for k in
                    ("responsible", "delegator", "execution_ref", "assignment_ref"))
                    or payload["policy"] != "registered_runtime_actor.v1"
                    or payload["state"] not in {"assigned", "unassigned"}
                    or (payload["state"] == "assigned") != (payload["executor"] is not None)):
                raise ReceiptError("actor evidence cannot grant task authority")
            context = payload.get("context")
            if not isinstance(context, Mapping) or not {"node", "entrypoint", "workspace_ref"} <= set(context) <= {"node", "entrypoint", "workspace_ref", "input_ref", "turn_final", "subcall", "tenant"}:
                raise ReceiptError("invalid actor context")
            if context["node"] is not None and not valid_node_identifier(context["node"]):
                raise ReceiptError("invalid actor node")
            if context["entrypoint"] not in {None, "codex_app", "feishu", "telegram", "cli", "automation"}:
                raise ReceiptError("invalid actor entrypoint")
            _ref(context["workspace_ref"], "workspace_ref")
            if "tenant" in context:
                from .tenant_context import validate_tenant_context
                validate_tenant_context(context["tenant"])
            if "subcall" in context:
                from .runtime_subcalls import validate_subcall
                validate_subcall(context["subcall"])
            if "input_ref" in context:
                _ref(context["input_ref"], "input_ref", False)
            if "turn_final" in context and (type(context["turn_final"]) is not bool or "input_ref" not in context):
                raise ReceiptError("native turn final requires an authenticated input")
        elif "context" in payload:
            raise ReceiptError("task attribution cannot overwrite actor context")
        if ("job" in payload) != (payload["policy"] == SCHEDULED_JOB_POLICY):
            raise ReceiptError("only a scheduled-job attribution names its job")
        if "job" in payload:
            validate_job(payload["job"])
            # The Relay job configuration either names a WI or the job itself.
            if (actor or payload["state"] != "assigned"
                    or (wi is None) != (payload.get("work_kind") == "cron")
                    or any(payload.get(k) is not None for k in
                           ("executor", "responsible", "delegator", "execution_ref", "assignment_ref"))):
                raise ReceiptError("invalid scheduled-job attribution")
        elif actor:
            pass
        elif payload.get("work_kind") is not None:
            # A reviewed non-WI classification is not a fabricated WI or an
            # execution grant. Actor evidence stays independent.
            # Exploration that created no WI is ordinary non-WI work too.
            if (payload["work_kind"] not in {"cron", "none"} or wi is not None
                    or payload["state"] != "assigned"
                    or (payload["policy"], payload["work_kind"]) not in {
                        ("non_wi_activity.v1", "cron"), ("non_wi_activity.v1", "none"), (EXPLORATION_POLICY, "none")}
                    or any(payload.get(k) is not None for k in
                           ("executor", "responsible", "delegator", "execution_ref", "assignment_ref"))):
                raise ReceiptError("invalid non-WI activity classification")
        elif payload["state"] == "assigned" and wi is None:
            raise ReceiptError("assigned attribution requires WI")
        if not payload["policy"]:
            raise ReceiptError("attribution policy is required")
    if kind in {"human", "human_summary"}:
        _count(payload["focused_seconds"], "focused_seconds")
    if kind == "human":
        if payload["origin"] not in {"human", "host", "replay"}:
            raise ReceiptError("human origin must be explicit")
        if payload["band"] not in {None, "micro", "bounded", "judgment", "framing"}:
            raise ReceiptError("invalid HAU band")
    if kind == "human_summary":
        _count(payload["count"], "count")
        if not isinstance(payload["bands"], Mapping) or set(payload["bands"]) != {"micro", "bounded", "judgment", "framing"}:
            raise ReceiptError("invalid HAU summary")
        for name, val in payload["bands"].items():
            _count(val, name)
        vals = list(payload["bands"].values())
        if payload["count"] is not None and all(v is not None for v in vals) and sum(vals) > payload["count"]:
            raise ReceiptError("HAU bands exceed human interaction count")
    if kind == "delivery":
        context = payload.get("outcome_context")
        if context is not None:
            if not isinstance(context, Mapping) or set(context) != {"schema","title","summary","verification_level"} or context["schema"] != "recorded_wi_outcome/v1":
                raise ReceiptError("invalid Outcome display context")
            if context["verification_level"] not in {"independent","human","self_with_controls"}:
                raise ReceiptError("invalid Outcome source metadata")
            for name, limit in (("title",240),("summary",1200)):
                value = context[name]
                if value is not None and (not isinstance(value,str) or not value or len(value)>limit):
                    raise ReceiptError("invalid bounded Outcome prose")
        for field in ("candidate_ref", "artifact_ref", "acceptance_ref"):
            _ref(payload[field], field, False)
        if payload["status"] not in {"submitted", "accepted", "partial", "rejected", "blocked", "unknown"}:
            raise ReceiptError("invalid delivery status")
    if kind == "review":
        for field in ("delivery_ref", "candidate_ref"):
            _ref(payload[field], field, False)
        if payload["decision"] not in {"approved", "changes_requested", "pending", "waived", "not_executed", "unknown"}:
            raise ReceiptError("invalid review decision")
        if payload["first_pass"] is not None and not isinstance(payload["first_pass"], bool):
            raise ReceiptError("first_pass must be known boolean or null")
        for field in ("rework_rounds", "post_approval_corrections"):
            _count(payload[field], field)
        if payload["score"] is not None:
            decimal(payload["score"], "score")
            if not payload["rubric"]:
                raise ReceiptError("score requires rubric")
        if not isinstance(payload["critical"], list) or any(not isinstance(v, str) for v in payload["critical"]):
            raise ReceiptError("critical evidence must be a list")
    if kind == "value":
        for field in ("delivery_ref", "artifact_ref", "effect_ref"):
            _ref(payload[field], field, False)
        if payload["mode"] not in {"increment", "cumulative", "snapshot"}:
            raise ReceiptError("invalid value mode")
        if payload["realization"] not in {"observed", "forecast", "qualitative"}:
            raise ReceiptError("invalid realization")
        if not isinstance(payload["complete"], bool):
            raise ReceiptError("value coverage is required")
        if payload["amount"] is not None:
            decimal(payload["amount"], "value.amount")
        if any(not isinstance(payload[k], str) or not payload[k] for k in ("consumer", "verifier", "unit")) or payload["consumer"] == payload["verifier"]:
            raise ReceiptError("value requires consumer and independent verifier")
        allocations = payload["allocations"]
        if not isinstance(allocations, list):
            raise ReceiptError("value allocations must be a list")
        by_role: dict[str, Decimal] = {}
        seen = set()
        for row in allocations:
            if not isinstance(row, Mapping) or set(row) != {"role", "agent", "share", "policy"} or row["role"] not in ROLES:
                raise ReceiptError("invalid allocation")
            if not all(isinstance(row[k], str) and 0 < len(row[k]) <= 128 for k in ("agent", "policy")):
                raise ReceiptError("allocation identity/policy invalid")
            key = (row["role"], row["agent"])
            if key in seen or not row["policy"] or not row["agent"]:
                raise ReceiptError("allocation identity/policy invalid")
            seen.add(key)
            by_role[row["role"]] = by_role.get(row["role"], Decimal(0)) + decimal(row["share"], "share", nonnegative=True)
        if any(total > 1 for total in by_role.values()):
            raise ReceiptError("allocation exceeds one")
    if kind == "coverage":
        detail = payload.get("source_detail")
        if detail is not None:
            expected = {"label", "kind", "layout", "files_seen", "files_read", "files_invalid", "excluded_directories", "latest_usage_at", "errors"}
            if not isinstance(detail, Mapping) or set(detail) != expected:
                raise ReceiptError("invalid source detail")
            for key in ("label", "kind", "layout"):
                if not isinstance(detail[key], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", detail[key]):
                    raise ReceiptError("invalid source label")
            for key in ("files_seen", "files_read", "files_invalid", "excluded_directories"):
                if type(detail[key]) is not int or detail[key] < 0:
                    raise ReceiptError("invalid source diagnostic count")
            if detail["latest_usage_at"] is not None:
                parse_timestamp(detail["latest_usage_at"], "source latest usage")
            if not isinstance(detail["errors"], Mapping) or any(
                    key not in {"unsafe_source", "source_unreadable", "incomplete_final_record", "native_protocol_mismatch", "invalid_native_record"}
                    or type(n) is not int or n < 1 for key, n in detail["errors"].items()):
                raise ReceiptError("invalid source diagnostic category")
        if payload["domain"] not in {"execution", "usage", "human", "quality", "value"}:
            raise ReceiptError("invalid coverage domain")
        if payload["status"] not in {"complete", "partial", "unknown", "idle", "excluded", "unreachable"}:
            raise ReceiptError("invalid coverage status")
        for field in ("expected", "observed"):
            _count(payload[field], field)
        if payload["status"] == "complete" and (not payload["expected"] or payload["expected"] != payload["observed"]):
            raise ReceiptError("complete coverage requires an independent reconciled denominator")


class _ReceiptReadPool:
    """Bounded, read-local storage sharing, never a validation or disk cache.

    Equal immutable values of one read share one object: payload bytes,
    timestamps, kind/producer, each evidence ref and each whole evidence
    tuple (a long input route repeats the same tuple in many revisions).
    A fact's own entity/fact refs are almost all unique, so they only reuse
    an already shared equal value and never occupy the bound.
    """
    def __init__(self, *, max_entries=1 << 22, max_bytes=1 << 30):
        self.values = {}
        self.size = 0
        self.max_entries, self.max_bytes = max_entries, max_bytes

    def share(self, value):
        if value is None:
            return None
        prior = self.values.get(value)
        if prior is not None:
            return prior
        size = getsizeof(value)
        if size > self.max_bytes:
            return value
        if len(self.values) >= self.max_entries or self.size + size > self.max_bytes:
            self.values.clear()
            self.size = 0
        self.values[value] = value
        self.size += size
        return value

    def known(self, value):
        """An equal value already shared, else this one; never added."""
        return self.values.get(value, value)

    def refs(self, refs) -> tuple:
        """One shared tuple of shared refs."""
        share = self.share
        return share(tuple(share(ref) for ref in refs))


def _read_receipt(cls, value: Mapping[str, Any], payload_json: bytes, pool) -> "Receipt":
    if pool is None:
        return cls(value["kind"], value["entity_ref"], value["fact_ref"], value["occurred_at"],
                   value["observed_at"], value["producer"], tuple(value["evidence"]),
                   value["supersedes"], payload_json)
    share, known = pool.share, pool.known
    return cls(share(value["kind"]), known(value["entity_ref"]), known(value["fact_ref"]),
               share(value["occurred_at"]), share(value["observed_at"]), share(value["producer"]),
               pool.refs(value["evidence"]), share(value["supersedes"]), share(payload_json))


@dataclass(frozen=True, slots=True)
class Receipt:
    """An immutable domain fact. Revisions replace assertions, not history."""
    kind: str
    entity_ref: str
    fact_ref: str
    occurred_at: str | None
    observed_at: str
    producer: str
    evidence: tuple[str, ...]
    supersedes: str | None
    _payload_json: bytes

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)

    @classmethod
    def make(cls, kind: str, identity: object, payload: Mapping[str, Any], *,
             occurred_at: str | None, observed_at: str, producer: str,
             evidence: tuple[str, ...] = (), supersedes: str | None = None,
             entity_ref: str | None = None) -> "Receipt":
        entity = entity_ref or reference(kind, identity)
        body = {"schema": SCHEMA, "kind": kind, "entity_ref": entity,
                "occurred_at": utc_text(parse_timestamp(occurred_at, "occurred_at")) if occurred_at else None,
                "observed_at": utc_text(parse_timestamp(observed_at, "observed_at")),
                "producer": producer, "evidence": sorted(set(evidence)),
                "supersedes": supersedes, "payload": dict(payload)}
        body["fact_ref"] = reference("fact", {k: v for k, v in body.items() if k not in {"observed_at", "evidence"}})
        return cls.from_dict(body)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, _pool=None) -> "Receipt":
        if not isinstance(value, Mapping) or set(value) != {"schema", "kind", "entity_ref", "fact_ref",
                "occurred_at", "observed_at", "producer", "evidence", "supersedes", "payload"} or value["schema"] != SCHEMA:
            raise ReceiptError("invalid collaboration envelope")
        _ref(value["entity_ref"], "entity_ref", False)
        _ref(value["fact_ref"], "fact_ref", False)
        _ref(value["supersedes"], "supersedes")
        parse_timestamp(value["observed_at"], "observed_at")
        if value["occurred_at"] is not None:
            parse_timestamp(value["occurred_at"], "occurred_at")
        if not isinstance(value["producer"], str) or not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,128}", value["producer"]) or value["producer"].startswith("/"):
            raise ReceiptError("producer is required")
        if not isinstance(value["payload"], Mapping):
            raise ReceiptError("domain payload must be an object")
        validate_payload(value["kind"], value["payload"])
        ev = value["evidence"]
        if value["kind"] == "native_input":
            from .request_inputs import KEYS as SNAPSHOT_CLASSES
            # Snapshot inputs are versioned per key; every other class is immutable.
            if not ev or (value["supersedes"] is not None
                          and value["payload"].get("input_class") not in SNAPSHOT_CLASSES):
                raise ReceiptError("a native input is immutable and names its source")
        if value["kind"] in STAGE_FIELDS and not ev:
            raise ReceiptError("stage declarations require source evidence")
        if (value["kind"] == "attribution" and value["payload"].get("work_kind") is not None
                and (not ev or value["payload"].get("effective_at") is None)):
            raise ReceiptError("non-WI classification needs evidence and a boundary")
        if not isinstance(ev, list) or any(not isinstance(v, str) for v in ev) or ev != sorted(set(ev)):
            raise ReceiptError("evidence refs must be sorted and unique")
        for ref in ev:
            _ref(ref, "evidence", False)
        if reference("fact", {k: v for k, v in value.items() if k not in {"fact_ref", "observed_at", "evidence"}}) != value["fact_ref"]:
            raise ReceiptError("fact digest mismatch")
        # Pool only AFTER all envelope/payload/digest validation. It holds no
        # validation result or authority; equal immutable values share storage.
        return _read_receipt(cls, value, canonical(value["payload"]), _pool)

    @classmethod
    def from_verified(cls, value: Mapping[str, Any], *, _pool=None) -> "Receipt":
        """Rebuild a fact read back from a digest-verified ledger transaction.

        Every fact passed `from_dict` before it was appended, and the
        transaction digest proves these are the same bytes; validating each
        again on every read only repeats that work.
        """
        return _read_receipt(cls, value, canonical(value["payload"]), _pool)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "kind": self.kind, "entity_ref": self.entity_ref,
                "fact_ref": self.fact_ref, "occurred_at": self.occurred_at,
                "observed_at": self.observed_at, "producer": self.producer,
                "evidence": list(self.evidence), "supersedes": self.supersedes, "payload": self.payload}

    def semantic_key(self) -> tuple[str, str, str | None, bytes, str | None]:
        """Process-local equality key for the same fields exposed by semantic()."""
        return (self.kind, self.entity_ref, self.occurred_at, self._payload_json, self.supersedes)

    def semantic(self) -> dict[str, Any]:
        return {"kind": self.kind, "entity_ref": self.entity_ref, "occurred_at": self.occurred_at,
                "payload": self.payload, "supersedes": self.supersedes}



def non_wi_attribution(target: Receipt, *, work_kind: str, observed_at: str,
                       evidence: tuple[str, ...], supersedes: str | None = None) -> Receipt:
    """Classify proven non-WI consumption without minting usage or task rights.

    The caller supplies reviewed/native evidence, not a null-WI heuristic.
    Cron with a real WI keeps that WI and needs no non-WI classification.
    """
    if target.kind != "usage" or target.payload.get("wi") is not None or not evidence:
        raise ReceiptError("explicit evidence for a no-WI usage record is required")
    start = target.payload.get("interval_start") or target.occurred_at
    if start is None:
        raise ReceiptError("usage time must be established before classification")
    return Receipt.make("attribution", ["non_wi_activity", target.entity_ref], {
        "target_ref": target.entity_ref, "wi": None, "executor": None,
        "responsible": None, "delegator": None, "state": "assigned",
        "policy": "non_wi_activity.v1", "effective_at": start, "work_kind": work_kind,
    }, occurred_at=start, observed_at=observed_at, producer="non_wi_activity.v1",
       evidence=tuple(sorted(set(evidence) | {target.fact_ref})), supersedes=supersedes)


SOURCE_RETIREMENT_PRODUCER = "source_retirement.v1"
SOURCE_RETIREMENT_REASON = "source_retired"


def is_source_retirement(row: Receipt) -> bool:
    return (row.kind == "coverage" and row.producer == SOURCE_RETIREMENT_PRODUCER
            and row.payload["status"] == "excluded" and row.payload["reason"] == SOURCE_RETIREMENT_REASON)


# Coverage versions whose evidence may name replaced concurrent heads.
HEAD_NAMING_PRODUCERS = frozenset({"registered_collector.v1", SOURCE_RETIREMENT_PRODUCER})


def named_coverage_heads(facts: list[Receipt]) -> set[str]:
    """Same-entity coverage heads a later coverage version names as replaced.

    The envelope has one supersedes edge. A source retirement or a declared
    coverage recovery also lists the other concurrent heads it replaces in its
    immutable evidence; only exact coverage facts of the same entity are
    replaced. A head appended later is not named and still conflicts.
    """
    named: dict[str, str] = {}
    for row in facts:
        if row.kind == "coverage" and row.evidence and row.producer in HEAD_NAMING_PRODUCERS:
            named.update((ref, row.entity_ref) for ref in row.evidence)
    if not named:
        return set()
    return {row.fact_ref for row in facts
            if row.kind == "coverage" and named.get(row.fact_ref) == row.entity_ref}


def current_receipts(facts: list[Receipt]) -> tuple[list[Receipt], dict[str, list[Receipt]]]:
    """Resolve explicit supersession only. Concurrent heads remain conflicts."""
    replaced = {row.supersedes for row in facts if row.supersedes} | named_coverage_heads(facts)
    groups: dict[str, list[Receipt]] = {}
    for row in facts:
        if row.fact_ref not in replaced:
            groups.setdefault(row.entity_ref, []).append(row)
    active, conflicts = [], {}
    for key, rows in sorted(groups.items()):
        unique = {row.semantic_key(): row for row in rows}
        if len(unique) == 1:
            active.append(next(iter(unique.values())))
        else:
            conflicts[key] = list(unique.values())
    return active, conflicts
