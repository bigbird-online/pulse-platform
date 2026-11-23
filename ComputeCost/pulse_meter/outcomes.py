"""WI 产出（合同第 5 节末）：内核导出的 WI 事实 → 汇总库 `wi_outcomes`。

来源与旧链路相同：内核从 WI 账本日志导出的只读 `harness.stats_facts/v1`
（ownermac 为 ~/.local/state/pulse/stats/wi-facts-v1.json，登记为 sources.json 里
kind=harness_core 的 path）。三类产出：

- `wi_completed`：WI 完成（`completions` 里 profile=lightweight，即 complete_lightweight）；
- `work_accepted`：验收通过的 WorkRecorded（`completions` 里 profile=standard）；
- `pull_request_merged`：经内核合入 main 的 PR（`merged_pull_requests`）。

增量：导出文件整体约 2MB，按其 sha256 作游标，没变就不读；变了逐行幂等并入，
主键 (work_item_id, kind, ref) 不变、内容没变不写。不做全账本判定。
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from pulse_compute_cost.receipts import digest, reference

DDL = """
CREATE TABLE IF NOT EXISTS wi_outcomes(
  work_item_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN('wi_completed','work_accepted','pull_request_merged')),
  ref TEXT NOT NULL,                 -- 'rev:<日志修订号>' 或 '<仓库>#<PR 号>'
  at TEXT NOT NULL,                  -- UTC ISO8601
  agent TEXT,                        -- 完成者（completion.actor）；PR 取 WI owner
  detail TEXT NOT NULL DEFAULT '{}', -- 展示与引用所需：title、verification、三个摘要、namespace
  PRIMARY KEY(work_item_id, kind, ref));
CREATE INDEX IF NOT EXISTS wi_outcomes_at ON wi_outcomes(at);
CREATE TABLE IF NOT EXISTS wi_outcome_cursor(
  source TEXT PRIMARY KEY, sha256 TEXT NOT NULL, generated_at TEXT, coverage TEXT, synced_at TEXT NOT NULL);
-- 会话写过哪个 WI（导出的 session_writes；session_key 是内核对原生会话号的哈希，见 holder_supplement）。
CREATE TABLE IF NOT EXISTS wi_session_writes(
  session_key TEXT NOT NULL, at TEXT NOT NULL, work_item_id TEXT NOT NULL, actor TEXT NOT NULL,
  PRIMARY KEY(session_key, at, work_item_id));
CREATE TABLE IF NOT EXISTS wi_export_identity(key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- namespace、surfaces、actors
-- WI 的 owner（导出 items[].owner）：v_event 在 Agent 认不出时按 WI 认领人归属。
CREATE TABLE IF NOT EXISTS wi_owners(work_item_id TEXT PRIMARY KEY, owner TEXT NOT NULL);
"""
SCHEMA = 'harness.stats_facts/v1'
SOURCE = 'harness_core'
KINDS = {'lightweight': 'wi_completed', 'standard': 'work_accepted'}
LEVELS = {'human': 'human', 'independent': 'independent', 'agent_review': 'self_with_controls'}
# 导出超过这个年龄就不再算"已核验完整"（与 pull_request_outputs.MAX_EXPORT_AGE 相同）。
MAX_EXPORT_AGE = timedelta(minutes=75)
_WI = re.compile(r'wi_\d{8}_\d{3}')
_SHA = re.compile(r'[0-9a-f]{64}')


TABLES = ('wi_outcomes', 'wi_outcome_cursor', 'wi_session_writes', 'wi_export_identity', 'wi_owners')


def ensure(conn: sqlite3.Connection) -> None:
    """缺表才建：已建好时不跑 DDL（executescript 会先提交，会打断调用方的读事务）。"""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(TABLES) <= have:
        conn.executescript(DDL)


def _utc(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    from datetime import timezone
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def rows(document: Mapping[str, Any]) -> list[tuple[str, str, str, str, str | None, str]]:
    """导出文件 → wi_outcomes 行；读不懂的条目跳过（不猜）。"""
    if not isinstance(document, Mapping) or document.get('schema') != SCHEMA:
        raise ValueError('unsupported WI facts export')
    namespace = document.get('namespace') if isinstance(document.get('namespace'), str) else ''
    result = []
    for item in document.get('items') or []:
        if not isinstance(item, Mapping) or not isinstance(item.get('id'), str) or not _WI.fullmatch(item['id']):
            continue
        wi = item['id']
        owner = item.get('owner') if isinstance(item.get('owner'), str) else None
        for c in item.get('completions') or []:
            at = _utc(c.get('at')) if isinstance(c, Mapping) else None
            kind = KINDS.get(c.get('profile')) if isinstance(c, Mapping) else None
            if (at is None or kind is None or type(c.get('revision')) is not int
                    or any(not isinstance(c.get(f), str) or not _SHA.fullmatch(c[f])
                           for f in ('candidate', 'artifacts', 'acceptance'))):
                continue
            detail = {'namespace': namespace, 'title': c.get('title') if isinstance(c.get('title'), str) else None,
                      'verification': c.get('verification'), 'candidate': c['candidate'],
                      'artifacts': c['artifacts'], 'acceptance': c['acceptance']}
            actor = c.get('actor') if isinstance(c.get('actor'), str) else None
            result.append((wi, kind, 'rev:%d' % c['revision'], at, actor,
                           json.dumps(detail, sort_keys=True, ensure_ascii=False)))
        for pr in item.get('merged_pull_requests') or []:
            at = _utc(pr.get('merged_at')) if isinstance(pr, Mapping) else None
            if (at is None or not isinstance(pr.get('repository'), str) or type(pr.get('number')) is not int):
                continue
            detail = {'namespace': namespace, 'merge_commit': pr.get('merge_commit')}
            result.append((wi, 'pull_request_merged', '%s#%d' % (pr['repository'], pr['number']), at, owner,
                           json.dumps(detail, sort_keys=True, ensure_ascii=False)))
    return result


def session_writes(document: Mapping[str, Any]) -> tuple[list[tuple[str, str, str, str]], dict[str, str]]:
    """导出里的 session_writes 行，以及重算会话键所需的 namespace、surfaces、actors。"""
    result, actors = [], set()
    for item in document.get('items') or []:
        if not isinstance(item, Mapping) or not isinstance(item.get('id'), str) or not _WI.fullmatch(item['id']):
            continue
        for w in item.get('session_writes') or []:
            at = _utc(w.get('at')) if isinstance(w, Mapping) else None
            if (at is None or not isinstance(w.get('session'), str) or not _SHA.fullmatch(w['session'])
                    or not isinstance(w.get('actor'), str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', w['actor'])):
                continue
            result.append((w['session'], at, item['id'], w['actor']))
            actors.add(w['actor'])
    surfaces = sorted(set(re.findall(r'surface_v1_[a-z0-9_]{1,40}', json.dumps(document, ensure_ascii=False))))
    identity = {'namespace': document.get('namespace') if isinstance(document.get('namespace'), str) else '',
                'surfaces': json.dumps(surfaces), 'actors': json.dumps(sorted(actors))}
    return result, identity


def owners(document: Mapping[str, Any]) -> list[tuple[str, str]]:
    """导出里每个 WI 的 owner（Agent 名拼写才收）。"""
    result = []
    for item in document.get('items') or []:
        if (isinstance(item, Mapping) and isinstance(item.get('id'), str) and _WI.fullmatch(item['id'])
                and isinstance(item.get('owner'), str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', item['owner'])):
            result.append((item['id'], item['owner']))
    return result


def sync(conn: sqlite3.Connection, path: Path | None, *, now: datetime) -> dict[str, Any]:
    """读一次导出文件并入表；文件 sha256 与上次相同则不解析。"""
    ensure(conn)
    if path is None:
        return {'status': 'not_configured', 'changed': 0}
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            return {'status': 'missing', 'changed': 0}
        data = path.read_bytes()
    except OSError:
        return {'status': 'unreadable', 'changed': 0}
    sha = hashlib.sha256(data).hexdigest()
    known = conn.execute('SELECT sha256 FROM wi_outcome_cursor WHERE source=?', (SOURCE,)).fetchone()
    if known is not None and known[0] == sha:
        return {'status': 'unchanged', 'changed': 0}
    try:
        document = json.loads(data)
        found = rows(document)
        writes, identity = session_writes(document)
    except ValueError:
        return {'status': 'invalid', 'changed': 0}
    changed = 0
    conn.execute('BEGIN IMMEDIATE')
    try:
        for row in found:
            cursor = conn.execute(
                'INSERT INTO wi_outcomes(work_item_id, kind, ref, at, agent, detail) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(work_item_id, kind, ref) DO UPDATE SET at=excluded.at, agent=excluded.agent, '
                'detail=excluded.detail WHERE at IS NOT excluded.at OR agent IS NOT excluded.agent '
                'OR detail IS NOT excluded.detail', row)
            changed += cursor.rowcount
        conn.executemany('INSERT INTO wi_owners(work_item_id, owner) VALUES(?, ?) ON CONFLICT(work_item_id) '
                         'DO UPDATE SET owner=excluded.owner WHERE owner <> excluded.owner', owners(document))
        conn.executemany('INSERT OR IGNORE INTO wi_session_writes(session_key, at, work_item_id, actor) '
                         'VALUES(?,?,?,?)', writes)
        conn.executemany('INSERT INTO wi_export_identity(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE '
                         'SET value=excluded.value', sorted(identity.items()))
        conn.execute(
            'INSERT INTO wi_outcome_cursor(source, sha256, generated_at, coverage, synced_at) VALUES(?,?,?,?,?) '
            'ON CONFLICT(source) DO UPDATE SET sha256=excluded.sha256, generated_at=excluded.generated_at, '
            'coverage=excluded.coverage, synced_at=excluded.synced_at',
            (SOURCE, sha, document.get('generated_at') if isinstance(document.get('generated_at'), str) else None,
             document.get('coverage') if isinstance(document.get('coverage'), str) else None,
             _utc(now.isoformat())))
        conn.execute('COMMIT')
    except BaseException:
        conn.execute('ROLLBACK')
        raise
    return {'status': 'read', 'changed': changed, 'rows': len(found)}


def coverage(conn: sqlite3.Connection, *, now: datetime) -> str:
    """'complete'：最近一次读到的导出声明完整且不超过 75 分钟；'observed'：读到过；'unavailable'：没读到过。"""
    ensure(conn)
    row = conn.execute('SELECT generated_at, coverage FROM wi_outcome_cursor WHERE source=?', (SOURCE,)).fetchone()
    if row is None:
        return 'unavailable'
    try:
        generated = datetime.fromisoformat(str(row[0]).replace('Z', '+00:00'))
        fresh = generated.tzinfo is not None and now - MAX_EXPORT_AGE <= generated <= now + timedelta(minutes=5)
    except ValueError:
        fresh = False
    return 'complete' if fresh and row[1] == 'complete' else 'observed'


def in_window(conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    """[start, end) 两个 Asia/Shanghai 日期内的产出，按时间排。"""
    ensure(conn)
    return [{'wi': r[0], 'kind': r[1], 'ref': r[2], 'at': r[3], 'agent': r[4], 'detail': json.loads(r[5])}
            for r in conn.execute(
                "SELECT work_item_id, kind, ref, at, agent, detail FROM wi_outcomes "
                "WHERE date(at, '+8 hours') >= ? AND date(at, '+8 hours') < ? ORDER BY at, work_item_id, kind, ref",
                (start, end))]


def item(row: Mapping[str, Any]) -> dict[str, Any]:
    """一条 WI 完成或验收产出 → 发布物 `views[].outcomes.items[]`（与旧链路 kernel_outcomes 同引用算法）。"""
    d = row['detail']
    ns, wi = d.get('namespace') or '', row['wi']
    title = d.get('title')
    title = ' '.join(title.split())[:240] if isinstance(title, str) and title.strip() else None
    return {'outcome_ref': reference('outcome', ['harness_core', ns, wi, row['kind'], row['ref']]),
            'fact_ref': reference('wi_outcome', ['harness_core', ns, wi, row['kind'], row['ref']]),
            'wi': wi, 'responsible': row['agent'], 'status': 'accepted', 'counted': True,
            'occurred_at': row['at'],
            'candidate_ref': reference('candidate', ['harness_core', ns, wi, d['candidate']]),
            'artifact_ref': reference('artifact', ['harness_core', d['artifacts']]),
            'acceptance_ref': reference('acceptance', ['harness_core', d['acceptance']]),
            'review_decisions': [], 'review_refs': [], 'evidence_refs': [],
            'title': title, 'summary': None, 'verification_level': LEVELS.get(d.get('verification'))}


def session_key(namespace: str, actor: str, native_id: str, surface: str) -> str:
    """与 holder_supplement.session_key 相同：内核导出的会话写入键。"""
    return digest(['stats_session_write', 'session.' + digest([namespace, actor, native_id, surface])])


def holder_claims(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """会话写入补充（v_event 第 4 层）：(session_ref, turn_id 或 '*', wi)。

    一个原生会话只对上一个导出键时才用。整个会话只写过一个 WI：整会话归它（'*'）。
    写过多个 WI：按回合切窗口，回合起点＝该回合最早一条用量，窗口到下一回合起点；
    窗口内写过的 WI 各出一行（多于一个即该层冲突）；窗口内没写过的回合不补。
    表须已建（publish.connect 建）：这里不跑 DDL，以便在调用方的读事务里执行。
    """
    identity = dict(conn.execute('SELECT key, value FROM wi_export_identity'))
    writes: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key, at, wi in conn.execute('SELECT session_key, at, work_item_id FROM wi_session_writes'):
        writes[key].append((at, wi))
    if not identity or not writes:
        return []
    namespace = identity.get('namespace', '')
    surfaces, actors = json.loads(identity.get('surfaces', '[]')), json.loads(identity.get('actors', '[]'))
    turns: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for session_id, session_ref, turn_id, start in conn.execute(
            "SELECT session_id, session_ref, turn_id, min(occurred_at) FROM usage_events "
            "WHERE source = 'native' AND session_id <> '' GROUP BY session_id, session_ref, turn_id"):
        turns[(session_id, session_ref)].append((start, turn_id))
    result = []
    for (session_id, session_ref), found in turns.items():
        keys = {k for a in actors for s in surfaces if (k := session_key(namespace, a, session_id, s)) in writes}
        if len(keys) != 1:
            continue
        held = sorted(writes[keys.pop()])
        if len({wi for _, wi in held}) == 1:
            result.append((session_ref, '*', held[0][1]))
            continue
        ordered = sorted((start, turn) for start, turn in found if turn)
        for index, (start, turn) in enumerate(ordered):
            end = ordered[index + 1][0] if index + 1 < len(ordered) else '￿'
            for wi in sorted({wi for at, wi in held if start <= at < end}):
                result.append((session_ref, turn, wi))
    return result
