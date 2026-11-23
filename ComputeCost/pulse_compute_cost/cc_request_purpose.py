"""CC purpose follows an exact native request, never a persistent session clock.

Only paired successful MCP declarations and verified native ancestry provide
purpose. A fresh input needs its own declaration; a child or runtime completion
must return to its unique original prompt. Raw usage and declarations stay intact.
"""
from collections import defaultdict
from pathlib import Path
import json

from .cc_input_context import _conflicting_copy, _native_parent, _request_text, _task_notifications, _task_origin
from .cc_lineage import _main_root_session, _sidechain_root_session
from .child_work_items import dispatch_marker, lineage_child
from .events import parse_timestamp
from .receipts import Receipt, ReceiptError, current_receipts, digest, reference

# A subagent dispatched to an open child of the declared WI (first prompt line
# `[WI: <child>]`, see child_work_items) is attributed to that child under this
# same policy; its evidence then names `cc_child_work_item`.
POLICY = 'cc_native_request_purpose.v1'


def _coordinator_input(row):
    """A message the main agent sent a running or resumed subagent (SendMessage).

    CC writes it into the subagent's chain as a meta input carrying the
    promptId of the main request that sent it; the subagent's work after it
    continues that request, not the one that first launched the subagent.
    """
    return (row.get('type') == 'user' and row.get('isSidechain') is True and row.get('isMeta') is True
            and row.get('origin') == {'kind': 'coordinator'}
            and isinstance(row.get('promptId'), str) and bool(row['promptId']))


def request_bindings(paths, usages, declarations, diagnostics=None):
    """Qualify declarations by actual input/tool/turn identity, not latest WI.

    Historical sessions without request evidence retain their legacy reader.
    Once native input evidence exists, all observations in that session use
    request boundaries, including unclassified or unproved requests. A partial
    legacy declaration cannot restore whole-session purpose for new events.
    A new request ack without usable boundaries never gains session scope.
    """
    from .mcp_request_purpose import CC_TOOL, decode_cc_completion, CC_REQUEST_TRANSPORT_SCHEMA, PurposeBindingError
    nodes, roles, bad = {}, {}, set()
    positions, messages, prompts = defaultdict(dict), defaultdict(set), defaultdict(set)
    uses, results = defaultdict(list), defaultdict(list)
    # Each subagent file's first record: its dispatch prompt, when it is one.
    first_prompts = {}
    admitted = {d['native_evidence_ref']: d for ds in declarations.values() for d in ds}
    scopes = {scope for scope, ds in declarations.items() if ds}
    for path in sorted(set(map(Path, paths))):
        sid = path.parent.parent.name if path.parent.name == 'subagents' else path.parent.name if path.name == 'main.jsonl' else path.stem
        if reference('native_session', ['cc', sid]) not in scopes:
            continue
        if path.is_symlink() or not path.is_file():
            raise ReceiptError('native request purpose source is indirect')
        for ordinal, raw in enumerate(path.read_bytes().splitlines(keepends=True)):
            if not raw.endswith(b'\n'):
                break
            if not raw.strip():
                # CC may leave an empty separator between complete records; it
                # carries no evidence and must not void every declaration.
                continue
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ReceiptError('invalid native request purpose record')
            uid, side, session = row.get('uuid'), row.get('isSidechain'), row.get('sessionId')
            root = (_sidechain_root_session(path, row.get('agentId', '')) if side is True else
                    _main_root_session(path, session) if side is False else None)
            if root != sid or not isinstance(uid, str) or not uid:
                continue
            key = (root, uid)
            if side is True and path not in first_prompts:
                first_prompts[path] = ((dispatch_marker(_request_text(row)), key)
                                       if row.get('parentUuid') is None else (None, None))
            positions[key][path] = ordinal
            if key in nodes and _conflicting_copy(nodes[key], row, roles[key], side,
                                                  any(p != path for p in positions[key])):
                bad.add(key)
            nodes[key], roles[key] = row, side
            msg = row.get('message') or {}; blocks = msg.get('content')
            if row.get('type') == 'assistant' and msg.get('usage') is not None:
                messages[reference('usage', [session, msg.get('id') or uid])].add(key)
            if (_request_text(row) is not None and not side and not row.get('isCompactSummary')
                    and isinstance(row.get('promptId'), str) and row['promptId']):
                prompts[(root, row['promptId'])].add(key)
            if isinstance(blocks, list):
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if row.get('type') == 'assistant' and block.get('type') == 'tool_use' and block.get('name') == CC_TOOL:
                        uses[(root, block.get('id'))].append((key, block))
                    elif row.get('type') == 'user' and block.get('type') == 'tool_result':
                        results[(root, block.get('tool_use_id'))].append((key, block))
    notifications = _task_notifications(nodes, roles, positions, bad)
    # Records are final from here on. Each request walk revisits its whole
    # ancestry; hash every record once instead of once per descendant walk.
    record_refs = {}

    def record_ref(key):
        value = record_refs.get(key)
        if value is None:
            value = record_refs[key] = reference('native_record', digest(nodes[key]))
        return value

    def precedes(parent, child):
        # CC may append a subagent's context attachment after the record that
        # names it as parent; the native timestamps still order them.
        if any(positions[parent].get(p, n) < n for p, n in positions[child].items()):
            return True
        try:
            return (parse_timestamp(nodes[parent]['timestamp'], 'parent record')
                    <= parse_timestamp(nodes[child]['timestamp'], 'child record'))
        except (KeyError, ReceiptError, TypeError, ValueError):
            return False

    # Each record's nearest text ancestor by proven parent edges. The edges do
    # not depend on the observation being bound, so all records of a long
    # request share one walk instead of each repeating it.
    text_ancestor = {}

    def nearest_text(key):
        stretch, visited = [], set()
        while (key in nodes and key not in text_ancestor and key not in bad
               and _request_text(nodes[key]) is None and not _coordinator_input(nodes[key])):
            stretch.append(key); visited.add(key); row = nodes[key]
            parent = (key[0], row.get('parentUuid'))
            # A forked subagent continues its parent agent's copied records;
            # a deferred-tools record CC writes after its reply still parents it.
            if (parent not in nodes or roles[parent] != roles[key]
                    or (nodes[parent].get('agentId') != row.get('agentId')
                        and _native_parent(nodes, roles, positions, key) != parent)
                    or not (precedes(parent, key) or _native_parent(nodes, roles, positions, key) == parent)
                    or parent in visited):
                key = None
                break
            key = parent
        found = (text_ancestor[key] if key in text_ancestor else
                 key if key in nodes and key not in bad else None)
        for item in stretch:
            text_ancestor[item] = found
        return found

    def request(start):
        """The request input `start` belongs to and the records proving it.

        Evidence names the observation, the input and each notification or
        continuation hop; the parent chain between them is the transcript's
        own order.
        """
        key, seen = nearest_text(start), {start}
        while key is not None and key not in seen:
            seen.add(key); row = nodes[key]
            if key in notifications:
                key = nearest_text(notifications[key])
                continue
            if _task_origin(row.get('origin')):
                return None
            # A sidechain input and a compaction summary both continue the
            # request whose promptId they carry; CC writes the in-flight
            # prompt's id on the summary. Neither is a new request.
            if roles[key] or row.get('isCompactSummary'):
                candidates = prompts.get((key[0], row.get('promptId')), set())
                if len(candidates) != 1:
                    return None
                parent = next(iter(candidates))
                if parent in bad or parse_timestamp(nodes[parent]['timestamp'], 'parent request') > parse_timestamp(row['timestamp'], 'child request'):
                    return None
                key = parent
                continue
            prompt = row.get('promptId')
            if not isinstance(prompt, str) or not prompt or len(prompts[(key[0], prompt)]) != 1:
                return None
            if parse_timestamp(row['timestamp'], 'request') > parse_timestamp(nodes[start]['timestamp'], 'native observation'):
                return None
            return key, tuple(sorted(record_ref(k) for k in seen))
        return None

    def dispatched(keys):
        """The child WI the subagent of these records was dispatched to, with its prompt.

        Every record must be a sidechain one whose files all start with the
        same marked prompt; a fork's copied prompt is that same record.
        """
        found = set()
        for key in keys:
            if roles.get(key) is not True:
                return None
            found.update(first_prompts.get(path, (None, None)) for path in positions[key])
        if len(found) != 1:
            return None
        marker, prompt = next(iter(found))
        return (marker, record_ref(prompt)) if marker is not None and prompt not in bad else None

    qualified = defaultdict(dict)
    by_request = defaultdict(list)
    for call, found in uses.items():
        paired = results.get(call, [])
        if len({digest(nodes[k]) for k, _ in found}) != 1 or len({digest(nodes[k]) for k, _ in paired}) != 1:
            continue
        (use_key, use), (result_key, result) = found[0], paired[0]
        if use_key in bad or result_key in bad or roles[use_key] or roles[result_key]:
            continue
        try:
            decoded = decode_cc_completion(nodes[use_key], use, nodes[result_key], result)
        except PurposeBindingError:
            continue
        d = admitted.get(decoded['native_evidence_ref']); bound = request(use_key)
        if (d is None or bound is None or nodes[result_key].get('parentUuid') != use_key[1]
                or nodes[result_key].get('sourceToolAssistantUUID') != use_key[1]):
            continue
        request_key, ancestry = bound
        prompt = nodes[request_key]['promptId']
        if nodes[result_key].get('promptId') != prompt:
            continue
        request_ref = reference('cc_native_request', [request_key[0], request_key[1], prompt,
                                record_ref(request_key)])
        proof = {**d, 'request_ref': request_ref, 'request_at': nodes[request_key]['timestamp'],
                 'tool_scope_ref': reference('cc_native_purpose_tool', [use_key, result_key, call[1], d['declared_at']]),
                 'request_record_ref': record_ref(request_key),
                 'ancestry_refs': ancestry}
        qualified[d['native_scope_ref']][d['native_evidence_ref']] = proof
        by_request[request_key].append(proof)
    scoped = {reference('native_session', ['cc', key[0]]) for key, row in nodes.items()
              if roles[key] is False and _request_text(row) is not None
              and not row.get('isCompactSummary') and key not in notifications}
    if diagnostics is not None:
        for scope in sorted(scoped):
            missing = {d['native_evidence_ref'] for d in declarations.get(scope, [])} - set(qualified[scope])
            if missing:
                diagnostics.append('cc_request_declaration_boundary_unproved')
    # A request protocol cannot fall back to legacy whole-session semantics.
    scoped.update(scope for scope, ds in declarations.items()
                  if any(d.get('schema') == CC_REQUEST_TRANSPORT_SCHEMA for d in ds))
    output = {}
    for usage in usages:
        if usage.kind != 'usage' or usage.payload.get('native_scope_ref') not in scoped:
            continue
        entries = messages.get(usage.entity_ref, set())
        choices = [request(k) for k in entries]
        keys = {c[0] for c in choices if c is not None}
        bound = next(iter(keys)) if len(keys) == 1 and len(choices) == len([c for c in choices if c is not None]) else None
        evidence = {proof for c in choices if c is not None for proof in c[1]}
        ds = by_request.get(bound, []) if bound is not None else []
        output[usage.entity_ref] = {'declarations': ds, 'ancestry_refs': tuple(sorted(evidence)),
                                  'qualified_declaration_refs': tuple(sorted(qualified[usage.payload['native_scope_ref']])),
                                  'request_record_ref': record_ref(bound) if bound else None,
                                  'child_marker': dispatched(entries) if entries else None,
                                  'request_ref': ds[0]['request_ref'] if ds else
                                  reference('cc_native_request_unclassified', [bound]) if bound else None}
    return output


def request_attributions(facts, usages, bindings, declarations, observed, children=None):
    """Audit our proven session overreach, preserving other producers' claims.

    `children` (child_work_items.read_children) lets a subagent dispatched to
    an open child of the declared WI take that child; without it, it inherits.
    """
    from .mcp_request_purpose import NO_WI, NO_WI_POLICY, POLICY as LEGACY_POLICY
    versions = defaultdict(list)
    for row in usages:
        if row.kind == 'usage' and row.entity_ref in bindings:
            versions[row.entity_ref].append(row)
    entities = {reference('attribution', [policy, target]) for policy in (POLICY, LEGACY_POLICY) for target in versions}
    active, conflicts = current_receipts([r for r in facts if r.entity_ref in entities])
    heads = {r.entity_ref: [r] for r in active}; heads.update(conflicts)
    boundaries = defaultdict(set)
    for row in facts + usages:
        if row.kind == 'usage' and row.entity_ref in versions:
            boundary = row.payload.get('interval_start') or row.occurred_at
            if boundary:
                boundaries[row.entity_ref].add(boundary)
    output = []
    for target, group in versions.items():
        scopes = {r.payload.get('native_scope_ref') for r in group}
        if len(scopes) != 1 or not boundaries[target]:
            continue
        scope = next(iter(scopes)); binding = bindings[target]; ds = binding['declarations']
        # Observations may write one instant differently; choose one text
        # deterministically, or each collection would revise the claim again.
        start = min(boundaries[target], key=lambda t: (parse_timestamp(t, 'native boundary'), t))
        witness = reference('native_session_usage', [scope, target])
        all_proofs = {reference('native_session_wi', [scope, d['wi'], d['native_evidence_ref']]): d['wi']
                      for d in declarations.get(scope, [])}
        entity = reference('attribution', [LEGACY_POLICY, target]); prior = heads.get(entity, [])
        def valid_legacy(row):
            p = row.payload; proofs = set(row.evidence) & set(all_proofs)
            wis = {all_proofs[ref] for ref in proofs}
            expected = 'assigned' if len(wis) == 1 else 'ambiguous' if wis else 'unassigned'
            return (row.producer == LEGACY_POLICY and p.get('policy') == LEGACY_POLICY
                    and set(p) == {'target_ref','wi','executor','responsible','delegator','state','policy','effective_at'}
                    and p['target_ref'] == target and witness in row.evidence
                    and binding['request_ref'] is not None
                    and all(d['native_evidence_ref'] in binding.get('qualified_declaration_refs', ())
                            for d in declarations.get(scope, [])
                            if reference('native_session_wi', [scope, d['wi'], d['native_evidence_ref']]) in proofs)
                    and p['effective_at'] in boundaries[target]
                    and all(p[k] is None for k in ('executor','responsible','delegator'))
                    and (p['state'] == 'unassigned' and p['wi'] is None or
                         p['state'] == expected and (p['wi'] == next(iter(wis)) if len(wis) == 1 else p['wi'] is None)))
        # A later declaration in the same request switches its WI from then
        # on (an Agent that creates a WI mid-request declares it). Usage
        # before the first declaration belongs to that first one.
        if ds:
            at = parse_timestamp(start, 'native boundary')
            moments = sorted({parse_timestamp(d['declared_at'], 'declaration') for d in ds})
            current = max((m for m in moments if m <= at), default=moments[0])
            ds = [d for d in ds if parse_timestamp(d['declared_at'], 'declaration') == current]
        wis = {d['wi'] for d in ds}
        state = 'assigned' if len(wis) == 1 else 'ambiguous' if wis else 'unassigned'
        payload = {'target_ref': target, 'wi': next(iter(wis)) if len(wis) == 1 else None,
                   'executor': None, 'responsible': None, 'delegator': None, 'state': state,
                   'policy': POLICY, 'effective_at': start}
        if payload['wi'] == NO_WI:
            payload.update(wi=None, policy=NO_WI_POLICY, work_kind=NO_WI)
        proofs = {witness, reference('cc_native_request_usage', [scope, target, binding['request_ref']]),
                  *binding['ancestry_refs']}
        # Preserve the parser's exact input-record/request relationship as an
        # opaque witness. Mere membership in the ancestry cannot prove which
        # record was the actual request. Older source assertions stay intact.
        request_record = binding.get('request_record_ref')
        if binding['request_ref'] and request_record in binding['ancestry_refs']:
            proofs.add(reference('cc_native_request_record',[binding['request_ref'],request_record]))
        for d in ds:
            proofs.update({d['native_evidence_ref'], d['request_ref'], d['request_record_ref'], d['tool_scope_ref'], *d['ancestry_refs']})
        marker = binding.get('child_marker')
        if (marker and payload['policy'] == POLICY and payload['wi']
                and lineage_child(children or {}, marker[0], payload['wi'], parse_timestamp(start, 'native boundary'))):
            proofs.add(reference('cc_child_work_item', [marker[0], payload['wi'], marker[1]]))
            payload['wi'] = marker[0]
        # Keep the collector's stable per-observation entity. A policy
        # revision explicitly supersedes only its own proven legacy claim;
        # an extra unknown claim would conflict rather than retract overreach.
        entity = reference('attribution', [LEGACY_POLICY, target]); prior = heads.get(entity, [])
        legacy_revision = prior and all(valid_legacy(r) for r in prior)
        if not legacy_revision and (len(prior) > 1 or any(
                r.producer != POLICY or r.payload.get('policy') not in (POLICY, NO_WI_POLICY) for r in prior)):
            continue
        if prior and all(r.payload == payload and proofs <= set(r.evidence) for r in prior):
            continue
        for predecessor in prior or [None]:
            output.append(Receipt.make('attribution', None, payload, entity_ref=entity,
                supersedes=predecessor.fact_ref if predecessor else None, occurred_at=start, observed_at=observed,
                producer=POLICY, evidence=tuple(sorted(proofs | (set(predecessor.evidence) if predecessor else set())))))
    return output
