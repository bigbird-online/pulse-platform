"""Preserve a proven native parent entrypoint without inheriting WI or actor."""
from collections import defaultdict
from .receipts import reference



def native_codex_entrypoint(originator, source):
    """Native software origin; Desktop exec does not imply human interaction.

    A spawned Codex subagent records ``source`` as an object
    (``{"subagent": {"thread_spawn": {"parent_thread_id": ...}}}``). It has no
    entrypoint of its own; its trigger is inherited only through the verified
    parent edge in ``codex_entrypoint_origins``.
    """
    if not isinstance(source, str):
        return None
    if originator == 'Codex Desktop' and source in {'vscode', 'exec'}:
        return 'codex_app'
    if source == 'cli' or source == 'exec' and originator == 'codex_exec':
        return 'cli'
    return None


def codex_entrypoint_origins(observations):
    choices = defaultdict(set)
    proofs = defaultdict(set)
    for row in observations:
        choices[row.session_id].add((row.originator, row.native_source, row.parent_session_id))
        proofs[row.session_id].add(row.startup_ref)
    unique = {sid: next(iter(values)) for sid, values in choices.items() if len(values) == 1}
    result = {}
    for sid in unique:
        cursor = sid
        seen = set()
        evidence = set()
        for _ in range(16):
            if cursor in seen or cursor not in unique:
                break
            seen.add(cursor)
            originator, source, parent = unique[cursor]
            if not proofs[cursor] or None in proofs[cursor]:
                break
            evidence.update(proofs[cursor])
            if parent is None:
                entry = native_codex_entrypoint(originator, source)
                if entry:
                    result[sid] = (entry, tuple(sorted(evidence)))
                break
            # A child has its own native identity. Only its original trigger
            # crosses this edge, never a WI, quantity, price or actor identity.
            if source is not None:
                break
            evidence.add(reference('codex_native_parent', [cursor, parent]))
            cursor = parent
    return result
