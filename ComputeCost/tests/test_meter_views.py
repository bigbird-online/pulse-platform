"""合同第 4 节：归属与计价视图 v_usage / v_event。夹具按第 1 节 DDL 直接灌数。"""
from __future__ import annotations

from datetime import date
from itertools import combinations
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ComputeCost'))

from pulse_meter import outcomes, publish, schema_sql, turns  # noqa: E402
from pulse_compute_cost.receipts import reference  # noqa: E402

DAY = '2026-11-20'
AT = DAY + 'T04:00:00.000Z'
OPUS = 'claude-opus-5-5'
OPUS_KEY = 'anthropic/claude-opus-5.5'
# 1e-12 美元/Token：输入 4e-6，缓存读 2e-7，缓存写 5e-6，输出 2e-5。
OPUS_RATES = (4_000_000, 200_000, 5_000_000, 20_000_000)
WI_X = 'wi_20261121_007'


def session(runtime: str, sid: str) -> str:
    return reference('native_session', [runtime, sid])


class Fixture:
    """一个按 schema.sql 建好的汇总库，行直接 INSERT（不依赖线①的写入代码）。"""

    def __init__(self, root: Path):
        self.path = root / 'central.sqlite'
        db = sqlite3.connect(self.path)
        db.executescript(schema_sql())
        db.executemany('INSERT INTO meta(key, value) VALUES(?, ?)',
                       [('schema_version', '1'), ('db_epoch', 'e'), ('next_seq', '1')])
        db.commit()
        db.close()
        self.seq = 0
        self.prices(OPUS_KEY, '2026-11-01', OPUS_RATES)

    def _insert(self, table: str, row: dict) -> None:
        self.seq += 1
        row = {**row, 'change_seq': self.seq}
        db = sqlite3.connect(self.path)
        db.execute(f"INSERT INTO {table}({', '.join(row)}) VALUES({', '.join('?' * len(row))})", list(row.values()))
        db.execute("UPDATE meta SET value=? WHERE key='next_seq'", (str(self.seq + 1),))
        db.commit()
        db.close()

    def usage(self, event_id: str, *, runtime='cc', sid='s1', node='ownermac', source='native',
              granularity='request', agent_id='', turn_id='t1', at=AT, model=OPUS, cwd='/x',
              tokens=(100, 1000, 10, 50), request_id='', final=1) -> dict:
        row = {'node': node, 'runtime': runtime, 'event_id': event_id, 'request_id': request_id, 'source': source,
               'granularity': granularity, 'session_id': sid if source == 'native' else '',
               'session_ref': session(runtime, sid), 'agent_id': agent_id, 'turn_id': turn_id,
               'occurred_at': at, 'model': model, 'cwd': cwd, 'input_uncached': tokens[0], 'cache_read': tokens[1],
               'cache_write': tokens[2], 'output': tokens[3], 'final': final}
        self._insert('usage_events', row)
        return row

    def claim(self, claim_id: str, wi: str, *, scope='turn', sid='s1', runtime='cc', turn_id='t1', agent_id='',
              status='accepted', at=AT):
        self._insert('wi_claims', {'node': 'ownermac', 'runtime': runtime, 'claim_id': claim_id, 'scope': scope,
                                   'session_ref': session(runtime, sid), 'turn_id': turn_id, 'agent_id': agent_id,
                                   'wi': wi, 'status': status, 'occurred_at': at})

    def launch(self, receipt_id: str, kind: str, *, at=AT, sid=None, runtime='cc', **fields):
        row = {'node': 'ownermac', 'receipt_id': receipt_id, 'kind': kind, 'occurred_at': at,
               'session_ref': session(runtime, sid) if sid else None, **fields}
        self._insert('launch_receipts', row)

    def workspace(self, root: str, agent: str, node='ownermac'):
        self._insert('workspaces', {'node': node, 'workspace_root': root, 'agent': agent})

    def origin(self, turn_id: str, origin: str, at: str, *, sid='s1', runtime='cc'):
        db = sqlite3.connect(self.path)
        db.executescript(turns.DDL)
        db.commit()
        db.close()
        self._insert('turn_origins', {'node': 'ownermac', 'runtime': runtime, 'session_ref': session(runtime, sid),
                                      'turn_id': turn_id, 'origin': origin, 'occurred_at': at})

    def session_write(self, sid: str, wi: str, at: str, *, actor='pulse', surface='surface_v1_local_macos'):
        db = sqlite3.connect(self.path)
        db.executescript(outcomes.DDL)
        db.execute('INSERT INTO wi_session_writes VALUES(?,?,?,?)',
                   (outcomes.session_key('ns', actor, sid, surface), at, wi, actor))
        for key, value in (('namespace', 'ns'), ('surfaces', json.dumps([surface])), ('actors', json.dumps([actor]))):
            db.execute('INSERT OR REPLACE INTO wi_export_identity VALUES(?, ?)', (key, value))
        db.commit()
        db.close()

    def prices(self, key: str, effective: str, rates: tuple[int, int, int, int]):
        db = sqlite3.connect(self.path)
        db.execute('INSERT INTO prices VALUES(?,?,?,?,?,?,?)', (key, effective, *rates, 'c' * 64))
        db.commit()
        db.close()

    def events(self, start=date(2026, 11, 1), end=date(2026, 12, 1)) -> list[dict]:
        conn = publish.connect(self.path)
        try:
            return publish.events(conn, start, end)
        finally:
            conn.close()

    def one(self, event_id: str) -> dict:
        found = [e for e in self.events() if e['event_id'] == event_id]
        assert len(found) == 1, found
        return found[0]


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(Path(self.tmp.name))

    # Agent 四级
    def test_agent_levels(self):
        f = self.f
        f.workspace('/w/hestia', 'hestia')
        f.workspace('/w/hestia/deep_1', 'vigil')  # 最长前缀胜
        f.workspace('/w/a_b', 'aki')               # 下划线不是通配
        f.usage('e1', cwd='/w/hestia/src')
        f.usage('e2', cwd='/w/hestia/deep_1/x')
        f.usage('e3', cwd='/w/aXb')
        f.usage('e4', cwd='/u/.local/state/agent-harness/workspaces/org/PULSE/wi_20261121_413/pulse/sub')
        f.usage('e5', cwd='/u/.local/state/agent-harness/workspaces/org/PULSE/wi_20261121_413/workspace.op_1.0',
                sid='s5')
        f.launch('x5', 'execution', at='2026-11-20T03:00:00.000Z', agent='marsh', attempt_ref='a5')
        f.launch('b5', 'session_binding', sid='s5', attempt_ref='a5')
        f.usage('e6', cwd=None, sid='s6')
        f.usage('e7', cwd='/w/hestia', node='oracle-node-1', sid='s7')  # 本节点没登记：取别的节点一致的登记
        agents = {e['event_id']: e['agent'] for e in f.events()}
        self.assertEqual(agents, {'e1': 'hestia', 'e2': 'vigil', 'e3': 'unassigned', 'e4': 'pulse',
                                  'e5': 'marsh', 'e6': 'unassigned', 'e7': 'hestia'})

    def owner(self, wi: str, agent: str):
        db = sqlite3.connect(self.f.path)
        db.executescript(outcomes.DDL)
        db.execute('INSERT OR REPLACE INTO wi_owners VALUES(?, ?)', (wi, agent))
        db.commit()
        db.close()

    def test_agent_from_wi_owner_when_nothing_else_names_it(self):
        f = self.f
        self.owner('wi_20261023_675', 'amanda')
        self.owner('wi_20261121_001', 'emi')
        managed = '/u/.local/state/agent-harness/workspaces/org/CollaborateWithAI/wi_20261023_675/workspace.op_9.0'
        f.usage('ws', cwd=managed + '/sub', sid='m1', turn_id='')          # 受管工作区：该 WI 的 owner
        f.claim('c', 'wi_20261121_001', sid='t1', runtime='codex', turn_id='x')
        f.usage('tmp', cwd='/tmp/backfill/K1/out', sid='t1', runtime='codex', turn_id='x')  # 已归 WI：取其 owner
        f.usage('none', cwd='/tmp/x', sid='n1', turn_id='')                 # 都没有：未识别
        f.launch('x9', 'execution', sid='m2', agent='pulse', at='2026-11-20T01:00:00.000Z')
        f.usage('exec', cwd=managed, sid='m2', turn_id='')                  # 启动记录的执行者优先于 owner
        agents = {e['event_id']: e['agent'] for e in f.events()}
        self.assertEqual(agents, {'ws': 'amanda', 'tmp': 'emi', 'none': 'unassigned', 'exec': 'pulse'})

    def test_agent_from_workspace_registered_on_another_node(self):
        f = self.f
        f.workspace('/Users/owner/Coevolution/PARA/aki-space', 'aki', node='ownermac')
        f.workspace('/w/shared', 'emi', node='ownermac')
        f.workspace('/w/shared', 'ken', node='oracle-node-1')                 # 两节点登记不一致：不取
        f.usage('t1', node='tcloud', sid='t1', cwd='/Users/owner/Coevolution/PARA/aki-space/x', turn_id='')
        f.usage('t2', node='tcloud', sid='t2', cwd='/w/shared', turn_id='')
        # 执行回执归 Pulse、外节点同前缀登记归 Aki：仍是 Pulse（启动记录优先于跨节点登记）
        f.launch('x3', 'execution', sid='t3', agent='pulse', at='2026-11-20T01:00:00.000Z')
        f.usage('t3', node='tcloud', sid='t3', cwd='/Users/owner/Coevolution/PARA/aki-space', turn_id='')
        # 本节点有自己的登记时，不借别的节点的登记
        f.workspace('/srv/other', 'vigil', node='oracle-node-1')
        f.usage('t4', node='oracle-node-1', sid='t4', cwd='/Users/owner/Coevolution/PARA/aki-space', turn_id='')
        agents = {e['event_id']: e['agent'] for e in f.events()}
        self.assertEqual(agents, {'t1': 'aki', 't2': 'unassigned', 't3': 'pulse', 't4': 'unassigned'})

    def test_cross_node_registrations_must_agree_per_node(self):
        """A 的 /shared/project→pulse、B 的 /shared→hestia、C 无登记：C 上 /shared/project/dir 不取（两节点各自最长匹配不一致）。"""
        f = self.f
        f.workspace('/shared/project', 'pulse', node='node-a')
        f.workspace('/shared', 'hestia', node='node-b')
        f.workspace('/solo', 'emi', node='node-a')
        f.usage('c1', node='node-c', sid='c1', cwd='/shared/project/dir', turn_id='')
        f.usage('c2', node='node-c', sid='c2', cwd='/solo/x', turn_id='')          # 只有 A 登记：取 A 的
        f.usage('c3', node='node-c', sid='c3', cwd='/shared/other', turn_id='')    # 只有 B 匹配：取 B 的
        agents = {e['event_id']: e['agent'] for e in f.events()}
        self.assertEqual(agents, {'c1': 'unassigned', 'c2': 'emi', 'c3': 'hestia'})

    def test_agent_from_scheduled_job_launch(self):
        f = self.f
        f.launch('j1', 'scheduled_session', sid='cron1', job_id='aki-19dad28dcce', agent='aki',
                 at='2026-11-20T01:00:00.000Z')
        f.usage('c1', sid='cron1', runtime='cc', cwd='/home/pulse/Coevolution/PARA', turn_id='')
        e = f.one('c1')
        self.assertEqual((e['agent'], e['wi']), ('aki', 'cron:aki-19dad28dcce'))

    def test_agent_from_execution_not_later_than_event(self):
        f = self.f
        f.launch('x1', 'execution', at='2026-11-20T05:00:00.000Z', agent='late', attempt_ref='a1')
        f.launch('b1', 'session_binding', sid='s1', attempt_ref='a1')
        f.launch('x0', 'execution', at='2026-11-20T01:00:00.000Z', agent='early', sid='s1')
        f.usage('e1')
        self.assertEqual(f.one('e1')['agent'], 'early')

    # WI 五级与每级冲突
    def test_subagent_marker_first(self):
        f = self.f
        f.claim('m', 'wi_20261121_413', scope='subagent', agent_id='ag1', turn_id='')
        f.claim('t', 'wi_20261121_001')
        f.usage('e1', agent_id='ag1')
        e = f.one('e1')
        self.assertEqual((e['wi'], e['wi_source'], e['state']), ('wi_20261121_413', 'declared', 'known'))

    def test_subagent_marker_conflict_stops(self):
        f = self.f
        f.claim('m1', 'wi_20261121_413', scope='subagent', agent_id='ag1', turn_id='')
        f.claim('m2', 'wi_20261121_414', scope='subagent', agent_id='ag1', turn_id='')
        f.claim('t', 'wi_20261121_001')
        f.usage('e1', agent_id='ag1')
        e = f.one('e1')
        self.assertEqual((e['wi'], e['wi_source'], e['unlinked_reason'], e['state']),
                         ('unassigned', 'unattributed', 'subagent_marker_conflict', 'unlinked'))

    def test_subagent_without_marker_follows_turn(self):
        f = self.f
        f.claim('t', 'wi_20261121_001', turn_id='t9')
        f.usage('e1', agent_id='ag2', turn_id='t9')
        self.assertEqual(f.one('e1')['wi'], 'wi_20261121_001')

    def test_turn_declaration_and_none(self):
        f = self.f
        f.claim('t', 'none')
        f.usage('e1')
        e = f.one('e1')
        self.assertEqual((e['wi'], e['wi_source'], e['state']), ('none', 'declared', 'known'))

    def test_turn_declaration_conflict_stops(self):
        f = self.f
        f.claim('t1', 'wi_20261121_001')
        f.claim('t2', 'wi_20261121_002')
        f.launch('x', 'execution', sid='s1', wi='wi_20261121_003', at='2026-11-20T01:00:00.000Z')
        f.usage('e1')
        e = f.one('e1')
        self.assertEqual((e['wi'], e['unlinked_reason']), ('unassigned', 'turn_declaration_conflict'))

    def test_sequential_declarations_in_one_turn(self):
        f = self.f
        f.claim('c1', 'wi_20261121_001', at='2026-11-20T01:00:00.000Z')
        f.claim('c2', 'wi_20261121_002', at='2026-11-20T03:00:00.000Z')
        f.usage('before', at='2026-11-20T00:30:00.000Z')
        f.usage('mid', at='2026-11-20T02:00:00.000Z')
        f.usage('after', at='2026-11-20T04:00:00.000Z')
        rows = {e['event_id']: (e['wi'], e['unlinked_reason']) for e in f.events()}
        self.assertEqual(rows, {'before': ('unassigned', 'turn_declaration_conflict'),
                                'mid': ('wi_20261121_001', None), 'after': ('wi_20261121_002', None)})

    def test_continuation_turn_inherits_the_request_declaration(self):
        f = self.f
        f.origin('h1', 'human', '2026-11-20T01:00:00.000Z')
        f.origin('n1', 'continuation', '2026-11-20T02:00:00.000Z')
        f.origin('n2', 'continuation', '2026-11-20T03:00:00.000Z')
        f.origin('h2', 'human', '2026-11-20T04:00:00.000Z')
        f.origin('n3', 'continuation', '2026-11-20T05:00:00.000Z')
        f.origin('d1', 'delivery', '2026-11-20T06:00:00.000Z')
        f.origin('n4', 'continuation', '2026-11-20T07:00:00.000Z')
        f.claim('c1', 'wi_20261121_001', turn_id='h1', at='2026-11-20T01:00:01.000Z')
        f.claim('c2', 'wi_20261121_002', turn_id='n1', at='2026-11-20T02:30:00.000Z')  # 延续回合里改了声明
        f.usage('a', turn_id='n1', at='2026-11-20T02:10:00.000Z')
        f.usage('b', turn_id='n2', at='2026-11-20T03:10:00.000Z')
        f.usage('c', turn_id='h2', at='2026-11-20T04:10:00.000Z')
        f.usage('d', turn_id='n3', at='2026-11-20T05:10:00.000Z')  # 上一个人类请求没声明：不跨过它去继承
        f.usage('e', turn_id='d1', at='2026-11-20T06:10:00.000Z')
        f.usage('g', turn_id='n4', at='2026-11-20T07:10:00.000Z')
        f.usage('x', turn_id='unknown', at='2026-11-20T07:20:00.000Z')
        rows = {e['event_id']: (e['wi'], e['wi_source'], e['unlinked_reason']) for e in f.events()}
        self.assertEqual(rows, {
            'a': ('wi_20261121_002', 'declared', None),     # 回合内本身的声明在前（第 2 级，取它之前最近）
            'b': ('wi_20261121_002', 'declared', None),     # 沿用自 h1 以来最近的声明
            'c': ('undeclared', 'undeclared', 'human_request_undeclared'),   # 人类请求未声明：单列，不算未关联
            'd': ('unassigned', 'unattributed', 'continuation_without_declaration'),
            'e': ('unassigned', 'unattributed', 'delivery_undeclared'),
            'g': ('unassigned', 'unattributed', 'continuation_without_declaration'),
            'x': ('unassigned', 'unattributed', 'no_declaration')})

    def test_undeclared_human_request_is_its_own_state(self):
        f = self.f
        f.origin('h1', 'human', '2026-11-20T01:00:00.000Z')
        f.usage('u', turn_id='h1', at='2026-11-20T01:10:00.000Z')
        f.usage('n', model='nope', turn_id='h1', at='2026-11-20T01:20:00.000Z', request_id='r')  # 未计价优先
        rows = {e['event_id']: (e['wi'], e['state']) for e in f.events()}
        self.assertEqual(rows, {'u': ('undeclared', 'undeclared'), 'n': ('undeclared', 'unpriced')})

    def test_turn_origin_classification(self):
        self.assertEqual(turns.classify('继续  [Host human input reference: x]'), 'human')
        self.assertEqual(turns.classify('<task-notification> <task-id>a'), 'continuation')
        self.assertEqual(turns.classify('[Host session continuation; the previous'), 'continuation')
        self.assertEqual(turns.classify('[系统投递：协作合同，不是人类消息]'), 'delivery')
        self.assertEqual(turns.classify('anything', meta=True), 'continuation')
        self.assertEqual(turns.classify('<task-notification>', trigger='composer'), 'human')
        self.assertEqual(turns.classify_cc_line({'type': 'user', 'promptId': 'p', 'origin': {'kind': 'task-notification'},
                                                 'message': {'content': [{'type': 'text', 'text': 'x'}]}}),
                         'continuation')

    def test_pending_and_rejected_do_not_count(self):
        f = self.f
        f.claim('t1', 'wi_20261121_001', status='pending')
        f.claim('t2', 'wi_20261121_002', status='rejected', turn_id='t2')
        f.usage('e1')
        f.usage('e2', turn_id='t2')
        f.usage('e3', turn_id='t3')
        f.usage('e4', turn_id='')
        reasons = {e['event_id']: e['unlinked_reason'] for e in f.events()}
        self.assertEqual(reasons, {'e1': 'declaration_pending', 'e2': 'declaration_rejected',
                                   'e3': 'no_declaration', 'e4': 'no_turn'})
        self.assertEqual({e['launch_evidence'] for e in f.events()}, {'none'})

    def test_launch_record_classifies(self):
        f = self.f
        f.launch('x1', 'execution', at='2026-11-20T01:00:00.000Z', wi='wi_20261121_001', attempt_ref='a1')
        f.launch('b1', 'session_binding', sid='s1', attempt_ref='a1')
        f.launch('x2', 'execution', at='2026-11-20T03:00:00.000Z', wi='wi_20261121_002', sid='s1')
        f.launch('x3', 'execution', at='2026-11-20T09:00:00.000Z', wi='wi_20261121_009', sid='s1')  # 晚于事件
        f.usage('e1')
        e = f.one('e1')
        self.assertEqual((e['wi'], e['wi_source']), ('wi_20261121_002', 'classified'))

    def test_launch_record_conflict_and_after_event(self):
        f = self.f
        f.launch('x1', 'execution', at='2026-11-20T03:00:00.000Z', wi='wi_20261121_001', sid='s1')
        f.launch('x2', 'scheduled_session', at='2026-11-20T03:00:00.000Z', wi='wi_20261121_002', sid='s1',
                 job_id='j1')
        f.usage('e1')
        f.launch('x3', 'execution', at='2026-11-20T09:00:00.000Z', wi='wi_20261121_009', sid='s2')
        f.usage('e2', sid='s2')
        rows = {e['event_id']: e for e in f.events()}
        self.assertEqual((rows['e1']['wi'], rows['e1']['unlinked_reason']), ('unassigned', 'launch_wi_conflict'))
        self.assertEqual((rows['e2']['wi'], rows['e2']['launch_evidence']), ('unassigned', 'after_event'))

    def test_scheduled_job_and_conflict(self):
        f = self.f
        f.launch('j1', 'scheduled_session', sid='s1', job_id='nightly', job_name='夜跑', entrypoint='automation')
        f.usage('e1')
        f.launch('j2', 'scheduled_session', sid='s2', job_id='a')
        f.launch('j3', 'scheduled_session', sid='s2', job_id='b')
        f.usage('e2', sid='s2')
        rows = {e['event_id']: e for e in f.events()}
        self.assertEqual((rows['e1']['wi'], rows['e1']['wi_source'], rows['e1']['entrypoint']),
                         ('cron:nightly', 'scheduled_job', 'automation'))
        self.assertEqual((rows['e2']['wi'], rows['e2']['unlinked_reason'], rows['e2']['launch_evidence']),
                         ('unassigned', 'scheduled_job_conflict', 'without_wi'))

    def test_holder_supplement_whole_session(self):
        f = self.f
        f.session_write('s1', 'wi_20261121_005', '2026-11-20T04:30:00.000Z')
        f.launch('j', 'scheduled_session', sid='s1', job_id='nightly')
        f.usage('e1', turn_id='t1')
        f.usage('e2', turn_id='t2', at='2026-11-20T06:00:00.000Z')
        f.claim('c', 'wi_20261121_001', turn_id='t2')       # 声明压过会话写入
        rows = {e['event_id']: e for e in f.events()}
        self.assertEqual((rows['e1']['wi'], rows['e1']['wi_source']), ('wi_20261121_005', 'holder_supplement'))
        self.assertEqual((rows['e2']['wi'], rows['e2']['wi_source']), ('wi_20261121_001', 'declared'))

    def test_holder_supplement_by_turn_window(self):
        f = self.f
        f.usage('a', turn_id='t1', at='2026-11-20T01:00:00.000Z')
        f.usage('b', turn_id='t2', at='2026-11-20T02:00:00.000Z')
        f.usage('c', turn_id='t3', at='2026-11-20T03:00:00.000Z')
        f.usage('d', turn_id='t4', at='2026-11-20T04:00:00.000Z')
        f.session_write('s1', 'wi_20261121_001', '2026-11-20T01:30:00.000Z')   # t1 窗口
        f.session_write('s1', 'wi_20261121_002', '2026-11-20T03:10:00.000Z')   # t3 窗口
        f.session_write('s1', 'wi_20261121_001', '2026-11-20T03:20:00.000Z')   # t3 再写另一个：冲突
        f.session_write('s1', 'wi_20261121_002', '2026-11-20T05:00:00.000Z')   # t4 窗口（到会话末尾）
        rows = {e['event_id']: (e['wi'], e['wi_source'], e['unlinked_reason']) for e in f.events()}
        self.assertEqual(rows, {'a': ('wi_20261121_001', 'holder_supplement', None),
                                'b': ('unassigned', 'unattributed', 'no_declaration'),
                                'c': ('unassigned', 'unattributed', 'holder_wi_conflict'),
                                'd': ('wi_20261121_002', 'holder_supplement', None)})

    def test_entrypoint_priority(self):
        f = self.f
        for sid in ('a', 'b', 'c', 'd'):
            f._insert('session_entrypoints', {'node': 'ownermac', 'runtime': 'cc', 'session_ref': session('cc', sid),
                                              'entrypoint': 'sdk', 'source': 'cc_entrypoint'})
        f.launch('xa', 'execution', sid='a', entrypoint='telegram', at='2026-11-20T01:00:00.000Z')   # 渠道优先
        f.launch('ja', 'scheduled_session', sid='a', job_id='j', entrypoint='automation', at='2026-11-20T00:00:00.000Z')
        f.launch('jb', 'scheduled_session', sid='b', job_id='j', entrypoint='automation')             # 定时任务次之
        f.usage('ea', sid='a'); f.usage('eb', sid='b'); f.usage('ec', sid='c')                         # c：原生入口
        f.usage('ee', sid='e')                                                                          # 都没有
        rows = {e['event_id']: e['entrypoint'] for e in f.events()}
        self.assertEqual(rows, {'ea': 'telegram', 'eb': 'automation', 'ec': 'sdk', 'ee': 'unknown'})

    def test_entrypoint(self):
        f = self.f
        f.launch('x1', 'execution', at='2026-11-20T01:00:00.000Z', entrypoint='feishu', attempt_ref='a1')
        f.launch('b1', 'session_binding', sid='s1', attempt_ref='a1')
        f.usage('e1')
        f.usage('e2', sid='s2')
        rows = {e['event_id']: e['entrypoint'] for e in f.events()}
        self.assertEqual(rows, {'e1': 'feishu', 'e2': 'unknown'})

    # 原生压过 host；无原生时 CC 取 host turn、其他 Runtime 取 host request；host request 只取 final=1
    def test_native_beats_host(self):
        f = self.f
        f.usage('n1', sid='s1')
        f.usage('h1', sid='s1', source='host')
        self.assertEqual([e['event_id'] for e in f.events()], ['n1'])

    def test_cc_host_turn_beats_request(self):
        f = self.f
        f.usage('t1', sid='s2', source='host', granularity='turn')
        f.usage('r1', sid='s2', source='host', granularity='request')
        f.usage('r2', sid='s3', source='host', granularity='request')            # 无 turn：request 计入
        f.usage('r3', sid='s3', source='host', granularity='request', final=0)   # 消息开始时的行不计
        self.assertEqual(sorted(e['event_id'] for e in f.events()), ['r2', 't1'])

    def test_other_runtime_host_request_beats_turn(self):
        f = self.f
        f.usage('r1', runtime='codex', sid='s2', source='host', granularity='request')
        f.usage('t1', runtime='codex', sid='s2', source='host', granularity='turn')
        f.usage('r2', runtime='pi', sid='s3', source='host', granularity='request', final=0)
        f.usage('t2', runtime='pi', sid='s3', source='host', granularity='turn')  # 只有 final=0 的 request：取 turn
        self.assertEqual(sorted(e['event_id'] for e in f.events()), ['r1', 't2'])

    def test_host_turn_model_missing_from_native_is_added(self):
        f = self.f
        f.prices('anthropic/claude-haiku-5.1', '2026-11-01', (1, 1, 1, 1))
        f.claim('c', WI_X, turn_id='t7')
        f.workspace('/w/hestia', 'hestia')
        f.usage('n1', at='2026-11-20T03:00:00.000Z', turn_id='t7', cwd='/w/hestia/a')
        f.usage('n2', at='2026-11-20T05:00:00.000Z', turn_id='t8', cwd='/w/hestia/a')
        f.usage('haiku', source='host', granularity='turn', model='claude-haiku-5-1',
                at='2026-11-20T04:00:00.000Z', turn_id='', cwd=None, tokens=(1, 2, 3, 4))
        f.usage('opus', source='host', granularity='turn', at='2026-11-20T04:00:00.000Z', turn_id='')
        f.usage('early', source='host', granularity='turn', model='claude-haiku-5-1',
                at='2026-11-20T01:00:00.000Z', turn_id='', cwd=None, request_id='e')
        rows = {e['event_id']: e for e in f.events()}
        self.assertEqual(sorted(rows), ['early', 'haiku', 'n1', 'n2'])      # 同模型 opus 以原生为准
        haiku = rows['haiku']
        self.assertEqual((haiku['wi'], haiku['agent'], haiku['session_id'], haiku['usd_e12']),
                         (WI_X, 'hestia', 's1', 10))
        self.assertEqual(rows['early']['wi'], WI_X)                          # 早于所有原生记录：沿用最早一条

    # 价表按生效日取价；未计价原因
    def test_price_by_effective_day(self):
        f = self.f
        f.prices(OPUS_KEY, '2026-11-15', (8_000_000, 400_000, 10_000_000, 40_000_000))
        f.usage('old', at='2026-11-10T04:00:00.000Z', tokens=(1, 0, 0, 0))
        f.usage('new', at='2026-11-15T00:00:00.000Z', tokens=(1, 0, 0, 0))   # 北京 11-15 08:00
        f.usage('edge', at='2026-11-14T15:59:59.000Z', tokens=(1, 0, 0, 0))  # 北京 11-14 23:59
        rows = {e['event_id']: e['usd_e12'] for e in f.events()}
        self.assertEqual(rows, {'old': 4_000_000, 'new': 8_000_000, 'edge': 4_000_000})

    def test_amount_and_unpriced_reasons(self):
        f = self.f
        f.prices('openai/gpt-x', '2026-11-01', (1, -1, -1, 2))
        f.usage('e1', tokens=(100, 1000, 10, 50))
        f.usage('e2', model=None)
        f.usage('e3', tokens=(1, None, 0, 1))
        f.usage('e4', model='unknown-model')
        f.usage('e5', model='gpt-x', tokens=(1, 0, 0, 1))
        f.usage('e6', model='gpt-x', tokens=(1, 5, 0, 1))
        f.usage('e7', at='2026-10-31T04:00:00.000Z')  # 早于首个生效日
        rows = {e['event_id']: e for e in f.events(start=date(2026, 10, 1))}
        self.assertEqual(rows['e1']['usd_e12'], 100 * 4_000_000 + 1000 * 200_000 + 10 * 5_000_000 + 50 * 20_000_000)
        self.assertEqual(rows['e5']['usd_e12'], 3)
        self.assertEqual({k: v['unpriced_reason'] for k, v in rows.items()},
                         {'e1': None, 'e2': 'model_unknown', 'e3': 'quantity_unknown', 'e4': 'price_unavailable',
                          'e5': None, 'e6': 'cache_price_unavailable', 'e7': 'price_unavailable'})
        for row in rows.values():
            self.assertEqual(row['state'] == 'unpriced', row['usd_e12'] is None)

    # 三种状态互斥且覆盖；六维各自与联合守恒
    def _mixed(self):
        f = self.f
        f.workspace('/w/hestia', 'hestia')
        f.claim('t', 'wi_20261121_001')
        f.launch('j', 'scheduled_session', sid='s3', job_id='nightly', entrypoint='automation')
        for i in range(30):
            f.usage(f'e{i}', sid=f's{i % 4}', runtime=('cc', 'codex', 'pi')[i % 3],
                    node=('ownermac', 'oracle-node-1')[i % 2], cwd=('/w/hestia/a', '/q')[i % 2],
                    model=(OPUS, 'nope', None)[i % 5 % 3], turn_id=('t1', 't2')[i % 2],
                    tokens=(i, 2 * i, None if i % 7 == 0 else 3, 4))
        return f.events()

    def test_states_partition_all_events(self):
        rows = self._mixed()
        conn = publish.connect(self.f.path)
        try:
            usage_count = conn.execute('SELECT count(*) FROM v_usage').fetchone()[0]
            event_count = conn.execute('SELECT count(*) FROM v_event').fetchone()[0]
            states = dict(conn.execute('SELECT state, count(*) FROM v_event GROUP BY state').fetchall())
        finally:
            conn.close()
        self.assertEqual(usage_count, event_count)
        self.assertEqual(len(rows), event_count)
        self.assertEqual(set(states), {'known', 'unlinked', 'unpriced'})  # 夹具三种都有
        self.assertEqual(sum(states.values()), event_count)

    def test_staged_events_equal_the_view(self):
        """events() 逐层落表只为不重算，结果必须与直接查 v_event 一模一样。"""
        f = self.f
        f.origin('h1', 'human', '2026-11-20T00:00:00.000Z')
        f.origin('t1', 'continuation', '2026-11-20T00:30:00.000Z')
        self._mixed()
        f.usage('u', turn_id='h1', at='2026-11-20T00:10:00.000Z', request_id='u')
        staged = f.events()
        conn = publish.connect(f.path)
        try:
            direct = [dict(zip(publish.EVENT_COLUMNS, row)) for row in conn.execute(
                'SELECT ' + ', '.join(publish.EVENT_COLUMNS) + ' FROM v_event')]
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_temp_master WHERE name LIKE 't_event_%'")
                             .fetchone()[0], 0)   # 临时表用完即删
        finally:
            conn.close()
        for row in direct:
            row['model'] = row['model'] or 'unknown'
        key = lambda r: (r['node'], r['runtime'], r['event_id'], r['request_id'], r['source'])
        self.assertEqual(sorted(staged, key=key), sorted(direct, key=key))
        self.assertIn('undeclared', {r['wi'] for r in staged})

    def test_events_read_one_snapshot_while_another_connection_commits(self):
        """分层物化的两步之间，别的连接提交了回合来源与 WI 声明：本次结果仍是开始时的快照。"""
        from unittest.mock import patch
        f = self.f
        f.usage('e1', turn_id='h1', at='2026-11-20T01:10:00.000Z')
        conn = publish.connect(f.path)            # 汇总库转 WAL；读事务给一致快照
        try:
            before = publish.events(conn, date(2026, 11, 1), date(2026, 12, 1))
            original = publish._view_body
            fired = []

            def body(name):
                if name == 'v_event_base' and not fired:
                    fired.append(1)          # 第一阶段（t_event_raw）已物化，第二阶段之前插入并提交
                    f.origin('h1', 'human', '2026-11-20T01:00:00.000Z')
                    f.claim('c1', 'wi_20261121_009', turn_id='h1', at='2026-11-20T01:00:01.000Z')
                return original(name)

            with patch.object(publish, '_view_body', body):
                during = publish.events(conn, date(2026, 11, 1), date(2026, 12, 1))
            after = publish.events(conn, date(2026, 11, 1), date(2026, 12, 1))
        finally:
            conn.close()
        self.assertEqual(fired, [1])
        self.assertEqual(during, before)                                 # 没有混用两个快照
        self.assertEqual([(r['wi'], r['wi_source']) for r in before], [('unassigned', 'unattributed')])
        self.assertEqual([(r['wi'], r['wi_source']) for r in after], [('wi_20261121_009', 'declared')])

    def test_conservation_by_every_dimension(self):
        rows = self._mixed()
        measures = ('input_uncached', 'cache_read', 'cache_write', 'output', 'usd_e12')

        def totals(subset):
            return (len(subset),) + tuple(sum(r[m] for r in subset if r[m] is not None) for m in measures)

        whole = totals(rows)
        dims = list(publish.DIMENSIONS)
        for size in (1, len(dims)):
            for combo in combinations(dims, size):
                groups = {}
                for r in rows:
                    groups.setdefault(tuple(r[d] for d in combo), []).append(r)
                summed = tuple(map(sum, zip(*(totals(g) for g in groups.values()))))
                self.assertEqual(summed, whole, combo)


if __name__ == '__main__':
    unittest.main()
