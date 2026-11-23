"""价表装入（合同第 4 节）：OpenRouter 价表快照 → 汇总库 `prices`。

单位是每 Token 的 1e-12 美元（整数）。首次装入某模型 `effective_from='2026-11-01'`；
以后单价变了追加一行，`effective_from` 取新快照的观测日（Asia/Shanghai）。
缺缓存读写单价记 -1：该类 Token 为 0 的事件照常计价，非 0 的整条未计价。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import sqlite3
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from pulse_compute_cost.receipts import digest
from pulse_compute_cost.wi_pricing import _or_model_id

FIRST_EFFECTIVE = '2026-11-01'
MISSING = -1
SCALE = 10 ** 12
ZONE = ZoneInfo('Asia/Shanghai')
META_SHA = 'price_catalog_sha256'
META_OBSERVED = 'price_catalog_observed_at'


def model_key(model: str | None) -> str | None:
    """事件里的模型名 → 价表 id，只做拼写映射。"""
    return _or_model_id(model) if isinstance(model, str) and model else None


def rate_e12(value: Any) -> int | None:
    """'0.000004' → 4000000；非数、负数、不能整除到 1e-12 的单价不用。"""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value)) * SCALE
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def catalog_rates(catalog: Mapping[str, Any]) -> dict[str, tuple[int, int, int, int]]:
    """id 唯一且有输入、输出单价的模型 → (prompt, cache_read, cache_write, completion)。"""
    rows = catalog.get('data') if isinstance(catalog, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError('catalog has no model list')
    seen: dict[str, int] = {}
    for row in rows:
        if isinstance(row, Mapping) and isinstance(row.get('id'), str):
            seen[row['id']] = seen.get(row['id'], 0) + 1
    result = {}
    for row in rows:
        if not isinstance(row, Mapping) or seen.get(row.get('id')) != 1:
            continue  # 与 wi_pricing._model 相同：不唯一即无价
        pricing = row.get('pricing')
        if not isinstance(pricing, Mapping):
            continue
        prompt, completion = rate_e12(pricing.get('prompt')), rate_e12(pricing.get('completion'))
        if prompt is None or completion is None:
            continue
        read, write = rate_e12(pricing.get('input_cache_read')), rate_e12(pricing.get('input_cache_write'))
        result[row['id']] = (prompt, MISSING if read is None else read,
                             MISSING if write is None else write, completion)
    return result


def amount_e12(rates: str | None, uncached: int | None, read: int | None, write: int | None,
               output: int | None) -> int | None:
    """视图里 `prices` 一行（'p,r,w,c'）× 四类 Token；缺所需单价或数量则 None。"""
    if rates is None or None in (uncached, read, write, output):
        return None
    prompt, cache_read, cache_write, completion = (int(x) for x in rates.split(','))
    if (cache_read < 0 and read) or (cache_write < 0 and write):
        return None
    return uncached * prompt + read * max(cache_read, 0) + write * max(cache_write, 0) + output * completion


def observed_day(retrieved_at: str) -> str:
    moment = datetime.fromisoformat(retrieved_at.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('catalog retrieved_at must be aware')
    return moment.astimezone(ZONE).date().isoformat()


def load(conn: sqlite3.Connection, snapshot: Mapping[str, Any]) -> dict[str, int]:
    """把一份价表快照（{'retrieved_at','catalog'}）并入 `prices`；同价不写。"""
    catalog = snapshot['catalog']
    sha = digest(catalog)
    day = observed_day(snapshot['retrieved_at'])
    rates = catalog_rates(catalog)
    inserted = updated = 0
    conn.execute('BEGIN IMMEDIATE')
    try:
        latest = {row[0]: row[1:] for row in conn.execute(
            'SELECT model_key, effective_from, prompt, cache_read, cache_write, completion FROM prices p '
            'WHERE effective_from = (SELECT max(effective_from) FROM prices q WHERE q.model_key = p.model_key)')}
        for key, values in sorted(rates.items()):
            current = latest.get(key)
            if current is not None and tuple(current[1:]) == values:
                continue
            effective = FIRST_EFFECTIVE if current is None else max(day, FIRST_EFFECTIVE)
            if current is not None and effective <= current[0]:
                # 同一天（或时钟回拨）又变价：改这一行，不让旧日期的价被覆盖到更早。
                effective = current[0]
                conn.execute('UPDATE prices SET prompt=?, cache_read=?, cache_write=?, completion=?, catalog_sha256=? '
                             'WHERE model_key=? AND effective_from=?', (*values, sha, key, effective))
                updated += 1
                continue
            conn.execute('INSERT INTO prices(model_key, effective_from, prompt, cache_read, cache_write, completion, '
                         'catalog_sha256) VALUES(?,?,?,?,?,?,?)', (key, effective, *values, sha))
            inserted += 1
        for name, value in ((META_SHA, sha), (META_OBSERVED, snapshot['retrieved_at'])):
            conn.execute('INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value '
                         'WHERE value <> excluded.value', (name, value))
        conn.execute('COMMIT')
    except BaseException:
        conn.execute('ROLLBACK')
        raise
    return {'models': len(rates), 'inserted': inserted, 'updated': updated}


def catalog_identity(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """最近装入的价表快照：(catalog_sha256, 观测时间)。"""
    rows = dict(conn.execute('SELECT key, value FROM meta WHERE key IN (?, ?)', (META_SHA, META_OBSERVED)))
    return rows.get(META_SHA), rows.get(META_OBSERVED)


def max_rates(conn: sqlite3.Connection) -> tuple[int | None, int | None, int | None, int | None] | None:
    """价表里各类 Token 观测到的最高单价；某类没有任何已知单价（全是缺价 -1）则该类为 None；价表为空则 None。"""
    row = conn.execute('SELECT max(prompt), max(cache_read), max(cache_write), max(completion) FROM prices').fetchone()
    if row is None or all(v is None for v in row):
        return None
    return tuple(None if v is None or v < 0 else int(v) for v in row)
