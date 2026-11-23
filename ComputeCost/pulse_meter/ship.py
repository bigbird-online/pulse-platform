"""节点库到汇总库的传输：发送、接收、本地并入（合同第 3 节）。

发送端收到合格 ack 才推进 ship_cursors；任何失败本轮不重试，下一轮从原处重发。
接收端整批一个事务，按 store 的幂等规则写入，节点序号只进 transfer_cursors。

完整性：批头 `pending_rows` 是本批确认后节点仍未获确认的行数（`node_seq` 是节点最新变更序号，供排查）；
汇总库把最近一批的 pending_rows 记在 meta 的 `pending_unacked:<节点>`（十进制字符串），
不为 "0" 即该节点数据未传完（publish.node_coverage 据此判 complete）。
代次：ack 带汇总库的 db_epoch（`central_epoch`）。节点按目的地记在 meta 的 `ship_epoch:<目的地>`；
代次变了（汇总库重建）就把该目的地的确认游标归零，全量重放，幂等写入保证不重复。
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
from typing import Any, Callable, Iterable, Mapping

from . import store

# v2：批内含 session_entrypoints。接收端只收 v2；v1 接收端遇 v2 按“batch header mismatch”整批拒收，
# 发送端游标不动、记 ship_state 为 failed，接收端升级后下一轮从原处补齐。部署：先升接收端，回滚反序。
BATCH_SCHEMA = "pulse_meter_batch/v2"
ACK_SCHEMA = "pulse_meter_ack/v1"
MAX_ROWS = 5000
MAX_GZIP_BYTES = 4 * 1024 * 1024
LOCAL_DEST = "local"


class ShipError(RuntimeError):
    pass


def _acked(conn, dest: str) -> dict[str, int]:
    rows = conn.execute("SELECT table_name, acked_seq FROM ship_cursors WHERE dest=?", (dest,)).fetchall()
    got = {r[0]: r[1] for r in rows}
    return {t: got.get(t, 0) for t in store.FACT_TABLES}


def _pending_rows(conn, acked: Mapping[str, int], limit: int) -> list[dict[str, Any]]:
    """按表、按 change_seq 升序取未确认的行，合计至多 limit 行。"""
    out: list[dict[str, Any]] = []
    for table in store.FACT_TABLES:
        if len(out) >= limit:
            break
        cols = ", ".join(store.COLUMNS[table] + ("change_seq",))
        for row in conn.execute(f"SELECT {cols} FROM {table} WHERE change_seq > ? ORDER BY change_seq LIMIT ?",
                                (acked[table], limit - len(out))):
            out.append({"t": table, **dict(row)})
    return out


def source_details(conn) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT runtime, count(*), sum(offset >= size) FROM file_cursors GROUP BY runtime ORDER BY runtime")
    return [{"runtime": r[0], "files_seen": r[1], "files_read": r[2] or 0} for r in rows]


def _pending_count(conn, acked: Mapping[str, int]) -> int:
    return sum(conn.execute(f"SELECT count(*) FROM {t} WHERE change_seq > ?", (acked[t],)).fetchone()[0]
               for t in store.FACT_TABLES)


def header(conn, peer_id: str, *, pending_rows: int) -> dict[str, Any]:
    return {"schema": BATCH_SCHEMA, "peer": peer_id, "db_epoch": store.meta(conn, "db_epoch"),
            "backlog_bytes": int(store.meta(conn, "backlog_bytes") or 0),
            "backlog_files": int(store.meta(conn, "backlog_files") or 0),
            "pending_rows": pending_rows, "node_seq": int(store.meta(conn, "next_seq") or 1) - 1,
            "source_details": source_details(conn)}


def encode(head: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> bytes:
    lines = [json.dumps(head, separators=(",", ":"), ensure_ascii=False)]
    lines.extend(json.dumps(r, separators=(",", ":"), ensure_ascii=False) for r in rows)
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"), mtime=0)


def build_batch(conn, peer_id: str, acked: Mapping[str, int]) -> tuple[bytes, dict[str, int], int]:
    """(gzip 批, 各表本批最大序号, 行数)；超过 4MB 时减半行数重来。"""
    limit = MAX_ROWS
    total = _pending_count(conn, acked)
    while True:
        rows = _pending_rows(conn, acked, limit)
        data = encode(header(conn, peer_id, pending_rows=total - len(rows)), rows)
        if len(data) <= MAX_GZIP_BYTES or limit == 1:
            break
        limit = max(1, limit // 2)
    if len(data) > MAX_GZIP_BYTES:
        raise ShipError("single row exceeds the batch size bound")
    top: dict[str, int] = {}
    for row in rows:
        top[row["t"]] = max(top.get(row["t"], 0), row["change_seq"])
    return data, top, len(rows)


def _check_ack(raw: bytes, expected: Mapping[str, int]) -> tuple[dict[str, int], str]:
    try:
        ack = json.loads(raw.decode("utf-8").strip().splitlines()[-1])
    except (ValueError, IndexError, UnicodeDecodeError) as exc:
        raise ShipError("ack is not JSON") from exc
    acked, applied = ack.get("acked"), ack.get("applied")
    if ack.get("schema") != ACK_SCHEMA or not isinstance(acked, dict) or not isinstance(applied, dict):
        raise ShipError("ack schema mismatch")
    for table, seq in expected.items():
        if acked.get(table) != seq:
            raise ShipError(f"ack for {table} is {acked.get(table)!r}, expected {seq}")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in applied.values()):
        raise ShipError("ack applied counts are invalid")
    epoch = ack.get("central_epoch")
    if not isinstance(epoch, str) or not epoch:
        raise ShipError("ack has no central_epoch")
    return applied, epoch


def _epoch_changed(conn, dest: str, epoch: str) -> bool:
    """汇总库代次变了（或从未记过而游标已非零）就把该目的地游标归零；返回是否归零。"""
    key = f"ship_epoch:{dest}"
    known = store.meta(conn, key)
    if known == epoch:
        return False
    conn.execute("BEGIN IMMEDIATE")
    reset = known is not None or conn.execute(
        "SELECT count(*) FROM ship_cursors WHERE dest=? AND acked_seq > 0", (dest,)).fetchone()[0] > 0
    if reset:
        conn.execute("DELETE FROM ship_cursors WHERE dest=?", (dest,))
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, epoch))
    conn.execute("COMMIT")
    return reset


def _advance(conn, dest: str, top: Mapping[str, int]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    for table, seq in top.items():
        conn.execute("INSERT INTO ship_cursors(dest, table_name, acked_seq) VALUES(?,?,?) ON CONFLICT(dest, table_name)"
                     " DO UPDATE SET acked_seq=max(acked_seq, excluded.acked_seq)", (dest, table, seq))
    conn.execute("COMMIT")


def ssh_transport(peer: Mapping[str, Any], peer_id: str) -> Callable[[bytes, float], bytes]:
    """沿用 pulse-stats-cost-refresh.py 的 SSH 参数；远端入口是同一发布目录的 pulse-meter.py。"""
    host = peer["host"]
    if not re.fullmatch(r"[a-zA-Z0-9_.@-]+", host) or host.startswith("-"):
        raise ShipError("invalid SSH alias")
    prefix = list(peer.get("prefix", []))
    if prefix not in ([], ["runuser", "-u", "pulse", "--"]):
        raise ShipError("unsupported service-user prefix")
    script = str(Path(peer["script"]).parent / "pulse-meter.py")
    command = prefix + [peer["python"], "-I", "-B", script, "receive", "--peer", peer_id]

    def send(data: bytes, timeout: float) -> bytes:
        try:
            result = subprocess.run(
                ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10",
                 "-o", "ServerAliveCountMax=2", host, shlex.join(command)],
                input=data, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ShipError(f"ssh failed: {exc.__class__.__name__}") from exc
        if result.returncode:
            raise ShipError(f"remote exit {result.returncode}: {result.stderr[-300:].decode('utf-8', 'replace')}")
        return result.stdout
    return send


def send(node_db, publisher_peer, peer_id: str, *, deadline_seconds: float | None = 90,
         transport: Callable[[bytes, float], bytes] | None = None) -> dict[str, Any]:
    """一轮发送：连续发到取空或到时限。首批即使为空也发，让汇总端知道本节点在线与积压。"""
    started = time.monotonic()
    deadline = started + deadline_seconds if deadline_seconds is not None else float("inf")
    if transport is None:
        transport = ssh_transport(publisher_peer, peer_id)
    if isinstance(publisher_peer, Mapping):
        dest = str(publisher_peer.get("id") or publisher_peer.get("host"))
    else:
        dest = str(publisher_peer)
    conn = store.connect(node_db)
    sent = applied = batches = resets = 0
    error = None
    try:
        while True:
            acked = _acked(conn, dest)
            data, top, count = build_batch(conn, peer_id, acked)
            if count == 0 and batches:
                break
            try:
                result, epoch = _check_ack(transport(data, max(10.0, deadline - time.monotonic())), top)
            except ShipError as exc:
                error = str(exc)
                break
            batches += 1
            sent += count
            applied += sum(result.values())
            if _epoch_changed(conn, dest, epoch):
                resets += 1
                if resets > 1 or time.monotonic() > deadline:
                    break
                continue                      # 汇总库重建：从头重放
            _advance(conn, dest, top)
            if count == 0 or time.monotonic() > deadline:
                break
    finally:
        conn.close()
    out = {"sent": sent, "applied": applied, "batches": batches, "epoch_resets": resets}
    if error:
        out["error"] = error
    _record_state(node_db, dest, "failed" if error else "ok", error)
    return out


def _record_state(node_db, dest: str, state: str, error: str | None) -> None:
    """本轮发送结果记在节点 meta 的 ship_state:<目的地>（state、error、at），供排查与告警。"""
    conn = store.connect(node_db)
    try:
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (f"ship_state:{dest}", json.dumps({"state": state, "error": error, "at": store.utc_now()},
                                                      ensure_ascii=False)))
    finally:
        conn.close()


def _decode(data: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        lines = gzip.decompress(data).decode("utf-8").splitlines()
        records = [json.loads(line) for line in lines if line.strip()]
    except (OSError, ValueError, EOFError) as exc:
        raise ShipError("batch is not gzip JSONL") from exc
    if not records or not isinstance(records[0], dict) or records[0].get("schema") != BATCH_SCHEMA:
        raise ShipError("batch header mismatch")
    return records[0], records[1:]


def apply_batch(central, head: Mapping[str, Any], rows: list[Mapping[str, Any]], *, peer: str,
                allowed_peers: Iterable[str] | None = None) -> dict[str, Any]:
    """校验并在一个事务里写入一批；返回 ack。central 为已打开的汇总库连接。"""
    if head.get("peer") != peer or (allowed_peers is not None and peer not in set(allowed_peers)):
        raise ShipError(f"peer {head.get('peer')!r} is not registered")
    epoch = head.get("db_epoch")
    if not isinstance(epoch, str) or not epoch:
        raise ShipError("batch has no db_epoch")
    top: dict[str, int] = {}
    clean: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        table = row.get("t")
        if table not in store.FACT_TABLES or row.get("node") != peer:
            raise ShipError("row table or node does not match the peer")
        seq = row.get("change_seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise ShipError("row has no change_seq")
        top[table] = max(top.get(table, 0), seq)
        if table == "retractions":
            _check_tombstone(row, peer)
        clean.append((table, {c: row.get(c) for c in store.COLUMNS[table]}))
    pending = head.get("pending_rows")
    if not isinstance(pending, int) or isinstance(pending, bool) or pending < 0:
        raise ShipError("batch has no pending_rows")
    now = store.utc_now()
    with store.transaction(central) as writer:
        for table, row in clean:
            if table == "retractions":                # 墓碑：节点已撤销这一行，汇总库删掉它（不留墓碑）
                if row["table_name"] in store.RETRACTABLE:
                    writer.delete(row["table_name"], json.loads(row["row_key"]))
            else:
                writer.put(table, row)
        for table in store.FACT_TABLES:
            central.execute(
                "INSERT INTO transfer_cursors(peer, db_epoch, table_name, last_seq, last_received_at, backlog_bytes,"
                " backlog_files) VALUES(?,?,?,?,?,?,?) ON CONFLICT(peer, db_epoch, table_name) DO UPDATE SET"
                " last_seq=max(last_seq, excluded.last_seq), last_received_at=excluded.last_received_at,"
                " backlog_bytes=excluded.backlog_bytes, backlog_files=excluded.backlog_files",
                (peer, epoch, table, top.get(table, 0), now, int(head.get("backlog_bytes") or 0),
                 int(head.get("backlog_files") or 0)))
        central.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (f"pending_unacked:{peer}", str(pending)))
        if head.get("source_details") is not None:
            central.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (f"source_details:{peer}", json.dumps(head["source_details"], separators=(",", ":"))))
    return {"schema": ACK_SCHEMA, "acked": top, "applied": {t: n for t, n in writer.applied.items() if n or t in top},
            "central_epoch": store.meta(central, "db_epoch")}


def _check_tombstone(row: Mapping[str, Any], peer: str) -> None:
    """墓碑必须指向白名单里的表、主键形状合法且属于发送节点；不合法整批拒收。"""
    table = row.get("table_name")
    if table not in store.RETRACTABLE:
        raise ShipError(f"tombstone for table {table!r} is not allowed")
    try:
        key = json.loads(row.get("row_key"))
    except (TypeError, ValueError) as exc:
        raise ShipError("tombstone row_key is not JSON") from exc
    if (not isinstance(key, list) or len(key) != len(store.KEYS[table])
            or any(not isinstance(v, str) for v in key) or key[0] != peer):
        raise ShipError("tombstone row_key does not match the table key or the peer")


def receive(central_db, data: bytes, *, peer: str, allowed_peers: Iterable[str] | None) -> dict[str, Any]:
    head, rows = _decode(data)
    with store.single_instance(central_db, wait_seconds=60):
        central = store.connect(central_db)
        try:
            return apply_batch(central, head, rows, peer=peer, allowed_peers=allowed_peers)
        finally:
            central.close()


def apply_local(node_db, central_db) -> dict[str, Any]:
    """Oracle 本机节点库不走 SSH，按同一写入函数并入汇总库，直到取空。"""
    conn = store.connect(node_db)
    sent = applied = 0
    try:
        peer = store.meta(conn, "node")
        if not peer:
            raise ShipError("node database has no node name")
        with store.single_instance(central_db, wait_seconds=60):
            central = store.connect(central_db)
            try:
                _epoch_changed(conn, LOCAL_DEST, store.meta(central, "db_epoch"))
                first = True
                while True:
                    acked = _acked(conn, LOCAL_DEST)
                    rows = _pending_rows(conn, acked, MAX_ROWS)
                    if not rows and not first:
                        break
                    first = False
                    pending = _pending_count(conn, acked) - len(rows)
                    ack = apply_batch(central, header(conn, peer, pending_rows=pending), rows, peer=peer)
                    _advance(conn, LOCAL_DEST, ack["acked"])
                    sent += len(rows)
                    applied += sum(ack["applied"].values())
                    if not rows:
                        break
            finally:
                central.close()
    finally:
        conn.close()
    return {"sent": sent, "applied": applied}
