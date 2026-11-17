"""MTD CC evidence collection and six-metric billing projection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Dict, List, Mapping, Optional, Tuple

from .billing_core import (
    AttributionEvidence,
    BillingConfigError,
    allocate_subscription_costs,
    load_subscription_components,
)
from .live_contract import DISPLAY_TZ, _metric


CAPABILITY = "s1.request_trace_capture"
WINDOW = "month_to_date_cst"
IDS = (
    "billing.mtd.period", "billing.mtd.components", "billing.mtd.by_agent",
    "billing.mtd.attributed_cents", "billing.mtd.unattributed_cents",
    "billing.mtd.total_cents",
)
UNITS = ("list", "list", "list", "cents", "cents", "cents")
PARTITION_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})\.jsonl$")


def _time(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _blank(now: datetime, outcome: str, reason: str) -> AttributionEvidence:
    local = now.astimezone(DISPLAY_TZ)
    return AttributionEvidence(
        local.strftime("%Y-%m"), 0, local.day, False, 0, (), 0, outcome, reason,
    )


def _candidates(obs_dir: Path, now: datetime) -> Tuple[Optional[List[Path]], str]:
    try:
        if not obs_dir.is_dir():
            return None, "MTD obs directory is unavailable"
        paths = list(obs_dir.glob("*.jsonl"))
    except OSError as error:
        return None, f"MTD obs directory is unreadable: {error}"
    local = now.astimezone(DISPLAY_TZ)
    start = datetime(local.year, local.month, 1, tzinfo=DISPLAY_TZ)
    first, last = start.astimezone(timezone.utc).date(), now.date()
    dates = set()
    while first <= last:
        dates.add(first.isoformat())
        first += timedelta(days=1)
    selected = [
        path for path in paths
        if (
            (match := PARTITION_DATE.search(path.name)) is not None
            and match.group(1) in dates
        )
    ]
    return sorted(selected), ""


def collect_mtd_evidence(obs_dir: Path, now: datetime) -> AttributionEvidence:
    """Collect only same-event, explicitly gated Claude Code billing evidence."""
    if now.tzinfo is None or now.utcoffset() is None:
        return _blank(
            now.replace(tzinfo=timezone.utc), "unavailable",
            "collection time must be timezone-aware",
        )
    now = now.astimezone(timezone.utc)
    local = now.astimezone(DISPLAY_TZ)
    candidates, error = _candidates(obs_dir, now)
    if candidates is None:
        return _blank(now, "unavailable", error)
    agents: Dict[str, int] = {}
    missing_tokens = valid_turns = 0
    provenance_gap = False
    for path in candidates:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_no, raw in enumerate(handle, 1):
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw)
                    except (json.JSONDecodeError, ValueError) as parse_error:
                        return _blank(
                            now, "unavailable",
                            f"{path.name}:{line_no} has invalid JSON: {parse_error}",
                        )
                    if not isinstance(event, Mapping):
                        return _blank(
                            now, "unavailable",
                            f"{path.name}:{line_no} is not an event object",
                        )
                    if event.get("evt") != "turn.end":
                        continue
                    occurred = _time(event.get("ts"))
                    if occurred is None:
                        return _blank(
                            now, "unavailable",
                            f"{path.name}:{line_no} turn.end has invalid ts",
                        )
                    if (
                        occurred > now
                        or occurred.astimezone(DISPLAY_TZ).strftime("%Y-%m")
                        != local.strftime("%Y-%m")
                    ):
                        continue
                    if "billing_component_id" not in event or event["billing_component_id"] is None:
                        provenance_gap = True
                        continue
                    component_id = event["billing_component_id"]
                    if not isinstance(component_id, str) or not component_id:
                        return _blank(
                            now, "unavailable",
                            f"{path.name}:{line_no} has invalid billing_component_id",
                        )
                    if component_id != "claude_code":
                        continue
                    tokens = event.get("outputTokens")
                    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
                        return _blank(
                            now, "unavailable",
                            f"{path.name}:{line_no} CC turn.end has invalid outputTokens",
                        )
                    valid_turns += 1
                    agent = event.get("agent")
                    if (
                        isinstance(agent, str) and agent
                        and agent == agent.strip()
                    ):
                        agents[agent] = agents.get(agent, 0) + tokens
                    else:
                        missing_tokens += tokens
        except (OSError, UnicodeError) as read_error:
            return _blank(
                now, "unavailable",
                f"MTD obs partition {path.name} is unreadable: {read_error}",
            )
    if provenance_gap:
        return _blank(
            now, "insufficient",
            "MTD turn.end lacks billing_component_id; runtime/provider/file "
            "location cannot prove Claude Code subscription provenance",
        )
    total = sum(agents.values()) + missing_tokens
    if total <= 0:
        return AttributionEvidence(
            local.strftime("%Y-%m"), 0, local.day, False, valid_turns,
            tuple(sorted(agents.items())), missing_tokens, "insufficient",
            "No positive proven CC output and no partition-completeness marker",
        )
    return AttributionEvidence(
        local.strftime("%Y-%m"), 0, local.day, False, valid_turns,
        tuple(sorted(agents.items())), missing_tokens, "usable",
        "Provisional proven CC evidence; obs has no day-completeness marker",
    )


def _config_unavailable(reason: str) -> List[Mapping[str, object]]:
    return [
        _metric(
            metric_id, CAPABILITY, value=None, unit=unit, observed_at=None,
            window=WINDOW, quality="unavailable", reason=reason,
            scope="owner-reviewed fixed subscription billing",
        )
        for metric_id, unit in zip(IDS, UNITS)
    ]


def build_billing_metrics(
    config_path: Path, obs_dir: Path, now: datetime,
) -> List[Mapping[str, object]]:
    """Publish config facts independently from the four attribution metrics."""
    try:
        components = load_subscription_components(config_path, now)
    except BillingConfigError as error:
        return _config_unavailable(str(error))
    result = allocate_subscription_costs(
        components, collect_mtd_evidence(obs_dir, now),
    )
    component_rows = [
        {
            "component_id": item.component_id,
            "amount_cents": item.amount_cents,
            "currency": item.currency,
            "attribution": item.attribution,
        }
        for item in components
    ]
    agent_rows = (
        [
            {
                "agent": row.agent, "output_tokens": row.output_tokens,
                "attributed_cents": row.attributed_cents,
            }
            for row in result.by_agent
        ]
        if result.by_agent is not None else None
    )
    values = (
        [result.period] if result.period is not None else None,
        component_rows,
        agent_rows,
        result.attributed_cents,
        result.unattributed_cents,
        sum(item.amount_cents for item in components),
    )
    scopes = (
        "Asia/Shanghai MTD fixed-subscription attribution period",
        "active owner-reviewed fixed subscription components",
        "CC fixed subscription allocated by proven MTD output share",
        "exact integer cents attributed to named agents",
        "exact integer cents not attributable to named agents",
        "sum of active owner-reviewed fixed subscription components",
    )
    metrics = []
    for index, (metric_id, unit, value, scope) in enumerate(
        zip(IDS, UNITS, values, scopes)
    ):
        fact = index in (1, 5)
        metrics.append(_metric(
            metric_id, CAPABILITY, value=value, unit=unit,
            observed_at=now if value is not None else None, window=WINDOW,
            quality="validated" if fact else result.quality,
            sample_count=None if fact else result.sample_count,
            reason=None if fact else result.reason, scope=scope,
        ))
    return metrics
