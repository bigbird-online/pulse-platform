"""追尾：按游标逐行读原始记录，交给各 Runtime 的抽取器，幂等写进节点库（合同第 2 节）。

只消费以 \\n 结尾的完整行；inode 变、文件变短、文件头变、parser_version 升，任一发生即从 0 重读。
行与游标在同一事务落盘，中途退出不丢不重。符号链接一律跳过。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import time
from typing import Any, Callable, Mapping

from . import store

HEAD_BYTES = 4096
RETRACT = "retract:"          # 抽取器产出 ("retract:<表>", 行) 即撤销该行
PATH_FIELD = "__path__"       # 行的来源文件不是当前文件时由抽取器指明
CHUNK_BYTES = 8 * 1024 * 1024
# 10 月及以前不迁移（合同前提 P2）：最后修改早于 11-01（Asia/Shanghai）的文件不会含 11 月事件。
SINCE = "2026-11-01T00:00:00+08:00"
KINDS = ("cc", "codex", "pi", "relay_host")


@dataclass
class FileContext:
    node: str
    path: Path
    runtime: str                      # cc | codex | pi | host
    source: Mapping[str, Any] = field(default_factory=dict)
    shared: dict[str, Any] = field(default_factory=dict)   # 本轮共享索引（人类输入渠道、Codex 父线程）
    diagnostics: dict[str, int] = field(default_factory=dict)
    conn: Any = None

    def note(self, reason: str) -> None:
        self.diagnostics[reason] = self.diagnostics.get(reason, 0) + 1


def extractor_for(runtime: str):
    import importlib
    return importlib.import_module(f"{__package__}.extract_{runtime}").Extractor


RELAY_PATHS_ENV = "PULSE_RELAY_PATHS"
CRON_JOBS_PATH_ENV = "CRON_JOBS_PATH"


def _relay_path_map(file: str | Path) -> Mapping[str, Any] | None:
    try:
        document = json.loads(Path(file).read_bytes())
    except (OSError, ValueError):
        return None
    paths = document.get("paths") if isinstance(document, Mapping) else None
    return paths if isinstance(paths, Mapping) else None


def cron_jobs_path(env: Mapping[str, str] | None = None) -> Path | None:
    """本机 Relay 的 cron-jobs.json，沿用 Relay/src/config-paths.ts resolveCronJobsPath 的定位顺序：
    设了 $PULSE_RELAY_PATHS 就取其路径图的 cronConfig（受管实例以路径图为准）→ $CRON_JOBS_PATH →
    <PULSE>/data/cron-jobs.json。计量入口（ownermac launchd collect-local、Oracle systemd refresh）的环境里
    没有这两个变量，而两台机器的 Relay 都以 $PULSE_RELAY_PATHS=~/.local/share/pulse/config/relay/paths.json
    运行（11-22 核过），所以变量都没有时先读这张受管路径图，再退到 Relay 的仓内默认。"""
    env = os.environ if env is None else env
    explicit = env.get(RELAY_PATHS_ENV)
    if explicit:
        paths = _relay_path_map(explicit)
        if paths and isinstance(paths.get("cronConfig"), str):
            return Path(paths["cronConfig"])
    if env.get(CRON_JOBS_PATH_ENV):
        return Path(env[CRON_JOBS_PATH_ENV])
    paths = _relay_path_map(store.home() / ".local/share/pulse/config/relay/paths.json")
    if paths and isinstance(paths.get("cronConfig"), str):
        return Path(paths["cronConfig"])
    default = Path(__file__).resolve().parents[2] / "data" / "cron-jobs.json"
    return default if default.exists() else None


def load_registry(registry: str | Path | Mapping[str, Any] | None) -> Mapping[str, Any]:
    if registry is None:
        registry = store.home() / ".local/state/pulse/compute-cost/sources.json"
    if isinstance(registry, Mapping):
        return registry
    return json.loads(Path(registry).read_bytes())


JSONL = (".jsonl", ".jsonl.zst")


def compressed(path: Path) -> bool:
    """Codex 归档会话压缩成 .jsonl.zst（11 月有 hestia 一个 42MB 的会话）；整文件一次读完，不可续读。"""
    return path.name.endswith(".zst")


def _zst_batches(path: Path):
    from compression import zstd
    with zstd.open(path, "rb") as handle:
        batch, size = [], 0
        for line in handle:
            if not line.endswith(b"\n"):
                break
            batch.append(line[:-1])
            size += len(line)
            if size >= CHUNK_BYTES:
                yield batch
                batch, size = [], 0
        yield batch


def _walk(root: Path, accept: Callable[[Path, Path], bool]) -> list[Path]:
    """不跟随符号链接的递归枚举；accept(root, path) 决定是否收。"""
    if not root.exists() or root.is_symlink():
        return []
    if root.is_file():
        return [root]
    found = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        here = Path(directory)
        dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink())
        for name in sorted(files):
            path = here / name
            if name.endswith(JSONL) and not path.is_symlink() and accept(root, path):
                found.append(path)
    return found


def _cc_accept(root: Path, path: Path) -> bool:
    parts = path.relative_to(root).parts
    # <项目>/<会话>.jsonl，或 <项目>/<会话>/subagents/agent-<号>.jsonl
    return len(parts) == 2 or (len(parts) == 4 and parts[2] == "subagents")


def _pi_paths(source: Mapping[str, Any]) -> list[Path]:
    from pulse_compute_cost.source_layout import manifest_paths, pi_files
    roots = manifest_paths(source) or [Path(p) for p in ([source["path"]] if "path" in source else source.get("paths", []))]
    result = []
    for root in roots:
        if root.exists() and not root.is_symlink():
            result.extend(p for p in pi_files(root, source.get("layout"), {"files_invalid": 0, "errors": {}, "excluded_directories": 0}) if not p.is_symlink())
    return result


def enumerate_sources(registry: Mapping[str, Any]) -> list[tuple[str, Path, Mapping[str, Any]]]:
    """(runtime, 文件, 所属登记) 列表，同一文件只出现一次。"""
    result: dict[Path, tuple[str, Path, Mapping[str, Any]]] = {}
    for source in registry.get("sources", []):
        kind = source.get("kind")
        if kind not in KINDS:
            continue
        roots = [Path(p) for p in ([source["path"]] if "path" in source else []) + list(source.get("paths", []))]
        if kind == "codex" and source.get("calendar_root"):
            roots.append(Path(source["calendar_root"]))
        if kind == "cc":
            files = [p for r in roots for p in _walk(r, _cc_accept) if not compressed(p)]
        elif kind == "codex":
            files = [p for r in roots for p in _walk(r, lambda _r, p: p.name.startswith("rollout-"))]
        elif kind == "pi":
            files = _pi_paths(source)
        else:
            files = [p for r in roots for p in _walk(r, lambda r, p: p.parent == r and not compressed(p))]
        runtime = "host" if kind == "relay_host" else kind
        for path in files:
            result.setdefault(path, (runtime, path, source))
        if kind == "cc":
            # 同一 HOME 下的 Codex 会话（如 Relay agent-homes/<agent>/.codex/sessions）没有单独登记：
            # 登记了 <HOME>/.claude/projects 就一并追 <HOME>/.codex/sessions
            for root in roots:
                if root.name == "projects" and root.parent.name == ".claude":
                    sibling = root.parent.parent / ".codex" / "sessions"
                    for path in _walk(sibling, lambda _r, p: p.name.startswith("rollout-")):
                        result.setdefault(path, ("codex", path, {**source, "kind": "codex"}))
    return sorted(result.values(), key=lambda item: str(item[1]))


def workspace_rows(registry: Mapping[str, Any], node: str) -> list[tuple[str, dict[str, Any]]]:
    chosen: dict[str, str] = {}
    for source in registry.get("sources", []):
        for actor in source.get("workspace_actors") or []:
            root, agent = actor.get("workspace_root"), actor.get("agent")
            if isinstance(root, str) and isinstance(agent, str) and actor.get("node", node) == node:
                chosen[root] = min(agent, chosen.get(root, agent))
    return [("workspaces", {"node": node, "workspace_root": r, "agent": a}) for r, a in sorted(chosen.items())]


def registry_node(registry: Mapping[str, Any]) -> str:
    nodes = {s.get("node") for s in registry.get("sources", []) if isinstance(s.get("node"), str)}
    if len(nodes) == 1:
        return nodes.pop()
    return socket.gethostname().split(".")[0]


def _head(path: Path, size: int) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read(min(HEAD_BYTES, size))).hexdigest()


def _since_epoch(since: str | None) -> float | None:
    return datetime.fromisoformat(since).timestamp() if since else None


def _complete_lines(handle, offset: int, limit: int) -> tuple[list[bytes], int]:
    """从 offset 读至多约 limit 字节的完整行；返回 (行列表, 新 offset)。超长行整行读完。"""
    handle.seek(offset)
    data = handle.read(limit)
    end = data.rfind(b"\n")
    while end < 0 and data:
        more = handle.read(limit)
        if not more:
            return [], offset
        data += more
        end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    body = data[:end]
    return body.split(b"\n"), offset + end + 1


def process_file(conn, ctx: FileContext, extractor_cls, deadline: float) -> tuple[int, int, int]:
    """追一个文件；返回 (真变行数, 剩余积压字节, 本文件是否读过)。"""
    try:
        st = ctx.path.lstat()
    except FileNotFoundError:
        return 0, 0, 0
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return 0, 0, 0
    key = str(ctx.path)
    row = conn.execute("SELECT * FROM file_cursors WHERE path=?", (key,)).fetchone()
    version = extractor_cls.version(ctx.shared) if hasattr(extractor_cls, "version") else extractor_cls.PARSER_VERSION
    offset, state = 0, {}
    if row is not None:
        reset = (row["inode"] != st.st_ino or st.st_size < row["offset"] or row["parser_version"] != version
                 or _head(ctx.path, row["size"]) != row["head_sha256"])
        if not reset:
            offset, state = row["offset"], json.loads(row["state"])
            if offset == st.st_size and row["size"] == st.st_size:
                # 上次重读已到文件尾却没完成撤销（旧版本两步提交间中断）：在此补做
                return _recover_reread(conn, key), 0, 0
        else:
            _begin_reread(conn, key)
    changed = 0
    extractor = extractor_cls(ctx, state)
    if compressed(ctx.path):
        batches = list(_zst_batches(ctx.path))
        for index, lines in enumerate(batches):
            final = index == len(batches) - 1
            changed += _commit(conn, ctx, extractor, lines, st, st.st_size if final else 0, version, final=final)
        return changed, 0, 1
    with open(ctx.path, "rb") as handle:
        while offset < st.st_size:
            lines, new_offset = _complete_lines(handle, offset, CHUNK_BYTES)
            if new_offset == offset:
                break
            offset = new_offset
            changed += _commit(conn, ctx, extractor, lines, st, offset, version, final=offset >= st.st_size)
            if time.monotonic() > deadline:
                break
    if offset >= st.st_size:
        changed += _recover_reread(conn, key)         # 空文件重读：没有批次可带撤销
    return changed, max(0, st.st_size - offset), 1


# 撤销：每行记下产生它的来源文件（file_rows）。整文件重读开始时把旧登记改挂到 STALE+路径名下，
# 读完后旧登记里这次不再产生、且没有别的文件产生的行从事实表删除并记墓碑（retractions，随批上送，汇总库删行）。
# 文件被删除不撤销：用量确实发生过，只是原始记录没了。
STALE = "\x00stale:"


def _begin_reread(conn, key: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    if conn.execute("SELECT 1 FROM file_rows WHERE path=? LIMIT 1", (STALE + key,)).fetchone():
        conn.execute("DELETE FROM file_rows WHERE path=?", (key,))      # 上一次重读没读完：丢掉半截新登记
    else:
        conn.execute("UPDATE file_rows SET path=? WHERE path=?", (STALE + key, key))
    conn.execute("COMMIT")


def _has_stale(conn, key: str) -> bool:
    return conn.execute("SELECT 1 FROM file_rows WHERE path=? LIMIT 1", (STALE + key,)).fetchone() is not None


def _recover_reread(conn, key: str) -> int:
    if not _has_stale(conn, key):
        return 0
    with store.transaction(conn) as writer:
        return _finish_reread(conn, writer, key)


def _finish_reread(conn, writer, key: str) -> int:
    """在调用方的事务里完成重读：撤销旧登记里不再产生的行，与文件尾游标同一事务提交。"""
    stale = conn.execute("SELECT table_name, row_key FROM file_rows WHERE path=? AND NOT EXISTS("
                         "SELECT 1 FROM file_rows n WHERE n.path=? AND n.table_name=file_rows.table_name"
                         " AND n.row_key=file_rows.row_key)", (STALE + key, key)).fetchall()
    conn.execute("DELETE FROM file_rows WHERE path=?", (STALE + key,))
    retracted = 0
    for table, row_key in stale:
        if conn.execute("SELECT 1 FROM file_rows WHERE table_name=? AND row_key=? AND substr(path, 1, 1) <> ?"
                        " LIMIT 1", (table, row_key, STALE[0])).fetchone():
            continue                                                    # 别的文件仍产生这一行
        retracted += writer.retract(table, json.loads(row_key))
    return retracted


def sweep_orphans(conn) -> int:
    """旧库升级后的一次性清扫：所有文件已按新规则重读并登记来源后，没有任何来源文件的行撤销。"""
    if store.meta(conn, "orphan_sweep") != "pending" or \
            conn.execute("SELECT 1 FROM file_cursors WHERE parser_version = -1 LIMIT 1").fetchone():
        return 0                                                        # 还有文件没按新规则重读
    with store.transaction(conn) as writer:
        retracted = 0
        for table in store.RETRACTABLE:
            for row in conn.execute(f"SELECT {', '.join(store.KEYS[table])} FROM {table}").fetchall():
                values = list(row)
                key = store.row_key(table, dict(zip(store.KEYS[table], values)))
                if not conn.execute("SELECT 1 FROM file_rows WHERE table_name=? AND row_key=? LIMIT 1",
                                    (table, key)).fetchone():
                    retracted += writer.retract(table, values)
        conn.execute("UPDATE meta SET value='done' WHERE key='orphan_sweep'")
    return retracted


def _commit(conn, ctx: FileContext, extractor, lines: list[bytes], st, offset: int, version: int,
            *, final: bool = False) -> int:
    """抽一批行，与游标在同一事务落盘；读到文件尾的那一批连同重读撤销一起提交。"""
    items: list[tuple[str, dict[str, Any]]] = []
    for line in lines:
        if line.strip() and extractor.wants(line):
            items.extend(extractor.feed(line))
    size = st.st_size if compressed(ctx.path) else max(st.st_size, offset)
    with store.transaction(conn) as writer:
        changed = 0
        for table, row in items:
            if table.startswith(RETRACT):
                # 抽取器要求撤销的行（迟到计数使后继增量重算）：删行记墓碑，并去掉它的来源登记
                table = table[len(RETRACT):]
                changed += writer.retract(table, [row.get(c, store.DEFAULTS.get(c)) for c in store.KEYS[table]])
                conn.execute("DELETE FROM file_rows WHERE table_name=? AND row_key=?", (table, store.row_key(table, row)))
                continue
            changed += writer.put(table, row)
            if table in store.RETRACTABLE:
                conn.execute("INSERT OR IGNORE INTO file_rows(path, table_name, row_key) VALUES(?,?,?)",
                             (row.get(PATH_FIELD) or str(ctx.path), table, store.row_key(table, row)))
        if hasattr(extractor, "flush"):
            extractor.flush(conn)
        conn.execute(
            "INSERT INTO file_cursors(path, runtime, inode, size, offset, head_sha256, parser_version, state, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET runtime=excluded.runtime,"
            " inode=excluded.inode, size=excluded.size, offset=excluded.offset, head_sha256=excluded.head_sha256,"
            " parser_version=excluded.parser_version, state=excluded.state, updated_at=excluded.updated_at",
            (str(ctx.path), ctx.runtime, st.st_ino, size, offset, _head(ctx.path, size), version,
             json.dumps(extractor.state(), separators=(",", ":")), store.utc_now()))
        if final and _has_stale(conn, str(ctx.path)):
            changed += _finish_reread(conn, writer, str(ctx.path))
    return changed


def current_version(runtime: str, shared: dict[str, Any]) -> int:
    cls = extractor_for(runtime)
    return cls.version(shared) if hasattr(cls, "version") else cls.PARSER_VERSION


def _pending_bytes(conn, path: Path, version: int | None = None) -> int:
    """本文件尚未按当前规则读完的字节数。游标的解析版本与当前不同（升版本、owner 映射变了）即整份待重读，
    计全长（空文件计 1），使积压非 0、发布端不判完整。"""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return 0
    row = conn.execute("SELECT offset, inode, parser_version FROM file_cursors WHERE path=?", (str(path),)).fetchone()
    if row is None or row["inode"] != st.st_ino or st.st_size < row["offset"] \
            or (version is not None and row["parser_version"] != version):
        return max(st.st_size, 1) if row is not None else st.st_size
    return st.st_size - row["offset"]


def run(node_db, registry, *, deadline_seconds: float = 90, since: str | None = SINCE) -> dict[str, Any]:
    """一轮追尾。返回 rows_changed、backlog_bytes、backlog_files，另附 files_seen、files_read、diagnostics。"""
    started = time.monotonic()
    deadline = started + deadline_seconds if deadline_seconds is not None else float("inf")
    registry = load_registry(registry)
    node = registry_node(registry)
    floor = _since_epoch(since)
    with store.single_instance(node_db):
        conn = store.connect(node_db, node=node)
        node = store.meta(conn, "node") or node
        try:
            changed = sum(store.write_rows(conn, workspace_rows(registry, node)).values())
            files = enumerate_sources(registry)
            if floor is not None:
                # 启动记录不受下限约束：累计计数的基线可能在 10 月的文件里
                files = [f for f in files if f[0] == "host" or _mtime(f[1]) >= floor]
            # 启动记录按写入先后读（每个文件是一个 Relay 进程的流，互不交错），累计计数才能按时间取差
            files.sort(key=lambda f: (f[0] != "host", _mtime(f[1]) if f[0] == "host" else 0, str(f[1])))
            # Codex 子代理线程上溯根线程要按会话号找父文件；用全部登记的 Codex 文件（不受时间下限约束）
            shared: dict[str, Any] = {"codex_files": [p for r, p, _ in enumerate_sources(registry) if r == "codex"],
                                      "cron_jobs_path": cron_jobs_path()}
            diagnostics: dict[str, int] = {}
            backlog_bytes = backlog_files = files_read = 0
            for runtime, path, source in files:
                if files_read and time.monotonic() > deadline:   # 每轮至少推进一个文件
                    pending = _pending_bytes(conn, path, current_version(runtime, shared))
                else:
                    ctx = FileContext(node, path, runtime, source, shared, diagnostics, conn)
                    rows, pending, read = process_file(conn, ctx, extractor_for(runtime), deadline)
                    changed += rows
                    files_read += read
                if pending:
                    backlog_bytes += pending
                    backlog_files += 1
            if backlog_bytes == 0:
                changed += sweep_orphans(conn)
            conn.execute("BEGIN IMMEDIATE")
            for key, value in (("backlog_bytes", backlog_bytes), ("backlog_files", backlog_files)):
                conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, str(value)))
            conn.execute("COMMIT")
        finally:
            conn.close()
    return {"rows_changed": changed, "backlog_bytes": backlog_bytes, "backlog_files": backlog_files,
            "files_seen": len(files), "files_read": files_read, "diagnostics": diagnostics,
            "seconds": round(time.monotonic() - started, 3)}


def _mtime(path: Path) -> float:
    try:
        return path.lstat().st_mtime
    except FileNotFoundError:
        return 0.0


def backlog(node_db, registry, *, since: str | None = SINCE) -> dict[str, int]:
    registry = load_registry(registry)
    floor = _since_epoch(since)
    conn = store.connect(node_db, node=registry_node(registry))
    try:
        total = files = 0
        shared: dict[str, Any] = {"cron_jobs_path": cron_jobs_path()}
        for _runtime, path, _source in enumerate_sources(registry):
            if floor is not None and _runtime != "host" and _mtime(path) < floor:
                continue
            pending = _pending_bytes(conn, path, current_version(_runtime, shared))
            if pending:
                total += pending
                files += 1
        return {"backlog_bytes": total, "backlog_files": files}
    finally:
        conn.close()
