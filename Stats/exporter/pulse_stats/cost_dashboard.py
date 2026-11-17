"""Read-only, bounded WI300 cost carrier. No pricing, scanning or API calls."""
from __future__ import annotations
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

SCHEMA = 'pulse_stats_cost_dashboard/v1'
MAX_BYTES = 512 * 1024
# The whole App snapshot; the cost carrier travels gzip-compressed when expanded would not fit.
SNAPSHOT_MAX_BYTES = 1024 * 1024
# SHA-256 of the decoded gzip bytes, so the App checks transport integrity without
# re-serializing the publication; the exporter has verified the expanded digest.
TRANSPORT_DIGEST_FIELD = 'compressed_dashboard_sha256'
IDENTITY_DIMENSIONS = {'agent', 'node', 'runtime', 'model', 'entrypoint', 'wi'}
ACTIVITY_DIMENSIONS = ['agent', 'wi', 'entrypoint', 'node', 'runtime', 'model']
TENANT_ACTIVITY_DIMENSIONS = ['tenant', *ACTIVITY_DIMENSIONS]
QUERY_FIELDS = {'agent', 'agent_role', 'as_of', 'cost_basis', 'entrypoint', 'from_date',
                'group_by', 'human_hourly_rate', 'human_rate_policy', 'model', 'node',
                'runtime', 'scope', 'timezone', 'to_date', 'value_from', 'value_to',
                'watermark', 'work_items'}
DEFAULT_PATH = Path.home() / '.local/state/pulse/stats/cost-dashboard-v1.json'
LAST_WEEK_NAME = 'cost-last-week-v1.json'
# Written by Scripts/pulse-stats-cost-refresh.py beside the publication.
REFRESH_STATUS_NAME = 'cost-refresh-status-v1.json'
REFRESH_STATUS_SCHEMA = 'pulse_stats_cost_refresh_status/v1'
REFRESH_STATES = {'running', 'completed', 'failed'}
# A cycle still "running" this long after its start was stopped without a word.
# Oracle cycles run about 50 minutes.
REFRESH_RUNNING_LIMIT = timedelta(minutes=90)
# The longest validity a publisher sets (stats_dashboard.VALIDITY_MINUTES).
MAX_VALIDITY_SECONDS = 75 * 60


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')


def _date(value):
    result = datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:
        raise ValueError('aware timestamp required')
    return result


def _money(value):
    if value is not None and (not isinstance(value,str) or len(value)>38 or
                             not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',value)):
        raise ValueError('invalid amount')


def _unpriced_summary(value):
    fields = {'unpriced_count', 'unpriced_reason_counts', 'unpriced_sha256'}
    if not fields.intersection(value):
        return
    if not fields.issubset(value):
        raise ValueError('incomplete diagnostic summary')
    count, counts, fingerprint = (value[k] for k in
        ('unpriced_count', 'unpriced_reason_counts', 'unpriced_sha256'))
    if (type(count) is not int or not len(value['unpriced']) < count <= 2**63-1
            or not isinstance(counts, dict) or not counts
            or any(not isinstance(k, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,100}', k)
                   or type(n) is not int or n <= 0 for k, n in counts.items())
            or sum(counts.values()) != count
            or not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)):
        raise ValueError('invalid diagnostic summary')
    samples = {}
    for row in value['unpriced']:
        samples[row['reason']] = samples.get(row['reason'], 0) + 1
    if set(samples) != set(counts) or any(n > counts[k] for k, n in samples.items()):
        raise ValueError('diagnostic samples omit or contradict reasons')


def _cost(value, event_count=None):
    base = {'machine_cash_usd','partial_machine_cash_usd','cash_basis','reasons','unpriced'}
    extra = {'unpriced_count','unpriced_reason_counts','unpriced_sha256'}
    priced = {'priced_machine_cash_usd','priced_event_count'}
    classes = {'anomalies','proof_gaps'}
    fields = set(value) - classes
    if (fields not in (base, base | extra, base | priced, base | extra | priced)
            or len(classes & set(value)) == 1):
        raise ValueError('unsupported query cost')
    if value['cash_basis']!='openrouter_standard_token_cash':
        raise ValueError('unsupported cost basis')
    full,partial = value['machine_cash_usd'],value['partial_machine_cash_usd']
    _money(full); _money(partial)
    reasons = value['reasons']
    if not isinstance(reasons,list) or any(not isinstance(x,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,100}',x) for x in reasons):
        raise ValueError('invalid reason codes')
    if 'anomalies' in value:
        # Presentation classes must partition the unchanged reason codes.
        parts = [value['anomalies'], value['proof_gaps']]
        if (any(not isinstance(p, list) or p != sorted(set(p)) or any(x not in reasons for x in p) for p in parts)
                or set(parts[0]) & set(parts[1]) or set(parts[0]) | set(parts[1]) != set(reasons)):
            raise ValueError('reason classes must partition reasons')
    if ((full is not None and (partial is not None or reasons)) or
        (partial is not None and (not reasons or 'join_conflict' in reasons))):
        raise ValueError('inconsistent amount coverage')
    for row in value['unpriced']:
        if set(row)!={'fact_ref','reason'} or any(not isinstance(v,str) or len(v)>200 or '\n' in v or '/' in v for v in row.values()):
            raise ValueError('unpriced evidence must be opaque')

    _unpriced_summary(value)
    if 'priced_event_count' in value:
        _priced_total(value, event_count)


def _priced_total(value, event_count):
    """The priced total covers exactly the priced rows of the same query."""
    count, amount = value['priced_event_count'], value['priced_machine_cash_usd']
    _money(amount)
    known = value['machine_cash_usd'] or value['partial_machine_cash_usd']
    unpriced = value.get('unpriced_count', len(value['unpriced']))
    if (type(count) is not int or count < 0 or (amount is None) != (count == 0)
            or (known is not None and amount != known)
            or (event_count is not None and count + unpriced != event_count)):
        raise ValueError('priced total contradicts query cost')


TOKEN_METRICS = {
    'uncached_input': 'usage.today.input_tokens_uncached',
    'cache_read': 'usage.today.cache_read_tokens',
    'cache_write': 'usage.today.cache_creation_tokens',
    'output': 'usage.today.output_tokens',
}
REPLACED_USAGE_IDS = frozenset((*TOKEN_METRICS.values(), 'usage.today.input_tokens',
                              'usage.by_agent', 'usage.today.source_note'))


def _usage(usage):
    if type(usage.get('event_count')) is not int or usage['event_count'] < 0:
        raise ValueError('invalid usage event count')
    total = usage.get('known_total_tokens')
    if total is not None and (type(total) is not int or total < 0):
        raise ValueError('invalid total token quantity')
    for dimension in ('agent','wi'):
        count = usage.get(dimension + '_unassigned_event_count')
        if count is not None and (type(count) is not int or not 0 <= count <= usage['event_count']):
            raise ValueError('invalid identity coverage count')
        _money(usage.get(dimension + '_unassigned_priced_usd'))
    tokens = usage.get('tokens')
    if tokens is None:  # Old documents remain readable; never invent missing classes.
        return
    if not isinstance(tokens, dict) or set(tokens) != set(TOKEN_METRICS):
        raise ValueError('invalid token classes')
    for row in tokens.values():
        if (not isinstance(row, dict) or set(row) != {'known','unknown_event_count'}
                or (row['known'] is not None and (type(row['known']) is not int or row['known'] < 0))
                or type(row['unknown_event_count']) is not int or row['unknown_event_count'] < 0):
            raise ValueError('invalid token quantities')


def usage_metrics(document, *, now=None):
    """Project the SAME query, not another scan or a money-to-token conversion."""
    activity = [v for v in document['views'] if v['period']=='today' and v['dimension']=='activity']
    agents = [v for v in document['views'] if v['period']=='today' and v['dimension']=='agent']
    matches = activity if len(activity) == 1 else agents
    view = matches[0] if document['status']=='available' and len(matches)==1 else None
    day_changed = bool(view and now and view['query']['from_date'] !=
                       now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat())
    if day_changed:
        view = None
    usage = view['summary']['usage'] if view else {}
    tokens = usage.get('tokens') or {}
    observed = document['generated_at'] if view else None
    has_observations = usage.get('event_count', 0) > 0
    complete = usage.get('source_coverage') == 'complete'
    note = ('与协作成本使用同一批凭据；当前仅为已记录部分，未接入来源不计入'
            if has_observations and not complete else
            '今日已记录用量，与协作成本同源' if has_observations else
            '今日无用量，来源覆盖已确认' if complete else '今日用量来源未完整接通，不能判定没有用量')
    if day_changed:
        note = '已跨北京时间日界，等待当前日期的用量查询'
    elif not tokens:
        note = '用量数据不可用：' + str(document.get('reason') or '上游尚未提供 Token 分项')
    elif any(row.get('unknown_event_count',0) for row in tokens.values()):
        note += '；部分 Token 分项缺失，显示已知数量'

    def metric(identifier, value, unit='tokens'):
        return {'id':identifier, 'value':value, 'unit':unit, 'status':'available' if value is not None else 'unknown',
                'capability_id':'s1.request_trace_capture', 'quality':'validated' if complete else 'descriptive',
                'scope':'ComputeCost activity; Asia/Shanghai; same cost query watermark',
                'window':'today_cst', 'observed_at':observed, 'sample_count':usage.get('event_count'),
                'reason':note}
    values = {key: (tokens.get(key,{}).get('known') if has_observations or complete else None)
              for key in TOKEN_METRICS}
    result = [metric(identifier, values[key]) for key,identifier in TOKEN_METRICS.items()]
    inputs = [values[k] for k in ('uncached_input','cache_read','cache_write')]
    result.append(metric('usage.today.input_tokens', sum(inputs) if all(v is not None for v in inputs) else None))
    rows = None
    if view and tokens and (has_observations or complete):
        rows = []
        for group in view['groups']:
            group_usage = group.get('usage') or {}
            known = group_usage.get('tokens') or {}
            output = known.get('output',{}).get('known')
            if group_usage.get('event_count',0) <= 0 or output is None:
                continue
            name = group['key']['agent']
            rows.append({'agent': 'Agent 未识别' if name=='unassigned' else name,
                         'output_tokens':output, 'input_tokens':known.get('uncached_input',{}).get('known'),
                         'turns':None, 'session_count':None})
    result.append(metric('usage.by_agent',rows,'list'))
    result.append(metric('usage.today.source_note',usage.get('event_count'),'count'))
    return result



ATTRIBUTION_SOURCES = {'declared', 'holder_supplement', 'exploration', 'scheduled_job', 'classified', 'unattributed'}


def _attribution(group):
    """How a row's usage got its WI, and a scheduler job's public name."""
    if 'attribution' not in group:
        return
    value = group['attribution']; wi = group['key'].get('wi')
    is_job = isinstance(wi, str) and wi.startswith('cron:')
    if not isinstance(value, dict) or set(value) != ({'sources', 'job'} if is_job else {'sources'}):
        raise ValueError('invalid attribution sources')
    sources = value['sources']
    if not isinstance(sources, dict) or not set(sources) <= ATTRIBUTION_SOURCES:
        raise ValueError('invalid attribution sources')
    for row in sources.values():
        if (not isinstance(row, dict) or set(row) != {'event_count', 'priced_machine_cash_usd'}
                or type(row['event_count']) is not int or row['event_count'] < 1
                or row['priced_machine_cash_usd'] is None):
            raise ValueError('invalid attribution source amount')
        _money(row['priced_machine_cash_usd'])
    if 'usage' in group and sum(r['event_count'] for r in sources.values()) != group['usage']['event_count']:
        raise ValueError('attribution sources must partition the row usage')
    if is_job:
        job = value['job']
        if (not isinstance(job, dict) or set(job) != {'id', 'name'} or job['id'] != wi[len('cron:'):]
                or (job['name'] is not None and (not isinstance(job['name'], str) or not 1 <= len(job['name']) <= 120
                                                 or any(ord(c) < 32 for c in job['name'])))):
            raise ValueError('invalid scheduled job identity')


def _tenant(group, dimensions):
    """Accept only the declared public tenant projection, never route identities."""
    tenant = group.get('tenant')
    key = group['key'].get('tenant')
    if 'tenant' in group and tenant is None:
        raise ValueError('invalid tenant identity')
    if tenant is None:
        if key is not None and key not in {'unassigned', 'ambiguous', 'unknown', 'not_applicable'}:
            raise ValueError('named tenant requires declaration evidence')
        return
    base = {'id', 'name', 'binding_refs'}
    if (dimensions != TENANT_ACTIVITY_DIMENSIONS or not isinstance(tenant, dict)
            or set(tenant) not in (base, base | {'sources', 'registry_conflicts'})
            or not isinstance(tenant['id'], str) or tenant['id'] != key
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', key)
            or key in {'unassigned', 'unknown', 'not_applicable'}):
        raise ValueError('invalid tenant identity')
    name, refs = tenant['name'], tenant['binding_refs']
    # The query deliberately retains declaration refs when memberships or
    # public labels disagree. A missing label remains pending in Swift; it
    # must not suppress otherwise valid usage and cost for the whole document.
    if ((name is not None and (not isinstance(name, str) or not 1 <= len(name) <= 64
            or name != name.strip() or any(ord(c) < 32 or c in '/\\:@' for c in name)))
            or (key == 'ambiguous' and name is not None)
            or not isinstance(refs, list) or not refs
            or any(not isinstance(ref, str) or not re.fullmatch(r'ccf1_[a-f0-9]{64}', ref) for ref in refs)
            or len(refs) != len(set(refs))):
        raise ValueError('invalid tenant declaration evidence')
    if 'sources' in tenant:
        # Session proof (Relay input or declared membership) or the team
        # registry's Agent binding; a differing registry tenant is a conflict.
        sources, conflicts = tenant['sources'], tenant['registry_conflicts']
        if (not isinstance(sources, list) or not sources or sources != sorted(set(sources))
                or not set(sources) <= {'session', 'registry'}
                or not isinstance(conflicts, list) or conflicts != sorted(set(conflicts))
                or any(not isinstance(c, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', c)
                       or c in {key, 'unassigned', 'ambiguous', 'unknown', 'not_applicable'} for c in conflicts)
                or (conflicts and 'session' not in sources)):
            raise ValueError('invalid tenant source')


def _project_page_ok(value, wi):
    """Absent, or the absolute local path of this WI's kernel page."""
    if value is None:
        return True
    return (isinstance(value, str) and len(value) <= 1024 and value.startswith('/')
            and value.rsplit('/', 1)[-1] == wi + '.html' and '/../' not in value + '/')


def _validate_work_items(items):
    """Display text for each WI a row names; never an attribution input."""
    if not isinstance(items, list) or len(items) > 4096:
        raise ValueError('invalid work item display')
    ids = [item.get('id') if isinstance(item, dict) else None for item in items]
    if ids != sorted(set(i for i in ids if isinstance(i, str))):
        raise ValueError('invalid work item display')
    for item in items:
        if (not re.fullmatch(r'wi_\d{8}_\d{3}', item['id'])
                or set(item) - {'project_page', 'parent'} != {'id', 'title', 'summary', 'status'}
                or not _project_page_ok(item.get('project_page'), item['id'])
                or 'parent' in item and (not isinstance(item['parent'], str)
                                         or not re.fullmatch(r'wi_\d{8}_\d{3}', item['parent'])
                                         or item['parent'] == item['id'])
                or not isinstance(item['title'], str) or len(item['title']) > 80
                or not (item['summary'] is None or isinstance(item['summary'], str) and len(item['summary']) <= 200)
                or not (item['status'] is None or isinstance(item['status'], str) and len(item['status']) <= 40)):
            raise ValueError('invalid work item display')


def unverified_anomalies(reason):
    """The anomaly summary a reader shows when the published one cannot be trusted."""
    empty = {'count': None, 'items': []}
    return {'status': 'unverified', 'reason': reason, 'observed_at': None, 'since': None,
            'window_days': None, 'enforced_since': None, 'repositories': [],
            'unrecorded_merges': dict(empty), 'unenforced_merges': dict(empty), 'direct_commits': dict(empty)}


def published_anomalies(document):
    """The document's PR anomaly summary, or an unverified one when absent or invalid."""
    from .pull_request_view import validate_anomalies
    value = (document or {}).get('pull_request_anomalies')
    if value is None:
        return unverified_anomalies('anomalies_not_published')
    try:
        validate_anomalies(value)
    except (ValueError, TypeError, KeyError, AttributeError):
        return unverified_anomalies('anomaly_extension_invalid')
    return value


def period_window(day, period):
    """[from, to) Asia/Shanghai days of a published period, as of `day`."""
    monday = day-timedelta(days=day.weekday())
    if period == 'last_week':
        return monday-timedelta(days=7), monday
    return {'today':day,'week':monday,'month':day.replace(day=1)}[period], day+timedelta(days=1)


def _validate(document):
    fields = {'schema_version','generated_at','expires_at','source_revision','status','reason',
              'views','subscriptions','openrouter','sha256'}
    optional = set(document) - fields
    if not fields <= set(document) or not optional <= {'work_items', 'pull_request_anomalies'} or document['schema_version']!=SCHEMA:
        raise ValueError('unsupported cost snapshot')
    _validate_work_items(document.get('work_items', []))
    from .pull_request_view import FAMILY_FIELD, VIEW_FIELDS, validate_families, validate_pull_requests
    parents = {item['id']: item['parent'] for item in document.get('work_items', []) if 'parent' in item}
    # An optional audit extension never invalidates independently valid cost, and the
    # validated carrier stays byte-identical (its digest keeps verifying downstream).
    # Readers take the extension through published_anomalies().
    expected = hashlib.sha256(_canonical({k:v for k,v in document.items() if k!='sha256'})).hexdigest()
    if document['sha256']!=expected or not re.fullmatch(r'[0-9a-f]{40}',document['source_revision']):
        raise ValueError('invalid source binding')
    generated,expires = _date(document['generated_at']),_date(document['expires_at'])
    if not 0<(expires-generated).total_seconds()<=MAX_VALIDITY_SECONDS:
        raise ValueError('invalid validity interval')
    if document['status'] not in {'available','unavailable'} or not isinstance(document['views'],list):
        raise ValueError('invalid cost status')
    pairs=set(); watermarks=set()
    for view in document['views']:
        base_fields = {'period','dimension','source_schema','query','watermark','recorded_at',
                       'catalog_sha256','catalog_observed_at','report_sha256','summary','groups'}
        outcome_fields = {'outcomes','outcome_only_groups','active_agents'}
        allowed = (base_fields, base_fields | outcome_fields,
                   base_fields | outcome_fields | {'population_scope'})
        allowed += tuple(fields | {'stages'} for fields in allowed)
        if set(view) - VIEW_FIELDS - {FAMILY_FIELD} not in allowed:
            raise ValueError('invalid query view')
        pair=(view['period'],view['dimension']); q=view['query']
        if (pair in pairs or pair[0] not in {'today','week','month','last_week'}
                or pair[1] not in IDENTITY_DIMENSIONS | {'activity'}):
            raise ValueError('duplicate or unsupported view')
        dimensions = ACTIVITY_DIMENSIONS if pair[1] == 'activity' else [pair[1]]
        if pair[1] == 'activity' and q.get('group_by') == TENANT_ACTIVITY_DIMENSIONS:
            dimensions = TENANT_ACTIVITY_DIMENSIONS
        if set(q) not in (QUERY_FIELDS - {'node', 'entrypoint'}, QUERY_FIELDS, QUERY_FIELDS | {'tenant'}):
            raise ValueError('invalid query fields')
        if dimensions == TENANT_ACTIVITY_DIMENSIONS:
            expected_scope = ('filtered_activity' if any(q.get(k) for k in
                ('tenant','agent','node','runtime','model','entrypoint','work_items')) else 'all_registered_tenants')
            if 'tenant' not in q or view.get('population_scope') != expected_scope:
                raise ValueError('invalid tenant population scope')
        elif 'population_scope' in view:
            raise ValueError('tenant population scope requires tenant dimensions')
        pairs.add(pair); watermarks.add(view['watermark'])
        day=generated.astimezone(ZoneInfo('Asia/Shanghai')).date()
        if (q['from_date'],q['to_date'])!=tuple(d.isoformat() for d in period_window(day,pair[0])):
            raise ValueError('query window label mismatch')
        if view['recorded_at'] is not None and _date(view['recorded_at'])>generated:
            raise ValueError('future receipt watermark')
        if view['catalog_observed_at'] is not None and _date(view['catalog_observed_at'])>generated:
            raise ValueError('future price timestamp')
        if (view['source_schema']!='computecost_collaboration_query/v1' or q['scope']!='activity' or
            q['timezone']!='Asia/Shanghai' or q['group_by']!=dimensions or q['watermark']!=view['watermark'] or
            not re.fullmatch(r'[0-9a-f]{64}',view['report_sha256'])):
            raise ValueError('query identity mismatch')
        if type(view['watermark']) is not int or view['watermark']<0 or type(q['watermark']) is not int:
            raise ValueError('invalid watermark')
        _usage(view['summary']['usage'])
        _cost(view['summary']['cost'], view['summary']['usage']['event_count'])
        names=set()
        for group in view['groups']:
            group_fields = ({'key','cost'}, {'key','cost','usage'}, {'key','cost','usage','outcomes','detail'})
            allowed_fields = group_fields + tuple(fields | {'tenant'} for fields in group_fields)
            allowed_fields += tuple(fields | {'attribution'} for fields in allowed_fields if 'usage' in fields)
            if set(group) not in allowed_fields or set(group['key'])!=set(dimensions):
                raise ValueError('invalid group')
            _tenant(group, dimensions)
            _attribution(group)
            name=tuple(group['key'][key] for key in dimensions)
            if (any(not isinstance(v,str) or not 1<=len(v)<=200 or any(ord(c)<32 for c in v) for v in name)
                    or name in names):
                raise ValueError('invalid group identity')
            names.add(name)
            if 'usage' in group: _usage(group['usage'])
            _cost(group['cost'], group['usage']['event_count'] if 'usage' in group else None)
            if pair[1] == 'activity':
                count = group.get('usage', {}).get('event_count')
                if type(count) is not int or count <= 0:
                    raise ValueError('activity rows require actual usage')
        for group in view.get('outcome_only_groups', []):
            if (set(group) not in ({'key','cost','usage','outcomes'},
                                  {'key','cost','usage','outcomes','tenant'})
                    or set(group['key']) != set(dimensions)
                    or any(not isinstance(v, str) or not 1 <= len(v) <= 200
                           or any(ord(c) < 32 for c in v) for v in group['key'].values())):
                raise ValueError('invalid Outcome-only group')
            _tenant(group, dimensions)
            _usage(group['usage']); _cost(group['cost'], group['usage']['event_count'])
        from .outcome_view import validate_outcomes
        validate_outcomes(view)
        from .stage_view import validate_stages
        validate_stages(view)
        validate_pull_requests(view)
        validate_families(view, parents)
        for group in view.get('pull_request_only_groups', []):
            _tenant(group, dimensions)
            _usage(group['usage']); _cost(group['cost'], group['usage']['event_count'])
        if pair[1] == 'activity' and sum(g['usage']['event_count'] for g in view['groups']) != view['summary']['usage']['event_count']:
            raise ValueError('activity rows must partition all included usage')
    legacy = {(p,d) for p in ('today','week','month') for d in ('agent','wi','model')}
    full = {(p,d) for p in ('today','week','month') for d in IDENTITY_DIMENSIONS}
    compact = {(p,'activity') for p in ('today','week','month')}
    # The closed previous week is its own publication (cost-last-week-v1.json).
    closed_week = {('last_week','activity')}
    if document['status']=='available' and (pairs not in (legacy, full, full | compact, compact, closed_week)
                                             or len(watermarks)!=1):
        raise ValueError('missing views or mixed read watermark')
    if document['status']=='unavailable' and pairs:
        raise ValueError('unavailable source cannot carry amounts')
    for row in document['subscriptions']:
        if set(row)!={'id','name','status','monthly_amount_usd','billing_day','confirmed_on'}:
            raise ValueError('invalid subscription facts')
        _money(row['monthly_amount_usd'])
        if row['status'] not in {'active_user_confirmed','stopped_user_confirmed'}:
            raise ValueError('invalid subscription status')
        if row['billing_day'] is not None and (type(row['billing_day']) is not int or not 1<=row['billing_day']<=31):
            raise ValueError('invalid billing day')
    provider=document['openrouter']
    if set(provider)!={'status','reason','observed_at','month_utc','coverage','usage_usd','requests','first_returned_day','last_returned_day'}:
        raise ValueError('invalid procurement source')
    _money(provider['usage_usd'])
    if provider['coverage']!='returned_rows_only' or provider['status'] not in {'available','unavailable','stale'}:
        raise ValueError('invalid procurement coverage')
    if provider['status']!='available' and provider['usage_usd'] is not None:
        raise ValueError('unavailable procurement amount')
    return generated,expires


def unavailable(reason,now):
    return {'schema_version':SCHEMA,'status':'unavailable','reason':reason,
            'generated_at':now.isoformat(),'expires_at':(now+timedelta(minutes=45)).isoformat(),
            'source_revision':None,'views':[],'subscriptions':[],'openrouter':None,'sha256':None}


def load_refresh_status(path, *, now):
    """The publisher's latest cycle for the App's notice; unreadable means absent."""
    try:
        path=Path(path)
        if path.is_symlink():
            return None
        with path.open('rb') as handle:
            mode=__import__('os').fstat(handle.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode)&0o077:
                return None
            value=json.loads(handle.read(16*1024+1))
        if not isinstance(value,dict) or value.get('schema')!=REFRESH_STATUS_SCHEMA or value.get('state') not in REFRESH_STATES:
            return None
        started=_date(value['started_at'])
        finished=_date(value['finished_at']) if value.get('finished_at') is not None else None
        reason=value.get('reason')
        if started>now or (finished is not None and (finished<started or finished>now)):
            return None
        if reason is not None and not (isinstance(reason,str) and re.fullmatch(r'[a-z_]{1,64}',reason)):
            reason='publication_failed'
        state=value['state']
        if state=='running' and now-started>REFRESH_RUNNING_LIMIT:
            state,finished,reason='failed',started+REFRESH_RUNNING_LIMIT,'cycle_interrupted'
        if state!='running' and finished is None:
            return None
        peers=[{'id':p['id'],'status':p['status'],'reason':p.get('reason')}
               for p in (value.get('peers') or [])[:8]
               if isinstance(p,dict) and isinstance(p.get('id'),str) and re.fullmatch(r'[a-z0-9_-]{1,40}',p['id'])
               and p.get('status') in {'read','unreachable_or_invalid','paused'}
               and (p.get('reason') is None or isinstance(p['reason'],str) and re.fullmatch(r'[a-z_]{1,64}',p['reason']))]
        return {'state':state,'started_at':started.isoformat(),
                'finished_at':finished.isoformat() if finished else None,'reason':reason if state=='failed' else None,
                'peers':peers}
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        return None


def decode_cost_dashboard(data):
    value = json.loads(data)
    if isinstance(value, dict) and ('row_contexts' in value or 'compressed_dashboard' in value):
        from pulse_compute_cost.stats_dashboard_wire import expand_dashboard
        return expand_dashboard(value)
    return value


def transport_cost_dashboard(document):
    """The verified carrier as one gzip stream plus the hash of its bytes."""
    from pulse_compute_cost.stats_dashboard_wire import COMPRESSED_FIELD, compressed_dashboard_bytes
    packed = json.loads(compressed_dashboard_bytes(document))
    packed[TRANSPORT_DIGEST_FIELD] = hashlib.sha256(base64.b64decode(packed[COMPRESSED_FIELD])).hexdigest()
    return packed


def expand_transport_cost_dashboard(value):
    """A snapshot's cost carrier in its expanded form; the gzip form must match its hash."""
    if not isinstance(value, dict) or TRANSPORT_DIGEST_FIELD not in value:
        return value
    from pulse_compute_cost.stats_dashboard_wire import COMPRESSED_FIELD, expand_dashboard
    packed = {k: v for k, v in value.items() if k != TRANSPORT_DIGEST_FIELD}
    encoded = packed.get(COMPRESSED_FIELD)
    if not isinstance(encoded, str) or not isinstance(value[TRANSPORT_DIGEST_FIELD], str):
        raise ValueError('invalid cost transport')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError('invalid cost transport') from exc
    if hashlib.sha256(raw).hexdigest() != value[TRANSPORT_DIGEST_FIELD]:
        raise ValueError('cost transport digest mismatch')
    # The App snapshot, not the 512KiB publication file, bounds this carrier.
    return expand_dashboard(packed, max_encoded_bytes=SNAPSHOT_MAX_BYTES)


def load_cost_dashboard(path=DEFAULT_PATH, *, now=None, retain_expired=False):
    """The verified publication. `retain_expired` keeps an expired one whole for
    display under its own time; queries that answer "now" leave it False."""
    now=now or datetime.now(timezone.utc)
    path=Path(path)
    try:
        if path.is_symlink():
            raise ValueError('symlink cost source')
        with path.open('rb') as handle:
            mode=__import__('os').fstat(handle.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode)&0o077:
                raise ValueError('cost source not private')
            data=handle.read(MAX_BYTES+1)
        if len(data)>MAX_BYTES:
            raise ValueError('cost source oversized')
        value=decode_cost_dashboard(data)
        generated,expires=_validate(value)
        if generated>now:
            return unavailable('snapshot_from_future',now)
        if expires<=now and not retain_expired:
            return unavailable('snapshot_stale',now)
        return value
    except FileNotFoundError:
        return unavailable('source_not_configured',now)
    except (OSError,ValueError,KeyError,TypeError,AttributeError,ImportError):
        return unavailable('invalid_cost_snapshot',now)


def attach_cost_dashboard(rendered,path=DEFAULT_PATH,*,now=None):
    snapshot=json.loads(rendered)
    # Retire the obsolete active billing projection, not immutable historical receipts.
    snapshot['cost_dashboard']=load_cost_dashboard(path,now=now,retain_expired=True)
    refresh=load_refresh_status(Path(path).parent/REFRESH_STATUS_NAME,now=now or datetime.now(timezone.utc))
    if refresh is not None:
        snapshot['cost_refresh']=refresh
    snapshot['metrics']=[m for m in snapshot.get('metrics',[])
        if not m.get('id','').startswith(('billing.','compute_cost.shadow.','usage.mtd.'))
        and m.get('id') not in REPLACED_USAGE_IDS]
    snapshot['metrics'].extend(usage_metrics(snapshot['cost_dashboard'], now=now or datetime.now(timezone.utc)))
    value=json.dumps(snapshot,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    if len(value.encode('utf-8'))>SNAPSHOT_MAX_BYTES and snapshot['cost_dashboard'].get('status')=='available':
        # Only the transport changes: metrics above came from the verified expanded carrier.
        try:
            snapshot['cost_dashboard']=transport_cost_dashboard(snapshot['cost_dashboard'])
        except (ValueError,ImportError):
            pass
        else:
            value=json.dumps(snapshot,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    if len(value.encode('utf-8'))>SNAPSHOT_MAX_BYTES:
        snapshot['cost_dashboard']=unavailable('query_payload_too_large',now or datetime.now(timezone.utc))
        snapshot['metrics']=[m for m in snapshot['metrics'] if m.get('id') not in REPLACED_USAGE_IDS]
        snapshot['metrics'].extend(usage_metrics(snapshot['cost_dashboard']))
        value=json.dumps(snapshot,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    return value
