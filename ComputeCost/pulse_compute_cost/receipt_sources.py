"""Read registered real sources into UsageLedger's versioned receipt API.

Registry paths are private host configuration, not published receipts. There is
no model-text WI discovery, workflow execution, billing, or subscription split.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping, Iterable
import json

from .adapters import AdapterError, CodexNativeObservation, collect_usage_events
from .events import utc_text, parse_timestamp
from .coverage_recovery import recovered_coverage
from .ledger import UsageLedger
from .source_layout import codex_calendar_paths, pi_files, source_detail, failure, manifest_paths, native_binding_source
from .native_attribution import execution_bindings, bind_pi_boundaries, bind_cc_boundaries, _host_authorities
from .native_inventory import pi_prefix_inventory
from .receipts import (Receipt, ReceiptError, SCHEDULED_JOB_POLICY, TOKEN_FIELDS, NATIVE_TOKEN_FIELDS, reference, digest, decimal,
                       decimal_text, current_receipts, digest_hex)
from .delivery_value import HARNESS_REVIEW_SCHEMAS, validate_harness_review_snapshot, validate_business_value_receipt
from .harness_measurement import validate_harness_measurement_snapshot
from .scl_measurement import load_scl_measurement_snapshot, validate_scl_measurement_snapshot
from .token_semantics import InputTokenClasses, TokenSemanticsError

REGISTRY_SCHEMA = "computecost_source_registry/v1"
HOST_SCHEMA = "pulse_collaboration_host_event/v1"
# Diagnostics confined to one file, snapshot or tool call. They are counted in
# the collection summary but do not mark the readable rest of a source partial.
LOCAL_DIAGNOSTICS = frozenset({
    "native_mcp_sidechain_unbound",  # A subagent need not declare; its parent request does.
    "native_mcp_call_refused",  # A refused CC call declared nothing; other declarations stand.
    "pi_snapshot_baseline_missing",
    "runtime_subcall_file_invalid",
})


def _source_digest(path: Path, data: bytes | None = None) -> str:
    """Keep the existing source identity while bounding hexadecimal copies."""
    if data is None:
        with path.open("rb") as stream:
            return digest_hex(iter(lambda: stream.read(65536), b""))
    view = memoryview(data)
    return digest_hex(view[offset:offset + 65536] for offset in range(0, len(view), 65536))


def normalize_usage(runtime: str, usage: object, *, mapping: str | None = None) -> dict[str, int | None]:
    """Nulls stay null. The mapping, not a model name, defines inclusive input."""
    raw = usage if isinstance(usage, Mapping) else {}
    def count(*names: str) -> int | None:
        found = [raw[n] for n in names if n in raw and raw[n] is not None]
        if not found:
            return None
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in found) or len(set(found)) != 1:
            raise ReceiptError("conflicting or invalid native token field")
        return found[0]
    if runtime == "pi" and mapping != "relay_pi.v1":
        i, r, w, o = count("input"), count("cacheRead"), count("cacheWrite"), count("output")
    elif runtime == "codex":
        i = count("input_tokens", "inputTokens")
        r = count("cached_input_tokens", "cachedInputTokens")
        w = count("cache_write_input_tokens", "cacheWriteInputTokens")
        # Explicit CLI protocol has no cache-write class; App Server may have it.
        if mapping == "codex_cli.v1" and w is None:
            w = 0
        o = count("output_tokens", "outputTokens")
        try:
            i = InputTokenClasses.from_inclusive(i, r, w).uncached_input
        except TokenSemanticsError as exc:
            raise ReceiptError(str(exc)) from exc
    else:
        i = count("input_tokens", "inputTokens")
        r = count("cache_read_input_tokens", "cacheReadInputTokens")
        w = count("cache_creation_input_tokens", "cacheCreationInputTokens")
        o = count("output_tokens", "outputTokens")
    if runtime == "pi" and mapping != "relay_pi.v1" and raw.get("totalTokens") is not None:
        total = count("totalTokens")
        if all(v is not None for v in (i, r, w, o)) and total != i+r+w+o:
            raise ReceiptError("Pi native total does not conserve")
    return dict(zip(TOKEN_FIELDS, (i, r, w, o)))


def _common() -> dict[str, Any]:
    return {"wi": None, "executor": None, "responsible": None, "delegator": None}


def usage_receipt(runtime: str, identity: object, usage: object, *, at: str | None,
                  observed: str, model: str | None, provider: str | None = None,
                  execution_ref: str | None = None, common: Mapping[str, Any] | None = None,
                  granularity: str = "request", covers: list[str] | None = None,
                  coverage_ref: str | None = None, interval: tuple[str, str] | None = None,
                  final: bool = True, mapping: str | None = None,
                  entity_ref: str | None = None, native_scope_ref: str | None = None) -> Receipt:
    mapping = mapping or f"{runtime}_native.v1"
    return Receipt.make("usage", identity, {
        **(common or _common()), "execution_ref": execution_ref, "native_scope_ref": native_scope_ref,
        "runtime": runtime, "model": model, "provider": provider, "tokens": normalize_usage(runtime, usage, mapping=mapping),
        "native_tokens": {k:v for k,v in (usage.items() if isinstance(usage, Mapping) else []) if k in NATIVE_TOKEN_FIELDS},
        "mapping": mapping, "granularity": granularity, "coverage_ref": coverage_ref,
        "covers": covers or [], "interval_start": interval[0] if interval else None,
        "interval_end": interval[1] if interval else None, "final": final,
    }, occurred_at=at, observed_at=observed, producer=mapping, entity_ref=entity_ref)


def _pi_model_refines(prior: Receipt, current: Receipt) -> bool:
    """Same invocation/quantity, with evidence filling only an unknown model."""
    old, new = prior.payload, current.payload
    return (prior.kind == current.kind == "usage"
        and prior.entity_ref == current.entity_ref
        and prior.producer == current.producer == "pi_native.v1"
        and old.get("model") is None and new.get("model") is not None
        and prior.occurred_at == current.occurred_at
        and bool(set(prior.evidence) & set(current.evidence))
        and all(old.get(k) == new.get(k) for k in set(old) | set(new)
                if k not in {"model", "provider"})
        and old.get("provider") in {None, new.get("provider")})


def _revision(rows: list[Receipt], row: Receipt, *, histories: dict | None = None) -> None:
    """Append a proven refinement; never turn a final contradiction into a revision."""
    history = (histories.setdefault((row.kind, row.entity_ref), []) if histories is not None
               else [r for r in rows if r.kind == row.kind and r.entity_ref == row.entity_ref])
    if any(r.payload == row.payload and r.occurred_at == row.occurred_at for r in history):
        return
    supersedes = None
    if history:
        last = history[-1]
        if row.kind == "usage":
            old, new = last.payload, row.payload
            monotonic = all(old["tokens"][k] is None or (new["tokens"][k] is not None and new["tokens"][k] >= old["tokens"][k]) for k in TOKEN_FIELDS)
            # Identity metadata is not a counter refinement. In particular a
            # stale non-final observation may not hide a model/provider/native
            # scope contradiction behind a dominated token projection.
            same = all(old.get(k) == new.get(k) for k in ("execution_ref", "wi", "runtime", "granularity", "coverage_ref", "model", "provider", "native_scope_ref"))
            # Unknown cache partitions can hide real growth in inclusive native
            # counters. Both the native and normalized evidence must establish
            # monotonicity/dominance; null projections prove neither by themselves.
            native_monotonic = all(value is None or
                (new["native_tokens"].get(key) is not None and new["native_tokens"][key] >= value)
                for key, value in old["native_tokens"].items())
            native_dominated = all(value is None or
                (old["native_tokens"].get(key) is not None and value <= old["native_tokens"][key])
                for key, value in new["native_tokens"].items())
            monotonic = monotonic and native_monotonic
            # A stale copied CC snapshot must not manufacture a competing head.
            dominated = same and native_dominated and all(new["tokens"][k] is None or old["tokens"][k] is not None and new["tokens"][k] <= old["tokens"][k] for k in TOKEN_FIELDS)
            older = (row.occurred_at is not None and last.occurred_at is not None
                     and parse_timestamp(row.occurred_at, "occurred_at") < parse_timestamp(last.occurred_at, "occurred_at"))
            if dominated and not row.payload["final"] and (last.payload["final"] or older):
                # A non-final copied snapshot is never allowed to contest a
                # settled observation merely because filesystem ordering changed.
                # A final observation at the same counter is a real refinement.
                return
            if monotonic and same and (old["model"] is None or old["model"] == new["model"]) and not last.payload["final"]:
                supersedes = last.fact_ref
            # A corrected native adapter may recover an absent time boundary
            # for an already-final quantity. Only fill this exact metadata gap;
            # never revise known counters/models or a previously known interval.
            interval_refinement = (old.get("interval_start") is None and new.get("interval_start") is not None
                and all(old.get(k) == new.get(k) for k in set(old) | set(new)
                        if k not in {"interval_start", "interval_end"})
                and last.occurred_at == row.occurred_at and last.producer == row.producer)
            if interval_refinement:
                supersedes = last.fact_ref
            from .native_metering import metering_refines, shared_metering_source
            if metering_refines(row, last) and shared_metering_source(row, last):
                # Feature opt-out limits new extraction, not the durability of
                # an already reviewed exact timeline for this same quantity.
                return
            if metering_refines(last, row):
                supersedes = last.fact_ref
            # Same immutable Pi invocation and quantities, newly recovered
            # native model state. A known/different model is never replaced.
            if _pi_model_refines(row, last):
                # A later bounded observation lacking inherited model context
                # cannot undo the exact model evidence already recovered.
                return
            if _pi_model_refines(last, row):
                supersedes = last.fact_ref
        elif row.kind == "execution":
            terminal = last.payload["state"] in {"completed", "failed", "cancelled"}
            # Settlement may withdraw the usage expectation of a turn that made
            # no provider request; it never adds one or changes anything else.
            withdrawn = (last.payload["expected_usage"] is True and row.payload["expected_usage"] is False
                         and row.payload["state"] in {"completed", "failed", "cancelled"})
            stable = all(last.payload[k] == row.payload[k] for k in row.payload
                         if k != "state" and not (k == "expected_usage" and withdrawn))
            if stable and not terminal:
                supersedes = last.fact_ref
        elif row.kind == "coverage":
            supersedes = last.fact_ref
    if supersedes:
        row = Receipt.make(row.kind, None, row.payload, occurred_at=row.occurred_at,
                           observed_at=row.observed_at, producer=row.producer,
                           evidence=tuple(sorted(set(last.evidence) | set(row.evidence))),
                           entity_ref=row.entity_ref, supersedes=supersedes)
    rows.append(row)
    if histories is not None:
        history.append(row)


def reconcile_native_usage(original: list[Receipt], observations: list[Receipt]) -> list[Receipt]:
    """Reuse unique current correction chains; a new observation cannot pick a fork."""
    active, _ = current_receipts(original)
    prior_usage = {r.entity_ref: r for r in active if r.kind == "usage"}
    pending = []
    for usage in observations:
        prior = prior_usage.get(usage.entity_ref)
        if prior is not None:
            if (prior.payload == usage.payload and prior.occurred_at == usage.occurred_at
                    and prior.producer == usage.producer):
                usage = replace(prior, evidence=tuple(sorted(set(prior.evidence) | set(usage.evidence))))
            elif prior.payload != usage.payload:
                revised = [prior]
                _revision(revised, usage)
                if len(revised) == 1:
                    continue
                usage = revised[-1]
        pending.append(usage)
    return pending


def _json_lines(path: Path, data: bytes | None = None) -> Iterable[Mapping[str, Any]]:
    # Capture one immutable prefix per read. Appends after this read belong to
    # the next collection, never to this collection's source digest/watermark.
    content = path.read_bytes() if data is None else data
    for raw in content.splitlines(keepends=True):
        if not raw.strip():
            continue
        if not raw.endswith(b"\n"):
            raise ReceiptError("source has an incomplete final record")
        row = json.loads(raw)
        if not isinstance(row, Mapping):
            raise ReceiptError("source row is not an object")
        yield row



_PRINCIPAL_HISTORY_FIELDS = {"alias", "principal", "effective_from", "config_sha256"}


def _relay_principal(history: object, executor: object, effective_at: str | None, *, include_identity: bool = False) -> tuple[object, tuple[str, ...]]:
    """Resolve only an explicit time-bounded Relay alias mapping."""
    if executor is None or not history:
        return executor, ()
    if not isinstance(executor, str) or not isinstance(history, list) or effective_at is None:
        raise ReceiptError("invalid Relay principal mapping input")
    when = parse_timestamp(effective_at, "Relay principal boundary")
    candidates = []
    boundaries = set()
    for entry in history:
        if not isinstance(entry, Mapping) or set(entry) != _PRINCIPAL_HISTORY_FIELDS:
            raise ReceiptError("invalid Relay principal history")
        alias = entry.get("alias")
        principal = entry.get("principal")
        config_sha = entry.get("config_sha256")
        boundary = entry.get("effective_from")
        if (not isinstance(alias, str) or not isinstance(principal, str)
                or not isinstance(config_sha, str) or len(config_sha) != 64
                or any(char not in "0123456789abcdef" for char in config_sha)
                or not isinstance(boundary, str)):
            raise ReceiptError("invalid Relay principal history")
        instant = parse_timestamp(boundary, "Relay principal history")
        key = (alias, instant)
        if key in boundaries:
            raise ReceiptError("duplicate Relay principal history boundary")
        boundaries.add(key)
        if alias == executor and instant <= when:
            candidates.append((instant, principal, config_sha, boundary))
    if not candidates:
        return executor, ()
    latest = max(item[0] for item in candidates)
    selected = [item for item in candidates if item[0] == latest]
    if len(selected) != 1:
        raise ReceiptError("conflicting Relay principal history")
    _, principal, config_sha, boundary = selected[0]
    if principal == executor and not include_identity:
        return executor, ()
    proof = reference("relay_principal_mapping", [executor, principal, boundary, config_sha])
    return principal, (proof,)


def _principal_refinement(prior: Receipt, row: Receipt, history: object) -> bool:
    if (prior.kind != row.kind or prior.entity_ref != row.entity_ref
            or prior.producer != row.producer or prior.occurred_at != row.occurred_at):
        return False
    before, after = prior.payload, row.payload
    old_executor, new_executor = before.get("executor"), after.get("executor")
    if old_executor is None or old_executor == new_executor:
        return False
    boundary = after.get("interval_start") or row.occurred_at
    resolved, proof = _relay_principal(history, old_executor, boundary)
    if not proof or resolved != new_executor:
        return False
    keys = set(before) | set(after)
    return all(before.get(key) == after.get(key) for key in keys if key != "executor")


def _principal_head_corrections(active: list[Receipt], source_entities: set[str],
                                history: object, observed: str) -> list[Receipt]:
    """Revise only current Host heads; never replay intermediate execution states."""
    result = []
    for prior in active:
        if (prior.entity_ref not in source_entities
                or prior.kind not in {"usage", "execution", "attribution", "human"}):
            continue
        payload = prior.payload
        effective = payload.get("interval_start") or payload.get("effective_at") or prior.occurred_at
        principal, proof = _relay_principal(history, payload.get("executor"), effective)
        if not proof or principal == payload.get("executor"):
            continue
        result.append(Receipt.make(prior.kind, None, {**payload, "executor": principal},
            occurred_at=prior.occurred_at, observed_at=observed, producer=prior.producer,
            evidence=tuple(sorted(set(prior.evidence) | set(proof))),
            entity_ref=prior.entity_ref, supersedes=prior.fact_ref))
    return result


def _actor_principal_corrections(active: list[Receipt], target: Receipt,
                                 history: object, observed: str) -> list[Receipt]:
    """Revise old actor aliases on this exact target; never infer a new task."""
    executor = target.payload.get("executor")
    if executor is None or not history:
        return []
    effective = target.payload.get("interval_start") or target.occurred_at
    result = []
    for old in active:
        if (old.kind != "attribution" or old.producer != "registered_runtime_actor.v1"
                or old.payload.get("scope") != "actor"
                or old.payload.get("target_ref") != target.entity_ref
                or old.payload.get("executor") in {None, executor}):
            continue
        resolved, proof = _relay_principal(history, old.payload.get("executor"), effective)
        if not proof or resolved != executor:
            continue
        payload = {**old.payload, "executor": executor}
        result.append(Receipt.make("attribution", None, payload,
            occurred_at=old.occurred_at, observed_at=observed,
            producer=old.producer, evidence=tuple(sorted(set(old.evidence) | set(proof))),
            entity_ref=old.entity_ref, supersedes=old.fact_ref))
    return result


def read_host(path: Path, observed: str, *, into: list[Receipt] | None = None, data: bytes | None = None) -> list[Receipt]:
    rows = into if into is not None else []
    seen = {}
    writer = None
    previous_sequence = 0
    for raw in _json_lines(path, data):
        if raw.get("schema") != HOST_SCHEMA or not isinstance(raw.get("event_ref"), str):
            raise ReceiptError("unsupported Host receipt source")
        sequence_gap = False
        if raw.get("writer_ref") is not None:
            sequence = raw.get("sequence")
            if writer is not None and writer != raw["writer_ref"]:
                raise ReceiptError("mixed writers in a Host stream")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= previous_sequence:
                raise ReceiptError("Host stream sequence gap")
            sequence_gap = sequence != previous_sequence + 1
            writer, previous_sequence = raw["writer_ref"], sequence
        # A resend of the same event differs at most in its emission time.
        # Keep the first (earliest in writer order); any other difference is
        # a real identity conflict.
        semantic = digest({k:v for k,v in raw.items() if k not in {"writer_ref", "sequence", "lost_events", "at"}})
        if raw["event_ref"] in seen:
            if seen[raw["event_ref"]] != semantic:
                raise ReceiptError("conflicting Host event identity")
            continue
        seen[raw["event_ref"]] = semantic
        kind, at = raw["kind"], raw["at"]
        common = {k: raw.get(k) for k in _common()}
        attempt = raw.get("attempt_ref")
        if raw.get("lost_events", 0) or kind == "gap" or sequence_gap:
            rows.append(Receipt.make("coverage", [writer or "host", raw["event_ref"]],
                {"domain":"usage", "wi":raw.get("wi"), "source_ref":writer or reference("source", "host"),
                 "status":"partial", "expected":None, "observed":None, "watermark":str(previous_sequence),
                 "reason": "host_capture_gap"}, occurred_at=at, observed_at=at, producer="relay_host.v1"))
            if kind == "gap":
                continue
        if kind == "execution":
            row = Receipt.make("execution", None, {**common, "runtime": raw["runtime"],
                "assignment_ref": raw.get("assignment_ref"), "parent_ref": raw.get("parent_ref"),
                "state": raw["state"], "expected_usage": raw.get("expected_usage", True)},
                occurred_at=raw.get("started_at",at), observed_at=at, producer="relay_host.v1", entity_ref=attempt)
            _revision(rows, row)
        elif kind == "usage":
            row = usage_receipt(raw["runtime"], raw["event_ref"], raw.get("usage"), at=at, observed=at,
                model=raw.get("model"), provider=raw.get("provider"), execution_ref=attempt,
                common=common, granularity=raw["granularity"], covers=raw.get("covers", []),
                coverage_ref=raw.get("coverage_ref"), interval=(raw["started_at"], at) if raw.get("started_at") else None,
                final=raw.get("final", False), mapping=raw["mapping"], entity_ref=raw.get("usage_ref"),
                # Host owns this correlation; the collector never infers it from an attempt.
                native_scope_ref=raw.get("native_scope_ref"))
            _revision(rows, row)
        elif kind == "attribution":
            rows.append(Receipt.make("attribution", None, {
                "target_ref":raw["target_ref"], **common, "state":raw["state"],
                "policy":raw["policy"], "effective_at":raw["effective_at"]},
                occurred_at=raw["effective_at"], observed_at=at, producer="relay_host.v1",
                entity_ref=raw["attribution_ref"]))
        elif kind == "scheduled_session":
            # Relay observed the native session its scheduler run started.
            # The session scope is the target: its own usage, whichever
            # collector reads it, joins by the same native_scope_ref.
            wi = raw.get("wi")
            payload = {"target_ref": raw["native_scope_ref"], "wi": wi, "executor": None,
                       "responsible": None, "delegator": None, "state": "assigned",
                       "policy": SCHEDULED_JOB_POLICY, "effective_at": raw["started_at"],
                       "job": {"id": raw["job_id"], "name": raw["job_name"]},
                       **({} if wi is not None else {"work_kind": "cron"})}
            rows.append(Receipt.make("attribution", None, payload, occurred_at=raw["started_at"],
                observed_at=at, producer="relay_host.v1",
                entity_ref=reference("attribution", [SCHEDULED_JOB_POLICY, raw["native_scope_ref"]]),
                evidence=(raw["event_ref"], raw["run_ref"])))
        elif kind == "human":
            rows.append(Receipt.make("human", None, {**common, "execution_ref": attempt,
                "origin": "human", "category": None, "band": None, "focused_seconds": None,
                "method": "authenticated_ingress.v1"}, occurred_at=at, observed_at=at,
                producer="relay_host.v1", entity_ref=raw["human_ref"]))
        else:
            raise ReceiptError("unknown Host event kind")
    return rows


def read_cc(path: Path, observed: str, *, into: list[Receipt] | None = None, data: bytes | None = None) -> list[Receipt]:
    result = into if into is not None else []
    for row in _json_lines(path, data):
        if row.get("type") != "assistant" or not isinstance(row.get("message"), Mapping):
            continue
        msg = row["message"]
        identity = msg.get("id") or row.get("uuid")
        if not identity:
            raise ReceiptError("CC assistant lacks stable message identity")
        session = row.get("sessionId")
        item = usage_receipt("cc", [session, identity], msg.get("usage"),
            at=row.get("timestamp"), observed=observed, model=msg.get("model"),
            final=bool(msg.get("stop_reason")), mapping="cc_message.v1",
            native_scope_ref=reference("native_session", ["cc", session]) if isinstance(session, str) and session else None)
        from .cc_session_counters import native_counter_evidence
        item = replace(item, evidence=native_counter_evidence(item.payload.get('native_scope_ref'), row.get('version')))
        _revision(result, item)
    return result



def _input_principal(source: Mapping[str, Any], token: str, data: bytes) -> tuple[str | None, tuple[str, ...]]:
    """The already-authenticated input names a route, independently of a WI.

    Reuse the enrolled Host's historical principal mapping at input time; the
    native cwd may belong to an older machine and is not an identity fallback.
    Recheck the input key to reject a changed record between the two reads.
    """
    import hashlib
    record = json.loads(data)
    origin = record.get('source', {})
    pool, channel = record.get('poolKey'), origin.get('channel')
    if not isinstance(pool, str):
        raise ReceiptError('authenticated input has no routing pool')
    key = [channel, origin.get('eventId'), origin.get('humanRef'), pool]
    if hashlib.sha256(json.dumps(key, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest() != token:
        raise ReceiptError('authenticated input identity changed')
    parts = pool.split(':')
    if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != channel):
        return None, ()
    ms = record.get('observedAtMs')
    if type(ms) is not int or ms <= 0:
        raise ReceiptError('authenticated input timestamp is invalid')
    principal, proof = _relay_principal(source.get('principal_alias_history'), parts[1],
        datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(), include_identity=True)
    return (principal, proof) if proof else (None, ())


def _cc_session_starts(path: Path, data: bytes, cache: dict | None = None) -> dict[str, Mapping[str, Any]]:
    """First recorded cwd of each session in its main transcript.

    A Task subagent transcript (`<session>/subagents/agent-*.jsonl`) belongs
    to the main transcript's session and therefore to its start folder. Only
    when the main transcript is absent does the subagent's own first record
    stand in for it.
    """
    main, main_data = path, data
    if path.parent.name == 'subagents':
        candidate = path.parent.parent.parent / f'{path.parent.parent.name}.jsonl'
        if candidate.is_file() and not candidate.is_symlink():
            main, main_data = candidate, None
    key = ('cc_session_start', str(main))
    if cache is not None and key in cache and main_data is None:
        return cache[key]
    content = main.read_bytes() if main_data is None else main_data
    starts: dict[str, Mapping[str, Any]] = {}
    for raw in content.splitlines(keepends=True):
        if b'"cwd"' not in raw or not raw.endswith(b'\n'):
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, Mapping):
            continue
        session, cwd = row.get('sessionId'), row.get('cwd')
        if isinstance(session, str) and session and isinstance(cwd, str) and session not in starts:
            starts[session] = row
    if cache is not None and main_data is None:
        cache[key] = starts
    return starts


def relay_cc_executors(facts: Iterable[Receipt]) -> dict[str, tuple[str, str]]:
    """Agent the Relay Host recorded for each CC native session it dispatched.

    Only Host execution facts linked by Host usage to the same native session
    count; a session whose Host executions disagree has no Relay answer.
    """
    facts = list(facts)
    started = {r.entity_ref for r in facts if r.kind == 'execution' and r.producer == 'relay_host.v1'}
    host = [r for r in facts if r.entity_ref in started or (r.kind == 'usage' and r.payload.get('execution_ref') in started)]
    current, _ = current_receipts(list({r.fact_ref: r for r in host}.values()))
    executions = {r.entity_ref: r for r in current if r.kind == 'execution'}
    grouped: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for usage in current:
        if usage.kind != 'usage':
            continue
        scope = usage.payload.get('native_scope_ref')
        execution = executions.get(usage.payload.get('execution_ref'))
        if not scope or execution is None or execution.payload.get('runtime') not in {'cc', 'cc_sdk'}:
            continue
        executor = execution.payload.get('executor')
        if isinstance(executor, str) and executor:
            grouped[scope].add((executor, execution.entity_ref))
    result = {}
    for scope, rows in grouped.items():
        if len({executor for executor, _ in rows}) == 1:
            result[scope] = min(rows)
    return result


def _cc_prior_actors(active: Iterable[Receipt]) -> dict[str, list[Receipt]]:
    """Current CC workspace identities, keyed by the native request they name."""
    result: dict[str, list[Receipt]] = defaultdict(list)
    for row in active:
        p = row.payload
        if (row.kind == 'attribution' and row.producer == 'registered_runtime_actor.v1'
                and p.get('scope') == 'actor' and p.get('wi') is None and isinstance(p.get('executor'), str)
                and set(p.get('context') or ()) == {'node', 'entrypoint', 'workspace_ref'}
                and p['context']['entrypoint'] is None):
            result[p['target_ref']].append(row)
    return result


def cc_workspace_actor_receipts(path: Path, data: bytes, source: Mapping[str, Any],
                                facts: list[Receipt], observed: str, *, workspace_cache: dict | None = None,
                                relay_executors: Mapping[str, tuple[str, str]] | None = None,
                                prior_actors: Mapping[str, list[Receipt]] | None = None,
                                targets_cache: dict | None = None) -> list[Receipt]:
    """Join a CC session to its Agent, never WI owners.

    A Relay-dispatched session belongs to the Agent the Host recorded. Any
    other session belongs to the registered workspace of the folder it started
    in; a later chdir into a nested worktree or another Agent's folder does
    not move it. An older per-message identity for the same request is revised
    in place rather than left as a second, contrary head.

    Run outside the native parse cache: authority and workspace resolution may
    change while the original transcript bytes remain unchanged.

    `targets_cache`, shared across the files of one source, keeps the targets of
    an append-only `facts` list and adds only the facts appended since.
    """
    targets = _cc_usage_targets(facts, targets_cache)
    result = []
    workspace_cache = {} if workspace_cache is None else workspace_cache
    starts = _cc_session_starts(path, data, workspace_cache)
    for row in _json_lines(path, data):
        if row.get('type') != 'assistant' or not isinstance(row.get('message'), Mapping):
            continue
        message = row['message']
        identity = message.get('id') or row.get('uuid')
        session = row.get('sessionId')
        target = targets.get(reference('usage', [session, identity]))
        if target is None or not isinstance(session, str) or not session:
            continue
        evidence = (reference('native_cc_message_context', digest(row)),)
        relay = (relay_executors or {}).get(reference('native_session', ['cc', session]))
        if relay is not None:
            actor = {'executor': relay[0], 'node': source.get('node'), 'workspace_ref': None}
            evidence += (relay[1],)
        else:
            start = starts.get(session)
            cwd = start.get('cwd') if start is not None else row.get('cwd')
            actor = registered_workspace_actor(source, cwd, 'cc', workspace_cache=workspace_cache)
            if start is not None and cwd != row.get('cwd'):
                evidence += (reference('native_cc_session_start', digest(start)),)
        if actor['executor'] is None:
            continue
        receipt = actor_receipt(target, **actor, observed=observed, evidence=evidence,
                                path=['cc_workspace', source.get('id')])
        olds = [old for old in (prior_actors or {}).get(target.entity_ref, ())
                if old.entity_ref != receipt.entity_ref]
        if olds:
            # An agreeing head is already proof; revise only contrary ones,
            # which an agreeing sibling must not leave current.
            for old in olds:
                if old.payload.get('executor') == actor['executor']:
                    continue
                result.append(Receipt.make('attribution', None, receipt.payload,
                    occurred_at=old.occurred_at, observed_at=observed, producer=old.producer,
                    evidence=tuple(sorted(set(old.evidence) | set(evidence))),
                    entity_ref=old.entity_ref, supersedes=old.fact_ref))
            continue
        result.append(receipt)
    return result


def _cc_usage_targets(facts: list[Receipt], cache: dict | None) -> dict[str, Receipt]:
    """The earliest CC message usage per entity, the first of equals kept."""
    if cache is None or cache.get('facts') is not facts:
        fresh = {'facts': facts, 'synced': 0, 'targets': {}}
        if cache is None:
            cache = fresh
        else:
            cache.clear()
            cache.update(fresh)
    targets = cache['targets']
    for fact in facts[cache['synced']:]:
        if fact.kind != 'usage' or fact.payload.get('mapping') != 'cc_message.v1' or fact.occurred_at is None:
            continue
        previous = targets.get(fact.entity_ref)
        if previous is None or parse_timestamp(fact.occurred_at, 'CC boundary') < parse_timestamp(previous.occurred_at, 'CC boundary'):
            targets[fact.entity_ref] = fact
    cache['synced'] = len(facts)
    return targets


def _input_actor_alias_corrections(active: list[Receipt], targets: set[str],
                                   history: object, observed: str) -> list[Receipt]:
    """Revise identities recorded under a since-mapped Relay alias on these targets.

    A native input route names its executor through the Host alias history at
    input time. An entry appended later for that time (the principal of an
    alias the history had not yet recorded) supersedes the old alias claim;
    otherwise both stay active and dispute one executor.
    """
    result = []
    for old in active:
        payload = old.payload
        if (old.kind != "attribution" or old.producer != "registered_runtime_actor.v1"
                or payload.get("scope") != "actor" or payload.get("target_ref") not in targets
                or payload.get("executor") is None):
            continue
        effective = payload.get("effective_at") or old.occurred_at
        resolved, proof = _relay_principal(history, payload["executor"], effective)
        if not proof or resolved == payload["executor"]:
            continue
        result.append(Receipt.make("attribution", None, {**payload, "executor": resolved},
            occurred_at=old.occurred_at, observed_at=observed, producer=old.producer,
            evidence=tuple(sorted(set(old.evidence) | set(proof))),
            entity_ref=old.entity_ref, supersedes=old.fact_ref))
    return result


def _native_principal_source(source: Mapping[str, Any], sources: list) -> Mapping[str, Any]:
    """One existing Host registry supplies mappings; do not create a second registry."""
    if source.get('kind') != 'pi' or source.get('actor_attribution') is not True or not source.get('node'):
        return source
    histories = {}
    for host in sources:
        if (host.get('kind') != 'relay_host' or host.get('node') != source['node']
                or host.get('actor_attribution') is not True):
            continue
        history = host.get('principal_alias_history', [])
        if not isinstance(history, list):
            raise ReceiptError('invalid enrolled Host principal history')
        for row in history:
            histories[digest(row)] = row
    return {**source, 'principal_alias_history': [histories[k] for k in sorted(histories)]}


def read_pi(path: Path, observed: str, *, into: list[Receipt] | None = None, data: bytes | None = None, source_identity: Mapping[str, Any] | None = None) -> list[Receipt]:
    from .pi_context import PiAncestry, native_summary_model
    ancestry = PiAncestry(path, source_identity)
    metadata_refs = {}
    metadata_actors = {}
    result, session = (into if into is not None else []), None
    histories = {}
    for existing in result:
        histories.setdefault((existing.kind, existing.entity_ref), []).append(existing)
    for index, row in enumerate(_json_lines(path, data)):
        if index == 0:
            if row.get("type") != "session" or not row.get("id"):
                raise ReceiptError("Pi source requires its native session header")
            session = row["id"]
            ancestry.consume(row)
            continue
        context = ancestry.consume(row)
        model_evidence = None
        if row.get("type") == "message" and isinstance(row.get("message"), Mapping):
            msg = row["message"]
            if msg.get("role") not in {"assistant", "toolResult"}:
                continue
            if msg.get("role") == "toolResult" and "usage" not in msg:
                continue
            # Nested tool usage always retains its own model evidence.
            native = msg
            activity = "assistant" if msg.get("role") == "assistant" else "tool_summary"
        elif row.get("type") in {"compaction", "branch_summary"}:
            if row.get("usage") is None:
                continue  # A context-edit event alone is not another model call.
            model, provider, model_evidence = native_summary_model(row, context)
            native, activity = {**row, "model": model, "provider": provider}, row["type"]
        else:
            continue
        if not row.get("id"):
            raise ReceiptError("Pi usage entry lacks a stable identity")
        item = usage_receipt("pi", [session, row["id"], activity], native.get("usage"),
            at=row.get("timestamp"), observed=observed, model=native.get("model"), provider=native.get("provider"),
            # Compaction/branch-summary usage is its own model invocation;
            # only a tool's returned child summary is an overlapping aggregate.
            granularity="turn_aggregate" if activity == "tool_summary" else "request",
            final=True, mapping="pi_tool_summary.v1" if activity == "tool_summary" else "pi_native.v1",
            native_scope_ref=reference("native_session", ["pi", session]))
        evidence = (reference("native_record", digest(row)),)
        if model_evidence:
            evidence = tuple(sorted(set(evidence) | {model_evidence}))
        item = replace(item, evidence=evidence)
        _revision(result, item, histories=histories)
        if source_identity is not None:
            if context.channel and context.input_ref:
                input_ref = None
                executor, principal_proof = None, ()
                if source_identity.get('host_native_input') is True and context.host_input_token:
                    from .pi_context import input_bytes
                    import hashlib
                    token = context.host_input_token
                    if token not in metadata_refs:
                        metadata = input_bytes(source_identity, token)
                        metadata_refs[token] = (reference('input_metadata', hashlib.sha256(metadata).hexdigest())
                                                if metadata is not None else None)
                        metadata_actors[token] = _input_principal(source_identity, token, metadata) if metadata is not None else (None, ())
                    input_ref = metadata_refs[token]
                    executor, principal_proof = metadata_actors[token]
                result.append(actor_receipt(item, node=source_identity.get("node"), executor=executor,
                    entrypoint=context.channel, observed=observed, evidence=(context.input_ref, *principal_proof),
                    input_ref=input_ref, path=["pi_input", source_identity.get("id")],
                    turn_final=(activity == 'assistant' and native.get('stopReason') == 'stop')
                               if input_ref else None))
            elif context.input_boundary_ref and not context.human_input_claimed:
                from .source_layout import unattended_pi_proof
                unattended = unattended_pi_proof(source_identity, path)
                if unattended:
                    result.append(actor_receipt(item, node=source_identity.get("node"),
                        entrypoint="automation", observed=observed,
                        evidence=(context.input_boundary_ref, unattended),
                        path=["pi_unattended", source_identity.get("id")]))
    return result


def actor_receipt(target: Receipt, *, node: str | None, executor: str | None = None,
                  entrypoint: str | None = None, workspace_ref: str | None = None,
                  observed: str, evidence: tuple[str, ...] = (),
                  input_ref: str | None = None, turn_final: bool | None = None, subcall: Mapping[str, Any] | None = None,
                  tenant: Mapping[str, Any] | None = None, usage_fact_ref: str | None = None,
                  path: object = "actor") -> Receipt:
    """Append identity evidence for one existing fact, never another quantity/WI.

    The entity names only stable fields: the target, the producing `path`
    (with its source), node, entrypoint, workspace, input, subcall and tenant
    binding. Executor, effective time, evidence and a tenant's usage version
    are payload, revised in place (`actor_identity.ActorHeads`). A claim bound
    to one exact usage version keeps that version in its identity.
    """
    effective = target.payload.get("interval_start") or target.occurred_at
    context = {"node": node, "entrypoint": entrypoint, "workspace_ref": workspace_ref}
    if tenant is not None:
        context["tenant"] = dict(tenant)
    if subcall is not None:
        context["subcall"] = dict(subcall)
    if input_ref is not None:
        context['input_ref'] = input_ref
        if turn_final is not None:
            context['turn_final'] = turn_final
    identity = ["stable.v1", target.entity_ref, path, node, entrypoint, workspace_ref, input_ref,
                subcall, [tenant.get("id"), tenant.get("registry_ref")] if tenant is not None else None]
    if usage_fact_ref is not None:
        identity.append(usage_fact_ref)
    return Receipt.make("attribution", ["runtime_actor", identity], {
        "target_ref": target.entity_ref, "wi": None, "executor": executor,
        "responsible": None, "delegator": None, "state": "assigned" if executor else "unassigned",
        "policy": "registered_runtime_actor.v1", "effective_at": effective,
        "scope": "actor", "context": context,
        **({"usage_fact_ref": usage_fact_ref} if usage_fact_ref is not None else {}),
    }, occurred_at=effective, observed_at=observed, producer="registered_runtime_actor.v1", evidence=evidence)


def _same_registered_workspace(root: str, cwd: str, cache: dict | None = None, *, allow_descendants: bool = False) -> bool:
    """Exact folder, or its exact relative folder in a registered linked worktree.

    A basename or a copied origin URL is not proof. Both checkouts must share
    the same Git common directory; a different checkout must also be listed
    by Git. The original checkout is already enrolled by the source registry.
    CC may chdir inside the same registered folder; only CC opts into a real
    Git-verified descendant. Symlink escapes, nested foreign repositories and
    unregistered checkouts do not establish actor identity.
    The per-read cache avoids shelling out for each token/turn observation.
    """
    import os
    import subprocess
    left, right = os.path.normpath(root), os.path.normpath(cwd)
    if left == right:
        return True
    key = (left, right, allow_descendants)
    if cache is not None and key in cache:
        return cache[key]
    valid = False
    def git(where, *args):
        # Each answer describes one folder, so pairs sharing a root or cwd
        # reuse it within the same cache lifetime; a failure stays a failure.
        memo = ('git', str(where), args)
        if cache is not None and memo in cache:
            if cache[memo] is None:
                raise ValueError('workspace Git metadata unavailable')
            return cache[memo]
        result = subprocess.run(['git', '-C', str(where), *args], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=5,
            env={**os.environ, 'GIT_OPTIONAL_LOCKS':'0', 'GIT_TERMINAL_PROMPT':'0'})
        answer = None if result.returncode or len(result.stdout)>65536 else result.stdout.rstrip('\n')
        if cache is not None:
            cache[memo] = answer
        if answer is None:
            raise ValueError('workspace Git metadata unavailable')
        return answer
    try:
        if not Path(left).is_dir() or not Path(right).is_dir():
            raise ValueError('workspace is not present')
        declared_top = Path(git(left, 'rev-parse', '--show-toplevel')).resolve()
        actual_top = Path(git(right, 'rev-parse', '--show-toplevel')).resolve()
        declared_relative = Path(left).resolve().relative_to(declared_top)
        actual_relative = Path(right).resolve().relative_to(actual_top)
        def common(where):
            value=Path(git(where, 'rev-parse', '--git-common-dir'))
            return (value if value.is_absolute() else Path(where)/value).resolve()
        same_folder = declared_relative == actual_relative
        cc_child = allow_descendants and actual_relative.is_relative_to(declared_relative)
        if (same_folder or cc_child) and str(declared_relative) != '.' and common(left) == common(right):
            if allow_descendants and actual_top == declared_top:
                # The source already enrolls this checkout; submodules can
                # report their admin directory as the primary worktree.
                valid = True
            else:
                listed=git(left, 'worktree', 'list', '--porcelain', '-z').split('\x00')
                registered={Path(item[len('worktree '):]).resolve() for item in listed if item.startswith('worktree ')}
                valid=actual_top in registered
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if cache is not None:
        cache[key] = valid
    return valid


def workspace_actor(source: Mapping[str, Any], observation: CodexNativeObservation, *, workspace_cache: dict | None = None) -> dict[str, Any]:
    """Exact native startup + declared workspace mapping; no directory scanning.

    The source registry is a private deployment input. It is never reconstructed
    from the current shell cwd, a directory substring, display name or model.
    """
    node = source.get("node")
    from .codex_origin import native_codex_entrypoint
    entrypoint = native_codex_entrypoint(observation.originator, observation.native_source)
    result = {"executor": None, "node": node, "entrypoint": entrypoint, "workspace_ref": None}
    cwd = observation.startup_cwd
    if not isinstance(cwd, str) or not cwd.startswith("/") or not observation.startup_ref:
        return result
    return {**registered_workspace_actor(source, cwd, "codex", workspace_cache=workspace_cache), "entrypoint": entrypoint}


def registered_workspace_actor(source: Mapping[str, Any], cwd: str | None, runtime: str, *, workspace_cache: dict | None = None) -> dict[str, Any]:
    node = source.get("node")
    result = {"executor": None, "node": node, "workspace_ref": None}
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        return result
    rules = source.get("workspace_actors", [])
    if not isinstance(rules, list) or len(rules) > 256:
        raise ReceiptError("invalid registered workspace actors")
    matches = []
    for rule in rules:
        if not isinstance(rule, Mapping) or set(rule) != {"workspace_root", "agent", "node", "runtime", "registry_ref"}:
            raise ReceiptError("invalid workspace actor rule")
        root, agent = rule["workspace_root"], rule["agent"]
        if (not isinstance(root, str) or not root.startswith("/") or not isinstance(agent, str)
                or not __import__("re").fullmatch(r"[a-z][a-z0-9_-]{0,63}", agent)):
            raise ReceiptError("invalid registered workspace identity")
        if rule["node"] == node and rule["runtime"] == runtime and _same_registered_workspace(root, cwd, workspace_cache, allow_descendants=runtime == "cc"):
            matches.append(rule)
    if len({(m['agent'], m['registry_ref']) for m in matches}) > 1:
        raise ReceiptError("conflicting workspace actor rules")
    if matches:
        rule = matches[0]
        result.update(executor=rule['agent'], workspace_ref=reference("workspace", [node, cwd, rule['registry_ref']]))
    return result


def host_entrypoint_receipts(source: Mapping[str, Any], files: list[Path],
                             rows: list[Receipt], observed: str) -> list[Receipt]:
    """Read channel metadata from the existing authenticated input artifacts.

    Only an exact Host event linking the input to an execution is used. Message
    contents and raw identities never leave this reader. This creates no usage,
    task assignment or authorization; missing proof leaves the channel unknown.
    """
    import hashlib
    import os
    from .pi_context import input_directories, input_bytes
    directories = input_directories(source)
    if not directories:
        return []
    for directory in directories:
        if (directory.is_symlink() or not directory.is_dir()
                or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077):
            raise ReceiptError("registered input metadata must be a private directory")
    candidates = [raw for path in files for raw in _json_lines(path)
                  if raw.get('schema') == HOST_SCHEMA]
    claims = [raw for raw in candidates if raw.get('kind') == 'attribution'
              and raw.get('policy') == 'authenticated_dispatch_assignment.v1']
    direct = [raw for raw in candidates if raw.get('kind') == 'execution'
              and raw.get('state') == 'accepted' and raw.get('human_ref')
              and raw.get('human_input_at')]
    wanted = {raw['target_ref'] for raw in claims} | {raw['human_ref'] for raw in direct}
    channels = {}
    for path in sorted({p for directory in directories for p in directory.glob("*.json")}):
        if not __import__("re").fullmatch(r"[a-f0-9]{64}", path.stem):
            continue
        human = reference("human", path.stem)
        if human not in wanted:
            continue
        data = input_bytes(source, path.stem)
        if data is None:
            continue
        record = json.loads(data)
        origin = record["source"]
        channel = origin.get("channel")
        values = [channel, origin.get("eventId"), origin.get("humanRef"), record.get("poolKey")]
        if channel not in {"feishu", "telegram"} or any(not isinstance(v, str) or not v for v in values):
            raise ReceiptError("invalid input channel identity")
        raw = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
        if hashlib.sha256(raw).hexdigest() != path.stem:
            raise ReceiptError("input reference does not match its metadata")
        instant = record.get("observedAtMs")
        if type(instant) is not int or instant <= 0:
            raise ReceiptError("invalid input time")
        channels[human] = (channel, instant, reference("input_metadata", hashlib.sha256(data).hexdigest()))
    executions = {r.entity_ref: r for r in rows if r.kind == "execution" and r.producer == "relay_host.v1"}
    result = []
    for claim in claims:
        match = channels.get(claim["target_ref"])
        if not match:
            continue
        channel, milliseconds, evidence = match
        effective = utc_text(datetime.fromtimestamp(milliseconds / 1000, timezone.utc))
        if parse_timestamp(claim["effective_at"], "input") != parse_timestamp(effective, "input"):
            raise ReceiptError("input boundary disagrees with the Host")
        for execution in executions.values():
            p = execution.payload
            if (not p.get("assignment_ref") or not execution.occurred_at
                    or parse_timestamp(effective, "input") > parse_timestamp(execution.occurred_at, "dispatch")
                    or any(p.get(k) != claim.get(k) for k in _common())):
                continue
            if claim.get("event_ref") != reference("host_event", ["human_assignment", execution.entity_ref, claim["target_ref"]]):
                continue
            if claim.get("attribution_ref") != reference("attribution", ["human_assignment", claim["target_ref"], p["assignment_ref"]]):
                continue
            result.append(actor_receipt(execution, node=source.get("node"), executor=p.get("executor"),
                entrypoint=channel, observed=observed, evidence=(evidence, claim["event_ref"]),
                input_ref=evidence if source.get('host_native_input') is True else None,
                path=['host_input', source.get('id')]))
    for claim in direct:
        execution = executions.get(claim.get('attempt_ref'))
        match = channels.get(claim['human_ref'])
        if execution is None or match is None:
            continue
        channel, milliseconds, evidence = match
        effective = utc_text(datetime.fromtimestamp(milliseconds / 1000, timezone.utc))
        p = execution.payload
        # Exact producer identity, routing fields and input boundary must all
        # agree. No time-neighbour, raw pool-name or current-WI inference.
        expected = reference('host_event', [execution.entity_ref, 'authenticated_input', claim['human_ref']])
        if (claim.get('event_ref') != expected or not execution.occurred_at
                or claim.get('runtime') != p['runtime']
                or claim.get('assignment_ref') != p.get('assignment_ref')
                or any(claim.get(k) != p.get(k) for k in _common())
                or parse_timestamp(claim['human_input_at'], 'input') != parse_timestamp(effective, 'input')
                or parse_timestamp(claim.get('started_at'), 'dispatch') != parse_timestamp(execution.occurred_at, 'dispatch')
                or parse_timestamp(claim['at'], 'dispatch') != parse_timestamp(execution.occurred_at, 'dispatch')
                or parse_timestamp(effective, 'input') > parse_timestamp(execution.occurred_at, 'dispatch')):
            continue
        result.append(actor_receipt(execution, node=source.get('node'), executor=p.get('executor'),
            entrypoint=channel, observed=observed, evidence=(evidence, claim['event_ref']),
            input_ref=evidence if source.get('host_native_input') is True else None,
            path=['host_direct_input', source.get('id')]))
    result.extend(_continuation_routes(result, executions, rows, source, observed))
    from .host_recovery_input import host_recovery_inputs
    result.extend(host_recovery_inputs(source, candidates, rows, observed))
    return result


def _continuation_routes(routes: list[Receipt], executions: Mapping[str, Receipt], rows: list[Receipt],
                         source: Mapping[str, Any], observed: str) -> list[Receipt]:
    """A runtime-notification attempt continues its declared parent dispatch.

    Relay records the parent of a turn the runtime started itself (background
    work completing). Without its own human input, the child carries the
    parent's proven route only when runtime, executor and the single native
    session agree; anything else stays unknown.
    """
    scopes = defaultdict(set)
    for row in rows:
        p = row.payload
        if row.kind == 'usage' and p.get('execution_ref') and p.get('native_scope_ref'):
            scopes[p['execution_ref']].add(p['native_scope_ref'])
    proven = defaultdict(list)
    for route in routes:
        if route.payload['context'].get('entrypoint') in {'telegram', 'feishu'}:
            proven[route.payload['target_ref']].append(route)
    result = []
    for child in executions.values():
        p = child.payload; parent = executions.get(p.get('parent_ref'))
        parents = proven.get(p.get('parent_ref'), [])
        if (parent is None or child.entity_ref in proven or len(parents) != 1 or not child.occurred_at
                or p['runtime'] != parent.payload['runtime'] or not p.get('executor')
                or p['executor'] != parent.payload['executor']
                or len(scopes[child.entity_ref]) != 1 or scopes[child.entity_ref] != scopes[parent.entity_ref]
                or parse_timestamp(child.occurred_at, 'child') < parse_timestamp(parent.occurred_at, 'parent')):
            continue
        context = parents[0].payload['context']
        result.append(actor_receipt(child, node=source.get('node'), executor=p['executor'],
            entrypoint=context['entrypoint'], observed=observed,
            evidence=(parents[0].fact_ref, reference('host_continuation', [child.entity_ref, parent.entity_ref])),
            input_ref=context.get('input_ref'), path=['host_continuation', source.get('id')]))
    return result


def _codex_rollout_file_ids(path: Path) -> tuple[str, str] | None:
    """Codex RolloutFileName: thread UUID, optionally followed by rollout UUID.

    Source: openai/codex codex-rs/rollout/src/rollout_file_name.rs. This helper
    does not change legacy archive/fork selection or turn identities.
    """
    import re
    import uuid
    match = re.fullmatch(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-([0-9a-f-]{36})(?:_([0-9a-f-]{36}))?\.jsonl", path.name)
    if not match:
        return None
    try:
        datetime.strptime(match[1], '%Y-%m-%dT%H-%M-%S')
        thread = str(uuid.UUID(match[2])); rollout = str(uuid.UUID(match[3] or match[2]))
        return thread, rollout
    except ValueError:
        return None


def _codex_paginated_context(path: Path, session: str, candidates: list[Path]) -> dict[str, Any] | None:
    """Verify the exact carried counter, including counter-free retry segments.

    Every history edge must prove its logical thread, physical segment, bytes
    and ordinal boundary. A repeated cumulative notification proves no new
    consumption; its last_token_usage may still describe the previous response.
    Neither a filename nor a missing counter supplies an implicit zero.
    """
    import hashlib
    ids = _codex_rollout_file_ids(path)
    if ids is None or ids[0] == ids[1] or ids[0] != session or path.is_symlink():
        return None
    metadata, first, meta_bytes = None, None, None
    keys = ('input_tokens','cached_input_tokens','cache_write_input_tokens','output_tokens')

    def boundary_counter(history, visited, depth=0):
        if not isinstance(history, dict) or depth >= 16:
            return None
        offset, ordinal = history.get('end_byte_offset'), history.get('end_ordinal_exclusive')
        base_id = history.get('thread_id')
        edge = (base_id, offset, ordinal)
        if (not isinstance(base_id, str) or type(offset) is not int or type(ordinal) is not int
                or offset <= 0 or ordinal <= 0 or edge in visited):
            return None
        prefixes = []
        for candidate in candidates:
            base_ids = _codex_rollout_file_ids(candidate)
            if (base_ids is None or base_ids[0] != session or base_ids[1] != base_id
                    or candidate == path or candidate.is_symlink() or not candidate.is_file()
                    or candidate.stat().st_size < offset):
                continue
            hasher = hashlib.sha256()
            counter = counter_at = last_ordinal = header = None
            valid = True
            with candidate.open('rb') as stream:
                while stream.tell() < offset:
                    raw = stream.readline(offset - stream.tell() + 1)
                    if not raw or not raw.endswith(b'\n') or stream.tell() > offset:
                        valid = False
                        break
                    hasher.update(raw)
                    row = json.loads(raw)
                    last_ordinal = row.get('ordinal')
                    value = row.get('payload', {})
                    if not isinstance(value, dict):
                        valid = False
                        break
                    if row.get('type') == 'session_meta':
                        if (header is not None or value.get('id') != session
                                or value.get('session_id', session) != session):
                            valid = False
                            break
                        header = value
                    if value.get('type') == 'token_count' and isinstance(value.get('info'), dict):
                        observed = value['info'].get('total_token_usage')
                        if observed is not None:
                            counter, counter_at = observed, row.get('timestamp')
                at_boundary = stream.tell() == offset and last_ordinal == ordinal - 1
            if valid and at_boundary and header is not None:
                prefixes.append((hasher.hexdigest(), counter, counter_at, header))
        if not prefixes or len({p[0] for p in prefixes}) != 1:
            return None
        prefix_hash, counter, counter_at, header = prefixes[0]
        proof = [base_id, offset, ordinal, prefix_hash]
        if counter is not None:
            if (not counter_at or not isinstance(counter, dict)
                    or any(type(counter.get(k)) is not int or counter[k] < 0 for k in keys)):
                return None
            return counter, counter_at, [proof]
        # A retry may only have metadata before its selected cutoff. Follow
        # its verified history edge; do not scan beyond that cutoff for a value.
        if header.get('history_mode') != 'paginated':
            return None
        inherited = boundary_counter(header.get('history_base'), visited | {edge}, depth+1)
        if inherited is None:
            return None
        counter, counter_at, chain = inherited
        return counter, counter_at, [proof, *chain]

    try:
        with path.open('rb') as stream:
            for position, raw in enumerate(stream, 1):
                if not raw.endswith(b'\n'):
                    break
                if not any(term in raw for term in (b'"session_meta"',b'"token_count"')):
                    continue
                row = json.loads(raw)
                value = row.get('payload', {})
                if not isinstance(value, dict):
                    return None
                if row.get('type') == 'session_meta':
                    if metadata is not None or value.get('id') != session or value.get('session_id', session) != session:
                        return None
                    metadata, meta_bytes = value, raw
                elif value.get('type') == 'token_count' and first is None and isinstance(value.get('info'), dict):
                    first = (position, row.get('timestamp'), value['info'])
        if not metadata or metadata.get('history_mode') != 'paginated':
            return None
        context = {'startup': {k:metadata.get(k) if isinstance(metadata.get(k),str) else None
                               for k in ('cwd','originator','source')},
                   'startup_ref': reference('codex_paginated_startup', [ids, hashlib.sha256(meta_bytes).hexdigest()]),
                   'baseline': None, 'baseline_ref': None, 'first_position': first[0] if first else None}
        if first:
            total, last = first[2].get('total_token_usage'), first[2].get('last_token_usage')
            if (isinstance(total, dict) and isinstance(last, dict)
                    and all(type(total.get(k)) is int and total[k] >= 0
                            and type(last.get(k)) is int and total[k] == last[k] for k in keys)):
                context['baseline'] = dict.fromkeys(keys, 0)
                context['baseline_ref'] = reference('codex_native_zero_origin',
                    [ids, context['startup_ref'], first[0], first[1], {k:total[k] for k in keys}])
                return context
        if not first:
            return context
        inherited = boundary_counter(metadata.get('history_base'), set())
        if inherited is None:
            return context
        baseline, baseline_at, chain = inherited
        total, last = first[2].get('total_token_usage'), first[2].get('last_token_usage')
        if not isinstance(total, dict) or not isinstance(last, dict):
            return context
        if (parse_timestamp(baseline_at,'base') > parse_timestamp(first[1],'new counter')
                or any(type(mapping.get(k)) is not int or mapping[k] < 0
                       for mapping in (baseline,total,last) for k in keys)):
            return context
        unchanged = all(total[k] == baseline[k] for k in keys)
        advanced = all(total[k] - baseline[k] == last[k] for k in keys)
        if not (unchanged or advanced):
            return context
        context['baseline'] = {k:baseline[k] for k in keys}
        context['baseline_ref'] = reference('codex_paginated_counter_base',
            [ids, chain, context['baseline'], first[0], 'unchanged' if unchanged else 'advanced'])
        return context
    except (OSError, ValueError, TypeError, KeyError):
        return None


def read_codex(path: Path | list[Path], observed: str, *, diagnostic_out: list[str] | None = None, source_identity: Mapping[str, Any] | None = None, native_cache=None, metering: bool = False,
               workspace_cache: dict | None = None) -> list[Receipt]:
    # The adapter alone selects canonical archive/fork evidence. Its side-channel
    # preserves native cumulative counters and field watermarks for receipts.
    observations: dict[str, CodexNativeObservation] = {}
    workspace_cache = {} if workspace_cache is None else workspace_cache
    hosts_by_usage: dict[str, str] = {}
    roots = path if isinstance(path,list) else [path]
    candidate_paths = sorted({f for root in roots for f in (root.rglob('*.jsonl') if root.is_dir() else [root])})
    # Explicitly enrolled dependency roots supply only a referenced parent
    # prefix, not additional historical usage to bill in this collection.
    dependency_paths = list(candidate_paths)
    for value in (source_identity or {}).get("counter_dependency_paths", []):
        root = Path(value)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ReceiptError("unsafe Codex counter dependency root")
        dependency_paths.extend(f for f in root.rglob("*.jsonl") if not f.is_symlink())
    dependency_paths = sorted(set(dependency_paths))
    paginated_cache: dict[Path, dict[str, Any] | None] = {}
    metering_cache = {}
    from .native_parse_cache import file_signature
    dependency_signature = file_signature(dependency_paths) if native_cache else None
    if native_cache is None:
        collection = collect_usage_events(machine="registered-source", observed_at=observed,
                                          codex_sessions_dir=path, _codex_native_observations=observations)
    else:
        collection, observations = native_cache.codex(roots, candidate_paths, observed, collect_usage_events)
    if diagnostic_out is not None and collection.diagnostics.malformed_rows.get("codex_rollout", 0):
        diagnostic_out.append("codex_native_records_invalid")
    from .codex_origin import codex_entrypoint_origins
    actor_sources = []
    result = []
    first_positions = {}
    for observation in observations.values():
        first_positions[observation.source_path] = min(first_positions.get(observation.source_path, observation.source_position), observation.source_position)
    fields = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
    for event in collection.events:
        observation = observations.get(event.event_id)
        if observation is None:
            continue
        if observation.source_path not in paginated_cache:
            def context_reader():
                return _codex_paginated_context(observation.source_path, observation.session_id, dependency_paths)
            paginated_cache[observation.source_path] = (native_cache.context(observation.source_path,
                observation.session_id, dependency_signature, context_reader) if native_cache else context_reader())
        context = paginated_cache[observation.source_path]
        baseline_proof = None
        corrected_paginated = False
        if context is not None and observation.startup_ref is None:
            observation = replace(observation, startup_cwd=context['startup']['cwd'],
                originator=context['startup']['originator'], native_source=context['startup']['source'],
                startup_ref=context['startup_ref'])
        ids = _codex_rollout_file_ids(observation.source_path)
        segmented = ids is not None and ids[0] != ids[1]
        if (segmented and observation.source_position == first_positions[observation.source_path]
                and 'counter_origin_restarted_from_native_last' not in event.quality_flags):
            if context is not None and context['baseline'] is not None and all(observation.previous_counter.get(k) == 0 for k in fields):
                observation = replace(observation, previous_counter={**observation.previous_counter, **context['baseline']},
                    previous_fields=frozenset(fields))
                baseline_proof = context['baseline_ref']; corrected_paginated = True
            elif context is None or context['baseline'] is None:
                observation = replace(observation, previous_fields=frozenset())
                if diagnostic_out is not None:
                    diagnostic_out.append('codex_counter_scope_unresolved')
        native = {key: observation.counter[key] for key in fields if key in observation.counter_fields}
        # A missing prior watermark is unknown, not an implicit zero. Native
        # counters remain native cumulative values; only normalized values delta.
        delta = {key: (observation.counter[key] - observation.previous_counter[key]
                       if key in observation.counter_fields and key in observation.previous_fields else None)
                 for key in fields}
        uncached = (delta["input_tokens"] - delta["cached_input_tokens"] - delta["cache_write_input_tokens"]
                    if all(delta[key] is not None for key in fields[:3]) else None)
        scope = reference("native_session", ["codex", observation.session_id])
        p = {**_common(), "execution_ref": None, "native_scope_ref": scope, "runtime": "codex",
             "model": None if event.model == "unknown" else event.model,
             "provider": None if event.provider == "unknown" else event.provider,
             "tokens": {"uncached_input": uncached, "cache_read": delta["cached_input_tokens"],
                        "cache_write": delta["cache_write_input_tokens"], "output": delta["output_tokens"]},
             # Inclusive input + output conserves consumption even when cache
             # partitions are unknown. It is not an additional Token class and
             # never supplies a price for unknown partitions.
             "reported_total_tokens": (delta["input_tokens"] + delta["output_tokens"]
                 if delta["input_tokens"] is not None and delta["output_tokens"] is not None else None),
             "native_tokens": native, "mapping": "codex_rollout_paginated.v1" if corrected_paginated else "codex_rollout_cumulative.v1", "granularity": "turn_aggregate",
             "coverage_ref": None, "covers": [], "interval_start": observation.interval_start,
             "interval_end": observation.interval_end,
             "final": not any("incomplete" in f for f in event.quality_flags)}
        if metering:
            from .native_metering import extract_metering
            timing = extract_metering(observation, p, metering_cache)
            if timing is not None:
                p["metering"] = timing
        item = Receipt.make("usage", event.event_id, p, occurred_at=event.occurred_at,
                            observed_at=observed, producer="codex_rollout.v1",
                            evidence=(baseline_proof,) if baseline_proof else ())
        result.append(item)
        if source_identity is not None:
            from .scl_session_attribution import codex_host_ref
            hosts_by_usage[item.entity_ref] = codex_host_ref(observation.session_id)
            actor = workspace_actor(source_identity, observation, workspace_cache=workspace_cache)
            evidence = (observation.startup_ref,) if observation.startup_ref else ()
            actor_sources.append((item, observation, actor, evidence))
    # Parent startup can itself require a verified paginated source. Resolve
    # origins only after all observations have completed that existing check.
    origins = codex_entrypoint_origins(row[1] for row in actor_sources)
    from .verification_workspace import historical_bindings, observation_binding, task_receipt
    try:
        verification = historical_bindings(source_identity or {}, [row[1] for row in actor_sources], diagnostic_out)
    except (OSError, ValueError, TypeError, KeyError, __import__('yaml').YAMLError):
        verification = {}
        if diagnostic_out is not None:
            diagnostic_out.append('verification_workspace_binding_unavailable')
    for item, observation, actor, evidence in actor_sources:
        try:
            proven = observation_binding(observation, verification) if verification else None
        except (OSError, ValueError, TypeError, KeyError):
            proven = None
        if verification.get(observation.startup_cwd) and proven is None and diagnostic_out is not None:
            diagnostic_out.append('verification_workspace_interval_unproven')
        if proven:
            # A conflicting independent actor assertion is retained as its own
            # evidence; this past assignment cannot overwrite a static rule.
            result.append(actor_receipt(item, node=source_identity.get('node'),
                executor=proven['agent'], workspace_ref=proven['workspace_ref'],
                observed=observed, evidence=proven['proof'], usage_fact_ref=item.fact_ref,
                path=['codex_verification', source_identity.get('id')]))
            result.append(task_receipt(item, proven, observed))
        inherited = origins.get(observation.session_id)
        if actor['entrypoint'] is None and observation.parent_session_id and inherited:
            actor['entrypoint'] = inherited[0]
            evidence = tuple(sorted(set(evidence) | set(inherited[1])))
        result.append(actor_receipt(item, **actor, observed=observed, evidence=evidence,
                                    path=['codex_workspace', (source_identity or {}).get('id')]))
    if diagnostic_out is not None and any(r.kind == "usage" and
            any(v is None for v in r.payload["tokens"].values()) for r in result):
        diagnostic_out.append("codex_counter_scope_unresolved")
    if source_identity is not None and source_identity.get("scl_kernel_root"):
        import sqlite3
        from .scl_session_attribution import attribution_receipts
        try:
            result.extend(attribution_receipts(result, hosts_by_usage, source_identity, observed))
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            # A missing binding source never discards native quantities or a
            # separately proven Agent. Do not fall back to legacy executions.
            if diagnostic_out is not None:
                diagnostic_out.append("scl_session_binding_unavailable")
    return result


def paginated_counter_correction(previous: Receipt, current: Receipt) -> Receipt | None:
    """Version a previously zero-based interpretation of an exact native counter.

    Only the paginated reader above can supply the verified baseline evidence.
    The old assertion stays queryable at its original watermark. This is not
    a general permission to overwrite differing final quantities or models.
    """
    if previous.kind != 'usage' or current.kind != 'usage' or previous.entity_ref != current.entity_ref:
        return None
    old, new = previous.payload, current.payload
    if (old['mapping'] != 'codex_rollout_cumulative.v1' or new['mapping'] != 'codex_rollout_paginated.v1'
            or previous.producer != 'codex_rollout.v1' or current.producer != previous.producer
            or not current.evidence):
        return None
    keys = ('input_tokens','cached_input_tokens','cache_write_input_tokens','output_tokens')
    if any(type(old['native_tokens'].get(k)) is not int or type(new['native_tokens'].get(k)) is not int
           or new['native_tokens'][k] < old['native_tokens'][k] for k in keys):
        return None
    expected_old = {'uncached_input': old['native_tokens']['input_tokens']-old['native_tokens']['cached_input_tokens']-old['native_tokens']['cache_write_input_tokens'],
                    'cache_read':old['native_tokens']['cached_input_tokens'],
                    'cache_write':old['native_tokens']['cache_write_input_tokens'],'output':old['native_tokens']['output_tokens']}
    if (old['tokens'] != expected_old and not all(old['tokens'][k] is None for k in TOKEN_FIELDS)) or any(new['tokens'][k] is None for k in TOKEN_FIELDS):
        return None
    unchanged = ('runtime','model','provider','native_scope_ref','granularity','wi','executor','responsible','delegator',
                 'execution_ref','assignment_ref','coverage_ref','covers','interval_start')
    if (any(old.get(k) != new.get(k) for k in unchanged)
            or previous.occurred_at is None or current.occurred_at is None
            or parse_timestamp(current.occurred_at,'current') < parse_timestamp(previous.occurred_at,'previous')):
        return None
    return Receipt.make(current.kind,None,new,entity_ref=current.entity_ref,supersedes=previous.fact_ref,
        occurred_at=current.occurred_at,observed_at=current.observed_at,producer=current.producer,
        evidence=tuple(sorted(set(previous.evidence) | set(current.evidence))))


def import_harness_bundle(value: Mapping[str, Any], observed: str) -> list[Receipt]:
    """Use existing validators. Context supplies explicit periods, NOT fake value.

    This is a transport projection from the trusted Harness/consumer integration.
    Self-generated SHA values never promote untrusted input into that authority.
    """
    if value.get("schema") != "computecost_harness_import/v1":
        raise ReceiptError("unsupported Harness import")
    wi = value["work_item_id"]
    review = validate_harness_review_snapshot(value["review"], expected_work_item_id=wi)
    measurement = validate_harness_measurement_snapshot(value["measurement"], expected_work_item_id=wi) if value.get("measurement") else None
    delivery_ref = reference("delivery", value.get("delivery_id") or wi)
    candidate = reference("candidate", review["candidate_fingerprint"])
    artifact = reference("artifact", review["artifact_set_sha256"])
    quality = measurement["quality"] if measurement else {}
    if measurement and quality["final_review_status"] != review["decision"]:
        raise ReceiptError("Harness measurement and exact review disagree")
    status = quality.get("delivery_status", "unknown")
    status = status if status in {"accepted", "partial", "rejected", "blocked"} else "unknown"
    accepted_at = measurement["efficiency"].get("delivery_accepted_at") if measurement else None
    accepted_at = accepted_at if isinstance(accepted_at, str) and accepted_at != "unknown" else None
    at = review["reviewed_at"]
    rows = [Receipt.make("delivery", None, {"wi": wi, "responsible": value.get("responsible"),
            "candidate_ref": candidate, "artifact_ref": artifact,
            "acceptance_ref": reference("acceptance", review["acceptance_fingerprint"]),
            "status": status, "accepted_at": accepted_at}, occurred_at=at, observed_at=observed,
            producer="harness_review.v1", entity_ref=delivery_ref, supersedes=value.get("supersedes_delivery_fact"))]
    waiver = review.get("outcome_waiver")
    # A new v2 review-only record asserts a human decision, not a second
    # delivery competing with the separately imported canonical Outcome.
    # Keep the established v1 projection and evidence identities unchanged.
    if waiver is not None and measurement is None:
        rows = []
    def known(v: Any) -> Any:
        return None if v == "unknown" else v
    rows.append(Receipt.make("review", [delivery_ref, review["candidate_fingerprint"], review["reviewer"], at],
        {"wi": wi, "delivery_ref": delivery_ref, "candidate_ref": candidate, "reviewer": review["reviewer"],
         "decision": review["decision"], "first_pass": known(quality.get("first_pass_review")),
         "rework_rounds": known(quality.get("review_rework_rounds")),
         "post_approval_corrections": known(quality.get("post_approval_corrections")),
         "rubric": review["acceptance_fingerprint"], "score": None,
         "verification": "human_waiver" if waiver is not None else "human_review", "critical": []}, occurred_at=at, observed_at=observed,
         producer="harness_measurement.v1",
         evidence=(reference("harness_review_snapshot", review["snapshot_sha256"]),) if waiver is not None else ()))
    if measurement and isinstance(measurement["cost"]["human_attention"], Mapping):
        h = measurement["cost"]["human_attention"]
        rows.append(Receipt.make("human_summary", wi, {"wi": wi, "count": h["raw_turn_count"],
            "bands": h["effort_band_counts"], "focused_seconds": None, "method": h["contract_version"]},
            occurred_at=None, observed_at=observed, producer="harness_measurement.v1"))
    if value.get("business_value"):
        if review["decision"] != "approved" or status != "accepted":
            raise ReceiptError("value requires approved and accepted Harness artifacts")
        receipt = validate_business_value_receipt(value["business_value"], expected_work_item_id=wi,
                                                  expected_artifact_set_sha256=review["artifact_set_sha256"])
        m = receipt["measurement"]
        context = value.get("value_context", {})
        amount = None
        if m["delta"] is not None:
            amount = decimal(m["delta"], "value delta") * (1 if m["value_direction"] == "higher_is_value" else -1)
        payload = {"wi": wi, "delivery_ref": delivery_ref, "artifact_ref": artifact,
            "effect_ref": reference("effect", context.get("effect_id") or receipt["outcome_ref"]),
            "consumer": receipt["consumer"], "verifier": receipt["verifier"],
            "amount": decimal_text(amount) if amount is not None else None,
            "unit": "USD" if m["unit"].lower() == "usd" else m["unit"],
            "mode": context.get("mode", "snapshot"), "period_start": context.get("period_start"),
            "period_end": context.get("period_end"), "realization": "qualitative" if m["kind"] == "qualitative" else "observed",
            "complete": context.get("complete", False), "allocations": context.get("allocations", [])}
        rows.append(Receipt.make("value", receipt["receipt_sha256"], payload, occurred_at=receipt["observed_at"],
            observed_at=observed, producer="consumer_value.v1"))
    return rows



def _structured_document(path: Path) -> Mapping[str, Any]:
    """Read only explicitly registered canonical data, never arbitrary YAML code."""
    if path.is_symlink() or not path.is_file():
        raise ReceiptError("unsafe Harness source")
    raw = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        value = json.loads(raw)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise ReceiptError("Harness YAML discovery requires PyYAML") from exc
        # BaseLoader preserves timestamps/decimal strings for the existing validators.
        # Scalar types in typed measurement JSON must not be guessed from YAML text.
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ReceiptError("invalid Harness YAML document") from exc
    if not isinstance(value, Mapping):
        raise ReceiptError("Harness source must contain one object")
    return value


def _scl_expectations(root: Path, observed: str, into: list[Receipt]) -> list[Path]:
    """Read the independent, already-persisted SCL epoch denominator.

    Reuse the existing exact attribution/filename/seal/Pi-boundary validator.
    These are not Host attempts and not usage; a found epoch seal cannot be
    added to its native requests. A missing or unjoined epoch stays visible.
    """
    folder = root / "evidence/compute-cost/attributions"
    if folder.is_symlink():
        raise ReceiptError("unsafe SCL attribution directory")
    files: list[Path] = []
    if not folder.exists():
        return files
    for wi_dir in sorted(folder.iterdir()):
        if wi_dir.is_symlink() or not wi_dir.is_dir():
            raise ReceiptError("unsafe SCL WI attribution source")
        # The existing loader validates the canonical WI and every join, including
        # missing final/Pi artifacts. Directory existence is never 'complete WI'.
        snapshot = load_scl_measurement_snapshot(root, wi_dir.name)
        snapshot = validate_scl_measurement_snapshot(snapshot, expected_work_item_id=wi_dir.name)
        for epoch in snapshot["epochs"]:
            seal = epoch["usage"]
            bounded = (epoch["pi_usage"] if epoch["runtime"] == "pi"
                       else epoch["cc_usage"] if epoch["runtime"] in {"cc", "cc_sdk"} else None)
            native = bounded if bounded is not None else seal
            sealed = isinstance(seal, Mapping) and isinstance(native, Mapping)
            evidence = native.get("content_sha256") if sealed else None
            payload = {"wi": wi_dir.name, "executor": epoch["agent_id"],
                "runtime": ("cc_sdk" if epoch["runtime"] == "cc-sdk" else epoch["runtime"]
                            if epoch["runtime"] in {"pi", "codex", "cc", "cc_sdk"} else "unknown"),
                "scope": "scl_measurement_epoch", "source_ref": reference("scl_epoch", epoch["measurement_epoch_ref"]),
                "assignment_ref": (reference("assignment", bounded["runtime_binding_sha256"])
                                   if isinstance(bounded, Mapping) else None),
                "usage_ref": reference("scl_usage_evidence", evidence) if evidence else None,
                "started_at": epoch["started_at"], "ended_at": epoch["ended_at"],
                "status": "sealed" if sealed else "missing"}
            into.append(Receipt.make("execution_expectation", [wi_dir.name, epoch["measurement_epoch_ref"]],
                payload, occurred_at=epoch["started_at"], observed_at=observed, producer="scl_measurement.v2",
                evidence=(reference("scl_attribution", epoch["content_sha256"]),)))
        for subfolder in (wi_dir, root / "evidence/compute-cost/observations" / wi_dir.name,
                          root / "evidence/task-outcomes" / wi_dir.name):
            if subfolder.is_symlink():
                raise ReceiptError("unsafe SCL evidence directory")
            for source in sorted(subfolder.rglob("*.json")):
                if source.is_symlink():
                    raise ReceiptError("unsafe SCL evidence file")
                files.append(source)
    return files


def _is_harness_reference(document: Mapping[str, Any], source: Path, root: Path) -> bool:
    """Recognize retained authority notes, not usage, approval or recovered evidence.

    This documented schema is inventory-only. Its content hash is retained by
    the caller; none of its claims is promoted to a new cost/review fact.
    """
    if (document.get("schema_version") or document.get("schema")) != "s1_terminal_authority_reconciliation/v1":
        return False
    wi = document.get("work_item_id")
    if (not isinstance(wi, str) or not __import__("re").fullmatch(r"wi_\d{8}_\d{3}", wi)
            or source != root / "evidence" / "measurements" / wi / "terminal-authority-reconciliation.yaml"):
        raise ReceiptError("invalid Harness reference document identity")
    return True


def read_harness_root(path: Path, observed: str, into: list[Receipt], *, detail: dict[str, Any] | None = None, include_outcomes: bool = False) -> list[Path]:
    """Discover exact reviews and measurements at the existing canonical paths.

    A review snapshot is not a delivery acceptance or a complete review history.
    Measures are never attached to a newer candidate by WI name alone. Existing
    consumer bundles may supply the exact reviewed set and value-period context.
    """
    root = path / ".harness" if (path / ".harness").is_dir() else path
    if root.is_symlink():
        raise ReceiptError("unsafe Harness source")
    files: list[Path] = _scl_expectations(root, observed, into)
    if detail is not None:
        detail["files_seen"] += len(files)
        detail["files_read"] += len(files)
    for folder in (root / "reviews", root / "evidence" / "measurements", root / "evidence" / "business-values"):
        if folder.is_symlink():
            raise ReceiptError("unsafe Harness source")
        if not folder.is_dir():
            continue
        for source in sorted(folder.rglob("*")):
            if not source.is_file() or source.suffix not in {".yaml", ".yml", ".json"}:
                continue
            if detail is not None:
                detail["files_seen"] += 1
            pending: list[Receipt] = []
            readable = False
            try:
                document = _structured_document(source)
                readable = True
                schema = document.get("schema_version") or document.get("schema")
                if _is_harness_reference(document, source, root):
                    continue
                if schema in HARNESS_REVIEW_SCHEMAS:
                    wi = document.get("work_item_id")
                    pending.extend(import_harness_bundle({"schema": "computecost_harness_import/v1",
                        "work_item_id": wi, "review": document}, observed))
                elif schema == "s1_measurement_pilot.v1":
                    wi = document.get("work_item_id")
                    measure = validate_harness_measurement_snapshot(document, expected_work_item_id=wi)
                    h = measure["cost"]["human_attention"]
                    if isinstance(h, Mapping):
                        pending.append(Receipt.make("human_summary", wi, {"wi":wi, "count":h["raw_turn_count"],
                            "bands": h["effort_band_counts"], "focused_seconds":None,
                            "method": h["contract_version"]}, occurred_at=None, observed_at=observed,
                            producer="harness_measurement.v1"))
                    # The WI-wide measurement is deliberately NOT turned into a candidate approval.
                elif schema == "s1_human_attention_evidence/v1":
                    # Actual event import remains separate from WI summary counts;
                    # load_scl_measurement_snapshot validates this producer below.
                    joined = load_scl_measurement_snapshot(root, str(document.get("work_item_id")))
                    validate_scl_measurement_snapshot(joined, expected_work_item_id=str(document.get("work_item_id")))
                elif schema == "computecost_harness_import/v1":
                    pending.extend(import_harness_bundle(document, observed))
                else:
                    # A registered canonical directory with a new schema must not look fully imported.
                    raise ReceiptError("unsupported Harness source schema")
            except (OSError, ValueError, TypeError, KeyError, ReceiptError) as exc:
                if detail is None:
                    raise
                # Keep the existing source-detail wire vocabulary so retained
                # receipts remain readable by the prior runtime on rollback.
                category = "source_unreadable" if isinstance(exc, OSError) else "invalid_native_record"
                failure(detail, category)
            else:
                into.extend(pending)
            finally:
                # Include every readable candidate in source provenance, also
                # reference-only notes and unsupported schemas; never publish paths.
                if readable:
                    files.append(source)
                    if detail is not None:
                        detail["files_read"] += 1
    if include_outcomes:
        from .canonical_outcomes import read_canonical_outcomes
        files.extend(read_canonical_outcomes(root, observed, into, detail))
    return files

def host_usage_coverage(rows: list[Receipt]) -> tuple[int, int, str, str | None]:
    """Keep the original denominator; pending is not complete or a read error."""
    executions = {r.entity_ref for r in rows if r.kind == "execution" and r.payload["expected_usage"]}
    usage = {r.payload["execution_ref"] for r in rows if r.kind == "usage"}
    expected, observed = len(executions), len(executions & usage)
    status = "complete" if expected and expected == observed else "partial" if expected else "idle"
    reason = None
    missing = executions - usage
    if missing:
        states = {ref: set() for ref in missing}
        for row in rows:
            if row.kind == "execution" and row.entity_ref in states:
                states[row.entity_ref].add(row.payload["state"])
        # A dated historical gap cannot contain a dispatch that had not begun.
        # Keep every gap receipt and partial cost; only classify this remainder.
        starts = [r.occurred_at for r in rows if r.kind == "execution" and r.entity_ref in missing]
        earliest = min((parse_timestamp(at, "dispatch start") for at in starts), default=None) if all(starts) else None
        gap = any(r.kind == "coverage" and r.payload["status"] in {"partial", "unreachable"}
                  and (r.occurred_at is None or earliest is None
                       or parse_timestamp(r.occurred_at, "capture gap") >= earliest) for r in rows)
        if earliest is not None and not gap and all(values and values <= {"accepted", "running"} for values in states.values()):
            reason = "host_usage_pending"
    return expected, observed, status, reason


class _StageTimer:
    """Optional per-stage wall time for the caller's progress log; never facts."""
    def __init__(self, sink: dict[str, Any] | None):
        self.sink = sink
        self.at = perf_counter()

    def __call__(self, name: str) -> None:
        now = perf_counter()
        if self.sink is not None:
            stages = self.sink.setdefault("stages", {})
            stages[name] = round(stages.get(name, 0.0) + now - self.at, 3)
        self.at = now

    def source(self, kind: str, source_ref: str, read_seconds: float, total_seconds: float) -> None:
        if self.sink is not None:
            self.sink.setdefault("sources", []).append({"kind": kind, "source_ref": source_ref,
                "read_seconds": round(read_seconds, 3), "seconds": round(total_seconds, 3)})
        self.at = perf_counter()


COLLECTION_ATTEMPTS = 3


def _observed_files(observe: dict | None, source: Mapping[str, Any], files: list[Path]) -> list[Path]:
    """An observing run may read only some files of a source (its changed sessions)."""
    wanted = observe["files"].get(source["id"]) if observe is not None else None
    return files if wanted is None else [f for f in files if str(f) in wanted]


def _observed(observe: dict, batch: list[Receipt], root_paths: list[Path], root_entities: list[set[str]]) -> dict:
    """Finish an observing run: reconciled Host coverage, root links, this run's decided rows."""
    coverage = {r.payload["source_ref"]: r for r in batch if r.kind == "coverage"
                and r.producer == "registered_collector.v1" and r.payload.get("source_detail", {}).get("kind") == "relay_host"}
    for entry in observe["sources"]:
        head = coverage.get(entry["source_ref"])
        if head is not None:
            # reconcile_host_coverage may complete the Host denominator from this run's native inputs.
            entry["coverage"].update(status=head.payload["status"], observed=head.payload["observed"],
                                     reason=head.payload["reason"])
    if root_paths:
        from .codex_request_purpose import native_root_requests
        links = native_root_requests(root_paths)
        for entry in observe["sources"]:
            entities = {r.entity_ref for r in entry["rows"] if r.kind == "usage"}
            if entry["config"]["kind"] != "codex" or entities not in root_entities:
                continue
            # The same rule as a full collection: both usages read from this one source now.
            entry["root_links"] = {child: link for child, link in links.items() if {child, link["parent"]} <= entities}
    observe["decided"] = batch
    return observe


def collect_registered_sources(registry: Mapping[str, Any], ledger: UsageLedger, *, force_recompute: bool = False,
                               timings: dict[str, Any] | None = None,
                               retirements: Mapping[str, Mapping[str, str]] | None = None,
                               recoveries: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Read each configured source, preserve errors as independent coverage facts.

    Nodes append to one shared stream. When another node appended after this
    collection read it, nothing was written; collect again from the current
    stream, at most COLLECTION_ATTEMPTS times. Facts are content-addressed, so
    a repeated collection never counts a fact twice.
    ``timings`` optionally receives per-stage and per-source wall seconds.
    ``retirements`` defaults to the repository's retired source declaration;
    ``recoveries`` to its coverage recovery declaration.
    """
    from .ledger import SnapshotChanged
    from .source_retirement import load_retirements
    from .coverage_recovery import load_recoveries
    retirements = load_retirements() if retirements is None else retirements
    recoveries = load_recoveries() if recoveries is None else recoveries
    for attempt in range(1, COLLECTION_ATTEMPTS + 1):
        if timings is not None:
            timings["attempts"] = attempt
        try:
            return _collect_once(registry, ledger, force_recompute=force_recompute, timings=timings,
                                 retirements=retirements, recoveries=recoveries)
        except SnapshotChanged:
            if attempt == COLLECTION_ATTEMPTS:
                raise
    raise AssertionError("bounded collection loop exhausted")


def observe_registered_sources(registry: Mapping[str, Any], *, cache_root: Path, run: set[str],
                               facts: Iterable[Receipt] = (), host_observations: Mapping[str, list] | None = None,
                               files: Mapping[str, set[str]] | None = None, force_recompute: bool = False,
                               retirements: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, Any]:
    """Read sources without the observation stream; return what a judging collector needs.

    Only sources in ``run`` are read; ``files`` optionally narrows a source to
    some of its files. ``facts`` stand in for the stream where this node's own
    readers consult it (Host authorities and executors read earlier). Nothing
    is versioned against history, settled, attributed by stage or ingested:
    each read source yields its rows, coverage and the inputs its stages need.
    """
    from .source_retirement import load_retirements
    sink = {"facts": list(facts), "host_observations": dict(host_observations or {}), "files": dict(files or {}),
            "run": set(run), "cache_root": Path(cache_root), "sources": []}
    _collect_once(registry, None, force_recompute=force_recompute, timings=None,
                  retirements=load_retirements() if retirements is None else retirements, recoveries={}, observe=sink)
    return sink


def _collect_once(registry: Mapping[str, Any], ledger: UsageLedger | None, *, force_recompute: bool,
                  timings: dict[str, Any] | None,
                  retirements: Mapping[str, Mapping[str, str]],
                  recoveries: Mapping[str, Mapping[str, Any]], observe: dict | None = None) -> dict[str, Any]:
    if registry.get("schema") != REGISTRY_SCHEMA or not isinstance(registry.get("sources"), list):
        raise ReceiptError("invalid source registry")
    mark = _StageTimer(timings)
    now = utc_text(datetime.now(timezone.utc))
    snapshot = ledger.receipt_snapshot() if observe is None else {"facts": observe["facts"], "watermark": 0}
    mark("ledger_snapshot")
    from .native_parse_cache import NativeParseCache, input_signature
    native_cache = NativeParseCache((ledger.root if observe is None else observe["cache_root"]) / "native-cache",
                                    force=force_recompute)
    active, conflicts = current_receipts(snapshot["facts"])
    from .native_metering import metered_observation
    # The prior successful collector may already have appended a coarse copy
    # without an edge. Recover only this proven precision relationship; other
    # concurrent heads remain unresolved and must not acquire a chosen prior.
    active.extend(row for versions in conflicts.values()
                  if (row := metered_observation(versions)) is not None)
    prior_by_entity = {r.entity_ref: r for r in active}
    known_by_fact = {r.fact_ref: r for r in snapshot["facts"]}
    from .actor_identity import ActorHeads
    # Actor claims settle against current heads, old identity formula included.
    actor_heads = ActorHeads(prior_by_entity, [*active, *(r for rows in conflicts.values() for r in rows)])
    mark("current_receipts")
    batch, summaries, coverage_revision_conflicts = [], [], []
    recovered_coverage_entities: list[str] = []
    # Git workspace identity is a pure function of (root, cwd, descendants)
    # and current Git metadata. Check each pair once per collection, shared
    # by every source; never persisted across collections.
    workspace_cache: dict = {}
    cc_input_sources = []
    input_observations = dict(observe["host_observations"]) if observe is not None else {}
    runtime_launches = []
    runtime_root_paths = []
    runtime_root_source_entities = []
    native_request_inputs = []
    native_session_bindings = []
    cc_actor_context = None
    pi_session_requests = []
    # Holder supplement reads the core's session writes once per collection.
    holder = registry.get("core_session_writes")
    holder_sessions, holder_writes, holder_error, holder_created = [], None, False, None
    holder_scopes: set[str] = set()
    if holder is not None and "holder_supplement.v1" in registry.get("required_receipt_features", []):
        from .holder_supplement import read_creations, read_session_writes
        try:
            holder_namespace, holder_writes, holder_actors = read_session_writes(Path(holder["path"]))
            holder_created = read_creations(Path(holder["path"]))
        except (OSError, ValueError, TypeError, KeyError):
            holder_error = True
    authority_roots = []
    authority_error = False
    # A retired source is never read, even while a node still registers it.
    retired_registered = sorted(reference("source", s["id"]) for s in registry["sources"]
                                if isinstance(s, Mapping) and s.get("id") in retirements)
    for configured in registry["sources"]:
        if configured.get("kind") == "harness_root" and configured.get("id") not in retirements:
            try:
                authority_roots.extend(manifest_paths(configured) or [Path(configured["path"])])
            except (OSError, ValueError, TypeError, KeyError):
                authority_error = True
    try:
        bindings = execution_bindings(authority_roots)
    except (OSError, ValueError, TypeError, KeyError):
        bindings, authority_error = {}, True
    mark("execution_bindings")
    # Each spooled observation bundle stands in for the sources it carries.
    from .native_observations import commit_pushed, expand_pushed, source_heads
    pushed_sources = expand_pushed(registry["sources"], retirements)
    # Snapshot inputs are versioned against the stream here, where the stream is known.
    from .request_inputs import load_read_marks, save_read_marks, version_inputs
    read_marks_path = read_marks = None
    if pushed_sources["inputs"]:
        # The writer's latest read per snapshot key; it lives beside this ledger's caches.
        read_marks_path = ledger.root / "native-cache" / "input-read-marks.json"
        read_marks = load_read_marks(read_marks_path)
    batch.extend(version_inputs(pushed_sources["inputs"], snapshot["facts"], read_marks))
    summaries.extend(pushed_sources["summaries"])
    ids = set()
    for source in pushed_sources["sources"]:
        if not isinstance(source, Mapping) or not source.get("id") or source["id"] in ids:
            raise ReceiptError("source IDs must be present and unique")
        ids.add(source["id"])
        kind = source.get("kind")
        if kind not in {"relay_host", "pi", "cc", "codex", "harness_bundle", "harness_root", "harness_core", "receipts"}:
            raise ReceiptError("unsupported registered source kind")
        if source["id"] in retirements or (observe is not None and source["id"] not in observe["run"]):
            continue
        launches_start, request_inputs_start = len(runtime_launches), len(native_request_inputs)
        domain = "quality" if kind in {"harness_bundle", "harness_root", "harness_core"} else source.get("domain", "usage")
        source_ref = reference("source", source["id"])
        imported, raw_count, reason, watermark = [], 0, None, None
        observed_rows = None
        source_total_count = None
        source_latest_usage_at = None
        source_digests: list[str] = []
        native_diagnostics: list[str] = []
        session_binding = None
        cc_requests = None
        pi_requests = None
        pushed = source.get("_pushed")
        detail = source_detail(source) if pushed is None else dict(pushed["coverage"]["detail"])
        files = []
        inventories = []
        status, expected, observed = "unknown", None, None
        core_coverage = None
        source_started = perf_counter()
        try:
            if pushed is None:
                source = _native_principal_source(source, registry["sources"])
                if (source.get('verification_workspace_registry') is not None
                        and 'verification_workspace.v1' not in registry.get('required_receipt_features', [])):
                    raise ReceiptError('verification identity requires reader-first feature enrollment')
                resolved = codex_calendar_paths(source) or manifest_paths(source)
                if resolved is not None:
                    paths = resolved
                elif "paths" in source:
                    raw_paths = source["paths"]
                    if kind != "codex" or "path" in source or not isinstance(raw_paths, list) or not raw_paths or any(not isinstance(p, str) for p in raw_paths):
                        raise ReceiptError("only Codex accepts an explicit multi-root source")
                    paths = [Path(p) for p in raw_paths]
                else:
                    paths = [Path(source["path"])]
                for path in paths:
                    if path.is_symlink() or not path.exists():
                        raise OSError("registered source unavailable")
                path = paths[0]
            if pushed is not None:
                # Rows as the observing node's readers produced them, undecided.
                imported, inventories = list(pushed["rows"]), list(pushed["inventories"])
            elif kind == "harness_root":
                files = read_harness_root(path, now, imported, detail=detail,
                    include_outcomes="wi_outcome_context.v1" in registry.get("required_receipt_features", []))
                if detail["files_invalid"]:
                    native_diagnostics.append("harness_documents_incomplete")
                for f in files:
                    if f.is_symlink():
                        raise ReceiptError("source symlink is not accepted")
                    source_digests.append(_source_digest(f))
            elif kind == "harness_core":
                from .kernel_outcomes import read_kernel_outcomes
                if path.is_symlink() or not path.is_file():
                    raise OSError("unsafe source")
                files = [path]
                source_bytes = path.read_bytes()
                source_digests.append(_source_digest(path, source_bytes))
                core_coverage = read_kernel_outcomes(path, source_bytes, now, imported, detail)
            elif kind == "pi":
                from .pi_history import history_index, copy_receipts
                history = {}
                source_host_authorities = None
                source_native_bindings = None
                identity = source if source.get("actor_attribution") is True else None
                inputs_key = digest(input_signature(identity))
                files = _observed_files(observe, source,
                    sorted({f for root in paths for f in pi_files(root, source.get("layout"), detail)}))
                detail["files_seen"] = len(files)
                for f in files:
                    try:
                        if f.is_symlink() or not f.is_file():
                            raise OSError("unsafe source")
                        source_bytes = f.read_bytes()
                        source_digests.append(_source_digest(f, source_bytes))
                        detail["files_read"] += 1
                        # read_pi retains only the verified prefix when a writer
                        # is appending. Failure is local to this file, not the root.
                        before = len(imported)
                        inventory, native_history = native_cache.pi_read(f, source_bytes, identity,
                            inputs_key, source_ref, now, imported, read_pi)
                        inventories.append(inventory)
                        history[str(f)] = native_history
                        try:
                            # snapshot + batch is unchanged across this source's files.
                            # Resolve it once, never cache authority across collections.
                            if source_host_authorities is None and (f.parent / "compute-cost").is_dir():
                                source_host_authorities = _host_authorities(snapshot["facts"] + batch)
                            if source_native_bindings is None and (f.parent / "compute-cost").is_dir():
                                from .scl_session_attribution import read_native_bindings, pi_host_ref
                                selected = native_binding_source(source)
                                hosts = set()
                                for native_file in files:
                                    with native_file.open("rb") as handle:
                                        header = json.loads(handle.readline(2 * 1024 * 1024))
                                    if header.get("type") == "session" and isinstance(header.get("id"),str):
                                        hosts.add(pi_host_ref(header["id"]))
                                source_native_bindings = read_native_bindings(selected,hosts) if selected else {}
                            verified_boundaries = bind_pi_boundaries(f, source_bytes, imported[before:], bindings, now,
                                host_authorities=source_host_authorities, native_bindings=source_native_bindings,
                                registered_native_paths=files, diagnostics=native_diagnostics)
                            imported.extend(verified_boundaries)
                            from .pi_binding_alias import binding_alias_receipts
                            imported.extend(binding_alias_receipts(source_bytes, source_native_bindings or {}, now,
                                path=f, verified_boundaries=[*verified_boundaries, *active]))
                        except (OSError, ValueError, TypeError, KeyError, __import__("sqlite3").Error):
                            native_diagnostics.append("native_attribution_boundary_invalid")
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        category = ("source_unreadable" if isinstance(exc, OSError) else
                            "incomplete_final_record" if "incomplete final record" in str(exc) else
                            "native_protocol_mismatch" if "session header" in str(exc) else "invalid_native_record")
                        failure(detail, category)
                if "pi_parent_history.v1" in registry.get("required_receipt_features", []):
                    try:
                        imported.extend(copy_receipts(history, now))
                    except (OSError, ValueError, TypeError, KeyError):
                        native_diagnostics.append("pi_parent_history_invalid")
                if detail["files_invalid"]:
                    native_diagnostics.append("native_files_incomplete")
                if authority_error:
                    native_diagnostics.append("authority_binding_unavailable")
                if source.get("session_wi_mcp_since") is not None:
                    from .pi_request_purpose import request_bindings as pi_request_bindings
                    pi_requests = pi_request_bindings(files, since=source["session_wi_mcp_since"],
                                                      diagnostics=native_diagnostics)
                if source.get("actor_attribution") is True:
                    header_actors = {}
                    for file in files:
                        with file.open("rb") as handle:
                            header = json.loads(handle.readline(2 * 1024 * 1024))
                        if header.get("type") != "session" or not isinstance(header.get("id"), str):
                            continue
                        scope = reference("native_session", ["pi", header["id"]])
                        actor = registered_workspace_actor(source, header.get("cwd"), "pi", workspace_cache=workspace_cache)
                        if actor["executor"] is not None:
                            header_actors.setdefault(scope, []).append((actor, reference("native_startup", digest(header))))
                    for row in tuple(imported):
                        if row.kind != "usage":
                            continue
                        for actor, proof in header_actors.get(row.payload.get("native_scope_ref"), []):
                            imported.append(actor_receipt(row, **actor, observed=now, evidence=(proof, source_ref),
                                                          path=["pi_workspace", source["id"]]))
                    try:
                        from .scl_session_attribution import attribution_receipts, pi_host_ref
                        selected_source = native_binding_source(source)
                        if selected_source:
                            hosts = {}
                            for f in files:
                                with f.open("rb") as handle:
                                    header = json.loads(handle.readline(2 * 1024 * 1024))
                                if header.get("type") == "session" and isinstance(header.get("id"), str):
                                    scope = reference("native_session", ["pi", header["id"]])
                                    hosts[scope] = pi_host_ref(header["id"])
                            targets = {r.entity_ref: hosts[r.payload["native_scope_ref"]]
                                for r in imported if r.kind == "usage" and r.payload.get("native_scope_ref") in hosts}
                            linked = attribution_receipts(imported, targets, selected_source, now)
                            existing = {}
                            for r in active + imported:
                                if r.kind == "attribution" and r.payload.get("scope") != "actor" and r.payload["state"] == "assigned":
                                    ap = r.payload
                                    existing.setdefault(ap["target_ref"], set()).add((ap["wi"], ap["executor"]))
                            for r in linked:
                                ap = r.payload
                                # An existing exact boundary with the same answer
                                # is already sufficient; do not create two heads.
                                if ap["state"] == "assigned" and existing.get(ap["target_ref"]) == {(ap["wi"], ap["executor"])}:
                                    continue
                                imported.append(r)
                    except (OSError, ValueError, TypeError, KeyError, __import__('sqlite3').Error):
                        native_diagnostics.append("scl_session_binding_unavailable")
            elif kind == "codex":
                # Active/archive copies enter the same canonical adapter.
                imported = read_codex(paths, now, diagnostic_out=native_diagnostics,
                    source_identity=source if source.get("actor_attribution") is True else None,
                    native_cache=native_cache, workspace_cache=workspace_cache,
                    metering="native_metering.v1" in registry.get("required_receipt_features", []))
                files = sorted({f for root in paths for f in (root.rglob("*.jsonl") if root.is_dir() else [root])})
                if source.get("request_purpose_directory") is not None:
                    from .native_request_purpose import read_inputs
                    native_request_inputs.extend(read_inputs(Path(source["request_purpose_directory"]), imported, paths=files))
                if source.get("session_wi_mcp_since") is not None:
                    from .mcp_request_purpose import native_completions
                    session_binding = native_completions(files, imported, native_diagnostics, since=source["session_wi_mcp_since"])
                for f in files:
                    if f.is_symlink():
                        raise ReceiptError("source symlink is not accepted")
                    source_digests.append(_source_digest(f))
            else:
                cc_bound_sessions: list[tuple[Path, bytes]] = []
                cc_input_prefixes = []
                cc_actor_workspaces = workspace_cache  # Once per collection, never persisted.
                cc_actor_targets: dict = {}  # This source's append-only `imported`.
                host_entry_files = []
                files = _observed_files(observe, source,
                    sorted(path.rglob("*.json" if kind == "harness_bundle" else "*.jsonl")) if path.is_dir() else [path])
                for f in files:
                    if f.is_symlink():
                        raise ReceiptError("source symlink is not accepted")
                    source_bytes = f.read_bytes()
                    source_digests.append(_source_digest(f, source_bytes))
                    if kind == "relay_host":
                        try:
                            native_cache.receipts(read_host, f, now, source_bytes, imported)
                            host_entry_files.append(f)
                        except (ValueError, TypeError, KeyError):
                            native_diagnostics.append("host_stream_invalid")
                            detail["files_invalid"] += 1
                            detail["errors"]["invalid_native_record"] = detail["errors"].get("invalid_native_record", 0) + 1
                            continue
                    elif kind == "pi":
                        read_pi(f, now, into=imported, data=source_bytes)
                    elif kind == "cc":
                        try:
                            native_cache.receipts(read_cc, f, now, source_bytes, imported)
                        except (ValueError, TypeError, KeyError) as exc:
                            # One damaged transcript keeps its verified prefix and
                            # loses only its own identity joins this collection.
                            failure(detail, "incomplete_final_record" if "incomplete final record" in str(exc)
                                    else "invalid_native_record")
                            native_diagnostics.append("native_files_incomplete")
                            continue
                        cc_input_prefixes.append((f, source_bytes))
                        if source.get('actor_attribution') is True:
                            if cc_actor_context is None:
                                # Host facts are complete once the relay_host source
                                # has been read; one index serves every CC source.
                                cc_actor_context = (relay_cc_executors(active + batch),
                                                    _cc_prior_actors(active))
                            imported.extend(cc_workspace_actor_receipts(f, source_bytes, source, imported, now,
                                workspace_cache=cc_actor_workspaces, relay_executors=cc_actor_context[0],
                                prior_actors=cc_actor_context[1], targets_cache=cc_actor_targets))
                        if (f.parent / "compute-cost").is_dir():
                            cc_bound_sessions.append((f, source_bytes))
                    elif kind == "harness_bundle":
                        imported.extend(import_harness_bundle(json.loads(source_bytes), now))
                    elif kind == "receipts":
                        rows, total_count, latest = native_cache.receipt_bundle(
                            f, source_bytes, ledger_watermark=snapshot["watermark"],
                            known_fact_refs=set(known_by_fact) | {receipt.fact_ref for receipt in batch})
                        imported.extend(rows)
                        source_total_count = (source_total_count or 0) + total_count
                        if latest and (source_latest_usage_at is None
                                or parse_timestamp(latest, "usage time") > parse_timestamp(source_latest_usage_at, "usage time")):
                            source_latest_usage_at = latest
                    else:
                        imported.extend(Receipt.from_dict(r) for r in _json_lines(f, source_bytes))
                if cc_bound_sessions:
                    # Relay 绑定的 CC 有界快照：与 Pi 同一 Host 授权 + 字节前缀证据链。
                    # 子代理 transcript 全部读入后再绑定，快照覆盖主会话及其子代理。
                    cc_host_authorities = _host_authorities(snapshot["facts"] + batch, "cc")
                    for f, source_bytes in cc_bound_sessions:
                        scope = reference("native_session", ["cc", f.stem])
                        session_rows = [r for r in imported
                                        if r.kind == "usage" and r.payload.get("native_scope_ref") == scope]
                        try:
                            imported.extend(bind_cc_boundaries(f, source_bytes, session_rows, now,
                                host_authorities=cc_host_authorities))
                        except (OSError, ValueError, TypeError, KeyError):
                            native_diagnostics.append("native_attribution_boundary_invalid")
                if kind == "cc" and source.get("session_wi_mcp_since") is not None:
                    from .mcp_request_purpose import cc_native_completions
                    session_binding = cc_native_completions(files, imported, native_diagnostics, since=source["session_wi_mcp_since"])
                    from .cc_request_purpose import request_bindings
                    cc_requests = request_bindings(files, imported, session_binding, native_diagnostics)
            if pushed is None and kind == "cc" and source.get("actor_attribution") is True:
                cc_input_sources.append((source, cc_input_prefixes, list(imported)))
            if kind == "codex" and source.get("actor_attribution") is True and source.get("scl_kernel_root"):
                runtime_root_paths.extend(files)
                runtime_root_source_entities.append({r.entity_ref for r in imported if r.kind == "usage"})
            if "runtime_subcall.v1" in registry.get("required_receipt_features", []) and kind in {"codex", "cc"}:
                from .runtime_subcalls import native_launches, cc_native_launches
                reader = native_launches if kind == "codex" else cc_native_launches
                for native_file in files:
                    # One unreadable transcript loses only its own launch links,
                    # never the source's already verified session bindings.
                    try:
                        runtime_launches.extend(reader(native_file, source))
                    except (OSError, ValueError, TypeError, KeyError):
                        native_diagnostics.append("runtime_subcall_file_invalid")
            if pushed is None and source.get("actor_attribution") is True and kind == "relay_host" and source.get("human_input_directory"):
                try:
                    imported.extend(host_entrypoint_receipts(source, host_entry_files, imported, now))
                except (OSError, ValueError, TypeError, KeyError):
                    native_diagnostics.append("entrypoint_evidence_unavailable")
            principal_corrections: list[Receipt] = []
            # An observing node sends rows as read; the judging collector corrects and settles them.
            observed_rows = list(imported)
            if observe is None and kind == "relay_host" and source.get("principal_alias_history"):
                source_entities = {row.entity_ref for row in imported
                                   if row.kind in {"usage", "execution", "attribution", "human"}}
                if pushed is not None:
                    # A bundle carries changed sessions only; history keeps its witness.
                    source_entities |= source_heads(active, source_ref)
                principal_corrections = _principal_head_corrections(
                    active, source_entities, source["principal_alias_history"], now)
                imported.extend(principal_corrections)
            if observe is None and kind == "pi" and source.get("principal_alias_history"):
                imported.extend(_input_actor_alias_corrections(active,
                    {row.entity_ref for row in imported if row.kind == "usage"}
                    | (source_heads(active, source_ref) if pushed is not None else set()),
                    source["principal_alias_history"], now))
            if observe is None and source.get("actor_attribution") is True and kind in {"pi", "cc", "relay_host"}:
                actor_rows = [row for row in imported if row.kind in {"usage", "execution"}]
                if kind == "relay_host" and principal_corrections:
                    corrected = {row.entity_ref: row for row in principal_corrections
                                 if row.kind in {"usage", "execution"}}
                    raw_active, _ = current_receipts(
                        [row for row in actor_rows if row.entity_ref not in corrected])
                    actor_targets = tuple(raw_active) + tuple(corrected.values())
                    for target in corrected.values():
                        imported.extend(_actor_principal_corrections(
                            active, target, source["principal_alias_history"], now))
                else:
                    actor_targets = tuple(actor_rows)
                imported.extend(actor_receipt(row, node=source.get("node"),
                    executor=row.payload.get("executor"), observed=now,
                    evidence=(source_ref,), path=["source_executor", source["id"]])
                    for row in actor_targets)
            watermark = digest(sorted(source_digests)) if source_digests else None
            raw_count = len(imported)
            if pushed is not None:
                # Measured over every file of the source on the observing node.
                coverage = pushed["coverage"]
                status, reason, expected, observed = (coverage["status"], coverage["reason"],
                                                      coverage["expected"], coverage["observed"])
                watermark = coverage["watermark"]
                native_diagnostics.extend(d for d, n in coverage["diagnostics"].items() for _ in range(n))
            elif kind == "relay_host":
                expected, observed, status, reason = host_usage_coverage(imported)
            elif not files:
                status = "idle"
            elif kind == "harness_core":
                # The core's own completion list is the denominator of its Outcomes.
                status, reason, expected, observed = core_coverage
            elif kind == "receipts":
                # Coverage in a receipt bundle is already an assertion with
                # its own denominator. Report transport health, but do not mint
                # a second synthetic complete (or successful unknown).
                status = "complete"
            else:
                # Native logs do not provide an independent dispatch denominator.
                status, reason = "unknown", "execution_inventory_not_observed"
            degrading = [d for d in native_diagnostics if d not in LOCAL_DIAGNOSTICS]
            if degrading and pushed is None:
                status, reason = "partial", degrading[0]
        except (OSError, ValueError, TypeError, KeyError, ReceiptError, AdapterError) as exc:
            session_binding = None
            # Keep already verified facts from earlier files. Never publish raw exception text/paths.
            status, reason = "unreachable" if isinstance(exc, OSError) else "partial", "source_unreadable_or_invalid"
        source_read = perf_counter() - source_started
        native_times = [r.occurred_at for r in imported if r.kind == "usage" and r.payload["mapping"] != "pi_bounded_native.v1"
                        and r.occurred_at and (r.payload.get("reported_total_tokens") is not None
                        or any(v is not None for v in r.payload["tokens"].values()))]
        if pushed is None:
            detail["latest_usage_at"] = (source_latest_usage_at if kind == "receipts" else
                max(native_times, key=lambda t: parse_timestamp(t, "usage time")) if native_times else None)
        if pushed is None and kind not in {"pi", "harness_root", "harness_core"}:
            detail["files_seen"] = len(files)
            detail["files_read"] = len(source_digests)
        raw_count = (pushed["coverage"]["raw_count"] if pushed is not None else
                     source_total_count if source_total_count is not None else len(imported))
        source_batch_start = len(batch)
        if observe is not None:
            from .native_observations import bundle_config
            observe["sources"].append({"source_ref": source_ref, "config": bundle_config(source),
                "rows": observed_rows if observed_rows is not None else list(imported),
                "inventories": list(inventories), "appended": [], "digests": sorted(source_digests),
                "files": [str(f) for f in files],
                "coverage": {"status": status, "reason": reason, "expected": expected, "observed": observed,
                             "watermark": watermark, "raw_count": raw_count, "detail": dict(detail),
                             "diagnostics": dict(sorted(Counter(native_diagnostics).items()))},
                "launches": runtime_launches[launches_start:],
                "request_inputs": native_request_inputs[request_inputs_start:],
                "session_binding": session_binding, "cc_requests": cc_requests, "pi_requests": pi_requests})
        actor_claims: list[Receipt] = []
        if pushed is None and kind in {'pi', 'relay_host'} and source.get('host_native_input') is True:
            # Current readback, including known rows skipped by incremental ingest.
            # Never reuse old input claims when this collection could not read them.
            input_observations[source_ref] = list(imported)
        for row in imported:
            if kind == "receipts":
                exact = known_by_fact.get(row.fact_ref)
                if exact is not None and exact.semantic_key() == row.semantic_key() and set(row.evidence) <= set(exact.evidence):
                    # A receipt transport is already an immutable version graph.
                    # Known historical facts need not pass through ingest again.
                    continue
                # This is an already-versioned source graph, not a raw snapshot.
                # Same payload is NOT the same fact: supersedes participates in
                # fact_ref. Replacing a source ancestor strands its descendants.
                batch.append(row)
                continue
            if kind == "relay_host" and source.get("principal_alias_history"):
                exact = known_by_fact.get(row.fact_ref)
                if (exact is not None and exact.semantic_key() == row.semantic_key()
                        and set(row.evidence) <= set(exact.evidence)):
                    # A retained raw ancestor is not a new contrary assertion.
                    # Replaying it must not undo a proven principal correction.
                    continue
                payload = row.payload
                effective = (payload.get("interval_start") or payload.get("effective_at")
                             or row.occurred_at)
                principal, proof = _relay_principal(
                    source["principal_alias_history"], payload.get("executor"), effective)
                if proof:
                    # Normalize new observations at the same collector boundary.
                    # Existing lifecycle/refinement checks below still control
                    # state/quantity progression and reparent the version chain.
                    row = Receipt.make(row.kind, None, {**payload, "executor": principal},
                        occurred_at=row.occurred_at, observed_at=row.observed_at,
                        producer=row.producer, evidence=tuple(sorted(set(row.evidence) | set(proof))),
                        entity_ref=row.entity_ref, supersedes=row.supersedes)
            if kind != "receipts" and actor_heads.pending(row):
                # Settled after this source's versions are decided, against
                # the target's current version rather than this parse.
                actor_claims.append(row)
                continue
            prior = prior_by_entity.get(row.entity_ref)
            # A later reread of the same weak session inference cannot
            # undo an explicit declaration for this exact native input.
            # Strong task claims remain independent and conflict normally.
            if (prior is not None and kind == "codex" and row.kind == "attribution"
                    and prior.producer == "codex_request_purpose.v1"
                    and row.producer == "scl_host_session_binding.v1"
                    and row.payload.get("state") == "ambiguous"
                    and row.payload.get("target_ref") == prior.payload.get("target_ref")
                    and all(row.payload.get(k) is None for k in
                            ("wi", "executor", "responsible", "delegator", "assignment_ref", "execution_ref", "scope", "work_kind", "context"))):
                continue
            if (prior and kind == "relay_host" and source.get("principal_alias_history")
                    and _principal_refinement(prior, row, source["principal_alias_history"])):
                row = Receipt.make(row.kind, None, row.payload,
                    occurred_at=row.occurred_at, observed_at=row.observed_at,
                    producer=row.producer, evidence=tuple(sorted(set(prior.evidence) | set(row.evidence))),
                    entity_ref=row.entity_ref, supersedes=prior.fact_ref)
            # A review-only placeholder explicitly asserted no delivered status.
            # The canonical WI Outcome supplies that different evidence domain;
            # replacing this placeholder never transfers its old approval to the
            # canonical candidate. Original review facts remain independently keyed.
            if prior and kind == "harness_root" and row.kind == prior.kind == "delivery":
                proxy = lambda r: r.producer == "harness_review.v1" and r.payload["status"] == "unknown"
                canonical_outcome = lambda r: r.producer == "canonical_outcome.v1" and r.payload.get("outcome_context") is not None
                same_wi = row.payload["wi"] == prior.payload["wi"]
                if same_wi and canonical_outcome(prior) and proxy(row):
                    continue  # A later re-read of a review is not an Outcome withdrawal.
                if same_wi and canonical_outcome(row) and proxy(prior):
                    row = Receipt.make(row.kind, None, row.payload, entity_ref=row.entity_ref,
                        supersedes=prior.fact_ref, occurred_at=row.occurred_at,
                        observed_at=row.observed_at, producer=row.producer,
                        evidence=tuple(sorted(set(row.evidence) | {prior.fact_ref})))
            # A refreshed snapshot of the SAME reviewed candidate may gain measurement fields.
            # A new candidate requires explicit supersession; never approve it using an old review.
            if (prior and row.kind == prior.kind and row.payload == prior.payload
                    and row.occurred_at == prior.occurred_at and row.producer == prior.producer):
                row = replace(prior, evidence=tuple(sorted(set(prior.evidence) | set(row.evidence))))
            if prior and row.kind == "execution_expectation" and prior.payload != row.payload:
                old, new = prior.payload, row.payload
                stable = all(old[k] == new[k] for k in ("wi", "executor", "runtime", "scope", "source_ref", "started_at"))
                # This is a current SOURCE AVAILABILITY observation, not a
                # mutation of the sealed artifact or incurred usage. A later
                # deletion must surface missing rather than silently reusing
                # the old green readback; recorded-at retains both views.
                refined = (old["status"] != new["status"]
                           and (old["ended_at"] is None or old["ended_at"] == new["ended_at"]))
                if stable and refined:
                    row = Receipt.make(row.kind, None, row.payload, occurred_at=row.occurred_at,
                        observed_at=row.observed_at, producer=row.producer, evidence=row.evidence,
                        entity_ref=row.entity_ref, supersedes=prior.fact_ref)
            if prior and kind in {"harness_bundle", "harness_root"} and row.kind in {"human_summary", "delivery", "review"} and prior.payload != row.payload:
                # Supplemental measurement can refine the exact reviewed candidate,
                # but never silently replace a different artifact/candidate.
                stable = (row.kind == "human_summary" or
                    (row.kind == "delivery" and all(prior.payload[k] == row.payload[k] for k in ("wi", "candidate_ref", "artifact_ref", "acceptance_ref"))) or
                    (row.kind == "review" and all(prior.payload[k] == row.payload[k] for k in ("wi", "delivery_ref", "candidate_ref", "reviewer", "decision"))))
                # Measurements may only fill explicit unknowns. A different
                # known quality value remains a concurrent conflict.
                refinement = all(prior.payload.get(key) == row.payload[key] or prior.payload.get(key) is None
                                 for key in row.payload)
                if stable and refinement:
                    row = Receipt.make(row.kind, None, row.payload, occurred_at=row.occurred_at,
                        observed_at=row.observed_at, producer=row.producer,
                        evidence=tuple(sorted(set(prior.evidence) | set(row.evidence))), entity_ref=row.entity_ref, supersedes=prior.fact_ref)
            if prior and kind == "codex" and row.kind == "usage":
                row = paginated_counter_correction(prior, row) or row
            if prior and kind in {"pi", "cc", "codex", "relay_host"} and row.kind in {"usage", "execution"} and prior.payload != row.payload and row.supersedes != prior.fact_ref:
                revised = [prior]
                _revision(revised, row)
                if len(revised) == 1:
                    # An obsolete source copy does not become evidence for the
                    # newer assertion that it does not actually contain.
                    continue
                row = revised[-1]
            batch.append(row)
            actor_heads.note(row)
            if prior is None or row.supersedes == prior.fact_ref or row.semantic() == prior.semantic():
                prior_by_entity[row.entity_ref] = row
        batch.extend(actor_heads.settle_all(actor_claims, prior_by_entity))
        if pushed is not None:
            batch.extend(pushed["appended"])
        if kind == 'codex' and source.get('verification_workspace_registry'):
            from .verification_workspace import reanchor_receipts
            batch[source_batch_start:] = reanchor_receipts(batch[source_batch_start:], imported)
        for inventory in inventories:
            prior = prior_by_entity.get(inventory.entity_ref)
            if prior and prior.payload == inventory.payload:
                inventory = prior
            elif prior:
                inventory = Receipt.make(inventory.kind, None, inventory.payload, occurred_at=None,
                    observed_at=now, producer=inventory.producer, entity_ref=inventory.entity_ref, supersedes=prior.fact_ref,
                    evidence=inventory.evidence)
            batch.append(inventory)
            prior_by_entity[inventory.entity_ref] = inventory
        # A composite Harness source supplies several independent domains.
        # Its failure must not leave old execution/value facts looking complete
        # merely because the registry historically called it a quality source.
        domains = ("execution", "human", "quality", "value") if kind == "harness_root" else (
            ("human", "quality", "value") if kind == "harness_bundle" else (domain,))
        # Published source identity + immutable read digest, never a filesystem path.
        if imported and kind in {"pi", "codex"}:
            batch[source_batch_start:] = [r if r.producer in {"pi_native_inventory.v1", "pi_parent_history.v1"} else
                replace(r, evidence=tuple(sorted(set(r.evidence) | {source_ref}))) for r in batch[source_batch_start:]]
        if watermark and imported and kind not in {"receipts", "pi", "codex"}:
            # Transport carries source provenance unchanged. A new bundle digest
            # must not manufacture evidence revisions for every historical fact.
            origin = reference("source_snapshot", [source_ref, watermark])
            # A changed source snapshot is not new evidence for every unchanged
            # historical assertion. Keep its first byte-snapshot witness; the
            # coverage row below records today's full source fingerprint.
            batch[source_batch_start:] = [replace(r, evidence=tuple(sorted(set(r.evidence) | {source_ref}
                | ({origin} if r.fact_ref not in known_by_fact else set()))))
                for r in batch[source_batch_start:]]
        # Explicit host configuration bounds the trusted impact. Unscoped
        # sources retain the historical global-unknown behavior. Receipt
        # bundles transport their own current coverage on success.
        configured_wis = source.get("work_items")
        if configured_wis is not None and (not isinstance(configured_wis, list) or
                                           any(not isinstance(wi, str) for wi in configured_wis)):
            raise ReceiptError("source work_items must be a string list")
        coverage_wis = configured_wis if configured_wis is not None else [None]
        delegated_transport = kind == "receipts" and reason is None
        for covered_domain in domains:
            for covered_wi in coverage_wis:
                identity = [source_ref, covered_domain, covered_wi or "global"]
                if delegated_transport and reference("coverage", identity) not in prior_by_entity:
                    continue
                # Retire a previous transport failure on recovery without making
                # up a business denominator. Imported coverage still must prove it.
                coverage = Receipt.make("coverage", identity, {"domain": covered_domain, "wi": covered_wi,
                    "source_ref": source_ref, "status": "excluded" if delegated_transport else status,
                    "expected": None if delegated_transport else expected,
                    "observed": None if delegated_transport else observed,
                    "watermark": watermark, "reason": "transport_recovered_coverage_delegated" if delegated_transport else reason,
                    "source_detail": detail},
                    occurred_at=None, observed_at=now, producer="registered_collector.v1")
                recovered = (recovered_coverage(coverage, conflicts[coverage.entity_ref], recoveries)
                             if coverage.entity_ref in conflicts else None)
                if recovered is not None:
                    # A declared non-assertion head is named, not chosen: the
                    # fresh snapshot succeeds the one remaining head.
                    batch.append(recovered)
                    recovered_coverage_entities.append(coverage.entity_ref)
                    continue
                if coverage.entity_ref in conflicts:
                    # A fresh snapshot cannot select one concurrent head as prior.
                    # Report its current state without growing the unresolved set.
                    coverage_revision_conflicts.append({
                        "entity_ref": coverage.entity_ref, "source_ref": source_ref,
                        "domain": covered_domain, "wi": covered_wi,
                        "head_fact_refs": sorted(r.fact_ref for r in conflicts[coverage.entity_ref]),
                        "reason": "coverage_revision_conflict", "recovery_required": True,
                        "fresh_snapshot": coverage.payload})
                    continue
                prior = prior_by_entity.get(coverage.entity_ref)
                if prior and prior.payload == coverage.payload:
                    coverage = prior
                elif prior:
                    coverage = Receipt.make("coverage", None, coverage.payload, occurred_at=None, observed_at=now,
                        producer=coverage.producer, entity_ref=coverage.entity_ref, supersedes=prior.fact_ref)
                batch.append(coverage)
        if session_binding is not None:
            native_session_bindings.append((list(imported), session_binding, cc_requests))
        if kind in {"cc", "codex", "pi"} and (holder_writes is not None or holder_error):
            from .holder_supplement import held_key, held_sessions, session_candidates
            try:
                if holder_error:
                    raise ReceiptError("core session writes unavailable")
                candidates = session_candidates(kind, files, holder_namespace, holder_actors, holder["surface"],
                                                wanted=(lambda keys: True) if observe is not None else
                                                (lambda keys: held_key(keys, holder_writes) is not None))
                if observe is not None:
                    observe["sources"][-1]["holder"] = candidates
                # Every session read here, held or not, is decided here.
                holder_scopes.update(c["scope"] for c in candidates)
                holder_sessions.extend(held_sessions(candidates, holder_writes))
            except (OSError, ValueError, TypeError, KeyError):
                native_diagnostics.append("holder_supplement_unavailable")
        if pi_requests:
            pi_session_requests.append((list(imported), pi_requests))
        summaries.append({"source_ref": source_ref, "kind": kind, "status": status, "fact_count": raw_count, "reason": reason, "source_detail": detail,
                          "diagnostics": dict(sorted(Counter(native_diagnostics).items()))})
        recovery = [r for r in coverage_revision_conflicts if r["source_ref"] == source_ref]
        if recovery:
            summaries[-1].update(coverage_recovery_required=True,
                                 coverage_revision_conflicts=recovery)
        mark.source(kind, source_ref, source_read, perf_counter() - source_started)
    if cc_input_sources:
        from .cc_input_context import cc_input_actor_receipts
        host_facts = [r for observed_rows in input_observations.values() for r in observed_rows]
        for cc_source, prefixes, usages in cc_input_sources:
            rows = cc_input_actor_receipts(prefixes, cc_source, registry["sources"],
                                           host_facts, usages, now, workspace_cache=workspace_cache)
            batch.extend(rows)
            if observe is not None:
                next(e for e in observe["sources"] if e["config"]["id"] == cc_source["id"])["appended"].extend(rows)
    mark("cc_input_actor_receipts")
    from .host_native_input import reconcile_host_coverage
    reconcile_host_coverage(batch, summaries, now, input_observations, active)
    mark("reconcile_host_coverage")
    if observe is not None:
        observe["host_observations"] = input_observations
        return _observed(observe, batch, runtime_root_paths, runtime_root_source_entities)
    from .activity_classification import collect_reviewed_activity
    batch.extend(actor_heads.settle_all(collect_reviewed_activity(snapshot["facts"] + batch, registry, now)))
    mark("collect_reviewed_activity")
    if "runtime_subcall.v1" in registry.get("required_receipt_features", []):
        from .runtime_subcalls import launch_attributions
        from .native_inputs import merge_launches
        # Launches another node read from its own transcripts and recorded as inputs.
        runtime_launches = merge_launches(runtime_launches, snapshot["facts"] + batch)
        batch.extend(actor_heads.settle_all(launch_attributions(snapshot["facts"] + batch, runtime_launches, now)))
    mark("launch_attributions")
    # Request inputs another node recorded, for keys this collection did not read.
    from .request_inputs import recorded_requests
    # Inputs and usage versions this collection already decided count this round.
    current_heads = list(prior_by_entity.values())
    recorded = recorded_requests(snapshot["facts"] + batch, current_heads, conflicts,
        codex_targets={target for target, _, _ in native_request_inputs},
        session_scopes={scope for _, binding, _ in native_session_bindings for scope in binding},
        cc_usages={ref for _, _, requests in native_session_bindings for ref in (requests or {})},
        pi_usages={reference("usage", list(key)) for _, requests in pi_session_requests for key in requests})
    native_request_inputs.extend(recorded["codex"])
    if recorded["pi"]:
        pi_session_requests.append((recorded["usages"], recorded["pi"]))
    if recorded["sessions"] or recorded["cc"]:
        native_session_bindings.append((recorded["usages"], recorded["sessions"], recorded["cc"] or None))
    mark("recorded_request_inputs")
    if native_request_inputs:
        from .native_request_purpose import purpose_attributions
        batch.extend(purpose_attributions(snapshot["facts"] + batch, native_request_inputs, now))
    mark("purpose_attributions")
    for usages, pi_requests in pi_session_requests:
        from .pi_request_purpose import request_attributions as pi_request_attributions
        batch.extend(pi_request_attributions(snapshot["facts"] + batch, usages, pi_requests, now))
    mark("pi_request_attributions")
    child_work_items = None
    for usages, session_binding, cc_requests in native_session_bindings:
        from .mcp_request_purpose import session_attributions
        if cc_requests:
            from .cc_request_purpose import request_attributions
            if child_work_items is None:
                from .child_work_items import read_children
                child_work_items = read_children(registry)
            batch.extend(request_attributions(snapshot["facts"] + batch, usages, cc_requests, session_binding, now,
                                              children=child_work_items))
            mark("cc_request_attributions")
        legacy_usages = [r for r in usages if r.entity_ref not in (cc_requests or {})]
        batch.extend(session_attributions(snapshot["facts"] + batch, legacy_usages, session_binding, now))
        mark("session_attributions")
    if "runtime_subcall.v1" in registry.get("required_receipt_features", []):
        # After request purposes: a CC child spends its launching usage's WI.
        from .runtime_subcalls import subcall_purpose_attributions
        batch.extend(subcall_purpose_attributions(snapshot["facts"] + batch, runtime_launches, now))
    mark("subcall_purpose_attributions")
    from .codex_request_purpose import native_root_requests, root_request_attributions
    from .request_inputs import recorded_root_links
    links = {}
    if runtime_root_paths:
        links = native_root_requests(runtime_root_paths)
        # Both native observations must actually have been read from the same
        # enrolled source in this collection. A remote/historical parent alone
        # cannot lend its task identity. Existing contrary reviews stay visible.
        links = {child: link for child, link in links.items()
                 if any({child, link["parent"]} <= entities for entities in runtime_root_source_entities)}
    # A link another node recorded counts when both current usages carry its source.
    # Children whose usage this collection read itself are decided by its own links.
    recorded_links = recorded_root_links(snapshot["facts"] + batch, list(prior_by_entity.values()),
                                         set(links).union(*runtime_root_source_entities))
    if runtime_root_paths or recorded_links:
        batch.extend(root_request_attributions(snapshot["facts"] + batch, {**recorded_links, **links}, now))
    mark("root_request_attributions")
    if holder_writes is not None:
        # Sessions another node recorded with their candidate keys; this export decides.
        from .holder_supplement import held_sessions
        from .request_inputs import recorded_holder_candidates
        holder_sessions.extend(held_sessions(recorded_holder_candidates(
            snapshot["facts"] + batch, holder_scopes), holder_writes))
    if holder_sessions:
        from .holder_supplement import supplement_attributions
        batch.extend(supplement_attributions(snapshot["facts"] + batch, holder_sessions, holder_writes, now,
                                             created=holder_created))
    mark("holder_supplement_attributions")
    from .turn_residual_purpose import turn_residual_receipts
    batch.extend(turn_residual_receipts(snapshot["facts"] + batch, now))
    mark("turn_residual_purpose")
    from .standing_non_wi import standing_non_wi_receipts
    batch.extend(standing_non_wi_receipts(snapshot["facts"] + batch, now))
    mark("standing_non_wi")
    from .tenant_context import collect_tenant_context
    batch.extend(actor_heads.settle_all(collect_tenant_context(snapshot["facts"] + batch, registry, now,
                                                               ledger_refs=known_by_fact)))
    mark("collect_tenant_context")
    from .source_retirement import retirement_receipts
    retired = retirement_receipts(active, conflicts, retirements, now)
    batch.extend(retired)
    mark("source_retirements")
    result = ledger.ingest_receipts(batch, expected_watermark=snapshot["watermark"])
    mark("ingest_receipts")
    # A bundle cursor means its omitted prefix is durable in this ledger. If
    # ingest fails, no persistent cursor advances and the next run reparses.
    native_cache.commit_receipt_bundles(result["watermark"])
    if read_marks is not None:
        save_read_marks(read_marks_path, read_marks)  # Only with the ingest that decided against them.
    commit_pushed(pushed_sources["paths"])
    mark("commit_receipt_bundles")
    if timings is not None:
        timings["batch_facts"] = len(batch)
    return {"schema": "computecost_collection/v1", **result, "sources": summaries,
            "parser_cache": native_cache.summary(),
            "coverage_revision_conflicts": coverage_revision_conflicts,
            "recovered_coverage_entities": sorted(recovered_coverage_entities),
            "retired_sources": {"registered_source_refs": retired_registered,
                                "retired_coverage_entities": sorted(r.entity_ref for r in retired)}}
