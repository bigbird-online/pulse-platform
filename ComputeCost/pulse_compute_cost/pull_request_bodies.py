"""PR text for Stats' ``agent_day`` query: title, body and open/close times of PRs into main.

Collection reuses ``pull_request_audit``'s controlled ``pulse-gh session`` (read-only
repository GETs) and runs in the Stats refresh's GitHub thread after the audit, at most
once per ``REFRESH_SECONDS``. Each read lists ``pulls?state=all&base=main`` by update time,
newest first, back to the previous read less ``OVERLAP`` (``BACKFILL_DAYS`` on the first
read), and merges what it saw into the private cache, so history accumulates past one
read. Per repository, ``covered_since`` is the earliest instant from which every PR
updated up to ``observed_at`` is in the cache: a PR with any event in that span was
updated then, so it was listed. A repository that could not be read keeps its previous
entries and coverage; one read only part way covers from its oldest listed update.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from .pull_request_audit import (BUDGET_SECONDS, ENFORCEMENT_ZONE, PAGE, REPOSITORIES, GitHubUnavailable,
                                 Session, _branch, _instant, _utc)

SCHEMA = 'pulse_stats_pull_request_bodies/v1'
REFRESH_SECONDS = 3 * 3600
BACKFILL_DAYS = 120
RETAIN_DAYS = 400
OVERLAP = timedelta(hours=1)
PAGES = 30
BATCH = 4
BODY_LIMIT = 8000
TITLE_LIMIT = 300
LOGIN_LIMIT = 100


def _bounded(value: Any, limit: int) -> tuple[str | None, bool]:
    if not isinstance(value, str):
        return None, False
    value = value.replace('\r\n', '\n').replace('\x00', '')
    return (value, False) if len(value) <= limit else (value[:limit], True)


def _stamp(value: Any) -> str | None:
    return _utc(_instant(value)) if value else None


def entry(pull: Mapping[str, Any]) -> dict[str, Any]:
    """The kept fields of one GitHub PR; a malformed one raises (the page is unreadable)."""
    body, truncated = _bounded(pull.get('body'), BODY_LIMIT)
    login, _ = _bounded((pull.get('user') or {}).get('login'), LOGIN_LIMIT)
    title, _ = _bounded(pull.get('title'), TITLE_LIMIT)
    return {'number': int(pull['number']), 'state': str(pull['state']), 'draft': pull.get('draft') is True,
            'created_at': _stamp(pull['created_at']), 'updated_at': _stamp(pull['updated_at']),
            'closed_at': _stamp(pull.get('closed_at')), 'merged_at': _stamp(pull.get('merged_at')),
            'merge_commit': pull.get('merge_commit_sha') if pull.get('merged_at') else None,
            'title': title, 'body': body, 'body_truncated': truncated, 'author': login,
            'head_ref': _branch((pull.get('head') or {}).get('ref'))}


def read_repository(session: Session, repository: str, since: datetime) -> tuple[list[dict[str, Any]], datetime]:
    """PRs updated since ``since`` and the instant the listing is complete from.

    Complete back to ``since`` when a short page or an older update ends the listing;
    otherwise (page limit) only back to the oldest update it saw.
    """
    path = f'repos/{repository}/pulls?state=all&base=main&sort=updated&direction=desc'
    rows: list[dict[str, Any]] = []
    page = 1
    while page <= PAGES:
        batch = list(range(page, min(page + BATCH, PAGES + 1)))
        for answer in session.get_many([f'{path}&per_page={PAGE}&page={n}' for n in batch]):
            if isinstance(answer, Exception) or not isinstance(answer, list):
                raise GitHubUnavailable('GitHub page unreadable')
            rows.extend(entry(p) for p in answer)
            if len(answer) < PAGE or (answer and _instant(answer[-1]['updated_at']) < since):
                return [r for r in rows if _instant(r['updated_at']) >= since], since
        page = batch[-1] + 1
    oldest = min((_instant(r['updated_at']) for r in rows), default=None)
    if oldest is None:
        raise GitHubUnavailable('GitHub listing empty past its page limit')
    return rows, max(oldest, since)


def _previous(previous: Mapping[str, Any] | None, repository: str) -> dict[str, Any] | None:
    for row in (previous or {}).get('repositories') or []:
        if isinstance(row, dict) and row.get('repository') == repository:
            try:
                _instant(row['observed_at']), _instant(row['covered_since'])
            except (KeyError, TypeError, ValueError):
                return None
            return row
    return None


def merge_repository(old: Mapping[str, Any] | None, repository: str, now: datetime,
                     read: tuple[list[dict[str, Any]], datetime] | None, reason: str | None) -> dict[str, Any]:
    """The cache row after one read attempt (``read`` None: nothing read)."""
    cutoff = now - timedelta(days=RETAIN_DAYS)
    pulls = {p['number']: p for p in (old or {}).get('pull_requests') or []}
    observed = _instant(old['observed_at']) if old else None
    covered = _instant(old['covered_since']) if old else None
    if read is not None:
        rows, read_from = read
        for row in rows:
            known = pulls.get(row['number'])
            if known is None or _instant(known['updated_at']) <= _instant(row['updated_at']):
                pulls[row['number']] = row
        # Contiguous with the previous coverage when that read reached past this one's start.
        covered = covered if observed is not None and covered is not None and observed >= read_from else read_from
        observed, reason = now, reason
    elif old is None:
        return {'repository': repository, 'status': 'unverified', 'reason': reason, 'observed_at': None,
                'covered_since': None, 'pull_requests': []}
    kept = sorted((p for p in pulls.values() if _instant(p['updated_at']) >= cutoff), key=lambda p: p['number'])
    return {'repository': repository, 'status': 'read' if read is not None and reason is None else 'unverified',
            'reason': reason, 'observed_at': _utc(observed), 'covered_since': _utc(max(covered, cutoff)),
            'pull_requests': kept}


def collect(start: Callable[[str], Session], now: datetime, previous: Mapping[str, Any] | None = None, *,
            repositories=REPOSITORIES) -> dict[str, Any]:
    deadline = time.monotonic() + BUDGET_SECONDS
    rows = []
    for repository in repositories:
        old = _previous(previous, repository)
        since = now - timedelta(days=BACKFILL_DAYS)
        if old is not None:
            since = max(since, _instant(old['observed_at']) - OVERLAP)
        read, reason, session = None, None, None
        try:
            if time.monotonic() >= deadline:
                raise GitHubUnavailable('GitHub read budget spent')
            session = start(repository)
            session.deadline = deadline
            read = read_repository(session, repository, since)
            if read[1] > since:
                reason = 'pull_request_pages_exceeded'
        except (GitHubUnavailable, KeyError, TypeError, ValueError):
            read, reason = None, 'github_unavailable'
        finally:
            if session is not None:
                session.close()
        rows.append(merge_repository(old, repository, now, read, reason))
    return {'schema': SCHEMA, 'observed_at': _utc(now), 'repositories': rows}


def due(path: Path, now: datetime) -> bool:
    """Whether the cache is missing, unreadable, older than the refresh interval, or read before
    today's UTC+8 midnight, so yesterday's agent_day completes on the first refresh after it."""
    try:
        observed = _instant(json.loads(path.read_bytes())['observed_at'])
    except (OSError, ValueError, KeyError, TypeError):
        return True
    midnight = now.astimezone(ENFORCEMENT_ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    return not timedelta(0) <= now - observed < timedelta(seconds=REFRESH_SECONDS) or observed < midnight


def load(path: Path | None) -> Mapping[str, Any] | None:
    """The private cache, or None when absent, shared with others, a symlink or another schema."""
    if path is None:
        return None
    try:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            return None
        document = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return document if isinstance(document, Mapping) and document.get('schema') == SCHEMA else None

