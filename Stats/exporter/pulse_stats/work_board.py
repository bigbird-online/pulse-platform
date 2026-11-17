"""「在办」看板：在办 WI 与协作合同，按“下一步该谁动”分到各 Agent 名下。

只读 harness.stats_facts/v1 的 ``items`` 与 ``collaborations``，不推断合同状态。
合同归到 ``next.actors`` 的第一位：待接、办理中是接收方，已交回、已完成待采用
是发起方；没有下一步（已拒绝等）时归发起方。WI 归持有人，无持有人归负责人。
每张合同、每件 WI 恰好出现一次；已撤回的合同不显示。

快照值是扁平行：一行 ``board`` 总数，每个 Agent 一行 ``agent``，其后是该
Agent 的 ``contract`` / ``wi`` 行。行序即显示顺序。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Dict, List, Mapping, Optional

from .live_contract import DISPLAY_TZ, WI_STALLED_DAYS, WORK_BOARD_DRAFT_DAYS

METRIC_ID = 'work.board'
# 待接超过这么久算卡住。
PENDING_LIMIT = timedelta(hours=6)
IN_FLIGHT = {'draft', 'active', 'stopped', 'transferring'}
LINE_LIMIT = 80
ACTION = {'offered': '发出', 'accepted': '接单', 'returned': '退回', 'rejected': '拒绝',
          'completed': '完成', 'withdrawn': '撤回', 'reassigned': '改派'}
OPTION = {'accept': '接单', 'reject': '拒绝', 'complete': '完成', 'return': '退回', 'resend': '重发',
          'reassign': '改派', 'withdraw': '撤回', 'adopt': '采用成果', 'adopt_partial': '采用部分成果'}
# 发起方一侧看到的结果动词。
RESULT = {'returned': '交回', 'completed': '完成', 'rejected': '拒绝'}
STATE_RANK = {'pending': 0, 'accepted': 1, 'returned': 2, 'completed': 3, 'rejected': 4}
_SENTENCE = re.compile(r'[。！？!?\n]')


def _date(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError('timestamp required')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('unqualified timestamp')
    return stamp.astimezone(timezone.utc)


def _name(actor: str) -> str:
    return actor[:1].upper() + actor[1:]


def _clock(value: str) -> str:
    return _date(value).astimezone(DISPLAY_TZ).strftime('%m-%d %H:%M')


def span(delta: timedelta) -> str:
    """Coarse elapsed time: 分 under an hour, 时 under a day, then 天."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 3600:
        return f'{seconds // 60} 分'
    if seconds < 86400:
        return f'{seconds // 3600} 时'
    return f'{seconds // 86400} 天'


def first_sentence(text: str) -> str:
    """Up to the first sentence end or line break, without the kernel's trailing ellipsis."""
    head = _SENTENCE.split(text.strip(), maxsplit=1)[0].strip().rstrip('…')
    return head[:LINE_LIMIT - 1] + '…' if len(head) > LINE_LIMIT else head


def overdue(contract: Mapping, now: datetime) -> bool:
    """Still pending after six hours, or past its own deadline."""
    if contract['status'] != 'pending':
        return False
    if contract.get('deadline') and _date(contract['deadline']) <= now:
        return True
    return now - _date(contract['timeline'][0]['at']) > PENDING_LIMIT


def _next_text(step: Optional[Mapping]) -> str:
    if step is None:
        return '无需再处理'
    who = '、'.join(_name(a) for a in step['actors'])
    role = '发起方 ' if step['role'] == 'requester' else ''
    return f"{role}{who}：可{'／'.join(OPTION[o] for o in step['actions'])}"


def _timeline(contract: Mapping) -> str:
    lines = []
    for item in contract['timeline']:
        line = f"{_clock(item['at'])} {ACTION[item['action']]} · {_name(item['actor'])}"
        lines.append(line + (' · ' + item['reason'] if item.get('reason') else ''))
    return '\n'.join(lines)


def contract_row(contract: Mapping, now: datetime) -> Dict[str, object]:
    status = contract['status']
    if status not in STATE_RANK:
        raise ValueError('unknown contract status')
    main_only = contract.get('main_only', False)
    if type(main_only) is not bool:
        raise TypeError('main_only')
    sender = contract['offered_by']['actor']
    receivers = [r['actor'] for r in contract['recipients']]
    step = contract.get('next')
    agent = step['actors'][0] if step else sender
    timeline = contract['timeline']
    offered = _date(timeline[0]['at'])
    last = _date(timeline[-1]['at'])
    requester_side = step['role'] == 'requester' if step else True
    awaiting = requester_side and step is not None and status in ('returned', 'completed')
    stuck = overdue(contract, now) or awaiting
    if receivers == [sender]:
        prefix = '自派'
    elif not requester_side:
        prefix = _name(sender)
    else:
        prefix = '、'.join(_name(r) for r in receivers) + ' ' + RESULT.get(status, '处理中')
    if status == 'pending':
        right = '待接 ' + span(now - offered)
    elif status == 'accepted':
        right = '办理中 ' + span(now - last)
    elif awaiting:
        right = ('交回待处理 ' if status == 'returned' else '完成待采用 ') + span(now - last)
    else:
        right = {'returned': '已交回', 'completed': '已完成', 'rejected': '已拒绝'}[status]
    purpose = contract.get('purpose') or contract.get('work_item_title') or ''
    title = contract.get('work_item_title') or ''
    return dict(kind='contract', id=contract['contract_id'], agent=agent,
                line=prefix + '：' + first_sentence(purpose), status=right, stuck=stuck,
                main_only=main_only, state=status,
                work_item_id=contract.get('work_item_id') or '',
                work_item_title=title.splitlines()[0][:64] if title else '',
                route=_name(sender) + ' → ' + '、'.join(_name(r) for r in receivers),
                purpose=purpose, next=_next_text(step), timeline=_timeline(contract),
                last_at=last.isoformat(), _age=(now - (offered if status == 'pending' else last)).total_seconds())


def wi_row(item: Mapping, now: datetime) -> Dict[str, object]:
    updated = _date(item['updated_at'])
    quiet = now - updated
    days = quiet.days
    draft = item['status'] == 'draft'
    stuck = days > (WORK_BOARD_DRAFT_DAYS if draft else WI_STALLED_DAYS)
    head = {'draft': '草案 · ', 'stopped': '已停 · '}.get(item['status'], '')
    right = head + (f'{days} 天没推进' if stuck else span(quiet) + '前')
    title = item['title'].splitlines()[0] if item['title'] else item['id']
    text = (item.get('summary') or '').strip().splitlines()
    line = text[0] if text else title
    return dict(kind='wi', id=item['id'], agent=item.get('holder') or item['owner'],
                line=line[:LINE_LIMIT - 1] + '…' if len(line) > LINE_LIMIT else line,
                status=right, stuck=stuck, main_only=False, state=item['status'],
                work_item_id=item['id'], work_item_title=title, owner=item['owner'],
                holder=item.get('holder') or '', updated_at=updated.isoformat(),
                created_at=_date(item['created_at']).isoformat(),
                last_at=updated.isoformat(), _age=quiet.total_seconds())


def _summary(items: List[Mapping]) -> str:
    contracts = [r for r in items if r['kind'] == 'contract']
    parts = []
    pending = [r for r in contracts if r['state'] == 'pending']
    if pending:
        oldest = span(timedelta(seconds=max(r['_age'] for r in pending)))
        parts.append(f"待接合同 {len(pending)}（{'最久 ' if len(pending) > 1 else ''}{oldest}）")
    for state, label in (('accepted', '办理中合同'), ('returned', '交回待处理'), ('completed', '完成待采用')):
        count = sum(1 for r in contracts if r['state'] == state and (state == 'accepted' or r['stuck']))
        if count:
            parts.append(f'{label} {count}')
    stalled = sum(1 for r in items if r['kind'] == 'wi' and r['stuck'])
    if stalled:
        parts.append(f'停滞 WI {stalled}')
    return ' · '.join(parts)


def rows(facts: Mapping, now: datetime) -> List[Dict[str, object]]:
    """Validated facts to ordered board rows. Raises ValueError on malformed collaborations."""
    items = [wi_row(i, now) for i in facts['items'] if i['status'] in IN_FLIGHT]
    collaborations = facts.get('collaborations')
    available = isinstance(collaborations, list)
    contracts = []
    if available:
        try:
            contracts = [contract_row(c, now) for c in collaborations if c['status'] != 'withdrawn']
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            available, contracts = False, []
        if len({c['id'] for c in contracts}) != len(contracts):
            available, contracts = False, []
    entries = contracts + items
    agents: Dict[str, List[Dict[str, object]]] = {}
    for entry in entries:
        agents.setdefault(str(entry['agent']), []).append(entry)

    def order(entry):
        return (not entry['stuck'], entry['kind'] != 'contract', STATE_RANK.get(entry['state'], 0)
                if entry['kind'] == 'contract' else 0, -entry['_age'], entry['id'])

    groups = []
    for agent, members in agents.items():
        members.sort(key=order)
        last = max(_date(m['last_at']) for m in members)
        stuck = sum(1 for m in members if m['stuck'])
        summary = _summary(members) or '最近 ' + span(now - last) + '前'
        groups.append((stuck == 0, -last.timestamp(), agent, dict(
            kind='agent', id='agent:' + agent, agent=agent, name=_name(agent),
            wi_count=sum(1 for m in members if m['kind'] == 'wi'),
            contract_count=sum(1 for m in members if m['kind'] == 'contract'),
            stuck_count=stuck, summary=summary, last_at=last.isoformat()), members))
    groups.sort(key=lambda g: g[:3])
    result: List[Dict[str, object]] = [dict(
        kind='board', id='board', wi_total=len(items), contract_total=len(contracts),
        contracts_available=available, facts_at=_date(facts['generated_at']).isoformat())]
    for *_, head, members in groups:
        result.append(head)
        result.extend({k: v for k, v in m.items() if not k.startswith('_')} for m in members)
    return result
