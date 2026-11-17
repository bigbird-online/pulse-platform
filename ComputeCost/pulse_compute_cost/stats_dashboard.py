"""WI300: bounded publication of existing #513 queries, never a second ledger.

Collection is explicit and separate from App refresh. All requested views use one
immutable receipt snapshot. Subscription facts and provider bills are parallel
procurement context and never enter the query's WI machine-cash policy.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from time import perf_counter, process_time
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .collaboration_query import CollaborationQuery, CollaborationQuerySession, classify_cost_reasons
from .ledger import UsageLedger
from .receipts import canonical, digest
from .openrouter import load_dotenv_value, fetch_openrouter_activity, summarize_openrouter_activity
from .stats_dashboard_wire import COMPRESSED_FIELD, MAX_BYTES, dashboard_bytes, expand_dashboard, wire_encoding

SCHEMA = 'pulse_stats_cost_dashboard/v1'
QUERY_SCHEMA = 'computecost_collaboration_query/v1'
TENANT_REF_LIMIT = 1
# A disputed tenant keeps the witnesses of each side of the dispute.
TENANT_CONFLICT_REF_LIMIT = 6
# Technical reference samples per group; the counts carry the full number.
REF_SAMPLE = 1
DISPLAY_ZONE = ZoneInfo('Asia/Shanghai')
DEFAULT_FACTS = Path(__file__).resolve().parents[1] / 'config' / 'procurement-facts.json'
ACTIVITY_DIMENSIONS = ('agent', 'wi', 'entrypoint', 'node', 'runtime', 'model')
IDENTITY_DIMENSIONS = ('agent', 'node', 'runtime', 'model', 'entrypoint', 'wi')
# The App's 今日／本周／本月 run to the end of today. A weekly review reads the
# previous complete Monday..Sunday from its own publication (LAST_WEEK).
CURRENT_PERIODS = ('today', 'week', 'month')
LAST_WEEK = ('last_week',)
# How long a publication stays valid: 45 minutes on ownermac, 75 on the Oracle
# publisher whose cycle runs longer. Readers accept up to the longest.
VALIDITY_MINUTES = (45, 75)


def period_window(today: date, period: str) -> tuple[date, date]:
    """[from, to) Asia/Shanghai days of a period, as of the day `today`."""
    monday = today-timedelta(days=today.weekday())
    if period == 'last_week':
        return monday-timedelta(days=7), monday
    start = {'today':today,'week':monday,'month':today.replace(day=1)}[period]
    return start, today+timedelta(days=1)


def _instant(value: str) -> datetime:
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('aware timestamp required')
    return value


def _amount(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value) or len(value)>38:
        raise ValueError('exact nonnegative decimal string required')
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError('finite amount required')
    return value


def _subscriptions(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    if document.get('schema_version') != 'pulse_stats_procurement_facts/v1':
        raise ValueError('unsupported procurement facts')
    rows = document.get('subscriptions')
    if not isinstance(rows, list) or len(rows)>10:
        raise ValueError('invalid subscription facts')
    result, identities = [], set()
    fields = {'id','name','status','monthly_amount_usd','billing_day','confirmed_on'}
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError('invalid subscription shape')
        if not re.fullmatch(r'[a-z0-9_]{1,64}', row['id']) or row['id'] in identities:
            raise ValueError('invalid subscription identity')
        if not isinstance(row['name'],str) or not 1<=len(row['name'])<=80:
            raise ValueError('invalid subscription name')
        if row['status'] not in {'active_user_confirmed','stopped_user_confirmed'}:
            raise ValueError('invalid subscription status')
        if row['monthly_amount_usd'] is not None:
            _amount(row['monthly_amount_usd'])
        if row['billing_day'] is not None and (type(row['billing_day']) is not int or not 1<=row['billing_day']<=31):
            raise ValueError('invalid billing day')
        date.fromisoformat(row['confirmed_on'])
        identities.add(row['id']); result.append(deepcopy(row))
    return result


def project_openrouter(snapshot: Mapping[str, Any] | None, *, now: datetime) -> dict[str, Any]:
    """Only returned UTC daily rows. Missing dates and empty months are not zero."""
    result = {'status':'unavailable','reason':'provider_not_connected','observed_at':None,
              'month_utc':now.astimezone(timezone.utc).strftime('%Y-%m'),
              'coverage':'returned_rows_only','usage_usd':None,'requests':None,
              'first_returned_day':None,'last_returned_day':None}
    if snapshot is None:
        return result
    try:
        observed = _instant(snapshot['generated_at'])
        result['observed_at'] = observed.isoformat()
        age = (now-observed).total_seconds()
        if age < 0:
            raise ValueError('future provider snapshot')
        if age >= 86400:
            return {**result,'status':'stale','reason':'provider_snapshot_stale'}
        activity = snapshot['activity']
        if activity.get('source') != 'openrouter.activity' or activity.get('coverage') != 'last_30_completed_utc_days':
            raise ValueError('unsupported provider scope')
        rows = activity['by_day']
        if not isinstance(rows,list):
            raise ValueError('invalid daily rows')
        seen, selected = set(), []
        for row in rows:
            day = date.fromisoformat(row['date'])
            if day in seen or day >= now.astimezone(timezone.utc).date():
                raise ValueError('duplicate or unfinished UTC day')
            seen.add(day)
            _amount(row['usage_usd'])
            if type(row['requests']) is not int or row['requests'] < 0:
                raise ValueError('invalid request count')
            if day.strftime('%Y-%m') == result['month_utc']:
                selected.append(row)
        if not selected:
            return {**result,'reason':'no_returned_rows_in_month'}
        days = sorted(row['date'] for row in selected)
        return {**result,'status':'available','reason':'not_a_complete_month_bill',
                'usage_usd':format(sum((Decimal(r['usage_usd']) for r in selected),Decimal(0)),'f'),
                'requests':sum(r['requests'] for r in selected),
                'first_returned_day':days[0],'last_returned_day':days[-1]}
    except (KeyError,TypeError,ValueError,InvalidOperation):
        return {**result,'reason':'invalid_provider_snapshot'}


def project_cost(cost: Mapping[str, Any]) -> dict[str, Any]:
    """Bound diagnostic samples, never amounts, quantities or coverage.

    The complete query remains addressable by its watermark/report digest.
    Count and reason histogram describe ALL unpriced facts, not the sample.
    `anomalies`/`proof_gaps` partition `reasons` for presentation only.
    """
    result = deepcopy(cost)
    result.update(classify_cost_reasons(result['reasons']))
    rows = result['unpriced']
    if len(rows) <= 8:
        return result
    counts: dict[str, int] = {}
    samples = []
    for row in rows:
        reason = row['reason']
        counts[reason] = counts.get(reason, 0) + 1
        if counts[reason] <= 2:
            samples.append(row)
    result.update(unpriced=samples, unpriced_count=len(rows),
                  unpriced_reason_counts=counts, unpriced_sha256=digest(rows))
    return result


REASON_CLASSES = ('anomalies', 'proof_gaps')


def without_reason_classes(cost: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in cost.items() if k not in REASON_CLASSES}


def matches_query_cost(published: Mapping[str, Any], raw: Mapping[str, Any]) -> bool:
    """Published cost is the raw query cost plus classes derived from its reasons."""
    return (without_reason_classes(published) == raw
            and {k: published.get(k) for k in REASON_CLASSES} == classify_cost_reasons(raw['reasons']))


def project_stages(stage: Mapping[str, Any]) -> dict[str, Any]:
    """Carry output quantities unchanged; never sum unlike units or versions.

    A large optional detail section must not suppress the machine-cost view.
    No truncated list is presented as a complete denominator. The original
    query remains addressable by the enclosing watermark and report digest.
    """
    result = {k: deepcopy(stage[k]) for k in (
        'status', 'coverage', 'execution_authority', 'unsupported_filters', 'outputs')}
    result['reason'] = stage.get('reason')
    result['diagnostics'] = {k: deepcopy(stage[k]) for k in (
        'membership_conflicts', 'revision_conflicts', 'shared_refs', 'unknown_refs',
        'unresolved_link_targets', 'time_unallocated_refs')}
    if result['reason'] == 'stage_projection_failed':
        result['diagnostics'] = None
    if len(canonical(result)) > 64 * 1024:
        result.update(status='unavailable', reason='stage_payload_too_large',
                      outputs=None, diagnostics=None)
    return result


def _view(report: dict[str, Any], period: str, dimension: str) -> dict[str, Any]:
    if report['schema'] != QUERY_SCHEMA or report['report_sha256'] != digest({k:v for k,v in report.items() if k!='report_sha256'}):
        raise ValueError('query schema/digest mismatch')
    # Only query parameters contain tuples. Do not roundtrip the potentially
    # huge retained audit graph only to discard it from the bounded view.
    report = {**report, 'query': json.loads(canonical(report['query']))}
    usage = report['summary']['usage']
    view = {'period':period,'dimension':dimension,'source_schema':report['schema'],
            **{k:deepcopy(report[k]) for k in ('query','watermark','recorded_at','catalog_sha256',
                                               'catalog_observed_at','report_sha256')},
            'summary': {'cost':project_cost(report['summary']['cost']),
                        'usage':{k:deepcopy(usage[k]) for k in ('known_total_tokens','event_count',
                           'source_coverage','wi_inventory_coverage','tokens','last_usage_at','unassigned_event_count',
                           'agent_unassigned_event_count','agent_unassigned_priced_usd',
                           'wi_unassigned_event_count','wi_unassigned_priced_usd',
                           'non_wi_event_count','cron_event_count','no_wi_event_count',
                           'unassigned_known_total_tokens','source_details','conflicted_event_count','time_unallocated_event_count',
                           'conflict_reasons')}},
            'groups':[{'key':deepcopy(g['key']),'cost':project_cost(g['cost']),
                       'usage':{k:deepcopy(g['usage'][k]) for k in ('tokens','event_count','known_total_tokens',
                                                                    'conflicted_event_count','conflict_reasons')},
                       # Declared vs supplemented WI and the scheduler job name.
                       **({'attribution':deepcopy(g['attribution'])} if 'attribution' in g else {})}
                      for g in report['groups']
                      if dimension != 'activity' or g['usage']['event_count'] > 0]}
    if dimension == 'activity':
        view['stages'] = project_stages(report['stages'])
        from .outcome_projection import outcome_references
        if 'tenant' in report['query']['group_by']:
            view['population_scope'] = 'all_registered_tenants' if not any(report['query'].get(k) for k in ('tenant','agent','node','runtime','model','entrypoint','work_items')) else 'filtered_activity'
        outcomes = report['summary']['outcomes']
        view['outcomes'] = deepcopy(outcomes)
        originals = {canonical(g['key']): g for g in report['groups']}
        for group in view['groups']:
            original = originals[canonical(group['key'])]
            if original.get('tenant'):
                # Publish a bounded witness like fact_refs: the App lists five
                # and states that more remain; the full set stays in the
                # same-watermark query. One busy tenant held 2,241 refs.
                tenant = original['tenant']
                disputed = bool(tenant.get('registry_conflicts')) or original['key'].get('tenant') == 'ambiguous'
                limit = TENANT_CONFLICT_REF_LIMIT if disputed else TENANT_REF_LIMIT
                group['tenant'] = {**deepcopy(tenant), 'binding_refs': list(tenant['binding_refs'][:limit])}
            group['outcomes'] = outcome_references(original['outcomes'])
            u = original['usage']
            refs = u['fact_refs']; actor_refs = u.get('attribution_refs', [])
            group['detail'] = {'first_usage_at': u.get('first_usage_at'), 'last_usage_at': u.get('last_usage_at'),
                'fact_refs': refs[:REF_SAMPLE], 'fact_ref_count': len(refs),
                'attribution_refs': actor_refs[:REF_SAMPLE], 'attribution_ref_count': len(actor_refs)}
            if u.get('subcalls'):
                group['detail']['subcalls'] = {**deepcopy(u['subcalls']), 'items': deepcopy(u['subcalls']['items'][:5])}
        # An Outcome can arrive without machine usage in this window. Keep it
        # separate from the unchanged positive-usage partition, but publish a
        # real row for the shared GUI. No placeholder execution-only rows.
        present = {g['key']['wi'] for g in view['groups']}
        by_wi = {}
        for item in outcomes['items']:
            if item['counted'] and item['wi'] not in present:
                by_wi.setdefault(item['wi'], []).append(item)
        view['outcome_only_groups'] = []
        for wi, items in sorted(by_wi.items()):
            owners = {i['responsible'] for i in items if i['responsible']}
            key = dict.fromkeys(report['query']['group_by'], 'not_applicable')
            key.update(wi=wi, agent=next(iter(owners)) if len(owners) == 1 else 'unassigned')
            view['outcome_only_groups'].append({'key': key,
                'cost': project_cost({'machine_cash_usd': None, 'partial_machine_cash_usd': None,
                    'cash_basis': 'openrouter_standard_token_cash', 'reasons': ['no_usage_observations'], 'unpriced': []}),
                'usage': {'tokens': {k: {'known': None, 'unknown_event_count': 0} for k in ('uncached_input','cache_read','cache_write','output')},
                          'event_count': 0, 'known_total_tokens': None},
                'outcomes': {'status': outcomes['status'], 'count': None if outcomes['status'] == 'conflict' else len(items),
                    'known_count': len(items), 'refs': [i['outcome_ref'] for i in items],
                    'metric_scope': 'wi_reference_not_runtime_allocation'}})
        agents = {g['key']['agent'] for g in view['groups'] + view['outcome_only_groups']
                  if g['key']['agent'] not in {'unassigned','ambiguous','not_applicable'}}
        agents.update(i['responsible'] for i in outcomes['items'] if i['counted'] and i['responsible']
                      and i['responsible'] not in {'unassigned','ambiguous','not_applicable'})
        view['active_agents'] = sorted(agents)
    return view



def with_pull_requests(view: Mapping[str, Any], merged: Mapping[str, Any]) -> dict[str, Any]:
    """The joint view plus each WI's kernel-merged PRs in its period.

    Applied after the cached query view: PR records change without a new
    receipt watermark. WIs whose PRs merged without usage in the period get
    rows of their own (unless an Outcome-only row already names them).
    """
    from .pull_request_outputs import only_groups, project
    query = view['query']
    projection = project(merged, view['groups'], date.fromisoformat(query['from_date']),
                         date.fromisoformat(query['to_date']))
    shown = view['groups'] + view.get('outcome_only_groups', [])
    extra = [{**g, 'cost': project_cost(g['cost'])}
             for g in only_groups(projection, shown, query['group_by'], merged['owners'])]
    result = {**view, 'pull_request_outputs': projection, 'pull_request_only_groups': extra}
    if 'active_agents' in view:
        result['active_agents'] = sorted(set(view['active_agents']) | {
            g['key']['agent'] for g in extra if g['key']['agent'] not in {'unassigned', 'ambiguous', 'not_applicable'}})
    return result


def build_dashboard(ledger: UsageLedger | None, *, subscriptions: Mapping[str, Any],
                    source_revision: str, now: datetime | None = None,
                    catalog_snapshot: Mapping[str, Any] | None = None,
                    openrouter_snapshot: Mapping[str, Any] | None = None,
                    identity_dimensions: bool = False,
                    activity_table: bool = False,
                    activity_only: bool = False,
                    tenant_identity: bool = False,
                    agent_tenants: Mapping[str, Any] | None = None,
                    calculation_cache: Path | None = None,
                    force_recompute: bool = False,
                    timings: dict[str, Any] | None = None,
                    work_items: Mapping[str, Mapping[str, Any]] | None = None,
                    pull_requests: Mapping[str, Any] | None = None,
                    pull_request_audit: Mapping[str, Any] | None = None,
                    audit_pull_requests: bool = False,
                    kernel_merge_enforced_since: str | None = None,
                    periods: tuple[str, ...] = CURRENT_PERIODS,
                    validity_minutes: int = VALIDITY_MINUTES[0]) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if validity_minutes not in VALIDITY_MINUTES:
        raise ValueError('unsupported publication validity')
    _instant(now.isoformat())
    if periods not in (CURRENT_PERIODS, LAST_WEEK):
        raise ValueError('unsupported publication periods')
    if periods == LAST_WEEK and not (activity_table and activity_only):
        raise ValueError('the closed week publishes the joint activity view only')
    if not re.fullmatch(r'[0-9a-f]{40}', source_revision):
        raise ValueError('exact source revision required')
    today = now.astimezone(DISPLAY_ZONE).date()
    tomorrow = today+timedelta(days=1)
    expiry = min(now+timedelta(minutes=validity_minutes), datetime.combine(tomorrow,time.min,DISPLAY_ZONE))
    result = {'schema_version':SCHEMA,'generated_at':now.isoformat(),
              'expires_at':expiry.astimezone(timezone.utc).isoformat(),
              'source_revision':source_revision,'status':'unavailable','reason':'source_not_configured',
              'views':[], 'subscriptions':_subscriptions(subscriptions),
              'openrouter':project_openrouter(openrouter_snapshot,now=now)}
    if ledger is not None:
        try:
            started = perf_counter()
            snapshot = ledger.receipt_snapshot()
            if timings is not None:
                timings["ledger_snapshot_seconds"] = round(perf_counter()-started, 3)
            if snapshot['recorded_at'] is not None and _instant(snapshot['recorded_at'])>now:
                raise ValueError('future ingestion timestamp')
            catalog = catalog_snapshot.get('catalog') if catalog_snapshot else None
            observed = catalog_snapshot.get('retrieved_at') if catalog_snapshot else None
            if observed is not None and _instant(observed)>now:
                raise ValueError('future price snapshot')
            windows = {period: period_window(today, period) for period in periods}
            from dataclasses import asdict
            from .calculation_cache import CalculationCache, engine_digest
            cache = CalculationCache(calculation_cache, force=force_recompute) if calculation_cache else None
            engine = engine_digest() if cache else None
            # Already-priced native quantities remain reusable after a new
            # unrelated request or WI attribution arrives. Changed model/counts
            # or catalog bytes invalidate exactly that arithmetic key.
            prices = cache.get("prices", engine) if cache else None
            session = CollaborationQuerySession(snapshot, prices=prices if isinstance(prices, dict) else None,
                                                agent_tenants=agent_tenants)
            from .agent_tenants import agent_tenants_digest
            tenants_key = agent_tenants_digest(agent_tenants)
            def view_for(query, period, dimension):
                started = perf_counter()
                def record_timing(hit):
                    if timings is not None:
                        timings.setdefault("views", []).append({"period": period, "dimension": dimension,
                            "cache_hit": hit, "seconds": round(perf_counter()-started, 3)})
                key = [engine, snapshot['sha256'], snapshot['watermark'], snapshot['recorded_at'],
                       asdict(query), digest(catalog) if catalog else None, observed is not None]
                if tenants_key is not None:
                    key.append(tenants_key)  # A registry change re-derives tenants.
                slot = period + "-" + dimension
                saved = cache.get(slot, key) if cache else None
                if (isinstance(saved, dict) and
                        (observed is None or _instant(saved['catalog_observed_at']) <= _instant(observed))):
                    # Keep original calculation provenance. The managed catalog
                    # separately retains the latest successful retrieval time.
                    record_timing(True)
                    return saved
                report = session.query(query,catalog=catalog,catalog_observed_at=observed)
                view = _view(report,period,dimension)
                if cache:
                    cache.put(slot, key, view)
                record_timing(False)
                return view
            views = []
            if activity_only and not activity_table:
                raise ValueError('activity-only publication requires the joint activity view')
            for period,(start,end) in windows.items():
                if not activity_only:
                    for dimension in (IDENTITY_DIMENSIONS if identity_dimensions else ('agent','wi','model')):
                        query = CollaborationQuery(from_date=start.isoformat(),to_date=end.isoformat(),
                            timezone='Asia/Shanghai',scope='activity',group_by=(dimension,),
                            watermark=snapshot['watermark'])
                        views.append(view_for(query,period,dimension))
                if activity_table:
                    # One genuine joint grouping. Never join independent marginal
                    # totals or seed filter rows from open execution expectations.
                    query = CollaborationQuery(from_date=start.isoformat(),to_date=end.isoformat(),
                        timezone='Asia/Shanghai',scope='activity',group_by=(("tenant",) + ACTIVITY_DIMENSIONS if tenant_identity else ACTIVITY_DIMENSIONS),
                        watermark=snapshot['watermark'])
                    views.append(view_for(query,period,'activity'))
            if cache:
                # Bound derivative storage; evicting entries only costs a recompute.
                cache.put("prices", engine, session.export_prices())
            if pull_requests is not None:
                views = [with_pull_requests(v, pull_requests) if v['dimension'] == 'activity' else v
                         for v in views]
            result.update(status='available',reason=None,views=views)
        except (OSError,ValueError,TypeError,KeyError,RuntimeError):
            # Do not publish exception messages (paths/identities) or stamp old amounts fresh.
            result.update(status='unavailable',reason='query_source_unavailable',views=[])
    if audit_pull_requests:
        from .pull_request_audit import derive, unregistered
        from .pull_request_outputs import read_merged
        merged_records = pull_requests or read_merged(None)
        anomalies = derive(pull_request_audit, merged_records, now, kernel_merge_enforced_since)
        # Beside each period's PR count; branch prefixes hint only at registered Agents.
        anomalies['unregistered_pull_requests'] = unregistered(
            pull_request_audit, merged_records, now, {p: period_window(today, p) for p in periods},
            set(agent_tenants or ()))
        result['pull_request_anomalies'] = anomalies
    if work_items is not None:
        # Only the WIs a row names: their name, one-line purpose and status.
        named = {g['key'].get('wi') for v in result['views'] for rows in
                 ('groups', 'outcome_only_groups', 'pull_request_only_groups') for g in v.get(rows, [])}
        # A named child also names its parent, so its row can say whose part it is.
        parents = {wi: item['parent'] for wi, item in work_items.items() if item.get('parent')}
        named |= {parents[wi] for wi in named & set(parents)}
        result['work_items'] = [{'id': wi, **work_items[wi]} for wi in sorted(named & set(work_items))]
        if parents:
            from .pull_request_outputs import families
            views = []
            for view in result['views']:
                found = families(view, parents) if view['dimension'] == 'activity' else []
                views.append({**view, 'work_item_families': found} if found else view)
            result['views'] = views
    result['sha256'] = digest(result)
    if len(dashboard_bytes(result)) > MAX_BYTES:
        result.update(status='unavailable',reason='query_payload_too_large',views=[])
        result['sha256'] = digest({k:v for k,v in result.items() if k!='sha256'})
    return result


def publish_dashboard(path: Path, document: Mapping[str, Any], *, allow_row_contexts: bool = False,
                      allow_compression: bool = False) -> None:
    """Private atomic replace, serial writers, and no older snapshot rollback."""
    path = Path(path)
    if wire_encoding(document):
        # An accepted peer payload keeps its exact representation and digest.
        data = canonical(document) + b'\n'
        document = expand_dashboard(document)
    else:
        data = dashboard_bytes(document)
    encoding = wire_encoding(json.loads(data))
    if ((encoding == 'row_contexts' and not allow_row_contexts)
            or (encoding == COMPRESSED_FIELD and not allow_compression)):
        raise PublicationWithheld('cost_reader_update_required')
    if not path.is_absolute() or path.is_symlink() or len(data)>MAX_BYTES:
        raise ValueError('invalid publication target or size')
    if document.get('schema_version')!=SCHEMA or document.get('sha256')!=digest({k:v for k,v in document.items() if k!='sha256'}):
        raise ValueError('invalid publication document')
    if (allow_row_contexts or allow_compression) and digest(expand_dashboard(json.loads(data))) != digest(document):
        raise ValueError('compact cost publication failed its roundtrip')
    instant = _instant(document['generated_at'])
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.parent.is_symlink() or stat.S_IMODE(path.parent.stat().st_mode)&0o077:
        raise ValueError('publication directory must be private')
    lock_path = path.parent / ('.'+path.name+'.lock')
    fd = os.open(lock_path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if path.exists():
            previous = json.loads(path.read_text(encoding='utf-8'))
            if (_instant(previous['generated_at'])>instant or
                (_instant(previous['generated_at'])==instant and previous.get('sha256')!=document['sha256'])):
                raise ValueError('refusing older or conflicting same-time publication')
        fd,temporary = tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.pending',dir=path.parent)
        try:
            with os.fdopen(fd,'wb') as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary,path)
            directory = os.open(path.parent,os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def project_page_path(value: Any, wi: str) -> str | None:
    """The kernel page of this WI as an absolute local path, or None."""
    if not isinstance(value, str) or len(value) > 1024 or not value.startswith('/'):
        return None
    path = Path(value)
    return value if path.name == wi + '.html' and '..' not in path.parts else None


def core_facts(registry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The enrolled core's private `stats-facts` export, or None."""
    paths = [s.get('path') for s in (registry or {}).get('sources', []) if s.get('kind') == 'harness_core']
    if len(paths) != 1:
        return None
    path = Path(paths[0])
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        return None
    document = json.loads(path.read_bytes())
    if (not isinstance(document, dict) or document.get('schema') != 'harness.stats_facts/v1'
            or document.get('read_only') is not True):
        return None
    return document


def work_item_display(registry: Mapping[str, Any] | None,
                      document: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]] | None:
    """Name, one-line purpose and progress of each WI, from the enrolled core export."""
    document = document if document is not None else core_facts(registry)
    if document is None:
        return None
    result = {}
    for item in document.get('items', []):
        wi, title, summary = item.get('id'), item.get('title'), item.get('summary')
        if not isinstance(wi, str) or not re.fullmatch(r'wi_\d{8}_\d{3}', wi) or not isinstance(title, str):
            continue
        result[wi] = {'title': (title.strip().splitlines() or [''])[0][:80],
                      'summary': summary if isinstance(summary, str) and summary.strip() else None,
                      'status': item.get('business_progress') or item.get('status'),
                      'project_page': project_page_path(item.get('project_page'), wi)}
        parent = item.get('parent')
        if isinstance(parent, str) and re.fullmatch(r'wi_\d{8}_\d{3}', parent) and parent != wi:
            result[wi]['parent'] = parent  # A child WI; its row names this parent.
    return result


# Machine-safe codes for a failed publication; never exception text (paths, identities).
_LEDGER_MESSAGES = ('production observation', 'installed observation authority', 'original adoption unconfirmed')
_last_failure: str | None = None


class PublicationWithheld(RuntimeError):
    """A computed document without amounts never replaces a published one with them."""
    def __init__(self, reason: str):
        super().__init__('publication withheld')
        self.reason = reason


def failure_reason(error: BaseException | None) -> str:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        name = type(error).__name__
        if isinstance(error, PublicationWithheld):
            return error.reason
        if name == 'SnapshotChanged':
            return 'production_ledger_changed'
        if getattr(error, 'code', None) == 'ledger_unavailable' or (
                name in {'ReceiptError', 'LedgerError'} and str(error).startswith(_LEDGER_MESSAGES)):
            return 'production_ledger_unavailable'
        if name == 'TimeoutExpired':
            return 'timeout'
        error = error.__cause__ or error.__context__
    return 'publication_failed'


def last_failure_reason() -> str | None:
    """The reason of this process's most recent failed main(), for the refresh cycle."""
    return _last_failure


def _withhold_regression(path: Path, document: Mapping[str, Any]) -> None:
    """New data replaces old only when it was computed; old stays until then."""
    if document.get('status') == 'available' or not path.is_file() or path.is_symlink():
        return
    try:
        previous = json.loads(path.read_bytes()[:MAX_BYTES + 1])
    except (OSError, ValueError):
        return
    if isinstance(previous, dict) and previous.get('status') == 'available':
        raise PublicationWithheld(str(document.get('reason') or 'query_source_unavailable'))


def main(argv=None, *, row_contexts_ready=None) -> int:
    global _last_failure
    _last_failure = None
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger-root',type=Path)
    parser.add_argument('--source-registry',type=Path)
    parser.add_argument('--collect',action='store_true',help='explicit opt-in to existing registered collector')
    parser.add_argument('--force-recompute',action='store_true',help='ignore disposable calculation results and rebuild from evidence')
    parser.add_argument('--catalog-snapshot',type=Path)
    parser.add_argument('--procurement-facts',type=Path,default=DEFAULT_FACTS)
    parser.add_argument('--openrouter-env',type=Path,help='authorized ownermac dotenv; never printed or copied')
    parser.add_argument('--openrouter-snapshot',type=Path,help='existing sanitized openrouter-snapshot output')
    parser.add_argument('--pull-request-audit',type=Path,help='private cached GitHub PR audit (pull_request_audit.py)')
    parser.add_argument('--kernel-merge-enforced-since',help='ISO date from which a GitHub merge without a kernel record is an anomaly')
    parser.add_argument('--source-revision',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--validity-minutes',type=int,choices=VALIDITY_MINUTES,default=VALIDITY_MINUTES[0],
                        help='publication validity; the Oracle publisher uses the longer one')
    parser.add_argument('--last-week-output',type=Path,
                        help='also publish the previous complete Monday..Sunday after the current views')
    args = parser.parse_args(argv)
    try:
        from .catalog_source import resolve_catalog_path, observe_catalog_snapshot
        ledger_root, registry = args.ledger_root, None
        if args.collect and not args.source_registry:
            raise ValueError('collection requires the registered sources')
        if args.source_registry:
            if args.source_registry.is_symlink() or not args.source_registry.is_file() or args.source_registry.stat().st_mode & 0o077:
                raise ValueError('source registry must be private')
            registry = json.loads(args.source_registry.read_text())
            if registry.get('schema')!='computecost_source_registry/v1':
                raise ValueError('unsupported registry')
            if ledger_root and str(ledger_root)!=registry['ledger_root']:
                raise ValueError('conflicting ledger roots')
            ledger_root = Path(registry['ledger_root'])
        catalog, price_error = observe_catalog_snapshot(resolve_catalog_path(
            explicit=args.catalog_snapshot, registry=registry, registry_path=args.source_registry))
        from .observation_authority import registered_ledger
        ledger = registered_ledger(registry) if registry else UsageLedger(ledger_root) if ledger_root else None
        timings = {}
        started = perf_counter(); cpu_started = process_time()
        if args.collect:
            from .receipt_sources import collect_registered_sources
            collected_at = perf_counter()
            collection = {}
            collect_registered_sources(registry, ledger, force_recompute=args.force_recompute, timings=collection)
            timings["collection_seconds"] = round(perf_counter()-collected_at, 3)
            timings["collection"] = collection
        facts = json.loads(args.procurement_facts.read_text())
        provider = json.loads(args.openrouter_snapshot.read_text()) if args.openrouter_snapshot else None
        if args.openrouter_env and args.openrouter_snapshot:
            raise ValueError('select one provider source')
        if args.openrouter_env:
            try:
                key = load_dotenv_value(args.openrouter_env)
                fetched_at = datetime.now(timezone.utc).isoformat()
                provider = {'generated_at':fetched_at,'activity':summarize_openrouter_activity(fetch_openrouter_activity(key))}
                del key
            except (OSError,ValueError):
                provider = None
        tenant_identity = bool(registry and "tenant_context.v1" in registry.get("required_receipt_features", []))
        agent_tenants = None
        if tenant_identity:
            from .agent_tenants import load_agent_tenants
            try:
                agent_tenants = load_agent_tenants(registry)
            except (OSError, ValueError):
                # Session-proven tenants still publish; registry ones stay unknown.
                timings["agent_tenants"] = "unavailable"
        from .pull_request_audit import load as load_audit
        from .pull_request_outputs import read_merged
        try:
            facts_document = core_facts(registry)
            work_items = work_item_display(registry, facts_document)
        except (OSError, ValueError):
            facts_document = work_items = None
            timings["work_items"] = "unavailable"
        merged = read_merged(facts_document, datetime.now(timezone.utc))
        def publication(periods, **extra):
            return build_dashboard(ledger, pull_requests=merged,
                subscriptions=facts,catalog_snapshot=catalog,openrouter_snapshot=provider,
                source_revision=args.source_revision,identity_dimensions=True,activity_table=True,activity_only=True,
                tenant_identity=tenant_identity,
                agent_tenants=agent_tenants,
                calculation_cache=args.output.parent / 'calculation-cache', force_recompute=args.force_recompute,
                timings=timings, work_items=work_items, periods=periods,
                validity_minutes=args.validity_minutes, **extra)
        document = publication(CURRENT_PERIODS,
            pull_request_audit=load_audit(args.pull_request_audit), audit_pull_requests=True,
            kernel_merge_enforced_since=args.kernel_merge_enforced_since)
        if price_error:
            timings["price_observation"] = price_error
        _withhold_regression(args.output, document)
        def publish_selected(path, value):
            encoding = wire_encoding(json.loads(dashboard_bytes(value)))
            if encoding == COMPRESSED_FIELD:
                ready = row_contexts_ready is not None and row_contexts_ready(encoding)
                publish_dashboard(path, value, allow_compression=ready)
            else:
                ready = encoding == 'row_contexts' and row_contexts_ready is not None and row_contexts_ready()
                publish_dashboard(path, value, allow_row_contexts=ready)

        publish_selected(args.output, document)
        if args.last_week_output:
            # Its own receipt read and instant, after the current views. A
            # failure never withdraws the current publication; the previous
            # closed-week file then expires normally.
            try:
                closed = publication(LAST_WEEK)
                _withhold_regression(args.last_week_output, closed)
                publish_selected(args.last_week_output, closed)
                timings["last_week"] = closed['status']
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                timings["last_week"] = "publication_failed"
        timings.update(total_seconds=round(perf_counter()-started, 3), cpu_seconds=round(process_time()-cpu_started, 3))
        print(json.dumps({"schema": "stats_cost_calculation_timing/v1", **timings}), file=sys.stderr)
        print(json.dumps({'schema_version':SCHEMA,'status':document['status'],
                          'generated_at':document['generated_at'],'sha256':document['sha256']}))
        return 0
    except (OSError,ValueError,TypeError,KeyError,RuntimeError) as exc:
        _last_failure = failure_reason(exc)
        print(json.dumps({'schema': 'stats_cost_publication_failure/v1', 'reason': _last_failure,
                          'error_type': type(exc).__name__}), file=sys.stderr)
        print('Stats cost publication failed; the published snapshot stays shown with its own time.',file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
