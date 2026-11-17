"""CC ingress from original Host input and native ancestry, never session defaults."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping

from .cc_lineage import _main_root_session, _sidechain_root_session
from .events import parse_timestamp, utc_text
from .pi_context import _INPUT, REMINDER_SUFFIX, _matches_input_body, input_bytes
from .receipts import Receipt, ReceiptError, digest, reference

# A turn Relay itself delivered into the conversation (collaboration inbox),
# not a human message: Relay appends this one-off reference and records it in
# its Host spool bound to the native session. Its entrypoint is "automation".
DELIVERY_POLICY = 'relay_internal_delivery.v1'
_DELIVERY = re.compile(r"\n\n\[Host internal delivery reference: ([a-f0-9]{64})\.[^\]\r\n]*\]" + REMINDER_SUFFIX)


def _text_input(row: Mapping[str, Any]) -> str | None:
    if row.get('type') != 'user' or row.get('isMeta') is True:
        return None
    content = (row.get('message') or {}).get('content')
    if isinstance(content, str):
        return content
    if (isinstance(content, list) and content
            and all(isinstance(b, dict) and b.get('type') in {'text', 'image'} for b in content)):
        return ''.join(b['text'] for b in content if b.get('type') == 'text' and isinstance(b.get('text'), str))
    return None


# Text CC itself writes as a user record, under the interrupted request's
# promptId, when the user stops a turn. It is not another human request.
INTERRUPT_MARKERS = frozenset({'[Request interrupted by user]', '[Request interrupted by user for tool use]'})


def _request_text(row: Mapping[str, Any]) -> str | None:
    text = _text_input(row)
    return None if text is not None and text.strip() in INTERRUPT_MARKERS else text


def _notification_fields(text: str) -> tuple[str, str | None, str] | None:
    """Decode one bounded native envelope; opaque result bodies are not routing.

    A repeated completion of a task that already notified once (a subagent
    that waited on its own background work) omits its tool-use ID. A subagent
    launched with worktree isolation ends with the worktree it used; that
    location is not routing either.
    """
    if len(text) > 65536 or '<!' in text or '<?' in text:
        return None
    try:
        envelope = ET.fromstring(text)
    except ET.ParseError:
        return None
    fields = list(envelope)
    names = [field.tag for field in fields]
    allowed = ['task-id', 'tool-use-id', 'output-file', 'status', 'summary', 'note', 'result', 'usage', 'worktree']
    required = allowed[:5] if 'tool-use-id' in names else [n for n in allowed[:5] if n != 'tool-use-id']
    if (envelope.tag != 'task-notification' or envelope.attrib
            or (envelope.text or '').strip() or not len(required) <= len(fields) <= len(allowed)
            or names[:len(required)] != required or len(set(names)) != len(names)
            or any(name not in allowed for name in names)
            or names != sorted(names, key=allowed.index)
            or any(field.attrib or (field.tail or '').strip() for field in fields)):
        return None
    for field in fields:
        if field.tag == 'usage':
            counters = list(field)
            if ((field.text or '').strip() or len(counters) > 3
                    or len({c.tag for c in counters}) != len(counters)
                    or any(c.tag not in {'subagent_tokens', 'tool_uses', 'duration_ms'}
                           or c.attrib or list(c) or (c.tail or '').strip()
                           or not re.fullmatch(r'[0-9]{1,20}', (c.text or '').strip()) for c in counters)):
                return None
        elif field.tag == 'worktree':
            parts = list(field)
            if ((field.text or '').strip() or [p.tag for p in parts] != ['worktreePath', 'worktreeBranch']
                    or any(p.attrib or list(p) or (p.tail or '').strip()
                           or not 1 <= len((p.text or '').strip()) <= 4096 for p in parts)
                    or not Path(parts[0].text.strip()).is_absolute()):
                return None
        elif list(field):
            return None
    values = {f.tag: (f.text or '').strip() for f in fields}
    task, tool, output, status = (values['task-id'], values.get('tool-use-id'),
                                  values['output-file'], values['status'])
    if (not re.fullmatch(r'[^<>\s]{1,256}', task)
            or (tool is not None and not re.fullmatch(r'[^<>\s]{1,256}', tool))
            or status not in {'completed', 'failed', 'killed'} or not 1 <= len(output) <= 4096
            or not Path(output).is_absolute() or '..' in output.split('/')
            or any(ord(c) < 32 for c in output) or not output.endswith('/tasks/' + task + '.output')):
        return None
    return task, tool, output



def _send_message_result(content):
    """Decode a bounded native tool acknowledgement with no duplicate fields."""
    if isinstance(content, list) and content and all(
            isinstance(b, dict) and set(b) == {'type', 'text'}
            and b['type'] == 'text' and isinstance(b['text'], str) for b in content):
        content = ''.join(b['text'] for b in content)
    if not isinstance(content, str) or len(content) > 65536:
        return None
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate native result field')
            result[key] = value
        return result
    try:
        result = json.loads(content, object_pairs_hook=unique)
    except (ValueError, RecursionError):
        return None
    return result if isinstance(result, dict) else None


def _task_origin(origin) -> bool:
    """CC's native task-completion origin; newer CC adds an opaque runId."""
    return (isinstance(origin, dict) and origin.get('kind') == 'task-notification'
            and origin.get('producer') == 'session-task'
            and set(origin) <= {'kind', 'producer', 'runId'}
            and isinstance(origin.get('runId', ''), str))


# Fields CC fills in when a streamed reply finishes. A fork taken while its
# parent's reply was still streaming copies the record before they settle.
STREAM_SETTLED_FIELDS = frozenset({'usage', 'stop_reason', 'stop_sequence', 'stop_details'})


def _record_digest(row: Mapping[str, Any]) -> str:
    """Identity of a native record. A forked subagent's transcript repeats its
    parent agent's records under its own agentId; that copy is the same record,
    even when it was taken before the copied reply's stream settled."""
    identity = {k: v for k, v in row.items() if k != 'agentId'}
    message = identity.get('message')
    if row.get('type') == 'assistant' and isinstance(message, Mapping):
        identity['message'] = {k: v for k, v in message.items() if k not in STREAM_SETTLED_FIELDS}
    return digest(identity)


def _conflicting_copy(old: Mapping[str, Any], row: Mapping[str, Any], old_side, side,
                      other_file: bool) -> bool:
    """Whether two records sharing one native UUID contradict each other.

    A fork re-links the subagent records it copies into its own file: the
    same records, with the same content, may name a different but equally
    ordered parent there. Each file's own order still proves its edges.
    """
    if old_side != side:
        return True
    if _record_digest(old) == _record_digest(row):
        return False
    return not (side is True and other_file
                and _record_digest({**old, 'parentUuid': None}) == _record_digest({**row, 'parentUuid': None}))


def _after_reply(nodes: dict, positions: dict, key: tuple, parent: tuple) -> bool:
    """`parent` follows `key` in one file with only `key`'s own reply message between.

    CC streams one assistant message as several records (thinking, text,
    tool_use) under one message id; a record written after the reply comes
    after its last block.
    """
    message = (nodes[key].get('message') or {}).get('id')
    for path, ordinal in positions[key].items():
        end = positions[parent].get(path)
        if end is None or end <= ordinal:
            continue
        if end == ordinal + 1:
            return True
        between = [k for k, at in positions.items() if ordinal < at.get(path, -1) < end]
        if message and all(nodes[k].get('type') == 'assistant'
                           and (nodes[k].get('message') or {}).get('id') == message for k in between):
            return True
    return False


def _native_parent(nodes: dict, roles: dict, positions: dict, key: tuple) -> tuple | None:
    """The parentUuid record of `key` when native order proves the edge.

    The parent precedes the child in a shared transcript file. CC writes a
    subagent's context attachments out of order, so an attachment parent of
    an attachment may instead precede it by timestamp. CC also writes the
    deferred-tools record a reply was generated after right after that reply,
    following any further streamed blocks of the same reply message; that
    record's own parent precedes the reply.
    A forked subagent's transcript starts with a copy of its parent agent's
    records; that copy, earlier in the child's own file, continues the chain
    across the agentId.
    """
    row = nodes[key]
    parent = (key[0], row.get('parentUuid'))
    if parent not in nodes or roles[parent] != roles[key]:
        return None
    earlier = any(positions[parent].get(path, ordinal) < ordinal for path, ordinal in positions[key].items())
    if nodes[parent].get('agentId') != row.get('agentId'):
        return parent if roles[key] and earlier else None
    if earlier:
        return parent
    grandparent = (key[0], nodes[parent].get('parentUuid'))
    attachment = nodes[parent].get('attachment')
    if (nodes[parent].get('type') == 'attachment' and isinstance(attachment, Mapping)
            and attachment.get('type') == 'deferred_tools_record'
            and _after_reply(nodes, positions, key, parent)
            and grandparent in nodes and roles[grandparent] == roles[key]
            and nodes[grandparent].get('agentId') == row.get('agentId')
            and any(positions[grandparent].get(path, ordinal) < ordinal
                    for path, ordinal in positions[key].items())):
        return parent
    try:
        attachments = nodes[parent].get('type') == row.get('type') == 'attachment'
        return parent if attachments and (parse_timestamp(nodes[parent]['timestamp'], 'parent record')
                                          <= parse_timestamp(row['timestamp'], 'child record')) else None
    except (KeyError, ReceiptError, TypeError, ValueError):
        return None


def _task_notifications(nodes: dict, roles: dict, positions: dict, bad: set) -> dict:
    """Join a typed runtime completion to its exact successful trigger tool.

    The native system origin, verified root, unique tool result and task
    identities must agree. A resumed Agent uses the resume request's input,
    rather than the older launch input or an intervening human request.
    A main-session completion can refer to a verified sidechain's exact
    SendMessage trigger; its original input still needs a unique prompt proof.
    """
    tasks = defaultdict(set)
    for key, row in nodes.items():
        result = row.get('toolUseResult')
        assistant = (key[0], row.get('sourceToolAssistantUUID'))
        if (key in bad or assistant in bad
                or ('toolUseResult' in row and not isinstance(result, dict))
                or row.get('type') != 'user' or row.get('parentUuid') != assistant[1]
                or assistant not in nodes or nodes[assistant].get('type') != 'assistant'
                or roles[key] != roles[assistant]
                or row.get('agentId') != nodes[assistant].get('agentId')):
            continue
        blocks = (row.get('message') or {}).get('content')
        tools = (nodes[assistant].get('message') or {}).get('content')
        if not isinstance(blocks, list) or not isinstance(tools, list):
            continue
        for block in blocks:
            if (not isinstance(block, dict) or block.get('type') != 'tool_result'
                    or block.get('is_error') is True):
                continue
            tool_id = block.get('tool_use_id')
            matched = [t for t in tools if isinstance(t, dict) and t.get('type') == 'tool_use'
                       and t.get('id') == tool_id]
            if len(matched) != 1 or not isinstance(tool_id, str) or not isinstance(matched[0].get('input'), dict):
                continue
            tool = matched[0]; inputs = tool['input']; task = None; output = None; kind = tool.get('name')
            native_result = result
            if kind == 'SendMessage' and native_result is None:
                # CC also serializes its native acknowledgement in the paired
                # tool_result content. Only this exact successful tool edge is
                # decoded; notification result text never provides routing.
                native_result = _send_message_result(block.get('content'))
            if not isinstance(native_result, dict):
                continue
            if kind == 'Bash':
                # CC records the id of a background shell, whether requested
                # or moved to the background after its timeout.
                task = native_result.get('backgroundTaskId')
            # CC may run a subagent in the background by default; its own
            # async launch result is the proof. Only an explicit foreground
            # request contradicts it.
            elif (kind == 'Agent' and inputs.get('run_in_background') is not False
                  and native_result.get('isAsync') is True and native_result.get('status') == 'async_launched'):
                task, output = native_result.get('agentId'), native_result.get('outputFile')
                if not isinstance(output, str):
                    continue
            elif kind == 'SendMessage' and native_result.get('success') is True and inputs.get('type') == 'message':
                task = native_result.get('resumedAgentId')
                recipients = [inputs[k] for k in ('to', 'recipient') if k in inputs]
                if not recipients or any(recipient != task for recipient in recipients):
                    continue
                output = native_result.get('outputFile')
                if output is not None and not isinstance(output, str):
                    continue
            if not isinstance(task, str) or not re.fullmatch(r'[^<>\s]{1,256}', task):
                continue
            if (any(positions[assistant].get(path, ordinal) < ordinal for path, ordinal in positions[key].items())
                    and parse_timestamp(nodes[assistant]['timestamp'], 'task tool')
                        <= parse_timestamp(row['timestamp'], 'task result')):
                tasks[(key[0], task, tool_id)].add((key, kind, output))
    links = {}
    for key, row in nodes.items():
        if (key in bad or not _task_origin(row.get('origin'))
                or row.get('promptSource') != 'system' or row.get('turnOrigin') != 'task_notification'):
            continue
        text = _text_input(row)
        identity = _notification_fields(text) if isinstance(text, str) else None
        if identity is None:
            continue
        task, tool, output = identity
        if tool is None:
            # A repeat completion names only its task: it must have exactly one launch.
            launches = [k for k in tasks if k[:2] == (key[0], task)]
            tool = launches[0][2] if len(launches) == 1 else None
        candidates = tasks.get((key[0], task, tool), set()) if tool is not None else set()
        if len(candidates) != 1:
            continue
        parent, kind, declared_output = next(iter(candidates))
        blocks = nodes[parent]['message']['content']
        cross_sidechain = (kind == 'SendMessage' and roles[key] is False
                           and roles[parent] is True and row.get('agentId') is None)
        same_chain = (roles[key] == roles[parent]
                      and row.get('agentId') == nodes[parent].get('agentId'))
        if (not (same_chain or cross_sidechain)
                or (kind == 'Bash' and not any(isinstance(b.get('content'), str) and output in b['content']
                    for b in blocks if isinstance(b, dict) and b.get('type') == 'tool_result'))
                or (declared_output is not None and declared_output != output)
                or (not cross_sidechain and not any(positions[parent].get(path, ordinal) < ordinal
                           for path, ordinal in positions[key].items()))
                or parse_timestamp(nodes[parent]['timestamp'], 'task start') > parse_timestamp(row['timestamp'], 'task completion')):
            continue
        links[key] = parent
    return links


def _host_bindings(facts: list[Receipt], node: str) -> dict:
    """Reuse the Host's recorded input→execution→native-session identity proof."""
    executions, scopes, actor_rows = defaultdict(list), defaultdict(set), []
    for row in facts:
        p = row.payload
        if row.kind == 'execution' and row.producer == 'relay_host.v1':
            executions[row.entity_ref].append(row)
        elif (row.kind == 'usage' and p['runtime'] == 'cc' and p.get('execution_ref')
              and p.get('native_scope_ref') and p['mapping'] in {'cc_message.v1', 'cc_result.v1', 'cc_sdk_result.v1'}):
            scopes[p['execution_ref']].add(p['native_scope_ref'])
        elif (row.kind == 'attribution' and row.producer == 'registered_runtime_actor.v1'
              and p.get('scope') == 'actor' and p.get('policy') == row.producer
              and p['context'].get('input_ref') and p['context'].get('node') == node):
            actor_rows.append(row)
    # Route provenance belongs to the original dispatch, not terminal state.
    # State/WI revisions cannot erase agreeing immutable ingress identity.
    hosts = {}
    for ref, versions in executions.items():
        identities = {(r.occurred_at, r.payload['runtime'], r.payload['executor']) for r in versions}
        if len(identities) == 1 and versions[0].payload['runtime'] == 'cc':
            hosts[ref] = versions[0]
    result = defaultdict(set)
    for row in actor_rows:
        p = row.payload; host = hosts.get(p['target_ref']); c = p['context']
        if (host is None or len(scopes[host.entity_ref]) != 1 or not row.evidence
                or p['executor'] != host.payload['executor'] or not p['executor']
                or c.get('entrypoint') not in {'telegram', 'feishu'}):
            continue
        result[(next(iter(scopes[host.entity_ref])), c['input_ref'])].add(
            (c['entrypoint'], p['executor'], p['effective_at'], row.fact_ref))
    return result


def _host_deliveries(facts: list[Receipt]) -> tuple[dict, dict]:
    """Relay's own record of each turn it delivered (a collaboration inbox
    contract), keyed by the delivery reference bound to its native session.

    The record is an inert claim on that reference, not on any usage. A
    reference recorded twice with different times proves nothing.
    """
    times, refs = defaultdict(set), {}
    for row in facts:
        p = row.payload
        if (row.kind == 'attribution' and row.producer == 'relay_host.v1'
                and p.get('policy') == DELIVERY_POLICY and p.get('state') == 'unassigned'
                and all(p.get(k) is None for k in ('wi', 'executor', 'responsible', 'delegator'))
                and p.get('effective_at')):
            times[p['target_ref']].add(p['effective_at'])
            refs[p['target_ref']] = row.fact_ref
    return ({ref: next(iter(at)) for ref, at in times.items() if len(at) == 1},
            {ref: refs[ref] for ref, at in times.items() if len(at) == 1})


def cc_input_actor_receipts(prefixes: list[tuple[Path, bytes]], source: Mapping[str, Any],
                            host_sources: list[Mapping[str, Any]], host_facts: list[Receipt],
                            usages: list[Receipt], observed: str, *,
                            workspace_cache: dict | None = None) -> list[Receipt]:
    """One route-only fact per proved message; no quantity, task or execution grant.

    A sidechain input and a compaction summary continue the request whose
    native prompt ID they carry: the walk resumes at that request's unique main
    input, as in `cc_request_purpose`, so a subagent launched from a task
    completion still reaches the human input. Bare new inputs reset context.
    Input body/hash/principal/time and original Host dispatch scope must agree;
    quotes or a matching timestamp do not join.
    """
    from .receipt_sources import registered_workspace_actor, _input_principal
    node = source.get('node')
    hosts = [h for h in host_sources if h.get('kind') == 'relay_host' and h.get('node') == node
             and h.get('actor_attribution') is True and h.get('host_native_input') is True]
    if not hosts or source.get('actor_attribution') is not True or not node:
        return []
    host_links = _host_bindings(host_facts, node)
    nodes, roles, roots, bad, messages = {}, {}, set(), set(), defaultdict(set)
    positions = defaultdict(dict)
    source_root = Path(source['path']).resolve(strict=True)
    for path, data in prefixes:
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(source_root):
            raise ReceiptError('CC input source escaped registered root')
        for ordinal, raw in enumerate(data.splitlines()):
            if not raw.strip():
                continue
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ReceiptError('invalid CC ancestry record')
            session, uid = row.get('sessionId'), row.get('uuid')
            if not isinstance(session, str) or not session or not isinstance(uid, str) or not uid:
                continue
            side = row.get('isSidechain')
            root = (_sidechain_root_session(path, row.get('agentId', '')) if side is True else
                    _main_root_session(path, session) if side is False else None)
            if not root or session != root:
                continue
            key = (root, uid)
            positions[key].setdefault(path, ordinal)
            if key in nodes and _conflicting_copy(nodes[key], row, roles[key], side,
                                                  any(p != path for p in positions[key])):
                bad.add(key)
            nodes[key], roles[key] = row, side
            if not side:
                roots.add(root)
            msg = row.get('message') or {}
            if row.get('type') == 'assistant' and isinstance(msg, dict) and msg.get('usage') is not None:
                identity = msg.get('id') or uid
                messages[reference('usage', [session, identity])].add(key)
    task_notifications = _task_notifications(nodes, roles, positions, bad)
    deliveries, delivery_facts = _host_deliveries(host_facts)
    delivery_inputs = defaultdict(set)
    inputs, prompts, metadata_cache = {}, defaultdict(set), {}
    workspace_cache = {} if workspace_cache is None else workspace_cache
    origin_evidence = defaultdict(set)
    for key, row in nodes.items():
        text = _request_text(row)
        if text is None or roles[key] or row.get('isCompactSummary'):
            continue
        prompt = row.get('promptId')
        if isinstance(prompt, str) and prompt:
            prompts[(key[0], prompt)].add(key)
        if key in bad:
            continue
        proof = None
        match = _INPUT.search(text)
        if match:
            token = match[1]; found = set()
            actor = registered_workspace_actor(source, row.get('cwd'), 'cc', workspace_cache=workspace_cache)
            for host in hosts:
                cache_key = (digest(host), token)
                if cache_key not in metadata_cache:
                    metadata_cache[cache_key] = input_bytes(host, token)
                data = metadata_cache[cache_key]
                if data is None:
                    continue
                record = json.loads(data); principal, principal_refs = _input_principal(host, token, data)
                original = (record.get('source') or {}).get('message')
                if not isinstance(original, str) or not _matches_input_body(text[:match.start()], original):
                    continue
                pool = record['poolKey'].split(':'); channel = record['source']['channel']
                if len(pool) not in (2, 3) or pool[2:] not in ([], [channel]):
                    continue
                input_ref = reference('input_metadata', sha256(data).hexdigest())
                scope = reference('native_session', ['cc', key[0]])
                claims = host_links.get((scope, input_ref), set())
                executors = {c[1] for c in claims}
                # The Relay execution below names the executor. The enrolled
                # alias history must agree where it maps this route alias at
                # input time; an alias enrolled later (or not yet) has no
                # mapping then and is no contrary evidence. A cwd outside the
                # registered workspaces (a task worktree) is not contrary
                # evidence either; a cwd registered to another Agent is.
                if not principal_refs:
                    principal = next(iter(executors)) if len(executors) == 1 else None
                if principal is None or actor['executor'] not in {None, principal}:
                    continue
                when = utc_text(datetime.fromtimestamp(record['observedAtMs']/1000, timezone.utc))
                native_at = parse_timestamp(row.get('timestamp'), 'native input')
                # Relay continuation turns re-dispatch the same input later;
                # the original dispatch must precede this native input.
                dispatched = [parse_timestamp(c[2], 'dispatch') for c in claims]
                if (claims and {(c[0], c[1]) for c in claims} == {(channel, principal)}
                        and all(parse_timestamp(when, 'input') <= at for at in dispatched)
                        and any(at <= native_at for at in dispatched)):
                    found.add((channel, input_ref, when))
                    origin_evidence[input_ref].update(c[3] for c in claims)
            if len(found) == 1:
                proof = next(iter(found))
        else:
            delivery = _DELIVERY.search(text)
            if delivery:
                ref = reference('internal_delivery', [delivery[1], reference('native_session', ['cc', key[0]])])
                when = deliveries.get(ref)
                if when is not None and parse_timestamp(when, 'delivery') <= parse_timestamp(row.get('timestamp'), 'native input'):
                    delivery_inputs[ref].add(key)
        inputs[key] = proof
    # A delivery reference is one-off: two inputs carrying it prove neither.
    for ref, keys in delivery_inputs.items():
        if len(keys) == 1:
            inputs[next(iter(keys))] = ('automation', ref, deliveries[ref])
            origin_evidence[ref].add(delivery_facts[ref])
    record_refs = {}

    def record_ref(key):
        if key not in record_refs:
            record_refs[key] = reference('native_record', digest(nodes[key]))
        return record_refs[key]

    # Each record's nearest text ancestor by proven parent edges. The edges
    # are independent of the usage being routed, so every message of a long
    # request shares one walk instead of repeating it.
    text_ancestor = {}

    def nearest_text(key):
        stretch, visited = [], set()
        while (key in nodes and key not in text_ancestor and key not in bad
               and key[0] in roots and _request_text(nodes[key]) is None):
            stretch.append(key); visited.add(key)
            key = _native_parent(nodes, roles, positions, key)
            if key in visited:
                key = None
        found = (text_ancestor[key] if key in text_ancestor else
                 key if key in nodes and key not in bad and key[0] in roots else None)
        for item in stretch:
            text_ancestor[item] = found
        return found

    def walk(key, start, evidence):
        """The proved input of the request `key` belongs to, or None.

        Evidence names the usage record, its proving input and each task
        edge; the parent chain between them is the transcript's own order.
        """
        seen = set()
        key = nearest_text(key)
        while key is not None and key not in seen:
            seen.add(key); row = nodes[key]
            if key in task_notifications:
                if parse_timestamp(row['timestamp'], 'task completion') > parse_timestamp(nodes[start]['timestamp'], 'usage'):
                    return None
                task = task_notifications[key]
                evidence.update(record_ref(k) for k in (key, task, (key[0], nodes[task]['sourceToolAssistantUUID'])))
                key = nearest_text(task)
                continue
            if roles[key] or row.get('isCompactSummary'):
                parent = launching_input(key, row.get('promptId'))
                if parent is None:
                    return None
                key = nearest_text(parent)
                continue
            proof = inputs.get(key)
            if (proof is not None
                    and parse_timestamp(proof[2], 'input') <= parse_timestamp(row['timestamp'], 'native input')
                    and parse_timestamp(row['timestamp'], 'native input') <= parse_timestamp(nodes[start]['timestamp'], 'usage')):
                evidence.update((record_ref(start), record_ref(key)))
                return proof
            return None
        return None

    def launching_input(key, prompt):
        """The unique main input of `prompt`, not later than the record `key`."""
        candidates = prompts.get((key[0], prompt), set())
        parent = next(iter(candidates)) if len(candidates) == 1 else None
        if (parent is None or parent in bad
                or parse_timestamp(nodes[parent]['timestamp'], 'parent input')
                    > parse_timestamp(nodes[key]['timestamp'], 'child input')):
            return None
        return parent

    targets = {r.entity_ref for r in usages if r.kind == 'usage' and r.payload.get('mapping') == 'cc_message.v1'}
    result = []
    for target, entries in messages.items():
        if target not in targets:
            continue
        claims, evidence, unproved = set(), set(), False
        for start in entries:
            if start in bad or start[0] not in roots:
                unproved = True
                continue
            witness = set()
            proof = walk(start, start, witness)
            if proof is None:
                unproved = True
            else:
                claims.add(proof)
                evidence |= witness
        if unproved or len(claims) != 1:
            continue
        channel, input_ref, when = next(iter(claims))
        evidence.update({input_ref, reference("source", source["id"]), *origin_evidence[input_ref]})
        result.append(Receipt.make('attribution', ['cc_native_input_route', target, input_ref], {
            'target_ref': target, 'wi': None, 'executor': None, 'responsible': None, 'delegator': None,
            'state': 'unassigned', 'policy': 'registered_runtime_actor.v1', 'effective_at': when,
            'scope': 'actor', 'context': {'node': node, 'entrypoint': channel, 'workspace_ref': None}},
            occurred_at=when, observed_at=observed, producer='registered_runtime_actor.v1', evidence=tuple(sorted(evidence))))
    return result
