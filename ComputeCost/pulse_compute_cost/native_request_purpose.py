"""Native per-request declarations; no default WI, execution grant or live ledger write.

The native Hook captures input identity. The working Agent returns a stateless
declaration on its native tool channel; it never reads or writes the private
Host spool. The collector checks the actual call/result and captured input.
Neither source is a human acceptance receipt; original text is not retained.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile
from .adapters import _stable_event_id
from .receipts import Receipt, ReceiptError, current_receipts, digest, reference

POLICY = 'codex_request_purpose.v1'
INPUT_SCHEMA = 'codex_request_input/v1'
NATIVE_INPUT_SCHEMA = 'codex_native_request_input/v1'
DECLARATION_SCHEMA = 'codex_request_declaration/v1'
REF = re.compile(r'ccf1_[0-9a-f]{64}')
WI = re.compile(r'wi_[0-9]{8}_[0-9]{3}')
MAX_BYTES = 32768
MAX_INPUTS_PER_TURN = 128


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def default_root():
    return Path.home() / '.local/state/pulse/compute-cost/native-requests'


def _directory(path, *, create=False):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ReceiptError('native request input directory is indirect')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ReceiptError('native request inputs must be private and locally owned')
    return path


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        meta = os.fstat(stream.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
            raise ReceiptError('native request input is not a private regular file')
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ReceiptError('native request input exceeds its bound')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ReceiptError('native request input must be an object')
    return value


def _once(path, value):
    """Atomic create; retries return original evidence, never latest-write-wins."""
    descriptor, temporary = tempfile.mkstemp(prefix='.input-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            old = _read(path)
            if {k:v for k,v in old.items() if k not in {'captured_at','declared_at'}} != {
                    k:v for k,v in value.items() if k not in {'captured_at','declared_at'}}:
                raise ReceiptError('the original request declaration conflicts') from None
            return old
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return value
    finally:
        os.unlink(temporary)


def capture(payload, root):
    """Called only by native UserPromptSubmit, before any purpose selection."""
    if not isinstance(payload, dict):
        raise ReceiptError('native Hook envelope is not an object')
    if payload.get('hook_event_name') != 'UserPromptSubmit':
        return None
    session, turn, prompt = (payload.get(k) for k in ('session_id','turn_id','prompt'))
    if (any(not isinstance(v, str) or not v or len(v) > 256 for v in (session,turn))
            or not isinstance(prompt, str) or len(prompt.encode()) > 1024 * 1024):
        raise ReceiptError('native request identity is unavailable')
    target = reference('usage', _stable_event_id('codex','registered-source',session+':'+turn))
    scope = reference('native_session',['codex',session])
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    identity = reference('codex_request_input',[target,scope,prompt_hash])
    directory = _directory(_directory(root, create=True) / target, create=True)
    return _once(directory/(identity+'.input.json'), {
        'schema':INPUT_SCHEMA, 'request_ref':identity, 'target_ref':target,
        'native_scope_ref':scope, 'prompt_sha256':prompt_hash, 'captured_at':utc_now()})


def _input(value):
    if not isinstance(value, dict):
        raise ReceiptError('native request input must be an object')
    native = value.get('schema') == NATIVE_INPUT_SCHEMA
    fields = {'schema','request_ref','target_ref','native_scope_ref','captured_at'}
    fields |= {'native_item_ref','content_sha256'} if native else {'prompt_sha256'}
    refs = ('request_ref','target_ref','native_scope_ref') + (('native_item_ref',) if native else ())
    hashed = 'content_sha256' if native else 'prompt_sha256'
    if (set(value) != fields or value.get('schema') not in {INPUT_SCHEMA,NATIVE_INPUT_SCHEMA}
            or any(not isinstance(value.get(k),str) or not REF.fullmatch(value[k]) for k in refs)
            or not isinstance(value.get(hashed),str) or not re.fullmatch(r'[0-9a-f]{64}',value[hashed])):
        raise ReceiptError('native request input identity differs')
    identity = reference('codex_native_request_input' if native else 'codex_request_input',
        [value['target_ref'],value['native_scope_ref'],value['native_item_ref' if native else 'prompt_sha256']])
    if identity != value['request_ref']:
        raise ReceiptError('native request input identity differs')
    from .events import parse_timestamp
    parse_timestamp(value['captured_at'],'native input capture')
    return value


def declaration_message(target, request, scope, input_sha256, purpose, *, session):
    """Return a declaration to the native tool channel; never access the filesystem.

    This is an Agent statement, not an authenticated human decision. The actual
    collector independently joins it to the captured input and native call/result.
    """
    if (any(not isinstance(v, str) or not REF.fullmatch(v) for v in (target, request, scope))
            or not isinstance(input_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', input_sha256)
            or not isinstance(purpose, str) or not (purpose == 'none' or WI.fullmatch(purpose))
            or not isinstance(session, str) or not session
            or reference('native_session', ['codex', session]) != scope):
        raise ReceiptError('an exact current native request and explicit purpose are required')
    return {'schema': DECLARATION_SCHEMA, 'request_ref': request, 'target_ref': target,
            'native_scope_ref': scope, 'input_sha256': input_sha256,
            'purpose': purpose, 'origin': 'agent_declaration'}


def _declaration_command(command):
    """Recognize the exact plain frontend call, not shell code or quoted examples."""
    if not isinstance(command, str) or len(command) > MAX_BYTES or any(c in command for c in ('\n', '$', '`')):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()')
        lexer.whitespace_split = True
        args = list(lexer)
    except ValueError:
        return None
    if len(args) >= 4 and args[0] == 'cd' and Path(args[1]).is_absolute() and args[2] == '&&':
        args = args[3:]
    if len(args) > 1 and args[1] == '-B':
        args.pop(1)
    if (len(args) != 13 or not Path(args[0]).is_absolute()
            or not re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?', Path(args[0]).name)
            or not Path(args[1]).is_absolute() or Path(args[1]).name != 'codex-request-purpose.py'
            or args[2] != 'declare' or any(a and all(c in ';&|<>()' for c in a) for a in args)):
        return None
    fields = {}
    for flag, value in zip(args[3::2], args[4::2]):
        if flag not in {'--target', '--request', '--scope', '--input-sha256', '--purpose'} or flag in fields:
            return None
        fields[flag[2:].replace('-', '_')] = value
    return fields if len(fields) == 5 else None



def _leading_declaration_command(call):
    """The first awaited, directly printed command is independent of its tail.

    Do not evaluate JS or search later statements. The paired native result
    must occupy the first printed tool-result slot as well.
    """
    if call.get('type') != 'custom_tool_call' or call.get('name') != 'exec':
        return None
    script = call.get('input')
    if not isinstance(script, str):
        return None
    pattern = r'\s*text\(\s*await\s+tools\.exec_command\(\{((?:"(?:[^"\\]|\\.)*"|[^"{}])*)\}\)\s*\)\s*;'
    match = re.match(pattern, script[:MAX_BYTES], re.S)
    if match is None:
        return None
    from .runtime_subcalls import _literal_object
    args = _literal_object(match[1])
    command = args.get('cmd') if args else None
    return command if _declaration_command(command) is not None else None


def _leading_exec_texts(value):
    """Read one positional completed exec result, not a matching later print.

    Native code-mode returns its framing text followed by printed values.
    Unknown layouts, a failed first command or a later lookalike stay unknown.
    """
    if not isinstance(value, list) or len(value) < 2:
        return []
    frame, first = value[:2]
    for block in (frame, first):
        if (not isinstance(block, dict) or set(block) != {'type', 'text'}
                or block['type'] not in {'input_text', 'text'} or not isinstance(block['text'], str)):
            return []
    try:
        json.loads(frame['text'])
    except ValueError:
        pass
    else:
        return []  # A printed JSON value is not the native framing slot.
    if len(first['text']) > MAX_BYTES:
        return []
    try:
        result = json.loads(first['text'])
    except ValueError:
        return []
    required = {'chunk_id', 'exit_code', 'output', 'wall_time_seconds'}
    if (not isinstance(result, dict) or not required <= set(result) <= required | {'original_token_count'}
            or type(result['exit_code']) is not int or result['exit_code'] != 0
            or not isinstance(result['chunk_id'], str) or not result['chunk_id']
            or not isinstance(result['output'], str)):
        return []
    return [result['output']]


def native_declarations(paths, usages):
    """Read exact native shell-call/result pairs, never assistant/user prose.

    Both physical layouts reuse the existing filename/logical-thread contract.
    Same-source native usage must actually be present. No transcript is executed
    and no free-form stdout or historical declaration file becomes authority.
    """
    from .events import parse_timestamp
    from .receipt_sources import _codex_rollout_file_ids
    from .runtime_subcalls import _commands, _result_texts
    targets = {r.entity_ref: r.payload.get('native_scope_ref') for r in usages
               if r.kind == 'usage' and r.payload.get('runtime') == 'codex'}
    scopes = set(targets.values()) - {None}
    if not scopes:
        return []
    calls, outputs = defaultdict(dict), defaultdict(dict)
    for raw_path in sorted({str(p) for p in paths}):
        path = Path(raw_path)
        if path.is_symlink() or not path.is_file():
            raise ReceiptError('native declaration source is indirect')
        ids = _codex_rollout_file_ids(path)
        if ids is None or reference('native_session', ['codex', ids[0]]) not in scopes:
            continue
        session, turn, header_seen = None, None, False
        with path.open('rb') as stream:
            for raw in stream:
                if not raw.endswith(b'\n'):
                    break  # Only the complete original native prefix is observed.
                if not any(t in raw for t in (b'"session_meta"', b'"task_started"', b'"task_complete"',
                        b'"task_aborted"', b'"function_call"', b'"function_call_output"',
                        b'"custom_tool_call"', b'"custom_tool_call_output"')):
                    continue
                row = json.loads(raw)
                if not isinstance(row, dict) or not isinstance(row.get('payload'), dict):
                    continue
                p = row['payload']
                if row.get('type') == 'session_meta':
                    # Native SessionMeta.session_id is the ROOT session;
                    # id is this thread. A child must not be relabeled as its root.
                    if (header_seen or p.get('id') != ids[0]
                            or (ids[0] != ids[1] and p.get('history_mode') != 'paginated')):
                        raise ReceiptError('native declaration thread identity conflicts')
                    session, header_seen = ids[0], True
                    continue
                if session is None:
                    continue
                scope = reference('native_session', ['codex', session])
                if row.get('type') == 'event_msg':
                    if p.get('type') == 'task_started':
                        turn = p.get('turn_id') if isinstance(p.get('turn_id'), str) else None
                    elif p.get('type') in {'task_complete', 'task_aborted'}:
                        turn = None
                    continue
                if row.get('type') != 'response_item' or not isinstance(p.get('call_id'), str):
                    continue
                key = (scope, p['call_id'])
                proof = reference('native_purpose_tool_record', digest(row))
                if p.get('type') in {'function_call', 'custom_tool_call'} and turn:
                    target = reference('usage', _stable_event_id('codex', 'registered-source', session + ':' + turn))
                    if targets.get(target) == scope:
                        calls[key][proof] = (target, p, row.get('timestamp'), proof)
                elif p.get('type') in {'function_call_output', 'custom_tool_call_output'}:
                    outputs[key][proof] = (p.get('output'), row.get('timestamp'), proof, turn)
    found = {}
    for key, versions in calls.items():
        # A reused call ID with different native calls cannot select a winner.
        if len(versions) != 1:
            continue
        target, call, started, call_proof = next(iter(versions.values()))
        commands = _commands(call)
        if not commands:
            leading = _leading_declaration_command(call)
            commands = [('leading_exec_0', leading)] if leading is not None else []
        for index, command in commands:
            selected = _declaration_command(command)
            if selected is None or selected['target'] != target or selected['scope'] != key[0]:
                continue
            expected = {'schema': DECLARATION_SCHEMA, 'request_ref': selected['request'],
                        'target_ref': target, 'native_scope_ref': key[0],
                        'input_sha256': selected['input_sha256'], 'purpose': selected['purpose'],
                        'origin': 'agent_declaration'}
            for output, ended, result_proof, _ in outputs.get(key, {}).values():
                try:
                    if parse_timestamp(started, 'native declaration call') > parse_timestamp(ended, 'native declaration result'):
                        continue
                except (ValueError, TypeError):
                    continue
                texts = _leading_exec_texts(output) if index == 'leading_exec_0' else _result_texts(output, index)
                for text in texts:
                    for line in text.splitlines():
                        if len(line) > MAX_BYTES:
                            continue
                        try:
                            value = json.loads(line)
                        except ValueError:
                            continue
                        if value != expected:
                            continue
                        proof = reference('native_request_declaration_result', [key, index, call_proof, result_proof])
                        found[proof] = {**value, 'declared_at': ended, 'native_evidence_ref': proof}
    return list(found.values())


def read_inputs(root, usages, *, paths=()):
    """Enrich only positively captured requests, not unrelated native history.

    Usage ingestion still reads the complete enrolled source. This optional
    purpose reader has no claim to make for an uncaptured request. Validate
    applicable private inputs first, then read their exact native threads.
    """
    from .events import parse_timestamp
    root = _directory(root)
    pending, selected = [], []
    for usage in usages:
        if usage.kind != 'usage' or usage.payload.get('runtime') != 'codex':
            continue
        directory = root / usage.entity_ref
        if not directory.exists() and not directory.is_symlink():
            continue
        _directory(directory)
        originals = sorted(directory.glob('*.input.json'))
        if len(originals) > MAX_INPUTS_PER_TURN:
            raise ReceiptError('native input count exceeds the per-turn bound')
        for path in originals:
            original = _input(_read(path))
            if (path.name != original['request_ref'] + '.input.json'
                    or original['target_ref'] != usage.entity_ref
                    or original['native_scope_ref'] != usage.payload.get('native_scope_ref')):
                raise ReceiptError('native input is attached to a different source request')
            pending.append((usage, original))
        if originals:
            selected.append(usage)
    if not pending:
        return []
    results = defaultdict(list)
    decisions = native_declarations(paths, selected)
    for decision in decisions:
        results[decision['request_ref']].append(decision)
    result = []
    for usage, original in pending:
        valid = []
        for decision in results.get(original['request_ref'], []):
            if (decision['target_ref'] != usage.entity_ref
                    or decision['native_scope_ref'] != original['native_scope_ref']
                    or decision['input_sha256'] != digest(original)
                    or not isinstance(decision['purpose'], str)
                    or not (decision['purpose'] == 'none' or WI.fullmatch(decision['purpose']))):
                continue
            if parse_timestamp(decision['declared_at'], 'native declaration') < parse_timestamp(original['captured_at'], 'capture'):
                continue
            valid.append(decision)
        result.extend((usage.entity_ref, original, decision) for decision in (valid or [None]))
    return result


def purpose_attributions(facts, inputs, observed):
    """Explicit per-request purpose versions only its own or a weak inference.

    Missing declarations are not ordinary work. New steering without a matching
    decision invalidates an earlier whole-turn claim. Independent WI/actor facts
    are not overwritten. No usage, counts, Agent identity or grant is created.
    """
    active, conflicts = current_receipts(facts)
    heads = {r.entity_ref:r for r in active}
    usages = {r.entity_ref:r for r in active if r.kind=='usage'}
    grouped = defaultdict(dict)
    for target, original, decision in inputs:
        if original is None and decision is not None:
            raise ReceiptError('a declaration requires a verified native input')
        key = original['request_ref'] if original is not None else None
        grouped[target][(key, digest(decision) if decision else None)] = (original,decision)
    output = []
    for target, captured in grouped.items():
        usage = usages.get(target)
        if usage is None or usage.payload.get('runtime') != 'codex':
            continue
        own_ref = reference('attribution',[POLICY,target])
        own = heads.get(own_ref)
        records = list(captured.values())
        purposes = {decision['purpose'] for _,decision in records if decision is not None}
        complete = all(decision is not None for _,decision in records)
        if not complete and own is None:
            continue
        state = 'assigned' if complete and len(purposes)==1 else 'ambiguous' if len(purposes)>1 else 'unassigned'
        purpose = next(iter(purposes)) if state=='assigned' else None
        start = usage.payload.get('interval_start') or usage.occurred_at
        if start is None:
            continue
        payload = {'target_ref':target,'wi':purpose if purpose and purpose!='none' else None,
            'executor':None,'responsible':None,'delegator':None,'state':state,
            'policy':'non_wi_activity.v1' if purpose=='none' else POLICY,'effective_at':start}
        if purpose=='none':
            payload['work_kind']='none'
        proofs = {reference('native_request_input',digest(a)) for a,_ in records if a is not None}
        if any(a is None for a,_ in records):
            # An enrolled current target with no admissible witness can retract
            # only this reader's prior claim, never create a new assignment.
            proofs.add(reference('native_request_witness_unavailable',[target,usage.payload.get('native_scope_ref')]))
        proofs.update(reference('native_request_declaration',digest(d)) for _,d in records if d)
        # Bind to native logical identity, not a growing Token version. Quantity
        # updates do not append an identical purpose on every collection.
        proofs.add(reference('native_request_target',[target,usage.payload.get('native_scope_ref')]))
        weak_ref = reference('attribution',['scl_host_session_binding.v1',target])
        weak = heads.get(weak_ref)
        candidates = [(own_ref,own)]
        if weak is not None and (weak.producer == POLICY or (
                weak.producer=='scl_host_session_binding.v1' and weak.payload.get('state')=='ambiguous'
                and weak.payload.get('target_ref')==target
                and all(weak.payload.get(k) is None for k in ('wi','executor','responsible','delegator','assignment_ref','execution_ref','scope','work_kind','context')))):
            candidates.append((weak_ref,weak))
        for entity, prior in candidates:
            if entity in conflicts or (prior is not None and prior.producer not in {POLICY,'scl_host_session_binding.v1'}):
                continue
            evidence = tuple(sorted(proofs | (set(prior.evidence) if prior else set())))
            if prior is not None and prior.producer==POLICY and prior.payload==payload and set(evidence)==set(prior.evidence):
                continue
            output.append(Receipt.make('attribution',None,payload,entity_ref=entity,
                supersedes=prior.fact_ref if prior else None,occurred_at=start,observed_at=observed,
                producer=POLICY,evidence=evidence))
    return output


def hook(payload, root, command):
    original = capture(payload,root)
    if original is None:
        return None
    cmd = shlex.join([*command,'declare','--target',original['target_ref'],'--request',original['request_ref'],
        '--scope',original['native_scope_ref'],'--input-sha256',digest(original)])
    return {'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':
        'Record the cost purpose of THIS request once before business work: '+cmd+
        ' --purpose <wi_YYYYMMDD_NNN|none>. Select from this request and its business context; '
        'none explicitly means ordinary non-WI work, never a missing binding. Do not ask the user '
        'to fill bookkeeping fields. No execution authority is granted. A missing or conflicting '
        'declaration stays visible; do not block the user or retry in a loop. '
        'This command only returns the statement; normal native collection records it.'}}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('hook')
    p=sub.add_parser('declare')
    for name in ('target','request','scope','input-sha256','purpose'):
        p.add_argument('--'+name,required=True)
    args=parser.parse_args(argv)
    try:
        if args.action=='hook':
            raw=sys.stdin.buffer.read(1024*1024+1)
            if len(raw)>1024*1024:
                raise ReceiptError('native Hook input exceeds its bound')
            result=hook(json.loads(raw),default_root(),[sys.executable,str(Path(sys.argv[0]).resolve())])
        else:
            result=declaration_message(args.target,args.request,args.scope,args.input_sha256,args.purpose,
                session=os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID'))
        if result is not None:
            print(json.dumps(result,ensure_ascii=False))
    except (OSError,ValueError,TypeError,KeyError,ReceiptError):
        print(json.dumps({'systemMessage':'Cost purpose capture unavailable; continue business work. No purpose was assumed.'}
                        if args.action=='hook' else {'status':'unavailable','reason':'native_request_purpose_unverifiable'}))
        return 0 if args.action=='hook' else 1
    return 0

if __name__=='__main__':
    raise SystemExit(main())
