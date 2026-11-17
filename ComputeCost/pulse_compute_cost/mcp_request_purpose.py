"""Register a native session's WI once; collector remains the only ledger writer.

The Agent supplies one WI. The Host supplies session identity: Codex through
request metadata, CC through the sessionId of its own transcript record. Raw
usage, saved prices, independent claims and earlier ledger watermarks are
never rewritten.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from .events import parse_timestamp
from .native_request_purpose import WI
from .receipts import Receipt, ReceiptError, current_receipts, digest, reference

TOOL = "declare_session_wi"
SERVER = "pulse-request-purpose"
TRANSPORT_SCHEMA = "codex_session_wi/v1"
CC_TRANSPORT_SCHEMA = "cc_session_wi/v1"
CC_REQUEST_TRANSPORT_SCHEMA = "cc_request_wi/v1"
# Claude Code names an MCP tool mcp__<server>__<tool> in its transcript.
CC_TOOL = f"mcp__{SERVER}__{TOOL}"
HOSTS = ("codex", "cc")
POLICY = "session_wi_declaration.v1"
# Chat, read-only queries and explanations declare "none": ordinary non-WI work.
NO_WI = "none"
NO_WI_POLICY = "non_wi_activity.v1"
DESCRIPTION = "登记当前 session 的归属，调用一次即可。要改文件、提 PR 或动生产时登记所属 WI；闲聊、只读查询、解释说明登记 none，不要为此新建 WI。"
CC_DESCRIPTION = "登记当前原生请求的归属。要改文件、提 PR 或动生产时声明所属 WI；闲聊、只读查询、解释说明声明 none，不要为此新建 WI。每个新的人类请求或独立任务各自声明；复用会话不继承前一请求的归属。当前请求的子任务保留原始请求归属；子代理不需要声明，继承原请求的 WI 或其首行 [WI: 子WI] 指定的子 WI。一个会话只为一个 WI 谱系（一个 WI 及其子 WI）写代码、提 PR：声明过谱系外 WI 的会话，其提交登记会被拒；谱系外 WI 的写工作请人类 /new 后在新会话声明。本请求已声明 none 后不再改声明，写操作留到下一条请求。子 WI 的提交在人类消息触发的回合里由带标记的子代理做。"
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


class PurposeBindingError(ReceiptError):
    """A diagnostic for accounting; never an instruction to block business."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise PurposeBindingError(code)


def _uuid(value):
    if not isinstance(value, str) or UUID.fullmatch(value) is None:
        _fail("host_request_identity_missing")
    return value.lower()


def _time(value, label):
    try:
        return parse_timestamp(value, label)
    except (ValueError, TypeError, AttributeError):
        _fail("host_request_time_invalid")


def _identifier(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 1024
        and all(ord(c) >= 32 for c in value)
    )


def host_binding(metadata, observed_at):
    """Bind the current native session, never its root or an Agent parameter."""
    if not isinstance(metadata, dict):
        _fail("host_request_metadata_missing")
    turn = metadata.get("x-codex-turn-metadata")
    if not isinstance(turn, dict):
        _fail("host_request_metadata_missing")
    root, thread = (_uuid(turn.get(k)) for k in ("session_id", "thread_id"))
    if ("sessionId" in metadata and _uuid(metadata["sessionId"]) != root) or (
        "threadId" in metadata and _uuid(metadata["threadId"]) != thread
    ):
        _fail("host_request_identity_conflict")
    return {"native_scope_ref": reference("native_session", ["codex", thread])}


def purpose(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"wi"}:
        _fail("wi_only_arguments_required")
    wi = arguments["wi"]
    if not isinstance(wi, str) or (wi != NO_WI and WI.fullmatch(wi) is None):
        _fail("explicit_wi_required")
    return wi


ARGUMENT_REASONS = {"wi_only_arguments_required", "explicit_wi_required"}


def retry_arguments(arguments):
    """The only accepted shape, filled with a WI the Agent already supplied."""
    values = arguments.values() if isinstance(arguments, dict) else ()
    found = [v for v in values if isinstance(v, str) and (v == NO_WI or WI.fullmatch(v))]
    return {"wi": found[0] if len(set(found)) == 1 else "wi_YYYYMMDD_NNN"}


def cc_transport(wi, *, request=False):
    return {"schema": CC_REQUEST_TRANSPORT_SCHEMA if request else CC_TRANSPORT_SCHEMA, "wi": wi, "origin": "agent_declaration"}


def bind_declaration(arguments, metadata, *, declared_at, host="codex"):
    """A session registration: declaration time and turn do not limit its scope.

    CC sends no session identity to MCP servers. Its transcript records the
    call and result under the native sessionId, so the CC ack carries no scope
    and the collector binds it from that record.
    """
    wi = purpose(arguments)
    if host == "cc":
        if metadata is not None and (
            not isinstance(metadata, dict) or "x-codex-turn-metadata" in metadata
        ):
            _fail("host_request_identity_conflict")
        return cc_transport(wi, request=True)
    if host != "codex":
        _fail("host_runtime_unsupported")
    host = host_binding(metadata, declared_at)
    return {
        "schema": TRANSPORT_SCHEMA,
        "native_scope_ref": host["native_scope_ref"],
        "wi": wi,
        "origin": "agent_declaration",
    }


def decode_native_completion(row, *, expected_thread, expected_turn):
    """Decode the actual Desktop item_completed/McpToolCall protocol record.

    The registered source reader supplies the expected current native owner and
    turn. The completed item holds the real invocation and structured result;
    outer code-mode programs and their printed output are never interpreted.
    """
    if not isinstance(row, dict) or row.get("type") != "event_msg":
        _fail("native_mcp_record_invalid")
    event = row.get("payload")
    if not isinstance(event, dict) or event.get("type") != "item_completed":
        _fail("native_mcp_record_invalid")
    thread, turn = _uuid(expected_thread), _uuid(expected_turn)
    if _uuid(event.get("thread_id")) != thread or _uuid(event.get("turn_id")) != turn:
        _fail("native_mcp_owner_conflict")
    item = event.get("item")
    if (
        not isinstance(item, dict)
        or item.get("type") != "McpToolCall"
        or item.get("server") != "pulse-request-purpose"
        or item.get("tool") != TOOL
        or not _identifier(item.get("id"))
        or item.get("status") != "completed"
    ):
        _fail("native_mcp_record_invalid")
    start, end = event.get("started_at_ms"), event.get("completed_at_ms")
    if (
        any(
            type(value) is not int or not 0 < value < 253402300800000
            for value in (start, end)
        )
        or end < start
    ):
        _fail("native_mcp_time_invalid")
    completed_at = _time(row.get("timestamp"), "native MCP completion")
    if completed_at < datetime.fromtimestamp(end / 1000, tz=timezone.utc):
        _fail("native_mcp_time_invalid")
    scope = reference("native_session", ["codex", thread])
    transport = {
        "schema": TRANSPORT_SCHEMA,
        "native_scope_ref": scope,
        "wi": purpose(item.get("arguments")),
        "origin": "agent_declaration",
    }
    returned = item.get("result")
    if not isinstance(returned, dict) or returned.get("isError") is not False:
        _fail("native_mcp_result_unavailable")
    if returned.get("structuredContent") != transport:
        _fail("native_mcp_result_mismatch")
    blocks = returned.get("content")
    if not isinstance(blocks, list) or len(blocks) != 1:
        _fail("native_mcp_result_mismatch")
    try:
        block = blocks[0]
        if (
            set(block) != {"type", "text"}
            or block["type"] != "text"
            or json.loads(block["text"]) != transport
        ):
            _fail("native_mcp_result_mismatch")
    except (ValueError, TypeError, KeyError):
        _fail("native_mcp_result_mismatch")
    return {
        **transport,
        "declared_at": row["timestamp"],
        "native_evidence_ref": reference("native_mcp_purpose_record", digest(row)),
    }


def _has_admitted_session_call(path, cutoff):
    """Scope MCP validation to actual post-admission declarations.

    Other native history is still read by its own usage adapter. It cannot
    become a failed MCP declaration when no such declaration was attempted.
    Detection admits failed/malformed calls too; validation remains unchanged.
    """
    with path.open("rb") as stream:
        for raw in stream:
            if not raw.endswith(b"\n"):
                break
            if b'"item_completed"' not in raw or TOOL.encode() not in raw:
                continue
            row = json.loads(raw)
            event = row.get("payload") if isinstance(row, dict) else None
            item = event.get("item") if isinstance(event, dict) else None
            if (
                isinstance(row, dict)
                and isinstance(event, dict)
                and row.get("type") == "event_msg"
                and event.get("type") == "item_completed"
                and isinstance(item, dict)
                and item.get("type") == "McpToolCall"
                and item.get("server") == "pulse-request-purpose"
                and item.get("tool") == TOOL
                and _time(row.get("timestamp"), "native MCP admission") >= cutoff
            ):
                return True
    return False


def native_completions(paths, usages, diagnostics, *, since):
    """Read one session claim from its enrolled native call; never parse prose.

    The source cutover admits declarations, not selected usage turns. Once a
    session is registered its prior and later native usage uses that same claim.
    """
    from collections import defaultdict
    from pathlib import Path
    from .receipt_sources import _codex_rollout_file_ids
    from .adapters import (
        _codex_rollout_index,
        _copied_parent_positions,
        _root_codex_session,
    )

    cutoff = _time(since, "session declaration admission")
    scopes = {
        r.payload.get("native_scope_ref")
        for r in usages
        if r.kind == "usage" and r.payload.get("runtime") == "codex"
    } - {None}
    paths = sorted({Path(p) for p in paths})
    selected, versions, owners = {}, defaultdict(dict), defaultdict(set)
    invalid, admitted = set(), {scope: {} for scope in scopes}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ReceiptError("native MCP source is indirect")
        ids = _codex_rollout_file_ids(path)
        if ids and reference("native_session", ["codex", ids[0]]) in scopes:
            selected[path] = ids
    # Inspect all copies of a declared session, including copies with no call.
    # Unrelated legacy history remains the usage adapter's responsibility.
    declared = {
        reference("native_session", ["codex", ids[0]])
        for path, ids in selected.items()
        if _has_admitted_session_call(path, cutoff)
    }
    selected = {
        path: ids
        for path, ids in selected.items()
        if reference("native_session", ["codex", ids[0]]) in declared
    }
    indexed = _codex_rollout_index(
        paths, [p for p, ids in selected.items() if ids[0] == ids[1]]
    )
    for path, ids in selected.items():
        scope = reference("native_session", ["codex", ids[0]])
        local = indexed.get(ids[0]) if ids[0] == ids[1] else None
        if ids[0] == ids[1] and local is None:
            invalid.add(scope)
            diagnostics.append("native_mcp_thread_identity_conflict")
            continue
        if local is not None and local.path != path:
            continue
        root = _root_codex_session(local, indexed) if local is not None else None
        ancestors = set()
        if root is not None:
            ancestor = local
            while ancestor.parent_session_id is not None:
                ancestors.add(ancestor.parent_session_id)
                ancestor = indexed[ancestor.parent_session_id]
        copied = (
            _copied_parent_positions(local, indexed)
            if root is not None
            else frozenset()
        )
        session, turn, started = None, None, None
        with path.open("rb") as stream:
            for position, raw in enumerate(stream, 1):
                if position in copied:
                    continue
                if not raw.endswith(b"\n"):
                    break
                if not any(
                    tag in raw
                    for tag in (
                        b'"session_meta"',
                        b'"task_started"',
                        b'"task_complete"',
                        b'"task_aborted"',
                        b'"item_completed"',
                    )
                ):
                    continue
                row = json.loads(raw)
                event = row.get("payload") if isinstance(row, dict) else None
                if not isinstance(event, dict):
                    continue
                if row.get("type") == "session_meta":
                    if event.get("id") in ancestors:
                        continue
                    if (
                        session is not None
                        or event.get("id") != ids[0]
                        or (root is not None and event.get("session_id", root) != root)
                        or (
                            ids[0] != ids[1]
                            and event.get("history_mode") != "paginated"
                        )
                    ):
                        invalid.add(scope)
                        diagnostics.append("native_mcp_thread_identity_conflict")
                        break
                    session = ids[0]
                    continue
                if session is None or row.get("type") != "event_msg":
                    continue
                kind = event.get("type")
                event_turn, event_owner = event.get("turn_id"), event.get("thread_id")
                if (
                    kind == "item_completed"
                    and isinstance(event_turn, str)
                    and isinstance(event_owner, str)
                ):
                    owners[(scope, event_turn)].add(event_owner)
                if kind == "task_started":
                    turn, started = event_turn, row.get("timestamp")
                    continue
                if kind in ("task_complete", "task_aborted"):
                    turn, started = None, None
                    continue
                item = event.get("item")
                if (
                    kind != "item_completed"
                    or not isinstance(item, dict)
                    or item.get("type") != "McpToolCall"
                    or item.get("server") != "pulse-request-purpose"
                    or item.get("tool") != TOOL
                ):
                    continue
                if _time(row.get("timestamp"), "native MCP completion") < cutoff:
                    continue
                if not isinstance(turn, str) or not _identifier(item.get("id")):
                    invalid.add(scope)
                    diagnostics.append("native_mcp_owner_unavailable")
                    continue
                versions[(scope, item["id"])][digest(row)] = (
                    row,
                    session,
                    turn,
                    started,
                )
    for (scope, _), variants in versions.items():
        if len(variants) != 1:
            invalid.add(scope)
            diagnostics.append("native_mcp_call_conflict")
            continue
        row, session, turn, started = next(iter(variants.values()))
        if owners[(scope, turn)] != {session}:
            invalid.add(scope)
            diagnostics.append("native_mcp_owner_conflict")
            continue
        try:
            declaration = decode_native_completion(
                row, expected_thread=session, expected_turn=turn
            )
            if (
                _time(started, "native turn start").timestamp() * 1000
                > row["payload"]["started_at_ms"]
            ):
                _fail("native_mcp_time_invalid")
            # The first actual record for a WI is enough. A retry is not another
            # registration, attribution version, or billable usage event.
            prior = admitted[scope].get(declaration["wi"])
            if prior is None or _time(
                declaration["declared_at"], "declaration"
            ) < _time(prior["declared_at"], "declaration"):
                admitted[scope][declaration["wi"]] = declaration
        except PurposeBindingError as exc:
            invalid.add(scope)
            diagnostics.append(exc.code)
    return {
        scope: [] if scope in invalid else list(claims.values())
        for scope, claims in admitted.items()
    }


def _cc_result_text(block):
    content = block.get("content")
    if isinstance(content, str):
        return content
    if (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and set(content[0]) == {"type", "text"}
        and content[0]["type"] == "text"
    ):
        return content[0]["text"]
    return None


def decode_cc_completion(use_row, use, result_row, result):
    """Decode one CC transcript tool_use and its paired tool_result.

    The session is the transcript record's own sessionId; the Agent's
    arguments never supply it. The result must be this server's exact ack.
    """
    session = use_row.get("sessionId")
    _uuid(session)
    if (
        result_row.get("sessionId") != session
        or result_row.get("isSidechain") is not use_row.get("isSidechain")
        or result_row.get("sourceToolAssistantUUID", use_row.get("uuid"))
        != use_row.get("uuid")
    ):
        _fail("native_mcp_owner_conflict")
    if not _identifier(use.get("id")) or result.get("tool_use_id") != use["id"]:
        _fail("native_mcp_record_invalid")
    wi = purpose(use.get("input"))
    used = _time(use_row.get("timestamp"), "native MCP call")
    if _time(result_row.get("timestamp"), "native MCP completion") < used:
        _fail("native_mcp_time_invalid")
    if result.get("is_error"):
        _fail("native_mcp_result_unavailable")
    try:
        text = _cc_result_text(result)
        transport = json.loads(text) if text is not None else None
        if transport not in (cc_transport(wi), cc_transport(wi, request=True)):
            _fail("native_mcp_result_mismatch")
    except (ValueError, TypeError):
        _fail("native_mcp_result_mismatch")
    record = {"session": session, "use": use, "result": result,
              "used_at": use_row["timestamp"], "completed_at": result_row["timestamp"]}
    return {
        **transport,
        "native_scope_ref": reference("native_session", ["cc", session]),
        "declared_at": result_row["timestamp"],
        "native_evidence_ref": reference("native_mcp_purpose_record", digest(record)),
    }


def cc_native_completions(paths, usages, diagnostics, *, since):
    """Read CC session declarations from transcripts; never parse prose.

    A main transcript is <sessionId>.jsonl; only its own non-sidechain records
    declare that session. Subagent transcripts share the parent sessionId, so
    their usage is already in the parent scope and inherits its declaration;
    a subagent's own call cannot claim the parent session and stays unbound.
    """
    from collections import defaultdict
    from pathlib import Path

    cutoff = _time(since, "session declaration admission")
    scopes = {
        r.payload.get("native_scope_ref")
        for r in usages
        if r.kind == "usage" and r.payload.get("runtime") == "cc"
    } - {None}
    needle = CC_TOOL.encode()
    versions, invalid = defaultdict(dict), set()
    admitted = {scope: {} for scope in scopes}
    for path in sorted({Path(p) for p in paths}):
        if path.is_symlink() or not path.is_file():
            raise ReceiptError("native MCP source is indirect")
        data = path.read_bytes()
        if needle not in data:
            continue
        uses, results = defaultdict(list), defaultdict(list)
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                break
            if needle not in raw and b'"tool_result"' not in raw:
                continue
            row = json.loads(raw)
            message = row.get("message") if isinstance(row, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if (
                    row.get("type") == "assistant"
                    and block.get("type") == "tool_use"
                    and block.get("name") == CC_TOOL
                ):
                    uses[block.get("id")].append((row, block))
                elif row.get("type") == "user" and block.get("type") == "tool_result":
                    results[block.get("tool_use_id")].append((row, block))
        for call, found in uses.items():
            row, use = found[0]
            if row.get("isSidechain") is True:
                diagnostics.append("native_mcp_sidechain_unbound")
                continue
            session = row.get("sessionId")
            if path.stem != session:
                # A copied or foreign record is not this transcript's session.
                continue
            scope = reference("native_session", ["cc", session])
            paired = results.get(call)
            if scope not in scopes or not paired:
                continue
            if len({digest(r) for r, _ in found}) != 1 or len(
                {digest(r) for r, _ in paired}
            ) != 1:
                invalid.add(scope)
                diagnostics.append("native_mcp_call_conflict")
                continue
            result_row, result = paired[0]
            if result.get("is_error") is True:
                # The server refused the call (for example wrong arguments):
                # nothing was declared, so other declarations stay valid.
                diagnostics.append("native_mcp_call_refused")
                continue
            try:
                if _time(result_row.get("timestamp"), "native MCP completion") < cutoff:
                    continue
                declaration = decode_cc_completion(row, use, result_row, result)
            except PurposeBindingError as exc:
                invalid.add(scope)
                diagnostics.append(exc.code)
                continue
            versions[(scope, call)][declaration["native_evidence_ref"]] = declaration
    for (scope, call), variants in versions.items():
        if len(variants) != 1:
            invalid.add(scope)
            diagnostics.append("native_mcp_call_conflict")
            continue
        declaration = next(iter(variants.values()))
        # Retain each admitted call: repeating a WI in a later request is a
        # separate scope witness, not a redundant session-wide registration.
        admitted[scope][call] = declaration
    return {
        scope: [] if scope in invalid else list(claims.values())
        for scope, claims in admitted.items()
    }


def session_attributions(facts, usages, bindings, observed):
    """One declaration per native observation, retaining every prior ledger fact."""
    from collections import defaultdict
    from .receipts import canonical

    versions = defaultdict(list)
    for usage in usages:
        if usage.kind == "usage" and usage.payload.get("runtime") in HOSTS:
            versions[usage.entity_ref].append(usage)
    if not versions:
        return []
    # A session declaration revises only its own exact attribution entities.
    # Keep all their predecessor/producer variants; do not project every other
    # WI's historical graph once again for each native source.
    entities = {reference("attribution", [POLICY, target]) for target in versions}
    active, conflicts = current_receipts([r for r in facts if r.entity_ref in entities])
    heads = {r.entity_ref: r for r in active}
    recorded_boundaries = defaultdict(set)
    for row in facts:
        if row.kind == "usage" and row.entity_ref in versions and row.payload.get("runtime") in {"cc", "cc_sdk"} and row.payload.get("mapping") == "cc_message.v1":
            boundary = row.payload.get("interval_start") or row.occurred_at
            if boundary:
                recorded_boundaries[(row.entity_ref, row.payload.get("native_scope_ref"))].add(boundary)
    output = []
    for target, group in versions.items():
        # Purpose is independent of quantity. Duplicate native content blocks
        # still name one request; their counter conflicts remain in the ledger.
        usage = group[0]
        if len({r.payload.get("runtime") for r in group}) != 1:
            continue
        scopes = {r.payload.get("native_scope_ref") for r in group}
        scope = usage.payload.get("native_scope_ref")
        if len(scopes) != 1 or scope not in bindings:
            continue
        boundaries = {r.payload.get("interval_start") or r.occurred_at for r in group} - {None}
        if not boundaries:
            continue
        # The declaration covers the session, including the exact message's
        # earlier Host stream observations. It is not a late execution grant.
        # Keep native block boundaries separately for validating old claims.
        complete_boundaries = boundaries | recorded_boundaries.get((target, scope), set())
        start = min(complete_boundaries, key=lambda value: _time(value, "native usage boundary"))
        entity = reference("attribution", [POLICY, target])
        prior = [heads[entity]] if entity in heads else conflicts.get(entity, [])
        if any(r.producer != POLICY for r in prior):
            continue
        declarations = bindings[scope]
        if not declarations and not prior:
            continue
        wis = {d["wi"] for d in declarations}
        # none -> WI, as for CC and Pi requests: usage before the session's
        # first WI declaration stays none, later usage takes the WI(s).
        if NO_WI in wis and len(wis) > 1:
            first_wi = min(_time(d["declared_at"], "declaration") for d in declarations if d["wi"] != NO_WI)
            first_none = min(_time(d["declared_at"], "declaration") for d in declarations if d["wi"] == NO_WI)
            wis = ({NO_WI} if first_none < first_wi and _time(start, "native usage boundary") < first_wi
                   else wis - {NO_WI})
        state = "assigned" if len(wis) == 1 else "ambiguous" if wis else "unassigned"
        witness = reference("native_session_usage", [scope, target])
        proofs = {reference("native_session_wi", [scope, d["wi"], d["native_evidence_ref"]]) for d in declarations}
        if entity in conflicts:
            # Old CC code issued one sibling per content-block timestamp.
            # Correct only our own otherwise-identical claims, when all their
            # boundaries are reread from this exact native message and the
            # original declaration is still verified. Opposite WI/state or an
            # invented boundary remains a conflict, never latest-writer-wins.
            signatures = {canonical({k: v for k, v in r.payload.items() if k != "effective_at"}) for r in prior}
            if (usage.payload.get("runtime") != "cc" or len(signatures) != 1
                    or any(r.payload.get("effective_at") not in complete_boundaries or witness not in r.evidence for r in prior)
                    or (declarations and any(not (proofs & set(r.evidence)) for r in prior))):
                continue
        payload = {"target_ref": target, "wi": next(iter(wis)) if len(wis) == 1 else None,
                   "executor": None, "responsible": None, "delegator": None,
                   "state": state, "policy": POLICY, "effective_at": start}
        if payload["wi"] == NO_WI:
            payload.update(wi=None, policy=NO_WI_POLICY, work_kind=NO_WI)
        for predecessor in prior or [None]:
            if predecessor is not None and predecessor.payload == payload:
                continue
            evidence = tuple(sorted(proofs | {witness} | (set(predecessor.evidence) if predecessor else set())))
            output.append(Receipt.make("attribution", None, payload, entity_ref=entity,
                supersedes=predecessor.fact_ref if predecessor else None,
                occurred_at=start, observed_at=observed, producer=POLICY, evidence=evidence))
    return output


INPUT_SCHEMA = {
    "type": "object",
    "properties": {"wi": {"type": "string", "pattern": f"^(?:{WI.pattern}|{NO_WI})$"}},
    "required": ["wi"],
    "additionalProperties": False,
}


def handle(message, *, clock, host="codex"):
    """Fixed stdio MCP tool; no shell, transcript, lifecycle or accounting write."""
    params = message.get("params", {})
    if not isinstance(params, dict):
        _fail("invalid_mcp_parameters")
    method = message.get("method")
    if method == "initialize":
        return {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER, "version": "0.2"},
            "instructions": f"使用 {TOOL}：{CC_DESCRIPTION if host == 'cc' else DESCRIPTION}",
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": TOOL,
                    "description": CC_DESCRIPTION if host == "cc" else DESCRIPTION,
                    "inputSchema": INPUT_SCHEMA,
                    "annotations": {
                        "readOnlyHint": True,
                        "destructiveHint": False,
                        "idempotentHint": True,
                        "openWorldHint": False,
                    },
                }
            ]
        }
    if method == "tools/call":
        try:
            if params.get("name") != TOOL:
                _fail("unknown_purpose_tool")
            purpose(params.get("arguments"))
            observed = clock()
            declaration = bind_declaration(
                params["arguments"],
                params.get("_meta"),
                declared_at=observed,
                host=host,
            )
            return {
                "content": [
                    {"type": "text", "text": json.dumps(declaration, sort_keys=True)}
                ],
                "structuredContent": declaration,
                "isError": False,
            }
        except (
            PurposeBindingError,
            ReceiptError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
        ) as exc:
            diagnostic = {
                "status": "unavailable",
                "reason": exc.code
                if isinstance(exc, PurposeBindingError)
                else "purpose_transport_invalid",
                "business_blocked": False,
                "usage_written": False,
            }
            if diagnostic["reason"] in ARGUMENT_REASONS:
                # The Agent can only recover if the rejection names the shape.
                diagnostic["retry_arguments"] = retry_arguments(params.get("arguments"))
            return {
                "content": [
                    {"type": "text", "text": json.dumps(diagnostic, sort_keys=True)}
                ],
                "structuredContent": diagnostic,
                "isError": True,
            }
    if method in ("resources/list", "prompts/list"):
        return {method.split("/")[0]: []}
    _fail("method_not_found")


def serve(input_stream, output_stream, *, host="codex"):
    from .native_request_purpose import utc_now

    while True:
        raw = input_stream.readline(1048577)
        if not raw:
            return
        if len(raw) > 1048576:
            raise ValueError("oversize_mcp_message")
        message = json.loads(raw)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError("invalid_mcp_envelope")
        if "id" not in message:
            continue
        try:
            response = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": handle(message, clock=utc_now, host=host),
            }
        except PurposeBindingError as exc:
            response = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32602, "message": exc.code},
            }
        output_stream.write(json.dumps(response, sort_keys=True) + "\n")
        output_stream.flush()


def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=HOSTS, default="codex")
    serve(sys.stdin.buffer, sys.stdout, host=parser.parse_args().host)
    return 0
