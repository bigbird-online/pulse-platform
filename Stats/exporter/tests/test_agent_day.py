"""agent_day: one Agent's commits, PRs and WI events per UTC+8 day, from synced checkouts only."""
from datetime import datetime, timezone
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
KERNEL = ROOT.parent / 'agent-harness'
sys.path.insert(0, str(KERNEL))
sys.path.insert(0, str(ROOT / 'exporter'))
from pulse_stats import agent_day, query_cli  # noqa: E402
from harness_rewrite import production_segments  # noqa: E402

SCRIPT = ROOT / 'exporter/pulse-stats-query.py'
UTC = timezone.utc
NOW = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)
SYNCED = datetime(2026, 11, 10, 1, 30, tzinfo=UTC)
REGISTRY = """agents:

  emi:
    workspace: Bigbird/LifeLab/emi-space/
  pulse:
    workspace: Bigbird/PULSE/pulse-space/
    surface_registry:
      repository: bigbird-online/PULSE
      path: config/scl-surfaces.yaml
"""
# Nothing of the calling shell reaches the fixture's own git or the query subprocess.
CLEAN_ENV = {'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin', 'LANG': 'en_US.UTF-8',
             'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}


def utc(text):
    return datetime.fromisoformat(text).astimezone(UTC)


def surfaces(*placed, retired=()):
    """``config/scl-surfaces.yaml``: (surface_id, workspace) in service, retired ids without a workspace."""
    return json.dumps({'agent_id': 'pulse', 'surfaces': [{'surface_id': i, 'workspace': w} for i, w in placed],
                       'retired_surfaces': [{'surface_id': i, 'retired_on': '2026-11-08'} for i in retired]})


def checkouts(*placed):
    return 'workspaces:\n' + ''.join(f'  {w}:\n    surface_id: {i}\n' for i, w in placed)


def cached_pull(number, body, created='2026-11-01T00:00:00Z', merged=None, closed=None, head=None):
    return {'number': number, 'state': 'closed' if merged or closed else 'open', 'draft': False,
            'created_at': created, 'updated_at': closed or merged or created, 'closed_at': closed or merged,
            'merged_at': merged, 'merge_commit': None, 'title': f'PR {number}', 'body': body,
            'body_truncated': False, 'author': 'example-owner', 'head_ref': head}


class Checkout:
    """A throwaway repository whose ``origin/main`` and FETCH_HEAD stand in for the synced clone."""

    def __init__(self, path: Path, home: Path):
        self.path, self.env = path, {**CLEAN_ENV, 'HOME': str(home)}
        path.mkdir(parents=True, exist_ok=True)
        self.git('init', '-q', '-b', 'main')

    def git(self, *args, at=None, check=True):
        env = dict(self.env)
        if at is not None:
            stamp = at.strftime('%Y-%m-%dT%H:%M:%S+0000')
            env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        env.update(GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='f@example.com',
                   GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='f@example.com')
        return subprocess.run(['git', '-C', str(self.path), *args], check=check, capture_output=True,
                              text=True, env=env).stdout.strip()

    def write(self, rel, text):
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text) if isinstance(text, bytes) else target.write_text(text)

    def commit(self, message, at, files):
        for rel, text in files.items():
            self.write(rel, text)
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', message, at=at)
        return self.git('rev-parse', 'HEAD')

    def merge(self, branch, number, title, at):
        self.git('merge', '-q', '--no-ff', branch, '-m', f'Merge pull request #{number} from bigbird-online/{branch}',
                 '-m', title, at=at)
        return self.git('rev-parse', 'HEAD')

    def publish(self, synced=SYNCED):
        """What a fetch of origin main leaves: origin/main set, its newest reflog entry at ``synced``."""
        if self.git('rev-parse', '-q', '--verify', 'refs/remotes/origin/main', check=False):
            self.git('update-ref', '-d', 'refs/remotes/origin/main')  # restage the fixture's sync time
        self.git('update-ref', '-m', 'fetch origin main: fast-forward', 'refs/remotes/origin/main', 'HEAD',
                 at=synced)


def decision(wi, revision, at, events, actor='pulse', human=False):
    return {'wi': wi, 'revision': revision, 'at': at, 'events': events, 'request_id': f'r{revision}',
            'originals': [], 'semantics': {'principal': {'actor': actor, 'session': 'session.x'},
                                           'source': {'kind': 'native_human' if human else 'native_agent'}}}


def entry(*args, **kwargs):
    """The plain encoding: the decision inline under ``record``."""
    return json.dumps({'record': decision(*args, **kwargs), 'originals': {}})


def chunked(files, *args, **kwargs):
    """The kernel's encoding of a large decision: ``entry`` points at content-addressed chunks."""
    objects = {}
    ref = production_segments.pack(json.dumps(decision(*args, **kwargs)).encode(), objects)
    files.update(objects)
    return json.dumps({'entry': ref, 'originals': {}})


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def claim(key, record):
    """The kernel's PR claim: a locator of the claiming decision, named by digests."""
    locator = {'wi': record['wi'], 'revision': record['revision'], 'digest': production_segments.digest(record)}
    return f'.harness/core/pull-requests/{digest(key)}/{digest(locator)}.json', json.dumps(locator)


def heads(files):
    """Each native journal's head, as the kernel records it beside the journal."""
    revisions = {}
    for path in files:
        parts = path.split('/')
        if path.startswith('.harness/core/journals/') and len(parts) == 5:
            revisions[parts[3]] = max(revisions.get(parts[3], 0), int(parts[4][:-5]))
    for wi, last in revisions.items():
        files[f'.harness/core/work-items/{wi}.json'] = json.dumps({'work_item_id': wi, 'revision': last})


class AgentDayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / 'home'
        self.home.mkdir()
        self.root = Path(tmp.name) / 'Coevolution'
        self.cwa = Checkout(self.root, self.home)
        self.pulse = Checkout(self.root / 'Bigbird/PULSE', self.home)
        self.oikos = Checkout(self.root / 'Bigbird/Oikos', self.home)
        self.facts = Path(tmp.name) / 'wi-facts-v1.json'
        self.bodies = Path(tmp.name) / 'pr-bodies-v1.json'
        self.build()
        self.write_bodies()

    def build(self):
        cwa, pulse, oikos = self.cwa, self.pulse, self.oikos
        cwa.write('.gitignore', 'Bigbird/PULSE/\nBigbird/Oikos/\n')
        # A root commit inside the window that touches emi's workspace.
        self.root_sha = cwa.commit('docs(emi): first day wi_20261101_001', utc('2026-11-08T09:00:00+08:00'),
                                   {'ssot/registry.yaml': REGISTRY, 'Bigbird/LifeLab/emi-space/a.md': 'a'})
        # Names Hestia's WI the day before its events: that naming does not reach the 8th.
        cwa.commit('docs(emi): note wi_20261101_003', utc('2026-11-07T12:00:00+08:00'),
                   {'Bigbird/LifeLab/emi-space/n': 'n'})
        cwa.commit('audit(SCL): publish op', utc('2026-11-08T10:00:00+08:00'), {'Bigbird/LifeLab/emi-space/s': '1'})
        cwa.commit('chore(harness): flush runtime event ledger', utc('2026-11-08T10:05:00+08:00'),
                   {'Bigbird/LifeLab/emi-space/s': '2'})
        cwa.commit('chore(broker): snapshot human workspace dirt', utc('2026-11-08T10:06:00+08:00'),
                   {'Bigbird/LifeLab/emi-space/s': '3'})
        cwa.commit('fix(scl): a real change', utc('2026-11-08T10:10:00+08:00'), {'Bigbird/LifeLab/emi-space/b': 'b'})
        # UTC+8 edges: 23:59:59 on the 8th and 00:00:00 on the 9th.
        cwa.commit('docs(emi): last second', utc('2026-11-08T23:59:59+08:00'), {'Bigbird/LifeLab/emi-space/c': 'c'})
        cwa.commit('docs(emi): next day', utc('2026-11-09T00:00:00+08:00'), {'Bigbird/LifeLab/emi-space/d': 'd'})
        # A PR from a branch, merged on the 9th.
        cwa.git('checkout', '-q', '-b', 'emi/day-log')
        self.branch_sha = cwa.commit('docs(emi): branch work', utc('2026-11-09T08:00:00+08:00'),
                                     {'Bigbird/LifeLab/emi-space/e': 'e'})
        cwa.git('checkout', '-q', 'main')
        self.merge_sha = cwa.merge('emi/day-log', 7, 'Emi day log for wi_20261101_002', utc('2026-11-09T09:00:00+08:00'))
        # A PR merged on GitHub that no WorkItem ever claimed.
        cwa.git('checkout', '-q', '-b', 'emi/no-kernel')
        cwa.commit('docs(emi): outside the kernel', utc('2026-11-09T10:00:00+08:00'), {'Bigbird/LifeLab/emi-space/f': 'f'})
        cwa.git('checkout', '-q', 'main')
        cwa.merge('emi/no-kernel', 8, 'Unregistered change', utc('2026-11-09T10:30:00+08:00'))
        cwa.git('checkout', '-q', '-b', 'pulse/moved')
        cwa.commit('docs(pulse): moved PR', utc('2026-11-09T09:20:00+08:00'), {'PARA/moved.md': 'm'})
        cwa.git('checkout', '-q', 'main')
        self.moved_sha = cwa.merge('pulse/moved', 9, 'Moved between WorkItems', utc('2026-11-09T09:30:00+08:00'))
        cwa.commit('docs(other): outside the workspace', utc('2026-11-08T12:00:00+08:00'), {'PARA/x.md': 'x'})
        cwa.publish()

        wi = 'wi_20261101_001'
        journal = f'.harness/core/journals/{wi}/'
        key = 'bigbird-online/collaboratewithai#7'
        events = [
            (1, '2026-11-01T01:00:00+00:00', [{'kind': 'WorkItemCreated', 'payload': {
                'definition': {'owner': 'emi', 'what': 'Emi day log'}}}], 'emi', False),
            (2, '2026-11-08T01:00:00+00:00', [{'kind': 'CheckpointRecorded', 'payload': {
                'record': {'summary': 'checkpoint on the 8th'}}}], 'emi', False),
            (3, '2026-11-08T02:00:00+00:00', [{'kind': 'DiskUsageObserved', 'payload': {}}], 'emi', False),
            (4, '2026-11-08T15:59:59+00:00', [{'kind': 'CollaborationOffered', 'payload': {
                'contract_id': 'op_1', 'purpose': 'please review', 'recipients': [{'actor': 'pulse'}]}}],
             'emi', True),
            (5, '2026-11-08T16:00:00+00:00', [{'kind': 'WorkRecorded', 'payload': {
                'record': {'summary': 'record on the 9th'}}}], 'emi', False),
            (6, '2026-11-09T00:30:00+00:00', [{'kind': 'PullRequestSubmitted', 'payload': {
                'key': key, 'number': 7, 'repository': 'bigbird-online/CollaborateWithAI', 'head_sha': self.branch_sha,
                'author': {'actor': 'emi'}, 'submitted_at': '2026-11-09T00:30:00+00:00'}}], 'emi', False),
            # GitHub merged at 01:00Z on the 9th; the kernel read it back a day later.
            (7, '2026-11-10T01:00:30+00:00', [{'kind': 'EffectObserved', 'payload': {
                'effect_id': 'op_x.merge', 'observation': {'outcome': 'success', 'details': {
                    'merged': True, 'number': 7, 'repository': 'bigbird-online/CollaborateWithAI',
                    'merge_commit': self.merge_sha, 'merged_at': '2026-11-09T01:00:00Z'}}}}], 'pulse', False),
        ]
        files = {journal + f'{rev:012d}.json': entry(wi, rev, at, ev, actor, human)
                 for rev, at, ev, actor, human in events}
        path, locator = claim(key, decision(wi, *events[5]))  # rev 6 registers PR #7
        files[path] = locator
        # Someone else's WI where pulse only answered once.
        other = 'wi_20261101_003'
        files[f'.harness/core/journals/{other}/000000000001.json'] = entry(other, 1, '2026-11-08T03:00:00+00:00', [
            {'kind': 'WorkItemCreated', 'payload': {'definition': {'owner': 'hestia', 'what': 'Hestia work'}}}], 'hestia')
        files[f'.harness/core/journals/{other}/000000000002.json'] = entry(other, 2, '2026-11-08T04:00:00+00:00', [
            {'kind': 'CollaborationResponded', 'payload': {'decision': 'accept', 'reason': 'ok'}}], 'emi', True)
        files[f'.harness/core/journals/{other}/000000000003.json'] = entry(other, 3, '2026-11-08T05:00:00+00:00', [
            {'kind': 'CheckpointRecorded', 'payload': {'record': {'summary': 'hestia alone'}}}], 'hestia')
        # A ~600 KB checkpoint the kernel stores as chunked objects.
        big = 'wi_20261101_005'
        self.big_summary = 'large checkpoint ' + 'x' * 600_000
        files[f'.harness/core/journals/{big}/000000000001.json'] = entry(big, 1, '2026-11-08T01:00:00+00:00', [
            {'kind': 'WorkItemCreated', 'payload': {'definition': {'owner': 'pulse', 'what': 'Pulse work'}}}])
        files[f'.harness/core/journals/{big}/000000000002.json'] = chunked(
            files, big, 2, '2026-11-08T07:00:00+00:00',
            [{'kind': 'CheckpointRecorded', 'payload': {'record': {'summary': self.big_summary}}}])
        # Imported production history of a legacy WI.
        files['.harness/events/wi_20260901_004.events.yaml'] = (
            "---\nevent_type: execution.checkpointed\noperator: emi\npayload:\n  summary: legacy checkpoint\n"
            "timestamp: '2026-11-08T06:00:00Z'\nwork_item_id: wi_20260901_004\n"
            "---\nevent_type: work_item.native_journal_observed\noperator: emi\npayload: {}\n"
            "timestamp: '2026-11-08T06:00:01Z'\nwork_item_id: wi_20260901_004\n")
        # The ledger's own layout and the imported record of the legacy WI.
        files['.harness/core/format.json'] = json.dumps({'schema': 'harness.production-ledger/v2',
                                                         'namespace': 'pulse-production'})
        # PR #9 moved between WorkItems: registered on wi_..._007 (rev 2), withdrawn there
        # (rev 5), registered again on wi_..._008 at its rev 1, which the kernel then merged.
        # Per-WI revisions (5 > 1) must not decide which claim is current.
        moved, old = 'bigbird-online/collaboratewithai#9', 'wi_20261101_007'
        new = 'wi_20261101_008'
        created = {'kind': 'WorkItemCreated', 'payload': {'definition': {'owner': 'pulse', 'what': 'Moved PR'}}}
        submit = {'kind': 'PullRequestSubmitted', 'payload': {'key': moved, 'number': 9, 'author': {'actor': 'pulse'},
                  'repository': 'bigbird-online/CollaborateWithAI'}}
        moved_decisions = [
            decision(old, 1, '2026-11-09T00:00:00+00:00', [created]),
            decision(old, 2, '2026-11-09T00:10:00+00:00', [submit]),
            decision(old, 3, '2026-11-09T00:11:00+00:00', [{'kind': 'DiskUsageObserved', 'payload': {}}]),
            decision(old, 4, '2026-11-09T00:12:00+00:00', [{'kind': 'DiskUsageObserved', 'payload': {}}]),
            decision(old, 5, '2026-11-09T00:20:00+00:00', [{'kind': 'PullRequestWithdrawn', 'payload': {
                'key': moved, 'withdrawal': {'reason': 'moved'}}}]),
            # PR #10: registered and withdrawn on the same WorkItem, held by none afterwards.
            decision(old, 6, '2026-11-09T00:30:00+00:00', [{**submit, 'payload': {**submit['payload'], 'number': 10,
                     'key': 'bigbird-online/collaboratewithai#10'}}]),
            decision(old, 7, '2026-11-09T00:40:00+00:00', [{'kind': 'PullRequestWithdrawn', 'payload': {
                'key': 'bigbird-online/collaboratewithai#10', 'withdrawal': {'reason': 'abandoned'}}}]),
            decision(new, 1, '2026-11-09T00:25:00+00:00', [created, submit]),
            decision(new, 2, '2026-11-09T02:00:00+00:00', [{'kind': 'EffectObserved', 'payload': {
                'effect_id': 'op_y.merge', 'observation': {'outcome': 'success', 'details': {
                    'merged': True, 'number': 9, 'repository': 'bigbird-online/CollaborateWithAI',
                    'merge_commit': self.moved_sha, 'merged_at': '2026-11-09T01:30:00Z'}}}}]),
        ]
        for record in moved_decisions:
            files[f".harness/core/journals/{record['wi']}/{record['revision']:012d}.json"] = json.dumps(
                {'record': record, 'originals': {}})
            for event in record['events']:
                if event['kind'].startswith('PullRequest'):
                    path, locator = claim(event['payload']['key'], record)
                    files[path] = locator
        heads(files)
        files['.harness/work-items/wi_20260901_004.yaml'] = 'work_item_id: wi_20260901_004\nowner: emi\n'
        # Pulse's execution surfaces: vultr served until the 8th, then retired and its directory removed.
        local, vultr = ('surface_local', 'local-pulse-space'), ('surface_vultr', 'vultr-pulse-space')
        pulse.commit('config: register surfaces', utc('2026-09-01T00:00:00+08:00'), {
            'config/scl-surfaces.yaml': surfaces(local, vultr), 'config/pulse-workspace-checkouts.yaml':
            checkouts(local, vultr), 'pulse-space/IDENTITY.md': 'p', 'local-pulse-space/CLAUDE.md': 'l',
            'vultr-pulse-space/CLAUDE.md': 'v'})
        self.vultr_sha = pulse.commit('docs(pulse): vultr surface', utc('2026-11-08T11:00:00+08:00'),
                                      {'vultr-pulse-space/HEARTBEAT.md': 'beat'})
        self.both_sha = pulse.commit('docs(pulse): two surfaces', utc('2026-11-08T12:00:00+08:00'),
                                     {'local-pulse-space/a': 'a', 'vultr-pulse-space/a': 'a'})
        pulse.git('rm', '-r', '-q', 'vultr-pulse-space')
        self.retire_sha = pulse.commit('config: retire vultr', utc('2026-11-08T13:00:00+08:00'), {
            'config/scl-surfaces.yaml': surfaces(local, retired=['surface_vultr']),
            'config/pulse-workspace-checkouts.yaml': checkouts(local)})
        pulse.commit('SCL: record native production decision', utc('2026-11-09T09:30:00+08:00'), files)
        pulse.publish()
        oikos.commit('docs(hestia): init', utc('2026-11-01T00:00:00+08:00'), {'hestia-space/a': 'a'})
        oikos.publish()
        self.write_facts(SYNCED)

    def write_facts(self, generated, items=None):
        items = items if items is not None else [
            {'id': 'wi_20260901_004', 'status': 'completed', 'title': 'Legacy work', 'owner': 'emi',
             'created_at': '2026-09-01T00:00:00Z', 'updated_at': '2026-11-08T06:00:01Z'},
            {'id': 'wi_20261101_001', 'status': 'active', 'title': 'Emi day log', 'owner': 'emi',
             'created_at': '2026-11-01T01:00:00Z', 'updated_at': '2026-11-10T01:00:30Z', 'local_revision': 7}]
        self.facts.write_text(json.dumps({'schema': 'harness.stats_facts/v1', 'read_only': True, 'source': 'journal',
                                          'generated_at': generated.isoformat(), 'coverage': 'complete',
                                          'items': items, 'unavailable_items': [], 'collaborations': []}))
        self.facts.chmod(0o600)

    def write_bodies(self, pulls=None, covered_since='2026-10-01T00:00:00Z', observed=SYNCED):
        """The refresh's GitHub PR cache; CollaborateWithAI's PRs #7-#10 by default."""
        pulls = pulls if pulls is not None else [
            cached_pull(7, 'Body of #7', merged='2026-11-09T01:00:00Z', head='emi/day-log'),
            cached_pull(8, 'Body of #8', merged='2026-11-09T02:30:00Z', head='emi/no-kernel'),
            cached_pull(9, None, merged='2026-11-09T01:30:00Z', head='pulse/moved'),
            cached_pull(10, 'Abandoned', closed='2026-11-01T00:00:00Z', head='pulse/ten')]
        stamp = observed.strftime('%Y-%m-%dT%H:%M:%SZ')
        rows = [{'repository': slug, 'status': 'read', 'reason': None, 'observed_at': stamp,
                 'covered_since': covered_since, 'pull_requests': pulls if slug.endswith('CollaborateWithAI') else []}
                for slug in ('bigbird-online/PULSE', 'bigbird-online/CollaborateWithAI', 'bigbird-online/Oikos')]
        self.bodies.write_text(json.dumps({'schema': 'pulse_stats_pull_request_bodies/v1', 'observed_at': stamp,
                                           'repositories': rows}))
        self.bodies.chmod(0o600)

    def ask(self, *argv, now=NOW):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(query_cli.main(['agent_day', *argv, '--coevolution-root', str(self.root),
                                             '--wi-facts', str(self.facts), '--kernel', str(KERNEL),
                                             '--pr-bodies', str(self.bodies)],
                                            now=now, home=self.home), 0)
        return json.loads(out.getvalue())

    def test_day_detail_classifies_root_and_ledger_commits_on_utc8_bounds(self):
        document = self.ask('--agent', 'emi', '--date', '2026-11-08')
        result = document['result']
        self.assertEqual(document['schema'], 'pulse_stats_query/v1')
        self.assertEqual(result['schema'], 'pulse_stats_agent_day/v1')
        self.assertEqual(result['ws'], {'path': 'Bigbird/LifeLab/emi-space/', 'repository': 'bigbird-online/CollaborateWithAI',
                                        'repository_path': 'Bigbird/LifeLab/emi-space/', 'source': 'registry'})
        subjects = [c['subject'] for c in result['commits']]
        self.assertEqual(subjects, ['docs(emi): first day wi_20261101_001', 'fix(scl): a real change',
                                    'docs(emi): last second'])
        root = result['commits'][0]
        self.assertTrue(root['root'])
        self.assertEqual(root['sha'], self.root_sha)
        self.assertEqual(root['named_wis'], ['wi_20261101_001'])
        self.assertEqual(root['committed_at'], '2026-11-08T09:00:00+08:00')
        self.assertEqual([c['subject'].split(':')[0] for c in result['ledger_commits']],
                         ['audit(SCL)', 'chore(harness)', 'chore(broker)'])
        # Every source synced past the day's end and no PR that day: nothing is missing.
        self.assertIs(result['complete'], True)
        self.assertEqual(document['status'], 'ok')

    def test_wi_events_drop_noise_keep_history_and_cut_at_utc8_midnight(self):
        result = self.ask('--agent', 'emi', '--date', '2026-11-08')['result']
        items = {w['id']: w for w in result['work_items']}
        mine = items['wi_20261101_001']
        self.assertEqual([e['kind'] for e in mine['events']], ['CheckpointRecorded', 'CollaborationOffered'])
        self.assertEqual(mine['noise_excluded'], 1)
        self.assertEqual(mine['events'][0]['text'], 'checkpoint on the 8th')
        self.assertTrue(mine['events'][1]['human_input'])
        self.assertEqual(mine['events'][1]['at'], '2026-11-08T23:59:59+08:00')
        legacy = items['wi_20260901_004']
        self.assertEqual([(e['kind'], e['source']) for e in legacy['events']],
                         [('execution.checkpointed', 'production_history')])
        self.assertEqual(legacy['noise_excluded'], 1)
        # Emi only answered on Hestia's WI: that answer is listed, Hestia's own checkpoint is not.
        other = items['wi_20261101_003']
        self.assertEqual((other['owner'], other['scope'], other['reasons']), ('hestia', 'agent_events', ['actor']))
        self.assertEqual([e['kind'] for e in other['events']], ['CollaborationResponded'])

    def test_recipient_sees_the_contract_offered_to_it(self):
        result = self.ask('--agent', 'pulse', '--date', '2026-11-08', '--ws', 'Bigbird/PULSE/pulse-space/')['result']
        item = next(w for w in result['work_items'] if w['id'] == 'wi_20261101_001')
        self.assertEqual(item['reasons'], ['recipient'])
        self.assertEqual([e['kind'] for e in item['events']], ['CollaborationOffered'])

    def test_pull_request_from_merge_commit_and_kernel_ledger(self):
        result = self.ask('--agent', 'emi', '--date', '2026-11-09')['result']
        self.assertEqual([p['number'] for p in result['pull_requests']], [7, 8])
        pr, unclaimed = result['pull_requests']
        self.assertEqual((unclaimed['kernel_merged'], unclaimed['reasons'], unclaimed['work_item_id']),
                         (False, ['branch_prefix', 'touches_workspace'], None))
        self.assertEqual((pr['repository'], pr['number'], pr['title'], pr['head_branch']),
                         ('bigbird-online/CollaborateWithAI', 7, 'Emi day log for wi_20261101_002', 'emi/day-log'))
        self.assertEqual(pr['kernel_merged'], True)
        self.assertEqual(pr['work_item_id'], 'wi_20261101_001')
        self.assertEqual(pr['submitter'], 'emi')
        self.assertEqual([e['kind'] for e in pr['events']], ['registered', 'merged'])
        self.assertEqual(pr['merged_at'], '2026-11-09T09:00:00+08:00')
        self.assertEqual(pr['first_registered_at'], '2026-11-09T08:30:00+08:00')
        self.assertEqual(pr['commits'], [self.branch_sha])
        self.assertEqual((pr['body'], pr['body_source'], pr['state']), ('Body of #7', 'github_pr_cache', 'merged'))
        self.assertEqual(pr['opened_at'], '2026-11-01T08:00:00+08:00')
        self.assertIn('touches_workspace', pr['reasons'])
        branch = next(c for c in result['commits'] if c['sha'] == self.branch_sha)
        self.assertEqual(branch['pull_request'], {'number': 7, 'method': 'merge'})
        self.assertEqual(branch['landed_by'], self.merge_sha)
        # Bodies came from the GitHub PR cache, which covers the day: nothing is missing.
        self.assertEqual(result['completeness']['sections']['pull_requests'], {'status': 'complete', 'reasons': []})
        self.assertIn('wi_20261101_002', result['named_work_items_without_events'])

    def test_unsynced_window_is_incomplete_not_zero(self):
        self.cwa.publish(synced=utc('2026-11-08T12:00:00+08:00'))
        document = self.ask('--agent', 'emi', '--date', '2026-11-08')
        result = document['result']
        self.assertEqual(document['status'], 'partial')
        self.assertEqual(document['sources']['git.CollaborateWithAI']['status'], 'incomplete')
        self.assertEqual(result['completeness']['sections']['commits']['status'], 'incomplete')
        self.assertIn('commits:incomplete', document['errors'])
        self.assertFalse(result['complete'])

    def test_missing_checkout_and_facts_are_named(self):
        subprocess.run(['rm', '-rf', str(self.root / 'Bigbird/Oikos')], check=True)
        self.facts.unlink()
        document = self.ask('--agent', 'emi', '--date', '2026-11-08')
        sections = document['result']['completeness']['sections']
        self.assertEqual(document['sources']['git.Oikos']['status'], 'unavailable')
        self.assertEqual(sections['pull_requests']['status'], 'unavailable')
        self.assertEqual(sections['wi_events']['status'], 'incomplete')
        self.assertEqual(document['result']['completeness']['status'], 'unavailable')
        # Owners still come from the journal when WI facts are missing.
        items = {w['id']: w for w in document['result']['work_items']}
        self.assertEqual(items['wi_20261101_001']['owner'], 'emi')

    def test_stale_facts_still_answer_but_mark_owners(self):
        self.write_facts(utc('2026-11-08T12:00:00+08:00'))
        document = self.ask('--agent', 'emi', '--date', '2026-11-08')
        self.assertEqual(document['sources']['wi_facts']['status'], 'incomplete')
        self.assertEqual(document['result']['completeness']['sections']['wi_events']['status'], 'incomplete')
        self.assertIn('wi_20261101_001', {w['id'] for w in document['result']['work_items']})

    def test_range_gives_one_count_row_per_day(self):
        result = self.ask('--agent', 'emi', '--date', '2026-11-07', '--end-date', '2026-11-09')['result']
        self.assertEqual(result['mode'], 'range')
        rows = {r['date']: r for r in result['days']}
        self.assertEqual(list(rows), ['2026-11-07', '2026-11-08', '2026-11-09'])
        self.assertEqual((rows['2026-11-07']['commits'], rows['2026-11-07']['wi_events']), (1, 0))
        self.assertEqual((rows['2026-11-08']['commits'], rows['2026-11-08']['ledger_commits']), (3, 3))
        self.assertEqual(rows['2026-11-08']['wi_events'], 4)
        self.assertEqual((rows['2026-11-09']['commits'], rows['2026-11-09']['pull_requests_merged'],
                          rows['2026-11-09']['pull_requests_registered']), (3, 2, 1))
        self.assertTrue(all(r['sources_cover_day'] for r in result['days']))
        self.assertNotIn('commits', {k for k in result if k != 'days' and isinstance(result[k], list)})

    def test_range_rows_equal_each_days_own_query(self):
        rows = self.ask('--agent', 'emi', '--date', '2026-11-07', '--end-date', '2026-11-09')['result']['days']
        for row in rows:
            with self.subTest(day=row['date']):
                counts = self.ask('--agent', 'emi', '--date', row['date'])['result']['counts']
                self.assertEqual((row['commits'], row['ledger_commits'], row['pull_requests'], row['wi_events'],
                                  row['work_items']),
                                 (counts['commits'], counts['ledger_commits'], counts['pull_requests'],
                                  counts['wi_events'], counts['work_items']))

    def test_chunked_large_decision_is_read_like_the_kernel(self):
        document = self.ask('--agent', 'pulse', '--date', '2026-11-08', '--ws', 'Bigbird/PULSE/pulse-space/')
        item = next(w for w in document['result']['work_items'] if w['id'] == 'wi_20261101_005')
        self.assertEqual([e['kind'] for e in item['events']], ['WorkItemCreated', 'CheckpointRecorded'])
        self.assertTrue(item['events'][1]['text'].startswith('large checkpoint xxx'))
        self.assertTrue(item['events'][1]['text'].endswith('[truncated]'))
        self.assertEqual(document['result']['completeness']['sections']['wi_events'], {'status': 'complete',
                                                                                        'reasons': []})

    def test_late_merge_read_back_keeps_the_merge_day(self):
        day = self.ask('--agent', 'emi', '--date', '2026-11-09')['result']
        pr = next(p for p in day['pull_requests'] if p['number'] == 7)
        self.assertIs(pr['kernel_merged'], True)
        self.assertEqual(pr['kernel_merge_observed_at'], '2026-11-10T09:00:30+08:00')
        self.assertEqual([e['kind'] for e in pr['events']], ['registered', 'merged'])
        rows = {r['date']: r for r in self.ask('--agent', 'emi', '--date', '2026-11-08',
                                               '--end-date', '2026-11-10')['result']['days']}
        self.assertEqual((rows['2026-11-08']['pull_requests_merged'], rows['2026-11-09']['pull_requests_merged'],
                          rows['2026-11-10']['pull_requests_merged']), (0, 2, 0))
        # The read-back itself is a WI event of the day the kernel recorded it.
        tenth = self.ask('--agent', 'emi', '--date', '2026-11-10')['result']
        item = next(w for w in tenth['work_items'] if w['id'] == 'wi_20261101_001')
        self.assertEqual([e['kind'] for e in item['events']], ['PullRequestMerged'])

    def test_missing_or_old_facts_leave_days_uncovered(self):
        rows = self.ask('--agent', 'emi', '--date', '2026-11-08', '--end-date', '2026-11-09')['result']['days']
        self.assertEqual([r['sources_cover_day'] for r in rows], [True, True])
        self.write_facts(utc('2026-11-09T12:00:00+08:00'))
        rows = self.ask('--agent', 'emi', '--date', '2026-11-08', '--end-date', '2026-11-09')['result']['days']
        self.assertEqual([r['sources_cover_day'] for r in rows], [True, False])
        self.facts.unlink()
        rows = self.ask('--agent', 'emi', '--date', '2026-11-08', '--end-date', '2026-11-09')['result']['days']
        self.assertEqual([r['sources_cover_day'] for r in rows], [False, False])

    def test_unloadable_kernel_codec_makes_the_ledger_unavailable(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            query_cli.main(['agent_day', '--agent', 'emi', '--date', '2026-11-08', '--coevolution-root', str(self.root),
                            '--wi-facts', str(self.facts), '--kernel', str(self.home / 'missing/active/harness.pyz')],
                           now=NOW, home=self.home)
        document = json.loads(out.getvalue())
        self.assertEqual(document['sources']['ledger']['reason'], 'kernel_codec_unavailable')
        self.assertEqual(document['result']['completeness']['sections']['wi_events']['status'], 'unavailable')
        self.assertEqual(document['result']['commits'][0]['sha'], self.root_sha)

    def test_removed_ledger_is_unavailable_not_empty(self):
        self.pulse.git('rm', '-r', '-q', '.harness')
        self.pulse.commit('SCL: drop', utc('2026-11-09T10:00:00+08:00'), {})
        self.pulse.publish()
        document = self.ask('--agent', 'emi', '--date', '2026-11-08')
        self.assertEqual(document['status'], 'partial')
        self.assertTrue(document['sources']['ledger']['reason'].startswith('ledger_absent:.harness/core/format.json'))
        self.assertEqual(document['result']['completeness']['sections']['wi_events']['status'], 'unavailable')
        self.assertFalse(document['result']['complete'])

    def test_missing_journal_of_an_agent_wi_is_named(self):
        self.pulse.git('rm', '-r', '-q', '.harness/core/journals/wi_20261101_001')
        self.pulse.commit('SCL: lose one journal', utc('2026-11-09T10:00:00+08:00'), {})
        self.pulse.publish()
        sections = self.ask('--agent', 'emi', '--date', '2026-11-08')['result']['completeness']['sections']
        self.assertIn('ledger:work_item_missing:wi_20261101_001', sections['wi_events']['reasons'])
        self.assertEqual(sections['wi_events']['status'], 'unavailable')

    def test_sync_time_comes_from_the_origin_main_reflog_not_fetch_head(self):
        self.cwa.publish(synced=utc('2026-11-08T12:00:00+08:00'))
        # Fetching some other ref later rewrites FETCH_HEAD; that proves nothing about origin/main.
        fetch = self.root / '.git/FETCH_HEAD'
        fetch.write_text('0' * 40 + "\t\t'refs/pull/9/head' of fixture\n")
        os.utime(fetch, (SYNCED.timestamp(), SYNCED.timestamp()))
        document = self.ask('--agent', 'emi', '--date', '2026-11-08')
        self.assertEqual(document['sources']['git.CollaborateWithAI']['synced_at'], '2026-11-08T12:00:00+08:00')
        self.assertEqual(document['result']['completeness']['sections']['commits']['status'], 'incomplete')

    def stamp(self, sha, at, **extra):
        """What four-end-sync leaves after a fetch, even one that found nothing new."""
        path = self.root / '.git' / agent_day.SYNC_STAMP
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema': agent_day.SYNC_STAMP_SCHEMA, 'ref': agent_day.TRACKING,
                                    'sha': sha, 'synced_at': at.isoformat(), **extra}))

    def test_a_quiet_repository_is_synced_by_the_fetch_stamp(self):
        # The last commit came in at 12:00; later fetches found nothing new and left no reflog entry.
        self.cwa.publish(synced=utc('2026-11-08T12:00:00+08:00'))
        self.stamp(self.cwa.git('rev-parse', 'refs/remotes/origin/main'), SYNCED)
        document = self.ask('--agent', 'emi', '--date', '2026-11-08')
        source = document['sources']['git.CollaborateWithAI']
        self.assertEqual((source['synced_at'], source['sync_evidence'], source['status']),
                         ('2026-11-10T09:30:00+08:00', 'fetch_stamp', 'ok'))
        self.assertNotIn('git.CollaborateWithAI:not_synced_through_window_end',
                         document['result']['completeness']['sections']['commits']['reasons'])

    def test_a_stamp_for_another_commit_or_older_than_the_reflog_is_ignored(self):
        self.cwa.publish(synced=utc('2026-11-08T12:00:00+08:00'))
        tip = self.cwa.git('rev-parse', 'refs/remotes/origin/main')
        for sha, at in (('f' * 40, SYNCED), (tip, utc('2026-11-08T11:00:00+08:00'))):
            self.stamp(sha, at)
            source = self.ask('--agent', 'emi', '--date', '2026-11-08')['sources']['git.CollaborateWithAI']
            self.assertEqual((source['synced_at'], source['sync_evidence']), ('2026-11-08T12:00:00+08:00', 'reflog'))

    def test_a_malformed_stamp_is_no_evidence(self):
        (self.root / '.git/logs/refs/remotes/origin/main').unlink()
        tip = self.cwa.git('rev-parse', 'refs/remotes/origin/main')
        for bad in ({'ref': 'refs/heads/main'}, {'schema': 'other/v1'}, {'synced_at': '2026-11-10T01:30:00'}):
            self.stamp(tip, SYNCED, **bad)
            source = self.ask('--agent', 'emi', '--date', '2026-11-08')['sources']['git.CollaborateWithAI']
            self.assertEqual(source['reason'], 'no_sync_evidence')

    def test_no_reflog_means_unknown_sync(self):
        (self.root / '.git/logs/refs/remotes/origin/main').unlink()
        document = self.ask('--agent', 'emi', '--date', '2026-11-08')
        self.assertEqual(document['sources']['git.CollaborateWithAI']['reason'], 'no_sync_evidence')
        self.assertIn('git.CollaborateWithAI:no_sync_evidence',
                      document['result']['completeness']['sections']['commits']['reasons'])

    def test_every_read_uses_the_commit_the_reflog_vouches_for(self):
        recorded = self.cwa.git('rev-parse', 'refs/remotes/origin/main')
        later = self.cwa.commit('docs(emi): unsynced', utc('2026-11-08T13:00:00+08:00'),
                                {'Bigbird/LifeLab/emi-space/late': 'x'})
        # The ref moved without a fetch record (no reflog entry): its time is not known.
        (self.root / '.git/refs/remotes/origin/main').write_text(later + '\n')
        document = self.ask('--agent', 'emi', '--date', '2026-11-08')
        source = document['sources']['git.CollaborateWithAI']
        self.assertEqual((source['tip'], source['moved_since_read']), (recorded, True))
        self.assertNotIn('docs(emi): unsynced', [c['subject'] for c in document['result']['commits']])

    def test_unreadable_registry_is_not_a_default(self):
        self.cwa.git('rm', '-q', 'ssot/registry.yaml')
        self.cwa.commit('docs: drop registry', utc('2026-11-09T11:00:00+08:00'), {})
        self.cwa.publish()
        document = self.ask('--agent', 'emi', '--date', '2026-11-08', '--ws', 'Bigbird/LifeLab/emi-space/')
        self.assertEqual((document['status'], document['errors']), ('unavailable', ['registry_unavailable']))

    def test_absent_workspace_is_not_an_empty_one(self):
        sections = self.ask('--agent', 'emi', '--date', '2026-11-08', '--ws', 'PARA/nowhere/')['result'][
            'completeness']['sections']
        self.assertIn('git.CollaborateWithAI:ws_path_absent', sections['commits']['reasons'])

    def test_pr_moved_between_work_items_follows_the_kernel_claim_rule(self):
        result = self.ask('--agent', 'pulse', '--date', '2026-11-09', '--ws', 'Bigbird/PULSE/pulse-space/')['result']
        pr = next(p for p in result['pull_requests'] if p['number'] == 9)
        self.assertEqual((pr['work_item_id'], pr['kernel_merged'], pr['submitter']),
                         ('wi_20261101_008', True, 'pulse'))
        self.assertEqual(pr['first_registered_at'], '2026-11-09T08:10:00+08:00')
        # GitHub had no body for it: collected as empty, not missing.
        self.assertEqual((pr['body'], pr['body_source']), (None, 'github_pr_cache'))
        self.assertEqual(result['completeness']['sections']['pull_requests']['reasons'], [])

    def test_pr_withdrawn_everywhere_stays_with_its_work_item(self):
        result = self.ask('--agent', 'pulse', '--date', '2026-11-09', '--ws', 'Bigbird/PULSE/pulse-space/')['result']
        pr = next(p for p in result['pull_requests'] if p['number'] == 10)
        self.assertEqual((pr['work_item_id'], pr['kernel_merged'], [e['kind'] for e in pr['events']]),
                         ('wi_20261101_007', False, ['registered', 'withdrawn']))

    def test_missing_journal_revision_is_a_gap_not_silence(self):
        self.pulse.git('rm', '-q', '.harness/core/journals/wi_20261101_001/000000000002.json')
        self.pulse.commit('SCL: lose a revision', utc('2026-11-09T10:00:00+08:00'), {})
        self.pulse.publish()
        result = self.ask('--agent', 'emi', '--date', '2026-11-08')['result']
        reasons = result['completeness']['sections']['wi_events']['reasons']
        self.assertIn('ledger:journal_gap:wi_20261101_001:missing_revisions:2', reasons)
        self.assertEqual(result['completeness']['sections']['wi_events']['status'], 'incomplete')
        rows = self.ask('--agent', 'emi', '--date', '2026-11-08', '--end-date', '2026-11-09')['result']['days']
        self.assertEqual([r['sources_cover_day'] for r in rows], [False, False])

    def test_missing_journal_head_is_a_gap(self):
        self.pulse.git('rm', '-q', '.harness/core/work-items/wi_20261101_003.json')
        self.pulse.commit('SCL: lose a head', utc('2026-11-09T10:00:00+08:00'), {})
        self.pulse.publish()
        reasons = self.ask('--agent', 'emi', '--date', '2026-11-08')['result']['completeness']['sections'][
            'wi_events']['reasons']
        self.assertIn('ledger:journal_gap:wi_20261101_003:head_unreadable', reasons)

    def test_claim_that_does_not_match_its_decision_is_unreadable(self):
        key = 'bigbird-online/collaboratewithai#7'
        directory = self.pulse.path / f'.harness/core/pull-requests/{digest(key)}'
        target = next(directory.iterdir())
        locator = json.loads(target.read_text())
        target.write_text(json.dumps({**locator, 'digest': '0' * 64}))
        self.pulse.commit('SCL: tamper claim', utc('2026-11-09T10:00:00+08:00'), {})
        self.pulse.publish()
        result = self.ask('--agent', 'emi', '--date', '2026-11-09')['result']
        self.assertIn('ledger:unreadable:claim:' + key, result['completeness']['sections']['wi_events']['reasons'])

    def test_range_day_after_sync_is_not_covered(self):
        result = self.ask('--agent', 'emi', '--date', '2026-11-09', '--end-date', '2026-11-10')['result']
        rows = {r['date']: r['sources_cover_day'] for r in result['days']}
        self.assertEqual(rows, {'2026-11-09': True, '2026-11-10': False})

    def test_bad_arguments_answer_unavailable(self):
        for argv, code in ((['--agent', 'nobody', '--date', '2026-11-08'], 'unknown_agent'),
                           (['--agent', 'emi'], 'agent_and_date_required'),
                           (['--agent', 'emi', '--date', '2026-11-09', '--end-date', '2026-11-08'],
                            'end_date_before_date'),
                           (['--agent', 'emi', '--date', '2026-01-01', '--end-date', '2026-11-08'],
                            'date_range_too_long'),
                           (['--agent', 'emi', '--date', '2026-11-08', '--ws', '../etc/'], 'invalid_ws')):
            with self.subTest(code=code):
                try:
                    document = self.ask(*argv)
                except SystemExit:  # argparse refuses the token itself
                    self.assertEqual(code, 'invalid_ws')
                    continue
                self.assertEqual((document['status'], document['errors']), ('unavailable', [code]))

    def test_git_runs_with_a_fixed_environment(self):
        with mock.patch.dict(os.environ, {'PULSE_CORE_HUMAN_INPUTS': '/production', 'GIT_DIR': '/elsewhere'}):
            env = agent_day.git_env(self.home)
            self.assertNotIn('PULSE_CORE_HUMAN_INPUTS', env)
            self.assertNotIn('GIT_DIR', env)
            result = self.ask('--agent', 'emi', '--date', '2026-11-08')['result']
        self.assertEqual(len(result['commits']), 3)

    def test_installed_script_runs_without_the_callers_environment(self):
        env = {**CLEAN_ENV, 'HOME': str(self.home)}
        out = subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), 'agent_day', '--agent', 'emi',
                              '--date', '2026-11-08', '--coevolution-root', str(self.root),
                              '--wi-facts', str(self.facts), '--kernel', str(KERNEL), '--pr-bodies', str(self.bodies)],
                             capture_output=True, text=True, env=env, cwd='/',
                             check=True)
        document = json.loads(out.stdout)
        self.assertEqual(document['result']['counts']['commits'], 3)
        self.assertEqual(document['query'], 'agent_day')


    # GitHub PR text comes from the refresh's cache; what it lacks is never read as empty.

    def test_pr_closed_unmerged_comes_from_the_cache_with_its_body(self):
        self.write_bodies([cached_pull(11, 'Tried another way', created='2026-11-08T01:00:00Z',
                                       closed='2026-11-08T05:00:00Z', head='emi/abandoned')])
        result = self.ask('--agent', 'emi', '--date', '2026-11-08')['result']
        [pr] = result['pull_requests']
        self.assertEqual((pr['number'], pr['state'], pr['body'], pr['title'], pr['title_source'], pr['reasons']),
                         (11, 'closed', 'Tried another way', 'PR 11', 'github_pr_cache', ['branch_prefix']))
        self.assertEqual([(e['kind'], e['at'], e['source']) for e in pr['events']],
                         [('opened', '2026-11-08T09:00:00+08:00', 'github_pr_cache'),
                          ('closed', '2026-11-08T13:00:00+08:00', 'github_pr_cache')])
        self.assertIs(result['complete'], True)

    def test_pr_the_cache_lacks_has_no_body_and_leaves_its_day_uncovered(self):
        self.write_bodies([cached_pull(8, 'Body of #8', merged='2026-11-09T02:30:00Z')])
        result = self.ask('--agent', 'emi', '--date', '2026-11-09')['result']
        pr = next(p for p in result['pull_requests'] if p['number'] == 7)
        self.assertEqual((pr['body'], pr['body_source']), (None, 'not_collected'))
        self.assertEqual(result['completeness']['sections']['pull_requests'],
                         {'status': 'incomplete', 'reasons': ['github:pr_body_not_collected:bigbird-online/collaboratewithai#7']})
        rows = self.ask('--agent', 'emi', '--date', '2026-11-08', '--end-date', '2026-11-09')['result']['days']
        self.assertEqual([r['sources_cover_day'] for r in rows], [True, False])

    def test_missing_or_short_cache_is_incomplete_not_empty(self):
        self.write_bodies(covered_since='2026-11-08T16:00:00Z')  # 00:00 on the 9th, UTC+8
        result = self.ask('--agent', 'emi', '--date', '2026-11-08')['result']
        self.assertIn('github.Oikos:pr_cache_not_covering_window', result['completeness']['sections']['pull_requests']['reasons'])
        self.assertFalse(result['complete'])
        rows = self.ask('--agent', 'emi', '--date', '2026-11-08', '--end-date', '2026-11-09')['result']['days']
        self.assertEqual([r['sources_cover_day'] for r in rows], [False, True])
        self.bodies.unlink()
        document = self.ask('--agent', 'emi', '--date', '2026-11-09')
        self.assertEqual(document['sources']['github.pr_cache']['reason'], 'missing')
        self.assertIn('github:pr_cache_unavailable:missing',
                      document['result']['completeness']['sections']['pull_requests']['reasons'])
        self.assertEqual({p['body_source'] for p in document['result']['pull_requests']}, {'not_collected'})
        rows = self.ask('--agent', 'emi', '--date', '2026-11-08', '--end-date', '2026-11-09')['result']['days']
        self.assertEqual([r['sources_cover_day'] for r in rows], [False, False])

    # An Agent's commits: its canonical workspace and every execution surface, retired ones included.

    def test_commits_in_every_surface_directory_count_retired_ones_too(self):
        result = self.ask('--agent', 'pulse', '--date', '2026-11-08')['result']
        self.assertEqual([c['sha'] for c in result['commits']], [self.vultr_sha, self.both_sha, self.retire_sha])
        both = next(c for c in result['commits'] if c['sha'] == self.both_sha)
        self.assertEqual(both['workspaces'], ['Bigbird/PULSE/local-pulse-space/', 'Bigbird/PULSE/vultr-pulse-space/'])
        rows = {w['path']: w for w in result['workspaces']}
        self.assertEqual(list(rows), ['Bigbird/PULSE/pulse-space/', 'Bigbird/PULSE/local-pulse-space/',
                                      'Bigbird/PULSE/vultr-pulse-space/'])
        self.assertEqual(rows['Bigbird/PULSE/pulse-space/']['role'], 'canonical')
        vultr = rows['Bigbird/PULSE/vultr-pulse-space/']
        self.assertEqual((vultr['role'], vultr['registered_now'], vultr['surface_ids'], vultr['sources']),
                         ('surface', False, ['surface_vultr'], ['bigbird-online/PULSE:config/scl-surfaces.yaml',
                                                                'bigbird-online/PULSE:config/pulse-workspace-checkouts.yaml']))
        self.assertEqual(result['completeness']['sections']['commits'], {'status': 'complete', 'reasons': []})

    def test_explicit_ws_reads_only_that_directory_and_lists_it(self):
        result = self.ask('--agent', 'pulse', '--date', '2026-11-08', '--ws', 'Bigbird/PULSE/pulse-space/')['result']
        self.assertEqual(result['commits'], [])
        self.assertEqual(result['workspaces'], [{
            'path': 'Bigbird/PULSE/pulse-space/', 'repository': 'bigbird-online/PULSE', 'repository_path': 'pulse-space/',
            'role': 'argument', 'sources': ['argument'], 'surface_ids': [], 'registered_now': True}])
        self.assertEqual(result['completeness']['sections']['commits']['status'], 'complete')

    def test_unreadable_or_short_surface_registry_is_incomplete(self):
        self.pulse.commit('config: name a surface nowhere', utc('2026-11-09T11:00:00+08:00'), {
            'config/scl-surfaces.yaml': surfaces(('surface_local', 'local-pulse-space'), retired=['surface_ghost']),
            'muse-pulse-space/CLAUDE.md': 'unregistered'})
        self.pulse.publish()
        reasons = self.ask('--agent', 'pulse', '--date', '2026-11-08')['result']['completeness']['sections'][
            'commits']['reasons']
        self.assertEqual(reasons, ['surfaces:surface_without_workspace:bigbird-online/PULSE:config/scl-surfaces.yaml:'
                                   'surface_ghost', 'surfaces:unregistered_sibling:Bigbird/PULSE/muse-pulse-space/'])
        self.pulse.git('rm', '-q', 'config/scl-surfaces.yaml')
        self.pulse.commit('config: drop the surface registry', utc('2026-11-09T11:30:00+08:00'), {})
        self.pulse.publish()
        result = self.ask('--agent', 'pulse', '--date', '2026-11-08')['result']
        commits = result['completeness']['sections']['commits']
        self.assertEqual(commits['status'], 'incomplete')
        self.assertIn('surfaces:registry_unreadable:bigbird-online/PULSE:config/scl-surfaces.yaml', commits['reasons'])
        # The companion registry still places local and vultr; their commits are not lost.
        self.assertEqual(len(result['commits']), 3)


if __name__ == '__main__':
    unittest.main()
