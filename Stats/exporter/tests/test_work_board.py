"""「在办」看板：分组、计数守恒与卡住判据。"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'Stats/exporter'))
from pulse_stats import wi_facts, work_board as b

NOW = datetime(2026, 10, 26, 0, tzinfo=timezone.utc)


def at(days=0, hours=0, minutes=0):
    return (NOW - timedelta(days=days, hours=hours, minutes=minutes)).isoformat()


def item(wi, status, updated, owner='pulse', holder=None, summary=None, created=None):
    return {'id': wi, 'title': wi + ' 标题\n第二行', 'owner': owner, 'holder': holder, 'status': status,
            'summary': summary, 'created_at': created or updated, 'updated_at': updated, 'closed_at': None,
            'main_only': False}


def contract(cid, status, steps, sender='hestia', recipients=('pulse',), next_step=None, main_only=False,
             purpose='GROK 周配额纳入 Stats。另附说明', deadline=None):
    value = {'contract_id': cid, 'kind': 'request', 'main_only': main_only, 'status': status,
             'offered_by': {'actor': sender, 'surface': 'surface_v1_local_macos'},
             'recipients': [{'actor': r} for r in recipients], 'purpose': purpose,
             'work_item_id': 'wi_20261015_422', 'work_item_title': '经营 PULSE 平台\n细节',
             'timeline': [{'at': when, 'action': action, 'actor': actor, **({'reason': why} if why else {})}
                          for when, action, actor, why in steps],
             'next': next_step, 'required': True, 'settled': status in ('withdrawn', 'rejected')}
    if deadline:
        value['deadline'] = deadline
    return value


def recipient(*actors, actions=('accept', 'reject')):
    return {'role': 'recipient', 'actors': list(actors), 'actions': list(actions)}


def requester(*actors, actions=('adopt',)):
    return {'role': 'requester', 'actors': list(actors), 'actions': list(actions)}


ITEMS = [
    item('wi_pulse_old', 'active', at(days=10), summary='PULSE 生产态标准 3：存量 WI 清零'),
    item('wi_pulse_new', 'active', at(hours=17)),
    item('wi_hestia_held', 'active', at(hours=3), owner='pulse', holder='hestia'),
    item('wi_hestia_draft', 'draft', at(days=4), owner='hestia'),
    item('wi_amanda', 'active', at(hours=7), owner='amanda'),
    item('wi_ken', 'stopped', at(hours=1), owner='ken'),
    item('wi_done', 'completed', at(hours=1)),
    item('wi_gone', 'terminated', at(hours=1)),
]
CONTRACTS = [
    contract('op_wait_old', 'pending', [(at(days=5), 'offered', 'hestia', None)], next_step=recipient('pulse'),
             main_only=True),
    contract('op_wait_new', 'pending', [(at(hours=5, minutes=59), 'offered', 'hestia', None)],
             next_step=recipient('pulse')),
    contract('op_back', 'returned', [(at(days=1), 'offered', 'pulse', None), (at(hours=2), 'returned', 'hestia', '部分成果')],
             sender='pulse', recipients=('hestia',), purpose='AGENTS.md 写明声明 WI',
             next_step=requester('pulse', actions=('resend', 'reassign', 'withdraw', 'adopt_partial'))),
    contract('op_done', 'completed', [(at(days=1), 'offered', 'pulse', None), (at(hours=4), 'completed', 'hestia', '已修')],
             sender='pulse', recipients=('hestia',), purpose='限制 HA 重复日志', next_step=requester('pulse')),
    contract('op_self', 'accepted', [(at(days=5), 'offered', 'hestia', None), (at(days=5), 'accepted', 'hestia', None)],
             recipients=('hestia',), purpose='WI632 子任务：手机调查', next_step=recipient('hestia', actions=('complete', 'return'))),
    contract('op_rejected', 'rejected', [(at(hours=3), 'offered', 'aki', None), (at(hours=2), 'rejected', 'pulse', '不归我')],
             sender='aki', purpose='请重取字幕'),
    contract('op_withdrawn', 'withdrawn', [(at(hours=3), 'offered', 'aki', None), (at(hours=2), 'withdrawn', 'aki', None)],
             sender='aki'),
]


def facts(items=ITEMS, contracts=CONTRACTS):
    return {'schema': wi_facts.SCHEMA, 'generated_at': at(minutes=5), 'namespace': 'n', 'items': list(items),
            'collaborations': list(contracts), 'unavailable_items': [], 'coverage': 'complete',
            'source': 'journal', 'read_only': True}


def board(document=None, now=NOW):
    rows = b.rows(document or facts(), now)
    head = rows[0]
    agents = [r for r in rows if r['kind'] == 'agent']
    entries = {r['id']: r for r in rows if r['kind'] in ('wi', 'contract')}
    return head, agents, entries, rows


class WorkBoardTests(unittest.TestCase):
    def test_counts_are_conserved_and_every_item_appears_once(self):
        head, agents, entries, rows = board()
        in_flight = [i['id'] for i in ITEMS if i['status'] in b.IN_FLIGHT]
        shown = [c['contract_id'] for c in CONTRACTS if c['status'] != 'withdrawn']
        self.assertEqual((head['wi_total'], head['contract_total'], head['contracts_available']),
                         (len(in_flight), len(shown), True))
        ids = [r['id'] for r in rows if r['kind'] in ('wi', 'contract')]
        self.assertEqual(sorted(ids), sorted(in_flight + shown))
        self.assertEqual(sum(a['wi_count'] for a in agents), head['wi_total'])
        self.assertEqual(sum(a['contract_count'] for a in agents), head['contract_total'])
        self.assertNotIn('op_withdrawn', entries)
        # Agent rows precede exactly their own entries.
        current = None
        for row in rows[1:]:
            if row['kind'] == 'agent':
                current = row['agent']
            else:
                self.assertEqual(row['agent'], current)

    def test_contracts_go_to_whoever_moves_next_and_wis_to_their_holder(self):
        _, _, entries, _ = board()
        self.assertEqual(entries['op_wait_old']['agent'], 'pulse')        # recipient accepts
        self.assertEqual(entries['op_self']['agent'], 'hestia')           # recipient is working
        self.assertEqual(entries['op_back']['agent'], 'pulse')            # requester settles a hand-back
        self.assertEqual(entries['op_done']['agent'], 'pulse')            # requester adopts the result
        self.assertEqual(entries['op_rejected']['agent'], 'aki')          # no next step: requester
        self.assertEqual(entries['wi_hestia_held']['agent'], 'hestia')    # holder before owner
        self.assertEqual(entries['wi_amanda']['agent'], 'amanda')

    def test_one_line_per_item_with_direction_status_and_elapsed_time(self):
        _, _, entries, _ = board()
        wait = entries['op_wait_old']
        self.assertEqual((wait['line'], wait['status'], wait['main_only']), ('Hestia：GROK 周配额纳入 Stats', '待接 5 天', True))
        self.assertEqual((entries['op_back']['line'], entries['op_back']['status']), ('Hestia 交回：AGENTS.md 写明声明 WI', '交回待处理 2 时'))
        self.assertEqual((entries['op_done']['line'], entries['op_done']['status']), ('Hestia 完成：限制 HA 重复日志', '完成待采用 4 时'))
        self.assertEqual((entries['op_self']['line'], entries['op_self']['status']), ('自派：WI632 子任务：手机调查', '办理中 5 天'))
        self.assertEqual(entries['op_rejected']['status'], '已拒绝')
        self.assertEqual((entries['wi_pulse_old']['line'], entries['wi_pulse_old']['status']), ('PULSE 生产态标准 3：存量 WI 清零', '10 天没推进'))
        self.assertEqual((entries['wi_pulse_new']['line'], entries['wi_pulse_new']['status']), ('wi_pulse_new 标题', '17 时前'))
        self.assertEqual(entries['wi_hestia_draft']['status'], '草案 · 4 天没推进')
        self.assertEqual(entries['wi_ken']['status'], '已停 · 1 时前')
        # Evidence stays in the row for the expanded view.
        self.assertTrue(entries['op_back']['timeline'].splitlines()[-1].endswith('退回 · Hestia · 部分成果'))
        self.assertEqual(entries['op_back']['next'], '发起方 Pulse：可重发／改派／撤回／采用部分成果')
        self.assertEqual(entries['op_back']['work_item_title'], '经营 PULSE 平台')

    def test_stuck_rules(self):
        _, _, entries, _ = board()
        stuck = {k for k, r in entries.items() if r['stuck']}
        self.assertEqual(stuck, {'op_wait_old', 'op_back', 'op_done', 'wi_pulse_old', 'wi_hestia_draft'})
        # Six hours to the minute pending, a deadline, seven versus eight days, three versus four days of draft.
        later = NOW + timedelta(minutes=2)
        _, _, entries, _ = board(now=later)
        self.assertTrue(entries['op_wait_new']['stuck'])
        due = contract('op_due', 'pending', [(at(minutes=10), 'offered', 'hestia', None)], next_step=recipient('pulse'),
                       deadline=at(minutes=1))
        edge = [item('wi_seven', 'active', at(days=7, hours=23)), item('wi_eight', 'active', at(days=8)),
                item('wi_draft3', 'draft', at(days=3, hours=23)), item('wi_draft4', 'draft', at(days=4))]
        _, _, entries, _ = board(facts(edge, [due]))
        self.assertEqual({k for k, r in entries.items() if r['stuck']}, {'op_due', 'wi_eight', 'wi_draft4'})

    def test_agents_with_stuck_items_first_then_most_recent_activity(self):
        _, agents, _, rows = board()
        self.assertEqual([a['agent'] for a in agents], ['pulse', 'hestia', 'ken', 'aki', 'amanda'])
        pulse = agents[0]
        self.assertEqual((pulse['wi_count'], pulse['contract_count'], pulse['stuck_count']), (2, 4, 4))
        self.assertEqual(pulse['summary'], '待接合同 2（最久 5 天） · 交回待处理 1 · 完成待采用 1 · 停滞 WI 1')
        self.assertEqual(next(a for a in agents if a['agent'] == 'amanda')['summary'], '最近 7 时前')
        # Within an Agent: stuck first, contracts before WIs, longest waiting first.
        order = [r['id'] for r in rows if r.get('agent') == 'pulse' and r['kind'] != 'agent']
        self.assertEqual(order, ['op_wait_old', 'op_back', 'op_done', 'wi_pulse_old', 'op_wait_new', 'wi_pulse_new'])

    def test_malformed_contracts_keep_the_work_items(self):
        for broken in ([{'contract_id': 'op_bad'}], [CONTRACTS[0], CONTRACTS[0]],
                       [contract('op_x', 'pending', [(at(), 'offered', 'hestia', None)], main_only='yes')], None):
            document = facts(contracts=broken or [])
            if broken is None:
                document.pop('collaborations')
            head, agents, entries, _ = board(document)
            self.assertEqual((head['contracts_available'], head['contract_total']), (False, 0))
            self.assertEqual(head['wi_total'], 6)
            self.assertFalse(any(r['kind'] == 'contract' for r in entries.values()))

    def test_first_sentence_is_one_line(self):
        self.assertEqual(b.first_sentence('第一句。第二句'), '第一句')
        self.assertEqual(b.first_sentence('上一行\n下一行'), '上一行')
        self.assertEqual(b.first_sentence('被内核截断的目的…'), '被内核截断的目的')
        self.assertEqual(len(b.first_sentence('长' * 200)), b.LINE_LIMIT)


class LiveBoardTests(unittest.TestCase):
    def test_live_snapshot_publishes_the_board_from_native_facts(self):
        from Stats.exporter.tests.test_live import LiveFixture, FakeRunner, ack_result, systemd_result, NOW as LIVE_NOW
        with tempfile.TemporaryDirectory() as temp:
            fixture = LiveFixture(Path(temp))
            path = Path(temp) / 'wi-facts-v1.json'
            document = facts()
            document['generated_at'] = (LIVE_NOW - timedelta(minutes=5)).isoformat()
            path.write_text(json.dumps(document))
            os.chmod(path, 0o600)
            fixture.paths = replace(fixture.paths, wi_facts=path)
            snapshot = json.loads(fixture.service(FakeRunner(ack=ack_result(), systemd=systemd_result())).render_json())
        metric = next(m for m in snapshot['metrics'] if m['id'] == b.METRIC_ID)
        self.assertEqual(metric['status'], 'available')
        head = metric['value'][0]
        self.assertEqual((head['wi_total'], head['contract_total']), (6, 6))
        self.assertFalse(any(m['id'] == 'automation.collaboration_contracts' for m in snapshot['metrics']))


if __name__ == '__main__':
    unittest.main()
