"""Display layout of automation rows: purpose group and a two-line plan.

Grouping is a reading aid only. It never changes a row's status, permission or
mapping, and it is not part of the service map digest that peers report.
"""
from __future__ import annotations
import json
from pathlib import Path
import re

GROUPS_SCHEMA = 'pulse_stats_automation_groups/v1'
DISPLAY_ZONE = 'Asia/Shanghai'
WEEKDAYS = '日一二三四五六日'


def check_groups(value, catalog) -> list[str]:
    """Every difference that keeps each row in at most one purpose group."""
    if not isinstance(value, dict) or value.get('schema') != GROUPS_SCHEMA:
        return [f'用途分组 schema 不是 {GROUPS_SCHEMA}']
    unassigned, groups = value.get('unassigned'), value.get('groups')
    if not isinstance(unassigned, dict) or not isinstance(groups, list):
        return ['用途分组缺 unassigned 或 groups']
    known = {s['id'] for s in catalog['services']} | {f['id'] for f in catalog['families']}
    diffs, ids, rows = [], {unassigned.get('id')}, {}
    for group in groups:
        gid = group.get('id') if isinstance(group, dict) else None
        if not isinstance(gid, str) or gid in ids or not isinstance(group.get('title'), str):
            diffs.append(f'用途 id 缺失或重复：{gid}'); continue
        ids.add(gid)
        for row in group.get('rows', []):
            if row not in known:
                diffs.append(f'用途 {gid} 的 {row} 不在服务目录')
            elif row in rows:
                diffs.append(f'{row} 同时归入用途 {rows[row]} 与 {gid}')
            else:
                rows[row] = gid
        for pattern in group.get('jobs', []):
            try:
                re.compile(pattern)
            except re.error:
                diffs.append(f'用途 {gid} 的任务规则无效：{pattern}')
    return diffs


def groups() -> dict:
    from .automation_source import catalog
    value = json.loads((Path(__file__).resolve().parents[1]/'config/automation-groups.json').read_text())
    diffs = check_groups(value, catalog())
    if diffs:
        raise ValueError('自动任务用途分组校验失败：' + '；'.join(diffs))
    return value


def category(row_id: str, job_id: str | None, layout: dict) -> dict:
    """The one purpose group of a row; a single job is matched by its own id."""
    for rank, group in enumerate(layout['groups']):
        if row_id in group['rows'] or (job_id and any(re.fullmatch(p, job_id) for p in group['jobs'])):
            return dict(category=group['id'], category_title=group['title'], category_rank=rank)
    other = layout['unassigned']
    return dict(category=other['id'], category_title=other['title'], category_rank=len(layout['groups']))


def _clock(hour: str, minute: str) -> str | None:
    if hour.isdigit() and minute.isdigit():
        return f'{int(hour):02d}:{int(minute):02d}'
    return None


def _days(dow: str) -> str | None:
    if not re.fullmatch(r'[0-7](,[0-7])*', dow):
        return None
    return '、'.join(dict.fromkeys(WEEKDAYS[int(d)] for d in dow.split(',')))


def describe(schedule) -> tuple[str, str]:
    """(cadence, time) of a numeric five-field schedule; anything else keeps its raw text."""
    if not isinstance(schedule, dict) or not isinstance(schedule.get('expr'), str):
        return '计划未知', ''
    expr, tz = schedule['expr'], schedule.get('tz')
    fields = expr.split()
    zone = '' if tz in (None, DISPLAY_ZONE) else f' ({tz})'
    if len(fields) != 5:
        return expr, zone.strip()
    minute, hour, dom, month, dow = fields
    months = '' if month == '*' else f' · {month.replace(",", "、")}月'
    clock = _clock(hour, minute)
    head = None
    if dom == '*' and clock:
        days = _days(dow)
        head = '每日' if dow == '*' else f'每周{days}' if days else None
        sub = clock
    elif dom == '*' and dow == '*' and hour == '*':
        step = re.fullmatch(r'(?:\*|(\d+)-59)/(\d+)', minute)
        if step:
            head, sub = f'每{step[2]}分', (f':{int(step[1]):02d} 起' if step[1] else '')
        elif re.fullmatch(r'\d+(,\d+)*', minute):
            head, sub = '每小时', ' / '.join(f':{int(m):02d}' for m in minute.split(','))
    if head is None:
        return expr, zone.strip()
    return head, (sub + months + zone).strip()


def plan_lines(schedules, empty: tuple[str, str]) -> dict:
    """Two scalar lines for the plan column; several stages list the first and the rest."""
    lines = list(dict.fromkeys(describe(s) for s in schedules))
    if not lines:
        head, sub = empty
    elif len(lines) == 1:
        head, sub = lines[0]
    else:
        head = ' '.join(part for part in lines[0] if part)
        sub = '；'.join(' '.join(part for part in line if part) for line in lines[1:])
    return dict(plan_head=head, plan_sub=sub)


def family_plan(schedules) -> dict:
    """A folded family shows the members' shared cadence, or says each keeps its own."""
    lines = list(dict.fromkeys(describe(s) for s in schedules))
    if len(lines) == 1:
        return dict(plan_head=lines[0][0], plan_sub=lines[0][1])
    return dict(plan_head='按各自计划' if lines else '计划未定', plan_sub='各 Agent 分别登记' if lines else '无启用成员')
