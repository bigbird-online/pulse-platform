"""pulse-stats command line over the read-only query core (pulse_stats_query/v1)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import re

from .cost_dashboard import LAST_WEEK_NAME
from .query import CLOSED_PERIODS, GROUPS, NAME_PATTERN, PERIODS, QUERIES, WI_PATTERN, Sources, answer, read_snapshot


def account_home() -> Path:
    """Agent sessions may carry an isolated HOME; the App runs under the account home."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def default_app_snapshot(home: Path) -> Path:
    return home / 'Library/Application Support/PulseStats/snapshot-v2.json'


def default_agent_snapshot(home: Path) -> Path:
    return home / '.local/state/pulse/stats/agent-snapshot-v1.json'


def current_snapshot(home: Path) -> tuple[Path, str]:
    """The App's snapshot, or the refresh job's copy when that one is newer.

    The App stays quiet overnight; the refresh job then keeps a snapshot from the
    App's own exporter command so Agent queries do not go stale with the App.
    """
    app, agent = default_app_snapshot(home), default_agent_snapshot(home)
    app_value, _ = read_snapshot(app)
    agent_value, _ = read_snapshot(agent)
    def generated(value):
        return datetime.fromisoformat(value['generated_at'].replace('Z', '+00:00')) if value else None
    if agent_value and (not app_value or generated(agent_value) > generated(app_value)):
        return agent, 'refresh_cache'
    return app, 'app_cache'


def default_cost_publication(home: Path, period: str = 'today') -> Path:
    directory = home / '.local/state/pulse/stats'
    return directory / (LAST_WEEK_NAME if period in CLOSED_PERIODS else 'cost-dashboard-v1.json')


def _token(pattern):
    def parse(value):
        if not pattern.fullmatch(value):
            raise argparse.ArgumentTypeError('invalid value')
        return value
    return parse


def parser():
    p = argparse.ArgumentParser(prog='pulse-stats', description='Read-only PULSE Stats query (pulse_stats_query/v1).')
    p.add_argument('query', choices=QUERIES)
    p.add_argument('--period', choices=PERIODS + CLOSED_PERIODS, default='today',
                   help='last_week: the previous complete Monday..Sunday (Asia/Shanghai)')
    p.add_argument('--group', choices=GROUPS)
    p.add_argument('--agent', type=_token(NAME_PATTERN))
    p.add_argument('--runtime', type=_token(NAME_PATTERN))
    p.add_argument('--work-item-id', dest='work_item_id', type=_token(WI_PATTERN))
    p.add_argument('--metric-id', dest='metric_id', type=_token(re.compile(r'[a-z0-9_.]{1,100}')))
    p.add_argument('--session-id', dest='session_id', type=_token(re.compile(r'[A-Za-z0-9-]{8,64}')),
                   help='compactions: one CC native session ID (hashed before matching)')
    p.add_argument('--snapshot', type=Path,
                   help='absolute exporter snapshot file instead of the App cache; cost then comes from it')
    p.add_argument('--cost-dashboard', type=Path, help='absolute cost publication path')
    p.add_argument('--date', type=_token(re.compile(r'\d{4}-\d{2}-\d{2}')),
                   help='agent_day: the UTC+8 day (YYYY-MM-DD), or the first day of a range')
    p.add_argument('--end-date', dest='end_date', type=_token(re.compile(r'\d{4}-\d{2}-\d{2}')),
                   help='agent_day: last day of a range (inclusive); then one count row per day')
    p.add_argument('--ws', type=_token(re.compile(r'(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]*/?')),
                   help='agent_day: workspace path relative to the Coevolution root; default the registry\'s')
    p.add_argument('--coevolution-root', dest='coevolution_root', type=Path,
                   help='agent_day: absolute Coevolution checkout (default ~/Coevolution)')
    p.add_argument('--wi-facts', dest='wi_facts', type=Path,
                   help='agent_day: absolute WI facts file (default the Stats state file)')
    p.add_argument('--pr-bodies', dest='pr_bodies', type=Path,
                   help='agent_day: absolute GitHub PR cache (default the Stats state file pr-bodies-v1.json)')
    p.add_argument('--kernel', type=Path,
                   help='agent_day: absolute kernel used to decode the ledger (default the installed '
                        'scl-core active/harness.pyz; an agent-harness source directory for development)')
    p.add_argument('--format', choices=('json', 'table'), default='json')
    p.add_argument('--json', action='store_true', help='JSON output (default)')
    return p


def _table(document):
    result = document.get('result')
    lines = ['status: ' + document['status'] + ('  errors: ' + ', '.join(document['errors']) if document['errors'] else '')]
    for name, entry in document['sources'].items():
        lines.append(f"{name}: {entry.get('status')} generated_at={entry.get('generated_at')} stale={entry.get('stale')}")
    rows = ((result or {}).get('groups') or (result or {}).get('rows') or (result or {}).get('days')
            if isinstance(result, dict) else None)
    if rows:
        columns = [c for c in rows[0] if not isinstance(rows[0][c], (dict, list, set))] or list(rows[0])
        lines.append('\t'.join(columns))
        lines.extend('\t'.join(str(r.get(c)) for c in columns) for r in rows)
    else:
        lines.append(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return '\n'.join(lines)


def main(argv=None, *, now=None, home=None):
    args = parser().parse_args(argv)
    for path in (args.snapshot, args.cost_dashboard, args.coevolution_root, args.wi_facts, args.kernel,
                 args.pr_bodies):
        if path is not None and not path.is_absolute():
            parser().error('paths must be absolute')
    home = home or account_home()
    now = now or datetime.now(timezone.utc)
    day = {'home': home, 'coevolution_root': args.coevolution_root or home / 'Coevolution',
           'wi_facts': args.wi_facts or home / '.local/state/pulse/stats/wi-facts-v1.json',
           'pr_bodies': args.pr_bodies or home / '.local/state/pulse/stats/pr-bodies-v1.json',
           'kernel': args.kernel or home / '.local/share/pulse/tools/scl-core/active/harness.pyz',
           'compactions': home / '.local/state/pulse/stats/context-compactions-v1.json'}
    if args.snapshot is not None:
        sources = Sources(app_snapshot=args.snapshot, cost_publication=args.cost_dashboard, now=now, app_kind='file',
                          **day)
    else:
        snapshot, kind = current_snapshot(home)
        sources = Sources(app_snapshot=snapshot, app_kind=kind,
                          cost_publication=args.cost_dashboard or default_cost_publication(home, args.period), now=now,
                          **day)
    document = answer(args, sources)
    if args.format == 'table':
        print(_table(document))
    else:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str))
    return 0
