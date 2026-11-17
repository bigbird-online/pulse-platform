"""Historical verification cost identity, with no current execution admission.

Only an enrolled registry, its immutable candidate, paired audit and the native
session's own startup can establish identity. A parent's cwd/owner is not proof.
Every emitted claim is bound to one usage version and its whole time interval.
"""
from collections import defaultdict
from pathlib import Path
from hashlib import sha256
import json, os, re, socket, stat, subprocess

from .events import parse_timestamp
from .receipts import Receipt, ReceiptError, reference

FEATURE = POLICY = 'verification_workspace.v1'
FIELDS = {'registration_root', 'authority_root', 'host', 'surface_id'}
WI = re.compile(r'wi_[0-9]{8}_[0-9]{3}')
HO = re.compile(r'ho_[0-9]{8}_[0-9]{3}')
AGENT = re.compile(r'[a-z][a-z0-9_-]{0,63}')
MAX_REGISTRATIONS = 2048


def _owned(path, *, directory=False, limit=4*1024*1024):
    path = Path(path); info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (path.is_symlink() or path.resolve() != path or not valid
            or info.st_uid != os.getuid() or info.st_mode & 0o022
            or (not directory and info.st_size > limit)):
        raise ReceiptError('unsafe enrolled verification metadata')
    return path


def _yaml(path, *, many=False):
    import yaml
    raw = _owned(path).read_bytes()
    document = list(yaml.safe_load_all(raw)) if many else yaml.safe_load(raw)
    return raw, document


def _git(where, *args):
    result = subprocess.run(['git', '-C', str(where), *args], stdin=subprocess.DEVNULL,
        capture_output=True, timeout=5,
        env={**os.environ, 'GIT_OPTIONAL_LOCKS':'0', 'GIT_TERMINAL_PROMPT':'0'})
    if result.returncode or len(result.stdout) > 4*1024*1024:
        raise ReceiptError('verification Git identity unavailable')
    return result.stdout


def candidate_sha256(item):
    """Frozen v1 Harness fingerprint; not today's WI status or owner."""
    fields = ('work_item_id','owner','outcome_producer','outcome_claim',
              'outcome_change_type','outcome_acceptance_fingerprint')
    value = {key:item[key] for key in fields}
    value['updated_at'] = parse_timestamp(item['updated_at'], 'candidate freeze').isoformat()
    return sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def accepted_window(item, contract, audits, binding):
    """Read past authority from paired audit; current ACK cannot backfill it."""
    wi, aid, verifier = item['work_item_id'], binding['assignment_id'], binding['verifier']
    freeze = parse_timestamp(item['updated_at'], 'candidate freeze')
    created = parse_timestamp(contract['created_at'], 'assignment creation')
    criteria = item.get('acceptance_criteria')
    if not isinstance(criteria,list) or not criteria or not all(isinstance(c,str) for c in criteria):
        raise ReceiptError('verification candidate criteria missing')
    fingerprint = 'sha256:'+sha256(json.dumps([c.strip() for c in criteria], ensure_ascii=False,
                                      separators=(',',':')).encode()).hexdigest()
    receiver = contract.get('to_owners') or [contract.get('to_owner')]
    if (item.get('status') != 'awaiting_verification' or not item.get('outcome_claim')
            or not item.get('outcome_change_type') or item.get('outcome_acceptance_fingerprint') != fingerprint
            or item.get('owner') != binding['owner'] or item.get('outcome_producer') != binding['producer']
            or verifier == binding['producer'] or candidate_sha256(item) != binding['candidate_sha256']
            or contract.get('handoff_id') != aid or contract.get('work_item_id') != wi
            or contract.get('from_owner') != binding['producer'] or receiver != [verifier]
            or contract.get('to_owner') not in {None,verifier}
            or contract.get('purpose') != 'outcome_verification'
            or contract.get('responsibility_transfer') != 'not_transferred'
            or contract.get('original_responsible_owner') not in {None,binding['producer']}
            or created < freeze):
        raise ReceiptError('historical verification candidate does not match registration')
    records = [e for e in audits if e and e.get('work_item_id') == wi]
    if len({e.get('event_id') for e in records}) != len(records):
        raise ReceiptError('duplicate verification audit identity')
    own = [e for e in records if e.get('payload',{}).get('handoff_id') == aid]
    creation = [e for e in own if e.get('event_type') == 'handoff.created']
    accepted = [e for e in own if e.get('event_type') == 'handoff.accepted']
    if len(creation) != 1 or len(accepted) != 1:
        raise ReceiptError('historical assignment lacks unique paired acceptance')
    c, a = creation[0], accepted[0]
    cp, ap = c['payload'], a['payload']
    start = parse_timestamp(a['timestamp'], 'assignment accepted')
    if (parse_timestamp(c['timestamp'],'audit creation') != created
            or c.get('operator') != binding['producer'] or cp.get('to_owner') != verifier
            or cp.get('purpose') != contract.get('purpose')
            or cp.get('responsibility_transfer') != contract.get('responsibility_transfer')
            or cp.get('original_responsible_owner') != contract.get('original_responsible_owner')
            or a.get('operator') != verifier or ap.get('from_state') != 'pending'
            or ap.get('to_state') != 'accepted' or start < created):
        raise ReceiptError('historical assignment audit does not match caller or boundary')
    endings = []
    for event in records:
        at = parse_timestamp(event['timestamp'], 'verification audit')
        p = event.get('payload',{})
        if (p.get('handoff_id') == aid and p.get('to_state')
                and event is not a and at >= created):
            if at <= start:
                raise ReceiptError('assignment state conflicts before acceptance')
            if p['to_state'] != 'accepted':
                endings.append((at,event))
        if (event.get('event_type','').startswith('work_item.') and at > freeze
                and p.get('to_status') not in {None,'awaiting_verification'}):
            endings.append((at,event))
        if (event.get('event_type') == 'handoff.created' and p.get('handoff_id') != aid
                and at >= freeze and p.get('purpose') == 'outcome_verification'
                and p.get('responsibility_transfer') == 'not_transferred'):
            # A second live assignment is not selected by folder/name. A prior
            # assignment already revoked before this creation is harmless.
            other = p.get('handoff_id')
            states = [e for e in records if e.get('payload',{}).get('handoff_id') == other
                      and parse_timestamp(e['timestamp'],'other assignment') <= created
                      and e.get('payload',{}).get('to_state')]
            if at <= created and states and max(states,key=lambda e:parse_timestamp(e['timestamp'],'ACK'))['payload']['to_state'] not in {'pending','accepted'}:
                continue
            if at <= created:
                raise ReceiptError('multiple historical verification assignments')
            endings.append((at,event))
    end, ended = min(endings,key=lambda pair:pair[0]) if endings else (None,None)
    if end is not None and end <= start:
        raise ReceiptError('verification assignment expired before acceptance')
    evidence = [reference('verification_audit',sha256(json.dumps(e,sort_keys=True,default=str).encode()).hexdigest())
                for e in (c,a,ended) if e is not None]
    return start, end, tuple(evidence)


def _startup(observation):
    """The adapter's verified own startup, never a copied parent's header."""
    path = Path(observation.source_path)
    _owned(path,limit=512*1024*1024)
    with path.open('rb') as handle:
        for ordinal, line in enumerate(handle):
            if ordinal > 4096:
                break
            if b'"session_meta"' not in line or len(line) > 65536:
                continue
            row = json.loads(line); p = row.get('payload',{})
            if row.get('type') != 'session_meta' or p.get('id') != observation.session_id:
                continue
            native_source = p.get('source') if isinstance(p.get('source'),str) else None
            expected = reference('codex_startup',[p['id'],p.get('cwd'),p.get('originator'),native_source])
            if (expected != observation.startup_ref or p.get('cwd') != observation.startup_cwd):
                raise ReceiptError('verification startup differs from native observation')
            return parse_timestamp(p.get('timestamp') or row.get('timestamp'),'native startup'), reference('verification_native_startup',sha256(line).hexdigest())
    raise ReceiptError('own verification startup missing')


def historical_bindings(source, observations, diagnostics=None):
    config = source.get('verification_workspace_registry')
    if config is None:
        return {}
    if (not isinstance(config,dict) or set(config) != FIELDS
            or config['host'] != socket.gethostname()
            or config['surface_id'] != source.get('scl_surface_id')
            or not re.fullmatch(r'surface_v1_[a-z0-9_]+',config['surface_id'])):
        raise ReceiptError('invalid enrolled verification workspace registry')
    root, authority = (_owned(Path(config[k]),directory=True) for k in ('registration_root','authority_root'))
    if not root.is_absolute() or not authority.is_absolute() or authority.name != '.harness':
        raise ReceiptError('verification roots must be explicitly absolute')
    wanted = {o.startup_cwd for o in observations if o.startup_ref and o.startup_cwd}
    paths = sorted(root.glob('wi_*/verification/ho_*/'+config['surface_id']+'.yaml'))
    if len(paths) > MAX_REGISTRATIONS:
        raise ReceiptError('enrolled verification registration inventory exceeds bound')
    bindings = defaultdict(list)
    for path in paths:
        raw, reg = _yaml(path)
        if not isinstance(reg,dict):
            raise ReceiptError('invalid verification registration')
        repos = reg.get('repositories',[])
        if not isinstance(repos,list):
            raise ReceiptError('invalid verification repository list')
        selected = [r for r in repos if isinstance(r,dict) and r.get('worktree_path') in wanted]
        if not selected:
            continue
        try:
            wi, principal, binding = reg['work_item_id'], reg['agent_id'], reg['verification']
            aid = binding['assignment_id']
            if (not WI.fullmatch(wi) or not AGENT.fullmatch(principal) or not HO.fullmatch(aid)
                    or reg.get('schema_version') != 'work_item_workspace_registration/v1'
                    or reg.get('surface_id') != config['surface_id']
                    or reg.get('registered_by') != principal or binding['verifier'] != principal
                    or binding['host'] != config['host'] or set(binding) != {'assignment_id','host','owner','producer','verifier','candidate_sha256'}
                    or path != root/wi/'verification'/aid/(config['surface_id']+'.yaml')):
                raise ReceiptError('verification registration identity differs')
            for repo in selected:
                cwd = Path(repo['worktree_path'])
                base = repo['base_sha']
                if (not re.fullmatch(r'[a-f0-9]{40}',base) or repo.get('shared_root') != str(authority.parent)
                        or repo.get('head_sha_at_registration') != base):
                    raise ReceiptError('verification immutable repository registration differs')
                _owned(cwd,directory=True)
                common = Path(_git(cwd,'rev-parse','--git-common-dir').decode().strip())
                common = (common if common.is_absolute() else cwd/common).resolve()
                enrolled_common = Path(_git(authority.parent,'rev-parse','--git-common-dir').decode().strip())
                enrolled_common = (enrolled_common if enrolled_common.is_absolute() else authority.parent/enrolled_common).resolve()
                listed = _git(authority.parent,'worktree','list','--porcelain','-z').decode().split('\0')
                if (str(common) != repo.get('git_common_dir') or common != enrolled_common
                        or _git(cwd,'rev-parse','--show-toplevel').decode().strip() != str(cwd)
                        or 'worktree '+str(cwd) not in listed):
                    raise ReceiptError('verification exact Git workspace is not registered')
                import yaml
                candidate_raw = _git(authority.parent,'show',base+':.harness/work-items/'+wi+'.yaml')
                item = yaml.safe_load(candidate_raw)
                handoff_raw, contract = _yaml(authority/'handoffs'/(aid+'.yaml'))
                audits_raw, audits = _yaml(authority/'events'/(wi+'.events.yaml'),many=True)
                start, end, proof = accepted_window(item,contract,audits,binding)
                registered = parse_timestamp(reg['created_at'],'verification registered')
                if registered < start:
                    raise ReceiptError('verification workspace was registered before accepted assignment')
                proofs = (*proof, reference('verification_registration',sha256(raw).hexdigest()),
                          reference('verification_candidate',[base,sha256(candidate_raw).hexdigest(),binding['candidate_sha256']]),
                          reference('verification_handoff',sha256(handoff_raw).hexdigest()))
                bindings[str(cwd)].append({'wi':wi,'agent':principal,'start':start,'end':end,
                    'registered':registered,'proof':proofs,'workspace_ref':reference('verification_workspace',[source.get('node'),str(cwd),sha256(raw).hexdigest()])})
        except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError):
            if diagnostics is not None:
                diagnostics.append('verification_workspace_binding_unavailable')
            # One invalid registration cannot grant the remaining same-folder
            # assignment by elimination. That folder remains unproven.
            for repo in selected:
                bindings[repo['worktree_path']].append(None)
    return dict(bindings)


def observation_binding(observation, bindings):
    matches = bindings.get(observation.startup_cwd,[])
    if len(matches) != 1 or matches[0] is None or not observation.startup_ref:
        return None
    binding = matches[0]
    startup, startup_ref = _startup(observation)
    low = parse_timestamp(observation.interval_start,'verification usage start')
    high = parse_timestamp(observation.interval_end,'verification usage end')
    if (startup < max(binding['start'],binding['registered']) or low < startup or high < low
            or (binding['end'] is not None and high >= binding['end'])):
        return None
    return {**binding,'proof':(*binding['proof'],startup_ref,observation.startup_ref)}


def task_receipt(target, binding, observed):
    effective = target.payload['interval_start']
    return Receipt.make('attribution',[POLICY,target.entity_ref,target.fact_ref],{
        'target_ref':target.entity_ref,'wi':binding['wi'],'executor':None,'responsible':None,'delegator':None,
        'state':'assigned','policy':POLICY,'effective_at':effective,'usage_fact_ref':target.fact_ref},
        occurred_at=effective,observed_at=observed,producer=POLICY,
        evidence=tuple(sorted({target.fact_ref,*binding['proof']})))


def reanchor_receipts(rows, native_rows):
    """Keep the collector's canonical version edge, never a parse-only anchor.

    Equal native payloads can already have a committed supersession edge. Bind
    the same proof to that exact reused/refined version after the collector's
    existing progression rules; an obsolete or conflicting source cannot carry
    historical authority into a different final payload.
    """
    native = {r.fact_ref:r for r in native_rows if r.kind == 'usage'}
    usages = defaultdict(list)
    for row in rows:
        if row.kind == 'usage':
            usages[row.entity_ref].append(row)
    result = []
    for row in rows:
        anchor = row.payload.get('usage_fact_ref') if row.kind == 'attribution' else None
        if anchor is None:
            result.append(row); continue
        old = native.get(anchor)
        matches = {r.fact_ref:r for r in usages.get(row.payload['target_ref'],[])
                   if old is not None and r.payload == old.payload
                   and r.occurred_at == old.occurred_at and r.producer == old.producer}
        if len(matches) != 1:
            continue
        usage = next(iter(matches.values()))
        if usage.fact_ref == anchor:
            result.append(row); continue
        payload = {**row.payload, 'usage_fact_ref':usage.fact_ref}
        evidence = tuple(sorted(set(row.evidence)|{usage.fact_ref}))
        entity = ([POLICY,usage.entity_ref,usage.fact_ref] if row.producer == POLICY else
                  ['verification_runtime_actor',usage.entity_ref,usage.fact_ref,payload])
        result.append(Receipt.make(row.kind,entity,payload,occurred_at=row.occurred_at,
                      observed_at=row.observed_at,producer=row.producer,evidence=evidence))
    return result
