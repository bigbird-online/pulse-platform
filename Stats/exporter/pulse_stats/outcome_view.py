"""Validation for the optional Outcome extension of a joint cost view."""
from __future__ import annotations
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def references(values):
    if not isinstance(values, list) or any(not isinstance(v, str) or not re.fullmatch(r'[a-zA-Z0-9_:-]{1,200}', v) for v in values):
        raise ValueError('invalid opaque evidence references')
    if len(values) != len(set(values)):
        raise ValueError('duplicate evidence references')


def validate_outcomes(view):
    if 'outcomes' not in view:
        return
    result = view['outcomes']
    fields = {'status','count','known_count','metric_scope','items','unallocated_refs','conflict_refs','coverage'}
    if view['dimension'] != 'activity' or not isinstance(result, dict) or set(result) != fields:
        raise ValueError('invalid Outcome extension')
    if result['metric_scope'] != 'wi_outcomes_in_period' or result['status'] not in {'complete','observed','partial','unavailable','conflict'}:
        raise ValueError('invalid Outcome scope or status')
    references(result['unallocated_refs']); references(result['conflict_refs'])
    if result['coverage'] not in {'complete','observed_subset','unknown'}:
        raise ValueError('invalid Outcome coverage')
    if type(result['known_count']) is not int or result['known_count'] < 0:
        raise ValueError('invalid Outcome count')
    if result['count'] is not None and (type(result['count']) is not int or result['count'] != result['known_count']):
        raise ValueError('Outcome count must match known objects')
    if result['status'] in {'unavailable','conflict'} and result['count'] is not None:
        raise ValueError('unavailable Outcome must not become zero')
    if result['count'] == 0 and result['status'] != 'complete':
        raise ValueError('unconfirmed absence is not zero')
    items = {}
    for item in result['items']:
        expected = {'outcome_ref','fact_ref','wi','responsible','status','counted','occurred_at','candidate_ref','artifact_ref','acceptance_ref','review_decisions','review_refs','evidence_refs','title','summary'}
        # Newer queries add who accepted it; older publications omit the field.
        if not isinstance(item, dict) or set(item) - {'verification_level'} != expected or item.get(
                'verification_level') not in {None, 'independent', 'human', 'self_with_controls'}:
            raise ValueError('invalid Outcome item')
        if item['status'] not in {'submitted','accepted','partial','rejected','blocked','unknown'} or type(item['counted']) is not bool or item['counted'] != (item['status'] != 'unknown'):
            raise ValueError('review-only evidence is not a delivered Outcome')
        if not isinstance(item['wi'], str) or not re.fullmatch(r'wi_\d{8}_\d{3}', item['wi']):
            raise ValueError('Outcome requires a real WI')
        for field in ('outcome_ref','fact_ref','candidate_ref','artifact_ref','acceptance_ref'):
            references([item[field]])
        if item['outcome_ref'] in items:
            raise ValueError('duplicate Outcome object')
        items[item['outcome_ref']] = item
        at = datetime.fromisoformat(item['occurred_at'].replace('Z','+00:00'))
        if at.tzinfo is None:
            raise ValueError('Outcome time requires timezone')
        day = at.astimezone(ZoneInfo(view['query']['timezone'])).date().isoformat()
        if not view['query']['from_date'] <= day < view['query']['to_date']:
            raise ValueError('Outcome escaped query period')
        for name, limit in (('title',240),('summary',1200)):
            value=item[name]
            if value is not None and (not isinstance(value,str) or not value or len(value)>limit or any(ord(c)<32 and c not in '\n\t' for c in value)):
                raise ValueError('invalid bounded Outcome prose')
        references(item['review_refs']); references(item['evidence_refs'])
        if any(d not in {'approved','changes_requested','pending','waived','not_executed','unknown'} for d in item['review_decisions']):
            raise ValueError('invalid upstream review state')
    if sum(item['counted'] for item in items.values()) != result['known_count']:
        raise ValueError('Outcome object count does not conserve')
    all_rows = view['groups'] + view['outcome_only_groups']
    for group in all_rows:
        o = group['outcomes']
        if set(o) != {'status','count','known_count','refs','metric_scope'} or o['metric_scope'] != 'wi_reference_not_runtime_allocation':
            raise ValueError('invalid row Outcome scope')
        references(o['refs'])
        if any(ref not in items or items[ref]['wi'] != group['key']['wi'] for ref in o['refs']):
            raise ValueError('row refers to another WI Outcome')
        count = sum(items[ref]['counted'] for ref in o['refs'])
        if o['known_count'] != count or (o['count'] is not None and o['count'] != count):
            raise ValueError('row Outcome count mismatch')
        detail = group.get('detail')
        if detail is not None:
            for field in ('fact','attribution'):
                references(detail[field+'_refs'])
                if len(detail[field+'_refs']) > 20 or type(detail[field+'_ref_count']) is not int or detail[field+'_ref_count'] < len(detail[field+'_refs']):
                    raise ValueError('invalid bounded row evidence')
    # Rows for WIs with only merged PRs in the period are presentation rows too.
    all_rows = all_rows + view.get('pull_request_only_groups', [])
    keys = [tuple(sorted(row['key'].items())) for row in all_rows]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate presentation row')
    for row in view['outcome_only_groups']:
        if row['usage']['event_count'] != 0 or row['outcomes']['known_count'] <= 0 or row['cost']['machine_cash_usd'] is not None or row['cost']['partial_machine_cash_usd'] is not None:
            raise ValueError('Outcome-only row must not invent consumption')
    agents = sorted({r['key']['agent'] for r in all_rows if r['key']['agent'] not in {'unassigned','ambiguous','not_applicable'}})
    agents = sorted(set(agents) | {i['responsible'] for i in items.values() if i['counted'] and i['responsible']
                                  and i['responsible'] not in {'unassigned','ambiguous','not_applicable'}})
    if agents != view['active_agents']:
        raise ValueError('active Agent list does not match real activity')
