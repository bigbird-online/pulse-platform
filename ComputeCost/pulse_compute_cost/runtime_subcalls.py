"""Evidenced runtime subcalls; no usage creation. WI follows only a proven CC launching usage."""
from __future__ import annotations
from collections import defaultdict
from pathlib import Path
from typing import Mapping
import json
import re
import shlex

from .events import parse_timestamp
from .receipts import Receipt, ReceiptError, current_receipts, digest, reference, REF

FEATURE = "runtime_subcall.v1"
RUNTIMES = {"codex", "cc", "pi", "cc_sdk"}
ENTRIES = {None, "codex_app", "feishu", "telegram", "cli", "automation"}
UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)
READ_ONLY_GIT = {"log", "rev-parse", "status"}
CLI_WORD = re.compile(rb'(?:^|[ /"])(?:codex|claude|pi) ')


def validate_subcall(value):
    fields = {"parent_scope_ref", "child_scope_ref", "call_ref", "parent_runtime", "child_runtime", "basis"}
    if not isinstance(value, Mapping) or not fields <= set(value) <= fields | {"label"}:
        raise ReceiptError("invalid runtime subcall context")
    if any(not isinstance(value[k], str) or not REF.fullmatch(value[k]) for k in ("parent_scope_ref", "child_scope_ref", "call_ref")):
        raise ReceiptError("subcall requires opaque native and tool references")
    if value["parent_scope_ref"] == value["child_scope_ref"]:
        raise ReceiptError("subcall cannot parent itself")
    if any(value[k] not in RUNTIMES for k in ("parent_runtime", "child_runtime")):
        raise ReceiptError("subcall runtime is unknown")
    if value["basis"] not in {"native_tool_result", "reviewed_launch"}:
        raise ReceiptError("subcall evidence basis is unknown")
    label = value.get("label")
    if label is not None and (not isinstance(label, str) or not 1 <= len(label) <= 160 or any(ord(c) < 32 for c in label)):
        raise ReceiptError("subcall label is invalid")


def subcall_actor(target, context, *, node, entrypoint, observed, evidence, executor=None):
    validate_subcall(context)
    if (target.kind != "usage" or target.payload.get("native_scope_ref") != context["child_scope_ref"]
            or target.payload["runtime"] != context["child_runtime"] or not evidence):
        raise ReceiptError("subcall differs from its original native usage")
    from .receipt_sources import actor_receipt
    return actor_receipt(target, node=node, executor=executor, entrypoint=entrypoint, observed=observed,
                         evidence=tuple(evidence), subcall=context, path="subcall")


def reviewed_subcalls(facts, document, observed):
    result = []
    by_fact = {r.fact_ref: r for r in facts}
    items = document.get("subcalls", [])
    if not isinstance(items, list):
        raise ReceiptError("reviewed subcalls must be a list")
    for item in items:
        required = {"context", "target_fact_refs", "node", "entrypoint", "evidence_refs"}
        if not isinstance(item, Mapping) or set(item) != required:
            raise ReceiptError("invalid reviewed subcall")
        context = item["context"]
        validate_subcall(context)
        if context["basis"] != "reviewed_launch" or item["entrypoint"] not in ENTRIES:
            raise ReceiptError("invalid reviewed subcall basis or entrypoint")
        targets, proofs = item["target_fact_refs"], item["evidence_refs"]
        if (not isinstance(targets, list) or not targets or not isinstance(proofs, list) or not proofs
                or any(not isinstance(v, str) or not REF.fullmatch(v) for v in targets + proofs)
                or len(targets) != len(set(targets))):
            raise ReceiptError("reviewed subcall requires exact retained facts")
        for target in targets:
            if target not in by_fact:
                continue
            result.append(subcall_actor(by_fact[target], context, node=item["node"],
                entrypoint=item["entrypoint"], observed=observed,
                evidence=tuple(proofs) + (target, reference("reviewed_subcall", digest(item)))))
    return result


def summarize_subcalls(rows, contexts):
    groups = {}
    count = 0
    for row in rows:
        call = contexts.get(row.entity_ref, {}).get("subcall")
        if call is None:
            continue
        key = digest(call)
        group = groups.setdefault(key, {**call, "event_count": 0})
        group["event_count"] += 1
        count += 1
    if not count:
        return {}
    items = [groups[k] for k in sorted(groups)]
    return {"subcalls": {"event_count": count, "item_count": len(items), "items": items,
                         "sha256": digest(items)}}


def _command_runtime(command):
    """Recognize a direct CLI. Shell programs and prompt examples are not launches."""
    if not isinstance(command, str) or len(command) > 65536 or any(c in command for c in ("$", "`", "\n")):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        args = list(lexer)
    except ValueError:
        return None
    # Literal directory and read-only Git status prefixes change neither the
    # tool nor native identity; each is its own `&&` step before the CLI.
    while "&&" in args:
        step, args = args[:args.index("&&")], args[args.index("&&") + 1:]
        if not (len(step) == 2 and step[0] == "cd" and step[1].startswith("/")
                or len(step) >= 2 and step[0] == "git" and step[1] in READ_ONLY_GIT
                and not any(a and all(c in ";&|<>()" for c in a) for a in step)):
            return None
    # One trailing literal input file carries a multi-line prompt; nothing is
    # evaluated and the output still returns in the tool result.
    if len(args) >= 3 and args[-2] == "<" and args[-1] and not all(c in ";&|<>()" for c in args[-1]):
        args = args[:-2]
    if not args or any(a and all(c in ";&|<>()" for c in a) for a in args):
        return None
    exe = Path(args[0]).name
    if exe == "claude" and any(a in {"-p", "--print"} for a in args[1:]):
        return "cc"
    if exe == "pi" and any(a in {"-p", "--print", "--mode"} for a in args[1:]) and "--list-models" not in args:
        return "pi"
    if exe == "codex" and len(args) > 1 and args[1] in {"exec", "e"}:
        return "codex"
    return None


def _literal_object(text):
    """Decode a flat literal JS object without evaluating a program."""
    decoder = json.JSONDecoder()
    pos = 0
    result = {}
    while pos < len(text):
        match = re.match(r'\s*([A-Za-z_][A-Za-z_0-9]*|"(?:[^"\\]|\\.)*")\s*:\s*', text[pos:])
        if not match:
            return None
        key = match[1]
        key = json.loads(key) if key.startswith('"') else key
        pos += match.end()
        try:
            value, end = decoder.raw_decode(text[pos:])
        except ValueError:
            return None
        if key in result or type(value) not in (str, int, float):
            return None
        result[key] = value
        pos += end
        tail = re.match(r"\s*(,\s*)?", text[pos:])
        pos += tail.end()
        if not tail[1] and pos != len(text):
            return None
    return result


def _commands(payload):
    if payload.get("name") in {"exec_command", "bash", "Bash"}:
        args = payload.get("arguments", payload.get("input", {}))
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except ValueError:
            return []
        return [(None, args.get("cmd", args.get("command")))] if isinstance(args, Mapping) else []
    if payload.get("type") != "custom_tool_call" or payload.get("name") != "exec":
        return []
    script = payload.get("input")
    if not isinstance(script, str):
        return []
    match = re.fullmatch(r"\s*const r\s*=\s*await tools\.exec_command\(\{(.*)\}\);\s*text\(r\);\s*", script, re.S)
    if not match:
        match = re.fullmatch(r"\s*text\(\s*await\s+tools\.exec_command\(\{(.*)\}\)\s*\)\s*;?\s*", script, re.S)
    if match:
        args = _literal_object(match[1])
        return [(None, args.get("cmd"))] if args else []
    match = re.fullmatch(r"\s*const r\s*=\s*await Promise\.allSettled\(\[(.*)\]\);\s*r\.forEach\(\(x,i\)=>text\(JSON\.stringify\(\{i,result:x\}\)\)\);\s*", script, re.S)
    if not match:
        return []
    body, commands = match[1].strip(), []
    pattern = r'tools\.exec_command\(\{((?:"(?:[^"\\]|\\.)*"|[^"{}])*)\}\)\s*(?:,\s*|$)'
    while body:
        part = re.match(pattern, body, re.S)
        if not part:
            return []
        args = _literal_object(part[1])
        if args is None:
            return []
        commands.append((len(commands), args.get("cmd")))
        body = body[part.end():].lstrip()
    return commands


def _texts(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v["text"] for v in value if isinstance(v, Mapping) and v.get("type") in {"text", "input_text"} and isinstance(v.get("text"), str)]
    return []


def _result_texts(value, index):
    results = []
    for text in _texts(value):
        try:
            obj = json.loads(text)
        except ValueError:
            if index is None:
                results.append(text)
            continue
        if index is not None:
            if not isinstance(obj, Mapping) or obj.get("i") != index:
                continue
            result = obj.get("result", {})
            if not isinstance(result, Mapping) or result.get("status") != "fulfilled":
                continue
            obj = result.get("value")
        if isinstance(obj, Mapping) and isinstance(obj.get("output"), str):
            results.append(obj["output"])
        elif index is None:
            results.append(text)
    return results


def _codex_text_session(text):
    """Read the CLI's startup header, never an echoed prompt or numeric tool id."""
    matches = list(re.finditer(r"(?:^|\n)OpenAI Codex v[^\n]+\n--------\n(.*?)\n--------(?:\n|$)", text, re.S))
    if len(matches) != 1:
        return None
    match = matches[0]
    # A model/user transcript containing a header is not a startup receipt.
    if any(line.strip() in {"user", "codex", "assistant"} for line in text[:match.start()].splitlines()):
        return None
    fields = {}
    for line in match[1].splitlines():
        key, sep, value = line.partition(": ")
        if not sep or key in fields:
            return None
        fields[key] = value
    if not {"workdir", "model", "provider", "approval", "sandbox", "session id"} <= fields.keys():
        return None
    value = fields["session id"]
    return value if UUID.fullmatch(value) else None


def _child_session(runtime, texts):
    """The one native UUID the launched CLI itself returned, else None."""
    ids = set()
    for text in texts:
        text_id = _codex_text_session(text) if runtime == "codex" else None
        if text_id is not None:
            ids.add(text_id)
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, Mapping):
                continue
            sid = (obj.get("session_id") if runtime == "cc" and obj.get("type") == "result"
                   else obj.get("id") if runtime == "pi" and obj.get("type") == "session"
                   else obj.get("thread_id") if runtime == "codex" and obj.get("type") == "thread.started" else None)
            if isinstance(sid, str) and UUID.fullmatch(sid):
                ids.add(sid)
    return next(iter(ids)) if len(ids) == 1 else None


def native_launches(path, source):
    """Read exact launch/result pairs from an enrolled Codex rollout.

    Only returned native UUIDs identify a child. Numeric process-tool session
    IDs, command arguments, cwd and nearby timestamps cannot identify it.
    Unsupported programs are not evaluated and produce no inferred relation.
    """
    calls, outputs = {}, defaultdict(list)
    parent, origin = None, None
    with Path(path).open("rb") as handle:
        for raw in handle:
            if not any(word in raw for word in (b'"session_meta"', b'"function_call"', b'"custom_tool_call"', b'"function_call_output"', b'"custom_tool_call_output"')):
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            p = row.get("payload")
            if not isinstance(p, Mapping):
                continue
            if row.get("type") == "session_meta":
                sid = p.get("id")
                if not isinstance(sid, str) or (parent is not None and parent != sid):
                    return []
                parent = sid
                from .codex_origin import native_codex_entrypoint
                origin = native_codex_entrypoint(p.get("originator"), p.get("source"))
            elif row.get("type") == "response_item":
                call = p.get("call_id")
                if not isinstance(call, str):
                    continue
                if p.get("type") in {"function_call", "custom_tool_call"}:
                    item = (p, row.get("timestamp"), reference("native_tool_record", digest(row)))
                    if call in calls and calls[call] != item:
                        return []
                    calls[call] = item
                elif p.get("type") in {"function_call_output", "custom_tool_call_output"}:
                    outputs[call].append((p.get("output"), row.get("timestamp"), reference("native_tool_record", digest(row))))
    if parent is None:
        return []
    found = []
    for call_id, (payload, started, proof) in calls.items():
        for index, command in _commands(payload):
            runtime = _command_runtime(command)
            if runtime is None:
                continue
            for output, ended, result_proof in outputs.get(call_id, []):
                try:
                    if not started or not ended or parse_timestamp(started, "launch") > parse_timestamp(ended, "result"):
                        continue
                except ValueError:
                    continue
                child = _child_session(runtime, _result_texts(output, index))
                if child is None:
                    continue
                found.append({"context": {"parent_scope_ref": reference("native_session", ["codex", parent]),
                    "parent_runtime": "codex", "child_scope_ref": reference("native_session", [runtime, child]),
                    "child_runtime": runtime, "call_ref": reference("runtime_tool_call", [parent, call_id, index]),
                    "basis": "native_tool_result"}, "node": source.get("node"), "entrypoint": origin,
                    "started_at": started, "ended_at": ended, "evidence": [proof, result_proof]})
    return found


def cc_native_launches(path, source):
    """Exact Bash launch/result pairs from one CC transcript (main or subagent).

    The result must name the launching assistant record and return the child's
    own native UUID. A backgrounded command returns only a task handle; its
    completion notice carries no output, so it never identifies a child.
    The launching assistant message is the parent usage whose proven executor
    and request purpose the child spends.
    """
    uses, results, session, pending = {}, defaultdict(list), None, set()
    with Path(path).open("rb") as handle:
        for raw in handle:
            # Parse only a possible CLI launch and the results that answer one.
            if not raw.endswith(b"\n") or not (
                    b'"tool_use"' in raw and b'"Bash"' in raw and CLI_WORD.search(raw)
                    or b'"tool_result"' in raw and any(call in raw for call in pending)):
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, Mapping) or row.get("type") not in {"assistant", "user"}:
                continue
            msg = row.get("message")
            blocks = msg.get("content") if isinstance(msg, Mapping) else None
            if not isinstance(blocks, list):
                continue
            sid = row.get("sessionId")
            if not isinstance(sid, str) or not UUID.fullmatch(sid) or session not in (None, sid):
                return []
            session = sid
            for block in blocks:
                if not isinstance(block, Mapping):
                    continue
                if row["type"] == "assistant" and block.get("type") == "tool_use" and block.get("name") == "Bash":
                    call, uid = block.get("id"), row.get("uuid")
                    if not isinstance(call, str) or not isinstance(uid, str) or not isinstance(block.get("input"), Mapping):
                        continue
                    item = (block["input"], row.get("timestamp"), uid,
                            reference("usage", [sid, msg.get("id") or uid]), reference("native_tool_record", digest(row)))
                    if call not in uses:
                        uses[call] = item
                        pending.add(call.encode())
                    elif uses[call] != item:
                        uses[call] = None  # Two different launches cannot share one id.
                elif row["type"] == "user" and block.get("type") == "tool_result":
                    call = block.get("tool_use_id")
                    if isinstance(call, str):
                        results[call].append((block.get("content"), row.get("timestamp"),
                            row.get("sourceToolAssistantUUID"), reference("native_tool_record", digest(row))))
    found = []
    for call, item in uses.items():
        if item is None:
            continue
        args, started, uid, parent_usage, proof = item
        runtime = _command_runtime(args.get("command"))
        if runtime is None or args.get("run_in_background") not in (None, False):
            continue
        paired = {(ended, result_proof): content for content, ended, owner, result_proof in results.get(call, [])
                  if owner == uid}
        if len(paired) != 1:
            continue
        (ended, result_proof), content = next(iter(paired.items()))
        try:
            if not started or not ended or parse_timestamp(started, "launch") > parse_timestamp(ended, "result"):
                continue
        except ValueError:
            continue
        child = _child_session(runtime, _result_texts(content, None))
        if child is None:
            continue
        found.append({"context": {"parent_scope_ref": reference("native_session", ["cc", session]),
            "parent_runtime": "cc", "child_scope_ref": reference("native_session", [runtime, child]),
            "child_runtime": runtime, "call_ref": reference("runtime_tool_call", ["cc", session, call]),
            "basis": "native_tool_result"}, "node": source.get("node"), "entrypoint": None,
            "parent_usage_ref": parent_usage, "started_at": started, "ended_at": ended,
            "evidence": [proof, result_proof]})
    return found


def _children(by_scope, launch):
    """Child usages wholly inside the launch's call/result interval."""
    low, high = parse_timestamp(launch["started_at"], "launch"), parse_timestamp(launch["ended_at"], "result")
    for row in by_scope[launch["context"]["child_scope_ref"]]:
        p = row.payload
        start, end = p.get("interval_start") or row.occurred_at, p.get("interval_end") or row.occurred_at
        if start and end and low <= parse_timestamp(start, "usage") <= parse_timestamp(end, "usage") <= high:
            yield row


def _usage_versions(active, conflicts):
    """Every current version of each usage. A CC request observed by both the
    Host and its transcript has two heads until the query reconciles them;
    either one is the launching usage."""
    versions = defaultdict(list)
    for row in active + [r for group in conflicts.values() for r in group]:
        if row.kind == "usage":
            versions[row.entity_ref].append(row)
    return versions


def _parent_executor(launch, usages, actors):
    """The one executor already proven for the launching usage, else None.

    All current versions of that usage must agree; an unknown one is silent.
    """
    parents = usages.get(launch.get("parent_usage_ref"), [])
    if not parents:
        return None, ()
    at = parse_timestamp(launch["started_at"], "launch")
    values, proofs = set(), {parent.fact_ref for parent in parents}
    for parent in parents:
        if parent.payload.get("executor") is not None:
            values.add(parent.payload["executor"])
        for target in {parent.entity_ref, parent.payload.get("execution_ref")} - {None}:
            for row in actors.get(target, []):
                a = row.payload
                if a["context"].get("subcall") is not None or not a.get("effective_at"):
                    continue
                if parse_timestamp(a["effective_at"], "identity boundary") <= at and a["executor"] is not None:
                    values.add(a["executor"])
                    proofs.add(row.fact_ref)
    return (next(iter(values)), tuple(sorted(proofs))) if len(values) == 1 else (None, ())


def launch_attributions(facts, launches, observed):
    active, conflicts = current_receipts(facts)
    by_scope, actors = defaultdict(list), defaultdict(list)
    usages = _usage_versions(active, conflicts)
    for row in active:
        if row.kind == "usage" and row.payload.get("native_scope_ref"):
            by_scope[row.payload["native_scope_ref"]].append(row)
    for row in active + [r for versions in conflicts.values() for r in versions]:
        if row.kind == "attribution" and row.payload.get("scope") == "actor":
            actors[row.payload["target_ref"]].append(row)
    result = []
    for launch in launches:
        executor, proofs = _parent_executor(launch, usages, actors)
        for row in _children(by_scope, launch):
            result.append(subcall_actor(row, launch["context"], node=launch["node"], entrypoint=launch["entrypoint"],
                observed=observed, evidence=tuple(launch["evidence"]) + proofs + (row.fact_ref,), executor=executor))
    return result


PURPOSE_POLICY = "runtime_subcall_purpose.v1"
REQUEST_POLICY = "cc_native_request_purpose.v1"


def subcall_purpose_attributions(facts, launches, observed):
    """A proven child spends the WI its launching CC usage was declared for.

    The parent's claims must all be assigned to one WI, including a native
    request declaration. A child with its own purpose claim keeps it. If the
    parent loses that proof, this producer withdraws only its own claim.
    """
    active, conflicts = current_receipts(facts)
    heads = {row.entity_ref: row for row in active}
    by_scope, claims = defaultdict(list), defaultdict(list)
    usages = _usage_versions(active, conflicts)
    for row in active:
        if row.kind == "usage" and row.payload.get("native_scope_ref"):
            by_scope[row.payload["native_scope_ref"]].append(row)
    for row in active + [r for versions in conflicts.values() for r in versions]:
        if row.kind == "attribution" and row.payload.get("scope") != "actor":
            claims[row.payload["target_ref"]].append(row)
    result = {}
    for launch in launches:
        parent_ref = launch.get("parent_usage_ref")
        parents = usages.get(parent_ref, [])
        if not parents:
            continue
        parent_claims = claims[parent_ref]
        purposes = {(r.payload.get("state"), r.payload.get("wi"), r.payload.get("work_kind")) for r in parent_claims}
        wi = None
        if (len(purposes) == 1 and any(r.producer == REQUEST_POLICY for r in parent_claims)):
            state, value, kind = next(iter(purposes))
            wi = value if state == "assigned" and kind is None else None
        for child in _children(by_scope, launch):
            entity = reference("attribution", [PURPOSE_POLICY, child.entity_ref, parent_ref])
            prior = heads.get(entity)
            if entity in conflicts or entity in result or (prior is not None and prior.producer != PURPOSE_POLICY):
                continue
            if child.payload.get("wi") is not None or any(r.producer != PURPOSE_POLICY for r in claims[child.entity_ref]):
                continue
            start = child.payload.get("interval_start") or child.occurred_at
            if wi is None and (prior is None or prior.payload["state"] != "assigned"):
                continue
            payload = {"target_ref": child.entity_ref, "wi": wi, "executor": None, "responsible": None,
                       "delegator": None, "state": "assigned" if wi else "unassigned",
                       "policy": PURPOSE_POLICY, "effective_at": start}
            if prior is not None and prior.payload == payload:
                continue
            evidence = (set(launch["evidence"]) | {child.fact_ref} | {p.fact_ref for p in parents}
                        | {r.fact_ref for r in parent_claims})
            if prior is not None:
                evidence.add(prior.fact_ref)
            result[entity] = Receipt.make("attribution", None, payload, entity_ref=entity,
                supersedes=prior.fact_ref if prior else None, occurred_at=start, observed_at=observed,
                producer=PURPOSE_POLICY, evidence=tuple(sorted(evidence)))
    return list(result.values())
