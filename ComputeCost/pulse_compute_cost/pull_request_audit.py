"""PR output anomalies: GitHub merges the core did not make, direct pushes to main.

Collection reads GitHub only through the controlled ``pulse-gh session``
(one preflight, then read-only repository GETs) and saves what it saw in a
private cache; it runs beside the Stats refresh, at most once per
``REFRESH_SECONDS``. The publisher joins the cache with the core's merge
records (``pull_request_outputs.read_merged``). A repository, page or lookup
that could not be read leaves its count unknown ("unverified"), never zero.

(a) ``unrecorded_merges``: PRs merged into main within the window that no WI's
kernel merge record names, merged on or after the configured enforcement date
(``kernel_merge_enforced_since``); earlier ones, or all of them while no date
is set, are only a reference count (``unenforced_merges``). (b) ``direct_commits``: first-parent, single-parent
commits on main that came from no PR. The core's own ledger commits (``SCL:``
in PULSE) are not changes. A GitHub squash or rebase merge is also a
single-parent commit; its ``(#N)`` title suffix or GitHub's commit-to-PR
association places it under (a) instead.

(c) ``unregistered``: per published period (today, week, month), the merged PRs
of that Asia/Shanghai period without a kernel merge record, beside the period's
kernel PR count. The PR list is read back to the earlier of this month's and
this week's start (``pulls_since``) so each period is covered. A PR whose head
branch starts with ``<agent>/`` for a registered Agent carries that Agent as an
unverified hint; it is never output, attribution or cost.
"""
from __future__ import annotations

from datetime import date, datetime, time as day_start, timedelta, timezone
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import subprocess
import time
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

SCHEMA = 'pulse_stats_pull_request_audit/v1'
REPOSITORIES = ('bigbird-online/PULSE', 'bigbird-online/CollaborateWithAI', 'bigbird-online/Oikos')
LEDGER_REPOSITORY, LEDGER_PREFIX = 'bigbird-online/PULSE', 'SCL:'
WINDOW_DAYS = 7
REFRESH_SECONDS = 3 * 3600
STALE_SECONDS = 12 * 3600
# The enforcement date is a Beijing calendar day.
ENFORCEMENT_ZONE = ZoneInfo("Asia/Shanghai")
PAGE = 100
PULL_PAGES, COMMIT_PAGES, LOOKUPS, BATCH = 10, 60, 60, 8
ITEM_LIMIT = 20
CALL_TIMEOUT = 60
BUDGET_SECONDS = 300
_PR_SUFFIX = re.compile(r'\(#(\d{1,7})\)\s*$')
_SHA = re.compile(r'[0-9a-f]{40}')
_BRANCH = re.compile(r'[A-Za-z0-9._/+-]{1,200}')


def _branch(value: Any) -> str | None:
    return value if isinstance(value, str) and _BRANCH.fullmatch(value) else None


def pulls_start(now: datetime, since: datetime) -> datetime:
    """The earliest instant a published period needs: this month's or this week's start."""
    today = now.astimezone(ENFORCEMENT_ZONE).date()
    first = min(today.replace(day=1), today - timedelta(days=today.weekday()))
    return min(since, datetime.combine(first, day_start.min, ENFORCEMENT_ZONE))


class GitHubUnavailable(OSError):
    pass


def _instant(value: Any) -> datetime:
    moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError('aware timestamp required')
    return moment


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _committed(commit: Mapping[str, Any]) -> str | None:
    date = ((commit.get('commit') or {}).get('committer') or {}).get('date')
    return _utc(_instant(date)) if date else None


def _title(message: Any) -> str:
    """First line, bounded; control characters (tabs and the like) become spaces."""
    first = (str(message or '').strip().splitlines() or [''])[0]
    return ''.join(' ' if ord(ch) < 32 or ord(ch) == 127 else ch for ch in first).strip()[:120]


class Session:
    """One ``pulse-gh session`` for one repository; provider text is never kept."""

    def __init__(self, command: list[str], env: Mapping[str, str]):
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, env=dict(env), cwd='/')
        except OSError as exc:
            raise GitHubUnavailable('GitHub control plane unavailable') from exc
        self.buffer, self.sent = b'', 0
        self.deadline: float | None = None  # Monotonic end of the whole read.

    def get_many(self, paths: list[str]) -> list[Any]:
        """Parsed JSON per path, or a GitHubUnavailable for a refused request."""
        stdin, stdout = self.process.stdin, self.process.stdout
        assert stdin is not None and stdout is not None
        idents = list(range(self.sent, self.sent + len(paths)))
        self.sent += len(paths)
        try:
            for ident, path in zip(idents, paths):
                stdin.write((json.dumps({'id': ident, 'args': ['api', path]}) + '\n').encode())
            stdin.flush()
        except OSError as exc:
            raise GitHubUnavailable('GitHub control plane unavailable') from exc
        answers: dict[int, Any] = {}
        deadline = time.monotonic() + CALL_TIMEOUT
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        with selectors.DefaultSelector() as selector:
            selector.register(stdout.fileno(), selectors.EVENT_READ)
            while len(answers) < len(idents):
                while b'\n' not in self.buffer:
                    left = deadline - time.monotonic()
                    if left <= 0 or not selector.select(left):
                        raise GitHubUnavailable('GitHub control plane timed out')
                    chunk = os.read(stdout.fileno(), 1 << 16)
                    if not chunk:
                        raise GitHubUnavailable('GitHub control plane unavailable')
                    self.buffer += chunk
                line, _, self.buffer = self.buffer.partition(b'\n')
                try:
                    answer = json.loads(line)
                    if answer.get('id') not in idents:
                        raise ValueError('out of turn')
                    answers[answer['id']] = (json.loads(answer.get('stdout') or 'null')
                                             if answer.get('code') == 0
                                             else GitHubUnavailable('GitHub request refused'))
                except (ValueError, AttributeError) as exc:
                    raise GitHubUnavailable('GitHub response unreadable') from exc
        return [answers[i] for i in idents]

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            self.process.kill()
            self.process.wait()


def pulse_gh_session(script: Path, python: str) -> Callable[[str], Session]:
    """Sessions through the repository's own pulse-gh, with the account's HOME."""
    def start(repository: str) -> Session:
        env = {**os.environ, 'HOME': pwd.getpwuid(os.getuid()).pw_dir, 'GH_PROMPT_DISABLED': '1'}
        env['PATH'] = '/opt/homebrew/bin:/usr/local/bin:' + env.get('PATH', '/usr/bin:/bin')
        return Session([python, '-I', '-B', str(script), '--transport', 'local',
                        '--repository', repository, 'session'], env)
    return start


def _pages(session: Session, path: str, limit: int, more: Callable[[list], bool]) -> tuple[list, bool]:
    """Fetch pages in concurrent batches until a short page or ``more`` is false."""
    rows: list = []
    page = 1
    while page <= limit:
        batch = list(range(page, min(page + BATCH, limit + 1)))
        answers = session.get_many([f'{path}&per_page={PAGE}&page={n}' for n in batch])
        for answer in answers:
            if isinstance(answer, Exception) or not isinstance(answer, list):
                raise GitHubUnavailable('GitHub page unreadable')
            rows.extend(answer)
            if len(answer) < PAGE or not more(answer):
                return rows, True
        page = batch[-1] + 1
    return rows, False


def collect_repository(session: Session, repository: str, since: datetime,
                       pulls_since: datetime | None = None) -> dict[str, Any]:
    stamp, pulls_since = _utc(since), min(since, pulls_since or since)
    result: dict[str, Any] = {'repository': repository, 'merges': 'unverified', 'commits': 'unverified',
                              'reason': None, 'merged_pull_requests': [], 'pr_commits': [],
                              'direct_commits': []}
    try:
        pulls, complete = _pages(session, f'repos/{repository}/pulls?state=closed&base=main'
                                 '&sort=updated&direction=desc', PULL_PAGES,
                                 lambda page: _instant(page[-1]['updated_at']) >= pulls_since)
        result['merged_pull_requests'] = sorted((
            {'number': p['number'], 'merged_at': _utc(_instant(p['merged_at'])),
             'merge_commit': p.get('merge_commit_sha'), 'title': _title(p.get('title')),
             'head_ref': _branch((p.get('head') or {}).get('ref'))}
            for p in pulls if p.get('merged_at') and _instant(p['merged_at']) >= pulls_since
            and (p.get('base') or {}).get('ref') == 'main'), key=lambda r: r['number'])
        result['merges'] = 'read' if complete else 'unverified'
        result['reason'] = None if complete else 'pull_request_pages_exceeded'
        commits, complete = _pages(session, f'repos/{repository}/commits?sha=main&since={stamp}',
                                   COMMIT_PAGES, lambda page: True)
        if not complete:
            result['reason'] = 'commit_pages_exceeded'
            return result
        known = {c['sha']: c for c in commits}
        candidates, sha = [], commits[0]['sha'] if commits else None
        while sha in known:
            commit = known[sha]
            parents = [p['sha'] for p in commit.get('parents') or []]
            message = (commit.get('commit') or {}).get('message') or ''
            if len(parents) == 1 and not (repository == LEDGER_REPOSITORY and message.startswith(LEDGER_PREFIX)):
                candidates.append(commit)
            sha = parents[0] if parents else None
        lookups = []
        for commit in candidates:
            title = _title((commit.get('commit') or {}).get('message'))
            suffix = _PR_SUFFIX.search(title)
            if suffix:
                result['pr_commits'].append({'sha': commit['sha'], 'number': int(suffix.group(1)), 'committed_at': _committed(commit)})
            else:
                lookups.append(commit)
        if len(lookups) > LOOKUPS:
            result['reason'] = 'commit_lookups_exceeded'
            return result
        answers = session.get_many([f'repos/{repository}/commits/{c["sha"]}/pulls' for c in lookups]) if lookups else []
        for commit, answer in zip(lookups, answers):
            if isinstance(answer, Exception) or not isinstance(answer, list):
                raise GitHubUnavailable('GitHub commit association unreadable')
            merged = [p for p in answer if p.get('merged_at') and (p.get('base') or {}).get('ref') == 'main']
            if merged:
                result['pr_commits'].append({'sha': commit['sha'], 'number': merged[0]['number'], 'committed_at': _committed(commit)})
                continue
            data = commit.get('commit') or {}
            result['direct_commits'].append({
                'sha': commit['sha'], 'committed_at': _utc(_instant((data.get('committer') or {})['date'])),
                'title': _title(data.get('message')), 'author': (commit.get('author') or {}).get('login')})
        result['commits'] = 'read'
    except (GitHubUnavailable, KeyError, TypeError, ValueError, IndexError):
        result['reason'] = result['reason'] or 'github_unavailable'
        if result['merges'] != 'read':
            result['merged_pull_requests'] = []
        result['pr_commits'], result['direct_commits'] = [], []
        result['commits'] = 'unverified'
    return result


def collect(start: Callable[[str], Session], now: datetime, *,
            repositories=REPOSITORIES, window_days: int = WINDOW_DAYS) -> dict[str, Any]:
    since = now - timedelta(days=window_days)
    pulls_since = pulls_start(now, since)
    rows = []
    # One budget for the whole read: a slow GitHub cannot hold the refresh cycle.
    deadline = time.monotonic() + BUDGET_SECONDS
    for repository in repositories:
        try:
            if time.monotonic() >= deadline:
                raise GitHubUnavailable('GitHub read budget spent')
            session = start(repository)
            session.deadline = deadline
        except GitHubUnavailable:
            rows.append({'repository': repository, 'merges': 'unverified', 'commits': 'unverified',
                         'reason': 'github_unavailable', 'merged_pull_requests': [], 'pr_commits': [],
                         'direct_commits': []})
            continue
        try:
            rows.append(collect_repository(session, repository, since, pulls_since))
        finally:
            session.close()
    return {'schema': SCHEMA, 'observed_at': _utc(now), 'since': _utc(since),
            'pulls_since': _utc(pulls_since), 'window_days': window_days, 'repositories': rows}


def due(path: Path, now: datetime) -> bool:
    """Whether the cache is missing, unreadable or older than the refresh interval."""
    try:
        observed = _instant(json.loads(path.read_bytes())['observed_at'])
    except (OSError, ValueError, KeyError, TypeError):
        return True
    return not timedelta(0) <= now - observed < timedelta(seconds=REFRESH_SECONDS)


def load(path: Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    try:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            return None
        document = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return document if isinstance(document, Mapping) and document.get('schema') == SCHEMA else None


def enforcement_start(value: Any) -> datetime | None:
    """``kernel_merge_enforced_since`` (ISO date): its first Asia/Shanghai instant, or None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('kernel_merge_enforced_since must be an ISO date')
    return datetime.combine(date.fromisoformat(value), datetime.min.time(), ENFORCEMENT_ZONE)


def derive(audit: Mapping[str, Any] | None, merged: Mapping[str, Any], now: datetime,
           enforced_since: str | None = None) -> dict[str, Any]:
    """The two anomaly lists for the dashboard; unknown counts stay None.

    A GitHub merge without a kernel record is an anomaly only when merged on or
    after ``enforced_since``; before it, or with no date configured, the gate
    was not yet enforced and the merge is a reference count
    (``unenforced_merges``). Direct pushes to main are always anomalies.
    """
    empty = {'count': None, 'items': []}
    result: dict[str, Any] = {'status': 'unverified', 'reason': 'audit_not_collected', 'observed_at': None,
                              'since': None, 'window_days': None, 'enforced_since': None, 'repositories': [],
                              'unrecorded_merges': dict(empty), 'unenforced_merges': dict(empty),
                              'direct_commits': dict(empty)}
    try:
        start = enforcement_start(enforced_since)
    except ValueError:
        return {**result, 'reason': 'enforcement_date_invalid'}
    result['enforced_since'] = enforced_since
    try:
        if audit is None:
            return result
        observed = _instant(audit['observed_at'])
        result.update(observed_at=_utc(observed), since=_utc(_instant(audit['since'])),
                      window_days=int(audit['window_days']))
        if not timedelta(0) <= now - observed < timedelta(seconds=STALE_SECONDS):
            return {**result, 'reason': 'audit_stale'}
        recorded = {(r['repository'], r['number']) for rows in merged['by_wi'].values() for r in rows}
        merges, commits, repositories = [], [], []
        merges_known = merged['status'] == 'complete'
        commits_known = True
        window = _instant(audit['since'])
        for row in audit['repositories']:
            repo = row['repository']
            repositories.append({k: row[k] for k in ('repository', 'merges', 'commits', 'reason')})
            if row['merges'] != 'read' or row['commits'] != 'read':
                merges_known = merges_known and row['merges'] == 'read'
                commits_known = commits_known and row['commits'] == 'read'
            # The PR list reaches back further for the periods; anomalies keep the window.
            by_number = {p['number']: {k: p.get(k) for k in ('number', 'merged_at', 'merge_commit', 'title')}
                         for p in row['merged_pull_requests'] if _instant(p['merged_at']) >= window}
            for c in row['pr_commits']:
                by_number.setdefault(c['number'], {'number': c['number'], 'merged_at': c.get('committed_at'),
                                                   'merge_commit': c['sha'], 'title': None})
            merges += [{'repository': repo, **p} for n, p in sorted(by_number.items())
                       if (repo, n) not in recorded]
            commits += [{'repository': repo, **c} for c in row['direct_commits']]
        merges.sort(key=lambda r: (r['merged_at'] or '', r['repository'], r['number']), reverse=True)
        commits.sort(key=lambda r: (r['committed_at'], r['repository'], r['sha']), reverse=True)
        # A merge whose time is unknown cannot be shown to precede enforcement.
        enforced = [m for m in merges if start is not None
                    and (m['merged_at'] is None or _instant(m['merged_at']) >= start)]
        reference = [m for m in merges if m not in enforced]
        # Without every WI's merge records, a GitHub merge cannot be called unrecorded.
        listed = merged['status'] == 'complete'
        result.update(
            status='verified' if merges_known and commits_known else 'partial',
            reason=None if merges_known and commits_known else (
                'core_pull_requests_unverified' if merged['status'] != 'complete' else 'github_partially_read'),
            repositories=repositories,
            unrecorded_merges={'count': len(enforced) if merges_known else None,
                               'items': enforced[:ITEM_LIMIT] if listed else []},
            unenforced_merges={'count': len(reference) if merges_known else None,
                               'items': reference[:ITEM_LIMIT] if listed else []},
            direct_commits={'count': len(commits) if commits_known else None, 'items': commits[:ITEM_LIMIT]})
        return result
    except (KeyError, TypeError, ValueError):
        return {**result, 'status': 'unverified', 'reason': 'audit_invalid', 'repositories': [],
                'unrecorded_merges': dict(empty), 'unenforced_merges': dict(empty),
                'direct_commits': dict(empty)}


def unregistered(audit: Mapping[str, Any] | None, merged: Mapping[str, Any], now: datetime,
                 windows: Mapping[str, tuple[date, date]], agents: set[str] | frozenset[str]) -> dict[str, Any]:
    """Per period: merged PRs without a kernel merge record, with branch-prefix Agent hints.

    The count is known only when the cache is fresh, every repository's PR list
    and every WI's kernel merge records were read, and the list reaches back to
    the period's start; otherwise it is None with a reason, never zero.
    """
    def unknown(reason: str) -> dict[str, Any]:
        return {p: {'from_date': s.isoformat(), 'to_date': e.isoformat(), 'status': 'unverified',
                    'reason': reason, 'count': None, 'agents': [], 'unhinted_count': None, 'items': []}
                for p, (s, e) in windows.items()}
    if audit is None:
        return unknown('audit_not_collected')
    try:
        if not timedelta(0) <= now - _instant(audit['observed_at']) < timedelta(seconds=STALE_SECONDS):
            return unknown('audit_stale')
        if merged['status'] != 'complete':
            return unknown('core_pull_requests_unverified')
        if any(row['merges'] != 'read' for row in audit['repositories']):
            return unknown('github_partially_read')
        covered = _instant(audit.get('pulls_since') or audit['since'])
        recorded = {(r['repository'], r['number']) for rows in merged['by_wi'].values() for r in rows}
        found = []
        for row in audit['repositories']:
            for p in row['merged_pull_requests']:
                if (row['repository'], p['number']) in recorded:
                    continue
                branch = _branch(p.get('head_ref'))
                prefix = branch.split('/', 1)[0] if branch and '/' in branch else None
                found.append({'repository': row['repository'], 'number': p['number'],
                              'merged_at': _utc(_instant(p['merged_at'])), 'title': p.get('title'),
                              'head_ref': branch, 'agent_hint': prefix if prefix in agents else None})
        found.sort(key=lambda r: (r['merged_at'], r['repository'], r['number']), reverse=True)
        result = unknown('audit_window_short')
        for period, (start, end) in windows.items():
            if datetime.combine(start, day_start.min, ENFORCEMENT_ZONE) < covered:
                continue
            rows = [r for r in found if start <= _instant(r['merged_at']).astimezone(ENFORCEMENT_ZONE).date() < end]
            counts: dict[str, int] = {}
            for r in rows:
                if r['agent_hint']:
                    counts[r['agent_hint']] = counts.get(r['agent_hint'], 0) + 1
            result[period].update(
                status='verified', reason=None, count=len(rows),
                agents=[{'agent': a, 'count': n} for a, n in sorted(counts.items(), key=lambda i: (-i[1], i[0]))],
                unhinted_count=len(rows) - sum(counts.values()), items=rows[:ITEM_LIMIT])
        return result
    except (KeyError, TypeError, ValueError):
        return unknown('audit_invalid')
