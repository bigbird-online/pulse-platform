"""Pi purpose follows an exact native request, as CC does.

The Relay Pi extension offers `declare_session_wi`. Its call and paired result
are recorded in the Pi session; the collector reads them there, never the
Agent's prose. A request is the nearest user message on the entry's parentId
chain. Each request needs its own declaration; a refused call declares nothing.
Raw usage and Relay execution bindings stay intact.
"""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re

from .events import parse_timestamp
from .receipts import Receipt, ReceiptError, current_receipts, digest, reference

POLICY = 'pi_native_request_purpose.v1'
TOOL = 'declare_session_wi'
SCHEMA = 'pi_request_wi/v1'
WI = re.compile(r'wi_[0-9]{8}_[0-9]{3}')


def transport(wi):
    """The exact tool result the Relay extension returns."""
    return {'schema': SCHEMA, 'wi': wi, 'origin': 'agent_declaration'}


def _text(message):
    content = message.get('content')
    if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and content[0].get('type') == 'text' and isinstance(content[0].get('text'), str)):
        return content[0]['text']
    return None


def request_bindings(paths, *, since, diagnostics=None):
    """Map each Pi usage entry to the WI its own request declared.

    Returns {(session, entry_id, activity): {'request': ..., 'wis': set, 'evidence': tuple}}
    for every assistant or compaction entry whose request is proven; unproven
    entries are absent.
    """
    cutoff = parse_timestamp(since, 'Pi declaration admission')
    output = {}
    for path in sorted({Path(p) for p in paths}):
        if path.is_symlink() or not path.is_file():
            raise ReceiptError('Pi request purpose source is indirect')
        rows = [json.loads(raw) for raw in path.read_bytes().splitlines()
                if raw.strip() and raw.endswith(b'}')]
        if not rows or rows[0].get('type') != 'session' or not rows[0].get('id'):
            continue
        session = rows[0]['id']
        entries, calls, results = {}, {}, {}
        for row in rows[1:]:
            ident = row.get('id')
            if not isinstance(ident, str) or not ident:
                continue
            if ident in entries and digest(entries[ident]) != digest(row):
                entries[ident] = None  # A conflicting copy proves nothing.
                continue
            entries[ident] = row
            message = row.get('message') if row.get('type') == 'message' else None
            if not isinstance(message, dict):
                continue
            if message.get('role') == 'assistant' and isinstance(message.get('content'), list):
                for block in message['content']:
                    if isinstance(block, dict) and block.get('type') == 'toolCall' and block.get('name') == TOOL:
                        calls[block.get('id')] = (ident, block)
            elif message.get('role') == 'toolResult' and message.get('toolName') == TOOL:
                results.setdefault(message.get('toolCallId'), []).append(ident)

        # Entries are final from here on; hash each record once, not once per
        # descendant whose request walk passes through it.
        record_refs = {}

        def record_ref(key):
            value = record_refs.get(key)
            if value is None:
                value = record_refs[key] = reference('native_record', digest(entries[key]))
            return value

        def request(start):
            key, seen = start, set()
            while key in entries and key not in seen:
                seen.add(key)
                row = entries[key]
                if row is None:
                    return None
                message = row.get('message') if row.get('type') == 'message' else None
                if isinstance(message, dict) and message.get('role') == 'user':
                    return key, tuple(sorted(record_ref(k) for k in seen))
                # A compaction edits the context inside the running request; its
                # parent is that request's previous entry. A branch summary moves
                # to another branch and ends the proven chain.
                if row.get('type') not in {'message', 'model_change', 'thinking_level_change', 'custom', 'compaction'}:
                    return None
                key = row.get('parentId')
            return None

        declared = defaultdict(set)
        evidence = defaultdict(set)
        for call_id, (use_id, block) in calls.items():
            paired = results.get(call_id, [])
            if len(paired) != 1 or entries.get(paired[0]) is None or entries.get(use_id) is None:
                continue
            result_row = entries[paired[0]]
            result = result_row['message']
            if result.get('isError') is True:
                if diagnostics is not None:
                    diagnostics.append('pi_declaration_refused')
                continue
            arguments = block.get('arguments')
            if not isinstance(arguments, dict) or set(arguments) != {'wi'} or not isinstance(arguments['wi'], str) \
                    or (arguments['wi'] != 'none' and WI.fullmatch(arguments['wi']) is None):
                continue
            try:
                declared_at = parse_timestamp(result_row['timestamp'], 'Pi declaration')
                if declared_at < cutoff:
                    continue
                ack = json.loads(_text(result) or 'null')
            except (KeyError, ReceiptError, ValueError, TypeError):
                continue
            if ack != transport(arguments['wi']):
                if diagnostics is not None:
                    diagnostics.append('pi_declaration_result_mismatch')
                continue
            # Pi chains parallel results one after another; the pair is the
            # call id, and both records must belong to the same request.
            bound = request(use_id)
            result_bound = request(paired[0])
            if bound is None or result_bound is None or result_bound[0] != bound[0]:
                continue
            declared[bound[0]].add((declared_at, arguments['wi']))
            evidence[bound[0]].update({record_ref(use_id), record_ref(paired[0]), *bound[1]})
        for ident, row in entries.items():
            message = row.get('message') if row and row.get('type') == 'message' else None
            if isinstance(message, dict) and message.get('role') == 'assistant':
                activity = 'assistant'
            elif row and row.get('type') == 'compaction' and row.get('usage') is not None:
                activity = 'compaction'  # The summary is a model call of its request.
            else:
                continue
            bound = request(ident)
            if bound is None:
                continue
            key = bound[0]
            claims = declared.get(key, set())
            wis = {wi for _, wi in claims if wi != 'none'}
            # none -> WI: a request first declared chat/read-only, then took
            # on a WI. Usage before that WI's first declaration stays none.
            first_wi = min((at for at, wi in claims if wi != 'none'), default=None)
            none_from = min((at for at, wi in claims if wi == 'none'), default=None)
            output[(session, ident, activity)] = {
                'request_ref': reference('pi_native_request', [session, key, digest(entries[key])]),
                'wis': wis or ({'none'} if none_from is not None else set()),
                'none_until': first_wi if none_from is not None and wis and none_from < first_wi else None,
                'evidence': tuple(sorted(evidence.get(key, set()) | set(bound[1]))),
            }
    return output


def request_attributions(facts, usages, bindings, observed):
    """Append a request-qualified WI claim for each declared Pi usage."""
    # read_pi names a usage by its native session, entry and activity. A
    # recorded input (request_inputs.pi_bindings) is already keyed by usage.
    by_usage = {(reference('usage', list(key)) if isinstance(key, tuple) else key): binding
                for key, binding in bindings.items()}
    targets = {}
    for row in usages:
        if row.kind != 'usage' or row.payload.get('runtime') != 'pi':
            continue
        binding = by_usage.get(row.entity_ref)
        if binding is None or not binding['wis']:
            continue  # An undeclared request stays with its existing evidence.
        start = row.payload.get('interval_start') or row.occurred_at
        if start:
            targets[row.entity_ref] = (binding, start)
    entities = {reference('attribution', [POLICY, target]): target for target in targets}
    active, conflicts = current_receipts([r for r in facts if r.entity_ref in entities])
    heads = {r.entity_ref: [r] for r in active}
    heads.update(conflicts)
    output = []
    for entity, target in entities.items():
        binding, start = targets[target]
        wis = binding['wis']
        until = binding.get('none_until')
        if until is not None and parse_timestamp(start, 'Pi usage') < until:
            wis = {'none'}
        payload = {'target_ref': target, 'wi': next(iter(wis)) if len(wis) == 1 else None,
                   'executor': None, 'responsible': None, 'delegator': None,
                   'state': 'assigned' if len(wis) == 1 else 'ambiguous',
                   'policy': POLICY, 'effective_at': start}
        if payload['wi'] == 'none':
            # Chat or read-only work, declared as such: ordinary non-WI activity.
            payload.update(wi=None, policy='non_wi_activity.v1', work_kind='none')
        proofs = {binding['request_ref'], *binding['evidence']}
        prior = heads.get(entity, [])
        if len(prior) > 1 or any(r.producer != POLICY for r in prior):
            continue
        if prior and prior[0].payload == payload and proofs <= set(prior[0].evidence):
            continue
        predecessor = prior[0] if prior else None
        output.append(Receipt.make('attribution', None, payload, entity_ref=entity,
            supersedes=predecessor.fact_ref if predecessor else None, occurred_at=start,
            observed_at=observed, producer=POLICY,
            evidence=tuple(sorted(proofs | (set(predecessor.evidence) if predecessor else set())))))
    return output
