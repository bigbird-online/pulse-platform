"""Reconcile persisted CC model counters with native detail, never sum samples.

The counter is the entire root session including children, not one Host turn.
Native detail stays visible. Only an evidenced nonnegative unitemized residual
is added, per interval between consecutive counters; no child/date allocation
inside one interval is invented.
"""
from bisect import bisect_right
from collections import defaultdict
from dataclasses import replace
import re

from .events import parse_timestamp, utc_text
from .receipts import TOKEN_FIELDS, canonical, reference

BASIS = 'cc_persisted_session_counter.v1'
RUNTIMES = {'cc', 'cc_sdk'}


def scope_witness(scope):
    return reference(BASIS, scope)


def native_counter_evidence(scope, version):
    match = re.fullmatch(r'2\.1\.(\d+)', version) if isinstance(version, str) else None
    if not scope or not match or int(match[1]) < 277:
        return ()
    # Receipt evidence is a sorted set; digest order is not the claim order.
    return tuple(sorted((scope_witness(scope), reference('cc_counter_engine', [scope, version]))))


def supported_scopes(native_rows):
    versions = defaultdict(list)
    for row in native_rows:
        scope = row.payload.get('native_scope_ref')
        if scope:
            versions[scope].append(scope_witness(scope) in row.evidence)
    return {scope for scope, valid in versions.items() if valid and all(valid)}


def _activity_times(rows):
    """Each native scope's sorted message moments, of every model."""
    times = defaultdict(list)
    for row in rows:
        p = row.payload
        seen = p.get('interval_start') or row.occurred_at
        if p.get('native_scope_ref') and seen and _counter_detail(row):
            try:
                times[p['native_scope_ref']].append(parse_timestamp(seen, 'CC message'))
            except (ValueError, TypeError):
                continue
    return {scope: sorted(values) for scope, values in times.items()}


def _residual_start(previous_end, turn_start, activity):
    """The earliest moment a residual between two counters can have been spent.

    Unitemized usage is spent by CC activity. After the previous counter, none
    happened before both this counter's own turn and the session's next
    message (of any model), so an idle gap does not stretch the interval.
    """
    after = parse_timestamp(previous_end, 'previous counter')
    candidates = []
    if turn_start:
        candidates.append(parse_timestamp(turn_start, 'counter turn start'))
    following = bisect_right(activity, after)
    if following < len(activity):
        candidates.append(activity[following])
    if not candidates or min(candidates) <= after:
        return previous_end
    return utc_text(min(candidates))


def reconcile_session_counters(rows, scopes, native_refs, *, rejected_scopes=()):
    """Read projection only; every input fact remains in the original ledger.

    A verified native prefix must exactly equal the first model counter. This
    refuses unknown restored/fork baselines. Any regression, missing class,
    conflicting final, or cross-WI family keeps the old unresolved path.
    """
    groups = defaultdict(list)
    activity = _activity_times(rows)
    for row in rows:
        p = row.payload
        if p.get('native_scope_ref') in scopes and p['runtime'] in RUNTIMES:
            groups[(p['native_scope_ref'], p['model'])].append(row)
    suppressed, residuals, proofs = set(), [], []
    for (scope, model), family in groups.items():
        if scope in rejected_scopes or model is None:
            continue
        counters = [r for r in family if r.producer == r.payload['mapping'] == 'cc_sdk_result.v1'
                    and r.payload['granularity'] in {'turn_aggregate', 'session_counter'}]
        native = [r for r in family if r.entity_ref in native_refs
                  and r.payload['mapping'] == 'cc_message.v1' and r.payload['granularity'] == 'request']
        if not counters or not native:
            continue
        def at(r):
            return parse_timestamp(r.payload.get('interval_end') or r.occurred_at, 'CC observation')
        try:
            counters.sort(key=lambda r: (at(r), r.fact_ref))
            native.sort(key=lambda r: (at(r), r.fact_ref))
        except (ValueError, TypeError):
            continue
        if any(not c.payload['final'] for c in counters):
            continue
        if any(any(type(r.payload['tokens'].get(k)) is not int for k in TOKEN_FIELDS)
               for r in counters + native):
            continue
        if any(any(b.payload['tokens'][k] < a.payload['tokens'][k] for k in TOKEN_FIELDS)
               or (at(a) == at(b) and a.payload['tokens'] != b.payload['tokens'])
               for a, b in zip(counters, counters[1:])):
            continue
        first, latest = counters[0], counters[-1]
        covered = [r for r in native if at(r) <= at(latest)]
        if not covered:
            continue
        identities = [{r.payload.get(k) for r in counters + covered if r.payload.get(k) is not None}
                      for k in ('wi', 'executor', 'responsible', 'delegator')]
        if any(len(values) > 1 or values & {'ambiguous', 'excluded', 'unassigned'} for values in identities):
            continue
        # First exact prefix is the anchor, not an assumed zero baseline.
        totals = dict.fromkeys(TOKEN_FIELDS, 0)
        pos = 0
        zero_at = None
        failed = False
        unitemized = []  # Cumulative unitemized usage at each counter.
        for index, counter in enumerate(counters):
            while pos < len(covered) and at(covered[pos]) <= at(counter):
                for key in TOKEN_FIELDS:
                    totals[key] += covered[pos].payload['tokens'][key]
                pos += 1
            if index == 0 and (pos == 0 or totals != counter.payload['tokens']):
                failed = True
                break
            unitemized.append({k: counter.payload['tokens'][k] - totals[k] for k in TOKEN_FIELDS})
            if totals == counter.payload['tokens']:
                zero_at = counter
        if failed or zero_at is None:
            continue
        difference = {k: latest.payload['tokens'][k] - totals[k] for k in TOKEN_FIELDS}
        if any(value < 0 for value in difference.values()):
            continue
        # Non-native Host messages are alternative observations of this same
        # root. Do not discard independently final or unbounded measurements.
        other = [r for r in family if r.payload['mapping'] == 'cc_message.v1'
                 and r.entity_ref not in native_refs]
        if other:
            continue
        refs = {c.entity_ref for c in counters}
        suppressed.update(refs)
        # The unitemized remainder grows between consecutive counters; each
        # increase belongs to the interval between them, so a day boundary
        # crosses at most one turn. Detail can precede its counter (a running
        # background agent's messages join the counter when it ends) or follow
        # it, so one measurement may over- or understate. The remainder known
        # by each counter is the least one measured from then on, never below
        # zero: nondecreasing, ending exactly at the total.
        start = counters.index(zero_at)
        bound = [dict(unitemized[-1])]
        for measured in reversed(unitemized[start:-1]):
            bound.append({k: max(0, min(measured[k], bound[-1][k])) for k in TOKEN_FIELDS})
        bound.reverse()
        steps = [(counters[start + i - 1], counters[start + i],
                  {k: bound[i][k] - bound[i - 1][k] for k in TOKEN_FIELDS})
                 for i in range(1, len(bound))]
        residual_refs = []
        for previous, counter, amount in steps:
            if not any(amount.values()):
                continue
            p = counter.payload
            # A result with no own WI still belongs to the unanimously assigned
            # native family. This is accounting inheritance, not an execution grant.
            for field, values in zip(('wi', 'executor', 'responsible', 'delegator'), identities):
                if p.get(field) is None and len(values) == 1:
                    p[field] = next(iter(values))
            native_difference = {'inputTokens': amount['uncached_input'],
                'cacheReadInputTokens': amount['cache_read'],
                'cacheCreationInputTokens': amount['cache_write'], 'outputTokens': amount['output']}
            p.update(tokens=amount, native_tokens=native_difference, reported_total_tokens=None,
                     mapping='cc_cumulative_residual.v1', granularity='turn_aggregate', covers=[],
                     interval_start=_residual_start(previous.payload.get('interval_end') or previous.occurred_at,
                                                    p.get('interval_start'), activity.get(scope, [])),
                     interval_end=p.get('interval_end') or counter.occurred_at)
            # Preserve the counter's source identity and make the derived scope
            # explicit. Its original full counters are never overwritten.
            residuals.append(replace(counter, _payload_json=canonical(p),
                evidence=tuple(sorted(set(counter.evidence) | {c.fact_ref for c in counters}
                                      | {r.fact_ref for r in covered}))))
            residual_refs.append(counter.entity_ref)
        proofs.append({'basis': BASIS, 'native_scope_ref': scope, 'model': model,
            'counter_refs': sorted(refs), 'baseline_fact_ref': first.fact_ref,
            'latest_fact_ref': latest.fact_ref, 'residual_entity_refs': residual_refs,
            'latest_at': latest.payload.get('interval_end') or latest.occurred_at,
            'native_observation_count': len(covered),
            'native_tokens': totals, 'cumulative_tokens': latest.payload['tokens'],
            'unitemized_tokens': difference, 'residual_after': zero_at.occurred_at,
            'detail_and_residual_count_once': True})
    return [r for r in rows if r.entity_ref not in suppressed], residuals, suppressed, proofs


UNITEMIZED_BASIS = 'cc_unitemized_model_counter.v1'
IDENTITY_FIELDS = ('wi', 'executor', 'responsible', 'delegator')


def _model_counter(row):
    p = row.payload
    return (row.producer == p['mapping'] == 'cc_sdk_result.v1' and p['granularity'] == 'turn_aggregate'
            and p['final'])


def _counter_detail(row):
    p = row.payload
    return p['mapping'] == 'cc_message.v1' and p['granularity'] == 'request'


def reconcile_unitemized_counters(rows):
    """Count each CC session counter increase exactly once.

    A Host `cc_sdk_result` is CC's cumulative counter of the root session and
    model (`modelUsage`), never the usage of one Host attempt. Every result is
    therefore read as its increase over the previous result of the same native
    scope/model. Message detail (native or Host-observed) inside that interval
    stays visible; only the nonnegative remainder becomes a
    `cc_cumulative_residual.v1`. Internal calls of an auxiliary model never
    appear as messages, so its whole increase is that remainder.

    Without message detail the first counter starts no earlier than the
    session's first observed usage of any model: an auxiliary model is called
    inside the session's turns. A session with no other usage leaves it with
    no known start, time-unallocated. With detail, the first counter is only the baseline:
    its unknown earlier usage is not reconstructed. A remainder with a
    negative class means the detail and counter are not time-aligned (for
    example a cancelled request's snapshot); nothing is emitted and the next
    counter is measured from the last reconciled one. A counter with an
    unknown class, or two different counters at one time, is a
    contradiction of the whole family (reported separately: a missing field
    is not a regression). A decreasing counter is a regression of that
    result only: it starts a new segment as its baseline. Families mixing
    other aggregate kinds, or detail with unknown counts, keep the original
    summary/detail reconciliation.
    """
    families, details, foreign = defaultdict(list), defaultdict(list), set()
    first_seen: dict[str, tuple] = {}
    activity = _activity_times(rows)
    for row in rows:
        p = row.payload
        scope = p.get('native_scope_ref')
        if not scope or p['runtime'] not in RUNTIMES or p['model'] is None:
            continue
        seen = p.get('interval_start') or row.occurred_at
        if seen and not _model_counter(row):
            try:
                moment = (parse_timestamp(seen, 'CC session usage'), seen)
            except (ValueError, TypeError):
                moment = None
            if moment and (scope not in first_seen or moment < first_seen[scope]):
                first_seen[scope] = moment
        key = (scope, p['model'])
        if _model_counter(row):
            families[key].append(row)
        elif _counter_detail(row):
            details[key].append(row)
        else:
            foreign.add(key)
    suppressed, missing, regressed, residuals, proofs = set(), set(), set(), [], []
    for (scope, model), family in families.items():
        if (scope, model) in foreign:
            continue
        def at(r):
            return parse_timestamp(r.payload.get('interval_end') or r.occurred_at, 'CC counter')
        refs = {r.entity_ref for r in family}
        detail = details.get((scope, model), [])
        try:
            family.sort(key=lambda r: (at(r), r.fact_ref))
        except (ValueError, TypeError):
            regressed.update(refs)
            continue
        if any(any(type(r.payload['tokens'].get(k)) is not int for k in TOKEN_FIELDS) for r in family):
            missing.update(refs)
            continue
        if any(at(a) == at(b) and a.payload['tokens'] != b.payload['tokens']
               for a, b in zip(family, family[1:])):
            regressed.update(refs)
            continue
        # A decrease (e.g. a counter resumed after a Relay restart) splits the
        # family: only that result's increase is unknown, and it becomes the
        # baseline of the next segment.
        broken = {b.entity_ref for a, b in zip(family, family[1:])
                  if any(b.payload['tokens'][k] < a.payload['tokens'][k] for k in TOKEN_FIELDS)}
        try:
            detail_tokens = sorted(((at(r), r, r.payload) for r in detail),
                                   key=lambda item: (item[0], item[1].fact_ref))
        except (ValueError, TypeError):
            continue
        listed = {ref for r in family for ref in r.payload['covers']}
        if not listed <= {r.entity_ref for _, r, _ in detail_tokens}:
            # A listed message outside this scope/model contradicts the claim.
            continue
        if detail_tokens and (len(family) < 2 or any(
                any(type(q['tokens'].get(k)) is not int for k in TOKEN_FIELDS) for _, _, q in detail_tokens)):
            # One result has no earlier counter to difference against; it
            # stays the attempt summary of its listed/interval detail.
            continue
        regressed.update(broken)
        suppressed.update(refs - broken)
        # With detail, the first counter only anchors later increases.
        previous = family[0] if detail_tokens else None
        start = 1 if detail_tokens else 0
        pos = 0
        while previous is not None and pos < len(detail_tokens) and detail_tokens[pos][0] <= at(previous):
            pos += 1
        increments, itemized = [], dict.fromkeys(TOKEN_FIELDS, 0)
        for counter in family[start:]:
            if counter.entity_ref in broken:
                previous = counter
                while pos < len(detail_tokens) and detail_tokens[pos][0] <= at(previous):
                    pos += 1
                continue
            p = counter.payload
            base = previous.payload['tokens'] if previous else dict.fromkeys(TOKEN_FIELDS, 0)
            end, covered = pos, []
            while end < len(detail_tokens) and detail_tokens[end][0] <= at(counter):
                covered.append(detail_tokens[end])
                end += 1
            amount = {k: p['tokens'][k] - base[k] - sum(q['tokens'][k] for _, _, q in covered)
                      for k in TOKEN_FIELDS}
            if any(value < 0 for value in amount.values()):
                continue
            pos = end
            for k in TOKEN_FIELDS:
                itemized[k] += sum(q['tokens'][k] for _, _, q in covered)
            if any(amount.values()):
                # A result without its own identity inherits the unanimous
                # identity of the detail it reconciles; never an execution grant.
                for field in IDENTITY_FIELDS:
                    values = {q.get(field) for _, _, q in covered if q.get(field) is not None}
                    if p.get(field) is None and len(values) == 1 and not values & {'ambiguous', 'excluded', 'unassigned'}:
                        p[field] = next(iter(values))
                p.update(tokens=amount, reported_total_tokens=None, covers=[],
                         native_tokens={'inputTokens': amount['uncached_input'],
                                        'cacheReadInputTokens': amount['cache_read'],
                                        'cacheCreationInputTokens': amount['cache_write'],
                                        'outputTokens': amount['output']},
                         mapping='cc_cumulative_residual.v1')
                session_start = first_seen.get(scope)
                if previous is None and session_start and session_start[0] <= at(counter):
                    # An auxiliary model runs inside turns, after the session's first usage.
                    p.update(granularity='turn_aggregate', interval_start=session_start[1],
                             interval_end=p.get('interval_end') or counter.occurred_at)
                elif previous is None:
                    # Usage before the first observed counter has no known start.
                    p.update(granularity='session_counter', interval_start=None, interval_end=None)
                else:
                    p.update(granularity='turn_aggregate',
                             interval_start=_residual_start(previous.payload.get('interval_end') or previous.occurred_at,
                                                            p.get('interval_start'), activity.get(scope, [])),
                             interval_end=p.get('interval_end') or counter.occurred_at)
                evidence = ({counter.fact_ref} | ({previous.fact_ref} if previous else set())
                            | {r.fact_ref for _, r, _ in covered})
                residuals.append(replace(counter, _payload_json=canonical(p),
                                         evidence=tuple(sorted(set(counter.evidence) | evidence))))
                increments.append(counter.entity_ref)
            previous = counter
        proofs.append({'basis': UNITEMIZED_BASIS, 'native_scope_ref': scope, 'model': model,
                       'counter_refs': sorted(refs), 'residual_entity_refs': increments,
                       'baseline_fact_ref': family[0].fact_ref if detail_tokens else None,
                       'reconciled_fact_ref': previous.fact_ref if previous else None,
                       'latest_fact_ref': family[-1].fact_ref,
                       'itemized_tokens': itemized if detail_tokens else None,
                       'cumulative_tokens': family[-1].payload['tokens']})
    kept = [r for r in rows if r.entity_ref not in suppressed | missing | regressed]
    return kept, residuals, suppressed, missing, regressed, proofs
