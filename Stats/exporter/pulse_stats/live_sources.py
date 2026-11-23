"""Read-only collectors for the fixed PULSE production sources."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
import re
import subprocess
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .live_contract import (
    ACK_ARGV,
    ALERT_MESSAGE_CHARS,
    ALERT_TEMPLATES,
    ACK_TIMEOUT_SECONDS,
    ALERT_WINDOW_DAYS,
    DECISION_REQUEST_SHARD_MONTHS,
    AUTONOMOUS_EVENTS_PREFIX,
    BASELINE_VERSION,
    CC_QUOTA_MAX_AGE_SECONDS,
    CODEX_QUOTA_MAX_AGE_SECONDS,
    CODEX_REMOTE_TIMEOUT_SECONDS,
    DISPLAY_TZ,
    MAX_ALERT_GROUPS,
    HUMAN_DECISION_KINDS,
    LAUNCHCTL,
    RECOVERY_AGENT_LABELS,
    RECOVERY_AGENT_SUCCESS_STATUS,
    RECOVERY_HEARTBEAT_SOURCES,
    RECOVERY_UNIT_ARGV,
    RECOVERY_UNIT_NAMES,
    RECOVERY_UNIT_TIMEOUT_SECONDS,
    RELAY_CC_STATUS_URL,
    TIER_DONE_LABELS,
    RELAY_HEALTH_URL,
    RELAY_LAUNCHD_LABEL,
    SYSTEMD_ARGV,
    SYSTEMD_UNITS,
    WI_STALLED_DAYS,
    Collection,
    CommandRunner,
    LivePaths,
    _capability,
    _metric,
    _parse_date,
    _parse_systemd_timestamp,
    _short_hash,
    _source,
    codex_remote_argv,
)
from .reducer import (
    TIER_ACT,
    TIER_DONE,
    TIER_WATCH,
    IncidentEvent,
    IncidentProjection,
)
from . import billing, managed_cost_shadow, usage_sources, wi_facts, work_board
from .global_authority import GlobalAuthorityCarrierError, load_workflow_profiles
from .producer import authority_revision_for_git


_ALERT_SEVERITY = {"minor": "info", "major": "warning", "critical": "critical"}
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
# Work-item and handoff ids are the stable subject of nearly every escalation
# message, so they -- not the raw line -- decide what counts as "one thing".
_ALERT_SUBJECT_RE = re.compile(r"\b((?:wi|ho)_\d+_\d+)\b")
_SYSTEMD_SERVICE_RE = re.compile(r"^[A-Za-z0-9:_.@-]+\.service$")


def _month_shift(moment: datetime, months: int) -> datetime:
    """Same moment shifted by whole months, clamped to day 1.

    Only the year/month matter -- the result names a monthly shard file -- so
    the day is pinned to 1 rather than doing calendar-correct arithmetic that
    would trip on the 31st.
    """
    total = (moment.year * 12 + moment.month - 1) + months
    return moment.replace(year=total // 12, month=total % 12 + 1, day=1)


def _alert_subject(message: object, agent_id: object) -> str:
    """Identify the thing an escalation event is about.

    Preference order is the work-item/handoff id carried in the message, then
    the emitting agent, then a placeholder. Returning a stable string for every
    input keeps grouping total: no event can fail to land in some group.
    """
    if isinstance(message, str):
        found = _ALERT_SUBJECT_RE.search(message)
        if found:
            return found.group(1)
    if isinstance(agent_id, str) and agent_id.strip():
        return agent_id.strip()
    return "-"


def _heartbeat_journal_unit(registry_entry: Mapping[str, object], source_id: str) -> str:
    """Resolve a heartbeat source to its registered systemd service when possible."""

    service = registry_entry.get("service")
    if isinstance(service, str) and _SYSTEMD_SERVICE_RE.fullmatch(service):
        return service
    return source_id


def _heartbeat_action(registry_entry: Mapping[str, object], source_id: str) -> str:
    """Name the real node and scheduler; a receiver outage is not sender death."""
    if source_id == "ownermac-probe":
        return "在 ownermac 检查 launchctl print gui/$(id -u)/com.pulse.heartbeat-send；同时核对 TCloud 心跳接收端"
    if source_id == "hestia-p0-probe":
        return "在 ownermac 检查 Hestia p0-health-probe 的 cron 执行日志；同时核对 TCloud 心跳接收端"
    unit = _heartbeat_journal_unit(registry_entry, source_id)
    timer = registry_entry.get("timer")
    schedule = (f"systemctl --user status {timer}；"
                if isinstance(timer, str) and re.fullmatch(r"[A-Za-z0-9@_.-]+\.timer", timer) else "")
    return f"在探针所在 Linux 节点检查 {schedule}journalctl --user -u {unit} -n 50"


def _alert_message_excerpt(message: Optional[str]) -> str:
    """First paragraph of the source message, whitespace-collapsed and bounded."""
    if not message:
        return ""
    first = message.strip().split("\n", 1)[0]
    collapsed = " ".join(first.split())
    if len(collapsed) > ALERT_MESSAGE_CHARS:
        return collapsed[: ALERT_MESSAGE_CHARS - 1].rstrip() + "…"
    return collapsed


@dataclass
class _AlertGroup:
    """One real-world thing: every event sharing a (match_label, subject)."""

    match_label: str
    subject: str
    capability_id: str
    title: str
    severity: str
    first_seen: datetime
    last_seen: datetime
    message: Optional[str]
    occurrence_count: int
    requires_human: bool = False
    human_decision_kind: Optional[str] = None

    def observe(
        self,
        occurred: datetime,
        severity: str,
        message: object,
        requires_human: bool = False,
        human_decision_kind: Optional[str] = None,
    ) -> None:
        if occurred < self.first_seen:
            self.first_seen = occurred
        # The newest event supplies the message, so the reason reflects the
        # latest state of the thing rather than how it first looked.
        if occurred >= self.last_seen:
            self.last_seen = occurred
            if isinstance(message, str):
                self.message = message
            self.requires_human = requires_human
            self.human_decision_kind = human_decision_kind
        if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(self.severity, 0):
            self.severity = severity
        self.occurrence_count += 1

    def recovered_at(
        self,
        heartbeats: Optional[Mapping[str, datetime]],
        units: Optional[Mapping[str, Tuple[int, Optional[datetime]]]] = None,
        agent_successes: Optional[Mapping[str, datetime]] = None,
        subject_closures: Optional[Mapping[str, datetime]] = None,
    ) -> Optional[datetime]:
        """When this thing last proved it is working again, by any signal.

        Four independent routes, each tied to a specific kind of alert:

        * a heartbeat the probe posted itself, stamped after the last
          complaint;
        * a systemd exit record showing the unit last finished successfully
          after the last complaint -- an outside observation, available even
          for probes that post no heartbeat;
        * for an agent's own autonomous run failing, that agent's next
          successful run; or
        * for an alert *about a specific wi/handoff*, that ticket reaching a
          terminal state -- the ticket answering for itself.

        All are consulted and the *latest* one wins, so a stale signal can
        never veto a fresher one. Missing or unreadable evidence returns
        ``None``, i.e. "not proven healed" -- never the reverse.

        Note this no longer feeds tiering (``tier()`` returns ``watch`` for
        everything not in ``TIER_DONE_LABELS``). Its live consumer is
        ``_collect_alerts``, which counts healed groups for the "已自愈降档"
        line in the capability reason.
        """
        candidates: List[datetime] = []

        source = RECOVERY_HEARTBEAT_SOURCES.get(self.match_label) if heartbeats else None
        if source is not None:
            beat = heartbeats.get(source) if heartbeats else None
            if beat is not None:
                candidates.append(beat)

        unit = RECOVERY_UNIT_NAMES.get(self.match_label) if units else None
        if unit is not None:
            record = units.get(unit) if units else None
            # A nonzero exit is the unit confirming it is still broken, so it
            # contributes no recovery evidence at all.
            if record is not None and record[0] == 0 and record[1] is not None:
                candidates.append(record[1])

        if agent_successes and self.match_label in RECOVERY_AGENT_LABELS:
            # The subject must be the agent *itself*. Work-item and handoff ids
            # are deliberately excluded rather than resolved back to whoever
            # holds them: a stuck wi_20260712_057 is not fixed by its executor
            # succeeding at some other task, and treating it as fixed would
            # silence exactly the alerts that most need the owner. A ticket can
            # still clear its own alert, but only by closing — see below.
            subject = self.subject
            if not subject.startswith(("wi_", "ho_")):
                success = agent_successes.get(subject)
                if success is not None:
                    candidates.append(success)

        # These three routes are all *liveness* evidence, and liveness only
        # counts if it postdates the complaint: a heartbeat from before the
        # failure proves nothing about now. Filter per candidate rather than
        # on the maximum, so one stale signal cannot mask a fresh one.
        live = [item for item in candidates if item > self.last_seen]
        latest = max(live) if live else None

        # A ticket answers for itself: if this alert is about wi_X/ho_X and
        # that ticket has reached a terminal state, the thing being complained
        # about is done. Without this route a closed-out wi kept demanding
        # action forever — the other three routes are all agent- or unit-scoped
        # and none of them can speak for one specific ticket.
        # (2026-07-27: three wi were completed and four act alerts still said
        # "要你出手".)
        #
        # Deliberately *not* subject to the `> last_seen` test above. Closure is
        # a state, not a heartbeat: a rejected handoff is rejected no matter
        # what was logged afterwards. Applying the time comparison here is what
        # made the 07-27 fix a no-op on handoffs — the alerts these tickets
        # raise (wrap_missing, ledger findings) are emitted *during* closeout,
        # so the complaint always postdates the closure and the route could
        # never fire. Observed 07-28: ho_20260727_294 closed 07:48, its alert
        # last seen 08:43, still demanding action.
        if subject_closures:
            closed = subject_closures.get(self.subject)
            if closed is not None:
                return max(closed, latest) if latest is not None else closed

        return latest

    def tier(
        self,
        now: datetime,
        heartbeats: Optional[Mapping[str, datetime]] = None,
        units: Optional[Mapping[str, Tuple[int, Optional[datetime]]]] = None,
        agent_successes: Optional[Mapping[str, datetime]] = None,
        subject_closures: Optional[Mapping[str, datetime]] = None,
    ) -> str:
        """Which bucket this thing belongs in.

        ``act`` requires the structured receipt emitted by Harness's current
        WI/HO gate.  ``done`` remains label-derived; everything else is watch.
        **No escalation label or severity can reach ``act`` from here**,
        and none of the arguments affects the answer; they are kept because the
        signature is part of the call contract and because the evidence they
        carry is still consulted by the caller (``recovered_at`` at the
        ``_collect_alerts`` call site, for the "已自愈降档" count).

        The structured receipt is produced only after Harness has re-read the
        current WI/HO authority state.  Pending ``main_only`` handoffs remain a
        second, direct source emitted later in ``_collect_alerts``.

        Everything below is history -- **none of it still runs**. It is kept so
        the next reader knows these rules were removed on purpose rather than
        lost, and does not reinstate them.

        **Aging, removed 2026-07-28** (大鸟「不用『老化』这个功能」). The rule
        was: an act-listed label gone quiet past ``TIER_ACT_MAX_AGE_HOURS``
        (48h) drops to ``watch``, on the theory that a thing that fired and then
        went silent was either handled or stopped mattering. That holds for
        faults and fails completely for decision requests -- a thing waiting on
        the owner's word is still waiting however quiet it is. Measured cost
        (07-28, first full ``escalation list-open`` sweep): **9 things genuinely
        awaiting a decision were buried by this rule**, the oldest by 1122 hours
        (47 days) -- the C-2 adjudicator three-way choice awaiting 大鸟+Ken,
        ho_555 fork handling, a suspected cross-workspace file revert, the
        bb-space governance gap. The menu bar showed none of them. **A miss that
        hid 9 decisions for over a month costs far more than a false alarm.**
        The only way to clear one now is explicit disposal --
        ``harness escalation resolve <id>`` (whoever decides closes it, name and
        reason required) -- which reaches the code through ``resolved_at`` on the
        ``subject_closures`` recovery route. Dropping the aging without adding
        that exit would have made the red dot monotonically increasing, so the
        two had to ship together.

        **Repetition escalation, removed 2026-07-28.** The rule was: any label
        firing ``TIER_ESCALATE_COUNT`` (20) times within
        ``TIER_ESCALATE_WINDOW_HOURS`` (24) is promoted to ``act``, on the theory
        that a thing still firing and still unfixed is unattended whatever its
        label says. True under "act = the serious things"; false under the
        owner's re-set "act = a decision awaiting you or Duke" -- **firing a lot
        does not mean someone must decide**, only that I have not fixed it. It
        also dragged just-demoted probe alerts straight back in front of the
        owner (this month ack_probe_down 22, ledger_lint_probe_down 20, both
        over the threshold). "Fires forever and nobody acts" is still a real
        problem; its exit is now the nightly patrol, which must reach zero and
        otherwise emits ``daily_patrol_action_required`` -- so it still reaches
        the owner, but **as "I cannot settle this, please decide"** rather than
        leaving him to spot which of a pile of repeats deserves attention.

        Both constants were deleted from ``live_contract.py`` on 2026-08-01,
        having been unread since the rules above were removed.
        """
        if self.match_label in TIER_DONE_LABELS:
            return TIER_DONE
        if (
            self.requires_human
            and self.human_decision_kind in HUMAN_DECISION_KINDS
        ):
            return TIER_ACT
        return TIER_WATCH

    def reason(self) -> str:
        """Human-readable one-liner: when it started, when last seen, how often.

        Timestamps are stored in UTC and rendered in the owner's timezone, so
        every entry in the list sits on one clock regardless of which machine
        emitted it.
        """
        # 必须带 UTC+8 后缀：同一份 snapshot 里 occurred_at 是 UTC,这里是本地时间,
        # 两套时钟不标注就会被读成"未来告警"(03:00 UTC 的事渲染成 11:00,读者按 UTC
        # 理解 = 7 小时后才发生)。标注是唯一区分手段,不要为省字省掉。
        first_local = self.first_seen.astimezone(DISPLAY_TZ)
        last_local = self.last_seen.astimezone(DISPLAY_TZ)
        span = f"{first_local:%m-%d %H:%M}(UTC+8) 起"
        if self.occurrence_count > 1:
            span = f"{span} · 最近 {last_local:%m-%d %H:%M} · {self.occurrence_count} 次"
        if self.subject != "-":
            span = f"{span} · {self.subject}"
        excerpt = _alert_message_excerpt(self.message)
        return f"{span}｜{excerpt}" if excerpt else span


# 大鸟面板上「未闭环事故」的挂龄门限（天）。探针自身的 15 分钟门限是给
# dispatcher 的运维信号，不是给人看的——2026-07-31 大鸟定 3 天。
OWNER_VISIBLE_ACK_OVERDUE_DAYS = 3.0

# 这些 agent 由租户里的真人接活，按设计就没有 dispatcher cron。真源是
# harness-site-policy.yaml 的 tenant_namespaces；读不到时用它兜底。宁可少挡：
# 漏挡只是多一条噪音，错挡会吞掉真故障。
_TENANT_DISPATCHER_EXEMPT_FALLBACK = ("cz-pulse", "xb", "cici")


_LEDGER_OBJECT_ID = re.compile(r"\b((?:ho|wi)_\d{8}_[0-9a-z]+)\b")


def _ledger_finding_label(raw_key: str) -> str:
    """A safe, checkable label for one lint finding.

    Renders the invariant name plus our own object id when the key carries
    one — that id is what makes the finding actionable ("跑 harness show
    ho_20260729_879"). Any other text in the key comes from upstream and is
    dropped, in line with this module's rule that unreviewed source prose never
    reaches the snapshot.
    """
    invariant = raw_key.split(":", 1)[0].strip() if ":" in raw_key else ""
    match = _LEDGER_OBJECT_ID.search(raw_key)
    if match and invariant:
        return f"{invariant} {match.group(1)}"
    if match:
        return match.group(1)
    return invariant or "未具名发现"


def _tenant_agent_ids(site_policy_path: Optional[str] = None) -> frozenset:
    """Tenant agents that legitimately have no dispatcher cron."""
    path = site_policy_path or os.environ.get("HARNESS_SITE_POLICY_PATH")
    if path:
        try:
            import yaml

            with open(path, "r", encoding="utf-8") as handle:
                policy = yaml.safe_load(handle) or {}
            namespaces = policy.get("tenant_namespaces")
            if isinstance(namespaces, dict) and namespaces:
                return frozenset(str(key) for key in namespaces)
        except Exception:  # noqa: BLE001 — fall back rather than lose the panel
            pass
    return frozenset(_TENANT_DISPATCHER_EXEMPT_FALLBACK)


def _is_tenant_dispatcher_finding(raw_key: str, tenant_agents: frozenset) -> bool:
    """True when an INV2 finding is only "a tenant agent has no dispatcher".

    Keyed on the receiving agent, which is the whole content of that finding.
    Any other invariant (INV3, INV7, …) is never suppressed, and neither is an
    INV2 finding whose receiver is a normal agent that really should have had a
    dispatcher.
    """
    if not raw_key.startswith("INV2:") or not tenant_agents:
        return False
    ho_id = raw_key.split(":", 1)[1].strip()
    return bool(ho_id) and ho_id in _tenant_only_handoffs(tenant_agents)


def _tenant_only_handoffs(tenant_agents: frozenset) -> frozenset:
    """Pending handoff ids whose receivers are all tenant agents."""
    root = os.environ.get("HARNESS_ROOT")
    if not root:
        return frozenset()
    try:
        import yaml

        directory = os.path.join(root, "handoffs")
        found = set()
        for name in os.listdir(directory):
            if not name.endswith(".yaml"):
                continue
            try:
                with open(
                    os.path.join(directory, name), "r", encoding="utf-8"
                ) as handle:
                    record = yaml.safe_load(handle) or {}
            except Exception:  # noqa: BLE001 — one bad file must not blind the rest
                continue
            if not isinstance(record, dict):
                continue
            receivers = record.get("to_owners")
            if not isinstance(receivers, list) or not receivers:
                receivers = [record.get("to_owner")]
            named = [str(x) for x in receivers if x]
            if named and all(name_ in tenant_agents for name_ in named):
                found.add(str(record.get("handoff_id") or name[:-5]))
        return frozenset(found)
    except Exception:  # noqa: BLE001
        return frozenset()


class LiveCollector:
    def __init__(
        self,
        *,
        paths: LivePaths,
        runner: CommandRunner,
        now: datetime,
        ack_argv: Sequence[str] = ACK_ARGV,
        systemd_argv: Sequence[str] = SYSTEMD_ARGV,
        git_argv: Optional[Sequence[str]] = None,
        harness_git_argv: Optional[Sequence[str]] = None,
        http_fetch=None,
        codex_remote_fetch=None,
        codex_probe=None,
        cc_quota_fetch=None,
        cc_token_expiry=None,
        cc_quota_cache_read=None,
        cc_quota_cache_write=None,
    ):
        self.paths = paths
        self.runner = runner
        self.now = now
        self._wi_facts: Optional[Tuple[Optional[Mapping[str, object]], Optional[str]]] = None
        self.ack_argv = tuple(ack_argv)
        self.systemd_argv = tuple(systemd_argv)
        self.git_argv = tuple(git_argv) if git_argv else (
            "/usr/bin/git", "-C", str(paths.repo), "rev-parse", "--short", "HEAD"
        )
        self.harness_git_argv = (
            tuple(harness_git_argv)
            if harness_git_argv
            else (
                "/usr/bin/git",
                "-C",
                str(paths.harness_repo),
                "rev-parse",
                "HEAD",
            )
        )
        # Seams so tests never touch the network or the peer machine.
        self.http_fetch = http_fetch if http_fetch is not None else usage_sources.fetch_json
        self.codex_remote_fetch = (
            codex_remote_fetch
            if codex_remote_fetch is not None
            else (lambda: usage_sources.run_codex_remote(codex_remote_argv(), CODEX_REMOTE_TIMEOUT_SECONDS))
        )
        # The Codex probe spends real subscription quota, so it defaults to a
        # no-op rather than to the live command: a test that forgets to stub it
        # would otherwise bill the owner's account and take 7.7s per case.
        # `pulse-stats-snapshot.py` opts in explicitly.
        self.codex_probe = codex_probe if codex_probe is not None else (lambda: False)
        self.cc_quota_fetch = (
            cc_quota_fetch if cc_quota_fetch is not None else usage_sources.fetch_cc_quota_headers
        )
        # Reads only the expiry stamp, never the credential -- but it still
        # points at a fixed production path, so it gets a seam like the rest.
        self.cc_token_expiry = (
            cc_token_expiry if cc_token_expiry is not None else usage_sources.cc_token_expiry
        )
        # 缓存读写同样给 seam：测试不许碰生产盘上的那份。
        self.cc_quota_cache_read = (
            cc_quota_cache_read if cc_quota_cache_read is not None
            else usage_sources.read_cc_quota_cache
        )
        self.cc_quota_cache_write = (
            cc_quota_cache_write if cc_quota_cache_write is not None
            else usage_sources.write_cc_quota_cache
        )

    def collect(self, existing: Sequence[IncidentProjection]) -> Collection:
        from . import legacy_retirement
        collection = Collection(
            capabilities=[],
            metrics=[],
            events=[],
            provenance_sources=[],
            source_revisions={},
            source_watermarks={},
            source_cursors={},
            incident_action_overrides={},
            core_successes=0,
            projection_complete=False,
        )
        # The two remote quota probes are the slowest sources (Codex over ssh at
        # 3.4-5.2s, the CC API call bounded at 5s). Both start first and run
        # concurrently with each other and with local collection, so their
        # latency overlaps instead of adding to the end-to-end budget.
        with ThreadPoolExecutor(max_workers=2) as pool:
            codex_remote_future = pool.submit(self._safe_codex_remote)
            cc_quota_future = (pool.submit(self._safe_cc_quota)
                               if self.paths.cc_quota is None else None)
            heartbeat_ok, heartbeat_fresh = self._collect_heartbeats(collection, existing)
            if self.paths.retire_legacy_probes:
                ack_ok = ledger_ok = True
                collection.capabilities.extend(legacy_retirement.capability(k) for k in ('s2.ack_overdue','s2.ledger_invariants'))
            else:
                ack_ok = self._collect_ack(collection, existing)
                ledger_ok = self._collect_ledger(collection, existing, heartbeat_fresh)
            systemd_ok = self._collect_systemd(collection)
            self._collect_alerts(collection)
            self._collect_revision(collection)
            self._collect_usage_metrics(collection)
            if self.paths.emit_legacy_cost_metrics or self.paths.compute_cost_shadow_enabled:
                collection.metrics.extend(managed_cost_shadow.build_managed_cost_shadow_metrics(
                    enabled=self.paths.compute_cost_shadow_enabled,
                    path=self.paths.compute_cost_shadow,
                    now=self.now,
                ))
            self._collect_harness_metrics(collection)
            self._collect_relay_metrics(collection, codex_remote_future, cc_quota_future)
        # An unregistered heartbeat source (None) is neither a success nor a gap.
        projection = [ok for ok in ((heartbeat_ok, systemd_ok) if self.paths.retire_legacy_probes
                                    else (heartbeat_ok, ack_ok, ledger_ok)) if ok is not None]
        core = [ok for ok in ((heartbeat_ok, systemd_ok) if self.paths.retire_legacy_probes
                              else (heartbeat_ok, ack_ok, ledger_ok, systemd_ok)) if ok is not None]
        collection.core_successes = sum(core)
        collection.projection_complete = bool(projection) and all(projection)
        self._collect_request_trace(collection)
        self._add_baseline_gaps(collection)
        collection.capabilities.append(_capability(
            "snapshot.canonical_producer", health="ok", freshness="fresh", observed_at=self.now,
            max_age_seconds=180, reason="One-shot canonical producer completed this collection",
            implementation_status="active_partial", aggregation_role="coverage_only",
            evidence=("producer:pulse-stats-snapshot",),
        ))
        return collection

    def _collect_request_trace(self, collection: Collection) -> None:
        """把 `s1.request_trace_capture` 从占位 gap 升成真读源的能力。

        SPEC §143 早已判它 `active_validated`,exporter 却一直用
        `_add_baseline_gaps` 里那条 "Trace source is not yet adapted" 占位——
        2026-08-02 查出的 spec-code drift,且单这一项就把 overall coverage
        短路成 unknown。这里真去读 obs 事件流,读不到就**不 append**,让占位
        条目照常兜底(gap 表按 id 跳过已存在项)。

        判据(SPEC §322「只有 active_validated 且 freshness 合格的能力可以
        为其 scope 提供绿色证据」):三类埋点当天全部到齐才算 validated+fresh;
        只到一部分说明链路断在中间,降 active_partial+stale 并说明缺哪几类。
        """
        trace = usage_sources.collect_request_trace(self.paths.obs, self.now)
        if trace is None:
            return
        counts = trace["counts"]
        complete = trace["kinds_present"] == trace["kinds_expected"]
        detail = "、".join(f"{name} {counts[name]}" for name in counts)
        if complete:
            status, freshness, health = "active_validated", "fresh", "ok"
            reason = f"三类埋点当天均在落盘（{detail}）"
        else:
            status, freshness, health = "active_partial", "stale", "unknown"
            missing = "、".join(name for name, n in counts.items() if not n)
            reason = f"埋点未到齐,缺 {missing}（{detail}）"
        collection.capabilities.append(_capability(
            "s1.request_trace_capture", health=health, freshness=freshness,
            observed_at=trace["latest"] if complete else None,
            max_age_seconds=86400, reason=reason,
            implementation_status=status, aggregation_role="coverage_only",
            evidence=("source:relay-obs-request-trace",),
        ))

    def _add_baseline_gaps(self, collection: Collection) -> None:
        present = {str(item["id"]) for item in collection.capabilities}
        definitions = (
            ("s1.request_trace_capture", "active_partial", "unknown", "Trace source is not yet adapted"),
            ("s1.request_reply_reconciliation", "retired", "not_applicable", "Heuristic reconciliation is retired"),
            ("notify.action_package", "active_partial", "unknown", "No readable current alert source was available"),
            ("policy.commitment_registry", "active_partial", "unknown", "Policy registry is not yet adapted"),
            ("s1.stable_interaction_identity", "not_implemented", "not_applicable", "Stable interaction identity is not implemented"),
            ("s1.external_input_delivery_canary", "not_implemented", "not_applicable", "External-input delivery canary is not implemented"),
            ("s1.headless_artifact_reconciliation", "not_implemented", "not_applicable", "Headless artifact reconciliation is not implemented"),
            ("s2.cross_node_reconciliation", "not_implemented", "not_applicable", "Cross-node reconciliation is not implemented"),
            ("slo.commitment_curves", "not_implemented", "not_applicable", "Commitment curves are not implemented"),
            ("ops.incident_ledger", "not_implemented", "not_applicable", "Incident ledger is not implemented"),
        )
        for capability_id, status, freshness, reason in definitions:
            if capability_id in present:
                continue
            collection.capabilities.append(_capability(
                capability_id,
                health="not_applicable" if freshness == "not_applicable" else "unknown",
                freshness=freshness, observed_at=None, max_age_seconds=None, reason=reason,
                implementation_status=status, aggregation_role="coverage_only",
                evidence=(f"baseline:{BASELINE_VERSION}",),
            ))

    def _collect_heartbeats(
        self,
        collection: Collection,
        existing: Sequence[IncidentProjection],
    ) -> Tuple[Optional[bool], Dict[str, Tuple[bool, Optional[datetime]]]]:
        freshness_by_source: Dict[str, Tuple[bool, Optional[datetime]]] = {}
        if self.paths.registry is None:
            # Not a stale signal: this reader has no registered heartbeat receiver.
            collection.capabilities.append(_capability(
                "meta.probe_heartbeat", health="not_applicable", freshness="not_applicable",
                observed_at=None, max_age_seconds=None,
                reason="本机读取端未登记心跳接收端；Hestia 心跳由 TCloud Relay 接收，Stats 不读取",
                implementation_status="not_implemented", aggregation_role="coverage_only",
                evidence=("source:heartbeat-registry-unregistered",),
            ))
            collection.provenance_sources.append(_source(
                "probe-heartbeats", "unknown", error_code="registry_not_registered"
            ))
            from .legacy_retirement import capability
            collection.capabilities.extend(
                capability(capability_id) for capability_id in ("s2.executor_reaper", "notify.daily_flush"))
            return None, freshness_by_source
        try:
            registry = json.loads(self.paths.registry.read_text(encoding="utf-8"))
            heartbeats = json.loads(self.paths.heartbeats.read_text(encoding="utf-8"))
            sources = registry["sources"]
            if registry.get("schema_version") != 1 or not isinstance(sources, list):
                raise ValueError("invalid heartbeat registry")
            if isinstance(heartbeats, dict) and isinstance(heartbeats.get("sources"), dict):
                heartbeats = heartbeats["sources"]
            if not isinstance(heartbeats, dict):
                raise ValueError("invalid heartbeat map")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            collection.capabilities.append(_capability(
                "meta.probe_heartbeat", health="unknown", freshness="error", observed_at=None,
                max_age_seconds=None, reason="Heartbeat source could not be read",
                implementation_status="active_partial", evidence=("source:heartbeat-registry",),
            ))
            collection.provenance_sources.append(_source("probe-heartbeats", "error", error_code="read_or_parse"))
            return False, freshness_by_source

        observed_values: List[datetime] = []
        any_stale = False
        enabled_count = 0
        for raw in sources:
            if isinstance(raw, dict) and raw.get("enabled") is True:
                if not isinstance(raw.get("source"), str) or not isinstance(raw.get("max_age_seconds"), int):
                    collection.capabilities.append(_capability(
                        "meta.probe_heartbeat", health="unknown", freshness="error", observed_at=None,
                        max_age_seconds=None, reason="Enabled heartbeat registry entry is invalid",
                        implementation_status="active_partial", evidence=("source:heartbeat-registry",),
                    ))
                    collection.provenance_sources.append(_source(
                        "probe-heartbeats", "error", error_code="invalid_registry_entry"
                    ))
                    return False, freshness_by_source
        for raw in sources:
            if not isinstance(raw, dict) or raw.get("enabled") is not True:
                continue
            source_id = raw.get("source")
            max_age = raw.get("max_age_seconds")
            if not isinstance(source_id, str) or not isinstance(max_age, int):
                continue
            enabled_count += 1
            entry = heartbeats.get(source_id)
            observed: Optional[datetime] = None
            if isinstance(entry, dict):
                for field in ("receivedAt", "ts"):
                    if entry.get(field):
                        try:
                            observed = _parse_date(entry[field])
                            break
                        except ValueError:
                            continue
            age = self.now - observed if observed is not None else None
            fresh = age is not None and timedelta(0) <= age <= timedelta(seconds=max_age)
            freshness_by_source[source_id] = (fresh, observed)
            if observed:
                observed_values.append(observed)
            any_stale = any_stale or not fresh
            scope_key = _short_hash(source_id)
            key_suffix = f"meta.probe_heartbeat:source_stale:{scope_key}"
            journal_unit = _heartbeat_journal_unit(raw, source_id)
            collection.incident_action_overrides[key_suffix] = _heartbeat_action(raw, source_id)
            prior = next((item for item in existing if item.incident_key == key_suffix), None)
            if not fresh:
                if observed is not None:
                    # A heartbeat becomes stale at the threshold boundary, not
                    # at its old success timestamp. This stable onset is later
                    # than that success and identifies one stale episode across
                    # repeated collections.
                    occurred_at = observed + timedelta(seconds=max_age)
                elif prior is not None and prior.state == "open":
                    # There is no source timestamp for a missing heartbeat. Reuse
                    # the current episode's opening time so refreshes stay
                    # idempotent; after recovery, the next detection uses now and
                    # therefore creates a distinct episode.
                    occurred_at = prior.opened_at
                else:
                    occurred_at = self.now
                collection.events.append(IncidentEvent(
                    event_id=f"heartbeat-stale-{scope_key}-{_short_hash(occurred_at.isoformat())}",
                    kind="breach", capability_id="meta.probe_heartbeat", condition_key="source_stale",
                    # 面板上留下的唯一一类事故，必须说人话、必须点名是哪个探针
                    # ——scope 从哈希改成 source_id 本身，大鸟拿它就能直接查
                    # journalctl。措辞按「要大鸟做什么」定档：探针哑了＝我这边
                    # 的监控瞎了一只眼，他要知道但不必立刻动手，故 warning。
                    scope=f"探针 {source_id}", normalized_scope_key=scope_key,
                    severity="warning",
                    title=f"探针 {source_id} 没心跳了",
                    occurred_at=occurred_at,
                    reason="这个探针超过约定时限没上报，它盯的那段现在没人看",
                    impact="监控有盲区：这段时间即使真出事，也不会有告警",
                    suggested_action=collection.incident_action_overrides[key_suffix],
                ))
            elif prior is not None and prior.state == "open":
                collection.events.append(IncidentEvent(
                    event_id=f"heartbeat-recovery-{scope_key}-{_short_hash(observed.isoformat())}",
                    kind="recovery", capability_id="meta.probe_heartbeat", condition_key="source_stale",
                    scope=f"探针 {source_id}", normalized_scope_key=scope_key,
                    severity="warning", title=f"探针 {source_id} 心跳恢复", occurred_at=observed,
                    reason="这个探针又开始按时上报了",
                ))

        latest = max(observed_values) if observed_values else None
        state = "stale" if any_stale or enabled_count == 0 else "fresh"
        collection.capabilities.append(_capability(
            "meta.probe_heartbeat", health="stale" if state == "stale" else "ok",
            freshness=state, observed_at=latest, max_age_seconds=None,
            reason="One or more enabled heartbeat sources are stale" if state == "stale" else "Enabled heartbeat sources are fresh",
            implementation_status="active_partial", evidence=("source:probe-heartbeats",),
        ))
        collection.provenance_sources.append(_source("probe-heartbeats", state, latest))
        if latest:
            collection.source_watermarks["probe-heartbeats"] = latest.isoformat()
        for source_id, capability_id, label, maximum in (
            ("vultr-executor-reaper", "s2.executor_reaper", "Executor reaper", 8100),
            ("vultr-harness-daily-flush", "notify.daily_flush", "Daily escalation flush", 97200),
        ):
            if self.paths.retire_legacy_probes:
                from .legacy_retirement import capability
                collection.capabilities.append(capability(capability_id))
                continue
            source_fresh, source_observed = freshness_by_source.get(source_id, (False, None))
            source_state = "fresh" if source_fresh else ("stale" if source_observed else "unknown")
            collection.capabilities.append(_capability(
                capability_id, health="ok" if source_fresh else source_state,
                freshness=source_state, observed_at=source_observed, max_age_seconds=maximum,
                reason=f"{label} heartbeat is {source_state}", evidence=(f"heartbeat:{source_id}",),
            ))
        return True, freshness_by_source

    def _collect_ack(self, collection: Collection, existing: Sequence[IncidentProjection]) -> bool:
        if not self.ack_argv:
            collection.capabilities.append(_capability(
                "s2.ack_overdue", health="unknown", freshness="error", observed_at=None,
                max_age_seconds=2100, reason="Ack probe is retired and no command was injected",
                evidence=("command:ack-probe-not-configured",),
            ))
            collection.provenance_sources.append(_source("ack-probe", "error", error_code="not_configured"))
            return False
        try:
            result = self.runner.run(self.ack_argv, timeout=ACK_TIMEOUT_SECONDS)
            if result.returncode != 0:
                raise ValueError("nonzero")
            raw = json.loads(result.stdout)
            observed = _parse_date(raw["ts"])
            breaches = raw["breaches"]
            if not isinstance(breaches, list):
                raise ValueError("breaches")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            collection.capabilities.append(_capability(
                "s2.ack_overdue", health="unknown", freshness="error", observed_at=None,
                max_age_seconds=2100, reason="Ack probe could not be read", evidence=("command:ack-probe-json",),
            ))
            collection.provenance_sources.append(_source("ack-probe", "error", error_code="command_or_parse"))
            return False

        current_keys = set()
        for breach in breaches:
            if not isinstance(breach, dict) or not breach.get("ho_id"):
                continue
            scope_key = _short_hash(breach["ho_id"])
            current_keys.add(f"s2.ack_overdue:handoff_overdue:{scope_key}")
            occurred = observed
            if breach.get("created_at"):
                try:
                    occurred = _parse_date(breach["created_at"])
                except ValueError:
                    pass
            # 只有真的挂久了才进面板。探针的 15 分钟门限是给 dispatcher 的
            # 运维信号——「发出 15 分钟没人接」说明自动接活卡住了，值得机器
            # 关注；摆到大鸟面前当「未闭环事故」就是噪音。2026-07-31 他看到
            # Reeve 14:19 发的单 14:34 已被报成事故，问「这些是什么？」。
            # 他当天定的线是 3 天。
            ho_id = str(breach["ho_id"])
            age_days = max(0.0, (self.now - occurred).total_seconds() / 86400.0)
            if age_days < OWNER_VISIBLE_ACK_OVERDUE_DAYS:
                current_keys.discard(f"s2.ack_overdue:handoff_overdue:{scope_key}")
                continue
            receivers = breach.get("receivers")
            named = [str(x) for x in receivers if x] if isinstance(receivers, list) else []
            waiting_on = f"，在等 {'、'.join(named)}" if named else ""
            collection.events.append(IncidentEvent(
                event_id=f"ack-breach-{scope_key}-{_short_hash(occurred.isoformat())}", kind="breach",
                capability_id="s2.ack_overdue", condition_key="handoff_overdue",
                # 单号写进 scope：哈希只够机器去重，大鸟要能直接拿它去查。
                scope=f"交接单 {ho_id}", normalized_scope_key=scope_key, severity="warning",
                title=f"{ho_id} 发出 {int(age_days)} 天还没人接{waiting_on}",
                occurred_at=occurred,
                reason="交接单一直是 pending，没有 agent 认领",
                impact="这件活还没开始做",
                suggested_action=f"跑 harness show {ho_id} 看是谁的活，必要时改派",
            ))
        for prior in existing:
            if prior.capability_id != "s2.ack_overdue" or prior.state != "open" or prior.incident_key in current_keys:
                continue
            scope_key = prior.incident_key.rsplit(":", 1)[-1]
            collection.events.append(IncidentEvent(
                event_id=f"ack-recovery-{scope_key}-{_short_hash(observed.isoformat())}", kind="recovery",
                capability_id="s2.ack_overdue", condition_key="handoff_overdue",
                scope=prior.scope, normalized_scope_key=scope_key, severity="warning",
                title="Handoff acknowledgement recovered", occurred_at=observed,
                reason="The handoff is no longer reported as overdue",
            ))
        pending = raw.get("pending_total")
        collection.metrics.append({
            "id": "s2.ack_overdue.pending", "capability_id": "s2.ack_overdue",
            "status": "available" if isinstance(pending, int) else "unknown",
            "value": pending if isinstance(pending, int) else None, "unit": "count", "window": "current",
            "sample_count": None, "quality": "validated", "scope": "enabled dispatchers",
            "observed_at": observed.isoformat(), "reason": None,
        })
        collection.capabilities.append(_capability(
            "s2.ack_overdue", health="warning" if breaches else "ok", freshness="fresh",
            observed_at=observed, max_age_seconds=2100,
            reason=f"{len(breaches)} current acknowledgement breaches", evidence=("command:ack-probe-json",),
        ))
        collection.provenance_sources.append(_source("ack-probe", "fresh", observed))
        collection.source_watermarks["ack-probe"] = observed.isoformat()
        return True

    def _collect_ledger(
        self,
        collection: Collection,
        existing: Sequence[IncidentProjection],
        heartbeat_fresh: Mapping[str, Tuple[bool, Optional[datetime]]],
    ) -> bool:
        observed = self.now
        try:
            if self.paths.ledger.exists():
                raw = json.loads(self.paths.ledger.read_text(encoding="utf-8"))
                if raw.get("schema_version") != 1 or not isinstance(raw.get("keys"), list):
                    raise ValueError("invalid ledger state")
                keys = [str(value) for value in raw["keys"]]
                fingerprint = str(raw.get("fingerprint", _short_hash("|".join(keys))))
            else:
                fresh, heartbeat_at = heartbeat_fresh.get("vultr-ledger-lint-probe", (False, None))
                if not fresh:
                    raise FileNotFoundError("ledger state absent without fresh lint heartbeat")
                keys = []
                fingerprint = "clean"
                observed = heartbeat_at or self.now
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            collection.capabilities.append(_capability(
                "s2.ledger_invariants", health="unknown", freshness="error", observed_at=None,
                max_age_seconds=8100, reason="Ledger projection could not be read", evidence=("source:ledger-state",),
            ))
            collection.provenance_sources.append(_source("ledger-state", "error", error_code="read_or_parse"))
            return False

        # 账本违例**不进大鸟的面板**（2026-08-04 大鸟裁，同日两处执行：探针侧
        # 停推送 11adfc1，本处停显示）。台账违例是 agent 的日常活——INV7 多半
        # 只是「文件还没 commit」——第一出口是账上的单，由日巡和 dispatcher
        # 消化。摆到面板上既要不到决策，又把「未闭环事故」稀释成噪音：97 条
        # 历史事故里 69 条是账本类，没有一条真正需要大鸟出手。
        #
        # current_keys 恒为空集 = 本 capability 不再产生任何 breach，且下面的
        # 消解循环会把**存量 open 的账本事故全部关掉**（不留尸体在面板上）。
        # 只关显示不关 capability：s2.ledger_invariants 的 health 仍照常上报
        # （见函数末尾），仪表盘看得见「账本体检 N 条」，守恒校验照跑。
        # 真正该惊动大鸟的是「自动处置链失灵」——由探针在 24h 未自愈时推一次
        # （11adfc1），不走本区块。
        #
        # 遗留：_is_tenant_dispatcher_finding / _ledger_finding_label /
        # _tenant_agent_ids 现在**只被测试引用**，生产路径零调用。没有顺手删，
        # 因为它们承载着「租户 agent 没有 dispatcher 是设计不是故障」这条判据
        # （2026-07-31 大鸟问「怎么还存在？」的结论）——若日后账本类要以别的
        # 形式回到面板，这条判据仍然成立、不该重新踩一遍。但要认清现状：
        # 它们是被自己的测试吊着命的死代码，下次系统性舍弃该一并处置。
        current_keys: set[str] = set()
        for prior in existing:
            if prior.capability_id != "s2.ledger_invariants" or prior.state != "open" or prior.incident_key in current_keys:
                continue
            scope_key = prior.incident_key.rsplit(":", 1)[-1]
            # id 必须带 observed，不能只带 fingerprint：账本 clean 时 fingerprint
            # 恒为 "clean"，每轮生成的 recovery id 就完全相同。第一条若因时序
            # （recovery 的 occurred_at 早于 last_breach_at）未能关单，它已被记进
            # seen_event_ids，此后每轮重试都在 _apply_recovery 第一行被当重复事件
            # 丢弃 —— 事故就此永久不可消解（2026-08-04 实测：五条 ledger 事故
            # 带着 recovery_evidence 却一直 state=open）。幂等键不能取一个在正常
            # 状态下恒定的值，否则"我又查了一次，仍然是好的"会被读成"这是旧消息"。
            collection.events.append(IncidentEvent(
                event_id=(
                    f"ledger-recovery-{scope_key}-{_short_hash(fingerprint)}"
                    f"-{int(observed.timestamp())}"
                ), kind="recovery",
                capability_id="s2.ledger_invariants", condition_key="finding", scope=prior.scope,
                normalized_scope_key=scope_key, severity="warning", title="Ledger finding recovered",
                occurred_at=observed, reason="The latest complete lint projection no longer contains this finding",
            ))
        collection.capabilities.append(_capability(
            "s2.ledger_invariants", health="warning" if keys else "ok", freshness="fresh",
            observed_at=observed, max_age_seconds=8100, reason=f"{len(keys)} current ledger findings",
            evidence=(f"ledger-fingerprint:{_short_hash(fingerprint)}",),
        ))
        collection.provenance_sources.append(_source("ledger-state", "fresh", observed))
        collection.source_watermarks["ledger-state"] = observed.isoformat()
        collection.source_cursors["ledger-state"] = _short_hash(fingerprint)
        return True

    def _collect_launchd(self, collection: Collection) -> bool:
        """The macOS reader's Relay is one per-user launchd job."""
        try:
            result = self.runner.run((LAUNCHCTL, "print", f"gui/{os.getuid()}/{RELAY_LAUNCHD_LABEL}"))
            if result.returncode != 0:
                raise ValueError("nonzero")
            states = re.findall(r"^\tstate = (.+)$", result.stdout, re.MULTILINE)
            if len(states) != 1:
                raise ValueError("unparsed launchd state")
            healthy = states[0].strip() == "running"
        except (OSError, subprocess.SubprocessError, ValueError):
            collection.capabilities.append(_capability(
                "meta.systemd_health", health="unknown", freshness="error", observed_at=None,
                max_age_seconds=180, reason="Fixed launchd query failed", evidence=("command:launchctl-print",),
            ))
            collection.provenance_sources.append(_source("launchd", "error", error_code="command_or_parse"))
            return False
        collection.capabilities.append(_capability(
            "meta.systemd_health", health="ok" if healthy else "unknown",
            freshness="fresh" if healthy else "error", observed_at=self.now, max_age_seconds=180,
            reason="Relay launchd job is running" if healthy else "Relay launchd job is not running",
            evidence=("command:launchctl-print",),
        ))
        collection.provenance_sources.append(_source("launchd", "fresh" if healthy else "error", self.now,
                                                     None if healthy else "unit_not_active"))
        collection.source_watermarks["launchd"] = self.now.isoformat()
        return True

    def _collect_systemd(self, collection: Collection) -> bool:
        if self.paths.relay_supervisor == "launchd":
            return self._collect_launchd(collection)
        try:
            argv = ("/usr/bin/systemctl","--user","show","relay.service",
                    "--property=Id,ActiveState,UnitFileState,Result","--no-pager") if self.paths.retire_legacy_probes else self.systemd_argv
            result = self.runner.run(argv)
            if result.returncode != 0:
                raise ValueError("nonzero")
            blocks = [block for block in result.stdout.split("\n\n") if block.strip()]
            unit_states: Dict[str, str] = {}
            for block in blocks:
                fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
                if fields.get("Id"):
                    unit_states[fields["Id"]] = fields.get("ActiveState", "unknown")
            ids = list(unit_states)
            active_count = sum(value == "active" for value in unit_states.values())
            if not ids:
                raise ValueError("no units")
            required = ("relay.service",) if self.paths.retire_legacy_probes else SYSTEMD_UNITS
            healthy = set(required).issubset(set(ids)) and all(unit_states[k] == "active" for k in required)
        except (OSError, subprocess.SubprocessError, ValueError):
            collection.capabilities.append(_capability(
                "meta.systemd_health", health="unknown", freshness="error", observed_at=None,
                max_age_seconds=180, reason="Fixed systemd query failed", evidence=("command:systemctl-show",),
            ))
            collection.provenance_sources.append(_source("systemd", "error", error_code="command_or_parse"))
            return False
        collection.capabilities.append(_capability(
            "meta.systemd_health", health="ok" if healthy else "unknown",
            freshness="fresh" if healthy else "error", observed_at=self.now, max_age_seconds=180,
            reason="Fixed Relay and timer units are active" if healthy else "One or more fixed units are not active",
            evidence=("command:systemctl-show",),
        ))
        collection.provenance_sources.append(_source("systemd", "fresh" if healthy else "error", self.now, None if healthy else "unit_not_active"))
        collection.source_watermarks["systemd"] = self.now.isoformat()
        if self.paths.retire_legacy_probes:
            return True
        degen_active = unit_states.get("degen-probe.timer") == "active"
        collection.capabilities.append(_capability(
            "s1.reply_quality_degen", health="ok" if degen_active else "unknown",
            freshness="fresh" if degen_active else "error", observed_at=self.now,
            max_age_seconds=180, reason="Degeneration probe timer is active" if degen_active else "Degeneration probe timer is not active",
            evidence=("systemd:degen-probe.timer",),
        ))
        return True

    def _recovery_heartbeats(self) -> Optional[Dict[str, datetime]]:
        """Latest heartbeat per source that can prove an alert recovered.

        Returns ``None`` -- not an empty map -- when the file cannot be read or
        parsed, so that a missing source degrades to the old age-only rule
        instead of silently declaring everything recovered. Timestamps are
        normalised to UTC by ``_parse_date``; the file genuinely mixes ``Z``,
        ``+00:00`` and ``+0800`` because each probe stamps on its own clock.
        """
        try:
            raw = json.loads(self.paths.heartbeats.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if isinstance(raw, dict) and isinstance(raw.get("sources"), dict):
            raw = raw["sources"]
        if not isinstance(raw, dict):
            return None
        latest: Dict[str, datetime] = {}
        for source in set(RECOVERY_HEARTBEAT_SOURCES.values()):
            entry = raw.get(source)
            if not isinstance(entry, dict):
                continue
            for field in ("receivedAt", "ts"):
                if entry.get(field):
                    try:
                        latest[source] = _parse_date(entry[field])
                        break
                    except ValueError:
                        continue
        return latest

    def _recovery_units(self) -> Optional[Dict[str, Tuple[int, Optional[datetime]]]]:
        """Last exit status and time for the probe units that post no heartbeat.

        Returns ``None`` -- not an empty map -- when systemctl is unavailable,
        times out or cannot be parsed, so that missing evidence degrades to the
        age-only rule instead of silently declaring anything recovered.
        """
        if self.paths.relay_supervisor != "systemd":
            return None
        try:
            # The shared runner's signature is `run(argv)`; the production
            # implementation also takes a timeout, so pass one when it is
            # supported and fall back cleanly when it is not (test fakes).
            try:
                result = self.runner.run(
                    RECOVERY_UNIT_ARGV, timeout=RECOVERY_UNIT_TIMEOUT_SECONDS
                )
            except TypeError:
                result = self.runner.run(RECOVERY_UNIT_ARGV)
            if result.returncode != 0:
                return None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

        units: Dict[str, Tuple[int, Optional[datetime]]] = {}
        for block in result.stdout.split("\n\n"):
            if not block.strip():
                continue
            fields = dict(
                line.split("=", 1) for line in block.splitlines() if "=" in line
            )
            unit_id = fields.get("Id")
            if not unit_id:
                continue
            try:
                status = int(fields.get("ExecMainStatus", ""))
            except ValueError:
                # No exit status recorded (never run, or still running): no
                # evidence either way.
                continue
            units[unit_id] = (
                status,
                _parse_systemd_timestamp(fields.get("ExecMainExitTimestamp", "")),
            )
        return units or None

    def _recovery_agent_successes(self) -> Optional[Dict[str, datetime]]:
        """Latest successful autonomous run per agent.

        Returns ``None`` -- not an empty map -- when the stream cannot be read
        or parsed, so missing evidence degrades to the age-only rule instead of
        silently declaring anything recovered. Only completed successes are
        recorded; a `started` event says a session was launched, not that it
        accomplished anything.
        """
        path = self.paths.alerts_dir / f"{AUTONOMOUS_EVENTS_PREFIX}-{self.now:%Y-%m}.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return None
        latest: Dict[str, datetime] = {}
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if str(raw.get("status")) != RECOVERY_AGENT_SUCCESS_STATUS:
                    continue
                agent = raw.get("agent_id")
                if not isinstance(agent, str) or not agent.strip():
                    continue
                occurred = _parse_date(raw["occurred_at"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # One malformed record must never abort the collection.
                continue
            agent = agent.strip()
            if agent not in latest or occurred > latest[agent]:
                latest[agent] = occurred
        return latest

    def native_wi_facts(self) -> Tuple[Optional[Mapping[str, object]], Optional[str]]:
        """Native core WorkItem facts, read once per collection; None when unconfigured."""
        if self._wi_facts is None:
            self._wi_facts = wi_facts.load(self.paths.wi_facts, self.now)
        return self._wi_facts

    def _collect_alerts(self, collection: Collection) -> None:
        # 读当月 + 往回若干个月分片。**只读当月是第三道把决策请求挡在门外的闸**
        # （2026-07-28 查出）：47 天前那条 C-2 接入路径待决躺在 2026-06 分片里，
        # 就算不老化、就算放宽读取窗口，只要不翻上个月的文件就永远看不见。
        # 三道闸——分档老化 / 读取窗口 / 分片范围——**任一道没改，「不老化」都
        # 等于没做**。这是「机制只做了一半」在同一件事上连犯三次的现场。
        lines: List[str] = []
        missing_current = False
        for offset in range(DECISION_REQUEST_SHARD_MONTHS):
            month = _month_shift(self.now, -offset)
            path = self.paths.alerts_dir / f"escalation_event-{month:%Y-%m}.jsonl"
            try:
                lines.extend(path.read_text(encoding="utf-8").splitlines())
            except OSError:
                # 当月缺失才算源不可用；历史分片不存在是正常的（新装机器、
                # 或那个月压根没有事件），不该报成采集失败。
                if offset == 0:
                    missing_current = True
        if missing_current and not lines:
            collection.provenance_sources.append(_source("escalation-events", "unknown", error_code="missing"))
            # 不 return：「要你出手」读的是 main_only handoff，与 escalation 流
            # 是两个独立源。共用一条失败出口会让「事件文件缺失」顺手吞掉整个
            # 红点——正是本文件反复栽的那种「一半机制」。
            lines = []

        cutoff = self.now - timedelta(days=ALERT_WINDOW_DAYS)
        groups: "OrderedDict[Tuple[str, str], _AlertGroup]" = OrderedDict()
        raw_accepted = 0
        # The escalation log is an append-only state stream.  A later row with
        # the same event_id supersedes the earlier row (delivery result,
        # resolution, or a freshness suppression).  Grouping every row first
        # leaves the old unresolved copy visible forever, because skipping the
        # new resolved row does not remove the group created by the old one.
        latest_by_event: "OrderedDict[str, Mapping[str, object]]" = OrderedDict()
        for line_number, line in enumerate(lines):
            try:
                raw = json.loads(line)
                occurred = _parse_date(raw["occurred_at"])
                match_label = str(raw.get("match_label", ""))
                ALERT_TEMPLATES[match_label]
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # One malformed record must never abort the whole collection.
                continue
            if occurred < cutoff:
                continue
            event_id = raw.get("event_id")
            logical_id = (
                str(event_id).strip()
                if isinstance(event_id, str) and event_id.strip()
                else f"legacy-line-{line_number}"
            )
            latest_by_event[logical_id] = raw
            raw_accepted += 1

        for raw in latest_by_event.values():
            try:
                occurred = _parse_date(raw["occurred_at"])
                severity = _ALERT_SEVERITY.get(str(raw.get("severity", "minor")), "info")
                match_label = str(raw.get("match_label", ""))
                capability_id, title = ALERT_TEMPLATES[match_label]
            except (KeyError, TypeError, ValueError):
                continue
            # 已被显式处置的不再进入分档（`escalation resolve` 写 resolved_at）。
            # 现在 act 的真源是 main_only handoff、不再是标签，所以这一条只影响
            # watch/done 的呈现——保留是因为「已经有人处置过」在任何档位都不该
            # 再重复出现，不是为了灭红点。
            if raw.get("resolved_at"):
                continue
            # Drills and link-checks are the operator talking to themselves.
            # The emitter already marks them (`escalation emit --drill`), but
            # until 2026-07-28 nothing downstream read the flag, so a probe
            # self-test sat in "要你出手" at critical severity next to real
            # failures. An alert channel that cries wolf about its own test
            # traffic spends the credibility it exists to hold.
            if raw.get("is_drill") is True:
                continue
            message = raw.get("message")
            decision_kind = raw.get("human_decision_kind")
            requires_owner = bool(
                raw.get("requires_human") is True
                and decision_kind in HUMAN_DECISION_KINDS
            )
            subject = _alert_subject(message, raw.get("agent_id"))
            group = groups.get((match_label, subject))
            if group is None:
                groups[(match_label, subject)] = _AlertGroup(
                    match_label=match_label, subject=subject, capability_id=capability_id,
                    title=title, severity=severity, first_seen=occurred, last_seen=occurred,
                    message=message if isinstance(message, str) else None, occurrence_count=1,
                    requires_human=requires_owner,
                    human_decision_kind=(
                        str(decision_kind) if requires_owner else None
                    ),
                )
            else:
                group.observe(
                    occurred,
                    severity,
                    message,
                    requires_human=requires_owner,
                    human_decision_kind=(
                        str(decision_kind) if requires_owner else None
                    ),
                )

        heartbeats = self._recovery_heartbeats()
        units = self._recovery_units()
        agent_successes = self._recovery_agent_successes()
        subject_closures = usage_sources.collect_subject_closures(
            self.paths.work_items, self.paths.handoffs
        )
        action_coverage_errors: List[str] = []
        if self.paths.wi_facts is not None:
            # Native WorkItems carry human requests as states; legacy handoffs are history.
            facts, _ = self.native_wi_facts()
            pending_main_only = wi_facts.human_requests(facts, self.now) if facts is not None else None
        else:
            pending_main_only = usage_sources.collect_main_only_pending(
                self.paths.handoffs, self.now, self.paths.work_items,
                coverage_errors=action_coverage_errors,
            )
        manual_subjects = {
            str(subject)
            for row in pending_main_only or []
            for subject in (row.get("business_subject"), row.get("id"))
            if subject
        }
        ordered = sorted(groups.values(), key=lambda item: item.last_seen, reverse=True)
        eligible_groups: List[Tuple[_AlertGroup, str]] = []
        for group in ordered:
            # Terminal WI/HO authority is stronger than historical event metadata.
            # In particular, an executor_reaped receipt emitted while a main_only
            # WI was open can retain requires_human=true forever in the append-only
            # stream.  Once the ticket closes it is history, never a fresh owner
            # decision.  Preserve it in ``done`` rather than deleting evidence.
            if subject_closures and group.subject in subject_closures:
                tier = TIER_DONE
            else:
                tier = group.tier(
                    self.now,
                    heartbeats,
                    units,
                    agent_successes,
                    subject_closures,
                )
            # One business obligation gets one owner action.  The canonical
            # main_only card carries the current WI/handoff entry point; a
            # structured executor-reaped or other receipt for that same
            # subject is supporting signal, not a second action card.
            if tier == TIER_ACT and group.subject in manual_subjects:
                continue
            eligible_groups.append((group, tier))

        truncated = max(0, len(eligible_groups) - MAX_ALERT_GROUPS)
        tier_counts = {TIER_ACT: 0, TIER_WATCH: 0, TIER_DONE: 0}
        recovered_count = 0
        emitted_group_count = 0
        for group, tier in eligible_groups[:MAX_ALERT_GROUPS]:
            tier_counts[tier] += 1
            emitted_group_count += 1
            if group.recovered_at(heartbeats, units, agent_successes, subject_closures) is not None:
                recovered_count += 1
            event_id = f"alert-{_short_hash((group.match_label, group.subject))}"
            collection.events.append(IncidentEvent(
                event_id=event_id, kind="event_only", capability_id=group.capability_id,
                condition_key="event_only", scope="structured escalation event",
                normalized_scope_key=_short_hash(event_id), severity=group.severity,
                title=group.title, occurred_at=group.last_seen, reason=group.reason(),
                tier=tier,
            ))

        # 「要你出手」的真源：main_only 未闭环的 handoff（大鸟 2026-07-28
        # 「main_only 为 true，必须大鸟参与，无论 wi 属于谁」）。它不来自
        # escalation 标签——那一族全是「分身不敢往下走」，该我接过来做。
        for row in pending_main_only or []:
            tier_counts[TIER_ACT] += 1
            business_subject = str(row.get("business_subject") or row["id"])
            event_id = f"mainonly-{_short_hash((business_subject,))}"
            days = row.get("days")
            waited = f"挂 {days} 天" if isinstance(days, int) else "今天新建"
            # 往返轮次：一件事来回谈了几张单。1 轮不写——没往返就没信息量,
            # 写「往返 1 轮」只是噪音。>1 才说,且它正是大鸟要的那句「这事
            # 谈了 7 个来回还没完」（2026-08-02 折叠）。
            rounds = row.get("rounds")
            if isinstance(rounds, int) and rounds > 1:
                waited = f"{waited} · 往返 {rounds} 轮"
            # 卡片四件事（大鸟 2026-07-28 指定的列）：大意当标题、谁→谁、挂多久、单号。
            # 标题原先三张卡一律写死「要你参与的协作」——最大的那行字零信息量,
            # 读者必须读完小字才知道是什么事。现在标题就是那句大意（wi title）,
            # 取不到才退回 expected_output 首行。
            gist = str(row.get("gist") or "").strip()
            asks = str(row.get("asks") or "").strip()
            title = gist or asks or "要你参与的协作"
            # 正文回答「要我做什么」,标题回答「这是什么事」,两句就够决定
            # 「现在处理还是待会」。砍到 60 字：原先整段铺开正是大鸟说的
            # 「不容易理解」——四段式验收规格不是给他读的,要逐条看去开 handoff。
            #
            # 标题已经用掉 asks（没 wi title 那种）时不重复铺一遍,改说等谁。
            # 正文**不得为空**：App 侧 testEveryActAlertIsActionable 要求每张
            # act 卡都带「自己的话」,空正文既让测试红、也让卡片只剩一句标题。
            if gist and asks:
                body = usage_sources.first_line(asks, limit=60)
            elif row.get("ack_state") == "no_handoff":
                # 裸 main_only wi：没有 handoff 可开,别让正文指一个不存在的
                # 去处。要动手就是去开这张 wi 本身(2026-08-05)。
                body = f"{row['from_owner']} 名下,尚未发出 handoff；要动手去开这张 wi"
            else:
                body = f"等 {row['to_owner']} 回应；要逐条看去开这张 handoff"
            collection.events.append(IncidentEvent(
                event_id=event_id, kind="event_only",
                capability_id="notify.action_package",
                condition_key="event_only", scope="main_only handoff",
                normalized_scope_key=_short_hash(event_id),
                severity="warning",
                title=title,
                occurred_at=self.now,
                reason=(
                    # 裸 wi 两端 owner 同值,写「ken→ken」是噪音,直接报归属。
                    (f"{row['from_owner']} 名下"
                     if row.get("ack_state") == "no_handoff"
                     else f"{row['from_owner']}→{row['to_owner']}")
                    + f" · {waited} · {business_subject}"
                    + (f" · {row['id']}" if row["id"] != business_subject else "")
                    + f"｜{body}"
                ),
                tier=TIER_ACT,
                work_item_id=business_subject if re.fullmatch(r"wi_\d{8}_\d{3}", business_subject) else None,
            ))

        kept = emitted_group_count + len(pending_main_only or [])
        reason = (
            f"{kept} 件事（{raw_accepted} 条原始事件收敛）"
            f" · 要你出手 {tier_counts[TIER_ACT]}"
            f" · 我在盯 {tier_counts[TIER_WATCH]}"
            f" · 已过去 {tier_counts[TIER_DONE]}"
        )
        if heartbeats is None and units is None and agent_successes is None:
            # Say so rather than fail quietly: with no signal readable at all
            # the tiers are the older, blunter age-only judgement.
            reason = f"{reason}（恢复信号都读不到，自愈降档未生效）"
        elif recovered_count:
            reason = f"{reason}（{recovered_count} 件已自愈降档）"
        if truncated:
            reason = f"{reason}，已截断 {truncated} 件（只保留最近的 {MAX_ALERT_GROUPS} 件）"
        if missing_current:
            action_coverage_errors.append("current_escalations_unreadable")
        if pending_main_only is None:
            action_coverage_errors.append("human_requests_unavailable")
        action_complete = not action_coverage_errors
        if not action_complete:
            reason += " · 部分请求来源不可读，已展示可读记录；不能据空集合判断没有待办"
        collection.provenance_sources.append(_source("escalation-events",
            "fresh" if not missing_current else "unknown", self.now,
            None if not missing_current else "current_shard_unavailable"))
        collection.capabilities.append(_capability(
            "notify.action_package", health="ok" if action_complete else "unknown",
            freshness="fresh" if action_complete else "unknown", observed_at=self.now,
            max_age_seconds=97200, reason=reason,
            implementation_status="active_partial", aggregation_role="coverage_only",
            evidence=("source:escalation-events",),
        ))
        collection.source_cursors["escalation-events"] = f"{path.name}:{len(lines)}"
        collection.source_watermarks["escalation-events"] = self.now.isoformat()

    # ------------------------------------------------------------------
    # v2-A metric layer (spec pulse-stats-menubar-v2 §3 / §4)
    # ------------------------------------------------------------------

    def _safe_codex_remote(self) -> Optional[str]:
        try:
            return self.codex_remote_fetch()
        except Exception:  # noqa: BLE001 - a peer-machine failure must never break the snapshot
            return None

    def _safe_cc_quota(self) -> Optional[Mapping[str, object]]:
        return usage_sources.collect_cc_quota(self.cc_quota_fetch)

    def _safe_http(self, url: str) -> Optional[Mapping[str, object]]:
        try:
            return self.http_fetch(url)
        except Exception:  # noqa: BLE001 - loopback failure degrades to unknown
            return None

    def _collect_relay_metrics(self, collection: Collection, codex_remote_future, cc_quota_future=None) -> None:
        """§3.1 Relay: CC process counts, bots, 429 state and Codex quota."""
        cc_raw = self._safe_http(RELAY_CC_STATUS_URL)
        health_raw = self._safe_http(RELAY_HEALTH_URL)
        cc = usage_sources.collect_cc_status(cc_raw)
        health = usage_sources.collect_health(health_raw)
        endpoint_at = self.now if (cc_raw or health_raw) else None

        for metric_id, value, unit in (
            ("relay.cc.processes", cc["processes"], "count"),
            ("relay.cc.busy", cc["busy"], "count"),
            ("relay.cc.idle", cc["idle"], "count"),
            ("relay.bots_online", health["bots_online"], "count"),
            ("relay.uptime_seconds", health["uptime_seconds"], "seconds"),
        ):
            collection.metrics.append(_metric(
                metric_id, "s1.request_trace_capture", value=value, unit=unit,
                observed_at=endpoint_at if value is not None else None,
                reason=None if value is not None else "Relay internal endpoint was unavailable",
            ))

        rate_limited = usage_sources.detect_cc_rate_limited(self.paths.cc_projects, self.now)
        collection.metrics.append(_metric(
            "relay.cc.rate_limited", "s1.request_trace_capture", value=rate_limited, unit="boolean",
            observed_at=self.now if rate_limited is not None else None, window="24h",
            reason=None if rate_limited is not None else "CC transcript directory was unavailable",
        ))

        if self.paths.codex_quota is not None:
            from .codex_quota import load_quota
            quota, error = load_quota(self.paths.codex_quota, self.now)
            weekly = quota['weekly'] if quota else {}
            observed = _parse_date(quota['observed_at']) if quota else None
            for metric_id, key, unit in (
                ('relay.codex.used_percent','used_percent','percent'),
                ('relay.codex.resets_at','resets_at','unix_seconds'),
            ):
                collection.metrics.append(_metric(metric_id,'s1.request_trace_capture',
                    value=weekly.get(key),unit=unit,observed_at=observed,
                    scope='Codex account; codex bucket; 10080-minute window',
                    reason=error,window='provider_week'))
        else:
            try:
                remote_stdout = codex_remote_future.result()
            except Exception:  # noqa: BLE001
                remote_stdout = None
            quota = usage_sources.collect_codex(
                self.paths.codex_sessions,
                self.now,
                remote_stdout=remote_stdout,
                probe=self.codex_probe,
            )

            if quota is None:
                reason = "No Codex quota record was readable on either machine"
                for metric_id, unit in (
                    ("relay.codex.used_percent", "percent"),
                    ("relay.codex.resets_at", "unix_seconds"),
                ):
                    collection.metrics.append(_metric(
                        metric_id, "s1.request_trace_capture", value=None, unit=unit,
                        observed_at=None, reason=reason,
                    ))
            else:
                observed = quota["observed_at"]
                collection.metrics.append(_metric(
                    "relay.codex.used_percent", "s1.request_trace_capture",
                    value=quota["used_percent"], unit="percent", observed_at=observed,
                    scope=f"codex plan {quota.get('plan_type') or 'unknown'}",
                    max_age_seconds=CODEX_QUOTA_MAX_AGE_SECONDS, now=self.now,
                ))
                collection.metrics.append(_metric(
                    "relay.codex.resets_at", "s1.request_trace_capture",
                    value=quota["resets_at"], unit="unix_seconds", observed_at=observed,
                    reason=None if quota["resets_at"] is not None else "Reset timestamp was absent",
                    max_age_seconds=CODEX_QUOTA_MAX_AGE_SECONDS, now=self.now,
                ))

        if self.paths.cc_quota is not None:
            self._append_cc_quota_file_metrics(collection)
        else:
            self._append_cc_quota_metrics(collection, cc_quota_future)
        if self.paths.grok_quota is not None:
            self._append_grok_quota_metrics(collection)
        if self.paths.agy_quota is not None:
            self._append_agy_quota_metrics(collection)

    def _append_agy_quota_metrics(self, collection: Collection) -> None:
        """agy account quota groups as last read by the cost publisher.

        One row per bucket whose window has not reset: group, window
        (``weekly`` / ``5h``), remaining fraction and reset time. An
        unavailable fact leaves the value unknown with its reason and keeps the
        time of the failed read; a stale reading has neither value nor time.
        Not per-agent cost and never part of a cost total.
        """
        from .agy_quota import current_buckets, load_quota, observed_at
        quota, error = load_quota(self.paths.agy_quota, self.now)
        rows = current_buckets(quota, self.now) if quota and error is None else None
        if rows == []:
            rows, error = None, 'quota_window_elapsed'
        collection.metrics.append(_metric('relay.agy.quota', 's1.request_trace_capture',
            value=rows, unit='buckets', observed_at=observed_at(quota) if quota else None,
            scope='agy account quota groups; not per-agent cost', reason=error,
            sample_count=len(rows) if rows else None))

    def _append_grok_quota_metrics(self, collection: Collection) -> None:
        """Grok account weekly pool as last read by the cost publisher.

        Not per-bot cost and never part of a cost total. A not-applicable or
        unavailable fact, or a reading outside its period, leaves every value
        unknown with the stable reason, never 0; the read time is kept.
        """
        from .grok_quota import load_quota, observed_at
        quota, error = load_quota(self.paths.grok_quota, self.now)
        weekly = quota.get('weekly', {}) if quota and error is None else {}
        observed = observed_at(quota) if quota else None
        scope = 'Grok account weekly pool; not per-bot cost'
        for metric_id, key, unit in (
            ('relay.grok.used_percent', 'used_percent', 'percent'),
            ('relay.grok.resets_at', 'resets_at', 'unix_seconds'),
            ('relay.grok.period_start', 'period_start', 'unix_seconds'),
        ):
            collection.metrics.append(_metric(metric_id, 's1.request_trace_capture',
                value=weekly.get(key), unit=unit, observed_at=observed,
                scope=scope, reason=error, window='provider_week'))
        label = quota.get('plan_label') if quota else None
        collection.metrics.append(_metric('relay.grok.plan_label', 's1.request_trace_capture',
            value=label, unit='text', observed_at=observed if label else None,
            scope=scope, reason=None if label else error or 'plan_label_unavailable',
            window='provider_week'))

    def _append_cc_quota_file_metrics(self, collection: Collection) -> None:
        """CC weekly quota as last observed by Relay on a real CC turn.

        Valid only inside its current provider window (see ``cc_quota``);
        otherwise every figure is unknown with the stable reason, never 0.
        """
        from .cc_quota import five_hour, load_quota
        quota, error = load_quota(self.paths.cc_quota, self.now)
        weekly = quota['weekly'] if quota else {}
        observed = _parse_date(quota['observed_at']) if quota else None
        for metric_id, key, unit in (
            ('relay.cc.used_percent', 'used_percent', 'percent'),
            ('relay.cc.resets_at', 'resets_at', 'unix_seconds'),
        ):
            collection.metrics.append(_metric(metric_id, 's1.request_trace_capture',
                value=weekly.get(key), unit=unit, observed_at=observed,
                scope='CC subscription; seven_day window', reason=error, window='provider_week'))
        five, five_error = five_hour(quota, self.now) if quota else (None, error)
        collection.metrics.append(_metric('relay.cc.5h_used_percent', 's1.request_trace_capture',
            value=five, unit='percent', observed_at=observed if five is not None else None,
            scope='CC subscription; five_hour window', reason=five_error, window='5h'))

    def _append_cc_quota_metrics(self, collection: Collection, cc_quota_future) -> None:
        """CC subscription quota, read from API rate-limit response headers.

        Symmetric with the Codex quota above: an unobservable quota yields
        ``unknown`` metrics, never 0. The OAuth token used to obtain these
        numbers is confined to the fetch call and never reaches the snapshot.
        """
        cc_quota: Optional[Mapping[str, object]] = None
        if cc_quota_future is not None:
            try:
                cc_quota = cc_quota_future.result()
            except Exception:  # noqa: BLE001 - a quota probe must never break the snapshot
                cc_quota = None

        if cc_quota is None:
            # 拿不到就退回上次读到的那个数，而不是整格变「未知」。
            # 令牌每 8 小时过期一次，且过期后要等下一次 CC 真正发请求才被刷回
            # （2026-07-29 实测 05:55 过期、09:37 才刷新），空窗期不确定多长。
            # 此前空窗期内把一个几分钟前还好好的读数整个丢掉——额度是慢变量，
            # 「8 分钟前 82%」远比「未知」有用。观测时刻照实写，由新鲜度闸
            # （CC_QUOTA_MAX_AGE_SECONDS）判老，不假装它是现值。
            cached = self.cc_quota_cache_read()
            if cached is not None:
                observed = cached["observed_at"]
                collection.metrics.append(_metric(
                    "relay.cc.used_percent", "s1.request_trace_capture",
                    value=cached["used_percent"], unit="percent", observed_at=observed,
                    window="7d", scope="CC subscription weekly quota",
                    max_age_seconds=CC_QUOTA_MAX_AGE_SECONDS, now=self.now,
                ))
                collection.metrics.append(_metric(
                    "relay.cc.resets_at", "s1.request_trace_capture",
                    value=cached["resets_at"], unit="unix_seconds", observed_at=observed,
                    window="7d", max_age_seconds=CC_QUOTA_MAX_AGE_SECONDS, now=self.now,
                ))
                five_hour_cached = cached["five_hour_used_percent"]
                collection.metrics.append(_metric(
                    "relay.cc.5h_used_percent", "s1.request_trace_capture",
                    value=five_hour_cached, unit="percent",
                    observed_at=observed if five_hour_cached is not None else None,
                    window="5h", scope="CC subscription rolling 5-hour quota",
                    max_age_seconds=CC_QUOTA_MAX_AGE_SECONDS, now=self.now,
                ))
                return

            # Name the cause when the credential file can prove it. "未知" alone
            # sent me chasing a phantom regression on 2026-07-28: the token had
            # expired at 13:23 and my unrelated commit landed 13:33, so the
            # panel's silence read as "you broke it". The stamp was there all
            # along. An expiry that has not passed leaves the generic reason --
            # then the token is fine and the fault is elsewhere.
            reason = "CC quota headers were unavailable"
            expiry = self.cc_token_expiry()
            if expiry is not None and expiry <= self.now:
                local = expiry.astimezone(DISPLAY_TZ).strftime("%m-%d %H:%M")
                reason = f"CC 登录令牌已过期（{local} 到期），需重新登录"
            for metric_id, unit in (
                ("relay.cc.used_percent", "percent"),
                ("relay.cc.resets_at", "unix_seconds"),
                ("relay.cc.5h_used_percent", "percent"),
            ):
                collection.metrics.append(_metric(
                    metric_id, "s1.request_trace_capture", value=None, unit=unit,
                    observed_at=None, reason=reason,
                ))
            return

        # 读到了就落盘，供下次令牌空窗期回退。写失败不影响本次快照。
        self.cc_quota_cache_write(cc_quota, self.now)

        collection.metrics.append(_metric(
            "relay.cc.used_percent", "s1.request_trace_capture",
            value=cc_quota["used_percent"], unit="percent", observed_at=self.now,
            window="7d", scope="CC subscription weekly quota",
        ))
        collection.metrics.append(_metric(
            "relay.cc.resets_at", "s1.request_trace_capture",
            value=cc_quota["resets_at"], unit="unix_seconds", observed_at=self.now,
            window="7d",
            reason=None if cc_quota["resets_at"] is not None else "Reset timestamp was absent",
        ))
        five_hour = cc_quota["five_hour_used_percent"]
        collection.metrics.append(_metric(
            "relay.cc.5h_used_percent", "s1.request_trace_capture",
            value=five_hour, unit="percent", observed_at=self.now if five_hour is not None else None,
            window="5h", scope="CC subscription rolling 5-hour quota",
            reason=None if five_hour is not None else "Five-hour utilization header was absent",
        ))

    def _collect_usage_metrics(self, collection: Collection) -> None:
        """§3.2 today's usage aggregated from the obs event stream."""
        today_window = "today_cst"
        today_scope = "Beijing calendar day"
        usage = usage_sources.collect_usage(self.paths.obs, self.now)
        if self.paths.emit_legacy_cost_metrics:
            collection.metrics.extend(billing.build_billing_metrics(
                self.paths.subscriptions, self.paths.obs, self.now,
            ))

        # 本月各 Agent 用掉多少输出 token，以及按占比估算的分摊。
        #
        # 与上面的 billing.mtd.* 各答各的：那套回答「$270 由哪些组件构成」，
        # 严格到分且拒绝无证据的归因；这套回答「token 花在谁身上」，数据源是
        # obs 的 turn.end，一直都在。2026-07-30 后者被前者顶掉后整块消失
        # （0ef3260），面板上「每个 Agent 花了多少」就此没了下文——大鸟 07-31
        # 指出这一点，此处恢复。分摊值是按输出占比的估算，不是账单归因，
        # 呈现侧必须说清区别。
        if self.paths.emit_legacy_cost_metrics:
            mtd = usage_sources.collect_month_to_date(self.paths.obs, self.now)
            collection.metrics.append(_metric(
                "usage.mtd.by_agent", "s1.request_trace_capture",
                value=(mtd or {}).get("by_agent"), unit="list",
                observed_at=self.now if mtd else None, window="month_to_date_cst",
                sample_count=(mtd or {}).get("days_covered"),
                scope="subscription amortized by share of output tokens",
                reason=None if mtd else "obs directory unavailable",
            ))
            for metric_id, key, unit in (
                ("usage.mtd.output_tokens", "total_output_tokens", "tokens"),
                ("usage.mtd.days_covered", "days_covered", "count"),
                ("usage.mtd.subscription_usd", "subscription_usd", "usd"),
            ):
                collection.metrics.append(_metric(
                    metric_id, "s1.request_trace_capture",
                    value=(mtd or {}).get(key), unit=unit,
                    observed_at=self.now if mtd else None, window="month_to_date_cst",
                    reason=None if mtd else "obs directory unavailable",
                ))
        if usage is None:
            for metric_id, unit in (
                ("usage.today.input_tokens", "tokens"),
                ("usage.today.output_tokens", "tokens"),
                ("usage.today.turns", "count"),
                ("usage.today.failures", "count"),
                ("usage.today.inbound", "count"),
                ("usage.today.duration_p50", "milliseconds"),
                ("usage.hourly.output_tokens", "tokens"),
                ("usage.by_agent", "list"),
                ("usage.by_session", "list"),
                ("delivery.today.sent", "count"),
                ("delivery.today.attempts_histogram", "map"),
            ):
                collection.metrics.append(_metric(
                    metric_id, "s1.request_trace_capture", value=None, unit=unit,
                    observed_at=None, window=today_window, scope=today_scope,
                    reason="No observability stream was readable for the current Beijing calendar day",
                ))
            return

        turns = usage["turns"]
        usage_quality = "descriptive" if usage["malformed_lines"] else "validated"
        for metric_id, value, unit, samples in (
            ("usage.today.input_tokens", usage["input_tokens"], "tokens", turns),
            ("usage.today.input_tokens_uncached", usage["input_tokens_uncached"], "tokens", turns),
            ("usage.today.cache_read_tokens", usage["cache_read_tokens"], "tokens", turns),
            ("usage.today.cache_creation_tokens", usage["cache_creation_tokens"], "tokens", turns),
            ("usage.today.output_tokens", usage["output_tokens"], "tokens", turns),
            ("usage.today.turns", turns, "count", turns),
            ("usage.today.failures", usage["failures"], "count", turns),
            ("usage.today.inbound", usage["inbound"], "count", usage["inbound"]),
            ("usage.today.degen_observed", usage["degen_observed"], "count", None),
        ):
            collection.metrics.append(_metric(
                metric_id, "s1.request_trace_capture", value=value, unit=unit,
                observed_at=self.now, window=today_window, sample_count=samples,
                quality=usage_quality, scope=today_scope,
            ))

        collection.metrics.append(_metric(
            "usage.today.duration_p50", "s1.request_trace_capture",
            value=usage["duration_p50_ms"], unit="milliseconds", observed_at=self.now,
            window=today_window, sample_count=turns, quality=usage_quality,
            scope=f"{today_scope}; median of turn.end durationMs",
            reason=None if usage["duration_p50_ms"] is not None else "No completed turns carried a duration",
        ))
        collection.metrics.append(_metric(
            "usage.hourly.output_tokens", "s1.request_trace_capture",
            value=usage["hourly_output_tokens"], unit="tokens", observed_at=self.now,
            window=today_window, quality=usage_quality,
            scope=f"{today_scope}; 24 Beijing hour buckets",
        ))
        collection.metrics.append(_metric(
            "usage.by_agent", "s1.request_trace_capture", value=usage["by_agent"], unit="list",
            observed_at=self.now, window=today_window, quality=usage_quality,
            scope=f"{today_scope}; top agents by output tokens",
        ))
        collection.metrics.append(_metric(
            "usage.by_session", "s1.request_trace_capture", value=usage["by_session"], unit="list",
            observed_at=self.now, window=today_window, quality=usage_quality,
            scope=f"{today_scope}; top sessions by output tokens",
        ))

        collection.metrics.append(_metric(
            "delivery.today.sent", "s1.request_trace_capture", value=usage["delivery_sent"],
            unit="count", observed_at=self.now, window=today_window,
            sample_count=usage["delivery_total"], quality=usage_quality,
            scope=f"{today_scope}; delivery receipts with status sent",
        ))
        collection.metrics.append(_metric(
            "delivery.today.attempts_histogram", "s1.request_trace_capture",
            value=usage["delivery_attempts_histogram"], unit="map", observed_at=self.now,
            window=today_window, sample_count=usage["delivery_total"], quality=usage_quality,
            scope=f"{today_scope}; delivery receipt attempts",
        ))
        collection.metrics.append(_metric(
            "usage.today.inbound_types", "s1.request_trace_capture", value=usage["inbound_types"],
            unit="map", observed_at=self.now, window=today_window, quality=usage_quality,
            scope=f"{today_scope}; inbound message types; sender identities are never included",
        ))
        if usage["malformed_lines"]:
            collection.metrics.append(_metric(
                "usage.today.malformed_lines", "s1.request_trace_capture",
                value=usage["malformed_lines"], unit="count", observed_at=self.now,
                window=today_window, quality="validated",
                scope=(
                    f"{today_scope}; candidate UTC source files; "
                    "malformed rows have no event-day attribution"
                ),
                reason=(
                    "Unparseable observability lines in candidate UTC source files "
                    "were skipped; malformed rows have no event-day attribution"
                ),
            ))

    def _collect_harness_metrics(self, collection: Collection) -> None:
        """§3.3 work-item counters, stalled list and pending handoffs."""
        if self.paths.wi_facts is not None:
            self._collect_native_wi_metrics(collection)
            return
        harness = usage_sources.collect_harness(self.paths.work_items, self.paths.handoffs, self.now)
        unreadable = int(harness["unreadable_work_items"]) + int(harness["unreadable_handoffs"])
        quality = "descriptive" if unreadable else "validated"

        # window 标的必须是**实际算的那个窗口**。day_* 三项原标成 UTC day,
        # 但 collect_harness 早已按 DISPLAY_TZ(北京)算——标签停在旧判据上,
        # 北京 00:00-08:00 那八小时读者会以为看的是昨天。
        # iso_week_* = 自然周(周一清零),week_created = 滚动 7 天,两者并列且各标各的。
        for metric_id, key, unit, window in (
            ("harness.wi.week_created", "week_created", "count", "rolling_7d"),
            ("harness.wi.iso_week_created", "iso_week_created", "count", "iso_week_cst"),
            ("harness.wi.iso_week_completed", "iso_week_completed", "count", "iso_week_cst"),
            ("harness.wi.day_created", "day_created", "count", "today_cst"),
            ("harness.wi.day_completed", "day_completed", "count", "today_cst"),
            ("harness.handoff.pending", "handoff_pending", "count", "current"),
        ):
            value = harness[key]
            collection.metrics.append(_metric(
                metric_id, "s2.ledger_invariants", value=value, unit=unit,
                observed_at=self.now if value is not None else None, window=window, quality=quality,
                reason=None if value is not None else "Harness ledger directory was unavailable",
            ))

        stalled = harness["stalled"]
        collection.metrics.append(_metric(
            "harness.wi.stalled", "s2.ledger_invariants", value=stalled, unit="list",
            observed_at=self.now if stalled is not None else None, window="current", quality=quality,
            sample_count=len(stalled) if stalled is not None else None,
            scope=f"open work items untouched for more than {WI_STALLED_DAYS} days",
            reason=None if stalled is not None else "Harness work-item directory was unavailable",
        ))

        # Effective workflow profile includes sealed same-WI promotions from SCL.
        # Never infer transitions from WI ceremony fields. Consume only canonical
        # read-model bytes matching all four global-authority materializations.
        try:
            workflow_profiles = load_workflow_profiles(
                self.paths.global_authority,
                self.paths.canonical_read_model,
                self.paths.surface_registry,
                now=self.now,
                expected_authority_revision=authority_revision_for_git(
                    collection.source_revisions.get("harness")
                ),
            )
        except GlobalAuthorityCarrierError:
            workflow_profiles = {}

        # 「在飞项」保留全部非终态 WI；App 再按行内信号切成展开的
        # 人工层与折叠的自动层。在 exporter 先过滤会让巡检门自证。
        in_flight = usage_sources.collect_in_flight(
            self.paths.work_items,
            self.paths.handoffs,
            self.now,
            policies_dir=self.paths.policies,
            events_dir=self.paths.alerts_dir,
            workflow_profiles=workflow_profiles,
        )
        collection.metrics.append(_metric(
            "harness.wi.in_flight", "s2.ledger_invariants", value=in_flight, unit="list",
            observed_at=self.now if in_flight is not None else None,
            window="current", quality=quality,
            sample_count=len(in_flight) if in_flight is not None else None,
            scope="all non-terminal work items with human and automation disposition signals",
            reason=None if in_flight is not None else "Harness work-item directory was unavailable",
        ))

        if unreadable:
            collection.metrics.append(_metric(
                "harness.unreadable_records", "s2.ledger_invariants", value=unreadable,
                unit="count", observed_at=self.now, quality="validated",
                reason="Malformed YAML records were skipped during aggregation",
            ))

    def _collect_native_wi_metrics(self, collection: Collection) -> None:
        """WI counters and in-flight rows from the native core's journal facts."""
        facts, reason = self.native_wi_facts()
        harness = wi_facts.counters(facts, self.now) if facts is not None else None
        rows = wi_facts.in_flight(facts, self.now) if facts is not None else None
        quality = "validated" if facts is not None and facts.get("coverage") == "complete" else "descriptive"
        missing = f"Native WorkItem facts unavailable: {reason}"
        for metric_id, key, unit, window in (
            ("harness.wi.week_created", "week_created", "count", "rolling_7d"),
            ("harness.wi.iso_week_created", "iso_week_created", "count", "iso_week_cst"),
            ("harness.wi.iso_week_completed", "iso_week_completed", "count", "iso_week_cst"),
            ("harness.wi.day_created", "day_created", "count", "today_cst"),
            ("harness.wi.day_completed", "day_completed", "count", "today_cst"),
            ("harness.wi.month_created", "month_created", "count", "month_cst"),
            ("harness.wi.day_closed", "day_closed", "count", "today_cst"),
            ("harness.wi.iso_week_closed", "iso_week_closed", "count", "iso_week_cst"),
            ("harness.wi.month_closed", "month_closed", "count", "month_cst"),
        ):
            value = harness[key] if harness is not None else None
            collection.metrics.append(_metric(
                metric_id, "s2.ledger_invariants", value=value, unit=unit,
                observed_at=self.now if value is not None else None, window=window, quality=quality,
                reason=None if value is not None else missing,
            ))
        stalled = harness["stalled"] if harness is not None else None
        collection.metrics.append(_metric(
            "harness.wi.stalled", "s2.ledger_invariants", value=stalled, unit="list",
            observed_at=self.now if stalled is not None else None, window="current", quality=quality,
            sample_count=len(stalled) if stalled is not None else None,
            scope=f"open work items untouched for more than {WI_STALLED_DAYS} days",
            reason=None if stalled is not None else missing,
        ))
        collection.metrics.append(_metric(
            "harness.wi.in_flight", "s2.ledger_invariants", value=rows, unit="list",
            observed_at=self.now if rows is not None else None,
            window="current", quality=quality,
            sample_count=len(rows) if rows is not None else None,
            scope="all non-terminal native work items",
            reason=None if rows is not None else missing,
        ))
        board = work_board.rows(facts, self.now) if facts is not None else None
        collection.metrics.append(_metric(
            work_board.METRIC_ID, "s2.ledger_invariants", value=board, unit="list",
            observed_at=self.now if board is not None else None, window="current", quality=quality,
            sample_count=len(board) if board is not None else None,
            scope="in-flight native work items and collaboration contracts, grouped by who moves next",
            reason=None if board is not None else missing,
        ))
        unreadable = harness["unreadable_work_items"] if harness is not None else 0
        if unreadable:
            collection.metrics.append(_metric(
                "harness.unreadable_records", "s2.ledger_invariants", value=unreadable,
                unit="count", observed_at=self.now, quality="validated",
                reason="Native journals that could not be replayed were reported, not counted",
            ))

    def _collect_revision(self, collection: Collection) -> None:
        try:
            marker = self.paths.repo / ".stats-source-revision"
            if marker.is_file():
                revision = marker.read_text().strip()
                if re.fullmatch(r"[0-9a-f]{40}", revision):
                    collection.source_revisions["pulse"] = revision
            else:
                result = self.runner.run(self.git_argv)
                revision = result.stdout.strip()
                if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,40}", revision):
                    collection.source_revisions["pulse"] = revision
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            result = self.runner.run(self.harness_git_argv)
            revision = result.stdout.strip()
            if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", revision):
                collection.source_revisions["harness"] = revision
        except (OSError, subprocess.SubprocessError):
            pass
