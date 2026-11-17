"""One Agent's landed work per UTC+8 calendar day (pulse_stats ``agent_day``).

Read-only and local. The query never fetches, never calls GitHub and never
runs the kernel CLI; it reads what is already synced:

- Git: ``origin/main`` of the CollaborateWithAI, PULSE and Oikos checkouts
  under the Coevolution root, as of their last ``origin/main`` reflog entry or
  the later four-end-sync fetch stamp that vouches for the same commit.
  An Agent's commits are those touching its canonical workspace or any execution
  surface directory its surface registry has ever named (``surface_dirs``).
- Kernel ledger: the WorkItem journals (``.harness/core/journals``), pull
  request claims (``.harness/core/pull-requests``) and imported production
  history (``.harness/events/*.events.yaml``) on PULSE ``origin/main``.
- WI facts: Stats' ``wi-facts-v1.json`` (owners and titles). It only narrows
  which journals are read; a WorkItem missing from it is read anyway.
- GitHub PR cache: Stats' ``pr-bodies-v1.json`` (``ComputeCost/pulse_compute_cost/
  pull_request_bodies.py``, written by the refresh): PR bodies, open and close times.

A source covers a window only up to its sync time. A section whose sources do
not cover the whole window is ``incomplete``; an unreadable source makes its
section ``unavailable``. Neither is ever reported as zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable

from . import wi_facts

SCHEMA = 'pulse_stats_agent_day/v1'
ZONE = timezone(timedelta(hours=8), 'UTC+8')
MAX_DAYS = 120
TEXT_LIMIT = 2000
GIT_TIMEOUT = 40
# (name, GitHub repository, path of the checkout under the Coevolution root)
REPOSITORIES = (('CollaborateWithAI', 'bigbird-online/CollaborateWithAI', ''),
                ('PULSE', 'bigbird-online/PULSE', 'Bigbird/PULSE/'),
                ('Oikos', 'bigbird-online/Oikos', 'Bigbird/Oikos/'))
REPOSITORY_PATH = {name: rel for name, _, rel in REPOSITORIES}
REF = 'origin/main'
TRACKING = 'refs/remotes/origin/main'
# Written by four-end-sync (CollaborateWithAI ssot/skills/ops/four-end-sync) after every
# successful fetch, under the shared checkout's Git directory.
SYNC_STAMP = 'four-end-sync/origin-main.json'
SYNC_STAMP_SCHEMA = 'four_end_sync_fetch_stamp/v1'
LEDGER_SUBJECT = re.compile(r'^(SCL:|audit\(SCL\)|chore\(harness\)|chore\(broker\))')
WI_ID = re.compile(r'wi_\d{8}_\d{3}')
MERGE_SUBJECT = re.compile(r'^Merge pull request #(\d+) from (\S+)')
SQUASH_SUFFIX = re.compile(r'\s*\(#(\d+)\)\s*$')
WS_PATTERN = re.compile(r'(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]*/?')
# Read beside an Agent's surface registry: PULSE's workspace checkout registry names the
# same execution surfaces by directory.
SURFACE_COMPANIONS = {('bigbird-online/PULSE', 'config/scl-surfaces.yaml'): ('config/pulse-workspace-checkouts.yaml',)}
PR_BODIES_SCHEMA = 'pulse_stats_pull_request_bodies/v1'  # pull_request_bodies.SCHEMA

# Kernel bookkeeping and access plumbing; counted per WorkItem, never listed.
NOISE = frozenset({
    'DiskUsageObserved', 'EffectRequested', 'EffectAttemptStarted', 'EffectObserved', 'EffectCancelled',
    'EffectRetryRequested', 'SessionContinued', 'AccessGranted', 'AccessRevoked', 'ScopeGranted',
    'ScopeRevoked', 'UsageIntervalOpened', 'UsageIntervalClosed', 'MeasurementRecorded',
    'WorkspaceRequested', 'WorkspaceRetirementRequested', 'WorkspaceReplaced', 'SourceCaptured',
    'PullRequestReviewReceived', 'ProductionHistoryObserved',
    # Production history plumbing.
    'work_item.native_journal_observed', 'scan_skipped',
})
CATEGORY = {
    'WorkItemCreated': 'definition', 'DefinitionRevised': 'definition', 'WorkRenamed': 'definition',
    'RequirementAdded': 'definition', 'AffiliationRecorded': 'definition', 'AffiliationRevised': 'definition',
    'PlanAdopted': 'definition', 'PlanRevised': 'definition', 'ChildCreated': 'definition',
    'WorkflowPromoted': 'definition', 'HistoricalScopeDeclared': 'definition', 'NarrativeRecorded': 'record',
    'WorkStarted': 'work', 'TaskClaimed': 'work', 'TaskResultRecorded': 'work', 'TaskAbandoned': 'work',
    'ExistingExecutionContinued': 'work', 'ControlNodeRecorded': 'work', 'ControlRoundAdvanced': 'work',
    'WorkRecorded': 'record', 'CheckpointRecorded': 'checkpoint',
    'CollaborationOffered': 'contract', 'CollaborationResponded': 'contract',
    'CollaborationWithdrawn': 'contract', 'CollaborationReassigned': 'contract',
    'TransferReserved': 'transfer', 'TransferAccepted': 'transfer',
    'PullRequestSubmitted': 'pull_request', 'PullRequestReviewed': 'pull_request',
    'PullRequestMergeConfirmed': 'pull_request', 'PullRequestWithdrawn': 'pull_request',
    'PullRequestMerged': 'pull_request',
    'CandidateSubmitted': 'verification', 'VerificationAssigned': 'verification',
    'VerificationAcknowledged': 'verification', 'VerificationRecorded': 'verification',
    'ReviewRecorded': 'verification', 'ChildReviewed': 'verification',
    'WorkCompleted': 'completed', 'WorkTerminated': 'terminated',
    'HumanControlAssumed': 'human_decision',
}
LEGACY_CATEGORY = (('work_item.created', 'definition'), ('work_item.completed', 'completed'),
                   ('work_item.outcome_accepted', 'completed'), ('work_item.cancelled', 'terminated'),
                   ('work_item.superseded', 'terminated'), ('work_item.failed', 'terminated'),
                   ('execution.checkpointed', 'checkpoint'), ('handoff.', 'contract'),
                   ('continuation.', 'transfer'), ('work_item.review_', 'verification'),
                   ('work_item.awaiting_verification', 'verification'), ('work_item.', 'work'),
                   ('execution.', 'work'))


class DayQueryError(ValueError):
    """An argument the query cannot answer; the message is a fixed code."""


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(ZONE).isoformat()


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    return value if len(value) <= TEXT_LIMIT else value[:TEXT_LIMIT] + '…[truncated]'


def window(first: str, last: str | None) -> tuple[date, date, datetime, datetime]:
    try:
        start_day = date.fromisoformat(first)
        end_day = date.fromisoformat(last) if last else start_day
    except (TypeError, ValueError):
        raise DayQueryError('invalid_date') from None
    if end_day < start_day:
        raise DayQueryError('end_date_before_date')
    if (end_day - start_day).days + 1 > MAX_DAYS:
        raise DayQueryError('date_range_too_long')
    start = datetime.combine(start_day, datetime.min.time(), ZONE)
    return start_day, end_day, start, start + timedelta(days=(end_day - start_day).days + 1)


def ledger_commit(subject: str) -> bool:
    return bool(LEDGER_SUBJECT.match(subject))


def git_env(home: Path) -> dict[str, str]:
    """A fixed environment: nothing of the caller's shell reaches git."""
    return {'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin', 'HOME': str(home),
            'LANG': 'en_US.UTF-8', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0',
            'GIT_CONFIG_NOSYSTEM': '1'}


class GitUnavailable(OSError):
    pass


class Repo:
    """One read-only checkout, read at ``origin/main`` only."""

    def __init__(self, name: str, slug: str, path: Path, env: dict[str, str]):
        self.name, self.slug, self.path, self.env = name, slug, path, env
        # Every read uses this one commit, fixed by state(); never a moving ref name.
        self.ref: str | None = None
        self._batch: subprocess.Popen | None = None
        self._graph: dict[str, tuple[int, tuple[str, ...]]] | None = None
        self._landing: dict[str, str] | None = None

    def git(self, *args: str) -> str:
        try:
            out = subprocess.run(['git', '-C', str(self.path), '-c', 'core.quotepath=off', *args],
                                 capture_output=True, env=self.env, timeout=GIT_TIMEOUT, cwd='/')
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitUnavailable('git_failed') from exc
        if out.returncode != 0:
            raise GitUnavailable('git_failed')
        return out.stdout.decode('utf-8', 'replace')

    def blob(self, spec: str) -> bytes | None:
        """One object through a persistent ``cat-file --batch``; None when absent."""
        if self._batch is None:
            try:
                self._batch = subprocess.Popen(['git', '-C', str(self.path), 'cat-file', '--batch'],
                                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                               stderr=subprocess.DEVNULL, env=self.env, cwd='/')
            except OSError as exc:
                raise GitUnavailable('git_failed') from exc
        stdin, stdout = self._batch.stdin, self._batch.stdout
        assert stdin is not None and stdout is not None
        try:
            stdin.write(spec.encode() + b'\n')
            stdin.flush()
            header = stdout.readline().split()
        except OSError as exc:
            raise GitUnavailable('git_failed') from exc
        if len(header) < 3 or header[1] == b'missing':
            return None
        data = stdout.read(int(header[2]))
        stdout.read(1)
        return data

    def close(self) -> None:
        if self._batch is not None:
            try:
                if self._batch.stdin:
                    self._batch.stdin.close()
                self._batch.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                self._batch.kill()
                self._batch.wait()
            if self._batch.stdout:
                self._batch.stdout.close()
            self._batch = None

    def state(self) -> dict[str, Any]:
        """The ``origin/main`` commit every later read uses, and when it was last brought in.

        The sync time is the newest reflog entry of ``refs/remotes/origin/main``, taken
        together with the commit it set; that commit is what gets read. ``FETCH_HEAD``
        is not evidence: fetching any other remote or ref rewrites it. A fetch that found
        nothing new leaves no entry; the four-end-sync stamp covers it: when it names the
        reflog's commit or the current one and is newer, its time and commit are used.
        Either way the time is a lower bound, never too late. Neither: the commit is
        still read, the sync time is unknown.
        """
        line = self.git('reflog', 'show', '-n', '1', '--date=unix', '--format=%H %gd', TRACKING,
                        '--').strip()
        hit = re.fullmatch(r'([0-9a-f]{40}) \S*@\{(\d+)\}', line)
        current = self.git('rev-parse', '--verify', TRACKING + '^{commit}').strip()
        tip, synced, evidence = current, None, None
        if hit:
            tip, synced, evidence = hit.group(1), datetime.fromtimestamp(int(hit.group(2)), timezone.utc), 'reflog'
        stamp = self.sync_stamp()
        if stamp and stamp[0] in (tip, current) and (synced is None or stamp[1] > synced):
            (tip, synced), evidence = stamp, 'fetch_stamp'
        self.ref = tip
        tip_at = int(self.git('show', '-s', '--format=%ct', tip).strip())
        return {'tip': tip, 'tip_committed_at': _iso(datetime.fromtimestamp(tip_at, timezone.utc)),
                'synced_at': synced, 'sync_evidence': evidence, 'moved_since_read': current != tip}

    def sync_stamp(self) -> tuple[str, datetime] | None:
        """four-end-sync's last fetch: the origin/main it left and when the fetch began."""
        try:
            git_dir = self.git('rev-parse', '--absolute-git-dir').strip()
            stamp = json.loads((Path(git_dir) / SYNC_STAMP).read_text(encoding='utf-8'))
            sha, at = stamp['sha'], datetime.fromisoformat(stamp['synced_at'])
        except (OSError, ValueError, TypeError, KeyError):
            return None
        if (stamp.get('schema') != SYNC_STAMP_SCHEMA or stamp.get('ref') != TRACKING
                or not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha) or at.tzinfo is None):
            return None
        return sha, at.astimezone(timezone.utc)

    def graph(self) -> dict[str, tuple[int, tuple[str, ...]]]:
        if self._graph is None:
            graph = {}
            for line in self.git('log', '--format=%H %ct %P', self.ref).splitlines():
                parts = line.split()
                graph[parts[0]] = (int(parts[1]), tuple(parts[2:]))
            self._graph = graph
        return self._graph

    def landing(self) -> dict[str, str]:
        """Each commit's first-parent ``origin/main`` commit that brought it in.

        Mainline commits land as themselves; a branch commit lands with the
        first mainline merge that reaches it. Root commits have no parent.
        """
        if self._landing is None:
            graph = self.graph()
            line, sha = [], self.ref
            while sha in graph:
                line.append(sha)
                parents = graph[sha][1]
                sha = parents[0] if parents else None
            landing: dict[str, str] = {}
            for main in reversed(line):
                stack = [main]
                while stack:
                    commit = stack.pop()
                    if commit in landing or commit not in graph:
                        continue
                    landing[commit] = main
                    stack.extend(graph[commit][1])
            self._landing = landing
        return self._landing

    def records(self, *args: str) -> list[dict[str, Any]]:
        """Commits as dicts; one git call with unit and record separators."""
        out = self.git('log', '--format=%H%x1f%P%x1f%at%x1f%ct%x1f%an%x1f%B%x1e', *args)
        rows = []
        for chunk in out.split('\x1e'):
            chunk = chunk.strip('\n')
            if not chunk:
                continue
            sha, parents, authored, committed, author, message = chunk.split('\x1f', 5)
            lines = message.strip().splitlines()
            rows.append({'sha': sha, 'parents': tuple(parents.split()),
                         'authored_at': datetime.fromtimestamp(int(authored), timezone.utc),
                         'committed_at': datetime.fromtimestamp(int(committed), timezone.utc),
                         'author': author, 'subject': lines[0] if lines else '',
                         'body': '\n'.join(l for l in lines[1:] if not l.lower().startswith('co-authored-by:')).strip()})
        return rows


def pull_request_of(commit: dict[str, Any]) -> dict[str, Any] | None:
    """The PR a mainline commit lands: a GitHub merge commit or a squash ``(#N)``."""
    hit = MERGE_SUBJECT.match(commit['subject'])
    if hit and len(commit['parents']) > 1:
        title = next((l.strip() for l in commit['body'].splitlines() if l.strip()), None)
        branch = hit.group(2).split('/', 1)[-1] if '/' in hit.group(2) else hit.group(2)
        return {'number': int(hit.group(1)), 'title': title, 'head_branch': branch, 'method': 'merge'}
    hit = SQUASH_SUFFIX.search(commit['subject'])
    if hit and len(commit['parents']) == 1:
        return {'number': int(hit.group(1)), 'title': SQUASH_SUFFIX.sub('', commit['subject']),
                'head_branch': None, 'method': 'squash'}
    return None


def _wis(*texts: str | None) -> set[str]:
    return {wi for text in texts if text for wi in WI_ID.findall(text)}


def kernel_key(slug: str, number: int) -> str:
    return f'{slug.lower()}#{number}'


def key_digest(key: str) -> str:
    """The kernel's claim prefix for one repository#number (``model.digest``)."""
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()


class CodecUnavailable(ValueError):
    pass


def kernel_segments(source: Path):
    """The kernel's own ``production_segments`` (decision decoding and verification).

    ``source`` is the installed sealed ``.../active/harness.pyz`` (checked like
    ComputeCost's installed authority) or, for development and tests, an
    ``agent-harness`` source directory. Nothing is reimplemented here.
    """
    try:
        if source.is_dir():
            root = source.resolve(strict=True)
        else:
            if source.name != 'harness.pyz' or source.parent.name != 'active':
                raise ValueError('entry')
            root = source.resolve(strict=True)
            if root.parent.parent != source.parent.parent.resolve(strict=True) / 'releases':
                raise ValueError('release')
            receipt = json.loads((root.parent / 'installation.json').read_bytes())
            if hashlib.sha256(root.read_bytes()).hexdigest() != receipt['bundle_sha256']:
                raise ValueError('sealed program')
        prefix = str(root) + '/'
        for name, module in tuple(sys.modules.items()):
            if (name == 'harness_rewrite' or name.startswith('harness_rewrite.')) and not str(
                    getattr(module, '__file__', '')).startswith(prefix):
                raise ValueError('foreign core')
        sys.path.insert(0, str(root))
        try:
            return importlib.import_module('harness_rewrite.production_segments')
        finally:
            sys.path.remove(str(root))
    except Exception as exc:  # noqa: BLE001 - any failure leaves the ledger unreadable, never guessed.
        raise CodecUnavailable('kernel_codec_unavailable') from exc


class Ledger:
    """The production WorkItem ledger as published on PULSE ``origin/main``.

    Decisions are decoded with the kernel's ``load_entry``: the plain ``record``
    wrapper and the chunked ``entry`` + ``objects`` encoding of large decisions.
    """

    def __init__(self, repo: Repo, segments):
        self.repo, self.segments = repo, segments
        self._journals: dict[str, list[tuple[int, str]]] | None = None
        self._claims: dict[str, list[str]] | None = None
        self._facts: dict[str, dict[str, Any]] = {}
        self._entries: dict[tuple[str, int], dict[str, Any] | None] = {}
        self._gaps: dict[str, list[str]] = {}
        self.unreadable: set[str] = set()

    # The ledger's own layout; any part missing means this is not a readable ledger.
    REQUIRED = ('.harness/core/format.json', '.harness/core/journals', '.harness/core/work-items',
                '.harness/core/pull-requests', '.harness/work-items', '.harness/events')

    def exists(self, path: str) -> bool:
        return self.repo.blob(f'{self.repo.ref}:{path}') is not None

    def missing(self) -> list[str]:
        absent = [p for p in self.REQUIRED if not self.exists(p)]
        if not absent:
            try:
                form = json.loads(self.read('.harness/core/format.json'))
            except (ValueError, OSError):
                form = None
            if not isinstance(form, dict) or form.get('schema') != self.segments.SCHEMA:
                absent.append('.harness/core/format.json:schema')
        return absent

    def read(self, path: str) -> bytes:
        data = self.repo.blob(f'{self.repo.ref}:{path}')
        if data is None:
            raise FileNotFoundError(path)
        return data

    def journals(self) -> dict[str, list[tuple[int, str]]]:
        if self._journals is None:
            journals: dict[str, list[tuple[int, str]]] = {}
            for line in self.repo.git('ls-tree', '-r', self.repo.ref, '.harness/core/journals/').splitlines():
                meta, _, path = line.partition('\t')
                parts = path.split('/')
                if len(parts) == 5 and parts[4].endswith('.json') and parts[4][:-5].isdigit():
                    journals.setdefault(parts[3], []).append((int(parts[4][:-5]), meta.split()[2]))
            for rows in journals.values():
                rows.sort()
            self._journals = journals
        return self._journals

    def entry(self, wi: str, revision: int) -> dict[str, Any] | None:
        """One decision through the kernel codec; None (and the WI marked unreadable) if it fails."""
        if (wi, revision) in self._entries:
            return self._entries[wi, revision]
        self._entries[wi, revision] = record = self._decode(wi, revision)
        return record

    def _decode(self, wi: str, revision: int) -> dict[str, Any] | None:
        try:
            record, _ = self.segments.load_entry(self.segments.entry_path(wi, revision), self.read)
        except GitUnavailable:
            raise
        except Exception:  # noqa: BLE001 - the kernel's Rejected or a missing object.
            record = None
        if not isinstance(record, dict) or _instant(record.get('at')) is None:
            self.unreadable.add(wi)
            return None
        return record

    def between(self, wi: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Journal entries with ``start <= at < end``; entries are appended in time order."""
        rows = self.journals().get(wi, [])
        cache: dict[int, datetime] = {}

        def at(index: int) -> datetime:
            if index not in cache:
                record = self.entry(wi, rows[index][0])
                cache[index] = _instant(record['at']) if record else start
            return cache[index]

        lo, hi = 0, len(rows)
        while lo < hi:
            mid = (lo + hi) // 2
            if at(mid) < start:
                lo = mid + 1
            else:
                hi = mid
        found = []
        for index in range(lo, len(rows)):
            record = self.entry(wi, rows[index][0])
            if record is None:
                continue
            moment = _instant(record['at'])
            if moment >= end:
                break
            if moment >= start:
                found.append(record)
        return found

    def gaps(self, wi: str) -> list[str]:
        """Why this WorkItem's journal is not the whole of it: revisions missing between 1 and the
        head the ledger records for it (``core/work-items/<wi>.json``), or an unreadable head."""
        if wi not in self._gaps:
            present = {number for number, _ in self.journals().get(wi, [])}
            try:
                head = json.loads(self.read(f'.harness/core/work-items/{wi}.json'))
                last = head['revision'] if head.get('work_item_id') == wi else None
            except (ValueError, OSError, KeyError, TypeError, AttributeError):
                last = None
            if type(last) is not int or last < 1:
                self._gaps[wi] = ['head_unreadable']
            else:
                missing = sorted(set(range(1, last + 1)) - present)
                self._gaps[wi] = [f'missing_revisions:{_ranges(missing)}'] if missing else []
        return self._gaps[wi]

    def first(self, wi: str) -> dict[str, Any] | None:
        rows = self.journals().get(wi)
        # Revision 1 only: a journal missing its start (a gap, reported) names no creator.
        return self.entry(wi, 1) if rows and rows[0][0] == 1 else None

    def claims(self, key: str) -> list[dict[str, Any]]:
        """Journal entries that claimed or released one PR, each verified against its locator
        digest as the kernel's ``resolve_claim`` does. Revisions are per WorkItem, so the
        entries carry no order across WorkItems; ``pull_request`` applies the kernel's rule."""
        if self._claims is None:
            claims: dict[str, list[str]] = {}
            for line in self.repo.git('ls-tree', '-r', self.repo.ref, '.harness/core/pull-requests/').splitlines():
                meta, _, path = line.partition('\t')
                parts = path.split('/')
                if len(parts) == 5:
                    claims.setdefault(parts[3], []).append(meta.split()[2])
            self._claims = claims
        entries = []
        for blob in self._claims.get(key_digest(key), []):
            try:
                claim = json.loads(self.repo.blob(blob) or b'null')
                wi, revision = claim['wi'], int(claim['revision'])
            except (ValueError, KeyError, TypeError):
                self.unreadable.add('claim:' + key)
                continue
            record = self.entry(wi, revision)
            if record is None or self.segments.digest(record) != claim.get('digest'):
                self.unreadable.add('claim:' + key)
                continue
            entries.append(record)
        return entries

    def pull_request(self, key: str) -> dict[str, Any]:
        """Kernel facts of one PR, independent of any query window.

        ``claimed``: some WorkItem registered it. ``merge``: the kernel's merge
        read-back, found by reading the claiming WorkItem's journal forward from
        its last registration however late the read-back came.
        """
        if key in self._facts:
            return self._facts[key]
        facts: dict[str, Any] = {'claimed': False, 'work_item_id': None, 'submitter': None,
                                 'first_registered_at': None, 'merge': None, 'gaps': [], 'withdrawn': False}
        # The kernel's rule (ProductionLedger.check_pull_request): within each WorkItem its
        # latest Submitted/Withdrawn mark by that WorkItem's own revision; the PR belongs to
        # the one WorkItem whose latest mark is Submitted. Revisions never compare across WIs.
        latest: dict[str, tuple[int, str]] = {}
        marked_at: dict[str, datetime] = {}
        submits: list[tuple[datetime, str, int, str | None]] = []
        for record in self.claims(key):
            for event in record.get('events') or []:
                payload = event.get('payload') or {}
                if event.get('kind') in ('PullRequestSubmitted', 'PullRequestWithdrawn') and payload.get('key') == key:
                    mark = (int(record['revision']), event['kind'])
                    if mark >= latest.get(record['wi'], mark):
                        latest[record['wi']], marked_at[record['wi']] = mark, _instant(record['at'])
                    if event['kind'] == 'PullRequestSubmitted':
                        submits.append((_instant(record['at']), record['wi'], int(record['revision']),
                                        (payload.get('author') or {}).get('actor')))
        holders = sorted(wi for wi, (_, kind) in latest.items() if kind == 'PullRequestSubmitted')
        facts['claimed'] = bool(submits)
        if submits:
            facts['first_registered_at'] = min(s[0] for s in submits)
        if len(holders) > 1:
            facts['gaps'].append('claim_ambiguous:' + ','.join(holders))
        last = None
        # Withdrawn everywhere: no WorkItem holds it now. It is still reported under the
        # WorkItem that withdrew it last (time is global; revisions are not); kernel_merged is
        # true only if a read-back came before the withdrawal, otherwise false.
        released = None if holders or not marked_at else max(marked_at, key=lambda w: marked_at[w])
        if released:
            mine = sorted((s for s in submits if s[1] == released), key=lambda s: s[2])
            facts.update(work_item_id=released, submitter=mine[0][3] if mine else None, withdrawn=True)
            if mine:  # a read-back between its last registration and the withdrawal still counts
                last = (released, mine[-1][2])
                facts['gaps'] += [f'{released}:{g}' for g in self.gaps(released)]
        if len(holders) == 1:
            wi = holders[0]
            mine = sorted((s for s in submits if s[1] == wi), key=lambda s: s[2])
            facts.update(work_item_id=wi, submitter=mine[0][3] if mine else None)
            last = (wi, latest[wi][0])
            facts['gaps'] += [f'{wi}:{g}' for g in self.gaps(wi)]
        if last is not None:
            wi, revision = last
            for number, _ in self.journals().get(wi, []):
                if number <= revision:
                    continue
                record = self.entry(wi, number)
                for event in (record or {}).get('events') or []:
                    merge = _merge_observation(event)
                    if merge and kernel_key(str(merge.get('repository')), int(merge.get('number'))) == key:
                        facts['merge'] = {'merged_at': _instant(merge.get('merged_at')),
                                          'merge_commit': merge.get('merge_commit'),
                                          'observed_at': _instant(record['at'])}
                        break
                if facts['merge']:
                    break
        self._facts[key] = facts
        return facts

    def production_history(self) -> dict[str, list[dict[str, Any]]]:
        """Imported pre-native events per WorkItem (``.harness/events/<wi>.events.yaml``)."""
        import yaml  # The shared Python environment carries PyYAML.

        loader = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)
        found: dict[str, list[dict[str, Any]]] = {}
        for line in self.repo.git('ls-tree', self.repo.ref, '.harness/events/').splitlines():
            meta, _, path = line.partition('\t')
            name = path.rsplit('/', 1)[-1]
            if not name.endswith('.events.yaml'):
                continue
            wi = name[:-len('.events.yaml')]
            try:
                documents = list(yaml.load_all(self.repo.blob(meta.split()[2]) or b'', Loader=loader))
            except yaml.YAMLError:
                self.unreadable.add(wi)
                continue
            found[wi] = [d for d in documents if isinstance(d, dict)]
        return found


def _ranges(numbers: list[int]) -> str:
    """1,2,3,7 -> 1-3,7: a bounded description of missing revisions."""
    parts, run = [], []
    for n in numbers:
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            parts.append(f'{run[0]}-{run[-1]}' if len(run) > 1 else str(run[0]))
        run = [n]
    if run:
        parts.append(f'{run[0]}-{run[-1]}' if len(run) > 1 else str(run[0]))
    return ','.join(parts)


def _yaml(data: bytes | None) -> Any:
    import yaml  # The shared Python environment carries PyYAML.

    return yaml.load(data or b'', Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader))


def registry_agents(repo: Repo) -> dict[str, dict[str, Any]] | None:
    """Agent name -> its entry (``workspace``, ``surface_registry``) in ``ssot/registry.yaml`` on origin/main."""
    import yaml

    try:
        agents = _yaml(repo.blob(f'{repo.ref}:ssot/registry.yaml'))['agents']
        return {name: v if isinstance(v, dict) else {} for name, v in agents.items() if isinstance(name, str)}
    except (yaml.YAMLError, KeyError, TypeError, AttributeError):
        return None


def _surface_names(document: Any) -> tuple[dict[str, str | None], str | None]:
    """{workspace or ``id:<surface_id>``: surface_id} one version of a surface registry names, and its agent_id.

    Both registry shapes: ``surfaces``/``retired_surfaces`` lists (``config/scl-surfaces.yaml``) and a
    ``workspaces`` mapping (``config/pulse-workspace-checkouts.yaml``). A surface without a
    workspace is keyed by its id, so a caller can tell it was named but not placed.
    """
    if not isinstance(document, dict):
        raise ValueError('not a mapping')
    names: dict[str, str | None] = {}
    for key in ('surfaces', 'retired_surfaces'):
        for item in document.get(key) or []:
            if not isinstance(item, dict):
                raise ValueError('surface entry')
            sid = item.get('surface_id') if isinstance(item.get('surface_id'), str) else None
            workspace = item.get('workspace')
            if isinstance(workspace, str) and workspace:
                names[workspace] = sid
            elif sid:
                names['id:' + sid] = sid
    workspaces = document.get('workspaces') or {}
    if not isinstance(workspaces, dict):
        raise ValueError('workspaces')
    for workspace, item in workspaces.items():
        sid = (item or {}).get('surface_id') if isinstance(item, dict) else None
        names[str(workspace)] = sid if isinstance(sid, str) else None
    agent = document.get('agent_id')
    return names, agent if isinstance(agent, str) else None


def surface_dirs(repo: Repo, prefix: str, path: str, agent: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Execution-surface directories one registry file names now or named in any earlier version.

    Retired surfaces stay: a past day's commits in their directories are still the Agent's.
    Returns ``{dir: {surface_ids, registered_now, source}}`` (``dir`` relative to the
    Coevolution root) and the reasons attribution may be short.
    """
    source = f'{repo.slug}:{path}'
    reasons: list[str] = []
    current = repo.blob(f'{repo.ref}:{path}')
    if current is None:
        return {}, [f'surfaces:registry_unreadable:{source}']
    versions = [('current', current)]
    for sha in repo.git('log', '--format=%H', repo.ref, '--', path).split():
        data = repo.blob(f'{sha}:{path}')
        if data is not None:
            versions.append((sha, data))
    found: dict[str, dict[str, Any]] = {}
    placed: set[str] = set()
    unplaced: set[str] = set()
    for version, data in versions:
        try:
            names, owner = _surface_names(_yaml(data))
        except Exception:  # noqa: BLE001 - a version that does not parse may hide a directory.
            reasons.append(f'surfaces:registry_version_unreadable:{source}@{version[:12]}')
            continue
        if owner is not None and owner != agent:
            reasons.append(f'surfaces:registry_agent_mismatch:{source}@{version[:12]}')
            continue
        for name, sid in names.items():
            if name.startswith('id:'):
                unplaced.add(name[3:])
                continue
            if '..' in name.split('/') or name.startswith('/') or not WS_PATTERN.fullmatch(name.rstrip('/') + '/'):
                reasons.append(f'surfaces:invalid_workspace:{source}:{name}')
                continue
            directory = prefix + name.rstrip('/') + '/'
            entry = found.setdefault(directory, {'surface_ids': set(), 'registered_now': False, 'source': source})
            if sid:
                entry['surface_ids'].add(sid)
                placed.add(sid)
            if version == 'current':
                entry['registered_now'] = True
    reasons.extend(f'surfaces:surface_without_workspace:{source}:{sid}' for sid in sorted(unplaced - placed))
    return found, reasons


def _valid_ws(ws: str) -> bool:
    return '..' not in ws.split('/') and not ws.startswith('/') and bool(WS_PATTERN.fullmatch(ws))


def agent_workspaces(agent: str, entry: dict[str, Any], explicit: str | None, repos: dict[str, Repo],
                     states: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Every directory whose commits are the Agent's, where each came from, and attribution gaps.

    ``--ws`` given: that directory only. Otherwise the canonical workspace from the registry
    plus every execution surface its ``surface_registry`` (and that registry's companions)
    names now or named before; an unreadable registry, a surface never placed in a
    directory, or a sibling ``*-<workspace>`` directory no registry names leaves the list short.
    """
    def row(path: str, role: str, sources: list[str], surface_ids=(), registered_now=True) -> dict[str, Any]:
        name, inner = locate(path)
        return {'path': path, 'repository': repos[name].slug, 'repository_path': inner or '/', 'role': role,
                'sources': sources, 'surface_ids': sorted(surface_ids), 'registered_now': registered_now}

    if explicit:
        return [row(explicit, 'argument', ['argument'])], []
    ws = entry.get('workspace')
    if not isinstance(ws, str) or not ws:
        raise DayQueryError('workspace_unknown')
    ws = ws.rstrip('/') + '/'
    if not _valid_ws(ws):
        raise DayQueryError('invalid_ws')
    rows = [row(ws, 'canonical', [repos['CollaborateWithAI'].slug + ':ssot/registry.yaml'])]
    reasons: list[str] = []
    registry = entry.get('surface_registry')
    if registry is not None:
        slug, path = (registry.get('repository'), registry.get('path')) if isinstance(registry, dict) else (None, None)
        home = next((r for r in REPOSITORIES if isinstance(slug, str) and r[1].lower() == slug.lower()), None)
        if home is None or not isinstance(path, str) or not _valid_ws(path):
            reasons.append('surfaces:registry_entry_invalid')
        elif states.get(home[0]) is None:
            reasons.append(f'surfaces:registry_unreadable:{home[1]}:{path}')
        else:
            found: dict[str, dict[str, Any]] = {}
            try:
                for each in (path, *SURFACE_COMPANIONS.get((home[1], path), ())):
                    dirs, gaps = surface_dirs(repos[home[0]], home[2], each, agent)
                    reasons.extend(gaps)
                    for directory, info in dirs.items():
                        merged = found.setdefault(directory, {'surface_ids': set(), 'registered_now': False,
                                                              'sources': []})
                        merged['surface_ids'] |= info['surface_ids']
                        merged['registered_now'] |= info['registered_now']
                        merged['sources'].append(info['source'])
            except GitUnavailable:
                reasons.append(f'surfaces:registry_unreadable:{home[1]}:{path}')
            for directory, info in sorted(found.items()):
                if directory != ws:
                    rows.append(row(directory, 'surface', info['sources'], info['surface_ids'],
                                    info['registered_now']))
    # A sibling named like a surface of this workspace (``local-pulse-space`` beside
    # ``pulse-space``) that no registry names: its commits would be missed silently.
    name, inner = locate(ws)
    if states.get(name) is not None and inner:
        parent, base = inner.rstrip('/').rpartition('/')[0], inner.rstrip('/').rpartition('/')[2]
        known = {r['path'] for r in rows}
        try:
            listing = repos[name].git('ls-tree', '-d', '--name-only', repos[name].ref + ':' + parent if parent
                                      else repos[name].ref)
        except GitUnavailable:
            listing = ''
            reasons.append(f'surfaces:siblings_unreadable:{ws}')
        prefix = ws[:len(ws) - len(base) - 1]
        for child in listing.splitlines():
            if child.endswith('-' + base) and prefix + child + '/' not in known:
                reasons.append(f'surfaces:unregistered_sibling:{prefix}{child}/')
    return rows, reasons


def locate(ws: str) -> tuple[str, str]:
    """(repository name, path inside it) of a Coevolution-relative workspace path."""
    best = max((r for r in REPOSITORIES if ws.startswith(r[2]) or ws.rstrip('/') + '/' == r[2]),
               key=lambda r: len(r[2]))
    inner = ws[len(best[2]):] if ws.startswith(best[2]) else ''
    return best[0], inner


def _event_text(kind: str, payload: dict[str, Any]) -> str | None:
    record = payload.get('record') if isinstance(payload.get('record'), dict) else {}
    if kind in ('WorkRecorded', 'CheckpointRecorded', 'NarrativeRecorded'):
        return _text(record.get('summary') or payload.get('summary'))
    if kind == 'CollaborationOffered':
        return _text(payload.get('purpose'))
    if kind == 'CollaborationResponded':
        return _text(f"{payload.get('decision')}: {payload.get('reason') or ''}")
    if kind == 'WorkTerminated':
        successor = f" → {payload['successor']}" if payload.get('successor') else ''
        return _text(f"{payload.get('termination')}{successor}: {payload.get('reason') or ''}")
    if kind == 'WorkCompleted':
        return _text('accepted ' + ', '.join(payload.get('acceptance_refs') or []))
    if kind in ('WorkItemCreated', 'DefinitionRevised'):
        return _text(((payload.get('definition') or {}).get('what')))
    if kind == 'PullRequestSubmitted':
        return _text(f"{payload.get('key')} head {str(payload.get('head_sha') or '')[:12]}")
    if kind == 'PullRequestWithdrawn':
        return _text(f"{payload.get('key')}: {(payload.get('withdrawal') or {}).get('reason') or ''}")
    if kind == 'PullRequestMergeConfirmed':
        return _text(f"{payload.get('key')}: {(payload.get('confirmation') or {}).get('reason') or ''}")
    if kind == 'PullRequestReviewed':
        review = payload.get('review') or {}
        return _text(f"{payload.get('key')}: passed={review.get('passed')} score={review.get('score')}")
    if kind == 'PullRequestMerged':
        return _text(f"{payload.get('repository')}#{payload.get('number')} {payload.get('merge_commit') or ''}")
    for name in ('reason', 'summary', 'title', 'what'):
        if isinstance(payload.get(name), str):
            return _text(payload[name])
    return None


def _merge_observation(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = event.get('payload') or {}
    observation = payload.get('observation') or {}
    details = observation.get('details') or {}
    if (event.get('kind') == 'EffectObserved' and str(payload.get('effect_id', '')).endswith('.merge')
            and observation.get('outcome') == 'success' and details.get('merged') is True):
        return details
    return None


def legacy_category(kind: str) -> str:
    return next((c for prefix, c in LEGACY_CATEGORY if kind.startswith(prefix)), 'other')


@dataclass
class Coverage:
    """What one section's sources cover; reasons name every gap."""
    unavailable: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)

    def status(self) -> str:
        return 'unavailable' if self.unavailable else 'incomplete' if self.incomplete else 'complete'

    def as_dict(self) -> dict[str, Any]:
        return {'status': self.status(), 'reasons': sorted(set(self.unavailable + self.incomplete))}


@dataclass
class Inputs:
    agent: str
    first: str
    last: str | None
    ws: str | None
    root: Path
    facts_path: Path | None
    home: Path
    now: datetime
    kernel: Path
    pr_bodies: Path | None = None


def load_pr_bodies(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    """The refresh's private GitHub PR cache, or None and why (never a guess of empty)."""
    if path is None:
        return None, 'not_configured'
    try:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            return None, 'not_private'
        document = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None, 'missing'
    except (OSError, ValueError):
        return None, 'unreadable'
    if not isinstance(document, dict) or document.get('schema') != PR_BODIES_SCHEMA:
        return None, 'unsupported_schema'
    return document, None


def collect(inputs: Inputs) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The answer and its per-source freshness entries (for the query envelope)."""
    start_day, end_day, start, end = window(inputs.first, inputs.last)
    env = git_env(inputs.home)
    repos = {name: Repo(name, slug, inputs.root / rel if rel else inputs.root, env)
             for name, slug, rel in REPOSITORIES}
    sources: dict[str, dict[str, Any]] = {}
    sections = {name: Coverage() for name in ('commits', 'pull_requests', 'wi_events')}
    try:
        return _collect(inputs, repos, sources, sections, start_day, end_day, start, end), sources
    finally:
        for repo in repos.values():
            repo.close()


def _cover(entry: dict[str, Any], synced: datetime | None, end: datetime, name: str,
           sections: Iterable[Coverage]) -> None:
    if synced is None or synced < end:
        reason = 'no_sync_evidence' if synced is None else 'not_synced_through_window_end'
        entry['status'], entry['reason'] = 'incomplete', reason
        for section in sections:
            section.incomplete.append(f'{name}:{reason}')


def _collect(inputs, repos, sources, sections, start_day, end_day, start, end):
    commits_cov, prs_cov, wis_cov = sections['commits'], sections['pull_requests'], sections['wi_events']
    states: dict[str, dict[str, Any] | None] = {}
    for name, repo in repos.items():
        try:
            state = repo.state()
            entry = {'kind': 'git', 'status': 'ok', 'reason': None, 'ref': REF, 'tip': state['tip'],
                     'tip_committed_at': state['tip_committed_at'], 'generated_at': _iso(state['synced_at']),
                     'synced_at': _iso(state['synced_at']), 'sync_evidence': state['sync_evidence'],
                     'moved_since_read': state['moved_since_read'], 'stale': None}
            states[name] = state
            _cover(entry, state['synced_at'], end, 'git.' + name, [prs_cov])
        except GitUnavailable:
            entry = {'kind': 'git', 'status': 'unavailable', 'reason': 'checkout_unreadable', 'ref': REF,
                     'generated_at': None, 'stale': None}
            states[name] = None
            prs_cov.unavailable.append(f'git.{name}:checkout_unreadable')
        sources['git.' + name] = entry

    # The Agent and its workspace come from the registry on CollaborateWithAI origin/main.
    agents = registry_agents(repos['CollaborateWithAI']) if states['CollaborateWithAI'] else None
    if agents is None:
        raise DayQueryError('registry_unavailable')
    if inputs.agent not in agents:
        raise DayQueryError('unknown_agent')
    if inputs.ws is not None and not _valid_ws(inputs.ws):
        raise DayQueryError('invalid_ws')
    workspaces, gaps = agent_workspaces(inputs.agent, agents[inputs.agent], inputs.ws, repos, states)
    commits_cov.incomplete.extend(gaps)
    # Repository name -> paths inside it; '' is the whole repository.
    ws_paths: dict[str, list[str]] = {}
    for workspace in workspaces:
        name, inner = locate(workspace['path'])
        ws_paths.setdefault(name, []).append(inner)
    for name, inners in ws_paths.items():
        repo = repos[name]
        if states[name] is None:
            commits_cov.unavailable.append(f'git.{name}:checkout_unreadable')
            continue
        if sources['git.' + name]['status'] == 'incomplete':
            commits_cov.incomplete.append(f"git.{name}:{sources['git.' + name]['reason']}")
    for workspace in workspaces:
        name, inner = locate(workspace['path'])
        if (workspace['role'] != 'surface' and states[name] is not None and inner
                and repos[name].blob(f"{repos[name].ref}:{inner.rstrip('/')}") is None):
            # A path absent from the read commit lists nothing; that is not an empty workspace.
            # A retired surface's directory may be gone; its history still counts.
            commits_cov.incomplete.append(f'git.{name}:ws_path_absent')

    # WI facts: owners and titles; a narrowing hint for which journals to read.
    facts, facts_reason = wi_facts.load(inputs.facts_path, inputs.now, max_age=None)
    catalog = {item['id']: item for item in (facts or {}).get('items', [])}
    facts_at = _instant((facts or {}).get('generated_at'))
    sources['wi_facts'] = {'kind': 'stats_wi_facts', 'status': 'ok' if facts else 'unavailable',
                           'reason': facts_reason, 'generated_at': _iso(facts_at), 'stale': None,
                           'coverage': (facts or {}).get('coverage')}
    if facts is None:
        wis_cov.incomplete.append('wi_facts:' + str(facts_reason) + ':owners_from_journals_only')
    elif facts_at < end:
        sources['wi_facts'].update(status='incomplete', reason='older_than_window_end')
        wis_cov.incomplete.append('wi_facts:older_than_window_end:owner_of_new_wis_from_journals')
    if facts is not None and facts.get('coverage') != 'complete':
        wis_cov.incomplete.append('wi_facts:partial_coverage')

    pulse_state = states['PULSE']
    ledger, ledger_reason = None, 'checkout_unreadable'
    if pulse_state:
        try:
            ledger = Ledger(repos['PULSE'], kernel_segments(inputs.kernel))
            missing = ledger.missing()
        except CodecUnavailable as error:
            ledger_reason = str(error)
        except GitUnavailable:
            ledger, ledger_reason = None, 'read_failed'
        else:
            if missing:
                # An absent ledger reads as no journals at all; never answer that as no events.
                ledger, ledger_reason = None, 'ledger_absent:' + ','.join(missing)
    sources['ledger'] = {'kind': 'kernel_ledger_git', 'status': 'ok' if ledger else 'unavailable',
                         'reason': None if ledger else ledger_reason, 'ref': REF,
                         'generated_at': _iso((pulse_state or {}).get('synced_at')), 'stale': None}
    if ledger is None:
        wis_cov.unavailable.append('ledger:' + ledger_reason)
        prs_cov.unavailable.append('ledger:' + ledger_reason)
    else:
        _cover(sources['ledger'], pulse_state['synced_at'], end, 'ledger', [wis_cov])
        # Each WorkItem the facts give this Agent, existing before the ledger was read and
        # active in or after the window, must be in the ledger: a native journal when it has
        # native revisions, otherwise its imported record.
        ledger_at = pulse_state['synced_at'] or end
        # Other Agents' WorkItems missing likewise could hide this Agent's part in them.
        for wi, item in sorted(catalog.items()):
            created, updated = _instant(item.get('created_at')), _instant(item.get('updated_at'))
            if not created or not updated or created >= min(end, ledger_at) or updated < start:
                continue
            native = int(item.get('local_revision') or 0) > 1
            if not (ledger.journals().get(wi) if native else ledger.exists(f'.harness/work-items/{wi}.yaml')):
                (wis_cov.unavailable if item.get('owner') == inputs.agent else wis_cov.incomplete).append(
                    'ledger:work_item_missing:' + wi)

    # 1. Commits that entered any of the Agent's directories, by committer time; one row per commit.
    commits, ledger_commits = [], []
    ws_shas: dict[str, set[str]] = {}
    mainline: dict[str, dict[str, list[dict[str, Any]]]] = {}
    since = (start - timedelta(days=1)).isoformat()
    for name, inners in ws_paths.items():
        repo = repos[name]
        if states[name] is None:
            continue
        try:
            whole = '' in inners
            ws_shas[name] = set(repo.git('log', '--format=%H', '--no-merges', '--full-history', repo.ref,
                                         *([] if whole else ['--', *inners])).split())
            landing = repo.landing()
            mainline[name] = _mainline(repo, start)
            by_sha = {c['sha']: c for c in mainline[name]['all']}
            rows: dict[str, dict[str, Any]] = {}
            touched: dict[str, list[str]] = {}
            for inner in inners:
                for row in repo.records(f'--since-as-filter={since}', '--no-merges', '--full-history', repo.ref,
                                        *(['--', inner] if inner else [])):
                    if start <= row['committed_at'] < end:
                        rows[row['sha']] = row
                        touched.setdefault(row['sha'], []).append(REPOSITORY_PATH[name] + inner)
            for sha, row in rows.items():
                main = by_sha.get(landing.get(sha, ''))
                pr = pull_request_of(main) if main else None
                item = {'repository': repo.slug, 'sha': sha, 'committed_at': _iso(row['committed_at']),
                        'authored_at': _iso(row['authored_at']), 'author': row['author'],
                        'subject': row['subject'], 'root': not row['parents'], 'workspaces': sorted(touched[sha]),
                        'landed_at': _iso(main['committed_at']) if main else None,
                        'landed_by': main['sha'] if main else None,
                        'pull_request': pr and {'number': pr['number'], 'method': pr['method']},
                        'named_wis': sorted(_wis(row['subject'], row['body']))}
                if ledger_commit(row['subject']):
                    ledger_commits.append({k: item[k] for k in ('repository', 'sha', 'committed_at', 'author',
                                                                'subject', 'workspaces')})
                else:
                    commits.append(item)
        except GitUnavailable:
            commits_cov.unavailable.append(f'git.{name}:read_failed')
    commits.sort(key=lambda c: c['committed_at'])
    ledger_commits.sort(key=lambda c: c['committed_at'])

    # 2. Kernel events of every WorkItem active in the window (narrowed by WI facts when present).
    journals = ledger.journals() if ledger else {}
    candidates = sorted(wi for wi in journals
                        if wi not in catalog or facts_at is None or facts_at < end
                        or (_instant(catalog[wi].get('updated_at')) or end) >= start)
    entries: dict[str, list[dict[str, Any]]] = {}
    if ledger:
        try:
            for wi in candidates:
                # A revision missing inside the journal hides that decision; say so per WI.
                wis_cov.incomplete.extend(f'ledger:journal_gap:{wi}:{gap}' for gap in ledger.gaps(wi))
                found = ledger.between(wi, start, end)
                if found:
                    entries[wi] = found
        except GitUnavailable:
            wis_cov.unavailable.append('ledger:read_failed')

    owners = {wi: item.get('owner') for wi, item in catalog.items()}
    titles = {wi: item.get('title') for wi, item in catalog.items()}
    for wi in journals:
        if wi not in owners and ledger:
            first = ledger.first(wi) or {}
            for event in first.get('events', []):
                if event.get('kind') == 'WorkItemCreated':
                    definition = (event.get('payload') or {}).get('definition') or {}
                    owners[wi] = definition.get('owner')
                    titles[wi] = _text(definition.get('what'))

    # Kernel PR events recorded in the window: registration rounds, withdrawals and
    # human merge confirmations, each at the time the kernel recorded it.
    kernel_events: dict[str, list[dict[str, Any]]] = {}

    native_events: dict[str, list[dict[str, Any]]] = {}
    noise: dict[str, int] = {}
    for wi, records in entries.items():
        for record in records:
            moment = _instant(record['at'])
            semantics = record.get('semantics') or {}
            principal = semantics.get('principal') or {}
            human = (semantics.get('source') or {}).get('kind') == 'native_human'
            for event in record.get('events') or []:
                kind, payload = event.get('kind'), event.get('payload') or {}
                merge = _merge_observation(event)
                if merge:
                    # Listed when the kernel read it back; the PR's merge time comes from Git.
                    kind, payload = 'PullRequestMerged', {**merge}
                moment_used = moment
                key = str(payload.get('key'))
                if kind == 'PullRequestSubmitted':
                    kernel_events.setdefault(key, []).append(
                        {'kind': 'registered', 'at': moment, 'head_sha': payload.get('head_sha'), 'source': 'kernel'})
                elif kind == 'PullRequestWithdrawn':
                    withdrawal = payload.get('withdrawal') or {}
                    kernel_events.setdefault(key, []).append(
                        {'kind': 'withdrawn', 'at': moment, 'reason': _text(withdrawal.get('reason')),
                         'source': 'kernel'})
                elif kind == 'PullRequestMergeConfirmed':
                    kernel_events.setdefault(key, []).append(
                        {'kind': 'merge_confirmed_by_human', 'at': moment, 'source': 'kernel'})
                if kind in NOISE:
                    noise[wi] = noise.get(wi, 0) + 1
                    continue
                actor = principal.get('actor')
                recipients = {r.get('actor') for r in payload.get('recipients') or [] if isinstance(r, dict)}
                native_events.setdefault(wi, []).append({
                    'at': moment_used, 'revision': record.get('revision'), 'kind': kind,
                    'category': CATEGORY.get(kind, 'other'), 'actor': actor, 'human_input': human,
                    'recipients': sorted(r for r in recipients if r), 'contract_id': payload.get('contract_id'),
                    'text': _event_text(kind, payload), 'source': 'native_journal'})

    # Production history (imported pre-native events).
    history_events: dict[str, list[dict[str, Any]]] = {}
    if ledger:
        try:
            history = ledger.production_history()
        except GitUnavailable:
            history = {}
            wis_cov.unavailable.append('ledger:production_history_unreadable')
        for wi, events in history.items():
            for event in events:
                moment = _instant(event.get('timestamp'))
                kind = str(event.get('event_type'))
                if moment is None or not start <= moment < end:
                    continue
                if kind in NOISE:
                    noise[wi] = noise.get(wi, 0) + 1
                    continue
                operator = event.get('operator')
                payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
                to_owner = payload.get('to_owner')
                history_events.setdefault(wi, []).append({
                    'at': moment, 'revision': None, 'kind': kind, 'category': legacy_category(kind),
                    'actor': operator, 'human_input': None, 'recipients': [str(to_owner)] if to_owner else [],
                    'contract_id': payload.get('handoff_id'),
                    'text': _text(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
                    if payload else None, 'source': 'production_history'})

    # 3. Pull requests. Candidates: PRs with kernel events in the window and PRs whose merge
    # commit reached a synced main in the window. The kernel facts of each come from its
    # claims (pull_request), so a late merge read-back still decides kernel_merged.
    my_wis = {wi for wi, owner in owners.items() if owner == inputs.agent}
    names = {s.lower(): (n, s) for n, s, _ in REPOSITORIES}
    prs: dict[str, dict[str, Any]] = {}

    def facts_of(key: str) -> dict[str, Any] | None:
        if ledger is None:
            return None
        try:
            facts = ledger.pull_request(key)
        except GitUnavailable:
            prs_cov.unavailable.append('ledger:read_failed')
            return None
        prs_cov.incomplete.extend(f'ledger:pull_request:{key}:{gap}' for gap in facts['gaps'])
        return facts

    def pr_entry(key: str, name: str, number: int) -> dict[str, Any]:
        if key not in prs:
            facts = facts_of(key)
            merge = (facts or {}).get('merge')
            prs[key] = {
                'repository': dict((n, s) for n, s, _ in REPOSITORIES)[name], 'number': number, 'title': None,
                'title_source': None, 'head_branch': None, 'body': None, 'body_source': 'not_collected',
                'work_item_id': (facts or {}).get('work_item_id'), 'submitter': (facts or {}).get('submitter'),
                'first_registered_at': (facts or {}).get('first_registered_at'),
                'merge_commit': (merge or {}).get('merge_commit'), 'merged_at': (merge or {}).get('merged_at'),
                'kernel_merged': None if facts is None else (
                    True if merge else (False if facts['withdrawn'] or not facts['claimed'] else None)),
                'kernel_merge_observed_at': (merge or {}).get('observed_at'),
                'state': None, 'opened_at': None, 'closed_at': None, 'body_truncated': None,
                'github_observed_at': None,
                'commits': [], 'events': list(kernel_events.get(key, [])), 'reasons': set()}
            entry = prs[key]
            if entry['submitter'] == inputs.agent:
                entry['reasons'].add('submitted_by_agent')
            if entry['work_item_id'] in my_wis:
                entry['reasons'].add('agent_work_item')
        return prs[key]

    for key in kernel_events:
        slug, _, number = key.partition('#')
        if slug in names and number.isdigit():
            pr_entry(key, names[slug][0], int(number))
    for name, repo in repos.items():
        if states[name] is None:
            continue
        try:
            lines = mainline.get(name) or _mainline(repo, start)
            mainline[name] = lines
            introduced: dict[str, list[str]] = {}
            for sha, main in repo.landing().items():
                introduced.setdefault(main, []).append(sha)
            for commit in lines['all']:
                pr = pull_request_of(commit)
                if pr is None:
                    continue
                key = kernel_key(repo.slug, pr['number'])
                in_window = start <= commit['committed_at'] < end
                if key not in prs and not in_window:
                    continue
                entry = pr_entry(key, name, pr['number'])
                entry.update(title=pr['title'], title_source='merge_commit_message' if pr['method'] == 'merge'
                             else 'squash_commit_subject', head_branch=pr['head_branch'], merge_commit=commit['sha'],
                             merged_at=commit['committed_at'])
                entry['commits'] = sorted(c for c in introduced.get(commit['sha'], []) if c != commit['sha']
                                          or pr['method'] == 'squash')
                if set(introduced.get(commit['sha'], [])) & ws_shas.get(name, set()):
                    entry['reasons'].add('touches_workspace')
                if in_window:
                    # The merge's day is GitHub's merge time, the merge commit's committer time.
                    entry['events'].append({'kind': 'merged', 'at': commit['committed_at'],
                                            'source': 'kernel' if entry['kernel_merged'] else 'git_merge_commit'})
        except GitUnavailable:
            prs_cov.unavailable.append(f'git.{name}:read_failed')
    # GitHub's side from the refresh's PR cache: bodies, and PRs opened or closed unmerged in
    # the window (merges come from Git above). A repository whose cache does not cover the
    # window may miss such PRs; a listed PR the cache lacks has no body.
    cache, cache_reason = load_pr_bodies(inputs.pr_bodies)
    cached = {}
    github_from: datetime | None = None
    github_at: datetime | None = None
    rows = {str(r.get('repository')).lower(): r for r in (cache or {}).get('repositories') or [] if isinstance(r, dict)}
    cache_entry = {'kind': 'stats_github_pr_cache', 'status': 'ok' if cache else 'unavailable',
                   'reason': cache_reason, 'generated_at': None, 'stale': None, 'repositories': {}}
    if cache is None:
        prs_cov.incomplete.append(f'github:pr_cache_unavailable:{cache_reason}')
    else:
        froms, ats = [], []
        for name, slug, _ in REPOSITORIES:
            row = rows.get(slug.lower()) or {}
            covered_since, observed = _instant(row.get('covered_since')), _instant(row.get('observed_at'))
            cache_entry['repositories'][slug] = {'covered_since': _iso(covered_since), 'observed_at': _iso(observed),
                                                 'status': row.get('status'), 'reason': row.get('reason')}
            froms.append(covered_since)
            ats.append(observed)
            if covered_since is None or observed is None or covered_since > start or observed < end:
                prs_cov.incomplete.append(f'github.{name}:pr_cache_not_covering_window')
            for pull in row.get('pull_requests') or []:
                if isinstance(pull, dict) and isinstance(pull.get('number'), int):
                    cached[kernel_key(slug, pull['number'])] = (name, pull, observed)
        if all(froms) and all(ats):
            github_from, github_at = max(froms), min(ats)
        cache_entry['generated_at'] = _iso(github_at)
        if github_from is None or github_from > start or github_at < end:
            cache_entry.update(status='incomplete', reason='not_covering_window')
    sources['github.pr_cache'] = cache_entry
    for key, (name, pull, _) in cached.items():
        opened, closed = _instant(pull.get('created_at')), _instant(pull.get('closed_at'))
        unmerged_close = closed if closed and not pull.get('merged_at') else None
        if key in prs or any(t and start <= t < end for t in (opened, unmerged_close)):
            entry = pr_entry(key, name, pull['number'])
            entry['opened_at'], entry['closed_at'] = opened, closed
            entry['state'] = 'merged' if pull.get('merged_at') else pull.get('state')
            entry.update(body=pull.get('body'), body_source='github_pr_cache',
                         body_truncated=pull.get('body_truncated') is True, github_observed_at=cached[key][2])
            if entry['title'] is None and pull.get('title'):
                entry.update(title=pull['title'], title_source='github_pr_cache')
            if entry['head_branch'] is None:
                entry['head_branch'] = pull.get('head_ref')
            for kind, moment in (('opened', opened), ('closed', unmerged_close)):
                if moment and start <= moment < end:
                    entry['events'].append({'kind': kind, 'at': moment, 'source': 'github_pr_cache'})
    for entry in prs.values():
        if entry['title'] is None:
            entry['title_source'] = 'unavailable_not_on_synced_main'
        if entry['head_branch'] and entry['head_branch'].startswith(inputs.agent + '/'):
            entry['reasons'].add('branch_prefix')
    pull_requests = [p for p in prs.values() if p['reasons'] and any(start <= e['at'] < end for e in p['events'])]
    body_gap_days: set[str] = set()
    for pr in pull_requests:
        if pr['body_source'] == 'not_collected':
            key = kernel_key(pr['repository'], pr['number'])
            prs_cov.incomplete.append(f'github:pr_body_not_collected:{key}')
            body_gap_days.update(_day(e['at']) for e in pr['events'] if start <= e['at'] < end)

    # 4. WorkItems. An event is listed when its WorkItem is the Agent's own, when that day's
    # commits or PRs name the WorkItem, or when the Agent made it or was asked in it.
    # Naming is per day, so a range counts each day exactly as that day's own query.
    named_on: dict[str, set[str]] = {}
    for commit in commits:
        for wi in commit['named_wis']:
            named_on.setdefault(wi, set()).add(_day(_instant(commit['committed_at'])))
    for pr in pull_requests:
        days = {_day(e['at']) for e in pr['events'] if start <= e['at'] < end}
        for wi in _wis(pr['title']) | ({pr['work_item_id']} if pr['work_item_id'] else set()):
            named_on.setdefault(wi, set()).update(days)
    work_items = []
    for wi in sorted(set(native_events) | set(history_events)):
        owner = owners.get(wi) == inputs.agent
        reasons, kept = {'owner'} if owner else set(), []
        for event in sorted(native_events.get(wi, []) + history_events.get(wi, []), key=lambda e: e['at']):
            named = _day(event['at']) in named_on.get(wi, ())
            acted, asked = event['actor'] == inputs.agent, inputs.agent in event['recipients']
            if owner or named or acted or asked:
                kept.append(event)
                reasons.update(r for r, hit in (('named', named), ('actor', acted), ('recipient', asked)) if hit)
        if not kept:
            continue
        work_items.append({'id': wi, 'title': titles.get(wi), 'owner': owners.get(wi), 'reasons': sorted(reasons),
                           'scope': 'all_events' if reasons & {'owner', 'named'} else 'agent_events',
                           'events': kept, 'noise_excluded': noise.get(wi, 0)})
    found = {w['id'] for w in work_items}
    quiet_named = sorted(wi for wi in named_on if wi not in found)

    for wi in sorted(ledger.unreadable if ledger else ()):
        wis_cov.incomplete.append('ledger:unreadable:' + wi)
    # Every source that decides which events belong to a day: the three checkouts, the ledger
    # on PULSE, the WI facts (owners) and the GitHub PR cache. A day is covered only up to the
    # earliest of them, and from the PR cache's coverage start on.
    synced = [s.get('synced_at') if s else None for s in states.values()] + [facts_at, github_at]
    covered = None if any(t is None for t in synced) or ledger is None else min(synced)
    return _shape(inputs, workspaces, start_day, end_day, start, end, commits, ledger_commits,
                  pull_requests, work_items, quiet_named, sections, covered, github_from, body_gap_days)


def _mainline(repo: Repo, start: datetime) -> dict[str, list[dict[str, Any]]]:
    """First-parent ``origin/main`` commits from a day before ``start`` to the tip."""
    since = (start - timedelta(days=1)).isoformat()
    return {'all': repo.records('--first-parent', f'--since-as-filter={since}', repo.ref)}


def _day(moment: datetime) -> str:
    return moment.astimezone(ZONE).date().isoformat()


def _public(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value


def _shape(inputs, workspaces, start_day, end_day, start, end, commits, ledger_commits,
           pull_requests, work_items, quiet_named, sections, covered, github_from, body_gap_days):
    completeness = {name: cov.as_dict() for name, cov in sections.items()}
    overall = ('unavailable' if any(c['status'] == 'unavailable' for c in completeness.values())
               else 'incomplete' if any(c['status'] == 'incomplete' for c in completeness.values())
               else 'complete')
    for pr in pull_requests:
        pr['events'].sort(key=lambda e: e['at'])
    pull_requests.sort(key=lambda p: (min(e['at'] for e in p['events']), p['repository'], p['number']))
    result: dict[str, Any] = {
        'schema': SCHEMA, 'generated_at': inputs.now, 'agent': inputs.agent,
        'ws': {'path': workspaces[0]['path'], 'repository': workspaces[0]['repository'],
               'repository_path': workspaces[0]['repository_path'], 'source': 'argument' if inputs.ws else 'registry'},
        # Every directory whose commits were read: the canonical workspace (or --ws) first,
        # then the execution surfaces, each with the registry files that name it.
        'workspaces': workspaces,
        'timezone': 'UTC+8', 'from_date': start_day.isoformat(), 'to_date': end_day.isoformat(),
        'window': {'start': start, 'end': end},
        'mode': 'day' if start_day == end_day else 'range',
        'complete': overall == 'complete',
        'completeness': {'status': overall, 'sections': completeness, 'sources_synced_through': covered},
    }
    if start_day == end_day:
        result.update(
            commits=commits, ledger_commits=ledger_commits, pull_requests=pull_requests,
            work_items=work_items, named_work_items_without_events=quiet_named,
            counts={'commits': len(commits), 'ledger_commits': len(ledger_commits),
                    'pull_requests': len(pull_requests),
                    'pull_requests_merged': sum(any(e['kind'] == 'merged' for e in p['events'])
                                                for p in pull_requests),
                    'work_items': len(work_items),
                    'wi_events': sum(len(w['events']) for w in work_items)})
        return _public(result)
    days: dict[str, dict[str, Any]] = {}
    current = start_day
    while current <= end_day:
        days[current.isoformat()] = {'date': current.isoformat(), 'commits': 0, 'ledger_commits': 0,
                                     'pull_requests': 0, 'pull_requests_registered': 0,
                                     'pull_requests_merged': 0, 'pull_requests_withdrawn': 0,
                                     'wi_events': 0, 'work_items': set()}
        current += timedelta(days=1)
    for commit in commits:
        days[_day(_instant(commit['committed_at']))]['commits'] += 1
    for commit in ledger_commits:
        days[_day(_instant(commit['committed_at']))]['ledger_commits'] += 1
    for pr in pull_requests:
        touched = set()
        for event in pr['events']:
            if not start <= event['at'] < end:
                continue
            row = days[_day(event['at'])]
            touched.add(row['date'])
            name = {'registered': 'pull_requests_registered', 'merged': 'pull_requests_merged',
                    'withdrawn': 'pull_requests_withdrawn'}.get(event['kind'])
            if name:
                row[name] += 1
        for day in touched:
            days[day]['pull_requests'] += 1
    for item in work_items:
        for event in item['events']:
            row = days[_day(event['at'])]
            row['wi_events'] += 1
            row['work_items'].add(item['id'])
    # Whether every source (checkouts, ledger, WI facts, PR cache) covers the whole day and
    # nothing was unreadable or missing. The time-bound gaps and a missing PR body are judged
    # per day: by the sources' times, and by the days of that PR's events.
    timed = ('not_synced_through_window_end', 'older_than_window_end', 'pr_cache_not_covering_window',
             'pr_cache_unavailable', 'pr_body_not_collected')
    unreadable = any(c['status'] == 'unavailable' for c in completeness.values()) or any(
        not any(t in r for t in timed) for c in completeness.values() for r in c['reasons'])
    rows = []
    for row in days.values():
        day_start = datetime.combine(date.fromisoformat(row['date']), datetime.min.time(), ZONE)
        day_end = day_start + timedelta(days=1)
        row['sources_cover_day'] = (not unreadable and covered is not None and covered >= day_end
                                    and github_from is not None and github_from <= day_start
                                    and row['date'] not in body_gap_days)
        row['work_items'] = len(row['work_items'])
        row['activity'] = row['commits'] + row['pull_requests'] + row['wi_events']
        rows.append(row)
    result['days'] = rows
    result['totals'] = {k: sum(r[k] for r in rows) for k in ('commits', 'ledger_commits', 'pull_requests_registered',
                                                             'pull_requests_merged', 'pull_requests_withdrawn',
                                                             'wi_events')}
    result['note'] = ('Each day counts commits by committer time, PR events by their own time and WI events by '
                      'event time in UTC+8; completeness applies to the whole range.')
    return _public(result)

