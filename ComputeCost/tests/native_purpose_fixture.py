"""Synthetic native-stream fixtures, never a production transcript or declaration."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import shlex
from unittest.mock import patch
from pulse_compute_cost import native_request_purpose as n
from pulse_compute_cost.receipts import reference, digest
from pulse_compute_cost.adapters import _stable_event_id


def capture(payload, root):
    with patch.object(n, 'utc_now', return_value='2026-01-01T01:00:00Z'):
        return n.capture(payload, root)


def declare(root, target, request, purpose, *, session):
    """Exercise the stateless producer, then provide its native result as a fixture."""
    original = n._input(n._read(Path(root)/target/(request+'.input.json')))
    value = n.declaration_message(target, request, original['native_scope_ref'], digest(original), purpose, session=session)
    flags = ['--target', target, '--request', request, '--scope', original['native_scope_ref'],
             '--input-sha256', digest(original), '--purpose', purpose]
    command = shlex.join(['/fixture/bin/python', '/fixture/ComputeCost/runtime/codex-request-purpose.py', 'declare', *flags])
    for path in sorted((Path(root).parent/'logs').glob('*.jsonl')):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        if not rows or rows[0].get('payload', {}).get('id') != session:
            continue
        for position, row in enumerate(rows):
            p = row.get('payload', {})
            if row.get('type') != 'event_msg' or p.get('type') != 'task_started':
                continue
            identity = reference('usage', _stable_event_id('codex', 'registered-source', session+':'+p['turn_id']))
            if identity != target:
                continue
            at = (datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))+timedelta(seconds=1)).isoformat().replace('+00:00', 'Z')
            call = 'fixture-purpose-' + digest([request, purpose])
            pair = [
                {'type': 'response_item', 'timestamp': at, 'payload': {'type': 'function_call',
                 'name': 'exec_command', 'call_id': call, 'arguments': json.dumps({'cmd': command})}},
                {'type': 'response_item', 'timestamp': at, 'payload': {'type': 'function_call_output',
                 'call_id': call, 'output': json.dumps(value)}}]
            if all(item in rows for item in pair):
                return value  # Re-read the same native event, not another tool execution.
            end = next(i for i in range(position+1, len(rows)) if rows[i].get('payload', {}).get('type') in {'token_count', 'task_complete'})
            rows[end:end] = pair
            path.write_text(''.join(json.dumps(item)+'\n' for item in rows))
            return value
    raise AssertionError('fixture has no corresponding native turn')
