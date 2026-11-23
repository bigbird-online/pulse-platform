"""Read-only Agent query over the same published data the PulseStats App shows.

Sources, never written here:
- App snapshot: the last exporter response the App validated and displayed
  (``~/Library/Application Support/PulseStats/snapshot-v2.json``). Running the
  live exporter is not read-only: it takes the incident-state writer lock the
  App's own refresh needs.
- Cost publication: ``~/.local/state/pulse/stats/cost-dashboard-v1.json``, read
  through the same verifier the exporter uses (``load_cost_dashboard``).
- WorkItem facts ``wi-facts-v1.json`` (each WI's session rollovers) and Relay's
  CC compaction record ``context-compactions-v1.json``, both published by the
  refresh cycle into the same state directory.

Every answer carries ``pulse_stats_query/v1``, the source timestamps and
staleness. Sums over published rows are additions of published amounts, never
re-pricing.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import stat

from . import agy_quota, context_compactions
from .cost_dashboard import (SCHEMA as COST_SCHEMA, _validate as validate_cost, expand_transport_cost_dashboard,
                             load_cost_dashboard, period_window)

SCHEMA = 'pulse_stats_query/v1'
SNAPSHOT_SCHEMA = 'pulse.monitoring.snapshot.v2'
MAX_SNAPSHOT_BYTES = 1024 * 1024
QUERIES = ('overview', 'cost', 'wi', 'quotas', 'health', 'board', 'automation', 'metric', 'snapshot', 'agent_day',
           'compactions')
PERIODS = ('today', 'week', 'month')
# The previous complete Monday..Sunday: a cost period of its own publication.
CLOSED_PERIODS = ('last_week',)
try:
    from zoneinfo import ZoneInfo
    COST_ZONE = ZoneInfo('Asia/Shanghai')
except Exception:  # noqa: BLE001 - tzdata missing: Asia/Shanghai has no DST.
    from datetime import timezone as _tz
    COST_ZONE = _tz(timedelta(hours=8))


def cost_day(value):
    """The Asia/Shanghai calendar day of an instant, as the App's cost views use."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.astimezone(COST_ZONE).date()


def period_bounds(day: date, period):
    """[from_date, to_date) of a period as of `day`, as the App and publisher check them."""
    start, end = period_window(day, period)
    return start.isoformat(), end.isoformat()
GROUPS = ('agent', 'wi', 'runtime', 'model', 'node', 'entrypoint', 'tenant')
WI_PATTERN = re.compile(r'wi_\d{8}_\d{3}')
NAME_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
# Asia/Shanghai calendar periods, as the App's 今日／本周／本月 tiles.
WI_COUNT_METRICS = {
    'today': ('harness.wi.day_created', 'harness.wi.day_closed'),
    'week': ('harness.wi.iso_week_created', 'harness.wi.iso_week_closed'),
    'month': ('harness.wi.month_created', 'harness.wi.month_closed'),
}
QUOTA_METRICS = {
    'codex': ('relay.codex.used_percent', 'relay.codex.resets_at'),
    'cc': ('relay.cc.used_percent', 'relay.cc.resets_at', 'relay.cc.5h_used_percent'),
    'grok': ('relay.grok.used_percent', 'relay.grok.resets_at', 'relay.grok.period_start',
             'relay.grok.plan_label'),
}
RUNTIME_METRICS = ('relay.cc.processes', 'relay.cc.busy', 'relay.cc.idle', 'relay.cc.rate_limited',
                   'relay.bots_online', 'relay.uptime_seconds')


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _age(generated, expires, now):
    g, e = _date(generated), _date(expires)
    return {'generated_at': generated, 'expires_at': expires,
            'age_seconds': int((now - g).total_seconds()) if g else None,
            'stale': None if e is None else now >= e}


def read_snapshot(path: Path):
    """Private regular file, at most 1 MiB, supported schema; same bounds as the App."""
    try:
        if path.is_symlink():
            raise ValueError('symlink_snapshot')
        with path.open('rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError('snapshot_not_private')
            data = handle.read(MAX_SNAPSHOT_BYTES + 1)
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise ValueError('snapshot_oversized')
        value = json.loads(data)
        if not isinstance(value, dict) or value.get('schema_version') != SNAPSHOT_SCHEMA:
            raise ValueError('unsupported_snapshot_schema')
        if _date(value.get('generated_at')) is None or _date(value.get('expires_at')) is None:
            raise ValueError('invalid_snapshot_times')
        if not isinstance(value.get('metrics', []), list):
            raise ValueError('invalid_snapshot_metrics')
        return value, None
    except FileNotFoundError:
        return None, 'snapshot_missing'
    except ValueError as error:
        reason = str(error)
        return None, reason if re.fullmatch(r'[a-z_]+', reason) else 'invalid_snapshot'
    except (OSError, TypeError):
        return None, 'invalid_snapshot'


def embedded_cost(snapshot):
    """Re-verify the cost carrier inside a snapshot with the publication verifier."""
    value = (snapshot or {}).get('cost_dashboard')
    if not isinstance(value, dict):
        return None, 'cost_dashboard_missing'
    try:
        value = expand_transport_cost_dashboard(value)
    except (ValueError, KeyError, TypeError, ImportError):
        return None, 'invalid_cost_snapshot'
    if value.get('schema_version') != COST_SCHEMA:
        return None, 'unsupported_cost_schema'
    if value.get('status') == 'available':
        try:
            validate_cost(value)
        except (ValueError, KeyError, TypeError, AttributeError):
            return None, 'invalid_cost_snapshot'
    return value, None


def _raw_times(path):
    """Unverified timestamps of a rejected publication, for its age only."""
    try:
        with Path(path).open('rb') as handle:
            value = json.loads(handle.read(MAX_SNAPSHOT_BYTES + 1))
        return value.get('generated_at'), value.get('expires_at')
    except (OSError, ValueError, AttributeError):
        return None, None


class Sources:
    """Lazily loaded, read-only inputs plus their freshness description."""

    def __init__(self, *, app_snapshot: Path, cost_publication: Path | None, now: datetime,
                 app_kind: str = 'app_cache', home: Path | None = None, coevolution_root: Path | None = None,
                 wi_facts: Path | None = None, kernel: Path | None = None, pr_bodies: Path | None = None,
                 compactions: Path | None = None):
        self.app_path, self.cost_path, self.now, self.app_kind = app_snapshot, cost_publication, now, app_kind
        # agent_day reads the synced checkouts and Stats' WI facts, not the App or cost publication.
        self.home, self.coevolution_root, self.wi_facts, self.kernel = home, coevolution_root, wi_facts, kernel
        self.pr_bodies, self.compactions_path = pr_bodies, compactions
        self._app = self._cost = self._facts = self._compactions = None
        self.used = {}

    def facts(self):
        """The core's WorkItem facts; older than the publisher's bound is stale, not absent."""
        if self._facts is None:
            from . import wi_facts
            value, reason = wi_facts.load(self.wi_facts, self.now, max_age=None)
            entry = {'kind': 'stats_wi_facts', 'status': 'ok' if value else 'unavailable', 'reason': reason}
            generated = _date((value or {}).get('generated_at'))
            entry.update(_age(generated and generated.isoformat(),
                              generated and (generated + wi_facts.MAX_AGE).isoformat(), self.now))
            self._facts = (value, entry)
        self.used['wi_facts'] = self._facts[1]
        return self._facts[0]

    def compactions(self):
        """Relay's CC compaction record as the refresh cycle last read it; older than the publisher's bound is stale."""
        if self._compactions is None:
            value, reason = context_compactions.load(self.compactions_path, self.now)
            entry = {'kind': 'relay_context_compactions', 'status': 'ok' if value else 'unavailable',
                     'reason': reason}
            reading = context_compactions.observed(value, self.now) if value else {}
            entry.update(_age(reading.get('observed_at'), reading.get('expires_at'), self.now))
            self._compactions = (value, entry)
        self.used['context_compactions'] = self._compactions[1]
        return self._compactions[0]

    def app(self):
        if self._app is None:
            value, reason = read_snapshot(self.app_path)
            entry = {'kind': self.app_kind, 'status': 'ok' if value else 'unavailable', 'reason': reason}
            if value:
                entry.update(_age(value['generated_at'], value['expires_at'], self.now))
                entry['producer'] = (value.get('provenance') or {}).get('producer')
            self._app = (value, entry)
        self.used['app_snapshot'] = self._app[1]
        return self._app[0]

    def cost(self):
        if self._cost is None:
            if self.cost_path is None:
                value, reason = embedded_cost(self.app())
                kind = 'snapshot_embedded'
            else:
                value, reason, kind = load_cost_dashboard(self.cost_path, now=self.now), None, 'publication'
            entry = {'kind': kind, 'status': (value or {}).get('status', 'unavailable'),
                     'reason': reason or (value or {}).get('reason')}
            if value and kind == 'publication' and value.get('status') != 'available':
                # The verifier's unavailable carrier is stamped "now"; report the file's own times.
                entry.update(_age(*_raw_times(self.cost_path), self.now))
            elif value:
                entry.update(_age(value.get('generated_at'), value.get('expires_at'), self.now))
                entry['source_revision'] = value.get('source_revision')
                if value.get('collection_backlog') is not None:
                    # Some pushed observations were not judged yet: these amounts are incomplete.
                    entry['collection_backlog'] = value['collection_backlog']
                marks = {v.get('watermark') for v in value.get('views', [])}
                entry['watermark'] = marks.pop() if len(marks) == 1 else None
                if value.get('status') == 'available' and cost_day(value['generated_at']) != cost_day(self.now):
                    # Like the App: yesterday's today/week/month never answers for this day.
                    entry.update(status='unavailable', reason='cost_day_rolled_over', stale=True)
                    value = None
            self._cost = (value, entry)
        self.used['cost_publication'] = self._cost[1]
        value = self._cost[0]
        return value if value and value.get('status') == 'available' else None


def metric_map(snapshot):
    return {m.get('id'): m for m in (snapshot or {}).get('metrics', []) if isinstance(m, dict)}


def _metric(m):
    if m is None:
        return None
    return {k: m.get(k) for k in ('id', 'value', 'unit', 'status', 'quality', 'reason', 'observed_at',
                                  'window', 'scope') if k in m}


def amount(cost):
    """The App's displayed amount: full when complete, else the same query's priced total."""
    if not isinstance(cost, dict):
        return None, False
    if cost.get('machine_cash_usd') is not None:
        return cost['machine_cash_usd'], True
    if 'priced_event_count' in cost or 'priced_machine_cash_usd' in cost:
        return cost.get('priced_machine_cash_usd'), False
    return cost.get('partial_machine_cash_usd'), False


def _row(group, titles):
    value, complete = amount(group.get('cost'))
    usage = group.get('usage') or {}
    outcomes = group.get('outcomes') or {}
    key = group.get('key') or {}
    row = {'key': key, 'amount_usd': value, 'amount_complete': complete,
           'cost_reasons': (group.get('cost') or {}).get('reasons', []),
           'event_count': usage.get('event_count'), 'known_total_tokens': usage.get('known_total_tokens'),
           'tokens': usage.get('tokens'),
           'outcomes': {'status': outcomes.get('status'), 'known_count': outcomes.get('known_count'),
                        'refs': outcomes.get('refs', [])} if outcomes else None,
           'attribution_sources': (group.get('attribution') or {}).get('sources')}
    if 'wi' in key:
        row['wi_title'] = (titles.get(key['wi']) or {}).get('title')
        parent = (titles.get(key['wi']) or {}).get('parent')
        if parent:
            row['wi_parent'] = parent  # A child WI's own row; its parent's total is in work_item_families.
    _adjusted(row, group)
    detail = group.get('detail') or {}
    if detail:
        row['first_usage_at'], row['last_usage_at'] = detail.get('first_usage_at'), detail.get('last_usage_at')
    return row


def _adjusted(row, owner):
    """Own usage, entries against closed weeks, and their sum; only when published."""
    if 'total' not in owner:
        return row
    adjustments, total = owner['adjustments'], owner['total']
    row['adjustment'] = {'entry_count': adjustments['entry_count'],
                         'unpriced_entry_count': adjustments['unpriced_entry_count'],
                         'amount_usd': adjustments['priced_machine_cash_usd'],
                         'known_total_tokens': adjustments['known_total_tokens'],
                         'tokens': adjustments['tokens'], 'by_origin': adjustments['by_origin']}
    row['total'] = {'amount_usd': total['priced_machine_cash_usd'],
                    'known_total_tokens': total['known_total_tokens'], 'tokens': total['tokens']}
    return row


def _add(total, value):
    """Sum known values only; an unknown value never counts as zero."""
    if value is None:
        return total
    return value if total is None else total + value


def _money(value):
    return None if value is None else Decimal(value)


class _Adjusted:
    """One group's entries against closed weeks and its total, in the shape of a row's.

    Own known quantities and all entries are summed apart and then combined,
    as a view's total is: a row without events has nothing unknown, so a group
    of such rows alone owns zero; a class no row with events knows stays null.
    """

    def __init__(self):
        self.rows, self.used, self.origins = 0, 0, {}
        self.adjustment = {'entry_count': 0, 'unpriced_entry_count': 0, 'amount_usd': Decimal(0),
                           'known_total_tokens': 0, 'tokens': {}}
        self.own_amount, self.own_known, self.own_tokens = Decimal(0), None, {}

    @staticmethod
    def _entries(target, source, amount):
        for name in ('entry_count', 'unpriced_entry_count', 'known_total_tokens'):
            target[name] = target.get(name, 0) + source[name]
        target[amount] = target.get(amount, Decimal(0)) + Decimal(source[amount])
        tokens = target.setdefault('tokens', {})
        for name, value in source['tokens'].items():
            tokens[name] = tokens.get(name, 0) + value

    def add(self, row):
        if 'total' in row:
            self.rows += 1
            self._entries(self.adjustment, row['adjustment'], 'amount_usd')
            for part in row['adjustment']['by_origin']:
                self._entries(self.origins.setdefault((part['week_start'], part['reason']), {}),
                              part, 'priced_machine_cash_usd')
        self.own_amount += _money(row['amount_usd']) or 0  # As in a view's total: unpriced own usage adds nothing.
        if not row.get('event_count'):
            return
        self.used += 1
        self.own_known = _add(self.own_known, row['known_total_tokens'])
        for name, value in (row.get('tokens') or {}).items():
            self.own_tokens[name] = _add(self.own_tokens.get(name), (value or {}).get('known'))

    def publish(self, group):
        """Own usage stays in the group's amount_usd; nothing is added without entries."""
        if not self.rows:
            return
        by_origin = [{'week_start': week, 'reason': reason, **part,
                      'priced_machine_cash_usd': str(part['priced_machine_cash_usd'])}
                     for (week, reason), part in sorted(self.origins.items())]
        entries = self.adjustment
        group['adjustment'] = {**entries, 'amount_usd': str(entries['amount_usd']), 'by_origin': by_origin}

        def plus(own, change):
            if own is None and not self.used:
                own = 0
            return None if own is None else own + change

        group['total'] = {'amount_usd': str(self.own_amount + entries['amount_usd']),
                          'known_total_tokens': plus(self.own_known, entries['known_total_tokens']),
                          'tokens': {name: plus(self.own_tokens.get(name), change)
                                     for name, change in entries['tokens'].items()}}


def aggregate(rows, dimension, outcome_only=(), pull_request_only=(), adjustment_only=()):
    """Group published rows by one key; a total is complete only if every row is.

    Unknown amounts and token counts stay null (a group with no known value has
    no total). Outcome-only and PR-only rows join their group but carry no cost.
    """
    groups = {}

    def group(row):
        name = row['key'].get(dimension)
        return groups.setdefault(name, {dimension: name, 'amount_usd': None, 'amount_complete': True,
                                        'event_count': 0, 'known_total_tokens': None, 'rows': 0,
                                        'outcome_only_rows': 0, 'pull_request_only_rows': 0,
                                        'outcome_refs': set(),
                                        'cost_reasons': set(), '_adjusted': _Adjusted()})

    def adjust(g, row):
        g['_adjusted'].add(row)

    for row in rows:
        g = group(row)
        g['rows'] += 1
        value = row['amount_usd']
        g['amount_usd'] = _add(g['amount_usd'], None if value is None else Decimal(value))
        g['amount_complete'] = g['amount_complete'] and row['amount_complete'] and value is not None
        g['event_count'] += row['event_count'] or 0
        g['known_total_tokens'] = _add(g['known_total_tokens'], row['known_total_tokens'])
        g['outcome_refs'].update((row['outcomes'] or {}).get('refs', []))
        g['cost_reasons'].update(row['cost_reasons'])
        if 'wi_title' in row and dimension == 'wi':
            g['wi_title'] = row['wi_title']
        if 'wi_parent' in row and dimension == 'wi':
            g['wi_parent'] = row['wi_parent']
        adjust(g, row)
    for row in adjustment_only:
        g = group(row)
        adjust(g, row)
        if 'wi_title' in row and dimension == 'wi':
            g['wi_title'] = row['wi_title']
    for row in pull_request_only:
        g = group(row)
        g['pull_request_only_rows'] += 1
        if 'wi_title' in row and dimension == 'wi':
            g['wi_title'] = row['wi_title']
    for row in outcome_only:
        g = group(row)
        g['outcome_only_rows'] += 1
        g['outcome_refs'].update((row['outcomes'] or {}).get('refs', []))
        if 'wi_title' in row and dimension == 'wi':
            g['wi_title'] = row['wi_title']
    result = []
    for g in groups.values():
        if g['rows'] == 0:
            g['amount_complete'] = None  # no cost rows: nothing to be complete about
        g['amount_usd'] = None if g['amount_usd'] is None else str(g['amount_usd'])
        g.pop('_adjusted').publish(g)
        g['outcome_ref_count'] = len(g['outcome_refs'])
        g['outcome_refs'] = sorted(g['outcome_refs'])
        g['cost_reasons'] = sorted(g['cost_reasons'])
        result.append(g)
    return sorted(result, key=lambda g: (g['amount_usd'] is None,
                                         -Decimal(g['amount_usd'] or 0), str(g[dimension])))


def _view(cost, period, dimension='activity'):
    """The published view of this period, only when it covers the period of its own day."""
    if not cost:
        return None
    bounds = period_bounds(cost_day(cost['generated_at']), period)
    for view in cost.get('views', []):
        query = view.get('query') or {}
        if (view.get('period') == period and view.get('dimension') == dimension
                and (query.get('from_date'), query.get('to_date')) == bounds):
            return view
    return None


def _titles(cost):
    return {item['id']: item for item in (cost or {}).get('work_items', []) if isinstance(item, dict)}


def _summary(view):
    if view is None:
        return None
    s = view.get('summary') or {}
    value, complete = amount(s.get('cost'))
    return {'amount_usd': value, 'amount_complete': complete, 'cost': s.get('cost'), 'usage': s.get('usage'),
            'from_date': view['query'].get('from_date'), 'to_date': view['query'].get('to_date'),
            'timezone': view['query'].get('timezone'), 'watermark': view.get('watermark'),
            'active_agents': view.get('active_agents'), **_closing(view)}


def _closing(view):
    """The view's adjustment total and closed week, when the publisher closes weeks."""
    result = _adjusted({}, view)
    if 'closed_week' in view:
        result['closed_week'] = view['closed_week']
    return result


def scoped_summary(view, rows, only, adjustment_only=()):
    """The filtered rows' totals with the same null-not-zero rules as group totals."""
    total = (aggregate(rows, '_scope', only, (), adjustment_only) or [None])[0]
    query = view['query']
    if total is None:
        return {'amount_usd': None, 'amount_complete': None, 'event_count': 0, 'known_total_tokens': None,
                'rows': 0, 'outcome_only_rows': 0, 'from_date': query.get('from_date'),
                'to_date': query.get('to_date'), 'timezone': query.get('timezone')}
    total.pop('_scope', None)
    return {**total, 'from_date': query.get('from_date'), 'to_date': query.get('to_date'),
            'timezone': query.get('timezone')}


def scoped_pull_requests(outputs, wis):
    """Kernel-merged PRs (the Stats “产出”) of the given WIs; unknown never becomes 0."""
    if not isinstance(outputs, dict):
        return None
    items = [w for w in outputs.get('work_items') or [] if w.get('wi') in wis]
    unknown = (outputs.get('status') == 'unavailable'
               or bool(wis & set(outputs.get('unreadable_work_items') or [])))
    return {'status': outputs.get('status'), 'scope': 'filtered', 'work_items': items,
            'count': None if unknown else sum(w.get('count', 0) for w in items)}


def scoped_outcomes(outcomes, rows):
    """Outcome items referenced by the filtered rows only."""
    if not isinstance(outcomes, dict):
        return outcomes
    refs = {ref for row in rows for ref in ((row.get('outcomes') or {}).get('refs') or [])}
    items = [o for o in outcomes.get('items') or [] if o.get('outcome_ref') in refs]
    return {'status': outcomes.get('status'), 'metric_scope': outcomes.get('metric_scope'),
            'scope': 'filtered', 'items': items, 'refs': sorted(refs)}


def scoped_families(view, wis=None):
    """Parent WIs' totals including their children; the given WIs' families only when filtered."""
    families = [f for f in (view or {}).get('work_item_families') or [] if isinstance(f, dict)]
    if wis is not None:
        families = [f for f in families if f.get('wi') in wis or wis & set(f.get('children') or [])]
    return families


def q_cost(src, a):
    cost = src.cost()
    if cost is None:
        return None
    view = _view(cost, a.period)
    titles = _titles(cost)
    if view is None:
        # Older publications carry one view per dimension.
        dimension = a.group or 'agent'
        view = _view(cost, a.period, dimension)
        if view is None:
            return {'period': a.period, 'status': 'view_unavailable'}
    rows = [_row(g, titles) for g in view.get('groups', [])]
    only = [_row(g, titles) for g in view.get('outcome_only_groups', [])]
    # WIs with merged PRs but no usage or Outcome this period: no cost, never dropped.
    pr_only = [_row(g, titles) for g in view.get('pull_request_only_groups', [])]
    # Entries against closed weeks on keys without own usage in this window.
    adj_only = [_row(g, titles) for g in view.get('adjustment_only_groups') or []]
    for name, value in (('agent', a.agent), ('wi', a.work_item_id), ('runtime', a.runtime)):
        if value is not None:
            rows = [r for r in rows if r['key'].get(name) == value]
            only = [r for r in only if r['key'].get(name) == value]
            pr_only = [r for r in pr_only if r['key'].get(name) == value]
            adj_only = [r for r in adj_only if r['key'].get(name) == value]
    filters = {'agent': a.agent, 'wi': a.work_item_id, 'runtime': a.runtime}
    result = {'period': a.period, 'dimension': view.get('dimension'), 'filters': filters}
    outputs = view.get('pull_request_outputs')
    if any(v is not None for v in filters.values()):
        # PRs belong to WIs, not to agents or runtimes: the filtered answer counts the
        # WIs its own rows name (or the named WI alone); platform totals are named separately.
        if a.work_item_id is not None and a.agent is None and a.runtime is None:
            wis = {a.work_item_id}
        else:
            wis = {r['key'].get('wi') for r in rows + only + pr_only}
        result['pull_requests'] = scoped_pull_requests(outputs, wis)
        # Every answer field is the filtered scope; the platform totals are named as such.
        result['scope'] = 'filtered'
        result['summary'] = scoped_summary(view, rows, only, adj_only)
        result['outcomes'] = scoped_outcomes(view.get('outcomes'), rows + only)
        result['platform'] = {'scope': 'global', 'summary': _summary(view),
                              'outcomes': view.get('outcomes'), 'stages': view.get('stages'),
                              'pull_requests': outputs}
        result['work_item_families'] = scoped_families(view, wis)
    else:
        result.update(scope='global', summary=_summary(view), outcomes=view.get('outcomes'),
                      stages=view.get('stages'), pull_requests=outputs,
                      work_item_families=scoped_families(view))
    result['family_note'] = ('A parent WI\'s family total adds its children; its own rows keep their own '
                             'values and the summary never adds a family twice.')
    if a.group:
        result['group'] = a.group
        result['groups'] = aggregate(rows, a.group, only, pr_only, adj_only)
        result['note'] = ('Group totals add published row amounts; complete only if every row is complete. '
                          'Unknown amounts and tokens are null, never zero.')
    else:
        result['rows'] = rows
        result['outcome_only_rows'] = only
        result['pull_request_only_rows'] = pr_only
    if 'total' in view:
        result['adjustment_only_rows'] = adj_only
        result['adjustment_note'] = ('amount_usd is own usage in this window; adjustment is the entries booked in '
                                     'it against closed weeks; total (own plus adjustment) is the figure to show.')
    if 'closed_week' in view:
        # Null while the week is not yet closed; publications that do not close weeks have no such key.
        result['closed_week'] = view['closed_week']
    return result


def q_wi(src, a):
    if not a.work_item_id:
        raise ValueError('work_item_id_required')
    cost, snap = src.cost(), src.app()
    titles = _titles(cost)
    item = titles.get(a.work_item_id)
    periods = {}
    for period in PERIODS:
        view = _view(cost, period)
        if view is None:
            periods[period] = None
            continue
        mine = [g for g in view.get('groups', []) if (g.get('key') or {}).get('wi') == a.work_item_id]
        only = [g for g in view.get('outcome_only_groups', [])
                if (g.get('key') or {}).get('wi') == a.work_item_id]
        rows = [_row(g, titles) for g in mine]
        only_rows = [_row(g, titles) for g in only]
        pr_rows = [_row(g, titles) for g in view.get('pull_request_only_groups', [])
                   if (g.get('key') or {}).get('wi') == a.work_item_id]
        # Entries against closed weeks on this WI's keys without own usage in the period.
        adj_rows = [_row(g, titles) for g in view.get('adjustment_only_groups') or []
                    if (g.get('key') or {}).get('wi') == a.work_item_id]
        total = aggregate(rows, 'wi', only_rows, pr_rows, adj_rows)
        items = [o for o in (view.get('outcomes') or {}).get('items', []) if o.get('wi') == a.work_item_id]
        periods[period] = {'total': total[0] if total else None,
                           'family_total': next((f for f in scoped_families(view) if f.get('wi') == a.work_item_id), None),
                           'rows': rows,
                           'outcome_only_rows': only_rows, 'pull_request_only_rows': pr_rows,
                           **({'adjustment_only_rows': adj_rows} if 'total' in view else {}),
                           'pull_requests': scoped_pull_requests(view.get('pull_request_outputs'),
                                                                 {a.work_item_id}),
                           'outcomes': items,
                           'from_date': view['query'].get('from_date')}
    metrics = metric_map(snap)
    board = [r for r in ((metrics.get('work.board') or {}).get('value') or [])
             if isinstance(r, dict) and a.work_item_id in (r.get('work_item_id'), r.get('id'))]
    alerts = [x for x in (snap or {}).get('alerts', []) if x.get('work_item_id') == a.work_item_id]
    compactions, facts = src.compactions(), src.facts()
    return {'work_item_id': a.work_item_id, 'work_item': item,
            'project_page': (item or {}).get('project_page'),
            'parent': (item or {}).get('parent'),
            'children': sorted(i for i, t in titles.items() if t.get('parent') == a.work_item_id),
            'cost_by_period': periods, 'board': board, 'alerts': alerts,
            'rollovers': wi_rollovers(facts, a.work_item_id),
            'context_compactions': (context_compactions.summary(
                compactions, src.now, wi=a.work_item_id, wi_created_at=_wi_created(facts, a.work_item_id))
                if compactions else None)}


def _wi_created(facts, wi):
    """When the core created the WI, from its export; None when unknown."""
    return next((i.get('created_at') for i in (facts or {}).get('items', []) if i.get('id') == wi), None)


def wi_rollovers(facts, wi):
    """Completed session rollovers of one WI from the core export; unknown is never zero."""
    def unknown(reason):
        return {'count': None, 'by_agent': None, 'items': [], 'reason': reason}
    if facts is None:
        return unknown('wi_facts_unavailable')
    item = next((i for i in facts.get('items', []) if i.get('id') == wi), None)
    if item is None:
        return unknown('work_item_not_in_core_export')
    rows = item.get('rollovers')
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return unknown('core_export_without_rollovers')
    return {'count': len(rows), 'by_agent': sum(r.get('requested_by') == 'agent' for r in rows),
            'items': rows, 'reason': None}


def q_compactions(src, a):
    """CC context compactions per native session, of one WI or one session ID, with the record's coverage."""
    document = src.compactions()
    if document is None:
        raise ValueError('context_compactions_unavailable')
    ref = context_compactions.session_ref(a.session_id) if getattr(a, 'session_id', None) else None
    created = _wi_created(src.facts(), a.work_item_id) if a.work_item_id and ref is None else None
    result = context_compactions.summary(document, src.now, wi=a.work_item_id, ref=ref, wi_created_at=created)
    # No matching row is not zero, and a limited coverage leaves a WI total partial.
    return {**result, 'completeness': {'sections': {'context_compactions': {'status': result['status'],
                                                                            'reason': result['reason']}}}}


def _agy_quota_now(metric, now):
    """The agy reading as of the query time, not of the snapshot.

    A snapshot outlives a 5-hour bucket and the reading's own hour: buckets
    that have reset are dropped, and a reading past ``MAX_AGE_SECONDS`` or
    with no current bucket is unknown, as the snapshot itself would show it.
    """
    if not metric or metric.get('status') != 'available' or not isinstance(metric.get('value'), list):
        return metric
    try:
        observed = agy_quota.observed_at(metric)
    except agy_quota.QuotaError:
        observed = None
    if observed is None or (now - observed).total_seconds() >= agy_quota.MAX_AGE_SECONDS:
        return {**metric, 'value': None, 'status': 'unknown', 'reason': 'quota_reading_stale', 'observed_at': None}
    rows = [row for row in metric['value'] if isinstance(row, dict) and type(row.get('resets_at')) is int
            and row['resets_at'] > now.timestamp()]
    if not rows:
        return {**metric, 'value': None, 'status': 'unknown', 'reason': 'quota_window_elapsed'}
    return {**metric, 'value': rows}


def q_quotas(src, a):
    snap, cost = src.app(), src.cost()
    metrics = metric_map(snap)
    result = {name: {mid.rsplit('.', 1)[-1]: _metric(metrics.get(mid)) for mid in ids}
              for name, ids in QUOTA_METRICS.items()}
    # agy: one row per current bucket (group, window, remaining_fraction, resets_at).
    result['agy'] = {'quota': _agy_quota_now(_metric(metrics.get('relay.agy.quota')), src.now)}
    if snap is None:
        result = {name: None for name in (*QUOTA_METRICS, 'agy')}
    result['subscriptions'] = (cost or {}).get('subscriptions')
    result['openrouter'] = (cost or {}).get('openrouter')
    return result


def q_health(src, a):
    snap = src.app()
    src.cost()
    if snap is None:
        return None
    metrics = metric_map(snap)
    return {'overall': snap.get('overall'), 'alert_tiers': snap.get('alert_tiers'),
            'global_authority': snap.get('global_authority'),
            'capabilities': [{k: c.get(k) for k in ('id', 'health', 'implementation_status', 'freshness', 'reason')}
                             for c in snap.get('capabilities', [])],
            'incidents': snap.get('incidents'), 'oikos_nodes': _metric(metrics.get('oikos.platform.nodes')),
            'relay_runtime': {mid: _metric(metrics.get(mid)) for mid in RUNTIME_METRICS},
            'provenance_sources': (snap.get('provenance') or {}).get('sources')}


def q_board(src, a):
    snap = src.app()
    if snap is None:
        return None
    metrics = metric_map(snap)
    board = metrics.get('work.board') or {}
    rows = [r for r in (board.get('value') or []) if isinstance(r, dict)]
    alerts = snap.get('alerts', [])
    if a.agent:
        rows = [r for r in rows if r.get('kind') == 'board' or r.get('agent') == a.agent]
    return {'alert_tiers': snap.get('alert_tiers'),
            'act': [x for x in alerts if x.get('tier') == 'act'],
            'watch': [x for x in alerts if x.get('tier') == 'watch'],
            'board': {**_metric(board), 'value': rows} if board else None,
            'in_flight': _metric(metrics.get('harness.wi.in_flight')),
            'stalled': _metric(metrics.get('harness.wi.stalled'))}


def q_automation(src, a):
    snap = src.app()
    return None if snap is None else _metric(metric_map(snap).get('automation.tasks'))


def q_metric(src, a):
    snap = src.app()
    if snap is None:
        return None
    metrics = metric_map(snap)
    if a.metric_id is None:
        return {'metrics': [{'id': m['id'], 'status': m.get('status'), 'unit': m.get('unit'),
                             'observed_at': m.get('observed_at')} for m in metrics.values() if m.get('id')]}
    if a.metric_id not in metrics:
        raise ValueError('unknown_metric_id')
    return _metric(metrics[a.metric_id])


def q_snapshot(src, a):
    return src.app()


def q_agent_day(src, a):
    """One Agent's commits, PRs and WI events per UTC+8 day; see agent_day.py."""
    from .agent_day import DayQueryError, Inputs, collect

    if not a.agent or not getattr(a, 'date', None):
        raise ValueError('agent_and_date_required')
    if src.home is None or src.coevolution_root is None or src.kernel is None:
        raise ValueError('agent_day_sources_not_configured')
    try:
        result, used = collect(Inputs(agent=a.agent, first=a.date, last=getattr(a, 'end_date', None),
                                      ws=getattr(a, 'ws', None), root=src.coevolution_root,
                                      facts_path=src.wi_facts, home=src.home, now=src.now, kernel=src.kernel,
                                      pr_bodies=src.pr_bodies))
    except DayQueryError as error:
        raise ValueError(str(error)) from None
    src.used.update(used)
    return result


def _merged_pr_count(view):
    """Dashboard “PR 数”: exact when complete, a lower bound when partial, else unknown."""
    outputs = (view or {}).get('pull_request_outputs')
    if not isinstance(outputs, dict):
        return {'status': 'unavailable', 'count': None}
    return {'status': outputs.get('status'), 'count': outputs.get('count'),
            'known_count': outputs.get('known_count')}


def q_overview(src, a):
    if a.period not in WI_COUNT_METRICS:
        raise ValueError('overview covers today, week or month; ask cost for last_week')
    snap, cost = src.app(), src.cost()
    metrics = metric_map(snap)
    created, closed = WI_COUNT_METRICS[a.period]
    summary = _summary(_view(cost, a.period))
    return {'period': a.period,
            'platform_runtime': (snap or {}).get('overall', {}).get('runtime_health') if snap else None,
            'wi_created': _metric(metrics.get(created)), 'wi_closed': _metric(metrics.get(closed)),
            'merged_pull_requests': _merged_pr_count(_view(cost, a.period)),
            'total_cost_usd': summary['amount_usd'] if summary else None,
            'total_cost_complete': summary['amount_complete'] if summary else None,
            'active_agents': summary['active_agents'] if summary else None,
            'alert_tiers': (snap or {}).get('alert_tiers'),
            'quota_used_percent': {name: (metrics.get(ids[0]) or {}).get('value') for name, ids in QUOTA_METRICS.items()}}


HANDLERS = {'overview': q_overview, 'cost': q_cost, 'wi': q_wi, 'quotas': q_quotas, 'health': q_health,
            'board': q_board, 'automation': q_automation, 'metric': q_metric, 'snapshot': q_snapshot,
            'agent_day': q_agent_day, 'compactions': q_compactions}


def answer(args, sources: Sources):
    keys = ('agent', 'date', 'end_date', 'ws') if args.query == 'agent_day' else (
        'work_item_id', 'session_id') if args.query == 'compactions' else (
        'period', 'group', 'agent', 'work_item_id', 'runtime', 'metric_id')
    arguments = {k: getattr(args, k, None) for k in keys if getattr(args, k, None) is not None}
    errors = []
    try:
        result = HANDLERS[args.query](sources, args)
    except ValueError as error:
        result, errors = None, [str(error)]
    for name, entry in sources.used.items():
        if entry.get('status') not in ('ok', 'available'):
            errors.append(name + ':' + str(entry.get('reason') or entry.get('status')))
        elif entry.get('stale'):
            errors.append(name + ':stale')
    if isinstance(result, dict) and isinstance(result.get('completeness'), dict):
        for section, entry in (result['completeness'].get('sections') or {}).items():
            if entry.get('status') != 'complete':
                errors.append(f"{section}:{entry.get('status')}")
    status = 'unavailable' if result is None else ('partial' if errors else 'ok')
    return {'schema': SCHEMA, 'query': args.query, 'arguments': arguments, 'status': status,
            'queried_at': sources.now.isoformat(), 'sources': sources.used, 'result': result, 'errors': errors,
            'code_revision': _revision()}


def _revision():
    """The installed release marker when running from ~/.local/share/pulse/stats/releases/<SHA>."""
    marker = Path(__file__).resolve().parents[3] / '.stats-source-revision'
    try:
        value = marker.read_text().strip()
    except OSError:
        return None
    return value if re.fullmatch(r'[0-9a-f]{40}', value) else None
