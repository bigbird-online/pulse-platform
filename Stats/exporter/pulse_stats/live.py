"""Fixed-source, one-shot production snapshot adapter."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Sequence

from .live_contract import (
    ACK_ARGV,
    BASELINE_VERSION,
    MAX_PAYLOAD_BYTES,
    SYSTEMD_ARGV,
    SYSTEMD_UNITS,
    CommandResult,
    CommandRunner,
    LivePaths,
    LiveSnapshotError,
    SubprocessCommandRunner,
    _capability,
    _source,
)
from . import usage_sources, platform_health, production_paths, automation, legacy_retirement, wi_facts
from .global_authority import (
    GlobalAuthorityCarrierError,
    load_global_authority_evidence,
)
from .live_sources import LiveCollector
from .producer import build_snapshot, project_global_authority
from .reducer import reduce_incident_events
from .state import IncidentStateStore


class LiveSnapshotService:
    def __init__(
        self,
        *,
        paths: LivePaths,
        runner: CommandRunner,
        now: datetime,
        max_payload_bytes: int = MAX_PAYLOAD_BYTES,
        ack_argv: Sequence[str] = ACK_ARGV,
        systemd_argv: Sequence[str] = SYSTEMD_ARGV,
        http_fetch=None,
        codex_remote_fetch=None,
        codex_probe=None,
        cc_quota_fetch=None,
        cc_token_expiry=None,
        cc_quota_cache_read=None,
        cc_quota_cache_write=None,
        platform_health_fetch=None,
        relay_mapping_valid=None,
        automation_fetch=None,
        live_clock=False,
    ):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        self.paths = paths
        self.automation_fetch = automation_fetch
        self.live_clock = live_clock
        self.platform_health_fetch = platform_health_fetch
        self.relay_mapping_valid = relay_mapping_valid
        self.runner = runner
        self.now = now
        self.max_payload_bytes = max_payload_bytes
        self.ack_argv = tuple(ack_argv)
        self.systemd_argv = tuple(systemd_argv)
        self.http_fetch = http_fetch
        self.codex_remote_fetch = codex_remote_fetch
        self.codex_probe = codex_probe
        self.cc_quota_fetch = cc_quota_fetch
        self.cc_token_expiry = cc_token_expiry
        self.cc_quota_cache_read = cc_quota_cache_read
        self.cc_quota_cache_write = cc_quota_cache_write

    @classmethod
    def default(
        cls,
        *,
        compute_cost_shadow_path: Path | None = None,
    ) -> "LiveSnapshotService":
        """Production wiring reads the independently refreshed account quota cache.

        """
        if compute_cost_shadow_path is not None:
            if not compute_cost_shadow_path.is_absolute():
                raise ValueError("compute_cost_shadow_path must be absolute")
            shadow_enabled = True
            shadow_path = compute_cost_shadow_path
        else:
            shadow_enabled = os.environ.get("PULSE_COMPUTE_COST_SHADOW_ENABLED") == "1"
            shadow_path_raw = os.environ.get("PULSE_COMPUTE_COST_SHADOW_PATH")
            shadow_path = (
                Path(shadow_path_raw)
                if shadow_path_raw and Path(shadow_path_raw).is_absolute()
                else None
            )
        reader = Path(__file__).resolve().parents[3]
        paths, mapping_valid = production_paths.resolve(LivePaths(
            retire_legacy_probes=True,
            emit_legacy_cost_metrics=False,
            relay_supervisor="launchd",
            repo=reader,
            registry=None,
            surface_registry=reader/"config/scl-surfaces.yaml",
            compute_cost_shadow_enabled=shadow_enabled,
            compute_cost_shadow=shadow_path,
            codex_quota=Path.home()/".local/state/pulse/stats/codex-quota-v1.json",
            cc_quota=Path.home()/".local/state/pulse/stats/cc-quota-v1.json",
            grok_quota=Path.home()/".local/state/pulse/stats/grok-quota-v1.json",
            agy_quota=Path.home()/".local/state/pulse/stats/agy-quota-v1.json",
            wi_facts=Path.home()/".local/state/pulse/stats/wi-facts-v1.json",
        ), Path.home()/".local/share/pulse/config/relay/paths.json")
        return cls(
            paths=paths,
            platform_health_fetch=platform_health.fetch,
            automation_fetch=automation.fetch,
            live_clock=True,
            relay_mapping_valid=mapping_valid,
            runner=SubprocessCommandRunner(),
            now=datetime.now(timezone.utc),
            codex_probe=lambda: False,
            codex_remote_fetch=lambda: None,
            # CC quota comes from the Relay-observed publication (paths.cc_quota);
            # the legacy credential header probe stays disabled.
            cc_quota_fetch=lambda: None,
            cc_token_expiry=lambda: None,
            cc_quota_cache_read=lambda: None,
            cc_quota_cache_write=lambda *args, **kwargs: None,
        )

    def render_json(self) -> str:
        store = IncidentStateStore(self.paths.state)
        with store.single_writer():
            loaded = store.load()
            existing = loaded.incidents if loaded.status == "ok" else ()
            collector = LiveCollector(
                paths=self.paths, runner=self.runner, now=self.now,
                ack_argv=self.ack_argv, systemd_argv=self.systemd_argv,
                http_fetch=self.http_fetch, codex_remote_fetch=self.codex_remote_fetch,
                codex_probe=self.codex_probe, cc_quota_fetch=self.cc_quota_fetch,
                cc_token_expiry=self.cc_token_expiry,
                cc_quota_cache_read=self.cc_quota_cache_read,
                cc_quota_cache_write=self.cc_quota_cache_write,
            )
            # Oikos and automation are independent read models. Starting both
            # before local collection overlaps the automation peer's SSH latency
            # with the local collector; App Refresh still never triggers
            # ComputeCost collection or any remote write.
            with ThreadPoolExecutor(max_workers=2) as auxiliary_pool:
                health_future = (auxiliary_pool.submit(self.platform_health_fetch)
                                 if self.platform_health_fetch is not None else None)
                automation_future = (auxiliary_pool.submit(self.automation_fetch)
                                      if self.automation_fetch is not None else None)
                collected = collector.collect(existing)

                # Oikos owns health/confirmation/route facts. Its failure degrades
                # only this read model, not the cost publication or SCL authority.
                if health_future is not None:
                    try:
                        raw_health = health_future.result()
                    except (OSError, ValueError):
                        raw_health = None
                    # The report can finish while collection is in progress.
                    # Compare against the receipt time, not the round's start.
                    if self.live_clock:
                        self.now = datetime.now(timezone.utc)
                    health_metrics, health_source = platform_health.metrics(raw_health, self.now)
                    collected.core_successes += int(health_source["freshness"] == "fresh")
                    collected.metrics.extend(health_metrics)
                    collected.provenance_sources.append(health_source)
                    collected.capabilities.append(_capability(
                        platform_health.CAPABILITY_ID, health="ok" if health_source["freshness"] == "fresh" else "unknown",
                        freshness=health_source["freshness"],
                        observed_at=platform_health.date(health_source["observed_at"]) if health_source["observed_at"] else None,
                        max_age_seconds=platform_health.MAX_AGE_SECONDS,
                        reason="Oikos measured compute-resource read model", aggregation_role="coverage_only",
                        implementation_status="active_validated", evidence=("source:oikos-platform-health",),
                    ))
            if self.relay_mapping_valid is not None:
                collected.provenance_sources.append(_source(
                    "relay-runtime-paths", "fresh" if self.relay_mapping_valid else "error",
                    self.now, error_code=None if self.relay_mapping_valid else "invalid_runtime_mapping",
                ))
            if automation_future is not None:
                try:
                    task_document = automation_future.result()
                except (OSError, ValueError):
                    task_document = None
                # The sources were just observed. Only the production clock advances.
                if self.live_clock:
                    self.now = datetime.now(timezone.utc)
                task_metrics, task_source = automation.metrics(task_document, self.now)
                collected.metrics.extend(task_metrics)
                collected.provenance_sources.append(task_source)
                collected.core_successes += int(task_source['freshness'] == 'fresh')
                collected.capabilities.append(_capability(
                    'automation.service_results', health='ok' if task_source['freshness']=='fresh' else 'unknown',
                    freshness=task_source['freshness'], observed_at=self.now,
                    max_age_seconds=automation.MAX_AGE, reason='原生任务结果；计划暂停不是故障。',
                    aggregation_role='coverage_only',implementation_status='active_partial',
                    evidence=('source:automation-native-sources',)))
            if collected.core_successes == 0:
                raise LiveSnapshotError("all core sources unavailable")
            native_authority = None
            if self.paths.wi_facts is not None:
                facts, reason = collector.native_wi_facts()
                native_authority = wi_facts.authority(facts, reason)
                global_authority_evidence = None
                collected.provenance_sources.append(_source(
                    "native-wi-facts",
                    "fresh" if facts is not None else "error",
                    self.now if facts is not None else None,
                    error_code=reason,
                ))
            else:
                try:
                    global_authority_evidence = load_global_authority_evidence(
                        self.paths.global_authority,
                        self.paths.surface_registry,
                    )
                except GlobalAuthorityCarrierError:
                    global_authority_evidence = None
                    collected.provenance_sources.append(_source(
                        "global-authority-carrier",
                        "error",
                        error_code="unavailable_or_invalid",
                    ))
                else:
                    authority_projection = project_global_authority(
                        global_authority_evidence,
                        generated_at=self.now,
                        source_revision=collected.source_revisions.get("harness"),
                    )
                    source_mismatch = (
                        "source_revision" in authority_projection["degraded_reasons"]
                    )
                    collected.provenance_sources.append(_source(
                        "global-authority-carrier",
                        (
                            str(authority_projection["freshness"])
                            if not source_mismatch
                            else "error"
                        ),
                        error_code=(
                            "source_revision_mismatch" if source_mismatch else None
                        ),
                    ))
            incidents, alerts = reduce_incident_events(collected.events, existing)
            incidents = [
                replace(
                    incident,
                    suggested_action=collected.incident_action_overrides.get(
                        incident.incident_key, incident.suggested_action
                    ),
                )
                for incident in incidents
            ]
            retired_history = [item for item in incidents if self.paths.retire_legacy_probes and legacy_retirement.is_retired(item)]
            active_incidents = [item for item in incidents if item not in retired_history]
            projection_complete = collected.projection_complete
            collected.capabilities.append(_capability(
                    "incident.current_state_projection",
                    health="ok" if projection_complete else "unknown",
                    freshness="fresh" if projection_complete else "error",
                    observed_at=self.now if projection_complete else None, max_age_seconds=None,
                    reason=("Incident projection sources were complete" if projection_complete else
                            "One or more incident projection sources were incomplete"),
                    implementation_status="active_partial", evidence=("state:incident-state-v1",),
            ))
            required_capability_ids = [str(item["id"]) for item in collected.capabilities]
            snapshot = build_snapshot(
                generated_at=self.now,
                # TTL is derived from the consumer's refresh cadence, not picked.
                # `RefreshPolicy.interval` (PulseStats/Core/RefreshPolicy.swift)
                # fetches every 30 minutes, so 45 = one full interval plus a 15
                # minute grace: "we missed an entire refresh cycle and still have
                # nothing newer" is the first moment stale is the honest word.
                #
                # It was 180 seconds until 2026-08-02, with no stated derivation
                # — and an undefended number is how it survived: the bar refreshes
                # every 30 minutes, so a 3 minute TTL meant ~90% of every cycle
                # rendered "数据已过期" over data that was working exactly as
                # designed. Whoever retunes `RefreshPolicy.interval` must retune
                # this with it; the two numbers are one decision in two files.
                expires_at=self.now + timedelta(minutes=45),
                capabilities=collected.capabilities,
                required_capability_ids=required_capability_ids,
                incidents=active_incidents,
                # Alerts are already collapsed per thing and capped at
                # MAX_ALERT_GROUPS by the collector, which also states in the
                # notify.action_package reason whenever that cap bit. A second
                # blind tail-slice here would silently drop things the operator
                # is meant to be able to review, so the collected set is passed
                # through whole.
                alerts=alerts,
                metrics=collected.metrics,
                provenance={
                    "producer": {"name": "pulse-stats-snapshot", "version": "0.1.0"},
                    "source_revisions": collected.source_revisions,
                    "sources": collected.provenance_sources,
                },
                baseline_version=BASELINE_VERSION,
                headline=(
                    "Incident state is unknown" if not projection_complete
                    else f"{sum(item.state == 'open' for item in active_incidents)} open incidents"
                ),
                runtime_health_override="unknown" if not projection_complete else None,
                global_authority_evidence=global_authority_evidence,
                global_authority=native_authority,
            )
            if retired_history:
                snapshot['retired_incidents'] = [dict(item.as_snapshot_dict(),
                    retirement={'date':'2026-09-18','reason':'旧探针已退役，不代表故障曾恢复。','source_ref':legacy_retirement.DECISION}) for item in retired_history]
            encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            if len(encoded.encode("utf-8")) > self.max_payload_bytes:
                raise LiveSnapshotError("snapshot payload exceeds 1 MiB")
            if loaded.status == "ok" or projection_complete:
                if loaded.status == "corrupt":
                    store.backup_corrupt(self.now.astimezone(timezone.utc))
                watermarks = dict(loaded.source_watermarks)
                watermarks.update(collected.source_watermarks)
                cursors = dict(loaded.source_cursors)
                cursors.update(collected.source_cursors)
                store.save(incidents, source_watermarks=watermarks, source_cursors=cursors)
            return encoded
