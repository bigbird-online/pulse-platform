"""Read the Oikos producer's measured platform health, never re-evaluate alarms."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

SCHEMA = "oikos_platform_health/v1"
NODES = {"C1-ownermac", "tcloud-node2", "C3-ecs", "bigbird-sg"}
# Added resident nodes the producer may already report; the four above stay required.
OPTIONAL_NODES = {"tcloud-node3"}
MAX_BYTES = 65536
MAX_AGE_SECONDS = 1500
METRIC_ID = "oikos.platform.nodes"
CAPABILITY_ID = "oikos.platform_health"
# Hestia publishes this on the same ownermac account that runs the Stats reader.
REPORT_PATH = Path.home() / ".local/state/oikos/hestia/platform-health-v1.json"


def fetch(path: Path = REPORT_PATH) -> str | None:
    """One fixed, bounded local read; no token generation, log parsing or probing."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        return raw.decode("utf-8") if len(raw) <= MAX_BYTES else None
    except (OSError, UnicodeError):
        return None


def date(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("unqualified timestamp")
    return stamp.astimezone(timezone.utc)


def metrics(raw: str | None, now: datetime) -> tuple[list[dict], dict]:
    observed = None
    reason = "Oikos 资源快照暂不可读，不能据此宣布没有事故"
    state = "error"
    rows = None
    revision = None
    try:
        if raw is None or len(raw.encode()) > MAX_BYTES:
            raise ValueError("unavailable")
        data = json.loads(raw)
        if data.get("schema") != SCHEMA:
            raise ValueError("unsupported Oikos schema")
        observed = date(data["observed_at"])
        completed, expiry = date(data["completed_at"]), date(data["expires_at"])
        if not observed <= completed <= now or not 0 < (expiry-observed).total_seconds() <= MAX_AGE_SECONDS:
            raise ValueError("invalid clock or expiry")
        revision = data["source_revision"]
        if not re.fullmatch(r"[0-9a-f]{40}", revision) or not re.fullmatch(r"[0-9a-f]{64}", data["source_digest"]):
            raise ValueError("missing producer identity")
        items = data["nodes"]
        if not isinstance(items, list):
            raise ValueError("missing or duplicated compute node")
        ids = {row["id"] for row in items}
        if len(items) != len(ids) or not NODES <= ids <= NODES | OPTIONAL_NODES:
            raise ValueError("missing or duplicated compute node")
        for row in items:
            if row["observed_at"] != data["observed_at"] or row["status"] not in {"ok", "at_limit", "pending", "incident", "unknown"}:
                raise ValueError("mixed rounds or unknown node state")
            for key in ("disk_percent", "memory_available_percent", "swap_percent", "inode_percent"):
                value = row[key]
                if value is not None and (type(value) is not int or not 0 <= value <= 100):
                    raise ValueError("invalid resource reading")
                if row["status"] != "unknown" and value is None:
                    raise ValueError("missing measurement cannot be healthy")
            if row["writable"] not in {"ok", "failed", "unknown"}:
                raise ValueError("invalid write evidence")
            if row["status"] in {"ok", "at_limit"} and row["writable"] != "ok":
                raise ValueError("healthy requires measured writability")
            runs = row["confirmation_runs"]
            if runs is not None and (type(runs) is not int or runs < 0):
                raise ValueError("invalid confirmation count")
            if row["status"] == "incident" and (runs is None or runs < 3):
                raise ValueError("unconfirmed incident")
            if row["delivery"] not in {"not_needed", "accepted_or_notified", "not_delivered"}:
                raise ValueError("unknown delivery claim")
            for key in ("label", "role", "runner_state", "runner_enabled", "runner_expected", "reason"):
                if not isinstance(row[key], str) or len(row[key]) > 240:
                    raise ValueError("invalid display field")
        if now >= expiry:
            state = "stale"
            reason = "Oikos 资源采样已过期；旧读数不作为当前健康证据"
        else:
            state = "fresh"
            # Allowlisted flat rows keep private paths, credentials and raw records out of App caches.
            keys = ("id", "label", "role", "status", "disk_percent", "memory_available_percent",
                    "swap_percent", "inode_percent", "writable", "runner_state", "runner_enabled",
                    "runner_expected", "confirmation_runs", "delivery", "reason", "observed_at")
            rows = [{**{key: row[key] for key in keys}, "expires_at": data["expires_at"],
                     "source_revision": revision} for row in items]
            residents = sum(1 for row in items if row["role"] == "resident")
            reason = f"Oikos 实测：{residents} 个常驻节点与可信 Linux Runner 池；不代表所有业务链路均通过"
    except (ValueError, TypeError, KeyError, AttributeError):
        rows = None
        reason = "Oikos 资源快照缺失或不符合版本、时间、完整性契约"
    value = {"id": METRIC_ID, "capability_id": CAPABILITY_ID,
             "status": "available" if rows is not None else "unknown", "value": rows,
             "unit": "nodes", "window": "current", "sample_count": len(rows) if rows is not None else None,
             "quality": "validated" if rows is not None else "unavailable",
             "scope": "Oikos resident nodes and trusted Linux runner pool", "observed_at": observed.isoformat() if observed else None,
             "reason": reason}
    source = {"id": "oikos-platform-health", "freshness": state,
              "observed_at": observed.isoformat() if observed else None,
              "error_code": None if state == "fresh" else "stale" if state == "stale" else "unavailable_or_invalid"}
    return [value], source
