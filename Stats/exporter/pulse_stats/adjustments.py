"""Closed weeks and adjustment entries in a cost view (read side only).

A view's original fields stay its own usage in the window. A publisher that
closes weeks adds, per view and per row, the entries booked in the window
against closed weeks (`adjustments`) and own usage plus those entries
(`total`). Rows with entries but no own usage are `adjustment_only_groups`.
The previous week's view names when it was closed (`closed_week`).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

PROBE_SCHEMA = 'pulse_stats_cost_adjustments/v1'
VIEW_FIELDS = frozenset({'adjustments', 'total', 'adjustment_only_groups', 'closed_week'})
GROUP_FIELDS = frozenset({'adjustments', 'total'})
REASONS = ('reattributed', 'quantity_revised', 'late_usage', 'withdrawn')
TOKEN_CLASSES = ('uncached_input', 'cache_read', 'cache_write', 'output')
_SIGNED = re.compile(r'-?[0-9]+(?:\.[0-9]+)?')
_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')
_PEER = re.compile(r'[a-z0-9_-]{1,40}')


def _int(value, *, signed=True):
    return type(value) is int and (signed or value >= 0) and abs(value) <= 2**63 - 1


def _signed_money(value):
    if not isinstance(value, str) or len(value) > 40 or not _SIGNED.fullmatch(value):
        raise ValueError('invalid adjustment amount')
    return Decimal(value)


def _monday(value):
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError('invalid origin week') from None
    if day.weekday() != 0 or day.isoformat() != value:
        raise ValueError('invalid origin week')
    return day


def _amounts(value):
    """Counts, signed token and amount differences of one set of entries."""
    count, unpriced = value.get('entry_count'), value.get('unpriced_entry_count')
    tokens = value.get('tokens')
    if (not _int(count, signed=False) or not _int(unpriced, signed=False) or unpriced > count
            or not isinstance(tokens, dict) or set(tokens) != set(TOKEN_CLASSES)
            or any(not _int(n) for n in tokens.values()) or not _int(value.get('known_total_tokens'))):
        raise ValueError('invalid adjustment quantities')
    return (count, unpriced, tuple(tokens[k] for k in TOKEN_CLASSES), value['known_total_tokens'],
            _signed_money(value.get('priced_machine_cash_usd')))


def _sum(parts):
    count = unpriced = total = 0
    tokens, money = [0] * len(TOKEN_CLASSES), Decimal(0)
    for c, u, t, k, m in parts:
        count, unpriced, total, money = count + c, unpriced + u, total + k, money + m
        tokens = [a + b for a, b in zip(tokens, t)]
    return count, unpriced, tuple(tokens), total, money


def _block(value):
    """One `adjustments` block: its totals and its parts by (origin week, reason)."""
    amount_fields = {'entry_count', 'unpriced_entry_count', 'tokens', 'known_total_tokens', 'priced_machine_cash_usd'}
    if not isinstance(value, dict) or set(value) != amount_fields | {'by_origin'}:
        raise ValueError('invalid adjustments')
    own = _amounts(value)
    origins = value['by_origin']
    if not isinstance(origins, list) or len(origins) > 512:
        raise ValueError('invalid adjustment origins')
    keys, parts = [], []
    for row in origins:
        if (not isinstance(row, dict) or set(row) != amount_fields | {'week_start', 'reason'}
                or row['reason'] not in REASONS):
            raise ValueError('invalid adjustment origin')
        _monday(row['week_start'])
        parts.append(_amounts(row))
        if parts[-1][0] <= 0:
            raise ValueError('empty adjustment origin')
        keys.append((row['week_start'], row['reason']))
    if keys != sorted(set(keys)) or _sum(parts) != own:
        raise ValueError('adjustment origins must partition the adjustments')
    return own, dict(zip(keys, parts))


def validate_adjustments(value):
    """One `adjustments` block; its origin breakdown partitions its own totals."""
    return _block(value)[0]


def _no_own_usage(group):
    """An adjustment-only row carries neither usage nor an amount of its own."""
    usage, cost = group['usage'], group['cost']
    tokens = usage.get('tokens') or {}
    if (usage.get('event_count') != 0 or usage.get('known_total_tokens') not in (0, None)
            or any((tokens.get(k) or {}).get('known') not in (0, None)
                   or (tokens.get(k) or {}).get('unknown_event_count') not in (0, None) for k in TOKEN_CLASSES)
            or cost.get('priced_event_count') not in (0, None)
            or any(Decimal(cost.get(k) or 0) != 0
                   for k in ('machine_cash_usd', 'partial_machine_cash_usd', 'priced_machine_cash_usd'))):
        raise ValueError('adjustment-only groups have no own usage')


def _row_key(group):
    return tuple(sorted(group['key'].items()))


def own_priced(cost):
    """The amount a reader shows for own usage: complete, else priced, else partial; none is 0."""
    if cost.get('machine_cash_usd') is not None:
        return Decimal(cost['machine_cash_usd'])
    if 'priced_machine_cash_usd' in cost:
        return Decimal(cost['priced_machine_cash_usd'] or 0)
    return Decimal(cost.get('partial_machine_cash_usd') or 0)


def validate_total(owner, adjustments):
    """`total` is own usage plus the entries, class by class; unknown stays unknown."""
    total, usage = owner.get('total'), owner.get('usage') or {}
    if not isinstance(total, dict) or set(total) != {'tokens', 'known_total_tokens', 'priced_machine_cash_usd'}:
        raise ValueError('invalid adjusted total')
    _, _, delta, known_delta, money = adjustments
    own_tokens = usage.get('tokens') or {}
    # Types first: `True == 1` would pass the sums below, and a reader decoding integers drops it.
    if (not isinstance(total['tokens'], dict) or set(total['tokens']) != set(TOKEN_CLASSES)
            or any(v is not None and not _int(v) for v in (*total['tokens'].values(), total['known_total_tokens']))):
        raise ValueError('invalid adjusted total')
    for name, change in zip(TOKEN_CLASSES, delta):
        own = (own_tokens.get(name) or {}).get('known')
        if total['tokens'][name] != (None if own is None else own + change):
            raise ValueError('adjusted total contradicts own usage plus adjustments')
    own = usage.get('known_total_tokens')
    if total['known_total_tokens'] != (None if own is None else own + known_delta):
        raise ValueError('adjusted total contradicts own usage plus adjustments')
    if _signed_money(total['priced_machine_cash_usd']) != own_priced(owner['cost']) + money:
        raise ValueError('adjusted total contradicts own usage plus adjustments')


def _closed_week(view):
    value = view['closed_week']
    if view['period'] != 'last_week':
        raise ValueError('only the previous week is closed')
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {'week_start', 'closed_at', 'watermark', 'gaps'}
            or value['week_start'] != view['query']['from_date']
            or not _int(value['watermark'], signed=False) or value['watermark'] != view['watermark']
            or not isinstance(value['closed_at'], str) or not isinstance(value['gaps'], list)):
        raise ValueError('invalid closed week')
    _monday(value['week_start'])
    try:
        closed = datetime.fromisoformat(value['closed_at'].replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('invalid closed week') from None
    # A week closes only after it has ended: not before the next Monday 00:00 of the view's zone.
    try:
        zone = ZoneInfo(view['query']['timezone'])
    except (KeyError, TypeError, ValueError, OSError):
        raise ValueError('invalid closed week') from None
    week_end = datetime.combine(date.fromisoformat(value['week_start']) + timedelta(days=7), time(), zone)
    if closed.tzinfo is None or closed < week_end:
        raise ValueError('invalid closed week')
    peers = [g.get('peer') if isinstance(g, dict) else None for g in value['gaps']]
    if (any(not isinstance(g, dict) or set(g) != {'peer', 'reason'} or not isinstance(g['peer'], str)
            or not _PEER.fullmatch(g['peer']) or not isinstance(g['reason'], str)
            or not _CODE.fullmatch(g['reason']) for g in value['gaps'])
            or peers != sorted(set(peers)) or len(peers) > 16):
        raise ValueError('invalid closed week gaps')


def validate_view(view, validate_row):
    """Check a view's optional closing fields; `validate_row` checks key, tenant, usage and cost."""
    if 'closed_week' in view:
        _closed_week(view)
    rows = [g for g in view['groups'] if 'adjustments' in g or 'total' in g]
    only = view.get('adjustment_only_groups')
    if 'adjustments' not in view:
        # Present at all, even as null, the field claims adjustments the view does not carry.
        if 'total' in view or 'adjustment_only_groups' in view or rows:
            raise ValueError('adjusted rows require view adjustments')
        return
    view_own, view_origins = _block(view['adjustments'])
    validate_total({**view['summary'], 'total': view.get('total')}, view_own)
    if not isinstance(only, list):
        raise ValueError('adjusted view requires adjustment_only_groups')
    origins = {}

    def book(group):
        own, parts = _block(group['adjustments'])
        validate_total(group, own)
        for key, part in parts.items():
            origins.setdefault(key, []).append(part)

    for group in rows:
        if set(GROUP_FIELDS) - set(group):
            raise ValueError('adjusted row requires adjustments and total')
        book(group)
    # A key appears once: either with own usage or as an adjustment-only row.
    seen = {_row_key(g) for g in view['groups']}
    for group in only:
        if set(group) - {'tenant'} != {'key', 'cost', 'usage', 'adjustments', 'total'}:
            raise ValueError('invalid adjustment-only group')
        validate_row(group)
        _no_own_usage(group)
        if _row_key(group) in seen:
            raise ValueError('adjustment-only group repeats a key')
        seen.add(_row_key(group))
        book(group)
    # Rows reconcile with the view per origin week and reason, hence also in total.
    if {key: _sum(parts) for key, parts in origins.items()} != view_origins:
        raise ValueError('row adjustments must add up to the view adjustments by origin')


def probe():
    """A minimal adjusted row this reader must accept, and a wrong total it must reject."""
    tokens = dict.fromkeys(TOKEN_CLASSES, 0)
    entry = {'entry_count': 1, 'unpriced_entry_count': 0, 'tokens': {**tokens, 'output': 2},
             'known_total_tokens': 2, 'priced_machine_cash_usd': '0.5'}
    adjustments = {**entry, 'by_origin': [{**entry, 'week_start': '2026-11-09', 'reason': 'late_usage'}]}
    usage = {'event_count': 0, 'known_total_tokens': 0,
             'tokens': {k: {'known': 0, 'unknown_event_count': 0} for k in TOKEN_CLASSES}}
    row = {'usage': usage, 'cost': {'machine_cash_usd': '0'}, 'adjustments': adjustments,
           'total': {'tokens': {**tokens, 'output': 2}, 'known_total_tokens': 2, 'priced_machine_cash_usd': '0.5'}}
    validate_total(row, validate_adjustments(adjustments))
    try:
        validate_total({**row, 'total': {**row['total'], 'priced_machine_cash_usd': '0'}}, validate_adjustments(adjustments))
    except ValueError:
        return True
    return False
