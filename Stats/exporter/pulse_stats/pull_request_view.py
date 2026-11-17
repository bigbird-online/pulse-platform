"""Validation for the kernel-merged PR output of a joint cost view and its anomalies."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

VIEW_FIELDS = {'pull_request_outputs', 'pull_request_only_groups'}
# Optional: each parent WI's own amount and PRs plus its children's (display only).
FAMILY_FIELD = 'work_item_families'
STATUSES = {'complete', 'partial', 'unavailable'}
_REPOSITORY = r'[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}'
_WI = r'wi_\d{8}_\d{3}'
_AMOUNT = r'[0-9]+(?:\.[0-9]+)?'


def _instant(value):
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('aware timestamp required')
    return moment


def _amount(value):
    if value is not None and (not isinstance(value, str) or len(value) > 38 or not re.fullmatch(_AMOUNT, value)):
        raise ValueError('invalid PR cost amount')


def _text(value, limit, optional=True):
    if value is None and optional:
        return
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError('invalid bounded PR text')


def validate_pull_requests(view):
    """Counts conserve, each PR sits in the view's period, unknown is never zero."""
    present = VIEW_FIELDS & set(view)
    if not present:
        return
    if present != VIEW_FIELDS or view['dimension'] != 'activity':
        raise ValueError('invalid PR output extension')
    result = view['pull_request_outputs']
    fields = {'status', 'reason', 'metric_scope', 'count', 'known_count', 'work_items',
              'unreadable_work_items', 'time_unknown_count'}
    if (not isinstance(result, dict) or set(result) != fields
            or result['metric_scope'] != 'wi_kernel_merged_pull_requests_in_period'
            or result['status'] not in STATUSES):
        raise ValueError('invalid PR output scope or status')
    if (result['status'] == 'complete') != (result['count'] is not None) or (
            result['count'] is not None and result['count'] != result['known_count']):
        raise ValueError('PR count must be known exactly when complete')
    if result['status'] == 'unavailable' and (result['work_items'] or result['known_count']):
        raise ValueError('unavailable PR output must not carry counts')
    unreadable = result['unreadable_work_items']
    if (not isinstance(unreadable, list) or unreadable != sorted(set(unreadable))
            or any(not isinstance(w, str) or not re.fullmatch(_WI, w) for w in unreadable)):
        raise ValueError('invalid unreadable WIs')
    if type(result['time_unknown_count']) is not int or result['time_unknown_count'] < 0:
        raise ValueError('invalid PR time coverage')
    zone = ZoneInfo(view['query']['timezone'])
    keys, wis = set(), []
    for item in result['work_items']:
        if (not isinstance(item, dict) or set(item) != {'wi', 'count', 'cost_usd', 'cost_complete',
                                                         'cost_per_pull_request_usd', 'pull_requests'}
                or not isinstance(item['wi'], str) or not re.fullmatch(_WI, item['wi'])
                or item['wi'] in unreadable or type(item['cost_complete']) is not bool
                or type(item['count']) is not int or item['count'] < 1
                or item['count'] != len(item['pull_requests'])):
            raise ValueError('invalid PR output row')
        _amount(item['cost_usd']); _amount(item['cost_per_pull_request_usd'])
        if (item['cost_usd'] is None) != (item['cost_per_pull_request_usd'] is None) or (
                item['cost_complete'] and item['cost_usd'] is None):
            raise ValueError('per-PR cost requires the WI amount')
        wis.append(item['wi'])
        for pr in item['pull_requests']:
            if (not isinstance(pr, dict) or set(pr) != {'repository', 'number', 'merge_commit', 'merged_at',
                                                         'score', 'reviewed_rounds', 'decision'}
                    or not isinstance(pr['repository'], str) or not re.fullmatch(_REPOSITORY, pr['repository'])
                    or type(pr['number']) is not int or pr['number'] < 1
                    or not (pr['merge_commit'] is None or isinstance(pr['merge_commit'], str)
                            and re.fullmatch(r'[0-9a-f]{40}', pr['merge_commit']))
                    or not (pr['score'] is None or type(pr['score']) is int and 0 <= pr['score'] <= 100)
                    or type(pr['reviewed_rounds']) is not int or pr['reviewed_rounds'] < 0
                    or pr['decision'] not in {'review', 'human'}):
                raise ValueError('invalid merged PR')
            day = _instant(pr['merged_at']).astimezone(zone).date().isoformat()
            if not view['query']['from_date'] <= day < view['query']['to_date']:
                raise ValueError('merged PR escaped query period')
            keys.add((pr['repository'], pr['number']))
    if wis != sorted(set(wis)) or len(keys) != result['known_count']:
        raise ValueError('PR output count does not conserve')
    listed = set(wis)
    shown = {g['key'].get('wi') for g in view['groups'] + view.get('outcome_only_groups', [])}
    for group in view['pull_request_only_groups']:
        if (set(group) != {'key', 'cost', 'usage'} or set(group['key']) != set(view['query']['group_by'])
                or group['key']['wi'] not in listed or group['key']['wi'] in shown
                or group['usage']['event_count'] != 0 or group['cost']['machine_cash_usd'] is not None
                or group['cost']['partial_machine_cash_usd'] is not None):
            raise ValueError('PR-only row must not invent consumption')
        shown.add(group['key']['wi'])


def validate_anomalies(value):
    fields = {'status', 'reason', 'observed_at', 'since', 'window_days', 'enforced_since', 'repositories',
              'unrecorded_merges', 'unenforced_merges', 'direct_commits'}
    if (not isinstance(value, dict) or set(value) - {UNREGISTERED} != fields
            or value['status'] not in {'verified', 'partial', 'unverified'}):
        raise ValueError('invalid PR anomaly summary')
    if value['status'] != 'verified' and not isinstance(value['reason'], str):
        raise ValueError('unverified anomalies need a reason')
    for name in ('observed_at', 'since'):
        if value[name] is not None:
            _instant(value[name])
    if value['enforced_since'] is not None:
        date.fromisoformat(value['enforced_since'])
    elif value['unrecorded_merges']['count'] or value['unrecorded_merges']['items']:
        raise ValueError('without an enforcement date no GitHub merge is an anomaly')
    for repository in value['repositories']:
        if (set(repository) != {'repository', 'merges', 'commits', 'reason'}
                or repository['merges'] not in {'read', 'unverified'}
                or repository['commits'] not in {'read', 'unverified'}):
            raise ValueError('invalid audited repository')
    merge_fields = {'repository', 'number', 'merged_at', 'merge_commit', 'title'}
    for name, item_fields in (('unrecorded_merges', merge_fields), ('unenforced_merges', merge_fields),
                              ('direct_commits', {'repository', 'sha', 'committed_at', 'title', 'author'})):
        part = value[name]
        if (not isinstance(part, dict) or set(part) != {'count', 'items'}
                or not isinstance(part['items'], list) or len(part['items']) > 20):
            raise ValueError('invalid anomaly list')
        if part['count'] is not None and (type(part['count']) is not int or part['count'] < len(part['items'])):
            raise ValueError('anomaly count must cover its items')
        if value['status'] == 'unverified' and part['count'] is not None:
            raise ValueError('unverified anomalies must not become zero')
        for item in part['items']:
            if not isinstance(item, dict) or set(item) != item_fields:
                raise ValueError('invalid anomaly item')
            _text(item.get('title'), 120)
            _text(item.get('author'), 64)
    if UNREGISTERED in value:
        validate_unregistered(value[UNREGISTERED])


# Optional: per period, merged PRs without a kernel merge record (hint only, never output).
UNREGISTERED = 'unregistered_pull_requests'
_AGENT = r'[a-z][a-z0-9_-]{0,63}'


def validate_unregistered(value):
    """Each period's count covers its items, hints re-add, unknown is never zero."""
    if not isinstance(value, dict) or not set(value) <= {'today', 'week', 'month', 'last_week'}:
        raise ValueError('invalid unregistered PR periods')
    fields = {'from_date', 'to_date', 'status', 'reason', 'count', 'agents', 'unhinted_count', 'items'}
    item_fields = {'repository', 'number', 'merged_at', 'title', 'head_ref', 'agent_hint'}
    zone = ZoneInfo('Asia/Shanghai')
    for part in value.values():
        if (not isinstance(part, dict) or set(part) != fields or part['status'] not in {'verified', 'unverified'}
                or not date.fromisoformat(part['from_date']) < date.fromisoformat(part['to_date'])
                or not isinstance(part['agents'], list) or not isinstance(part['items'], list)
                or len(part['items']) > 20):
            raise ValueError('invalid unregistered PR period')
        if part['status'] == 'unverified':
            if (not isinstance(part['reason'], str) or part['count'] is not None
                    or part['unhinted_count'] is not None or part['agents'] or part['items']):
                raise ValueError('unverified unregistered PRs must not become zero')
            continue
        names = [a.get('agent') if isinstance(a, dict) else None for a in part['agents']]
        if (type(part['count']) is not int or type(part['unhinted_count']) is not int
                or part['unhinted_count'] < 0 or part['count'] < len(part['items'])
                or len(set(names)) != len(names)
                or any(set(a) != {'agent', 'count'} or not isinstance(a['agent'], str)
                       or not re.fullmatch(_AGENT, a['agent']) or type(a['count']) is not int or a['count'] < 1
                       for a in part['agents'])
                or sum(a['count'] for a in part['agents']) + part['unhinted_count'] != part['count']):
            raise ValueError('unregistered PR hints do not re-add')
        for item in part['items']:
            if (not isinstance(item, dict) or set(item) != item_fields
                    or not isinstance(item['repository'], str) or not re.fullmatch(_REPOSITORY, item['repository'])
                    or type(item['number']) is not int or item['number'] < 1
                    or item['agent_hint'] is not None and item['agent_hint'] not in names):
                raise ValueError('invalid unregistered PR')
            _text(item['title'], 120)
            _text(item['head_ref'], 200)
            day = _instant(item['merged_at']).astimezone(zone).date().isoformat()
            if not part['from_date'] <= day < part['to_date']:
                raise ValueError('unregistered PR escaped its period')


def _member_cost(groups, wi):
    """(amount, priced, complete) of one WI's rows, by the PR output's own amount rule."""
    total, priced, complete = Decimal(0), False, True
    for group in groups:
        if group['key'].get('wi') != wi:
            continue
        value = group['cost'].get('machine_cash_usd')
        if value is None:
            complete = False
            value = group['cost'].get('partial_machine_cash_usd')
        if value is not None:
            total += Decimal(value)
            priced = True
    return total, priced, complete


def validate_families(view, parents):
    """A family names published children of its parent; its totals re-add exactly."""
    if FAMILY_FIELD not in view:
        return
    families = view[FAMILY_FIELD]
    if view['dimension'] != 'activity' or not isinstance(families, list) or not families:
        raise ValueError('invalid WI family extension')
    rows = view['groups'] + view.get('outcome_only_groups', []) + view.get('pull_request_only_groups', [])
    named = {g['key'].get('wi') for g in rows}
    outputs = view.get('pull_request_outputs')
    counts = None if not outputs or outputs['status'] == 'unavailable' else {
        w['wi']: w['count'] for w in outputs['work_items']}
    unreadable = set((outputs or {}).get('unreadable_work_items') or [])
    wis, seen = [], set()
    for family in families:
        if (not isinstance(family, dict)
                or set(family) != {'wi', 'children', 'cost_usd', 'cost_complete', 'pull_request_count'}
                or not isinstance(family['wi'], str) or not re.fullmatch(_WI, family['wi'])
                or not isinstance(family['children'], list) or not family['children']
                or family['children'] != sorted(set(family['children']))
                or any(parents.get(c) != family['wi'] or c not in named or c in seen
                       for c in family['children'])
                or type(family['cost_complete']) is not bool):
            raise ValueError('invalid WI family')
        seen.update(family['children'])
        wis.append(family['wi'])
        _amount(family['cost_usd'])
        members = [family['wi'], *family['children']]
        parts = [_member_cost(view['groups'], wi) for wi in members]
        priced = any(p[1] for p in parts)
        expected = format(sum((p[0] for p in parts), Decimal(0)), 'f') if priced else None
        if (family['cost_usd'] is None) != (expected is None) or (
                expected is not None and Decimal(family['cost_usd']) != Decimal(expected)) or (
                family['cost_complete'] != (priced and all(p[2] for p in parts))):
            raise ValueError('WI family amount does not re-add')
        count = family['pull_request_count']
        known = None if counts is None or unreadable & set(members) else sum(
            counts.get(wi, 0) for wi in members)
        if count != known or (count is not None and (type(count) is not int or count < 0)):
            raise ValueError('WI family PR count does not re-add')
    if wis != sorted(set(wis)) or seen & set(wis):
        raise ValueError('WI families must be distinct parents')
