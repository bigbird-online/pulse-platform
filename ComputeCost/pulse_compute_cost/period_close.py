"""Closed weeks and adjustment entries (WI754 step 5, design 3.6).

A week runs Monday 00:00 to Monday 00:00 (Asia/Shanghai). Closing a week
archives its view once and books the then-current state of every usage row
of closed weeks (`booked`). A row is a usage entity in a week, or one metering
day of it: a view counts a metered entity day by day, so its entries are judged
day by day too. Each round, a closed-week row whose current state in
`usage_resolved` differs from its booked state yields adjustment entries in
the open week; the day an entry first appears is its booking day. Closing the
next week finalizes them (in its archive and in `closed_adjustment`, so a
window longer than a week keeps the entries booked in it) and books the
current state again.

State lives in `<derived>/period-close.sqlite`, beside the resolved store but
not in it: the store is emptied on every code or registry change, the booked
state must survive that. Archives live in `closed-weeks/` beside the
publications and are written once.

Invariants: for each closed-week row, booked plus open entries equals the
current state; a price change alone books no entry; a reattribution's two
entries cancel in amount; rows without a week (a cross-week interval, a counter
without start) are never booked.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .receipts import TOKEN_FIELDS, canonical, decimal_text
from .resolved_store import DIMENSIONS

ZONE = ZoneInfo("Asia/Shanghai")
FORCE_AT = time(6)  # By Monday 06:00 the previous week closes with whatever arrived.
ARCHIVE_SCHEMA = "pulse_stats_closed_week/v1"
_TABLES = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS closed_week(week_start TEXT PRIMARY KEY, closed_at TEXT NOT NULL,
  sequence INTEGER NOT NULL, head_sha256 TEXT NOT NULL, catalog_sha256 TEXT, engine TEXT NOT NULL,
  gaps TEXT NOT NULL, archive_sha256 TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS booked(entity_ref BLOB NOT NULL, week_start TEXT NOT NULL, part TEXT NOT NULL,
  included INTEGER NOT NULL,
  tenant TEXT NOT NULL, agent TEXT NOT NULL, wi TEXT NOT NULL, entrypoint TEXT NOT NULL, node TEXT NOT NULL,
  runtime TEXT NOT NULL, model TEXT NOT NULL,
  uncached_input INTEGER, cache_read INTEGER, cache_write INTEGER, output INTEGER, reported_total INTEGER,
  amount TEXT, unpriced_reason TEXT, origin_day TEXT NOT NULL,
  PRIMARY KEY(entity_ref, week_start, part)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS open_adjustment(entity_ref BLOB NOT NULL, week_start TEXT NOT NULL, part TEXT NOT NULL,
  booked_on TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(entity_ref, week_start, part)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS closed_adjustment(id INTEGER PRIMARY KEY, booked_on TEXT NOT NULL, entry TEXT NOT NULL);
"""
_ROW = ("entity_ref", "week_start", "part")
_STATE = ("included", *DIMENSIONS, *TOKEN_FIELDS)
_CURRENT = (*_ROW, *_STATE, "reported_total", "origin")


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def local_day(moment: datetime) -> date:
    return moment.astimezone(ZONE).date()


def _current_sql(alias: str) -> str:
    """The rows of the closed weeks as `usage_resolved` keeps them: a metered entity once per day."""
    return (f"SELECT {', '.join(_CURRENT[:-1])}, CASE WHEN timing = 'metered' THEN part ELSE t_lo END AS origin "
            f"FROM {alias}.usage_resolved WHERE week_start != '' AND week_start < :closed")


def _origin_day(origin: str) -> str:
    """The local day a row's usage started: a metered part's day, else its start instant's."""
    if len(origin) == 10:
        return origin
    return local_day(datetime.fromisoformat(origin.replace("Z", "+00:00"))).isoformat()


class Pricer:
    """Today's price of a row's Token classes, cached by model and quantities."""
    def __init__(self, catalog: Mapping[str, Any] | None):
        from .collaboration_query import _value_cost
        self._value, self.catalog, self._cache, self._models = _value_cost, catalog, {}, {}

    def __call__(self, model: str, tokens: tuple) -> tuple[str | None, str | None]:
        key = (model, tokens)
        if key not in self._cache:
            row = SimpleNamespace(payload={"model": model, "tokens": dict(zip(TOKEN_FIELDS, tokens))})
            amount, reason = self._value(row, self.catalog, self._models)
            self._cache[key] = (decimal_text(amount) if amount is not None else None, reason)
        return self._cache[key]


class PeriodClose:
    def __init__(self, db: sqlite3.Connection, path: Path):
        self._db, self.path = db, path

    @classmethod
    def open(cls, path: str | Path, resolved: str | Path) -> "PeriodClose":
        """The private close state, with the resolved store attached read-only as `r`."""
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))
        if path.stat().st_mode & 0o077:
            raise ValueError("period close state must be private")
        db = sqlite3.connect(path.resolve().as_uri(), uri=True, isolation_level=None, timeout=3600)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript(_TABLES)
        db.execute("ATTACH DATABASE ? AS r", (Path(resolved).resolve().as_uri() + "?mode=ro",))
        return cls(db, path)

    def close_connection(self) -> None:
        self._db.close()

    def closed_through(self) -> date | None:
        """The Monday before which every week is closed; None before enabling."""
        row = self._db.execute("SELECT value FROM meta WHERE key = 'closed_through'").fetchone()
        return date.fromisoformat(row[0]) if row else None

    def store_sequence(self) -> int:
        return int(self._db.execute("SELECT value FROM r.meta WHERE key = 'sequence'").fetchone()[0])

    # -- when -----------------------------------------------------------------
    def due(self, now: datetime, peers: Iterable[Mapping[str, Any]], local_pending: bool = False):
        """(week_start, gaps) to close in this round, or None.

        Complete: every unpaused peer was read from a collection that started
        after Monday 00:00 (`collected_from`), and the publisher judged all its
        observations. Otherwise only from Monday 06:00, listing the gaps.
        """
        this = monday(local_day(now))
        through = self.closed_through()
        if through is None or through >= this:
            return None
        start = datetime.combine(this, time.min, ZONE)
        gaps = []
        for peer in peers:
            if peer.get("status") == "paused":
                continue
            collected = peer.get("collected_from")
            if peer.get("status") != "read":
                gaps.append({"peer": peer["id"], "reason": peer.get("reason") or "peer_" + str(peer.get("status"))})
            elif collected is None or datetime.fromisoformat(collected.replace("Z", "+00:00")) < start:
                gaps.append({"peer": peer["id"], "reason": "collected_before_week_end"})
        if local_pending:
            gaps.append({"peer": "publisher", "reason": "observation_backlog"})
        gaps.sort(key=lambda g: g["peer"])
        if gaps and now < datetime.combine(this, FORCE_AT, ZONE):
            return None
        return (this - timedelta(days=7)).isoformat(), gaps

    # -- state ------------------------------------------------------------------
    def _rebook(self, closed: date, pricer: Pricer) -> None:
        """Booked := the current state of every row before `closed`.

        A row keeps its amount only if that amount is in the books: it was
        booked as included and neither model nor quantities changed. Any other
        row takes today's price, the one its entry of this round carries.
        """
        db = self._db
        db.execute("DROP TABLE IF EXISTS temp.booked_next")
        db.execute("CREATE TEMP TABLE booked_next AS SELECT * FROM booked WHERE 0")
        rows = db.execute(f"SELECT c.*, b.included, b.model, {', '.join('b.' + k for k in TOKEN_FIELDS)}, b.amount, "
                          f"b.unpriced_reason FROM ({_current_sql('r')}) c LEFT JOIN booked b "
                          f"USING({', '.join(_ROW)})", {"closed": closed.isoformat()})
        width = len(_CURRENT)
        while batch := rows.fetchmany(5000):
            out = []
            for row in batch:
                cur = dict(zip(_CURRENT, row))
                tokens = tuple(cur[k] for k in TOKEN_FIELDS)
                was_included, old_model, *old_tokens, amount, reason = row[width:]
                if not (was_included and old_model == cur["model"] and tuple(old_tokens) == tokens):
                    amount, reason = pricer(cur["model"], tokens)
                out.append((*row[:width - 1], amount, reason, _origin_day(cur["origin"])))
            db.executemany("INSERT INTO temp.booked_next VALUES (" + ",".join("?" * (width + 2)) + ")", out)
        db.execute("DELETE FROM booked")
        db.execute("INSERT INTO booked SELECT * FROM temp.booked_next")
        db.execute("DROP TABLE temp.booked_next")
        db.execute("DELETE FROM open_adjustment")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('closed_through', ?)", (closed.isoformat(),))

    def enable(self, now: datetime, pricer: Pricer, binding: Mapping[str, Any]) -> bool:
        """At the first round: every week before the current one is closed, without archive or entries."""
        if self.closed_through() is not None:
            return False
        this = monday(local_day(now))
        self._db.execute("BEGIN IMMEDIATE")
        try:
            weeks = [w for (w,) in self._db.execute(
                "SELECT DISTINCT week_start FROM r.usage_resolved WHERE week_start != '' AND week_start < ?",
                (this.isoformat(),))]
            last = (this - timedelta(days=7)).isoformat()
            self._record(sorted({*weeks, last}), now, binding, [], None, engine="enable")
            self._rebook(this, pricer)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return True

    def _record(self, weeks, now, binding, gaps, archive_sha256, *, engine=None):
        self._db.executemany("INSERT INTO closed_week VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
            (week, now.isoformat(), binding["sequence"], binding["head_sha256"], binding.get("catalog_sha256"),
             engine or binding["engine"], json.dumps(gaps, sort_keys=True),
             archive_sha256 if week == weeks[-1] else None) for week in weeks])

    def close(self, week_start: str, view: Mapping[str, Any], gaps: list, now: datetime, binding: Mapping[str, Any],
              archive_dir: Path, pricer: Pricer, *, entries: Iterable[Mapping[str, Any]] = (),
              work_items: list | None = None) -> Path:
        """Archive the week's view, book every closed week's current state and finalize the entries.

        `entries` are the open entries this close finalizes; `work_items` is the
        display data of the WIs the view names, which a reader checks the view's
        WI families against. The archive is written under a temporary name first
        and renamed after the commit; a run stopped in between repeats the close
        with the same result.
        """
        through = self.closed_through()
        week = date.fromisoformat(week_start)
        if through is None or week < through or week.weekday():
            raise ValueError("only the week after the closed ones can close")
        data = canonical({"schema": ARCHIVE_SCHEMA, "view": view,
                          **({} if work_items is None else {"work_items": work_items})})
        archive_dir = Path(archive_dir)
        archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        final = archive_dir / f"cost-week-{week_start}.json"
        temporary = final.with_name(final.name + ".tmp")
        with open(temporary, "wb", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Weeks no round closed (the publisher was down) close with it, without their own archive.
        weeks = [(through + timedelta(days=7 * n)).isoformat() for n in range((week - through).days // 7 + 1)]
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._record(weeks, now, binding, gaps, hashlib.sha256(data).hexdigest())
            self._db.executemany("INSERT INTO closed_adjustment(booked_on, entry) VALUES (?, ?)", [
                (e["booked_on"], json.dumps({**e, "tokens": list(e["tokens"]),
                                             "amount": None if e["amount"] is None else str(e["amount"])},
                                            sort_keys=True)) for e in entries])
            # No view reaches further back than the month of the week just closed.
            self._db.execute("DELETE FROM closed_adjustment WHERE booked_on < ?",
                             ((week.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat(),))
            self._rebook(week + timedelta(days=7), pricer)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        os.replace(temporary, final)
        return final

    def finalized(self, since: str) -> list[dict[str, Any]]:
        """The entries earlier closes finalized, booked on or after `since`."""
        result = []
        for (text,) in self._db.execute("SELECT entry FROM closed_adjustment WHERE booked_on >= ? ORDER BY id", (since,)):
            entry = json.loads(text)
            result.append({**entry, "tokens": tuple(entry["tokens"]),
                           "amount": None if entry["amount"] is None else Decimal(entry["amount"])})
        return result

    def archive(self, week_start: str, archive_dir: Path) -> dict[str, Any] | None:
        """A closed week's archive (`view`, and `work_items` when it had them) whose digest matches its record.

        Finishes an interrupted rename.
        """
        row = self._db.execute("SELECT closed_at, sequence, gaps, archive_sha256 FROM closed_week WHERE week_start = ?",
                               (week_start,)).fetchone()
        if row is None or row[3] is None:
            return None
        final = Path(archive_dir) / f"cost-week-{week_start}.json"
        temporary = final.with_name(final.name + ".tmp")
        for path in (final, temporary):
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                continue
            if hashlib.sha256(data).hexdigest() == row[3]:
                if path is temporary:
                    os.replace(temporary, final)
                return json.loads(data)
        raise ValueError("closed week archive is missing or changed")

    def closed_week(self, week_start: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT closed_at, sequence, gaps FROM closed_week WHERE week_start = ?",
                               (week_start,)).fetchone()
        return None if row is None else {"week_start": week_start, "closed_at": row[0], "watermark": row[1],
                                         "gaps": json.loads(row[2])}

    # -- entries ------------------------------------------------------------------
    def _differences(self):
        """(current, booked) pairs of closed-week rows whose state differs; either side may be None."""
        closed = self.closed_through()
        if closed is None:
            return
        cols = _STATE
        same = " AND ".join(f"c.{k} IS b.{k}" for k in cols)
        current = _current_sql("r")
        width = len(_ROW) + len(cols) + 1
        booked_cols = ", ".join(f"b.{k}" for k in (*cols, "amount", "origin_day"))
        query = (f"SELECT {', '.join('c.' + k for k in (*_ROW, *cols))}, c.origin, "
                 f"{booked_cols} FROM ({current}) c LEFT JOIN booked b USING({', '.join(_ROW)}) "
                 f"WHERE b.entity_ref IS NULL OR NOT ({same}) "
                 f"UNION ALL SELECT {', '.join('b.' + k for k in _ROW)}, "
                 f"{', '.join('NULL' for _ in range(len(cols) + 1))}, "
                 f"{booked_cols} FROM booked b WHERE NOT EXISTS (SELECT 1 FROM r.usage_resolved u WHERE "
                 + " AND ".join(f"u.{k} = b.{k}" for k in _ROW) + ")")
        for row in self._db.execute(query, {"closed": closed.isoformat()}):
            state = row[len(_ROW):width - 1]
            cur = None if state[0] is None else dict(zip(cols, state), origin_day=_origin_day(row[width - 1]))
            old = row[width:]
            booked = None if old[0] is None else dict(zip((*cols, "amount", "origin_day"), old))
            yield row[:len(_ROW)], cur, booked

    @staticmethod
    def _entries(cur, booked, pricer: Pricer) -> tuple[str | None, list[tuple]]:
        """(reason, [(sign, key, tokens, amount)]) of one row's difference."""
        def side(row):
            return tuple(row[d] for d in DIMENSIONS), tuple(row[k] for k in TOKEN_FIELDS)

        was, now = bool(booked and booked["included"]), bool(cur and cur["included"])
        if was and not now:
            return "withdrawn", [(-1, *side(booked), booked["amount"])]
        if now and not was:
            return "late_usage", [(1, *side(cur), pricer(cur["model"], side(cur)[1])[0])]
        if not now:
            return None, []
        (old_key, old_tokens), (new_key, new_tokens) = side(booked), side(cur)
        if old_tokens == new_tokens and booked["model"] == cur["model"]:
            if old_key == new_key:
                return None, []
            return "reattributed", [(-1, old_key, old_tokens, booked["amount"]), (1, new_key, new_tokens, booked["amount"])]
        return "quantity_revised", [(-1, old_key, old_tokens, booked["amount"]),
                                    (1, new_key, new_tokens, pricer(cur["model"], new_tokens)[0])]

    def refresh_adjustments(self, today: date, pricer: Pricer) -> list[dict[str, Any]]:
        """This round's entries; each keeps the day it first appeared, a vanished difference drops out."""
        found = {}
        for row, cur, booked in self._differences():
            reason, parts = self._entries(cur, booked, pricer)
            if reason:
                found[row] = (reason, (cur or booked)["origin_day"], parts)
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            known = {(e, w, p): d for e, w, p, d, _ in db.execute("SELECT * FROM open_adjustment")}
            db.executemany("DELETE FROM open_adjustment WHERE entity_ref = ? AND week_start = ? AND part = ?",
                           [k for k in known if k not in found])
            db.executemany("INSERT OR REPLACE INTO open_adjustment VALUES (?, ?, ?, ?, ?)", [
                (*row, known.get(row, today.isoformat()), found[row][0]) for row in found])
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        result = []
        for row, (reason, origin, parts) in sorted(found.items()):
            week, booked_on = row[1], known.get(row, today.isoformat())
            for sign, key, tokens, amount in parts:
                result.append({"week_start": week, "reason": reason, "booked_on": booked_on, "origin_day": origin,
                               "key": dict(zip(DIMENSIONS, key)), "sign": sign, "tokens": tokens,
                               "amount": None if amount is None else sign * Decimal(amount)})
        return result


def adjustments_for(entries: Iterable[Mapping[str, Any]], from_date: str, to_date: str) -> list[Mapping[str, Any]]:
    """Entries booked inside [from, to) for usage outside it; usage inside is the window's own.

    An entry is one row's difference, so each metering day of an entity is judged on its own day.
    """
    return [e for e in entries if from_date <= e["booked_on"] < to_date
            and not from_date <= e["origin_day"] < to_date]


def _block(entries: list[Mapping[str, Any]]) -> dict[str, Any]:
    def amounts(rows):
        tokens = {k: sum(e["sign"] * (e["tokens"][i] or 0) for e in rows) for i, k in enumerate(TOKEN_FIELDS)}
        return {"entry_count": len(rows), "unpriced_entry_count": sum(e["amount"] is None for e in rows),
                "tokens": tokens, "known_total_tokens": sum(tokens.values()),
                "priced_machine_cash_usd": decimal_text(sum((e["amount"] for e in rows if e["amount"] is not None),
                                                            Decimal(0)))}
    origins: dict[tuple, list] = {}
    for entry in entries:
        origins.setdefault((entry["week_start"], entry["reason"]), []).append(entry)
    return {**amounts(entries), "by_origin": [{**amounts(rows), "week_start": w, "reason": r}
                                              for (w, r), rows in sorted(origins.items())]}


def _total(owner: Mapping[str, Any], block: Mapping[str, Any]) -> dict[str, Any]:
    usage, cost = owner.get("usage") or {}, owner["cost"]
    own = usage.get("tokens") or {}
    tokens = {k: None if (own.get(k) or {}).get("known") is None else own[k]["known"] + block["tokens"][k]
              for k in TOKEN_FIELDS}
    known = usage.get("known_total_tokens")
    if cost.get("machine_cash_usd") is not None:
        amount = Decimal(cost["machine_cash_usd"])
    elif "priced_machine_cash_usd" in cost:
        amount = Decimal(cost["priced_machine_cash_usd"] or 0)
    else:
        amount = Decimal(cost.get("partial_machine_cash_usd") or 0)
    return {"tokens": tokens, "known_total_tokens": None if known is None else known + block["known_total_tokens"],
            "priced_machine_cash_usd": decimal_text(amount + Decimal(block["priced_machine_cash_usd"]))}


def provisional_last_week(view: Mapping[str, Any], closing: Mapping[str, Any]) -> dict[str, Any]:
    """The previous week's view before its close: not closed, with the entries its close would book."""
    if "entries" in closing and closing["watermark"] == view["watermark"]:
        view = with_adjustments(view, closing["entries"])
    return {**view, "closed_week": None}


def with_adjustments(view: Mapping[str, Any], entries: Iterable[Mapping[str, Any]],
                     tenants: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The view with the entries booked in its window: per row and in total, plus rows only they name.

    `tenants` maps a tenant id to its published identity; an entry whose named
    tenant has none in the publication is shown under `unassigned`.
    """
    dims = list(view["query"]["group_by"])
    window = adjustments_for(entries, view["query"]["from_date"], view["query"]["to_date"])
    tenants = dict(tenants or {})
    for group in view["groups"]:
        if "tenant" in group:
            tenants.setdefault(group["key"]["tenant"], group["tenant"])
    by_key: dict[tuple, list] = {}
    for entry in window:
        key = dict(entry["key"])
        if "tenant" in dims and key["tenant"] not in tenants and key["tenant"] not in {"unassigned", "ambiguous"}:
            key["tenant"] = "unassigned"
        by_key.setdefault(tuple(key[d] for d in dims), []).append(entry)
    result = dict(view)
    groups = []
    for group in view["groups"]:
        found = by_key.pop(tuple(group["key"][d] for d in dims), None)
        if found:
            block = _block(found)
            group = {**group, "adjustments": block, "total": _total(group, block)}
        groups.append(group)
    only = []
    zero = {k: {"known": 0, "unknown_event_count": 0} for k in TOKEN_FIELDS}
    for key, found in sorted(by_key.items()):
        row = {"key": dict(zip(dims, key)), "usage": {"tokens": zero, "event_count": 0, "known_total_tokens": 0},
               "cost": {"machine_cash_usd": "0", "partial_machine_cash_usd": None,
                        "cash_basis": "openrouter_standard_token_cash", "reasons": [], "unpriced": []}}
        if "tenant" in dims and row["key"]["tenant"] in tenants:
            row["tenant"] = tenants[row["key"]["tenant"]]
        block = _block(found)
        only.append({**row, "adjustments": block, "total": _total(row, block)})
    block = _block(window)
    result.update(groups=groups, adjustment_only_groups=only, adjustments=block,
                  total=_total(view["summary"], block))
    return result


def belonging_to_open_week(entries: Iterable[Mapping[str, Any]], through: date, today: date) -> list[dict[str, Any]]:
    """Entries with the day they count on: until the previous week closes, what a round finds belongs to it.

    `through` is the Monday before which every week is closed. The close books
    them on the last day of the week it closes; the views before it do the same.
    """
    last = (monday(today) - timedelta(days=1)).isoformat()
    if through >= monday(today):
        return [dict(e) for e in entries]
    return [{**e, "booked_on": min(e["booked_on"], last)} for e in entries]


def archived_last_week(resolved: Path, archive_dir: Path, now: datetime) -> dict[str, Any] | None:
    """The previous week's archive when it is closed with one, read without any closing work.

    For rounds whose closing work did not run or stopped early: a finalized
    week is published from its archive, never recomputed. None when the week
    has no archive (not closed yet, or closed by enabling); a missing or
    changed archive of a closed week raises.
    """
    state = Path(resolved).parent / "period-close.sqlite"
    if not state.is_file() or not Path(resolved).is_file():
        return None
    close = PeriodClose.open(state, resolved)
    try:
        archive = close.archive((monday(local_day(now)) - timedelta(days=7)).isoformat(), archive_dir)
    finally:
        close.close_connection()
    return None if archive is None else {"last_week_view": archive["view"],
                                         "last_week_work_items": archive.get("work_items")}


def closed_week_publication(template: Mapping[str, Any], closing: Mapping[str, Any]) -> dict[str, Any]:
    """The closed week's publication: its archived view and WI display data in this round's envelope."""
    from .receipts import digest
    result = {**template, "status": "available", "reason": None, "views": [closing["last_week_view"]]}
    if closing.get("last_week_work_items") is not None:
        result["work_items"] = closing["last_week_work_items"]
    result["sha256"] = digest({k: v for k, v in result.items() if k != "sha256"})
    return result


def _seal(document: Mapping[str, Any], views: list) -> dict[str, Any]:
    from .receipts import digest
    result = {k: v for k, v in document.items() if k != "sha256"}
    result["views"] = views
    result["sha256"] = digest(result)
    return result


def publisher_round(resolved: Path, document: Mapping[str, Any], *, now: datetime, catalog: Mapping[str, Any] | None,
                    peers: list, local_pending: bool, archive_dir: Path, last_week, timings: dict) -> dict[str, Any]:
    """One publication's closing work on the publisher.

    Enables at the first round, closes the previous week when due (its view
    from `last_week()`, a fresh LAST_WEEK document), adds this round's entries
    and those earlier closes finalized to the current activity views, and
    returns the previous week's archived view (with its `last_week_work_items`)
    when it is closed, else `entries` (with their `watermark`) for the view
    of the previous week computed from the current state. Any failure leaves
    the publication as it was.
    """
    from .calculation_cache import engine_digest
    from .receipts import digest
    result = {"document": document, "last_week_view": None}
    if document.get("status") != "available" or not Path(resolved).is_file():
        timings["period_close"] = "not_available"
        return result
    close = PeriodClose.open(Path(resolved).parent / "period-close.sqlite", resolved)
    try:
        pricer, today = Pricer(catalog), local_day(now)

        def binding(watermark):
            head = close._db.execute("SELECT value FROM r.meta WHERE key = 'head_sha256'").fetchone()[0]
            return {"sequence": watermark, "head_sha256": head, "engine": engine_digest(),
                    "catalog_sha256": digest(catalog) if catalog else None}

        watermark = document["views"][0]["watermark"]
        if close.store_sequence() != watermark:
            timings["period_close"] = "store_watermark_differs"
            return result
        record = {"enabled": close.enable(now, pricer, binding(watermark))}
        due = close.due(now, peers, local_pending)
        if due is not None:
            week, gaps = due
            closed = last_week()
            view = closed["views"][0] if closed.get("status") == "available" else None
            if view is None or close.store_sequence() != view["watermark"] or view["query"]["from_date"] != week:
                record["closed"] = "last_week_unavailable"
            else:
                # Entries found by this round, before closing, belong to the week being closed.
                entries = belonging_to_open_week(close.refresh_adjustments(today, pricer), close.closed_through(), today)
                view = with_adjustments(view, entries)
                view["closed_week"] = {"week_start": week, "closed_at": now.isoformat(), "watermark": view["watermark"],
                                       "gaps": gaps}
                close.close(week, view, gaps, now, binding(view["watermark"]), archive_dir, pricer,
                            entries=entries, work_items=closed.get("work_items"))
                record["closed"] = week
        if close.store_sequence() == watermark:
            entries = belonging_to_open_week(close.refresh_adjustments(today, pricer), close.closed_through(), today)
            record["open_entries"] = len(entries)
            # A window longer than a week also holds the entries booked in it that a close has finalized.
            entries = close.finalized(min(v["query"]["from_date"] for v in document["views"])) + entries
            result.update(entries=entries, watermark=watermark)
            views = [with_adjustments(v, entries) if v["dimension"] == "activity" else v for v in document["views"]]
            result["document"] = _seal(document, views)
        else:
            record["current"] = "store_watermark_moved"
        previous = (monday(today) - timedelta(days=7)).isoformat()
        archive = close.archive(previous, archive_dir) or {}
        result["last_week_view"] = archive.get("view")
        result["last_week_work_items"] = archive.get("work_items")
        result["last_week_closed"] = close.closed_week(previous) is not None
        timings["period_close"] = record
        return result
    finally:
        close.close_connection()
