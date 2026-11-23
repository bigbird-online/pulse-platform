"""汇总库视图 → Stats 成本发布物 `pulse_stats_cost_dashboard/v1`（合同第 4、5 节）。

每个数字的来源与口径：
- 用量、金额、归属：`v_event`（views.sql）逐事件一行；分组、合计都是同一批行的求和，
  所以按任一维度分组之和等于总数。金额 = 四类 Token × 当日生效单价（1e-12 美元整数），
  只在输出时转十进制字符串；未计价事件计入事件数与 Token，金额记未知，不记 0。
- 产出：`wi_outcomes`（outcomes.py），期内 WI 完成与验收通过的工作记录；合入 main 的 PR
  照旧在 `pull_request_outputs`。
- 订阅、OpenRouter 账单、PR 审计、WI 显示名：沿用 stats_dashboard 的独立小来源。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from pulse_compute_cost.receipts import digest, reference
from pulse_compute_cost import stats_dashboard as legacy
from pulse_compute_cost.stats_dashboard_wire import MAX_BYTES, dashboard_bytes

from . import outcomes as wi_outcomes
from . import turns
from . import prices
from . import schema_sql

SCHEMA = 'pulse_stats_cost_dashboard/v1'
QUERY_SCHEMA = 'computecost_collaboration_query/v1'
CASH_BASIS = 'openrouter_standard_token_cash'
DIMENSIONS = ('agent', 'wi', 'entrypoint', 'node', 'runtime', 'model')
# 有团队登记（ssot/registry.yaml 的 agents.<id>.tenant）时多出租户维度，与旧链路的七维发布相同。
TENANT_DIMENSIONS = ('tenant', *DIMENSIONS)
CURRENT_PERIODS = ('today', 'week', 'month')
LAST_WEEK = ('last_week',)
ZONE = ZoneInfo('Asia/Shanghai')
VALIDITY = timedelta(minutes=45)
# 节点超过这么久没有上送，或报告了积压，发布物就标"未收齐"。
STALE_PEER = timedelta(minutes=15)
TOKEN_CLASSES = (('uncached_input', 'input_uncached'), ('cache_read', 'cache_read'),
                 ('cache_write', 'cache_write'), ('output', 'output'))
UNPRICED_SAMPLE_LIMIT = 8
# 人类请求未声明 WI 的用量：v_event 的 wi 与 state 都记 'undeclared'，与未关联（'unassigned'）分开。
UNDECLARED = 'undeclared'
VIEWS_PATH = Path(__file__).with_name('views.sql')
_AGENT = re.compile(r'[a-z][a-z0-9_-]{0,39}')
_WI = re.compile(r'wi_\d{8}_\d{3}')
EVENT_COLUMNS = ('node', 'runtime', 'event_id', 'request_id', 'session_id', 'session_ref', 'agent_id', 'turn_id',
                 'source', 'granularity', 'occurred_at', 'day',
                 'model', 'cwd', 'input_uncached', 'cache_read', 'cache_write', 'output', 'agent', 'wi',
                 'wi_source', 'entrypoint', 'usd_e12', 'state', 'unlinked_reason', 'launch_evidence',
                 'unpriced_reason')


# ---------------------------------------------------------------- 连接与视图

def cwd_agent(cwd: str | None) -> str | None:
    """…/agent-harness/workspaces/<组织>/<仓库>/<WI>/<agent>[/…] 的 <agent>。

    受管工作区现名 `workspace.op_<哈希>.<序号>`，不是 Agent 名，不取（只认 Agent 名的拼写）。
    """
    if not isinstance(cwd, str):
        return None
    parts = cwd.split('/')
    for i in range(len(parts) - 5):
        if parts[i] == 'agent-harness' and parts[i + 1] == 'workspaces' and _WI.fullmatch(parts[i + 4]):
            name = parts[i + 5]
            return name if _AGENT.fullmatch(name) else None
    return None


def cwd_wi(cwd: str | None) -> str | None:
    """…/agent-harness/workspaces/<组织>/<仓库>/<WI>/… 的 <WI>。"""
    if not isinstance(cwd, str):
        return None
    parts = cwd.split('/')
    for i in range(len(parts) - 4):
        if parts[i] == 'agent-harness' and parts[i + 1] == 'workspaces' and _WI.fullmatch(parts[i + 4]):
            return parts[i + 4]
    return None


def connect(path: str | Path) -> sqlite3.Connection:
    """打开汇总库（表不存在则按 schema.sql 建），注册视图函数并建 TEMP 视图。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(str(path), timeout=60, isolation_level=None)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(schema_sql())
    wi_outcomes.ensure(conn)
    turns.ensure(conn)
    attach_views(conn)
    refresh_holder(conn)
    return conn


def refresh_holder(conn: sqlite3.Connection) -> None:
    """按最新并入的会话写入重填 v_event 第 4 层（t_holder）。"""
    conn.execute('DELETE FROM t_holder')
    conn.executemany('INSERT INTO t_holder(session_ref, turn_id, wi) VALUES(?, ?, ?)', wi_outcomes.holder_claims(conn))


def attach_views(conn: sqlite3.Connection) -> None:
    conn.create_function('pm_model_key', 1, prices.model_key, deterministic=True)
    conn.create_function('pm_cwd_agent', 1, cwd_agent, deterministic=True)
    conn.create_function('pm_cwd_wi', 1, cwd_wi, deterministic=True)
    conn.create_function('pm_amount', 5, prices.amount_e12, deterministic=True)
    conn.executescript(VIEWS_PATH.read_text(encoding='utf-8'))


def _utc_bound(day: date) -> str:
    """Asia/Shanghai 某日 00:00 的 UTC 毫秒串。"""
    return datetime.combine(day, time.min, ZONE).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _view_body(name: str) -> str:
    """views.sql 里 `CREATE TEMP VIEW <name> AS` 之后、到结尾分号为止的 SELECT。"""
    text = VIEWS_PATH.read_text(encoding='utf-8')
    head = f'CREATE TEMP VIEW {name} AS '
    start = text.index(head) + len(head)
    return text[start:text.index(';', start)]


def _launch_select() -> str:
    """views.sql 里 t_launch 的 SELECT（每次读事件都在同一读事务里重算）。"""
    text = VIEWS_PATH.read_text(encoding='utf-8')
    head = 'CREATE TEMP TABLE t_launch AS'
    start = text.index(head) + len(head)
    return text[start:text.index(';', start)]


@contextmanager
def read_snapshot(conn: sqlite3.Connection):
    """一个读事务：其中的全部读取看同一个快照（WAL 下看不到其间别的连接提交的写入）。

    只写临时表；结束时 COMMIT（主库无写入），出错 ROLLBACK。已在事务里则直接沿用。
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute('BEGIN')
    try:
        yield conn
    except BaseException:
        conn.execute('ROLLBACK')
        raise
    conn.execute('COMMIT')


def events(conn: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    """[start, end) Asia/Shanghai 日期内 `v_event` 的全部行。

    与直接查 `v_event` 同一口径（同一段 SQL，取自 views.sql），只是分层落到临时表：
    视图层层展开后，带标量子查询的列会在外层每被引用一次就重算一次（wi_pick、unlinked_reason
    各被引用三到五次），逐层落表后每行每列只算一次。
    全部步骤在同一个读事务里（`read_snapshot`）：启动记录表、会话写入补充、各层事件都读同一个快照，
    不会混用两次收件之间的数据。调用方已开 `read_snapshot` 时沿用它，把别的读取也放进同一快照。
    """
    window = (_utc_bound(start - timedelta(days=1)), _utc_bound(end + timedelta(days=1)),
              start.isoformat(), end.isoformat())
    sql = ('SELECT ' + ', '.join(EVENT_COLUMNS) + ' FROM ('
           + _view_body('v_event').replace('FROM v_event_base b', 'FROM temp.t_event_base b') + ')')
    result = []
    with read_snapshot(conn):
        conn.execute('DELETE FROM temp.t_launch')
        conn.execute('INSERT INTO temp.t_launch ' + _launch_select())
        refresh_holder(conn)
        conn.execute('DROP TABLE IF EXISTS temp.t_event_raw')
        conn.execute('DROP TABLE IF EXISTS temp.t_event_base')
        conn.execute('CREATE TEMP TABLE t_event_raw AS SELECT * FROM v_event_raw '
                     'WHERE occurred_at >= ? AND occurred_at < ? AND day >= ? AND day < ?', window)
        conn.execute('CREATE TEMP TABLE t_event_base AS '
                     + _view_body('v_event_base').replace('FROM v_event_raw r', 'FROM temp.t_event_raw r'))
        for row in conn.execute(sql):
            event = dict(zip(EVENT_COLUMNS, row))
            # 分组键必须是非空字符串；没有模型名的事件（已记为未计价 model_unknown）归入 'unknown'。
            event['model'] = event['model'] or 'unknown'
            result.append(event)
        conn.execute('DROP TABLE temp.t_event_raw')
        conn.execute('DROP TABLE temp.t_event_base')
    return result


# ---------------------------------------------------------------- 金额与聚合

def money(e12: int | None) -> str | None:
    """1e-12 美元整数 → 精确十进制字符串。"""
    if e12 is None:
        return None
    whole, rest = divmod(int(e12), prices.SCALE)
    return str(whole) if rest == 0 else ('%d.%012d' % (whole, rest)).rstrip('0')


def fact_ref(row: Mapping[str, Any]) -> str:
    return reference('usage', [row['runtime'], row['event_id'], row['request_id']])


def usage(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    tokens = {}
    for name, column in TOKEN_CLASSES:
        values = [r[column] for r in rows]
        tokens[name] = {'known': sum(v for v in values if v is not None),
                        'unknown_event_count': sum(1 for v in values if v is None)}
    return {'event_count': len(rows), 'known_total_tokens': sum(t['known'] for t in tokens.values()),
            'tokens': tokens}


def _priced(rows: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
    count = amount = 0
    for r in rows:
        if r['state'] != 'unpriced':
            count += 1
            amount += r['usd_e12']
    return count, amount


def cost(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """合同第 5 节 `groups[].cost`；校验器 `_cost`/`_priced_total` 的口径。"""
    count, amount = _priced(rows)
    reasons = []
    if any(r['wi'] == 'unassigned' for r in rows):
        reasons.append('attribution_unknown')
    if count < len(rows):
        reasons.append('incomplete_price_or_quantity')
    if not rows:
        reasons.append('no_usage_observations')
    complete = not reasons
    result = {'machine_cash_usd': money(amount) if complete else None,
              'partial_machine_cash_usd': money(amount) if not complete and count else None,
              'cash_basis': CASH_BASIS, 'reasons': sorted(reasons),
              'priced_event_count': count, 'priced_machine_cash_usd': money(amount) if count else None}
    unpriced = sorted(({'fact_ref': fact_ref(r), 'reason': r['unpriced_reason']}
                       for r in rows if r['state'] == 'unpriced'), key=lambda x: (x['fact_ref'], x['reason']))
    result['unpriced'] = unpriced
    if len(unpriced) > UNPRICED_SAMPLE_LIMIT:
        # 与 stats_dashboard.project_cost 相同的摘要：每个原因留两条样本，计数与指纹覆盖全部。
        counts: dict[str, int] = {}
        samples = []
        for row in unpriced:
            counts[row['reason']] = counts.get(row['reason'], 0) + 1
            if counts[row['reason']] <= 2:
                samples.append(row)
        result.update(unpriced=samples, unpriced_count=len(unpriced), unpriced_reason_counts=counts,
                      unpriced_sha256=digest(unpriced))
    return result


def attribution(rows: list[Mapping[str, Any]], wi: str, job_names: Mapping[str, str | None]) -> dict[str, Any]:
    sources: dict[str, list] = defaultdict(lambda: [0, 0])
    for r in rows:
        entry = sources[r['wi_source']]
        entry[0] += 1
        if r['state'] != 'unpriced':
            entry[1] += r['usd_e12']
    # 来源行的金额是该来源已计价行之和；全部未计价时为 0（校验器要求字符串）。
    result = {'sources': {k: {'event_count': n, 'priced_machine_cash_usd': money(a)}
                          for k, (n, a) in sorted(sources.items())}}
    if wi.startswith('cron:'):
        job = wi[len('cron:'):]
        name = job_names.get(job)
        result['job'] = {'id': job, 'name': name if isinstance(name, str) and 1 <= len(name) <= 120
                         and not any(ord(c) < 32 for c in name) else None}
    return result



# ---------------------------------------------------------------- 视图

def period_window(day: date, period: str) -> tuple[date, date]:
    return legacy.period_window(day, period)


def _outcome_rows(conn, start: date, end: date) -> list[dict[str, Any]]:
    return [r for r in wi_outcomes.in_window(conn, start.isoformat(), end.isoformat())
            if r['kind'] in ('wi_completed', 'work_accepted')]


def _row_outcomes(status: str, refs: list[str]) -> dict[str, Any]:
    n = len(refs)
    count = n if status in ('complete', 'observed') and (n or status == 'complete') else None
    return {'status': status, 'count': count, 'known_count': n, 'refs': refs,
            'metric_scope': 'wi_reference_not_runtime_allocation'}


def tenant_of(agent: str, agent_tenants: Mapping[str, Mapping[str, str]] | None) -> str:
    """Agent 的登记租户；Agent 未识别记 'unassigned'，登记里没有该 Agent 记 'unknown'（App 都显示"租户未知"）。"""
    if agent in ('unassigned', 'not_applicable'):
        return agent
    found = (agent_tenants or {}).get(agent)
    return found['id'] if found else 'unknown'


def tenant_identity(key: Mapping[str, str], agent_tenants: Mapping[str, Mapping[str, str]] | None) -> dict[str, Any]:
    """有登记租户的行带 `tenant` 身份：来源是团队登记的 Agent→租户绑定（与旧链路 registry 来源同一引用）。"""
    tenant = key.get('tenant')
    found = (agent_tenants or {}).get(key.get('agent'))
    if not found or tenant != found['id']:
        return {}
    return {'tenant': {'id': found['id'], 'name': found['name'], 'binding_refs': [found['binding_ref']],
                       'sources': ['registry'], 'registry_conflicts': []}}


def load_tenants(registry: Mapping[str, Any] | None) -> dict[str, dict[str, str]] | None:
    """团队登记里的 Agent→租户；没有登记或读不了返回 None（发布物不出租户维度，App 不显示该列）。"""
    if not registry:
        return None
    if not isinstance(registry, Mapping):
        print(json.dumps({'schema': 'pulse_meter_warning/v1', 'warning': 'tenant_registry_not_an_object'}),
              file=sys.stderr)
        return None
    from pulse_compute_cost.agent_tenants import load_agent_tenants
    try:
        found = load_agent_tenants(registry)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError) as exc:
        print(json.dumps({'schema': 'pulse_meter_warning/v1', 'warning': 'tenant_registry_unreadable',
                          'error_type': type(exc).__name__}), file=sys.stderr)
        return None
    return found or None


def build_view(rows: list[Mapping[str, Any]], period: str, start: date, end: date, *, watermark: int,
               recorded_at: str | None, catalog: tuple[str | None, str | None], coverage: str,
               outcome_rows: list[Mapping[str, Any]], outcome_status: str,
               job_names: Mapping[str, str | None],
               agent_tenants: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, Any]:
    dims = TENANT_DIMENSIONS if agent_tenants is not None else DIMENSIONS
    by_key: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_key[tuple(tenant_of(r['agent'], agent_tenants) if d == 'tenant' else r[d] for d in dims)].append(r)
    groups = []
    for key, members in sorted(by_key.items()):
        members.sort(key=lambda r: (r['occurred_at'], fact_ref(r)))
        group_key = dict(zip(dims, key))
        groups.append({'key': group_key, 'cost': cost(members), 'usage': usage(members),
                       **tenant_identity(group_key, agent_tenants),
                       'attribution': attribution(members, group_key['wi'], job_names),
                       'detail': {'first_usage_at': members[0]['occurred_at'],
                                  'last_usage_at': members[-1]['occurred_at'],
                                  'fact_refs': [fact_ref(members[0])], 'fact_ref_count': len(members),
                                  'attribution_refs': [], 'attribution_ref_count': 0}})
    # 产出：每条只挂到该 WI 的一行（已计价金额最大的那行），避免按行相加时重复计数。
    items = [wi_outcomes.item(r) for r in outcome_rows] if outcome_status != 'unavailable' else []
    by_wi: dict[str, list] = defaultdict(list)
    for it in items:
        by_wi[it['wi']].append(it)
    owner_row: dict[str, int] = {}
    ranks: dict[str, tuple] = {}
    for index, g in enumerate(groups):
        wi = g['key']['wi']
        if wi not in by_wi:
            continue
        rank = (g['cost']['priced_event_count'] and _priced(by_key[tuple(g['key'][d] for d in dims)])[1],
                g['usage']['event_count'])
        if wi not in ranks or rank > ranks[wi]:
            owner_row[wi], ranks[wi] = index, rank
    for index, g in enumerate(groups):
        wi = g['key']['wi']
        refs = [it['outcome_ref'] for it in by_wi.get(wi, [])] if owner_row.get(wi) == index else []
        g['outcomes'] = _row_outcomes(outcome_status, refs)
    outcome_only = []
    for wi, found in sorted(by_wi.items()):
        if wi in owner_row:
            continue
        owners = {it['responsible'] for it in found if it['responsible']}
        key = dict.fromkeys(dims, 'not_applicable')
        key.update(wi=wi, agent=next(iter(owners)) if len(owners) == 1 else 'unassigned')
        if agent_tenants is not None:
            key['tenant'] = tenant_of(key['agent'], agent_tenants)
        outcome_only.append({'key': key, 'cost': cost([]), **tenant_identity(key, agent_tenants),
                             'usage': {'tokens': {name: {'known': None, 'unknown_event_count': 0}
                                                  for name, _ in TOKEN_CLASSES},
                                       'event_count': 0, 'known_total_tokens': None},
                             'outcomes': _row_outcomes(outcome_status, [it['outcome_ref'] for it in found])})
    for row in outcome_only:
        row['cost'].pop('priced_event_count'); row['cost'].pop('priced_machine_cash_usd')
    n = len(items)
    view_outcomes = {'status': outcome_status,
                     'count': n if outcome_status in ('complete', 'observed') and (n or outcome_status == 'complete')
                     else None,
                     'known_count': n, 'metric_scope': 'wi_outcomes_in_period', 'items': items,
                     'unallocated_refs': [], 'conflict_refs': [],
                     'coverage': {'complete': 'complete', 'observed': 'observed_subset'}.get(outcome_status, 'unknown')}
    agents = {g['key']['agent'] for g in groups + outcome_only}
    agents |= {it['responsible'] for it in items if it['responsible']}
    agents -= {'unassigned', 'ambiguous', 'not_applicable'}

    total = usage(rows)
    agent_unassigned = [r for r in rows if r['agent'] == 'unassigned']
    wi_unassigned = [r for r in rows if r['wi'] == 'unassigned']
    wi_undeclared = [r for r in rows if r['wi'] == UNDECLARED]
    cron = sum(1 for r in rows if r['wi'].startswith('cron:'))
    no_wi = sum(1 for r in rows if r['wi'] == 'none')

    def priced_usd(subset):
        count, amount = _priced(subset)
        return money(amount) if count else None

    summary_usage = {**total, 'source_coverage': coverage, 'wi_inventory_coverage': 'unknown',
                     'last_usage_at': max((r['occurred_at'] for r in rows), default=None),
                     'agent_unassigned_event_count': len(agent_unassigned),
                     'agent_unassigned_priced_usd': priced_usd(agent_unassigned),
                     'wi_unassigned_event_count': len(wi_unassigned),
                     'wi_unassigned_priced_usd': priced_usd(wi_unassigned),
                     # 人类请求未声明 WI（不算未关联、不算异常），按 Agent 见各行 wi='undeclared'
                     'wi_undeclared_event_count': len(wi_undeclared),
                     'wi_undeclared_priced_usd': priced_usd(wi_undeclared),
                     'cron_event_count': cron, 'no_wi_event_count': no_wi, 'non_wi_event_count': cron + no_wi}
    query = {'agent': None, 'agent_role': 'executor', 'as_of': None, 'cost_basis': 'machine_standard',
             'entrypoint': None, 'from_date': start.isoformat(), 'group_by': list(dims),
             'human_hourly_rate': None, 'human_rate_policy': None, 'model': None, 'node': None,
             'runtime': None, 'scope': 'activity', 'timezone': 'Asia/Shanghai', 'to_date': end.isoformat(),
             'value_from': None, 'value_to': None, 'watermark': watermark, 'work_items': []}
    if agent_tenants is not None:
        query['tenant'] = None
    view = {'period': period, 'dimension': 'activity', 'source_schema': QUERY_SCHEMA, 'query': query,
            'watermark': watermark, 'recorded_at': recorded_at, 'catalog_sha256': catalog[0],
            'catalog_observed_at': catalog[1],
            'summary': {'cost': cost(rows), 'usage': summary_usage}, 'groups': groups,
            'outcomes': view_outcomes, 'outcome_only_groups': outcome_only, 'active_agents': sorted(agents)}
    if agent_tenants is not None:
        view['population_scope'] = 'all_registered_tenants'   # 全平台、无过滤
    view['report_sha256'] = digest(view)
    return view



# ---------------------------------------------------------------- 节点状态

PENDING_UNACKED = 'pending_unacked:'   # meta 键前缀：节点报来的待确认传输量（线①写入）


def node_coverage(conn: sqlite3.Connection, now: datetime,
                  expected: Iterable[str] | None) -> tuple[str | None, list[dict[str, Any]], dict[str, str]]:
    """(最近上送时间, 未收齐节点, 各未收齐节点的原因)。

    `expected` 是 refresh.json 启用的节点（peers 去 paused_peers，加本机）。每个都在
    STALE_PEER 内报到、积压字节为 0、meta `pending_unacked:<节点>` 为 0 才算收齐；
    没有 `expected` 就无从判定，原因记 `expected_nodes_unknown`。
    """
    latest: dict[str, tuple] = {}
    for peer, received, backlog_bytes, backlog_files in conn.execute(
            'SELECT peer, last_received_at, backlog_bytes, backlog_files FROM transfer_cursors'):
        if peer not in latest or received > latest[peer][0]:
            latest[peer] = (received, backlog_bytes, backlog_files)
    unacked = {k[len(PENDING_UNACKED):]: v for k, v in conn.execute(
        'SELECT key, value FROM meta WHERE key LIKE ?', (PENDING_UNACKED + '%',))}
    reasons: dict[str, str] = {}
    files: dict[str, int] = {}
    nodes = sorted(set(expected)) if expected is not None else sorted(latest)
    if expected is None:
        reasons[''] = 'expected_nodes_unknown'
    for node in nodes:
        if node not in latest:
            reasons[node] = 'never_received'
            continue
        received, backlog_bytes, backlog_files = latest[node]
        files[node] = int(backlog_files or 0)
        at = _instant(received)
        if at is None or now - at > STALE_PEER:
            reasons[node] = 'stale'
        elif backlog_bytes > 0:
            reasons[node] = 'backlog'
        elif node not in unacked:
            reasons[node] = 'pending_unacked_unknown'
        elif unacked[node] != '0':
            reasons[node] = 'pending_unacked'
    pending = [{'id': node, 'deferred_bundles': max(files.get(node, 0), 1)}
               for node in sorted(reasons) if node and re.fullmatch(r'[a-z0-9_-]{1,40}', node)]
    recorded = max((v[0] for v in latest.values()), default=None)
    return recorded, pending, reasons


def watermark(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='next_seq'").fetchone()
    try:
        return max(int(row[0]) - 1, 0) if row else 0
    except (TypeError, ValueError):
        return 0


def job_names(conn: sqlite3.Connection) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for job, name in conn.execute("SELECT job_id, job_name FROM launch_receipts WHERE kind='scheduled_session' "
                                  "AND job_id IS NOT NULL ORDER BY occurred_at"):
        if name or job not in result:
            result[job] = name
    return result


# ---------------------------------------------------------------- 发布物

def _instant(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _read_json(path: Path | None):
    if path is None:
        return None
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            return None
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def build(central_db, *, now: datetime, source_revision: str, catalog_path, last_week: bool = False,
          wi_facts_path=None, procurement_facts=legacy.DEFAULT_FACTS, openrouter_snapshot=None,
          pull_request_audit=None, kernel_merge_enforced_since: str | None = None,
          timings: dict | None = None, expected_nodes: Iterable[str] | None = None,
          events_out: list | None = None,
          agent_tenants: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, Any]:
    """汇总库 → 一份发布物。`last_week` 只出上周的联合视图（cost-last-week-v1.json）。

    合同第 6 节固定前四个参数；其余是旧发布器已有的独立小来源，缺省即不出。
    `agent_tenants`：团队登记的 Agent→租户（`load_tenants`）；给了就出七维（含租户），否则六维。
    `events_out`：给一个列表，本次算出的事件行追加进去，供同一轮的诊断复用。
    `expected_nodes`：refresh.json 启用的节点（peers 去 paused_peers，加本机）；缺省时来源覆盖记 unknown。
    """
    if not re.fullmatch(r'[0-9a-f]{40}', source_revision or ''):
        raise ValueError('exact source revision required')
    if now.tzinfo is None:
        raise ValueError('aware now required')
    periods = LAST_WEEK if last_week else CURRENT_PERIODS
    today = now.astimezone(ZONE).date()
    expiry = min(now + VALIDITY, datetime.combine(today + timedelta(days=1), time.min, ZONE))
    facts = _read_json(Path(procurement_facts)) if procurement_facts else None
    result = {'schema_version': SCHEMA, 'generated_at': now.isoformat(),
              'expires_at': expiry.astimezone(timezone.utc).isoformat(), 'source_revision': source_revision,
              'status': 'unavailable', 'reason': 'source_not_configured', 'views': [],
              'subscriptions': legacy._subscriptions(facts or {'schema_version': 'pulse_stats_procurement_facts/v1',
                                                               'subscriptions': []}),
              'openrouter': legacy.project_openrouter(_read_json(openrouter_snapshot), now=now)}
    core = _read_json(wi_facts_path)
    merged = __import__('pulse_compute_cost.pull_request_outputs', fromlist=['read_merged']).read_merged(
        core if isinstance(core, dict) and core.get('read_only') is True else None, now)
    backlog: list = []
    if central_db is not None and Path(central_db).exists():
        conn = connect(central_db)
        try:
            snapshot = None
            if catalog_path is not None:
                from pulse_compute_cost.catalog_source import load_catalog_snapshot
                try:
                    snapshot = load_catalog_snapshot(Path(catalog_path))
                except (ValueError, OSError):
                    snapshot = None
            if snapshot is not None:
                prices.load(conn, snapshot)
            if wi_outcomes.sync(conn, Path(wi_facts_path) if wi_facts_path else None, now=now)['status'] == 'read':
                refresh_holder(conn)
            # 价表身份、节点覆盖、水位、事件、产出、任务名：全部在同一个读事务（同一快照）里读。
            with read_snapshot(conn):
                catalog = prices.catalog_identity(conn)
                observed = _instant(catalog[1])
                catalog = (catalog[0], observed.isoformat() if observed and observed <= now else None)
                recorded_at, backlog, missing = node_coverage(conn, now, expected_nodes)
                if timings is not None and missing:
                    timings['coverage_missing'] = missing
                if recorded_at is not None and (_instant(recorded_at) is None or _instant(recorded_at) > now):
                    recorded_at = None
                coverage = 'complete' if not missing else 'unknown'
                mark = watermark(conn)
                windows = {p: period_window(today, p) for p in periods}
                lo = min(s for s, _ in windows.values())
                hi = max(e for _, e in windows.values())
                rows = events(conn, lo, hi)
                if events_out is not None:
                    events_out.extend(rows)   # 同一轮的诊断复用这批行，不再算一遍
                if timings is not None:
                    timings['events'] = len(rows)
                outcome_status = wi_outcomes.coverage(conn, now=now)
                names = job_names(conn)
                views = []
                for period, (start, end) in windows.items():
                    selected = [r for r in rows if start.isoformat() <= r['day'] < end.isoformat()]
                    views.append(build_view(selected, period, start, end, watermark=mark, recorded_at=recorded_at,
                                            catalog=catalog, coverage=coverage,
                                            outcome_rows=_outcome_rows(conn, start, end),
                                            outcome_status=outcome_status, job_names=names,
                                            agent_tenants=agent_tenants))
        finally:
            conn.close()
        views = [legacy.with_pull_requests(v, merged) for v in views]
        result.update(status='available', reason=None, views=views)
    if not last_week:
        from pulse_compute_cost.pull_request_audit import derive, load as load_audit, unregistered
        audit = load_audit(Path(pull_request_audit)) if pull_request_audit else None
        anomalies = derive(audit, merged, now, kernel_merge_enforced_since)
        anomalies['unregistered_pull_requests'] = unregistered(
            audit, merged, now, {p: period_window(today, p) for p in periods}, set())
        result['pull_request_anomalies'] = anomalies
    work_items = legacy.work_item_display(None, core) if isinstance(core, dict) else None
    if work_items is not None:
        named = {g['key'].get('wi') for v in result['views'] for rows_name in
                 ('groups', 'outcome_only_groups', 'pull_request_only_groups') for g in v.get(rows_name, [])}
        parents = {wi: item['parent'] for wi, item in work_items.items() if item.get('parent')}
        named |= {parents[wi] for wi in named & set(parents)}
        result['work_items'] = [{'id': wi, **work_items[wi]} for wi in sorted(named & set(work_items))]
        if parents:
            from pulse_compute_cost.pull_request_outputs import families
            updated = []
            for view in result['views']:
                found = families(view, parents)
                updated.append({**view, 'work_item_families': found} if found else view)
            result['views'] = updated
    if backlog and result['status'] == 'available':
        result['collection_backlog'] = {'status': 'partial', 'reason': 'observation_backlog', 'peers': backlog[:8]}
    result['sha256'] = digest(result)
    if len(dashboard_bytes(result)) > MAX_BYTES:
        result = {k: v for k, v in result.items() if k not in ('collection_backlog', 'sha256')}
        result.update(status='unavailable', reason='query_payload_too_large', views=[])
        result['sha256'] = digest(result)
    return result


# ---------------------------------------------------------------- 诊断与返工线（合同第 7 节用例 10）

def month_rows(rows: Iterable[Mapping[str, Any]], now: datetime) -> list[Mapping[str, Any]]:
    """本月（Asia/Shanghai）的行：从 build 的 `events_out` 里取，不再查库。"""
    start, end = period_window(now.astimezone(ZONE).date(), 'month')
    return [r for r in rows if start.isoformat() <= r['day'] < end.isoformat()]


def read_watermark(path) -> int:
    """只读汇总库水位，不建视图、不算会话写入补充（每拍判断要不要发布时用）。"""
    conn = sqlite3.connect(f'file:{Path(path)}?mode=ro', uri=True, timeout=60)
    try:
        return watermark(conn)
    finally:
        conn.close()


def _month_window(month: str) -> tuple[date, date]:
    start = date.fromisoformat(month + '-01')
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def diagnostics(rows: list[Mapping[str, Any]], *, limit: int = 20) -> dict[str, Any]:
    """未关联按已计价金额、未计价按 Token 的前 `limit` 项（Agent、会话、原因），以及按原因的合计。"""
    unlinked: dict[tuple, list] = defaultdict(lambda: [0, 0, 0])
    unpriced: dict[tuple, list] = defaultdict(lambda: [0, 0])
    unlinked_reasons: dict[str, list] = defaultdict(lambda: [0, 0])
    unpriced_reasons: dict[str, list] = defaultdict(lambda: [0, 0])
    undeclared_agents: dict[str, list] = defaultdict(lambda: [0, 0])
    for r in rows:
        tokens = sum(r[c] or 0 for _, c in TOKEN_CLASSES)
        if r['wi'] == UNDECLARED:
            undeclared_agents[r['agent']][0] += 1
            undeclared_agents[r['agent']][1] += r['usd_e12'] or 0
        if r['wi'] == 'unassigned':
            key = (r['agent'], r['node'], r['runtime'], r['session_id'], r['session_ref'],
                   r['unlinked_reason'], r['launch_evidence'])
            entry = unlinked[key]
            entry[0] += 1
            entry[1] += r['usd_e12'] or 0
            entry[2] += tokens
            unlinked_reasons[r['unlinked_reason']][0] += 1
            unlinked_reasons[r['unlinked_reason']][1] += r['usd_e12'] or 0
        if r['state'] == 'unpriced':
            key = (r['agent'], r['node'], r['runtime'], r['session_id'], r['session_ref'], r['model'],
                   r['unpriced_reason'])
            unpriced[key][0] += 1
            unpriced[key][1] += tokens
            unpriced_reasons[r['unpriced_reason']][0] += 1
            unpriced_reasons[r['unpriced_reason']][1] += tokens
    top_unlinked = sorted(unlinked.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0]))[:limit]
    top_unpriced = sorted(unpriced.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0]))[:limit]
    fields = ('agent', 'node', 'runtime', 'session_id', 'session_ref')
    return {
        'schema': 'pulse_meter_attribution_diagnostics/v1',
        'unlinked_top': [{**dict(zip(fields, k[:5])), 'reason': k[5], 'launch_evidence': k[6],
                          'event_count': v[0], 'priced_usd': money(v[1]), 'tokens': v[2]}
                         for k, v in top_unlinked],
        'unpriced_top': [{**dict(zip(fields, k[:5])), 'model': k[5], 'reason': k[6],
                          'event_count': v[0], 'tokens': v[1]} for k, v in top_unpriced],
        'unlinked_by_reason': {k: {'event_count': v[0], 'priced_usd': money(v[1])}
                               for k, v in sorted(unlinked_reasons.items())},
        'unpriced_by_reason': {k: {'event_count': v[0], 'tokens': v[1]} for k, v in sorted(unpriced_reasons.items())},
        'undeclared_by_agent': {k: {'event_count': v[0], 'priced_usd': money(v[1])}
                                for k, v in sorted(undeclared_agents.items())},
    }


def _baseline(document: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], datetime | None]:
    """旧发布物月视图的行与它的生成时间。"""
    if 'compressed_dashboard' in document or 'row_contexts' in document:
        from pulse_compute_cost.stats_dashboard_wire import expand_dashboard
        document = expand_dashboard(document)
    views = [v for v in document.get('views', []) if v.get('period') == 'month' and v.get('dimension') == 'activity']
    if len(views) != 1:
        raise ValueError('baseline has no single month activity view')
    return views[0]['groups'], _instant(document.get('generated_at'))


def _dec(value: Decimal) -> str:
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _old_amount(group: Mapping[str, Any]) -> Decimal:
    c = group['cost']
    value = c.get('priced_machine_cash_usd') or c.get('machine_cash_usd') or c.get('partial_machine_cash_usd')
    return Decimal(value) if value else Decimal(0)


def anomaly_ratios(rows: list[Mapping[str, Any]], *, baseline: Mapping[str, Any] | None = None,
                   explanations: Mapping[str, Mapping[str, Any]] | None = None,
                   threshold: Decimal = Decimal('0.01'), nodes: Iterable[str] | None = None,
                   max_rates: tuple[int | None, int | None, int | None, int | None] | None = None) -> dict[str, Any]:
    """用例 10：（未关联 + 未计价 + 对旧值无解释的缺口）占月合计的比例，按金额与按事件数各一个。

    - 未关联：wi='unassigned' 的事件（含其中未计价的，只计一次）。
    - 未计价：state='unpriced' 的事件；金额未知，所以按金额的比例在有未计价事件时只是下界。
    - 缺口按净额：同一 (runtime, model) 下旧值（baseline 月视图）高于新值的部分，扣掉
      `explanations['<runtime>/<model>']` 给出的 usd/events 后的余数。归到别的 Agent 或 WI 的
      不算缺口；新值只取旧发布物生成时刻以前的事件。`agents` 另列各 Agent 的新旧差，供逐项解释。
    - `max_rates`：价表各类 Token 的最高单价（1e-12 美元）。只用来给未计价事件算诊断估算
      `unpriced_estimate_usd`（basis=max_observed_rate），不参与判定：有 Token 非零的未计价事件时，
      按金额的比例只是下界，status 为 unverified。
    - `nodes`：只对比这些节点——新链路的事件与基线的行都按 `node` 过滤（新链路只追尾这些节点时，
      不能拿全平台的旧值来比）。`baseline_by_node` 始终列出基线各节点的金额与事件数。
    """
    wanted = sorted(set(nodes)) if nodes else None
    if wanted is not None:
        rows = [r for r in rows if r['node'] in wanted]
    total_events = len(rows)
    priced_count, priced_total = _priced(rows)
    unlinked = [r for r in rows if r['wi'] == 'unassigned']
    undeclared = [r for r in rows if r['wi'] == UNDECLARED]
    unpriced = [r for r in rows if r['state'] == 'unpriced']
    abnormal_events = len({id(r) for r in unlinked} | {id(r) for r in unpriced})
    unlinked_usd = Decimal(_priced(unlinked)[1]) / prices.SCALE
    gap_usd, gap_events, gaps, agents, baseline_at = Decimal(0), 0, [], [], None
    by_node: dict[str, list] = {}
    if baseline is not None:
        groups, generated = _baseline(baseline)
        for g in groups:
            entry = by_node.setdefault(g['key'].get('node') or 'unknown', [Decimal(0), 0])
            entry[0] += _old_amount(g)
            entry[1] += g.get('usage', {}).get('event_count', 0)
        if wanted is not None:
            groups = [g for g in groups if g['key'].get('node') in wanted]
        cutoff = generated.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z' if generated else None
        baseline_at = cutoff
        old: dict[tuple, list] = defaultdict(lambda: [Decimal(0), 0])
        old_agent: dict[str, list] = defaultdict(lambda: [Decimal(0), 0])
        for g in groups:
            model = g['key'].get('model')
            # 旧链路把没有模型名的用量记作 'unassigned'，新链路记作 'unknown'：同一类。
            key = (g['key'].get('runtime'), 'unknown' if model in ('unassigned', 'unknown', None) else model)
            old[key][0] += _old_amount(g)
            old[key][1] += g.get('usage', {}).get('event_count', 0)
            old_agent[g['key'].get('agent')][0] += _old_amount(g)
            old_agent[g['key'].get('agent')][1] += g.get('usage', {}).get('event_count', 0)
        new: dict[tuple, list] = defaultdict(lambda: [0, 0])
        new_agent: dict[str, list] = defaultdict(lambda: [0, 0])
        for r in rows:
            if cutoff is None or r['occurred_at'] <= cutoff:
                usd = r['usd_e12'] if r['state'] != 'unpriced' else 0
                for table, key in ((new, (r['runtime'], r['model'])), (new_agent, r['agent'])):
                    table[key][0] += usd
                    table[key][1] += 1
        for key, (old_usd, old_events) in sorted(old.items(), key=lambda kv: str(kv[0])):
            new_usd = Decimal(new[key][0]) / prices.SCALE
            short_usd = max(old_usd - new_usd, Decimal(0))
            short_events = max(old_events - new[key][1], 0)
            explained = (explanations or {}).get('%s/%s' % key) or {}
            rest_usd = max(short_usd - Decimal(str(explained.get('usd', '0'))), Decimal(0))
            rest_events = max(short_events - int(explained.get('events', 0)), 0)
            if short_usd or short_events:
                gaps.append({'runtime': key[0], 'model': key[1], 'old_usd': _dec(old_usd), 'new_usd': _dec(new_usd),
                             'short_usd': _dec(short_usd), 'short_events': short_events,
                             'unexplained_usd': _dec(rest_usd), 'unexplained_events': rest_events,
                             'explanation': explained.get('reason')})
            gap_usd += rest_usd
            gap_events += rest_events
        for agent in sorted(set(old_agent) | set(new_agent)):
            old_usd = old_agent[agent][0]
            new_usd = Decimal(new_agent[agent][0]) / prices.SCALE
            if old_usd != new_usd:
                agents.append({'agent': agent, 'old_usd': _dec(old_usd), 'new_usd': _dec(new_usd),
                               'old_events': old_agent[agent][1], 'new_events': new_agent[agent][1]})
    total_usd = Decimal(priced_total) / prices.SCALE
    amount_ratio = (unlinked_usd + gap_usd) / total_usd if total_usd else None
    # 未计价事件四类 Token 都已知且为 0 时金额必为 0，比例不再只是下界。
    lower_bound = any(any(r[c] is None or r[c] != 0 for _, c in TOKEN_CLASSES) for r in unpriced)
    # 未计价金额的诊断估算（不参与判定）：按价表里该类观测到的最高单价计。它不是该模型的
    # 硬上界——可比模型进不进价表就会改变它。任一事件有未知数量、或某类用量为正而该类在价表里
    # 没有任何已知单价，就不出估算。
    upper_e12 = None
    if max_rates is not None:
        upper_e12 = 0
        for r in unpriced:
            for (_, column), rate in zip(TOKEN_CLASSES, max_rates):
                quantity = r[column]
                if quantity is None or (quantity and rate is None):
                    upper_e12 = None
                    break
                upper_e12 += quantity * (rate or 0)
            if upper_e12 is None:
                break
    if not lower_bound:
        upper_e12 = 0
    upper_usd = None if upper_e12 is None else Decimal(upper_e12) / prices.SCALE
    # 判定：下界已越线即不通过；只要还有 Token 非零（或数量未知）的未计价事件，金额比例只是下界，
    # 结果不作数（unverified）；未计价事件全是零 Token（金额必为 0）时才照常判定。
    if amount_ratio is None:
        amount_pass = None
    elif amount_ratio > threshold:
        amount_pass = False
    elif lower_bound:
        amount_pass = None
    else:
        amount_pass = True
    event_ratio = Decimal(abnormal_events + gap_events) / total_events if total_events else None
    event_pass = None if event_ratio is None else event_ratio <= threshold
    # 没有旧值基线就核不了缺口：结果不作数，不能作为切换凭据。
    # 基线比新数据还新：两边不是同一时段，缺口不可比。
    stale_data = bool(baseline_at and rows and baseline_at > max(r['occurred_at'] for r in rows) and
                      _instant(baseline_at) - _instant(max(r['occurred_at'] for r in rows)) > STALE_PEER)
    status = ('unverified' if baseline is None or stale_data or amount_pass is None or event_pass is None
              else 'passed' if amount_pass and event_pass else 'failed')
    return {
        'schema': 'pulse_meter_case10/v1', 'status': status, 'threshold': str(threshold), 'nodes': wanted,
        'baseline_by_node': {k: {'usd': _dec(v[0]), 'events': v[1]} for k, v in sorted(by_node.items())},
        'baseline': 'read' if baseline is not None else 'missing',
        'event_count': total_events, 'priced_event_count': priced_count, 'priced_usd': _dec(total_usd),
        'unlinked_event_count': len(unlinked), 'unlinked_priced_usd': _dec(unlinked_usd),
        'unpriced_event_count': len(unpriced),
        # 人类请求未声明 WI：不计入两个比例（大鸟 11-21 定），单列
        'undeclared_event_count': len(undeclared), 'undeclared_priced_usd': _dec(Decimal(_priced(undeclared)[1]) / prices.SCALE),
        'unpriced_tokens': sum(sum(r[c] or 0 for _, c in TOKEN_CLASSES) for r in unpriced),
        'gap_unexplained_usd': _dec(gap_usd), 'gap_unexplained_events': gap_events, 'gaps': gaps, 'agents': agents,
        'amount_ratio': None if amount_ratio is None else str(amount_ratio.quantize(Decimal('0.000001'))),
        'amount_ratio_is_lower_bound': lower_bound,
        # 诊断估算，不参与判定（见上）
        'unpriced_estimate_usd': None if upper_usd is None or not lower_bound else _dec(upper_usd),
        'unpriced_estimate_basis': 'max_observed_rate' if lower_bound and upper_usd is not None else None,
        'baseline_at': baseline_at, 'data_until': max((r['occurred_at'] for r in rows), default=None),
        'baseline_after_data': stale_data,
        'event_ratio': None if event_ratio is None else str(event_ratio.quantize(Decimal('0.000001'))),
        'amount_pass': amount_pass, 'event_pass': event_pass,
    }


# ---------------------------------------------------------------- 入口

def _private_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name('.' + path.name + '.pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=1)
    os.replace(temporary, path)


def publish(document: Mapping[str, Any], path: Path, *, allow_compression: bool = False,
            allow_row_contexts: bool = False) -> None:
    """写发布物：私有原子替换；已有可用发布物时不拿不可用的去覆盖。"""
    legacy._withhold_regression(Path(path), document)
    legacy.publish_dashboard(Path(path), document, allow_compression=allow_compression,
                             allow_row_contexts=allow_row_contexts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('publish', help='汇总库 → cost-dashboard-v1.json（及上周、诊断）')
    p.add_argument('--central-db', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--last-week-output', type=Path)
    p.add_argument('--diagnostics-output', type=Path)
    p.add_argument('--source-revision', required=True)
    p.add_argument('--catalog-snapshot', type=Path)
    p.add_argument('--wi-facts', type=Path)
    p.add_argument('--procurement-facts', type=Path, default=legacy.DEFAULT_FACTS)
    p.add_argument('--openrouter-snapshot', type=Path)
    p.add_argument('--pull-request-audit', type=Path)
    p.add_argument('--kernel-merge-enforced-since')
    p.add_argument('--allow-compression', action='store_true')
    p.add_argument('--source-registry', type=Path, help='来源登记（含 workspace_registry_path）：出租户维度')
    p.add_argument('--expected-node', action='append', dest='expected_nodes',
                   help='refresh.json 启用的节点（可重复）；不给则来源覆盖记 unknown')
    for name in ('check', 'diagnostics'):
        c = sub.add_parser(name)
        c.add_argument('--central-db', type=Path, required=True)
        c.add_argument('--month', required=True, help='YYYY-MM（Asia/Shanghai）')
        if name == 'check':
            c.add_argument('--baseline', type=Path, help='切换前另存的旧 cost-dashboard-v1.json')
            c.add_argument('--node', action='append', dest='nodes',
                           help='只对比这些节点（可重复）：新链路事件与基线行都按 node 过滤')
            c.add_argument('--explanations', type=Path, help='{"<runtime>/<model>": {usd, events, reason}}')
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    if args.command == 'publish':
        timings: dict[str, Any] = {}
        common = dict(now=now, source_revision=args.source_revision, catalog_path=args.catalog_snapshot,
                      wi_facts_path=args.wi_facts, procurement_facts=args.procurement_facts,
                      openrouter_snapshot=args.openrouter_snapshot, timings=timings,
                      expected_nodes=args.expected_nodes,
                      agent_tenants=load_tenants(_read_json(args.source_registry)) if args.source_registry else None)
        computed: list = []
        document = build(args.central_db, pull_request_audit=args.pull_request_audit,
                         kernel_merge_enforced_since=args.kernel_merge_enforced_since, events_out=computed, **common)
        publish(document, args.output, allow_compression=args.allow_compression)
        if args.last_week_output:
            try:
                publish(build(args.central_db, last_week=True, **common), args.last_week_output,
                        allow_compression=args.allow_compression)
            except (OSError, ValueError, RuntimeError) as exc:
                timings['last_week'] = 'failed:' + type(exc).__name__
        if args.diagnostics_output and document['status'] == 'available':
            _private_json(args.diagnostics_output, {'generated_at': now.isoformat(),
                                                    **diagnostics(month_rows(computed, now))})
        print(json.dumps({'schema_version': SCHEMA, 'status': document['status'], 'reason': document['reason'],
                          'generated_at': document['generated_at'], 'sha256': document['sha256'], **timings}))
        return 0 if document['status'] == 'available' else 2
    conn = connect(args.central_db)
    try:
        with read_snapshot(conn):   # 事件（含已计价金额）与价表最高单价读同一快照
            rows = events(conn, *_month_window(args.month))
            max_rates = prices.max_rates(conn)
    finally:
        conn.close()
    if args.command == 'diagnostics':
        print(json.dumps(diagnostics(rows), ensure_ascii=False, indent=1))
        return 0
    baseline = _read_json(args.baseline) if args.baseline else None
    if baseline is not None and not isinstance(baseline, dict):
        baseline = None
    try:
        result = anomaly_ratios(rows, baseline=baseline, nodes=args.nodes, max_rates=max_rates,
                                explanations=_read_json(args.explanations) if args.explanations else None)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        result = anomaly_ratios(rows, nodes=args.nodes)   # 基线读不懂：同"缺失"，结果不作数
        result['baseline'] = 'unreadable'
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return {'passed': 0, 'failed': 1}.get(result['status'], 2)


if __name__ == '__main__':
    raise SystemExit(main())
