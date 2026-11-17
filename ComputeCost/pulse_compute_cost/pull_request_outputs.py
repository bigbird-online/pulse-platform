"""WI output: pull requests the native core merged itself (PR output gate).

The core's read-only ``stats-facts`` export lists, for each WorkItem, every
pull request registered on it that passed the core's heterogeneous review (or
a human's confirmation) and that the core merged and read back
(``merged_pull_requests``). This module only counts them: a PR belongs to the
WI it was registered on and to the Asia/Shanghai period containing its GitHub
merge time (the core's read-back time when GitHub gave none). No lines, files
or review prose are counted. Accepted WI Outcomes remain a separate measure.

An export from a core without the list is ``unavailable``, never zero; an
export with unreadable WorkItems is ``partial`` and those WIs have no count.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
import re
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

METRIC_SCOPE = 'wi_kernel_merged_pull_requests_in_period'
ZONE = ZoneInfo('Asia/Shanghai')
_WI = re.compile(r'wi_\d{8}_\d{3}')
_REPOSITORY = re.compile(r'[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}')
_SHA = re.compile(r'[0-9a-f]{40}')
_PER_PR_PRECISION = Decimal('0.000001')


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('timestamp text required')
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError('aware timestamp required')
    return moment


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def merged_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    """One exported kernel merge, normalized; malformed rows raise ValueError."""
    if not isinstance(raw, Mapping):
        raise ValueError('invalid merged pull request')
    repository, number = raw.get('repository'), raw.get('number')
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
        raise ValueError('invalid pull request repository')
    if type(number) is not int or number < 1:
        raise ValueError('invalid pull request number')
    commit = raw.get('merge_commit')
    if commit is not None and (not isinstance(commit, str) or not _SHA.fullmatch(commit)):
        raise ValueError('invalid merge commit')
    score, rounds = raw.get('score'), raw.get('reviewed_rounds')
    if score is not None and (type(score) is not int or not 0 <= score <= 100):
        raise ValueError('invalid review score')
    if type(rounds) is not int or rounds < 0:
        raise ValueError('invalid review rounds')
    if raw.get('decision') not in {'review', 'human'}:
        raise ValueError('invalid merge decision')
    moment = _instant(raw.get('merged_at')) or _instant(raw.get('observed_at'))
    return {'repository': repository, 'number': number, 'merge_commit': commit,
            'merged_at': None if moment is None else _utc(moment),
            'score': score, 'reviewed_rounds': rounds, 'decision': raw['decision']}


# The core export is refreshed with each publication (30 minutes); one missed cycle
# plus grace is stale, the same bound Stats uses for WI facts.
MAX_EXPORT_AGE = timedelta(minutes=75)


def read_merged(document: Mapping[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    """Kernel merges by WI from one core export, with its coverage.

    With ``now``, an export older than MAX_EXPORT_AGE, from the future or without a
    readable time is unavailable: a kept old file is never republished as current.
    """
    result: dict[str, Any] = {'status': 'unavailable', 'reason': 'core_facts_unavailable',
                              'by_wi': {}, 'owners': {}, 'unreadable': []}
    if not isinstance(document, Mapping) or not isinstance(document.get('items'), list):
        return result
    if now is not None:
        try:
            at = datetime.fromisoformat(str(document.get('generated_at')).replace('Z', '+00:00'))
            fresh = at.tzinfo is not None and now - MAX_EXPORT_AGE <= at <= now + timedelta(minutes=5)
        except ValueError:
            fresh = False
        if not fresh:
            return {**result, 'reason': 'core_facts_stale'}
    items = [i for i in document['items'] if isinstance(i, Mapping)]
    if any('merged_pull_requests' not in item for item in items):
        return {**result, 'reason': 'core_pull_requests_not_exported'}
    unreadable = {u.get('wi') for u in document.get('unavailable_items') or [] if isinstance(u, Mapping)}
    by_wi: dict[str, list[dict[str, Any]]] = {}
    owners: dict[str, str] = {}
    for item in items:
        wi = item.get('id')
        if not isinstance(wi, str) or not _WI.fullmatch(wi):
            continue
        if isinstance(item.get('owner'), str):
            owners[wi] = item['owner']
        try:
            rows = [merged_row(r) for r in item['merged_pull_requests']]
        except (ValueError, TypeError):
            unreadable.add(wi)
            continue
        if rows:
            by_wi[wi] = rows
    unreadable = sorted(w for w in unreadable if isinstance(w, str) and _WI.fullmatch(w))
    partial = bool(unreadable) or document.get('coverage') != 'complete'
    return {'status': 'partial' if partial else 'complete',
            'reason': 'core_items_unavailable' if partial else None,
            'by_wi': by_wi, 'owners': owners, 'unreadable': unreadable}


def merged_keys(merged: Mapping[str, Any]) -> set[tuple[str, int]]:
    """Every (repository, number) the core merged, across all WIs."""
    return {(r['repository'], r['number']) for rows in merged['by_wi'].values() for r in rows}


def _wi_costs(groups: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each WI's displayed amount: the sum of its rows' complete or priced amounts."""
    totals: dict[str, dict[str, Any]] = {}
    for group in groups:
        wi = group['key'].get('wi')
        if not isinstance(wi, str) or not _WI.fullmatch(wi):
            continue
        cost = group['cost']
        total = totals.setdefault(wi, {'amount': Decimal(0), 'priced': False, 'complete': True})
        value = cost.get('machine_cash_usd')
        if value is None:
            total['complete'] = False
            value = cost.get('partial_machine_cash_usd')
        if value is not None:
            total['amount'] += Decimal(value)
            total['priced'] = True
    return totals


def project(merged: Mapping[str, Any], groups: list[Mapping[str, Any]],
            start: date, end: date) -> dict[str, Any]:
    """PRs merged in [start, end) by WI, beside that WI's amount in the same view.

    A WI not listed has 0 PRs when the status is ``complete`` or ``partial``,
    except the ``unreadable_work_items``, whose count is unknown.
    """
    result: dict[str, Any] = {'status': merged['status'], 'reason': merged['reason'],
                              'metric_scope': METRIC_SCOPE, 'count': None, 'known_count': 0,
                              'work_items': [], 'unreadable_work_items': list(merged['unreadable']),
                              'time_unknown_count': 0}
    if merged['status'] == 'unavailable':
        return result
    costs = _wi_costs(groups)
    keys: set[tuple[str, int]] = set()
    for wi, rows in sorted(merged['by_wi'].items()):
        selected = []
        for row in rows:
            if row['merged_at'] is None:
                result['time_unknown_count'] += 1
                continue
            day = _instant(row['merged_at']).astimezone(ZONE).date()
            if start <= day < end:
                selected.append(row)
        if not selected:
            continue
        keys.update((r['repository'], r['number']) for r in selected)
        cost = costs.get(wi)
        amount = format(cost['amount'], 'f') if cost and cost['priced'] else None
        per = (format((cost['amount'] / len(selected)).quantize(_PER_PR_PRECISION, ROUND_HALF_UP), 'f')
               if amount is not None else None)
        result['work_items'].append({
            'wi': wi, 'count': len(selected),
            'cost_usd': amount, 'cost_complete': bool(cost and cost['complete'] and cost['priced']),
            'cost_per_pull_request_usd': per,
            'pull_requests': sorted(selected, key=lambda r: (r['merged_at'], r['repository'], r['number']))})
    result['known_count'] = len(keys)
    if result['time_unknown_count']:
        result.update(status='partial', reason=result['reason'] or 'merge_time_unknown')
    if result['status'] == 'complete':
        result['count'] = len(keys)
    return result


def only_groups(projection: Mapping[str, Any], groups: list[Mapping[str, Any]],
                group_by: Iterable[str], owners: Mapping[str, str]) -> list[dict[str, Any]]:
    """Rows for WIs whose PRs merged in the period without usage in it.

    They show the PR count beside an unknown amount; no consumption is invented.
    The Agent is the WI owner, the one the merged output belongs to.
    """
    present = {g['key'].get('wi') for g in groups}
    rows = []
    for item in projection['work_items']:
        if item['wi'] in present:
            continue
        key = dict.fromkeys(group_by, 'not_applicable')
        key.update(wi=item['wi'], agent=owners.get(item['wi']) or 'unassigned')
        rows.append({'key': key,
                     'cost': {'machine_cash_usd': None, 'partial_machine_cash_usd': None,
                              'cash_basis': 'openrouter_standard_token_cash',
                              'reasons': ['no_usage_observations'], 'unpriced': []},
                     'usage': {'tokens': {k: {'known': None, 'unknown_event_count': 0} for k in
                                          ('uncached_input', 'cache_read', 'cache_write', 'output')},
                               'event_count': 0, 'known_total_tokens': None}})
    return rows


def families(view: Mapping[str, Any], parents: Mapping[str, str]) -> list[dict[str, Any]]:
    """Each parent WI with a child named in this view: its own amount and PRs plus its children's.

    Display only. The parent's own rows keep their own values, and the view's totals
    never add a family on top of its members. ``pull_request_count`` is unknown when
    the PR records are unavailable or any member's were unreadable.
    """
    rows = (list(view['groups']) + list(view.get('outcome_only_groups', []))
            + list(view.get('pull_request_only_groups', [])))
    named = {g['key'].get('wi') for g in rows}
    members: dict[str, list[str]] = {}
    for child, parent in parents.items():
        if child in named and parent != child:
            members.setdefault(parent, []).append(child)
    costs = _wi_costs(view['groups'])
    outputs = view.get('pull_request_outputs')
    counts = None if not outputs or outputs['status'] == 'unavailable' else {
        w['wi']: w['count'] for w in outputs['work_items']}
    unreadable = set((outputs or {}).get('unreadable_work_items') or [])
    result = []
    for parent, children in sorted(members.items()):
        wis = [parent, *sorted(children)]
        total, priced, complete = Decimal(0), False, True
        for wi in wis:
            cost = costs.get(wi)
            if cost is not None:
                total += cost['amount']
                priced = priced or cost['priced']
                complete = complete and cost['complete']
        result.append({
            'wi': parent, 'children': sorted(children),
            'cost_usd': format(total, 'f') if priced else None,
            'cost_complete': priced and complete,
            'pull_request_count': None if counts is None or unreadable & set(wis)
            else sum(counts.get(wi, 0) for wi in wis)})
    return result
