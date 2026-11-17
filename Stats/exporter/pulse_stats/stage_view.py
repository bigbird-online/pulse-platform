"""Validate the optional stage-output projection, without storing or pricing.

Quantities remain ComputeCost's calculation. Here we check reference/window
partitions and unknown/conflict boundaries, never make a replacement total.
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from zoneinfo import ZoneInfo

DIAGNOSTICS = {'membership_conflicts', 'revision_conflicts', 'shared_refs',
               'unknown_refs', 'unresolved_link_targets', 'time_unallocated_refs'}
KEY = ('category', 'unit', 'comparison_basis', 'contribution')
ITEM = {'wi', 'context_ref', 'activity_refs', 'epistemic', 'object_ref', 'version_ref',
        'name', 'category', 'unit', 'comparison_basis', 'contribution', 'extent', 'quantity',
        'entity_ref', 'fact_ref', 'occurred_at', 'observed_at', 'producer', 'evidence'}


def _shape(value, fields):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid stage shape')


def _text(value, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError('invalid stage text')


def _ref(value):
    if not isinstance(value, str) or not re.fullmatch(r'ccf1_[a-f0-9]{64}', value):
        raise ValueError('invalid stage reference')


def _refs(values, *, nonempty=False):
    if not isinstance(values, list) or (nonempty and not values):
        raise ValueError('invalid stage references')
    for ref in values:
        _ref(ref)
    if len(values) != len(set(values)):
        raise ValueError('duplicate stage reference')


def _quantity(value):
    if value is None:
        return
    try:
        if not isinstance(value, str):
            raise ValueError('invalid stage quantity')
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError('invalid stage quantity')
    except InvalidOperation as exc:
        raise ValueError('invalid stage quantity') from exc


def _at(value):
    if not isinstance(value, str):
        raise ValueError('invalid stage time')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('invalid stage time')
    return result


def _groups(groups, items):
    if not isinstance(groups, list):
        raise ValueError('invalid stage groups')
    expected = defaultdict(list)
    for item in items:
        expected[tuple(item[k] for k in KEY)].append(item)
    seen = set()
    for group in groups:
        _shape(group, set(KEY) | {'object_count', 'object_refs', 'quantity', 'known_quantity',
                                 'unresolved_object_refs', 'epistemic', 'fact_refs'})
        for field in KEY:
            _text(group[field])
        key = tuple(group[k] for k in KEY)
        if key in seen or key not in expected:
            raise ValueError('duplicate or foreign stage group')
        seen.add(key)
        selected = expected[key]
        for field in ('object_refs', 'fact_refs', 'unresolved_object_refs'):
            _refs(group[field])
        if (type(group['object_count']) is not int
                or group['object_count'] != len(group['object_refs'])
                or set(group['object_refs']) != {i['object_ref'] for i in selected}
                or set(group['fact_refs']) != {i['fact_ref'] for i in selected}
                or not set(group['unresolved_object_refs']) <= set(group['object_refs'])
                or group['epistemic'] != 'calculated_from_agent_judgments'):
            raise ValueError('stage summary/detail mismatch')
        for field in ('quantity', 'known_quantity'):
            _quantity(group[field])
        if group['unresolved_object_refs'] and group['quantity'] is not None:
            raise ValueError('unresolved quantity cannot become a total')
        if not group['unresolved_object_refs'] and (group['quantity'] is None or group['known_quantity'] is None):
            raise ValueError('resolved stage quantity is missing')
        unknown = {i['object_ref'] for i in selected if i['quantity'] is None}
        if not unknown <= set(group['unresolved_object_refs']):
            raise ValueError('stage missing quantities were hidden')
    if seen != set(expected):
        raise ValueError('stage group partition incomplete')


def validate_stages(view):
    if 'stages' not in view:  # Older producer: unavailable, never zero.
        return
    stage = view['stages']
    _shape(stage, {'status', 'coverage', 'execution_authority', 'unsupported_filters',
                   'outputs', 'diagnostics', 'reason'})
    if (view['dimension'] != 'activity' or stage['coverage'] != 'recorded_declarations_only'
            or stage['execution_authority'] is not False
            or stage['status'] not in ('recorded_subset', 'unavailable', 'conflict', 'unsupported_filter')
            or not isinstance(stage['unsupported_filters'], list)):
        raise ValueError('invalid stage scope')
    for field in stage['unsupported_filters']:
        _text(field, 80)
    if stage['reason'] in {'stage_payload_too_large', 'stage_projection_failed'}:
        if stage['status'] != 'unavailable' or stage['outputs'] is not None or stage['diagnostics'] is not None:
            raise ValueError('truncated stage detail must remain unavailable')
        return
    if stage['reason'] is not None:
        raise ValueError('unsupported stage reason')
    diagnostics = stage['diagnostics']
    _shape(diagnostics, DIAGNOSTICS)
    for values in diagnostics.values():
        _refs(values)
    if stage['unsupported_filters'] and stage['status'] != 'unsupported_filter':
        raise ValueError('unsupported stage scope hidden')
    if ((diagnostics['membership_conflicts'] or diagnostics['revision_conflicts'])
            and stage['status'] not in ('conflict', 'unsupported_filter')):
        raise ValueError('stage conflicts hidden')
    outputs = stage['outputs']
    _shape(outputs, {'items', 'formed', 'cumulative', 'accepted'})
    accepted = outputs['accepted']
    if accepted.get('status') == 'unavailable':
        if set(accepted) != {'status', 'reason'} or accepted['reason'] not in {
                'native_exact_version_acceptance_not_joined', 'native_assessment_conflict'}:
            raise ValueError('stage declaration cannot mint acceptance')
    else:
        _shape(accepted, {'status', 'reason', 'object_count', 'groups', 'items'})
        if accepted['status'] != 'recorded_subset' or accepted['reason'] is not None:
            raise ValueError('invalid acceptance coverage')
    _shape(outputs['cumulative'], {'object_count', 'groups'})
    if not isinstance(outputs['items'], list):
        raise ValueError('invalid stage items')
    start, end = (datetime.fromisoformat(view['query'][k]).replace(tzinfo=ZoneInfo(view['query']['timezone']))
                  for k in ('from_date', 'to_date'))
    seen, entities, period_items = set(), set(), []
    for item in outputs['items']:
        _shape(item, ITEM)
        for field in ('context_ref', 'object_ref', 'version_ref', 'entity_ref', 'fact_ref'):
            _ref(item[field])
        for field in ('activity_refs', 'evidence'):
            _refs(item[field], nonempty=True)
        for field in ('name', 'category', 'unit', 'comparison_basis', 'extent', 'producer'):
            _text(item[field], 1024 if field == 'extent' else 256)
        _quantity(item['quantity'])
        if (item['epistemic'] != 'agent_judgment' or item['contribution'] not in ('new', 'improved', 'reused')
                or item['fact_ref'] in seen or item['entity_ref'] in entities
                or (item['wi'] is not None and (not isinstance(item['wi'], str)
                    or not re.fullmatch(r'wi_[A-Za-z0-9_]+|ambiguous', item['wi'])))):
            raise ValueError('invalid stage contribution')
        seen.add(item['fact_ref']); entities.add(item['entity_ref'])
        observed = _at(item['observed_at'])
        if view['recorded_at'] is None or observed > _at(view['recorded_at']):
            raise ValueError('stage escaped receipt watermark')
        if item['occurred_at'] is not None:
            at = _at(item['occurred_at'])
            if at >= end:
                raise ValueError('stage escaped cumulative cutoff')
            if start <= at:
                period_items.append(item)
        elif item['fact_ref'] not in diagnostics['time_unallocated_refs']:
            raise ValueError('unallocated stage time hidden')
    if stage['status'] in ('unavailable', 'unsupported_filter') and outputs['items']:
        raise ValueError('unavailable stages cannot carry outputs')
    cumulative = outputs['cumulative']
    if (type(cumulative['object_count']) is not int
            or cumulative['object_count'] != len({i['object_ref'] for i in outputs['items']})):
        raise ValueError('stage object inventory mismatch')
    _groups(outputs['formed'], period_items)
    _groups(cumulative['groups'], outputs['items'])
    validate_accepted(accepted, outputs['items'], view['recorded_at'])


def validate_accepted(accepted, outputs, recorded_at):
    if accepted['status'] == 'unavailable':
        return
    by_fact = {row['fact_ref']: row for row in outputs}
    seen, selected = set(), []
    if not isinstance(accepted['items'], list) or not accepted['items']:
        raise ValueError('empty accepted subset')
    for item in accepted['items']:
        _shape(item, {'output_fact_ref', 'object_ref', 'version_ref', 'candidate_ref', 'verification_ref',
                     'waiver_ref', 'human_review_ref', 'accepted_at', 'scope'})
        for key in ('output_fact_ref', 'object_ref', 'version_ref', 'candidate_ref', 'human_review_ref'):
            _ref(item[key])
        refs = [item[k] for k in ('verification_ref', 'waiver_ref') if item[k] is not None]
        if len(refs) != 1:
            raise ValueError('accepted version needs verification or an explicit waiver')
        _ref(refs[0])
        output = by_fact.get(item['output_fact_ref'])
        if (output is None or item['output_fact_ref'] in seen or item['scope'] != 'final'
                or any(output[k] != item[k] for k in ('object_ref', 'version_ref'))
                or recorded_at is None or _at(item['accepted_at']) > _at(recorded_at)):
            raise ValueError('acceptance is not bound to the displayed exact version')
        seen.add(item['output_fact_ref']); selected.append(output)
    if type(accepted['object_count']) is not int or accepted['object_count'] != len({r['object_ref'] for r in selected}):
        raise ValueError('accepted object inventory differs')
    _groups(accepted['groups'], selected)
