"""Exact reported-usage timing inside one native turn; never prorate tokens.

The timestamps are native metering notifications, not invented execution times.
The original aggregate identity, interval, quantities and old watermark survive.
"""
from __future__ import annotations
from dataclasses import replace
import json
from .events import parse_timestamp, utc_text
from .receipts import ReceiptError, TOKEN_FIELDS, REF, canonical, reference, digest
from .token_semantics import InputTokenClasses

FEATURE = 'native_metering.v1'
BASIS = 'codex_token_count_observed.v1'
NATIVE = ('input_tokens','cached_input_tokens','cache_write_input_tokens','output_tokens')
MAX_POINTS = 8192


def validate_metering(payload):
    metering = payload.get('metering')
    if metering is None:
        return
    if (not isinstance(metering, dict) or set(metering) != {'basis','baseline_ref','points'}
            or metering['basis'] != BASIS or not isinstance(metering['baseline_ref'],str)
            or not REF.fullmatch(metering['baseline_ref'])
            or payload.get('runtime') != 'codex' or payload.get('granularity') != 'turn_aggregate'
            or not payload.get('final') or not payload.get('interval_start') or not payload.get('interval_end')):
        raise ReceiptError('invalid native metering identity')
    points = metering['points']
    if not isinstance(points,list) or not 1 <= len(points) <= MAX_POINTS:
        raise ReceiptError('invalid native metering inventory')
    low = parse_timestamp(payload['interval_start'],'metering start')
    high = parse_timestamp(payload['interval_end'],'metering end')
    previous = low
    totals = dict.fromkeys(TOKEN_FIELDS,0)
    seen = set()
    for point in points:
        if (not isinstance(point,dict) or set(point) != {'at','tokens','evidence_ref'}
                or not isinstance(point['evidence_ref'],str) or not REF.fullmatch(point['evidence_ref'])
                or point['evidence_ref'] in seen
                or not isinstance(point['tokens'],dict) or set(point['tokens']) != set(TOKEN_FIELDS)):
            raise ReceiptError('invalid native metering point')
        seen.add(point['evidence_ref'])
        at = parse_timestamp(point['at'],'metered at')
        if not previous <= at <= high:
            raise ReceiptError('native metering time differs from aggregate')
        previous = at
        for key in TOKEN_FIELDS:
            value = point['tokens'][key]
            if type(value) is not int or value < 0:
                raise ReceiptError('native metering requires complete nonnegative classes')
            totals[key] += value
    if totals != payload['tokens']:
        raise ReceiptError('native metering does not conserve the original aggregate')



def metering_refines(coarse, metered):
    """Validated precision shape; source-dependent discards require shared proof.

    A forward upgrade appends its explicit refinement edge and retains the old
    fact, including legacy observations without source evidence. Discarding a
    later coarse observation or joining concurrent heads needs the additional
    shared-source predicate; equal quantities alone do not prove that relation.
    """
    if (coarse.kind != metered.kind or coarse.kind != 'usage'
            or coarse.entity_ref != metered.entity_ref
            or coarse.producer != metered.producer or metered.producer != 'codex_rollout.v1'
            or coarse.occurred_at != metered.occurred_at):
        return False
    old, new = coarse.payload, metered.payload
    if (old.get('metering') is not None or new.get('metering') is None
            or any(old.get(k) != new.get(k) for k in set(old) | set(new) if k != 'metering')):
        return False
    validate_metering(new)
    return True


def shared_metering_source(left, right):
    """A coarse-copy discard/projection requires nonempty shared provenance."""
    return bool(set(left.evidence) & set(right.evidence))


def metered_observation(observations):
    """Reconcile a coarse reread with its evidenced exact metering refinement.

    Keep the selected fact identity and every original observation. A changed
    counter, interval, actor, model or timeline remains a concurrent conflict.
    This projection does not add supersession edges to the immutable ledger.
    """
    candidates = [r for r in observations if r.kind == 'usage' and r.payload.get('metering')]
    if not candidates or len({(r.producer, r.occurred_at, r._payload_json) for r in candidates}) != 1:
        return None
    chosen = min(candidates, key=lambda r: r.fact_ref)
    if chosen.producer != 'codex_rollout.v1' or not chosen.evidence or not all(
            r.entity_ref == chosen.entity_ref and shared_metering_source(r, chosen)
            and ((r.producer == chosen.producer and r.occurred_at == chosen.occurred_at
                  and r._payload_json == chosen._payload_json) or metering_refines(r, chosen))
            for r in observations):
        return None
    validate_metering(chosen.payload)
    return replace(chosen, evidence=tuple(sorted(set().union(*(r.evidence for r in observations)))))


def extract_metering(observation, payload, cache):
    """Read only complete token notifications in the original selected prefix."""
    if (not payload.get('final') or not payload.get('interval_start')
            or any(payload['tokens'][k] is None for k in TOKEN_FIELDS)
            or not set(NATIVE) <= observation.previous_fields):
        return None
    path = observation.source_path
    if path.is_symlink():
        return None
    try:
        if path not in cache:
            indexed = []
            with path.open('rb') as stream:
                for position,raw in enumerate(stream,1):
                    if not raw.endswith(b'\n'):
                        break
                    if b'"token_count"' not in raw:
                        continue
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        indexed.append((position,None))
                        continue
                    if (value.get('type') == 'event_msg'
                            and value.get('payload',{}).get('type') == 'token_count'):
                        indexed.append((position,value))
            cache[path] = indexed
        low = parse_timestamp(payload['interval_start'],'native start')
        high = parse_timestamp(payload['interval_end'],'native end')
        previous = {k:observation.previous_counter[k] for k in NATIVE}
        baseline = dict(previous)
        points = []
        last_at = low
        for position,row in cache[path]:
            if position > observation.source_position:
                break
            if row is None:
                return None
            at = parse_timestamp(row['timestamp'],'native notification')
            if at < low:
                continue
            if at < last_at or at > high:
                return None
            info = row['payload'].get('info')
            if not isinstance(info,dict) or info.get('total_token_usage') is None:
                continue
            total = info['total_token_usage']
            if not isinstance(total,dict) or any(type(total.get(k)) is not int for k in NATIVE):
                return None
            delta = {k:total[k]-previous[k] for k in NATIVE}
            if any(v < 0 for v in delta.values()):
                return None
            inputs = InputTokenClasses.from_inclusive(
                delta['input_tokens'], delta['cached_input_tokens'], delta['cache_write_input_tokens'])
            tokens = {'uncached_input':inputs.require_uncached(),
                      'cache_read':delta['cached_input_tokens'],'cache_write':delta['cache_write_input_tokens'],
                      'output':delta['output_tokens']}
            points.append({'at':utc_text(at),'tokens':tokens,
                'evidence_ref':reference('native_metering_point',[payload['native_scope_ref'],position,digest(row)])})
            previous = {k:total[k] for k in NATIVE}
            last_at = at
        if not points or previous != {k:payload['native_tokens'].get(k) for k in NATIVE}:
            return None
        result = {'basis':BASIS,'baseline_ref':reference('native_metering_baseline',
            [payload['native_scope_ref'],payload['interval_start'],baseline]),'points':points}
        validate_metering({**payload,'metering':result})
        return result
    except (OSError,ValueError,TypeError,KeyError):
        return None


def metered_window(row, window):
    """Select actual notification amounts; retain one participating observation.

    No-window/WI queries retain the original aggregate. Calendar windows are
    half-open and sum original notifications, not elapsed-time proportions.
    Missing timing proof retains the old conservative unallocated result.
    """
    if window is None or row.kind != 'usage' or not row.payload.get('metering'):
        return row
    p = row.payload
    validate_metering(p)
    points = [point for point in p['metering']['points']
              if window[0] <= parse_timestamp(point['at'],'metered at') < window[1]]
    if not points:
        return None
    tokens = {k:sum(point['tokens'][k] for point in points) for k in TOKEN_FIELDS}
    projected = {**p,'tokens':tokens,'reported_total_tokens':sum(tokens.values()),
                 'interval_start':points[0]['at'],'interval_end':points[-1]['at'],
                 'metering':{**p['metering'],'points':points}}
    return replace(row,_payload_json=canonical(projected))
