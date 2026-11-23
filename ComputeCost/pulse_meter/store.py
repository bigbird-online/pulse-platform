"""建库、幂等写入与变更序号（合同第 1 节）。

节点库与汇总库用同一份 DDL 与同一组写入函数：内容没变不写、change_seq 不动；
新插入或内容真变时从本库 meta.next_seq 取新号。usage_events 的合并与到达顺序无关。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import secrets
import sqlite3
from typing import Any, Iterable, Iterator, Mapping

from . import SCHEMA_VERSION, schema_sql

FACT_TABLES = ("usage_events", "wi_claims", "launch_receipts", "workspaces", "turn_origins", "session_entrypoints",
               "retractions")
# 由来源文件产生、可被撤销的表（workspaces 来自登记，不撤销）
RETRACTABLE = ("usage_events", "wi_claims", "launch_receipts", "turn_origins", "session_entrypoints")

KEYS = {
    "usage_events": ("node", "runtime", "event_id", "request_id"),
    "wi_claims": ("node", "runtime", "claim_id"),
    "launch_receipts": ("node", "receipt_id"),
    "workspaces": ("node", "workspace_root"),
    "turn_origins": ("node", "runtime", "session_ref", "turn_id"),
    "session_entrypoints": ("node", "runtime", "session_ref", "entrypoint", "source"),
    "retractions": ("node", "table_name", "row_key"),
}
CONTENT = {
    "usage_events": ("source", "granularity", "session_id", "session_ref", "agent_id", "turn_id",
                     "occurred_at", "model", "cwd", "input_uncached", "cache_read", "cache_write",
                     "output", "final"),
    "wi_claims": ("scope", "session_ref", "turn_id", "agent_id", "wi", "status", "occurred_at"),
    "launch_receipts": ("kind", "occurred_at", "runtime", "agent", "wi", "attempt_ref", "session_ref",
                        "job_id", "job_name", "entrypoint", "state"),
    "workspaces": ("agent",),
    "turn_origins": ("origin", "occurred_at"),
    "session_entrypoints": (),
    "retractions": (),
}
COLUMNS = {t: KEYS[t] + CONTENT[t] for t in FACT_TABLES}
DEFAULTS = {"request_id": "", "session_id": "", "agent_id": "", "turn_id": "", "final": 0}
TOKENS = ("input_uncached", "cache_read", "cache_write", "output")
# usage_events 的描述列取 (occurred_at, session_id, …) 较小的那一行；比较全部描述列，
# 两行完全并列时结果也唯一。
DESCRIPTIVE = ("occurred_at", "session_id", "source", "granularity", "session_ref", "agent_id",
               "turn_id", "model", "cwd")
STATUS_RANK = "CASE {0} WHEN 'pending' THEN 0 WHEN 'rejected' THEN 1 ELSE 2 END"


def home() -> Path:
    """账户主目录；Agent 的 HOME 是另一处，不用 $HOME。"""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def meter_dir() -> Path:
    return home() / ".local/state/pulse/meter"


def node_db_path() -> Path:
    return meter_dir() / "node.sqlite"


def central_db_path() -> Path:
    return meter_dir() / "central.sqlite"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def utc_text(value: object) -> str | None:
    """原始时间统一成 UTC ISO8601 毫秒字符串，保证按字典序可比。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    else:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def connect(path: str | Path, *, node: str | None = None) -> sqlite3.Connection:
    """打开（必要时新建）库：目录 0700，WAL，建表，补齐 meta。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    conn = sqlite3.connect(str(path), timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    legacy = "file_cursors" in tables and "file_rows" not in tables and \
        conn.execute("SELECT count(*) FROM file_cursors").fetchone()[0] > 0
    conn.executescript(schema_sql())
    if legacy:
        # 旧库没有来源文件登记：所有文件强制重读一遍补登记，读完后清扫没有来源的行（见 tail.sweep_orphans）
        conn.execute("UPDATE file_cursors SET parser_version=-1")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('orphan_sweep', 'pending')")
    conn.execute("BEGIN IMMEDIATE")
    defaults = {"schema_version": SCHEMA_VERSION, "db_epoch": secrets.token_hex(16), "next_seq": "1"}
    if node is not None:
        defaults["node"] = node
    for key, value in defaults.items():
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)", (key, value))
    conn.execute("COMMIT")
    version = meta(conn, "schema_version")
    if version != SCHEMA_VERSION:
        raise RuntimeError(f"unsupported meter schema_version {version!r}")
    return conn


def row_key(table: str, row: Mapping[str, Any]) -> str:
    return json.dumps([row.get(c, DEFAULTS.get(c)) for c in KEYS[table]], separators=(",", ":"), ensure_ascii=False)


def meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


@contextmanager
def single_instance(db_path: str | Path, *, wait_seconds: float = 0) -> Iterator[None]:
    """每个库一把 flock 单实例锁；等 wait_seconds 仍拿不到即报错退出。"""
    import time
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = open(str(db_path) + ".lock", "a+")
    give_up = time.monotonic() + wait_seconds
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= give_up:
                    raise RuntimeError(f"{db_path} is locked by another instance") from exc
                time.sleep(0.2)
        yield
    finally:
        handle.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator["Writer"]:
    conn.execute("BEGIN IMMEDIATE")
    writer = Writer(conn)
    try:
        yield writer
        writer.flush()
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _merge_expr(table: str, column: str) -> str:
    if table == "usage_events":
        if column in TOKENS:
            return (f"CASE WHEN excluded.{column} IS NULL THEN {column} WHEN {column} IS NULL "
                    f"THEN excluded.{column} ELSE max({column}, excluded.{column}) END")
        if column == "final":
            return "max(final, excluded.final)"
        # NULL 记作 X'FF'（BLOB 排在任何 TEXT 之后），并列时已知值胜出。
        new = ", ".join(f"coalesce(excluded.{c}, X'FF')" for c in DESCRIPTIVE)
        old = ", ".join(f"coalesce({c}, X'FF')" for c in DESCRIPTIVE)
        return f"CASE WHEN ({new}) < ({old}) THEN excluded.{column} ELSE {column} END"
    if table == "wi_claims":
        # status 只前进：pending → rejected → accepted 取较大者；其余列跟随较早一行。
        if column == "status":
            return (f"CASE WHEN {STATUS_RANK.format('excluded.status')} > {STATUS_RANK.format('status')} "
                    "THEN excluded.status ELSE status END")
        new = "excluded.occurred_at, excluded.wi, excluded.session_ref, excluded.turn_id, excluded.agent_id"
        old = "occurred_at, wi, session_ref, turn_id, agent_id"
        return f"CASE WHEN ({new}) < ({old}) THEN excluded.{column} ELSE {column} END"
    if table == "turn_origins":
        # 同一回合多次写入：取 (occurred_at, origin) 较小的一行，与到达顺序无关
        return f"CASE WHEN (excluded.occurred_at, excluded.origin) < (occurred_at, origin) THEN excluded.{column} ELSE {column} END"
    if table == "launch_receipts" and column == "agent":
        # 保留历史归属：新值为空（定时任务已从配置删除、owner 歧义）时沿用库中已有的 agent，
        # 只有给出明确 agent 时才更新（大鸟 11-22 定）
        return "COALESCE(excluded.agent, agent)"
    return f"excluded.{column}"


def _upsert_sql(table: str) -> str:
    cols = COLUMNS[table] + ("change_seq",)
    if not CONTENT[table]:
        return f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)}) ON CONFLICT DO NOTHING"
    content = CONTENT[table]
    sets = ", ".join(f"{c} = {_merge_expr(table, c)}" for c in content)
    differs = " OR ".join(f"{c} IS NOT {_merge_expr(table, c)}" for c in content)
    return (f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT({', '.join(KEYS[table])}) DO UPDATE SET {sets}, change_seq = excluded.change_seq "
            f"WHERE {differs}")


UPSERT = {t: _upsert_sql(t) for t in FACT_TABLES}


class Writer:
    """同一事务内的幂等写入；序号只在真插入或真变更时前进。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.seq = int(meta(conn, "next_seq") or "1")
        self.start = self.seq
        self.applied: dict[str, int] = {t: 0 for t in FACT_TABLES}

    def put(self, table: str, row: Mapping[str, Any]) -> bool:
        values = [row.get(c, DEFAULTS.get(c)) for c in COLUMNS[table]] + [self.seq]
        cursor = self.conn.execute(UPSERT[table], values)
        if cursor.rowcount > 0:
            self.seq += 1
            self.applied[table] += 1
            if table in RETRACTABLE:
                # 已撤销的行再次产生：删掉还没上送的墓碑，免得同批里先插后删
                self.conn.execute("DELETE FROM retractions WHERE node=? AND table_name=? AND row_key=?",
                                  (row.get("node"), table, row_key(table, row)))
            return True
        return False

    def put_many(self, items: Iterable[tuple[str, Mapping[str, Any]]]) -> int:
        return sum(self.put(table, row) for table, row in items)

    def retract(self, table: str, key: list[Any]) -> bool:
        """节点撤销一行：从事实表删除，记墓碑（新序号，随批上送）。"""
        if not self._remove(table, key):
            return False
        self.conn.execute("INSERT OR REPLACE INTO retractions(node, table_name, row_key, change_seq) VALUES(?,?,?,?)",
                          (key[0], table, json.dumps(key, separators=(",", ":"), ensure_ascii=False), self.seq))
        self.seq += 1
        self.applied["retractions"] += 1
        return True

    def delete(self, table: str, key: list[Any]) -> bool:
        """汇总库应用墓碑：按主键删一行；删到了才算真变，并让 meta.next_seq 前进一号，
        使发布水位（next_seq − 1）随撤销变化，只含撤销的批次也会触发重新发布。"""
        if not self._remove(table, key):
            return False
        self.seq += 1
        return True

    def _remove(self, table: str, key: list[Any]) -> bool:
        where = " AND ".join(f"{c} = ?" for c in KEYS[table])
        cursor = self.conn.execute(f"DELETE FROM {table} WHERE {where}", key)
        if cursor.rowcount > 0:
            self.applied[table] += 1
            return True
        return False

    def flush(self) -> None:
        if self.seq != self.start:
            self.conn.execute("UPDATE meta SET value=? WHERE key='next_seq'", (str(self.seq),))
            self.start = self.seq


def write_rows(conn: sqlite3.Connection, items: Iterable[tuple[str, Mapping[str, Any]]]) -> dict[str, int]:
    """一个事务写入一批 (表名, 行)，返回各表真变行数。"""
    with transaction(conn) as writer:
        writer.put_many(items)
    return writer.applied


def table_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    cols = ", ".join(COLUMNS[table])
    order = ", ".join(KEYS[table])
    return [dict(r) for r in conn.execute(f"SELECT {cols} FROM {table} ORDER BY {order}")]
