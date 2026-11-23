"""Project selected automation services from native facts, never from probe heartbeats."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import re
import subprocess
from zoneinfo import ZoneInfo
from pathlib import Path
from .automation_source import catalog, catalog_digest, gather, SCHEMA, SITES as SOURCE_SITES
from .automation_layout import category, family_plan, groups, plan_lines
from .live_contract import DISPLAY_TZ

METRIC_ID = 'automation.tasks'
MAX_AGE = 2700
# A peer stamps its own observation; its clock may lead this reader's slightly.
CLOCK_SKEW = timedelta(seconds=5)
SITES = set(SOURCE_SITES)
# Family row ids, as the service map names them.
INBOX_ID = 'collab-inbox'
DELIVER_ID = 'collab-deliver'
REVIEW_ID = 'pr-review-dispatch'
# A round in progress is not a missed round.
RUNNING = {'执行中', '排队中'}
UNCHECKED = '已执行 · 未配结果检查'


def family_members(valid, family):
    """Job id -> (label, owner) for every job a family row folds, from the service map's rule.

    `named`: every job whose id matches; an owner unlike the id prefix is a conflict.
    `permitted_own`: only an Agent's own, permitted job; any other keeps its own row.
    """
    pattern = re.compile(family['pattern'])
    members = {}
    for source in valid.values():
        for job in source['jobs']:
            match = pattern.fullmatch(job.get('id') or '')
            if match is None:
                continue
            label = family['label'].format(*match.groups(), agent=_name(match[1]))
            if family['admit'] == 'named':
                members[job['id']] = (label, match[1])
            elif (match[1] == job.get('owner') and isinstance(source.get('execution_control'), dict)
                  and _permitted(source, job)):
                members[job['id']] = (label, job['owner'])
    return members


def date(value):
    if not isinstance(value, str):
        raise ValueError('missing timestamp')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('unqualified timestamp')
    return stamp.astimezone(timezone.utc)


def fetch():
    """Read this node in process and each registered peer over its Stats identity."""
    try:
        return gather(Path.home())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _field(text, minimum, maximum):
    result = set()
    for part in text.split(','):
        pieces = part.split('/')
        if len(pieces) > 2:
            raise ValueError('unsupported cron')
        span = pieces[0]
        step = int(pieces[1]) if len(pieces) == 2 else 1
        if step <= 0:
            raise ValueError('invalid cron step')
        if span == '*':
            lo, hi = minimum, maximum
        elif '-' in span:
            lo, hi = map(int, span.split('-'))
        else:
            lo = hi = int(span)
        if not minimum <= lo <= hi <= maximum:
            raise ValueError('invalid cron range')
        result.update(range(lo, hi + 1, step))
    return result


@lru_cache(maxsize=64)
def slots(expr, tz, minute, floor):
    """Bounded projection of the installed numeric five-field schedule, not a scheduler.

    Unsupported calendars are unknown; no guessed default cadence or manual backfill.
    Iterate UTC instants so repeated or nonexistent local times remain real instants.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError('unsupported cron')
    allowed = [_field(t,*bounds) for t,bounds in zip(fields,((0,59),(0,23),(1,31),(1,12),(0,7)))]
    zone = ZoneInfo(tz)
    now = date(minute)
    start = date(floor)
    def matches(stamp):
        local = stamp.astimezone(zone)
        dow = (local.weekday() + 1) % 7
        dom_ok, dow_ok = local.day in allowed[2], dow in allowed[4] or (dow == 0 and 7 in allowed[4])
        day_ok = dom_ok or dow_ok if fields[2] != '*' and fields[4] != '*' else dom_ok and dow_ok
        return local.minute in allowed[0] and local.hour in allowed[1] and local.month in allowed[3] and day_ok
    last, upcoming = None, None
    for offset in range(8*24*60 + 1):
        if last is None:
            prior = now - timedelta(minutes=offset)
            if prior >= start and matches(prior):
                last = prior
        if upcoming is None:
            future = now + timedelta(minutes=offset + 1)
            if future >= start and matches(future):
                upcoming = future
        if upcoming is not None and (last is not None or now-timedelta(minutes=offset) < start):
            break
    return last, upcoming


def _receipt(job, now):
    state = job['state']
    raw = state.get('lastResult')
    if not isinstance(raw, dict) or not raw.get('runId') or raw.get('definitionHash') != state.get('definitionHash'):
        return None
    try:
        if not date(state['activatedAt']) <= date(raw['startedAt']) <= date(raw['finishedAt']) <= now:
            return None
        if raw.get('status') not in ('ok','error','timeout'):
            return None
    except (KeyError, ValueError):
        return None
    return raw




def _round(job, now):
    """This definition's receipt, the due and next slots, and whether the receipt is an earlier round's."""
    state = job['state']
    raw = _receipt(job, now)
    floor = state.get('activatedAt')
    try:
        if date(floor) > now:
            raise ValueError('future activation')
        due, upcoming = slots(job['schedule']['expr'], job['schedule']['tz'],
            now.replace(second=0, microsecond=0).isoformat(), floor)
    except (ValueError, KeyError, TypeError):
        due = upcoming = None
    try:
        stale = raw is not None and due is not None and (not raw.get('scheduledFor') or date(raw['scheduledFor']) < due)
    except ValueError:
        stale = True
    return raw, due, upcoming, stale


def outcome(result):
    """The producer's own failure kind for one receipt."""
    if result.get('status') == 'timeout':
        return '执行超时'
    if result.get('deliveryStatus') == 'failed':
        return '交付失败'
    return '核验失败' if result.get('verifyStatus') == 'failed' else '执行失败'


def stage(job, now, spec=None):
    """This round's colour, status text, detail, latest result timestamp, next plan.

    Only the current definition's current round speaks here; earlier unsettled
    rounds and history gaps are `history`, never this status.
    """
    spec = spec or {}
    state = job['state']
    if not job['enabled'] and job.get('current_run_live'):
        return 'orange','暂停后仍有运行','暂停配置不等于上一轮已停止；本次观察不干预在途执行。',None,None
    if not job['enabled']:
        return 'gray','未运行 · 待恢复','该技术阶段尚未恢复；此次发布不改变调度。',None,None
    if not job.get('definition_verified'):
        # The reader names a missing check dependency; it never stands in for a match.
        reason = job.get('definition_reason')
        return 'orange','配置待核验',reason if isinstance(reason,str) and reason else '尚不能确认运行记录属于当前任务定义。',None,None
    raw, due, upcoming, stale = _round(job, now)
    latest = raw.get('finishedAt') if raw else None
    next_at = upcoming.isoformat() if upcoming else None
    if job.get('current_run_live'):
        phase = state.get('currentRun',{}).get('phase')
        return 'orange','排队中' if phase == 'queued' else '执行中','当前已登记运行仍由同一调度进程承载。',latest,next_at
    if raw and not stale and raw.get('deferred'):
        # Relay marks a round deferred only while its slot is planned to run again.
        return 'orange','等待重试','本轮主动延期，尚未完成业务交付。',latest,next_at
    if raw and not stale and raw['status'] in ('error','timeout'):
        return 'red',outcome(raw),'当前定义下的本轮失败已被任务自身记录。',latest,next_at
    if due is None and upcoming is not None:
        return 'green','按计划待执行','当前生效计划尚未到本轮执行时间，不代表已经完成。',latest,next_at
    if raw is None:
        return 'orange','结果待核验','缺少与当前定义绑定的运行结果；不会用历史错误或退出码补猜。',None,next_at
    if stale:
        prior = raw.get('scheduledFor') or raw.get('finishedAt')
        try:
            when = date(prior).astimezone(DISPLAY_TZ).strftime('%m-%d %H:%M')
        except ValueError:
            when = '时间无效'
        known = outcome(raw) if raw['status'] in ('error','timeout') else '已结束'
        return 'orange','本轮结果待核验',f'最后已知结果属于上一轮（{when} {known}），不代表本轮。',latest,next_at
    if raw.get('skipped') and raw.get('reasonCode') == 'automatic_paused':
        return 'gray','未运行 · 未获许可','任务已启用，但未登记执行许可；调度器到点拦下，本轮没有执行。',latest,next_at
    if raw.get('skipped') and raw.get('reasonCode') == 'wake_delivered':
        # A wake job starts no session of its own; what the woken session does is its WI's record.
        return 'green','已唤醒','已把任务说明交给握着该 WI 的会话；办理结果看该 WI 的记录。',latest,next_at
    if raw.get('skipped') and raw.get('reasonCode') == 'wake_not_delivered':
        reminded = raw.get('deliveryStatus') == 'sent'
        return ('orange','未送达','没有会话握着该 WI，本轮唤醒未送达；'
                + ('已提醒负责人。' if reminded else '提醒也未发出。'),latest,next_at)
    if raw.get('skipped') and job.get('skip_allowed'):
        summary = raw.get('skipSummary')
        declared = spec.get('skip')
        if declared and isinstance(summary, str):
            if declared.get('attention') and re.search(declared['attention'], summary):
                return 'orange',declared['attention_status'],declared['attention_detail'],latest,next_at
            return 'green',declared['status'],declared['detail'],latest,next_at
        return 'green','按约定无需执行','原任务记录本轮符合预先声明的静默/跳过条件。',latest,next_at
    if raw.get('executionStatus') != 'ok':
        return 'orange','结果待核验','运行结束，但缺少明确的执行结果。',latest,next_at
    return checked(job, raw, spec, latest, next_at)


# Each configured check needs its own receipt in this same-definition round's result.
CHECKS = (('requires_artifact', '产物检查', lambda raw: raw.get('artifactVerified') is True),
          ('requires_verify', '核验命令', lambda raw: raw.get('verifyStatus') == 'passed'))
# A Feishu document's template caption goes out as a separate best-effort text
# whose result the scheduler keeps no receipt of; a captionPath text, or a
# Telegram caption on the document itself, is inside the delivery receipt.
UNRECEIPTED_CAPTION = {('feishu_document', 'text')}


def checked(job, raw, spec, latest, next_at):
    """An exited round is complete only by every producer receipt its stage requires.

    Delivery, artifact check and verify command keep their own scope; a
    condition the service map names as unproven has no producer receipt.
    """
    proven = []
    delivers = job['delivery'] not in (None, 'none')
    if delivers:
        if raw.get('deliveryStatus') != 'sent':
            return 'orange','投递待核验','执行结束，尚无本轮成功投递凭据。',latest,next_at
        caption_gap = (job['delivery'], job.get('caption')) in UNRECEIPTED_CAPTION
        proven.append('投递出口记录成功' + ('（含说明文字）' if job.get('caption') else ''))
    configured = [(name, passed) for key, name, passed in CHECKS if job.get(key)]
    missing = [name for name, passed in configured if not passed(raw)]
    if missing:
        return ('orange','结果待核验',f"已配置{'、'.join(missing)}，本轮缺核验回执；"
                '不以配置存在或程序退出代替核验结果。',latest,next_at)
    checks = '、'.join(name for name, _ in configured)
    if checks:
        proven.append(f'{checks}本轮通过')
    if not proven:
        return 'green',UNCHECKED,'程序正常结束；本任务尚未配置可验证的业务结果检查（已知缺口），不代表产出已核验。',latest,next_at
    if delivers and caption_gap:
        # A known platform gap, not this round's outcome: once every configured
        # check passed, the colour follows the document.
        return ('green','文档已送达','；'.join(['文档已送达'] + proven[1:]) + '；同段说明文字由调度器另发且不保存送达回执（已知缺口），'
                '不能单独确认文字送达。',latest,next_at)
    unproven = spec.get('unproven') or []
    if unproven:
        return ('green','部分已核验',f"{'；'.join(proven)}；{'、'.join(unproven)}没有生产者回执（已知缺口），"
                '不能据此确认这部分完成。',latest,next_at)
    if delivers:
        return 'green','已送达','；'.join(proven) + '；不等同于人类已验收内容质量。',latest,next_at
    return 'green','本轮已核验',f'原任务配置的{checks}本轮通过。',latest,next_at


HISTORY_SHOWN = 3


def _when(result):
    try:
        return date(result.get('scheduledFor') or result['finishedAt']).astimezone(DISPLAY_TZ).strftime('%m-%d %H:%M')
    except (KeyError, ValueError):
        return '时间无效'


def history(site, job, now, shown=None):
    """Unsettled rounds outside the current one, and gaps in the kept summary.

    Each kind keeps its own scope: an earlier round of this definition, a
    round of a replaced definition, an incomplete record, a truncated summary.
    None of them is cleared by a later success or counted as this round.
    Only the failed receipt this round's row actually shows (`shown`, its
    runId) is left out; every other copy and status keeps all unsettled rounds.
    """
    state = job['state']
    raw, _, _, stale = _round(job, now)
    owed, old, invalid = [], [], 0
    for prior in state.get('unresolvedResults', []):
        if not isinstance(prior, dict):
            invalid += 1
            continue
        try:
            if not date(prior['startedAt']) <= date(prior['finishedAt']) <= now:
                raise ValueError('invalid prior time')
        except (KeyError, ValueError):
            invalid += 1
            continue
        if prior.get('status') not in ('error', 'timeout'):
            continue
        if shown is not None and prior.get('runId') == shown:
            continue
        (owed if prior.get('definitionHash') == state.get('definitionHash') else old).append(prior)
    label = f"{site} · {job['id']}"
    def rounds(kind, items):
        items = sorted(items, key=lambda r: r.get('scheduledFor') or r.get('finishedAt') or '', reverse=True)
        shown = '；'.join(f'{_when(r)} {outcome(r)}' for r in items[:HISTORY_SHOWN])
        more = f' 等 {len(items)} 轮' if len(items) > HISTORY_SHOWN else ''
        return f'{label} · {kind} {len(items)} 轮（{shown}{more}）'
    lines = []
    if owed:
        lines.append(rounds('上期欠交', owed))
    if old:
        lines.append(rounds('旧定义未核销', old))
    if invalid:
        lines.append(f'{label} · {invalid} 条未核销记录不完整，不能确认是否欠交')
    if state.get('resultHistoryIncomplete'):
        lines.append(f'{label} · 历史摘要不完整：调度器只保留最近 32 个未核销轮次，更早欠交缺原证据'
                     + ('；收敛任务的成功轮重读全部状态，只证明现态，不补证更早欠交' if job.get('convergent') else ''))
    return lines


def _history_fields(lines):
    """Row fields for history kept apart from this round's status."""
    if not lines:
        return dict(history='', note='', attention=False)
    return dict(history='\n'.join(lines), attention=True,
                note=f'另有 {len(lines)} 项历史待核销，不计入本期状态')


def _with_history(progress, lines):
    """The App shows progress on expanding a row; history goes there, apart from this round."""
    return progress + ('\n历史（不计入本期）：\n' + '\n'.join(lines) if lines else '')


def _executable(source, job):
    if job.get('current_run_live') is True:
        return True
    control = source.get('execution_control')
    if control is None:
        return job.get('enabled') is True
    permission = control.get('entries', {}).get('job:' + job['id'], {})
    return (job.get('enabled') is True and control.get('automatic_enabled') is True
            and permission.get('enabled') is True and permission.get('owner') == job['owner'])


def _permission_gap(source, job):
    """Why an enabled job may not run under a globally enabled execution control, or None."""
    control = source.get('execution_control')
    if not isinstance(control, dict) or control.get('automatic_enabled') is not True or not job.get('enabled'):
        return None
    permission = control.get('entries', {}).get('job:' + job['id'])
    if not isinstance(permission, dict):
        return f"执行登记缺 job:{job['id']}"
    if permission.get('enabled') is not True:
        return f"执行登记 job:{job['id']} 未许可"
    if permission.get('owner') != job['owner']:
        return f"执行登记 job:{job['id']} 负责人为 {permission.get('owner')}，任务为 {job['owner']}"
    return None


def _name(actor):
    return actor[:1].upper() + actor[1:]


def _safe_stage(job, now, spec=None):
    try:
        return stage(job, now, spec)
    except (KeyError, TypeError, ValueError):
        return ('orange', '结果待核验', '该节点运行记录不符合当前读取契约。', None, None)


def shown_run(job, result, now):
    """The runId of the failed current receipt a displayed stage result shows, or None."""
    try:
        raw, _, _, stale = _round(job, now)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if (raw is None or stale or result[0] != 'red' or raw.get('status') not in ('error', 'timeout')
            or result[1] != outcome(raw)):
        return None
    return raw.get('runId')


def _safe_history(site, job, now, shown=None):
    try:
        return history(site, job, now, shown)
    except (KeyError, TypeError, ValueError, AttributeError):
        return [f"{site} · {job.get('id')} · 历史记录不符合当前读取契约"]


def _missing(valid):
    """Expected nodes whose source this projection could not use, by name."""
    return '、'.join(sorted(SITES - set(valid)))


def _found(valid, job_id):
    """Every node's copy of a job, by node name rather than source position."""
    return sorted(((site, j) for site, s in valid.items() for j in s['jobs'] if j['id'] == job_id),
                  key=lambda pair: pair[0])


# A service speaks with its weakest stage: proven failure, evidence to check,
# a round in progress, a stage still due, then a completed stage.
GREEN_PENDING = {'按计划待执行'}


def _weight(result):
    color, status = result[0], result[1]
    if color == 'red':
        return 0
    if color == 'orange':
        return 2 if status in RUNNING else 1
    if color == 'gray':
        return 2.5  # Not run: weaker than any green, still after every orange.
    return 3 if status in GREEN_PENDING else 4


def _draft(job, result, now):
    """An executed local stage with no result check of its own."""
    receipt = _receipt(job, now)
    return (receipt is not None and receipt.get('executionStatus') == 'ok' and result[1] == UNCHECKED
            and job['delivery'] in (None, 'none') and not job.get('requires_artifact')
            and not job.get('requires_verify') and job.get('definition_verified'))


def aggregate(results, now):
    """One service result from (site, job, stage spec, stage result), whatever their order.

    Ties break on job id and node name, never on catalog or source position.
    A draft stage counts as its delivering stage only where the service map
    says so (`proven_by`) and that stage delivered.
    """
    delivered = {job['id']: result for _, job, _, result in results if result[1] == '已送达'}
    judged = []
    for site, job, spec, result in results:
        proof = (spec or {}).get('proven_by')
        if proof in delivered and _draft(job, result, now):
            result = delivered[proof]
        judged.append((site, job, result))
    return sorted(judged, key=lambda item: (_weight(item[2]), item[1]['id'], item[0]))[0][2]


def folded_row(valid, now, pause, spec, members):
    """One row for a family of jobs; `members` maps each job id to (label, owner)."""
    unit, count = spec['unit'], spec['count']
    complete = set(valid) == SITES
    owners, problems, paused, results, sites, past, schedules = set(), [], [], [], [], [], []
    for job_id, (label, owner) in sorted(members.items()):
        found = _found(valid, job_id)
        active = [(site, j) for site, j in found if j.get('enabled') is True]
        conflicted = len(active) > 1 or any(j.get('owner') != owner for _, j in active)
        runs = [] if conflicted else [(site, j) for site, j in (active or found[:1]) if _executable(valid[site], j)]
        result = _safe_stage(runs[0][1], now, spec) if runs else None
        past.extend(f'{label} · {line}' for site, j in found for line in _safe_history(
            site, j, now, shown_run(j, result, now) if runs and j is runs[0][1] else None))
        if conflicted:
            owners.add(owner)
            problems.append((label, '多处', ('orange', '运行归属冲突', spec['conflict'], None, None), None))
            continue
        if not runs:
            paused.append(label)
            continue
        site, job = runs[0]
        owners.add(owner)
        sites.append(site)
        schedules.append(job.get('schedule'))
        results.append(result)
        # A member that did not run (gray) is no more a success than a failure.
        if result[0] in ('red', 'gray') or (result[0] == 'orange' and result[1] not in RUNNING):
            problems.append((label, site, result, job.get('schedule')))
    red = [p for p in problems if p[2][0] == 'red']
    idle = [p for p in problems if p[2][0] == 'gray']
    pending = len(problems) - len(red) - len(idle)
    # The first count names the unit (e.g. "个 Agent"), later ones only the counter.
    parts = [(n, word) for n, word in ((len(red), '有失败'), (pending, '待核验'), (len(idle), '未运行')) if n]
    tally = '，'.join(f'{n} {unit if i == 0 else count}{word}' for i, (n, word) in enumerate(parts))
    latest = max((r[3] for r in results if r[3]), default=None)
    next_at = min((r[4] for r in results if r[4]), default=None)
    # Group by each member's own status so the summary claims no more than its rows.
    groups = {}
    for problem in problems:
        groups.setdefault(problem[2][1], []).append(problem)
    if len(groups) == 1:
        (label, members_), = groups.items()
        summary = f'{len(members_)} {unit}为{label}。{members_[0][2][2]}'
    else:
        head = '，'.join(part for part in (
            f'{len(red)} {unit}有失败证据' if red else '', f'{pending} {count}证据待核验' if pending else '',
            f'{len(idle)} {count}未运行' if idle else '') if part)
        if not red and pending:
            head = head.replace(f'{pending} {count}证据待核验', f'{pending} {unit}证据待核验', 1) + '，尚无失败证据'
        summary = head + '：' + '；'.join(f'{label} {len(members_)} {count}' for label, members_ in groups.items()) + '。'
    if not complete:
        color, status = ('red', f'{len(red)} {unit}有失败') if red else ('orange', '来源待核验')
        reason = _missing(valid) + ' 来源不可读取，' + spec['partial'] + (' 已读取节点中' + summary if problems else '')
    elif not owners:
        color, status, reason = 'gray', '未运行 · ' + pause['reason'], spec['idle']
    elif problems:
        color = 'red' if red else 'orange' if pending else 'gray'
        if len(problems) == 1:
            status = problems[0][0] + ' ' + problems[0][2][1]
        elif len(groups) == 1:
            status = f'{len(problems)} {unit}{problems[0][2][1]}'
        else:
            status = tally
        reason = problems[0][2][2] if len(problems) == 1 else summary
    else:
        clock = datetime.fromisoformat(latest.replace('Z', '+00:00')).astimezone(DISPLAY_TZ).strftime('%H:%M') if latest else ''
        color, status = 'green', (clock + ' 全部按时').strip()
        reason = spec['calm']
    lines = [f'{label} · {site} · {result[1]}' for label, site, result, _ in problems]
    if paused:
        lines.append('未启用：' + '、'.join(paused))
    plans = '；'.join(f"{label} {plan['expr']} ({plan['tz']})" for label, _, _, plan in problems if isinstance(plan, dict))
    observed = min((date(s['observed_at']) for s in valid.values()), default=now)
    past_fields = _history_fields(past)
    past_fields['attention'] = past_fields['attention'] or bool(problems)
    return dict(id=spec['id'], title=f"{spec['title']} · {len(owners)} 个 Agent", owner='各 Agent', color=color,
        status=status, reason=reason, expectation=spec['expectation'],
        plan=plans or '各 Agent 按各自登记的计划运行', progress=_with_history('\n'.join(lines) or spec['empty'], past),
        next_at=next_at, last_result_at=latest, reference=','.join(sorted(members)),
        node=','.join(dict.fromkeys(sites)) or '未运行', mapping='',
        source_at=observed.isoformat(), expires_at=(observed+timedelta(seconds=MAX_AGE)).isoformat(),
        coverage=f'已读 {len(valid)}/{len(SITES)} 个节点的任务来源', decision_ref=pause['source_ref'],
        **family_plan(schedules), **past_fields)


def _permitted(source, job):
    control = source.get('execution_control') or {}
    permission = control.get('entries', {}).get('job:' + job['id'], {})
    return (job['enabled'] and control.get('automatic_enabled') is True
            and permission.get('enabled') is True and permission.get('owner') == job['owner'])


def _valid_sources(document, now):
    """Sources of the expected inventory whose producer matches this reader's map and contract."""
    valid = {}
    if not isinstance(document, dict) or document.get('schema') != 'pulse_stats_automation_sources/v1':
        return valid
    try:
        sources = document['sources']
        if len(sources) != len(SITES) or {s['site'] for s in sources} != SITES:
            raise ValueError('incomplete site inventory')
        for source in sources:
            if source.get('schema') != SCHEMA or source.get('status') != 'available' or source.get('catalog_digest') != catalog_digest():
                continue
            stamp = date(source['observed_at'])
            if not stamp - CLOCK_SKEW <= now < stamp + timedelta(seconds=MAX_AGE):
                continue
            if any(not isinstance(source.get(key),str) or not re.fullmatch('[0-9a-f]{64}',source[key]) for key in ('config_digest','state_digest')):
                continue
            jobs = source['jobs']
            if not isinstance(jobs,list) or any(not isinstance(j,dict) or type(j.get('enabled')) is not bool or not isinstance(j.get('state'),dict) for j in jobs):
                continue
            if len({j['id'] for j in jobs}) != len(jobs):
                continue
            valid[source['site']] = source
    except (ValueError, TypeError, KeyError):
        return {}
    return valid


def service_row(service, valid, now, pause):
    """One service row: its stages' current round, mapping differences and history, apart."""
    complete = set(valid) == SITES
    entries, diffs, copies, gaps = [], [], [], []
    conflict = retired_live = False
    accounted = True
    for spec in service['stages']:
        job_id = spec['job']
        owner = spec.get('owner', service['owner'])
        found = _found(valid, job_id)
        active = [(site, j) for site, j in found if j.get('enabled') is True]
        accounted &= bool(active)
        if len(active) > 1:
            conflict = True
            diffs.append(f"{job_id} 在 {'、'.join(site for site, _ in active)} 同时启用")
        for site, j in active:
            if j.get('owner') != owner:
                conflict = True
                diffs.append(f"{job_id} 负责人：目录为 {owner}，{site} 实际为 {j.get('owner')}")
            gap = _permission_gap(valid[site], j)
            if gap:
                gaps.append(gap)
        if not found and complete:
            diffs.append(f'{job_id} 未在任何节点配置')
        copies.extend((site, j, '') for site, j in found)
        entries.extend((site, j, spec) for site, j in (active or found[:1]))
    for old in service.get('retired', []):
        for site, j in _found(valid, old['job']):
            if j.get('enabled') is True:
                retired_live = True
                diffs.append(f"已退役的 {old['job']} 仍在 {site} 启用")
            copies.append((site, j, '（已退役任务）'))
    diffs.extend(gaps)
    activated = [(site, j, spec) for site, j, spec in entries if _executable(valid[site], j)]
    results = [(site, j, spec, _safe_stage(j, now, spec)) for site, j, spec in activated]
    # A conflicting or unconfirmed map shows no stage result, so it settles no receipt.
    shown = {} if conflict or retired_live else {(site, j['id']): shown_run(j, r, now) for site, j, _, r in results}
    past = [line + tag for site, j, tag in copies
            for line in _safe_history(site, j, now, None if tag else shown.get((site, j['id'])))]
    if service.get('retired') and not complete:
        # An unread node may still hold a retired job's unsettled rounds.
        past.append(f"{_missing(valid)} 来源不可读取：已退役任务"
                    f"{'、'.join(old['job'] for old in service['retired'])}的历史欠交不可得，不当作无欠交")
    known_red = aggregate(results, now) if any(r[0] == 'red' for *_, r in results) else None
    # A missing node affects only services whose every stage is not already
    # running on a readable node; the others keep their known results.
    settled = complete or (accounted and len(activated) == len(service['stages']))
    mapping = '；'.join(diffs)
    if conflict:
        color, status, reason = 'orange', '运行归属冲突', f'服务映射与实际任务不符：{mapping}；不合并为成功。'
    elif retired_live:
        color, status, reason = 'orange', '映射待核对', f'服务映射与实际任务不符：{mapping}。'
    elif not settled:
        color, status, reason = (known_red[:3] if known_red else ('orange', '来源待核验',
            f'{_missing(valid)} 来源不可读取，不能确认该服务的阶段是否在其上运行；不从已知暂停推断全部仍暂停。'))
    elif not activated:
        color, status, reason = (('gray', '未运行 · 未获许可', f"任务已启用但未获执行许可：{'；'.join(gaps)}；Stats 不自动登记。")
            if gaps else ('gray', '未运行 · ' + pause['reason'], '已决定恢复，改造完成前暂停；Stats 不自动启用。'))
    elif len(activated) != len(service['stages']):
        color, status, reason = known_red[:3] if known_red else ('orange', '部分阶段未启用',
            '该服务的部分技术阶段仍暂停' + (f'：{mapping}。' if mapping else '。'))
    else:
        color, status, reason = aggregate(results, now)[:3]
    details = '\n'.join([f"{site} · {job['id']} · {r[1]}" for site, job, _, r in results]
                        + ([f'{_missing(valid)} · 来源不可读取'] if not complete and results else []))
    plan = '；'.join(f"{j['schedule']['expr']} ({j['schedule']['tz']})" for _, j, _ in entries if isinstance(j.get('schedule'), dict))
    latest = max((r[3] for *_, r in results if r[3]), default=None)
    next_at = min((r[4] for *_, r in results if r[4]), default=None)
    observed = min((date(s['observed_at']) for s in valid.values()), default=now)
    progress = (details or '当前没有启用中的技术阶段。') if complete else (details or '节点来源尚未完整读取。')
    progress = _with_history(progress, past)
    return dict(id=service['id'], title=service['title'], owner=service['owner'], color=color, status=status,
        reason=reason, expectation=service['expectation'],
        plan=plan or ('参考定义尚未部署；当前按迭代安排暂停' if complete and not activated else '未取得有效计划'),
        progress=progress, next_at=next_at, last_result_at=latest,
        reference=','.join(spec['job'] for spec in service['stages']),
        node=','.join(dict.fromkeys(site for site, _, _ in activated)) or '未运行', mapping=mapping,
        source_at=observed.isoformat(), expires_at=(observed+timedelta(seconds=MAX_AGE)).isoformat(),
        coverage=f'已读 {len(valid)}/{len(SITES)} 个节点的任务来源', decision_ref=pause['source_ref'],
        **plan_lines([j.get('schedule') for _, j, _ in entries],
                     ('计划未定', '未部署 · 暂停') if complete else ('计划未知', '来源待核验')),
        **_history_fields(past))


def metrics(document, now):
    cfg = catalog()
    valid = _valid_sources(document, now)
    stamps = [date(s['observed_at']) for s in valid.values()]
    observed = min(stamps) if stamps else now
    rows = [service_row(service, valid, now, cfg['pause']) for service in cfg['services']]
    known = {spec['job'] for service in cfg['services'] for spec in service['stages']}
    known |= {old['job'] for service in cfg['services'] for old in service.get('retired', [])}
    for family in cfg['families']:
        members = family_members(valid, family)
        known |= set(members)
        if members or family.get('show_empty'):
            rows.append(folded_row(valid, now, cfg['pause'], family, members))
    for site, native in valid.items():
        control = native.get('execution_control')
        if not isinstance(control, dict):
            continue
        # Execution control registers named jobs only (2026-10-19). A permitted job
        # without a service row shows under its own name and is judged from its own
        # results; an enabled unpermitted one keeps the notice; a disabled one is omitted.
        for job in native['jobs']:
            if job['id'] in known:
                continue
            entry = 'job:' + job['id']
            expires = (date(native['observed_at'])+timedelta(seconds=MAX_AGE)).isoformat()
            if not job['enabled']:
                # A stopped job is omitted unless it still owes rounds; stopping settles none.
                past = _safe_history(site, job, now)
                if past:
                    rows.append(dict(id='job:' + site + ':' + job['id'], title=job.get('name') or job['id'],
                        owner=job['owner'], color='gray', status='未运行 · 已停用',
                        reason='任务已停用；停用不核销此前未核销的轮次。', expectation='按登记计划运行；本轮结果以任务自身的检查回执为准',
                        plan=f"{(job.get('schedule') or {}).get('expr')} ({(job.get('schedule') or {}).get('tz')})",
                        progress=_with_history(f"{site} · {job['id']} · 已停用", past), next_at=None, last_result_at=None,
                        reference=entry, node=site, source_at=native['observed_at'], expires_at=expires,
                        coverage='实际任务来源', mapping='未归入服务目录',
                        decision_ref=control.get('decision_ref', 'missing_execution_registration'),
                        **plan_lines([job.get('schedule')], ('计划未知', '')), **_history_fields(past)))
                continue
            if _permitted(native, job):
                color, status, reason, latest, next_at = result = _safe_stage(job, now)
                past = _safe_history(site, job, now, shown_run(job, result, now))
                schedule = job.get('schedule') or {}
                rows.append(dict(id='job:' + site + ':' + job['id'], title=job.get('name') or job['id'],
                    owner=job['owner'], color=color, status=status, reason=reason,
                    expectation='按登记计划运行；本轮结果以任务自身的检查回执为准',
                    plan=f"{schedule.get('expr')} ({schedule.get('tz')})",
                    progress=_with_history(f"{site} · {job['id']} · {status}", past), next_at=next_at, last_result_at=latest,
                    reference=entry, node=site, source_at=native['observed_at'], expires_at=expires,
                    coverage='实际任务来源', mapping='未归入服务目录', decision_ref=control.get('entries', {}).get(entry, {}).get('decision_ref')
                    or control.get('decision_ref', 'missing_execution_registration'),
                    **plan_lines([job.get('schedule')], ('计划未知', '')), **_history_fields(past)))
                continue
            # Without permission nothing runs this round; a withdrawn permission settles no earlier round.
            past = _safe_history(site, job, now)
            rows.append(dict(id='job:' + site + ':' + job['id'], title='新增任务 · ' + job['id'],
                owner=job['owner'], color='gray', status='未运行 · 未获许可',
                reason='任务已启用，但未登记执行许可；调度器到点拦下，不会执行。', expectation='不通过新增入口绕过暂停',
                plan=job['schedule']['expr'], progress=_with_history('', past).lstrip('\n'), next_at=None, last_result_at=None,
                reference=entry, node=site, source_at=native['observed_at'], expires_at=expires,
                coverage='实际任务来源', mapping='未归入服务目录', decision_ref=control.get('decision_ref','missing_execution_registration'),
                **plan_lines([job.get('schedule')], ('计划未知', '')), **_history_fields(past)))
    layout = groups()
    for row in rows:
        job_id = row['reference'][4:] if row['id'].startswith('job:') else None
        row.update(category(row['id'], job_id, layout))
    metric=dict(id=METRIC_ID,capability_id='automation.service_results',status='available',value=rows,
        unit='tasks',window='current',sample_count=len(rows),quality='descriptive',scope='Selected automation services',
        observed_at=observed.isoformat(),reason='暂停是已确认安排；只有有证据的本轮失败才用红色。')
    source=dict(id='automation-native-sources',freshness='fresh' if set(valid)==SITES else 'error',
        observed_at=observed.isoformat(),error_code=None if set(valid)==SITES else 'incomplete_source_coverage')
    return [metric],source
